"""Durable sandbox handles for interactive agent execution."""

from __future__ import annotations

from ._connections import _live_refs

import asyncio
import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import math
import re
import time
from typing import Any, AsyncIterator, Iterator
import uuid
from urllib.parse import unquote, urlsplit

from ._secrets import _names
from .errors import APIError, APITimeoutError, NodusError, ValidationError
from .types import Event, _dt, _int, _num, _obj, _rows, _text

__all__ = [
    "SandboxState",
    "SandboxExecState",
    "SandboxOutputFrame",
    "SandboxOutputPage",
    "SandboxInputReceipt",
    "Sandboxes",
    "Sandbox",
    "SandboxExec",
    "AsyncSandboxes",
    "AsyncSandbox",
    "AsyncSandboxExec",
]


class _OpenWireEnum(str, Enum):
    @classmethod
    def coerce(cls, value: Any) -> Any:
        if value is None or isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError:
            return str(value)


class SandboxState(_OpenWireEnum):
    CREATING = "creating"
    READY = "ready"
    RUNNING = "running"
    IDLE = "idle"
    SUSPENDED = "suspended"
    RESUMING = "resuming"
    RECOVERING = "recovering"
    TERMINATED = "terminated"
    FAILED = "failed"


class SandboxExecState(_OpenWireEnum):
    QUEUED = "queued"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"


SANDBOX_TERMINAL = frozenset({"terminated", "failed"})
EXEC_TERMINAL = frozenset({"completed", "failed", "cancelled", "lost"})
_RESOURCE_ID = re.compile(r"\A[A-Za-z0-9_-]+\Z")


def _valid_id(value: Any, kind: str) -> str:
    if isinstance(value, str) and _RESOURCE_ID.match(value):
        return value
    raise ValidationError(
        f"{value!r} is not a {kind} id. Use letters, digits, underscores, and hyphens."
    )


def _mutation_receipt(
    client: Any, response: Any, path: str, idempotency_key: str,
    *, expected_id: str | None = None, expected_sandbox_id: str | None = None,
) -> dict[str, Any]:
    try:
        receipt = client._one(response, "POST", path)
        received_id = _valid_id(receipt.get("id"), "sandbox")
        if expected_id is not None and received_id != expected_id:
            raise NodusError("Sandbox receipt identity differs from the request")
        if expected_sandbox_id is not None and "sandbox_id" in receipt and receipt["sandbox_id"] != expected_sandbox_id:
            raise NodusError("Execution receipt sandbox differs from the request")
    except NodusError:
        raise client._unreached(APIError, f"POST {path} returned an invalid sandbox receipt", idempotency_key) from None
    return receipt


def _http_path(sandbox_id: str, port: int, path: str, method: str) -> str:
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535 or port == 18080:
        raise ValidationError("port must be a guest HTTP port from 1024 to 65535, excluding 18080")
    if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
        raise ValidationError("unsupported HTTP method")
    if not isinstance(path, str) or len(path) > 4096 or not path.startswith("/") or path.startswith("//"):
        raise ValidationError("path must be an absolute guest HTTP path")
    decoded = unquote(path)
    if any(c in decoded for c in ("\r", "\n", "\\", "\x00")) or urlsplit(path).fragment:
        raise ValidationError("invalid HTTP path")
    if any(part in {".", ".."} for part in urlsplit(decoded).path.split("/")):
        raise ValidationError("HTTP paths cannot contain dot segments")
    return f"/v1/sandboxes/{_valid_id(sandbox_id, 'sandbox')}/ports/{port}/{path[1:]}"


def _wire(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire(item) for item in value]
    return value


