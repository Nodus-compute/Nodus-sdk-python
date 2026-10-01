"""Typed workload calls through the server's versioned operations interface."""

from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, Literal, TypedDict, TypeVar, cast

from .errors import APIError, ValidationError
from .types import Event, Output

if TYPE_CHECKING:
    from . import AsyncClient, AsyncWorkload, Client, Workload

_PATH = "/v1/operations/v1"
_WorkloadT = TypeVar("_WorkloadT")


@dataclass(frozen=True)
class OperationDefinition:
    """A server-defined operation and its argument schema."""

    id: str
    version: str
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any]
    required_scope: str
    transports: list[str]


@dataclass(frozen=True)
class OperationCatalog:
    """Operations available through the requested interface version."""

    version: str
    operations: list[OperationDefinition]


@dataclass(frozen=True)
class WorkloadPage(Generic[_WorkloadT]):
    """One workload page and the server's next offset."""

    workloads: list[_WorkloadT]
    next_offset: int | None


@dataclass(frozen=True)
class WorkloadValidation:
    """A validated request that has not started compute."""

    valid: bool
    submitted: bool
    workload: dict[str, Any]
    message: str


class RunDraftValues(TypedDict, total=False):
    """Explicitly saved form values without inferred defaults."""

    name: str
    command: str
    image: str
    gpu: str
    gpu_count: int
    total_gpu_count: int
    memory_gb: float
    vcpus: float
    disk_gb: float
    data_regions: list[str]
    max_cost_usd: float
    checkpoint_paths: list[str]
    result_paths: list[str]


class RunDraftPatch(TypedDict, total=False):
    """Partial form edits where None removes a saved field."""

    name: str | None
    command: str | None
    image: str | None
    gpu: str | None
    gpu_count: int | None
    total_gpu_count: int | None
    memory_gb: float | None
    vcpus: float | None
    disk_gb: float | None
    data_regions: list[str] | None
    max_cost_usd: float | None
    checkpoint_paths: list[str] | None
    result_paths: list[str] | None


@dataclass(frozen=True)
class RunDraft:
    """The member's saved run form at one server revision."""

    revision: int
    values: RunDraftValues
    updated_at: str | None = None


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise APIError("Operation returned an invalid response object")
    return value


def _rows(value: Any, name: str) -> list[dict[str, Any]]:
    body = _object(value)
    if name not in body:
        raise APIError(f"Operation response is missing {name}")
    rows = body[name]
    if rows is None:
        return []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise APIError(f"Operation returned invalid {name}")
    return rows


def _catalog(value: Any) -> OperationCatalog:
    body = _object(value)
    if body.get("version") != "v1":
        raise APIError("Operation catalog has an unsupported version")
    definitions = []
    for row in _rows(body, "operations"):
        if (row.get("version") != "v1"
                or any(not isinstance(row.get(key), str) for key in ("id", "name", "description", "required_scope"))
                or not isinstance(row.get("inputSchema"), dict)
                or not isinstance(row.get("annotations"), dict)
                or not isinstance(row.get("transports"), list)
                or any(not isinstance(item, str) for item in row["transports"])):
            raise APIError("Operation catalog contains an invalid definition")
        definitions.append(OperationDefinition(row["id"], row["version"], row["name"], row["description"],
                                               row["inputSchema"], row["annotations"], row["required_scope"], row["transports"]))
    return OperationCatalog("v1", definitions)


def _payload(workload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(workload, dict):
        raise ValidationError("workload must be a customer API request object")
    # Retries retain the same command, resources and budget despite caller edits.
    try:
        return json.loads(json.dumps(workload, allow_nan=False))
    except (TypeError, ValueError, OverflowError) as error:
        raise ValidationError("workload must contain JSON-compatible finite values") from error


def _id(workload_id: str) -> dict[str, str]:
    from . import _valid_id
    return {"workload_id": _valid_id(workload_id)}


def _optional(**arguments: Any) -> dict[str, Any]:
    return {name: value for name, value in arguments.items() if value is not None}


def _submission(workload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
    from . import _valid_idempotency_key
    if not isinstance(idempotency_key, str):
        raise ValidationError("An explicit idempotency_key is required")
    key = _valid_idempotency_key(idempotency_key)
    return {"workload": _payload(workload), "idempotency_key": key}


def _workload(client: Client | AsyncClient, value: Any, *, asynchronous: bool,
              expected_id: str | None = None, idempotency_key: str | None = None) -> Workload | AsyncWorkload:
    from . import AsyncWorkload, Workload, _valid_id
    invalid = False
    if not isinstance(value, dict):
        invalid = True
    else:
        identifier = value.get("id") or value.get("workload_id")
        try:
            _valid_id(identifier)
        except ValidationError:
            invalid = True
        invalid = invalid or (expected_id is not None and identifier != expected_id)
        invalid = invalid or ("id" in value and "workload_id" in value and value["id"] != value["workload_id"])
    if invalid:
        raise client._unreached(APIError, "Operation returned an invalid workload receipt", idempotency_key)
    result = AsyncWorkload(client) if asynchronous else Workload(client)
    result._absorb(value, authoritative=expected_id is not None)
    return result


def _page(client: Client | AsyncClient, value: Any, *, asynchronous: bool) -> WorkloadPage:
    rows = _rows(value, "workloads")
    offset = value.get("next_offset")
    if offset is not None and (type(offset) is not int or offset < 0):
        raise APIError("Operation returned an invalid next_offset")
    return WorkloadPage([_workload(client, row, asynchronous=asynchronous) for row in rows], offset)


def _validation(value: Any) -> WorkloadValidation:
    body = _object(value)
    if (body.get("valid") is not True or body.get("submitted") is not False
            or not isinstance(body.get("workload"), dict) or not isinstance(body.get("message"), str)):
        raise APIError("Operation did not confirm validation without submission")
    return WorkloadValidation(True, False, body["workload"], body["message"])


def _cancelled(value: Any) -> None:
    if _object(value).get("status") != "cancel_requested":
        raise APIError("Operation did not confirm cancellation was requested")


def _run_draft(value: Any) -> RunDraft:
    body = _object(value)
    revision, values, updated_at = body.get("revision"), body.get("values"), body.get("updated_at")
    if (type(revision) is not int or not 0 <= revision <= 9007199254740991 or not isinstance(values, dict)
            or (updated_at is not None and not isinstance(updated_at, str))):
        raise APIError("Operation returned an invalid run draft")
    for name, saved in values.items():
        valid = False
        if name in ("name", "command", "image", "gpu"):
            valid = isinstance(saved, str)
        elif name == "gpu_count":
            valid = type(saved) is int
        elif name == "total_gpu_count":
            valid = type(saved) is int and 1 <= saved <= 2048 and "gpu_count" not in values
        elif name in ("memory_gb", "max_cost_usd"):
            valid = type(saved) in (int, float) and (type(saved) is int or math.isfinite(saved))
        elif name in ("vcpus", "disk_gb"):
            if type(saved) in (int, float) and saved > 0:
                try:
                    valid = math.isfinite(saved)
                except OverflowError:
                    valid = False
        elif name == "data_regions":
            if isinstance(saved, list) and len(saved) <= 32 and all(isinstance(region, str) for region in saved):
                try:
                    valid = len(set(saved)) == len(saved) and all(
                        region and len(region.encode("utf-8")) <= 128 and "," not in region
                        and not any(char.isspace() or unicodedata.category(char) == "Cc" for char in region)
                        for region in saved)
                except UnicodeEncodeError:
                    valid = False
        elif name in ("checkpoint_paths", "result_paths"):
            valid = isinstance(saved, list) and all(isinstance(path, str) for path in saved)
        if not valid:
            raise APIError("Operation returned an invalid run draft field")
    return RunDraft(revision, cast(RunDraftValues, values), updated_at)


def _updated_run_draft(value: Any, arguments: dict[str, Any]) -> RunDraft:
    draft = _run_draft(value)
    if draft.revision != arguments["expected_revision"] + 1:
        raise APIError("Operation did not confirm the next run draft revision")
    for name, requested in arguments["patch"].items():
        matches = name not in draft.values if requested is None else name in draft.values and draft.values[name] == requested
        if not matches:
            raise APIError("Operation did not confirm the requested run draft edit")
    return draft


def _run_draft_arguments(patch: RunDraftPatch, expected_revision: int) -> dict[str, Any]:
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValidationError("expected_revision must be a nonnegative integer")
    if not isinstance(patch, dict):
        raise ValidationError("patch must be a JSON object")
    try:
        return json.loads(json.dumps({"expected_revision": expected_revision, "patch": patch}, allow_nan=False))
    except (TypeError, ValueError, OverflowError) as error:
        raise ValidationError("patch must contain JSON-compatible finite values") from error


class Operations:
    """Version 1 workload and draft operations with server-authoritative rules."""

    version: Literal["v1"] = "v1"

    def __init__(self, client: Client):
        self._client = client

    def catalog(self) -> OperationCatalog:
        """Read operation names, permissions and schemas from the server."""
        return _catalog(self._client._request("GET", _PATH))

    def get_run_draft(self) -> RunDraft:
        """Read your team's member-specific run form without submitting work."""
        return _run_draft(self._client._request("POST", _PATH + "/run_draft.get", json={}))

    def update_run_draft(self, patch: RunDraftPatch, *, expected_revision: int) -> RunDraft:
        """Apply exact form edits only if the saved revision still matches."""
        arguments = _run_draft_arguments(patch, expected_revision)
        return _updated_run_draft(self._client._request("POST", _PATH + "/run_draft.update", json=arguments), arguments)

    def list(self, *, scope: Literal["team", "mine"] | None = None,
             limit: int | None = None, offset: int | None = None) -> WorkloadPage[Workload]:
        """Read one workload page, preserving omitted server defaults."""
        value = self._client._request("POST", _PATH + "/workloads.list", json=_optional(scope=scope, limit=limit, offset=offset))
        return _page(self._client, value, asynchronous=False)

    def get(self, workload_id: str) -> Workload:
        """Read workload status and the current meter."""
        value = self._client._request("POST", _PATH + "/workloads.get", json=_id(workload_id))
        return _workload(self._client, value, asynchronous=False, expected_id=workload_id)

    def events(self, workload_id: str, *, after: int | None = None) -> list[Event]:
        """Read one event page, using the last event's seq as the next cursor."""
        value = self._client._request("POST", _PATH + "/workloads.events", json={**_id(workload_id), **_optional(after=after)})
        return [Event.from_dict(row) for row in _rows(value, "events")]

    def logs(self, workload_id: str) -> str:
        """Read retained workload log text."""
        from . import _LOG_MAX_BYTES
        return self._client._request("POST", _PATH + "/workloads.logs", json=_id(workload_id), text=True, max_bytes=_LOG_MAX_BYTES)

    def outputs(self, workload_id: str) -> list[Output]:
        """List final output metadata and download paths."""
        value = self._client._request("POST", _PATH + "/workloads.outputs", json=_id(workload_id))
        return [Output.from_dict(row) for row in _rows(value, "outputs")]

    def validate(self, workload: dict[str, Any]) -> WorkloadValidation:
        """Validate a workload request without starting compute."""
        value = self._client._request("POST", _PATH + "/workloads.validate", json={"workload": _payload(workload)})
        return _validation(value)

    def submit(self, workload: dict[str, Any], *, idempotency_key: str) -> Workload:
        """Submit paid compute with a stable retry key."""
        from . import _was_replayed
        arguments = _submission(workload, idempotency_key)
        headers: dict[str, str] = {}
        value = self._client._request("POST", _PATH + "/workloads.submit", json=arguments,
                                      idempotency_key=idempotency_key, headers_out=headers)
        result = _workload(self._client, value, asynchronous=False, idempotency_key=idempotency_key)
        result.replayed = _was_replayed(headers)
        return result

    def cancel(self, workload_id: str) -> None:
        """Request cancellation while resource cleanup continues asynchronously."""
        _cancelled(self._client._request("POST", _PATH + "/workloads.cancel", json=_id(workload_id)))


class AsyncOperations:
    """Asynchronous version 1 workload and draft operations."""

    version: Literal["v1"] = "v1"

    def __init__(self, client: AsyncClient):
        self._client = client

    async def catalog(self) -> OperationCatalog:
        """Read operation names, permissions and schemas from the server."""
        return _catalog(await self._client._request("GET", _PATH))

    async def get_run_draft(self) -> RunDraft:
        """Read your team's member-specific run form without submitting work."""
        return _run_draft(await self._client._request("POST", _PATH + "/run_draft.get", json={}))

    async def update_run_draft(self, patch: RunDraftPatch, *, expected_revision: int) -> RunDraft:
        """Apply exact form edits only if the saved revision still matches."""
        arguments = _run_draft_arguments(patch, expected_revision)
        return _updated_run_draft(await self._client._request("POST", _PATH + "/run_draft.update", json=arguments), arguments)

    async def list(self, *, scope: Literal["team", "mine"] | None = None,
                   limit: int | None = None, offset: int | None = None) -> WorkloadPage[AsyncWorkload]:
        """Read one workload page, preserving omitted server defaults."""
        value = await self._client._request("POST", _PATH + "/workloads.list", json=_optional(scope=scope, limit=limit, offset=offset))
        return _page(self._client, value, asynchronous=True)

    async def get(self, workload_id: str) -> AsyncWorkload:
        """Read workload status and the current meter."""
        value = await self._client._request("POST", _PATH + "/workloads.get", json=_id(workload_id))
        return _workload(self._client, value, asynchronous=True, expected_id=workload_id)

    async def events(self, workload_id: str, *, after: int | None = None) -> list[Event]:
        """Read one event page, using the last event's seq as the next cursor."""
        value = await self._client._request("POST", _PATH + "/workloads.events", json={**_id(workload_id), **_optional(after=after)})
        return [Event.from_dict(row) for row in _rows(value, "events")]

    async def logs(self, workload_id: str) -> str:
        """Read retained workload log text."""
        from . import _LOG_MAX_BYTES
        return await self._client._request("POST", _PATH + "/workloads.logs", json=_id(workload_id), text=True, max_bytes=_LOG_MAX_BYTES)

    async def outputs(self, workload_id: str) -> list[Output]:
        """List final output metadata and download paths."""
        value = await self._client._request("POST", _PATH + "/workloads.outputs", json=_id(workload_id))
        return [Output.from_dict(row) for row in _rows(value, "outputs")]

    async def validate(self, workload: dict[str, Any]) -> WorkloadValidation:
        """Validate a workload request without starting compute."""
        value = await self._client._request("POST", _PATH + "/workloads.validate", json={"workload": _payload(workload)})
        return _validation(value)

    async def submit(self, workload: dict[str, Any], *, idempotency_key: str) -> AsyncWorkload:
        """Submit paid compute with a stable retry key."""
        from . import _was_replayed
        arguments = _submission(workload, idempotency_key)
        headers: dict[str, str] = {}
        value = await self._client._request("POST", _PATH + "/workloads.submit", json=arguments,
                                            idempotency_key=idempotency_key, headers_out=headers)
        result = _workload(self._client, value, asynchronous=True, idempotency_key=idempotency_key)
        result.replayed = _was_replayed(headers)
        return result

    async def cancel(self, workload_id: str) -> None:
        """Request cancellation while resource cleanup continues asynchronously."""
        _cancelled(await self._client._request("POST", _PATH + "/workloads.cancel", json=_id(workload_id)))