def _command(value: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(value, str):
        if not value or "\x00" in value:
            raise ValidationError("command must be nonempty text without NUL characters")
        return ["/bin/sh", "-lc", value]
    if not isinstance(value, (list, tuple)) or not value:
        raise ValidationError("command must be a nonempty argv list")
    if any(not isinstance(arg, str) or "\x00" in arg for arg in value):
        raise ValidationError("every command argument must be text without NUL characters")
    return list(value)


def _create_payload(
    *,
    image: str | None,
    template: str | None = None,
    project: Any = None,
    source_asset_id: str | None = None,
    network_permissions: list[str] | None = None,
    setup: str | None = None,
    cache_image: bool = False,
    name: str | None = None,
    budget: float | None = None,
    wake: str | None = None,
    requirements: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    lifecycle: dict[str, Any] | None = None,
    reservation: dict[str, Any] | None = None,
    continuity: dict[str, Any] | None = None,
    from_snapshot: str | None = None,
    secrets: list[str] | None = None,
    connections: list[str] | None = None,
    service: dict[str, Any] | None = None,
    bootstrap: dict[str, str] | None = None,
    stuck_after_s: int | None = None,
    workspace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if image is not None and (not isinstance(image, str) or not image.strip()):
        raise ValidationError("image must be nonempty text")
    if template is None and image is None and (not name or project is not None or source_asset_id is not None) and from_snapshot is None:
        template = "nodus:agent-tools-v1"
    if setup is not None:
        from ._projects import setup_command
        setup_command(setup)
        if template is None:
            raise ValidationError("setup requires a managed template")
    if template is not None and (template != "nodus:agent-tools-v1" or image is not None or cache_image):
        raise ValidationError("Choose the managed template or a custom image")
    if project is not None and (template is None or source_asset_id is not None or bootstrap is not None):
        raise ValidationError("project requires a managed template and cannot combine source or bootstrap")
    selected_budget = budget if budget is not None else (outcome or {}).get("max_cost_usd")
    if selected_budget is not None and (type(selected_budget) not in (int, float) or not math.isfinite(selected_budget) or selected_budget < 0):
        raise ValidationError("Legacy budget must be a finite nonnegative value")
    if stuck_after_s is not None and (type(stuck_after_s) is not int or not 30 <= stuck_after_s <= 604800):
        raise ValidationError("stuck_after_s must be an integer from 30 through 604800")
    if type(cache_image) is not bool or (cache_image and image is None):
        raise ValidationError("cache_image must be a boolean and requires an image when enabled")
    body: dict[str, Any] = {}
    if template is not None:
        body["template"] = template
    if setup is not None:
        body["setup"] = setup
    if network_permissions is not None:
        body["network_permissions"] = network_permissions
    if source_asset_id is not None:
        from ._assets import _id as asset_id
        body["source"] = {"asset_id": asset_id(source_asset_id)}
    if cache_image:
        body["source"] = {"image": image, "cache": True}
    if image is not None:
        body["image"] = image
    selected_requirements = dict(requirements or {})
    if (
        selected_requirements.get("gpu")
        or selected_requirements.get("gpu_count") is not None
        or selected_requirements.get("gpu_interconnect")
    ):
        selected_requirements.setdefault("compute_class", "accelerator")
    for key, value in (
        ("name", name),
        ("wake", wake),
        ("requirements", selected_requirements),
        ("policy", policy),
        ("lifecycle", lifecycle),
        ("reservation", reservation),
        ("continuity", continuity),
        ("from_snapshot", from_snapshot),
        ("secrets", _names(secrets)),
        ("connections", _live_refs(connections)),
        ("service", service),
        ("bootstrap", bootstrap),
        ("stuck_after_s", stuck_after_s),
        ("workspace", workspace),
    ):
        if value is not None:
            body[key] = _wire(value)
    final_outcome = dict(outcome or {})
    if budget is not None:
        if "max_cost_usd" in final_outcome and final_outcome["max_cost_usd"] != budget:
            raise ValidationError("budget conflicts with outcome.max_cost_usd")
        final_outcome["max_cost_usd"] = budget
    if final_outcome:
        body["outcome"] = _wire(final_outcome)
    return body


def _terminal_size(rows: int | None, cols: int | None) -> None:
    if any(type(n) is not int or not 1 <= n <= 4096 for n in (rows, cols)):
        raise ValidationError("rows and cols must be integers from 1 through 4096")


def _exec_payload(
    command: str | list[str] | tuple[str, ...],
    *,
    cwd: str | None,
    env: dict[str, str] | None,
    timeout_seconds: int | None,
    stdin: bool, tty: bool = False, rows: int | None = None, cols: int | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"command": _command(command)}
    if cwd is not None:
        body["cwd"] = cwd
    if env is not None:
        body["env"] = dict(env)
    if timeout_seconds is not None:
        body["timeout_s"] = timeout_seconds
    if stdin:
        body["stdin"] = True
    if tty:
        _terminal_size(rows, cols)
        if not stdin:
            raise ValidationError("tty requires stdin")
        body.update(tty=True, rows=rows, cols=cols)
    elif rows is not None or cols is not None:
        raise ValidationError("terminal dimensions require tty")
    return body


@dataclass(frozen=True)
class SandboxOutputFrame:
    sequence: int
    stream: str
    offset: int
    data: bytes
    created_at: datetime | None = None

    @property
    def text(self) -> str:
        return self.data.decode("utf-8", "replace")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SandboxOutputFrame":
        encoded = value.get("data")
        try:
            data = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else b""
        except (ValueError, binascii.Error):
            data = b""
        return cls(
            sequence=_int(value.get("sequence")),
            stream=_text(value.get("stream")),
            offset=_int(value.get("offset")),
            data=data,
            created_at=_dt(value.get("created_at")),
        )


@dataclass(frozen=True)
class SandboxOutputPage:
    frames: list[SandboxOutputFrame]
    next_sequence: int
    last_sequence: int
    final_sequence: int | None
    state: Any
    done: bool
    complete: bool

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "SandboxOutputPage":
        body = _obj(value)
        final = body.get("final_sequence")
        return cls(
            frames=[SandboxOutputFrame.from_dict(_obj(row)) for row in _rows(body.get("frames"))],
            next_sequence=_int(body.get("next_sequence")),
            last_sequence=_int(body.get("last_sequence")),
            final_sequence=_int(final) if final is not None else None,
            state=SandboxExecState.coerce(body.get("state")),
            done=bool(body.get("done")),
            complete=bool(body.get("complete")),
        )


@dataclass(frozen=True)
class SandboxInputReceipt:
    sequence: int
    bytes: int
    eof: bool
    created_at: datetime | None

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "SandboxInputReceipt":
        body = _obj(value)
        return cls(
            sequence=_int(body.get("sequence")),
            bytes=_int(body.get("bytes")),
            eof=bool(body.get("eof")),
            created_at=_dt(body.get("created_at")),
        )


class _SandboxState:
    network_usage: dict[str, int] | None
    failure: dict[str, Any] | None
    id: str
    state: Any
    envelope: dict[str, Any]
    cost_usd: float
    charge_state: str
    final_charge_usd: float | None
    url: str
    created_at: datetime | None
    updated_at: datetime | None
    last_activity_at: datetime | None
    terminal_at: datetime | None
    replayed: bool

    def _init_state(self, sandbox_id: str = "") -> None:
        self.id = sandbox_id
        self.state = None
        self.envelope = {}
        self.cost_usd = 0.0
        self.charge_state = ""
        self.final_charge_usd = None
        self.url = ""
        self.created_at = None
        self.updated_at = None
        self.last_activity_at = None
        self.terminal_at = None
        self.replayed = False
        self.failure = None
        self.network_usage = None
        self.startup = None
        self.warnings: list[str] = []

    def _absorb(self, value: dict[str, Any] | None) -> None:
        body = _obj(value)
        self.id = _text(body.get("id")) or self.id
        self.warnings = [x for x in body.get("warnings", []) if isinstance(x, str)]
        startup = body.get("startup")
        self.startup = dict(startup) if isinstance(startup, dict) else None
        usage = body.get("network_usage")
        self.network_usage = dict(usage) if isinstance(usage, dict) else None
        failure = body.get("failure")
        self.failure = dict(failure) if isinstance(failure, dict) else None
        if "state" in body:
            self.state = SandboxState.coerce(body.get("state"))
        if "envelope" in body:
            self.envelope = _obj(body.get("envelope"))
        if "cost_usd" in body:
            self.cost_usd = _num(body.get("cost_usd"))
        self.charge_state = _text(body.get("charge_state"))
        self.final_charge_usd = _num(body.get("final_charge_usd"), None)
        if "url" in body:
            self.url = _text(body.get("url"))
        for name in ("created_at", "updated_at", "last_activity_at", "terminal_at"):
            if name in body:
                setattr(self, name, _dt(body.get(name)))

    @property
    def is_terminal(self) -> bool:
        return getattr(self.state, "value", self.state) in SANDBOX_TERMINAL


class _SandboxExecState:
    id: str
    sandbox_id: str
    state: Any
    spec: dict[str, Any]
    exit_code: int | None
    failure_code: str
    replayed: bool
    output_sequence: int
    stdout_bytes: int
    stderr_bytes: int
    final_output_sequence: int | None

    def _init_exec_state(self, sandbox_id: str, exec_id: str = "") -> None:
        self.id = exec_id
        self.sandbox_id = sandbox_id
        self.state = None
        self.spec = {}
        self.exit_code = None
        self.failure_code = ""
        self.replayed = False
        self.output_sequence = 0
        self.stdout_bytes = 0
        self.stderr_bytes = 0
        self.final_output_sequence = None
        self.created_at = None
        self.updated_at = None
        self.dispatched_at = None
        self.deadline_at = None
        self.started_at = None
        self.completed_at = None

    def _absorb(self, value: dict[str, Any] | None) -> None:
        body = _obj(value)
        self.id = _text(body.get("id")) or self.id
        self.sandbox_id = _text(body.get("sandbox_id")) or self.sandbox_id
        if "state" in body:
            self.state = SandboxExecState.coerce(body.get("state"))
        if "spec" in body:
            self.spec = _obj(body.get("spec"))
        if "exit_code" in body:
            value = body.get("exit_code")
            self.exit_code = _int(value) if value is not None else None
        self.failure_code = _text(body.get("failure_code")) or self.failure_code
        for name in ("output_sequence", "stdout_bytes", "stderr_bytes"):
            if name in body:
                setattr(self, name, _int(body.get(name)))
        if "final_output_sequence" in body:
            final = body.get("final_output_sequence")
            self.final_output_sequence = _int(final) if final is not None else None
        for name in ("created_at", "updated_at", "dispatched_at", "deadline_at", "started_at", "completed_at"):
            if name in body:
                setattr(self, name, _dt(body.get(name)))

    @property
    def command(self) -> list[str]:
        return [str(arg) for arg in _rows(self.spec.get("command"))]

    @property
    def is_terminal(self) -> bool:
        return getattr(self.state, "value", self.state) in EXEC_TERMINAL

    @property
    def succeeded(self) -> bool:
        return self.state == SandboxExecState.COMPLETED and self.exit_code == 0


class Sandboxes:
    """Create, reconnect to, and list customer sandboxes."""

    def capabilities(self) -> dict[str, Any]:
        """Read the deployment's managed environment availability and limits."""
        return self._client._request("GET", "/v1/sandboxes/capabilities")

    def templates(self) -> dict[str, Any]:
        """List versioned managed environments and their availability."""
        return self._client._request("GET", "/v1/sandbox-templates")

    def connect(self, reference: str):
        """Reconnect by exact ID or an unambiguous active sandbox name."""
        from .errors import NotFoundError
        if not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", reference):
            raise ValidationError("Use a sandbox ID or a valid sandbox name")
        missing = None
        if reference.startswith("sb_") and "." not in reference:
            try:
                return self.from_id(reference)
            except NotFoundError as error:
                if error.code != "not_found" or re.fullmatch(r"sb_[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", reference):
                    raise
                missing = error
        matches, seen = {}, set()
        cursor = None
        while True:
            rows, next_cursor = self.list_page(name=reference, cursor=cursor)
            for row in rows:
                if row.envelope.get("name") == reference and not row.is_terminal:
                    matches[row.id] = row
            if not next_cursor:
                break
            if next_cursor in seen:
                raise APIError("Sandbox pagination did not advance")
            seen.add(next_cursor)
            cursor = next_cursor
        if not matches and missing is not None:
            raise missing
        if len(matches) != 1:
            raise ValidationError("Use an exact sandbox ID when the name is absent or ambiguous")
        return next(iter(matches.values()))

    def __init__(self, client: Any):
        self._client = client

    def create(
        self,
        *,
        image: str | None = None,
        template: str | None = None,
        project: Any = None,
        source_asset_id: str | None = None,
        network_permissions: list[str] | None = None,
        setup: str | None = None,
        cache_image: bool = False,
        name: str | None = None,
        budget: float | None = None,
        wake: str | None = None,
        requirements: dict[str, Any] | None = None,
        outcome: dict[str, Any] | None = None,
        policy: dict[str, Any] | None = None,
        lifecycle: dict[str, Any] | None = None,
        reservation: dict[str, Any] | None = None,
        continuity: dict[str, Any] | None = None,
        from_snapshot: str | None = None,
        secrets: list[str] | None = None,
        connections: list[str] | None = None,
        service: dict[str, Any] | None = None,
        bootstrap: dict[str, str] | None = None,
        stuck_after_s: int | None = None,
        workspace: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> "Sandbox":
        body = _create_payload(
            image=image, template=template, project=project, source_asset_id=source_asset_id, network_permissions=network_permissions, setup=setup, cache_image=cache_image, name=name, budget=budget, wake=wake, requirements=requirements,
            outcome=outcome, policy=policy, lifecycle=lifecycle,
            reservation=reservation, continuity=continuity,
            from_snapshot=from_snapshot, secrets=secrets, connections=connections, service=service, bootstrap=bootstrap, stuck_after_s=stuck_after_s, workspace=workspace,
        )
        from ._managed_agents import _key
        key = _key(idempotency_key)
        if project is not None:
            from ._projects import upload_project
            body["source"] = {"asset_id": upload_project(self._client, project, ("sandbox", key))}
        headers: dict[str, str] = {}
        response = self._client._request(
            "POST", "/v1/sandboxes", json=body,
            idempotency_key=key,
            headers_out=headers,
        )
        sandbox = Sandbox(self._client)
        sandbox._absorb(_mutation_receipt(self._client, response, "/v1/sandboxes", key))
        sandbox.replayed = any(name.lower() == "idempotent-replayed" and value.lower() == "true" for name, value in headers.items())
        return sandbox

    def from_id(self, sandbox_id: str) -> "Sandbox":
        path = f"/v1/sandboxes/{_valid_id(sandbox_id, 'sandbox')}"
        sandbox = Sandbox(self._client, sandbox_id)
        sandbox._absorb(self._client._one(self._client._request("GET", path), "GET", path))
        return sandbox

    def list_page(
        self, *, limit: int = 50, cursor: str | None = None,
        name: str | None = None, state: str | SandboxState | None = None,
    ) -> tuple[list["Sandbox"], str | None]:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if name is not None:
            params["name"] = name
        if state is not None:
            params["status"] = getattr(state, "value", state)
        response = _obj(self._client._request("GET", "/v1/sandboxes", params=params))
        rows = []
        for item in _rows(response.get("sandboxes")):
            sandbox = Sandbox(self._client)
            sandbox._absorb(_obj(item))
            rows.append(sandbox)
        cursor_value = response.get("next_cursor")
        return rows, cursor_value if isinstance(cursor_value, str) and cursor_value else None

    def list(self, **kwargs: Any) -> list["Sandbox"]:
        rows, _ = self.list_page(**kwargs)
        return rows

    def iterate(self) -> Iterator["Sandbox"]:
        cursor = None
        while True:
            rows, next_cursor = self.list_page(cursor=cursor)
            yield from rows
            if next_cursor is None or next_cursor == cursor:
                return
            cursor = next_cursor

    def __iter__(self) -> Iterator["Sandbox"]:
        return self.iterate()


class Sandbox(_SandboxState):
    @property
    def files(self):
        from ._sandbox_files import SandboxFiles
        return SandboxFiles(self)

    def _lifecycle(self, action: str, idempotency_key: str | None):
        sandbox_id = _valid_id(self.id, "sandbox")
        path = f"/v1/sandboxes/{sandbox_id}/{action}"
        key = idempotency_key or f"sandbox-{action}-{uuid.uuid4()}"
        response = self._client._request("POST", path, json={}, idempotency_key=key)
        self._absorb(_mutation_receipt(self._client, response, path, key, expected_id=sandbox_id))
        return self

    def sleep(self, *, idempotency_key: str | None = None):
        """Request a verified project save and release of compute."""
        return self._lifecycle("sleep", idempotency_key)

    def wake(self, *, idempotency_key: str | None = None):
        """Resume compute within this sandbox's existing authorization."""
        return self._lifecycle("wake", idempotency_key)

    @property
    def agent_events(self):
        from ._agent_runs import AgentEvents
        return AgentEvents(self._client, _valid_id(self.id, "sandbox"))

    @property
    def agent_runs(self):
        from ._agent_runs import AgentRuns
        return AgentRuns(self._client, _valid_id(self.id, "sandbox"))

    def __init__(
        self,
        client: Any | None = None,
        sandbox_id: str = "",
        *,
        image: str | None = None,
        template: str | None = None,
        project: Any = None,
        source_asset_id: str | None = None,
        network_permissions: list[str] | None = None,
        setup: str | None = None,
        cache_image: bool = False,
        name: str | None = None,
        budget: float | None = None,
        wake: str | None = None,
        requirements: dict[str, Any] | None = None,
        outcome: dict[str, Any] | None = None,
        policy: dict[str, Any] | None = None,
        lifecycle: dict[str, Any] | None = None,
        reservation: dict[str, Any] | None = None,
        continuity: dict[str, Any] | None = None,
        from_snapshot: str | None = None,
        secrets: list[str] | None = None,
        connections: list[str] | None = None,
        service: dict[str, Any] | None = None,
        bootstrap: dict[str, str] | None = None,
        stuck_after_s: int | None = None,
        workspace: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ):
        self._client = client
        self._owned_client = client is None
        self._init_state(sandbox_id)
        if client is None:
            if budget is None and not any((image, template, project, name, source_asset_id, from_snapshot)):
                raise ValidationError("Provide an image or name, or select a template or project for the sandbox.")
            from . import Client

            self._client = Client()
            try:
                created = self._client.sandboxes.create(
                    image=image, template=template, project=project, source_asset_id=source_asset_id, network_permissions=network_permissions, setup=setup,
                    cache_image=cache_image,
                    name=name,
                    budget=budget,
                    wake=wake,
                    requirements=requirements,
                    outcome=outcome,
                    policy=policy,
                    lifecycle=lifecycle,
                    reservation=reservation,
                    continuity=continuity,
                    from_snapshot=from_snapshot, secrets=secrets, connections=connections, service=service, bootstrap=bootstrap, stuck_after_s=stuck_after_s, workspace=workspace,
                    idempotency_key=idempotency_key,
                )
            except BaseException:
                self._client.close()
                raise
            self._absorb({
                "id": created.id,
                "state": getattr(created.state, "value", created.state),
                "envelope": created.envelope,
                "cost_usd": created.cost_usd,
                "charge_state": created.charge_state,
                "final_charge_usd": created.final_charge_usd,
                "failure": created.failure,
                "network_usage": created.network_usage,
                "url": created.url,
                "created_at": created.created_at,
                "updated_at": created.updated_at,
                "last_activity_at": created.last_activity_at,
                "terminal_at": created.terminal_at,
            })
            self.replayed = created.replayed

    def __enter__(self) -> "Sandbox":
        return self

    def close(self) -> None:
        """Close this handle's owned HTTP client without terminating the sandbox."""
        if self._owned_client:
            self._client.close()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            if getattr(self.state, "value", self.state) != SandboxState.TERMINATED.value:
                self.terminate()
        except BaseException:
            if exc_type is None:
                raise
        finally:
            self.close()

    def refresh(self) -> "Sandbox":
        path = f"/v1/sandboxes/{_valid_id(self.id, 'sandbox')}"
        self._absorb(self._client._one(self._client._request("GET", path), "GET", path))
        return self

    def refresh_secrets(self) -> None:
        """Bind current secret versions for subsequent commands."""
        path = f"/v1/sandboxes/{_valid_id(self.id, 'sandbox')}/refresh-secrets"
        self._client._request("POST", path)

    def metrics(self) -> dict[str, Any]:
        """Read actual measurements and 24 hour history. Missing measurements are None."""
        path = f"/v1/sandboxes/{_valid_id(self.id, 'sandbox')}/metrics"
        return _obj(self._client._request("GET", path))

    def events(self, *, after: int = 0) -> list[Event]:
        """Read up to 100 lifecycle and denied-host events after an event sequence."""
        path = f"/v1/sandboxes/{_valid_id(self.id, 'sandbox')}/events"
        response = self._client._request("GET", path, params={"after": after})
        return [Event.from_dict(_obj(row)) for row in _rows(_obj(response).get("events"))]

    def request(
        self, method: str, path: str = "/", *, port: int,
        json: Any = None, content: bytes | None = None,
    ) -> bytes:
        """Send JSON or raw bytes to a guest HTTP port and return bounded bytes.

        Requests are never retried. A timed out mutation may have executed.
        """
        if content is not None and (not isinstance(content, bytes) or json is not None):
            raise ValidationError("content must be bytes and cannot be combined with json")
        route = _http_path(self.id, port, path, method)
        return self._client._request(method, route, json=json, content=content, raw=True,
                                     max_bytes=65536, max_retries=0)

    def exec(
        self, command: str | list[str] | tuple[str, ...], *, cwd: str | None = None,
        env: dict[str, str] | None = None, timeout_seconds: int | None = None,
        stdin: bool = False, tty: bool = False, rows: int | None = None,
        cols: int | None = None, idempotency_key: str | None = None,
    ) -> "SandboxExec":
        sandbox_id = _valid_id(self.id, "sandbox")
        path = f"/v1/sandboxes/{sandbox_id}/exec"
        headers: dict[str, str] = {}
        key = idempotency_key or f"sandbox-exec-{uuid.uuid4()}"
        response = self._client._request(
            "POST", path,
            json=_exec_payload(command, cwd=cwd, env=env, timeout_seconds=timeout_seconds, stdin=stdin, tty=tty, rows=rows, cols=cols),
            idempotency_key=key,
            headers_out=headers,
        )
        execution = SandboxExec(self._client, sandbox_id)
        execution._absorb(_mutation_receipt(self._client, response, path, key, expected_sandbox_id=sandbox_id))
        execution.replayed = any(name.lower() == "idempotent-replayed" and value.lower() == "true" for name, value in headers.items())
        return execution

    def terminate(self, *, idempotency_key: str | None = None) -> "Sandbox":
        sandbox_id = _valid_id(self.id, "sandbox")
        path = f"/v1/sandboxes/{sandbox_id}/terminate"
        key = idempotency_key or f"sandbox-terminate-{uuid.uuid4()}"
        response = self._client._request(
            "POST", path, json={},
            idempotency_key=key,
        )
        self._absorb(_mutation_receipt(self._client, response, path, key, expected_id=sandbox_id))
        return self


class SandboxExec(_SandboxExecState):
    def __init__(self, client: Any, sandbox_id: str, exec_id: str = ""):
        self._client = client
        self._init_exec_state(sandbox_id, exec_id)

    def _path(self) -> str:
        return f"/v1/sandboxes/{_valid_id(self.sandbox_id, 'sandbox')}/execs/{_valid_id(self.id, 'sandbox execution')}"

    def refresh(self, *, wait: bool = False, _timeout: float | None = None) -> "SandboxExec":
        path = self._path()
        response = self._client._request(
            "GET", path, params={"wait": str(wait).lower()},
            timeout=_timeout if _timeout is not None else (35 if wait else None),
            max_retries=0 if _timeout is not None else None,
        )
        self._absorb(self._client._one(response, "GET", path))
        return self

    def wait(self, *, timeout_seconds: float | None = None) -> "SandboxExec":
        if timeout_seconds is not None and (not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be finite and greater than zero")
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        while not self.is_terminal:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise APITimeoutError(f"sandbox execution {self.id} did not finish in time")
            self.refresh(wait=True, _timeout=None if remaining is None else min(35, remaining))
        return self

    def output(self, *, after: int = 0, limit: int = 16, wait: bool = False) -> SandboxOutputPage:
        path = self._path() + "/stream"
        response = self._client._request(
            "GET", path,
            params={"after": after, "limit": limit, "wait": str(wait).lower()},
            timeout=35 if wait else None,
        )
        return SandboxOutputPage.from_dict(self._client._one(response, "GET", path))

    def iter_output(self, *, after: int = 0, follow: bool = True) -> Iterator[SandboxOutputFrame]:
        """Yield ordered output, draining only currently stored frames when follow is false."""
        cursor = after
        end_sequence = None
        while True:
            previous_cursor = cursor
            limit = 16 if end_sequence is None else min(16, end_sequence - cursor)
            page = self.output(after=cursor, limit=limit, wait=follow)
            if not follow and end_sequence is None:
                end_sequence = page.last_sequence
            for frame in page.frames:
                if frame.sequence > cursor:
                    cursor = frame.sequence
                    yield frame
            cursor = max(cursor, page.next_sequence)
            if (page.done and page.complete) or (end_sequence is not None and cursor >= end_sequence):
                return
            if (page.done or end_sequence is not None) and cursor <= previous_cursor:
                raise NodusError(f"Sandbox execution final output is unavailable after sequence {cursor}. Retry output(after={cursor}).")

    def write(
        self, data: bytes | str = b"", *, eof: bool = False,
        idempotency_key: str | None = None,
    ) -> SandboxInputReceipt:
        raw = data.encode() if isinstance(data, str) else bytes(data)
        path = self._path() + "/stdin"
        response = self._client._request(
            "POST", path,
            json={"data": base64.b64encode(raw).decode("ascii"), "eof": eof},
            idempotency_key=idempotency_key or f"sandbox-stdin-{uuid.uuid4()}",
        )
        return SandboxInputReceipt.from_dict(self._client._one(response, "POST", path))


    def resize(self, rows: int, cols: int) -> None:
        """Set terminal dimensions for a tty execution."""
        _terminal_size(rows, cols)
        self._client._request("POST", self._path() + "/resize", json={"rows": rows, "cols": cols})

    def cancel(self) -> "SandboxExec":
        """Request cancellation without terminating the sandbox."""
        path = self._path() + "/cancel"
        response = self._client._request("POST", path, json={})
        self._absorb(self._client._one(response, "POST", path))
        return self


class AsyncSandboxes:
    async def capabilities(self) -> dict[str, Any]:
        """Read the deployment's managed environment availability and limits."""
        return await self._client._request("GET", "/v1/sandboxes/capabilities")

    async def templates(self) -> dict[str, Any]:
        """List versioned managed environments and their availability."""
        return await self._client._request("GET", "/v1/sandbox-templates")

    async def connect(self, reference: str):
        """Reconnect by exact ID or an unambiguous active sandbox name."""
        from .errors import NotFoundError
        if not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", reference):
            raise ValidationError("Use a sandbox ID or a valid sandbox name")
        missing = None
        if reference.startswith("sb_") and "." not in reference:
            try:
                return await self.from_id(reference)
            except NotFoundError as error:
                if error.code != "not_found" or re.fullmatch(r"sb_[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", reference):
                    raise
                missing = error
        matches, seen = {}, set()
        cursor = None
        while True:
            rows, next_cursor = await self.list_page(name=reference, cursor=cursor)
            for row in rows:
                if row.envelope.get("name") == reference and not row.is_terminal:
                    matches[row.id] = row
            if not next_cursor:
                break
            if next_cursor in seen:
                raise APIError("Sandbox pagination did not advance")
            seen.add(next_cursor)
            cursor = next_cursor
        if not matches and missing is not None:
            raise missing
        if len(matches) != 1:
            raise ValidationError("Use an exact sandbox ID when the name is absent or ambiguous")
        return next(iter(matches.values()))

    def __init__(self, client: Any):
        self._client = client

    async def create(
        self,
        *,
        image: str | None = None,
        template: str | None = None,
        project: Any = None,
        source_asset_id: str | None = None,
        network_permissions: list[str] | None = None,
        setup: str | None = None,
        cache_image: bool = False,
        name: str | None = None,
        budget: float | None = None,
        wake: str | None = None,
        requirements: dict[str, Any] | None = None,
        outcome: dict[str, Any] | None = None,
        policy: dict[str, Any] | None = None,
        lifecycle: dict[str, Any] | None = None,
        reservation: dict[str, Any] | None = None,
        continuity: dict[str, Any] | None = None,
        from_snapshot: str | None = None,
        secrets: list[str] | None = None,
        connections: list[str] | None = None,
        service: dict[str, Any] | None = None,
        bootstrap: dict[str, str] | None = None,
        stuck_after_s: int | None = None,
        workspace: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> "AsyncSandbox":
        body = _create_payload(
            image=image, template=template, project=project, source_asset_id=source_asset_id, network_permissions=network_permissions, setup=setup, cache_image=cache_image, name=name, budget=budget, wake=wake, requirements=requirements,
            outcome=outcome, policy=policy, lifecycle=lifecycle,
            reservation=reservation, continuity=continuity,
            from_snapshot=from_snapshot, secrets=secrets, connections=connections, service=service, bootstrap=bootstrap, stuck_after_s=stuck_after_s, workspace=workspace,
        )
        from ._managed_agents import _key
        key = _key(idempotency_key)
        if project is not None:
            from ._projects import upload_project_async
            body["source"] = {"asset_id": await upload_project_async(self._client, project, ("sandbox", key))}
        headers: dict[str, str] = {}
        response = await self._client._request(
            "POST", "/v1/sandboxes", json=body,
            idempotency_key=key,
            headers_out=headers,
        )
        sandbox = AsyncSandbox(self._client)
        sandbox._absorb(_mutation_receipt(self._client, response, "/v1/sandboxes", key))
        sandbox.replayed = any(name.lower() == "idempotent-replayed" and value.lower() == "true" for name, value in headers.items())
        return sandbox

    async def from_id(self, sandbox_id: str) -> "AsyncSandbox":
        path = f"/v1/sandboxes/{_valid_id(sandbox_id, 'sandbox')}"
        sandbox = AsyncSandbox(self._client, sandbox_id)
        sandbox._absorb(self._client._one(await self._client._request("GET", path), "GET", path))
        return sandbox

    async def list_page(
        self, *, limit: int = 50, cursor: str | None = None,
        name: str | None = None, state: str | SandboxState | None = None,
    ) -> tuple[list["AsyncSandbox"], str | None]:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if name is not None:
            params["name"] = name
        if state is not None:
            params["status"] = getattr(state, "value", state)
        response = _obj(await self._client._request("GET", "/v1/sandboxes", params=params))
        rows = []
        for item in _rows(response.get("sandboxes")):
            sandbox = AsyncSandbox(self._client)
            sandbox._absorb(_obj(item))
            rows.append(sandbox)
        cursor_value = response.get("next_cursor")
        return rows, cursor_value if isinstance(cursor_value, str) and cursor_value else None

    async def list(self, **kwargs: Any) -> list["AsyncSandbox"]:
        rows, _ = await self.list_page(**kwargs)
        return rows

    async def iterate(self) -> AsyncIterator["AsyncSandbox"]:
        cursor = None
        while True:
            rows, next_cursor = await self.list_page(cursor=cursor)
            for row in rows:
                yield row
            if next_cursor is None or next_cursor == cursor:
                return
            cursor = next_cursor


class AsyncSandbox(_SandboxState):
    @property
    def files(self):
        from ._sandbox_files import AsyncSandboxFiles
        return AsyncSandboxFiles(self)

    async def _lifecycle(self, action: str, idempotency_key: str | None):
        sandbox_id = _valid_id(self.id, "sandbox")
        path = f"/v1/sandboxes/{sandbox_id}/{action}"
        key = idempotency_key or f"sandbox-{action}-{uuid.uuid4()}"
        response = await self._client._request("POST", path, json={}, idempotency_key=key)
        self._absorb(_mutation_receipt(self._client, response, path, key, expected_id=sandbox_id))
        return self

    async def sleep(self, *, idempotency_key: str | None = None):
        """Request a verified project save and release of compute."""
        return await self._lifecycle("sleep", idempotency_key)

    async def wake(self, *, idempotency_key: str | None = None):
        """Resume compute within this sandbox's existing authorization."""
        return await self._lifecycle("wake", idempotency_key)

    @property
    def agent_events(self):
        from ._agent_runs import AsyncAgentEvents
        return AsyncAgentEvents(self._client, _valid_id(self.id, "sandbox"))

    @property
    def agent_runs(self):
        from ._agent_runs import AsyncAgentRuns
        return AsyncAgentRuns(self._client, _valid_id(self.id, "sandbox"))

    def __init__(self, client: Any, sandbox_id: str = ""):
        self._client = client
        self._owned_client = False
        self._init_state(sandbox_id)

    async def close(self) -> None:
        """Close this handle's owned HTTP client without terminating the sandbox."""
        if self._owned_client:
            await self._client.aclose()

    async def refresh(self) -> "AsyncSandbox":
        path = f"/v1/sandboxes/{_valid_id(self.id, 'sandbox')}"
        self._absorb(self._client._one(await self._client._request("GET", path), "GET", path))
        return self

    async def refresh_secrets(self) -> None:
        """Bind current secret versions for subsequent commands."""
        path = f"/v1/sandboxes/{_valid_id(self.id, 'sandbox')}/refresh-secrets"
        await self._client._request("POST", path)

    async def metrics(self) -> dict[str, Any]:
        """Read actual measurements and 24 hour history. Missing measurements are None."""
        path = f"/v1/sandboxes/{_valid_id(self.id, 'sandbox')}/metrics"
        return _obj(await self._client._request("GET", path))

    async def events(self, *, after: int = 0) -> list[Event]:
        """Read up to 100 lifecycle and denied-host events after an event sequence."""
        path = f"/v1/sandboxes/{_valid_id(self.id, 'sandbox')}/events"
        response = await self._client._request("GET", path, params={"after": after})
        return [Event.from_dict(_obj(row)) for row in _rows(_obj(response).get("events"))]

    async def request(
        self, method: str, path: str = "/", *, port: int,
        json: Any = None, content: bytes | None = None,
    ) -> bytes:
        """Send JSON or raw bytes and return bounded bytes without automatic retries."""
        if content is not None and (not isinstance(content, bytes) or json is not None):
            raise ValidationError("content must be bytes and cannot be combined with json")
        route = _http_path(self.id, port, path, method)
        return await self._client._request(method, route, json=json, content=content, raw=True,
                                           max_bytes=65536, max_retries=0)

    async def exec(
        self, command: str | list[str] | tuple[str, ...], *, cwd: str | None = None,
        env: dict[str, str] | None = None, timeout_seconds: int | None = None,
        stdin: bool = False, tty: bool = False, rows: int | None = None,
        cols: int | None = None, idempotency_key: str | None = None,
    ) -> "AsyncSandboxExec":
        sandbox_id = _valid_id(self.id, "sandbox")
        path = f"/v1/sandboxes/{sandbox_id}/exec"
        headers: dict[str, str] = {}
        key = idempotency_key or f"sandbox-exec-{uuid.uuid4()}"
        response = await self._client._request(
            "POST", path,
            json=_exec_payload(command, cwd=cwd, env=env, timeout_seconds=timeout_seconds, stdin=stdin, tty=tty, rows=rows, cols=cols),
            idempotency_key=key,
            headers_out=headers,
        )
        execution = AsyncSandboxExec(self._client, sandbox_id)
        execution._absorb(_mutation_receipt(self._client, response, path, key, expected_sandbox_id=sandbox_id))
        execution.replayed = any(name.lower() == "idempotent-replayed" and value.lower() == "true" for name, value in headers.items())
        return execution

    async def terminate(self, *, idempotency_key: str | None = None) -> "AsyncSandbox":
        sandbox_id = _valid_id(self.id, "sandbox")
        path = f"/v1/sandboxes/{sandbox_id}/terminate"
        key = idempotency_key or f"sandbox-terminate-{uuid.uuid4()}"
        response = await self._client._request(
            "POST", path, json={},
            idempotency_key=key,
        )
        self._absorb(_mutation_receipt(self._client, response, path, key, expected_id=sandbox_id))
        return self


class AsyncSandboxExec(_SandboxExecState):
    def __init__(self, client: Any, sandbox_id: str, exec_id: str = ""):
        self._client = client
        self._init_exec_state(sandbox_id, exec_id)

    def _path(self) -> str:
        return f"/v1/sandboxes/{_valid_id(self.sandbox_id, 'sandbox')}/execs/{_valid_id(self.id, 'sandbox execution')}"

    async def refresh(self, *, wait: bool = False, _timeout: float | None = None) -> "AsyncSandboxExec":
        path = self._path()
        response = await self._client._request(
            "GET", path, params={"wait": str(wait).lower()},
            timeout=_timeout if _timeout is not None else (35 if wait else None),
            max_retries=0 if _timeout is not None else None,
        )
        self._absorb(self._client._one(response, "GET", path))
        return self

    async def wait(self, *, timeout_seconds: float | None = None) -> "AsyncSandboxExec":
        if timeout_seconds is not None and (not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be finite and greater than zero")
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        while not self.is_terminal:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise APITimeoutError(f"sandbox execution {self.id} did not finish in time")
            await self.refresh(wait=True, _timeout=None if remaining is None else min(35, remaining))
        return self

    async def output(self, *, after: int = 0, limit: int = 16, wait: bool = False) -> SandboxOutputPage:
        path = self._path() + "/stream"
        response = await self._client._request(
            "GET", path,
            params={"after": after, "limit": limit, "wait": str(wait).lower()},
            timeout=35 if wait else None,
        )
        return SandboxOutputPage.from_dict(self._client._one(response, "GET", path))

    async def iter_output(self, *, after: int = 0, follow: bool = True) -> AsyncIterator[SandboxOutputFrame]:
        """Yield ordered output, draining only currently stored frames when follow is false."""
        cursor = after
        end_sequence = None
        while True:
            previous_cursor = cursor
            limit = 16 if end_sequence is None else min(16, end_sequence - cursor)
            page = await self.output(after=cursor, limit=limit, wait=follow)
            if not follow and end_sequence is None:
                end_sequence = page.last_sequence
            for frame in page.frames:
                if frame.sequence > cursor:
                    cursor = frame.sequence
                    yield frame
            cursor = max(cursor, page.next_sequence)
            if (page.done and page.complete) or (end_sequence is not None and cursor >= end_sequence):
                return
            if (page.done or end_sequence is not None) and cursor <= previous_cursor:
                raise NodusError(f"Sandbox execution final output is unavailable after sequence {cursor}. Retry output(after={cursor}).")

    async def write(
        self, data: bytes | str = b"", *, eof: bool = False,
        idempotency_key: str | None = None,
    ) -> SandboxInputReceipt:
        raw = data.encode() if isinstance(data, str) else bytes(data)
        path = self._path() + "/stdin"
        response = await self._client._request(
            "POST", path,
            json={"data": base64.b64encode(raw).decode("ascii"), "eof": eof},
            idempotency_key=idempotency_key or f"sandbox-stdin-{uuid.uuid4()}",
        )
        return SandboxInputReceipt.from_dict(self._client._one(response, "POST", path))

    async def resize(self, rows: int, cols: int) -> None:
        """Set terminal dimensions for a tty execution."""
        _terminal_size(rows, cols)
        await self._client._request("POST", self._path() + "/resize", json={"rows": rows, "cols": cols})

    async def cancel(self) -> "AsyncSandboxExec":
        """Request cancellation without terminating the sandbox."""
        path = self._path() + "/cancel"
        response = await self._client._request("POST", path, json={})
        self._absorb(self._client._one(response, "POST", path))
        return self
