"""Reviewed reinforcement learning recipe runs."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import math
import re
from typing import TYPE_CHECKING, Any

from .errors import APIError, NodusError, ValidationError
from ._outputs import portable_output_name

if TYPE_CHECKING:
    from . import AsyncWorkload, Workload


__all__ = [
    "RL", "AsyncRL", "RLRecipe", "RLRunPreview", "RLEvent", "RLEventRow",
    "RLEventPage", "RLGradingReceipt", "RLGradingResults",
    "RLEnvironment", "RLExample",
]


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise APIError(f"RL response field {name!r} must be an object", body=value)
    return deepcopy(dict(value))


def _rows(value: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise APIError(f"RL response field {name!r} must be a list of objects", body=value)
    return [deepcopy(dict(item)) for item in value]


def _strings(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise APIError(f"RL response field {name!r} must be a list of strings", body=value)
    return list(value)


def _required_text(row: Mapping[str, Any], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value:
        raise APIError(f"RL response field {name!r} must be nonempty text", body=dict(row))
    return value


def _optional_text(row: Mapping[str, Any], name: str) -> str | None:
    value = row.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise APIError(f"RL response field {name!r} must be text", body=dict(row))
    return value


def _required_bool(row: Mapping[str, Any], name: str) -> bool:
    value = row.get(name)
    if not isinstance(value, bool):
        raise APIError(f"RL response field {name!r} must be true or false", body=dict(row))
    return value


def _required_int(row: Mapping[str, Any], name: str, minimum: int = 0) -> int:
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise APIError(f"RL response field {name!r} must be an integer >= {minimum}", body=dict(row))
    return value


def _optional_number(row: Mapping[str, Any], name: str) -> float | None:
    value = row.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise APIError(f"RL response field {name!r} must be a finite number", body=dict(row))
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise APIError(f"RL response field {name!r} must be a finite number", body=dict(row))
    return value


def _events_request(workload_id: str, after: str | None, limit: int) -> tuple[str, dict[str, Any]]:
    from . import _valid_id

    path = f"/v1/workloads/{_valid_id(workload_id)}/rl-events"
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValidationError("limit must be an integer between 1 and 200")
    params: dict[str, Any] = {"limit": limit}
    if after is not None:
        if not isinstance(after, str) or len(after) > 64:
            raise ValidationError("after must be an opaque cursor of at most 64 characters")
        params["after"] = after
    return path, params


def _grading_request(workload_id: str, revision: int) -> tuple[str, dict[str, int]]:
    from . import _valid_id

    path = f"/v1/workloads/{_valid_id(workload_id)}/rl-grading-results"
    if isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= 2147483647:
        raise ValidationError("revision must be an integer between 1 and 2147483647")
    return path, {"revision": revision}


def _replayed(headers: Mapping[str, str]) -> bool:
    for name, value in headers.items():
        if name.lower() == "idempotent-replayed":
            return value.strip().lower() == "true"
    return False


def _configuration(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("configuration must be an explicit mapping")
    return deepcopy(dict(value))


def _launch_body(
    configuration: Any,
    review_token: Any,
    idempotency_key: Any,
) -> tuple[dict[str, Any], str]:
    body = _configuration(configuration)
    if "review_token" in body:
        raise ValidationError(
            "configuration must not contain review_token, pass the reviewed token separately"
        )
    if not isinstance(review_token, str) or not review_token:
        raise ValidationError("review_token must be the nonempty token returned by preview")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValidationError("idempotency_key must be a nonempty stable key for this run")
    body["review_token"] = review_token
    return body, idempotency_key


_CATALOG_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def _catalog_id(label: str, value: Any) -> str:
    if not isinstance(value, str) or not _CATALOG_ID.fullmatch(value):
        raise ValidationError(f"{label} must be an id from list_environments(), not {value!r}")
    return value


def _summary_path(workload_id: str) -> str:
    from . import _valid_id

    return f"/v1/workloads/{_valid_id(workload_id)}/rl-summary"


def _example_workload(body: Any) -> dict[str, Any]:
    workload = body.get("workload") if isinstance(body, Mapping) else None
    if not isinstance(workload, dict):
        raise APIError("RL example workload response must contain a workload object", body=body)
    return workload


@dataclass(frozen=True)
class RLPhaseSummary:
    """One phase of an RL run: each attempt counted once, as its latest report."""

    attempts: int
    scored: int
    passed: int
    failed: int
    errors: int
    in_progress: int
    pass_rate: float | None
    mean_reward: float | None
    reward_samples: int

    @classmethod
    def from_dict(cls, value: Any) -> "RLPhaseSummary":
        row = _mapping(value, "phase summary")
        counts = {name: _required_int(row, name, 0) for name in
                  ("attempts", "scored", "passed", "failed", "errors", "in_progress", "reward_samples")}
        return cls(pass_rate=_optional_number(row, "pass_rate"), mean_reward=_optional_number(row, "mean_reward"), **counts)


@dataclass(frozen=True)
class RLComparison:
    """Baseline against evaluation, as the console's run page compares them.

    ``comparable`` is False when the phases scored different numbers of
    attempts or different tasks, or when some task events were not stored. ``measurable`` is also False when every
    attempt passed, or every attempt failed, in both. ``resolution_pp`` is the
    smallest change the task count can show.
    """

    baseline_pass_rate: float
    evaluation_pass_rate: float
    change_pp: float
    resolution_pp: float
    comparable: bool
    measurable: bool
    reason: str
    message: str

    @classmethod
    def from_dict(cls, value: Any) -> "RLComparison":
        row = _mapping(value, "comparison")
        numbers = {}
        for name in ("baseline_pass_rate", "evaluation_pass_rate", "change_pp", "resolution_pp"):
            number = _optional_number(row, name)
            if number is None:
                raise APIError(f"RL response field {name!r} must be a number", body=dict(row))
            numbers[name] = number
        flags = {}
        for name in ("comparable", "measurable"):
            if not isinstance(row.get(name), bool):
                raise APIError(f"RL response field {name!r} must be a boolean", body=dict(row))
            flags[name] = row[name]
        return cls(reason=_required_text(row, "reason"), message=_optional_text(row, "message") or "", **numbers, **flags)


@dataclass(frozen=True)
class RLSummary:
    """What the console's run page shows for an RL run."""

    workload_id: str
    phases: dict[str, RLPhaseSummary]
    comparison: RLComparison | None
    dropped_events: int
    truncated: bool

    @classmethod
    def from_dict(cls, value: Any) -> "RLSummary":
        row = _mapping(value, "RL summary")
        phases = _mapping(row.get("phases"), "phases")
        comparison = row.get("comparison")
        return cls(
            workload_id=_required_text(row, "workload_id"),
            phases={name: RLPhaseSummary.from_dict(phases.get(name)) for name in ("baseline", "training", "evaluation")},
            comparison=None if comparison is None else RLComparison.from_dict(comparison),
            dropped_events=_required_int(row, "dropped_events", 0),
            truncated=_required_bool(row, "truncated"),
        )


def _example_request(environment_id: Any, example_id: Any, idempotency_key: Any, name: Any) -> tuple[str, dict[str, Any]]:
    for label, value in (("environment_id", environment_id), ("example_id", example_id)):
        if not isinstance(value, str) or not _CATALOG_ID.fullmatch(value):
            raise ValidationError(f"{label} must be an id from list_environments(), not {value!r}")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValidationError("idempotency_key must be a nonempty stable key for this run")
    if name is not None and (not isinstance(name, str) or not name):
        raise ValidationError("name must be nonempty text when given")
    return f"/v1/rl-environments/{environment_id}/examples/{example_id}/runs", ({"name": name} if name else {})


@dataclass(frozen=True)
class RLRecipe:
    """One server-advertised recipe and its current availability."""

    id: str
    version: str
    environment_id: str
    name: str
    model: str
    model_revision: str
    taskset: str
    modes: list[str]
    available: bool
    unavailable_reason: str | None
    defaults: dict[str, Any]
    limits: dict[str, Any]
    outputs: list[dict[str, Any]]
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Any) -> "RLRecipe":
        row = _mapping(value, "recipe")
        return cls(
            id=_required_text(row, "id"),
            version=_required_text(row, "version"),
            environment_id=_required_text(row, "environment_id"),
            name=_required_text(row, "name"),
            model=_required_text(row, "model"),
            model_revision=_required_text(row, "model_revision"),
            taskset=_required_text(row, "taskset"),
            modes=_strings(row.get("modes"), "modes"),
            available=_required_bool(row, "available"),
            unavailable_reason=_optional_text(row, "unavailable_reason"),
            defaults=_mapping(row.get("defaults"), "defaults"),
            limits=_mapping(row.get("limits"), "limits"),
            outputs=_rows(row.get("outputs"), "outputs"),
            raw=row,
        )


@dataclass(frozen=True)
class RLRunPreview:
    """Server-normalized configuration and the evidence needed to review it."""

    recipe: RLRecipe
    configuration: dict[str, Any]
    review_token: str
    phases: list[dict[str, Any]]
    outputs: list[dict[str, Any]]
    estimate: dict[str, Any]
    launchable: bool
    blocking_reasons: list[str]
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Any) -> "RLRunPreview":
        row = _mapping(value, "preview")
        token = row.get("review_token")
        if not isinstance(token, str):
            raise APIError("RL response field 'review_token' must be text", body=row)
        return cls(
            recipe=RLRecipe.from_dict(row.get("recipe")),
            configuration=_mapping(row.get("configuration"), "configuration"),
            review_token=token,
            phases=_rows(row.get("phases"), "phases"),
            outputs=_rows(row.get("outputs"), "outputs"),
            estimate=_mapping(row.get("estimate"), "estimate"),
            launchable=_required_bool(row, "launchable"),
            blocking_reasons=_strings(row.get("blocking_reasons"), "blocking_reasons"),
            raw=row,
        )


@dataclass(frozen=True)
class RLEvent:
    """Application-reported task evidence, separate from workload lifecycle."""

    event_id: str
    phase: str
    task_id: str
    attempt: int
    kind: str
    outcome: str | None
    reward: float | None
    duration_ms: float | None
    message: str | None
    input: str | None
    output: str | None
    verifier: str | None
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Any) -> "RLEvent":
        row = _mapping(value, "event")
        return cls(
            event_id=_required_text(row, "event_id"), phase=_required_text(row, "phase"),
            task_id=_required_text(row, "task_id"), attempt=_required_int(row, "attempt", 1),
            kind=_required_text(row, "kind"), outcome=_optional_text(row, "outcome"),
            reward=_optional_number(row, "reward"), duration_ms=_optional_number(row, "duration_ms"),
            message=_optional_text(row, "message"), input=_optional_text(row, "input"),
            output=_optional_text(row, "output"), verifier=_optional_text(row, "verifier"), raw=row,
        )


@dataclass(frozen=True)
class RLEventRow:
    """One replay row with its original string identity and execution generation."""

    id: str
    stage_id: str
    generation: int
    received_at: str
    event: RLEvent
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Any) -> "RLEventRow":
        row = _mapping(value, "event row")
        return cls(
            id=_required_text(row, "id"), stage_id=_required_text(row, "stage_id"),
            generation=_required_int(row, "generation"), received_at=_required_text(row, "received_at"),
            event=RLEvent.from_dict(row.get("event")), raw=row,
        )


@dataclass(frozen=True)
class RLEventPage:
    """A replay page and loss metadata. An empty page does not mean training ended."""

    schema_version: int
    events: list[RLEventRow]
    next_cursor: str
    has_more: bool
    dropped_events: int
    truncated: bool
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Any) -> "RLEventPage":
        row = _mapping(value, "event page")
        cursor = row.get("next_cursor")
        if not isinstance(cursor, str):
            raise APIError("RL response field 'next_cursor' must be text", body=row)
        return cls(
            schema_version=_required_int(row, "schema_version", 1),
            events=[RLEventRow.from_dict(item) for item in _rows(row.get("events"), "events")],
            next_cursor=cursor, has_more=_required_bool(row, "has_more"),
            dropped_events=_required_int(row, "dropped_events"),
            truncated=_required_bool(row, "truncated"), raw=row,
        )


@dataclass(frozen=True)
class RLGradingReceipt:
    """A public grading receipt. Missing reward is distinct from a measured zero."""

    attempt_id: str
    request_id: str
    task_id: str
    candidate_sha256: str
    plan_sha256: str
    manifest_sha256: str
    state: str
    reward: float | None
    cleanup_complete: bool
    infrastructure_code: str | None
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Any) -> "RLGradingReceipt":
        row = _mapping(value, "grading receipt")
        return cls(
            attempt_id=_required_text(row, "attempt_id"), request_id=_required_text(row, "request_id"),
            task_id=_required_text(row, "task_id"), candidate_sha256=_required_text(row, "candidate_sha256"),
            plan_sha256=_required_text(row, "plan_sha256"), manifest_sha256=_required_text(row, "manifest_sha256"),
            state=_required_text(row, "state"), reward=_optional_number(row, "reward"),
            cleanup_complete=_required_bool(row, "cleanup_complete"),
            infrastructure_code=_optional_text(row, "infrastructure_code"), raw=row,
        )


@dataclass(frozen=True)
class RLGradingResults:
    """Server-verified grading receipts for one explicit workload revision."""

    schema_version: int
    workload_id: str
    revision: int
    plan_sha256: str
    parent_status: str
    grading_cleanup_complete: bool
    receipts: list[RLGradingReceipt]
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Any) -> "RLGradingResults":
        row = _mapping(value, "grading results")
        return cls(
            schema_version=_required_int(row, "schema_version", 1), workload_id=_required_text(row, "workload_id"),
            revision=_required_int(row, "revision", 1), plan_sha256=_required_text(row, "plan_sha256"),
            parent_status=_required_text(row, "parent_status"),
            grading_cleanup_complete=_required_bool(row, "grading_cleanup_complete"),
            receipts=[RLGradingReceipt.from_dict(item) for item in _rows(row.get("receipts"), "receipts")], raw=row,
        )


@dataclass(frozen=True)
class RLExample:
    """One runnable example from the catalog, with what was measured by running it.

    ``run_arguments()`` returns everything ``Client.run`` needs. It never sets a
    budget.
    """

    environment_id: str
    name: str
    dataset: str
    mode: str
    summary: str
    command: str
    image: str
    model: str
    planned_tasks: int
    results: list[str]
    baseline_pass_rate: float | None
    measured_change_pp: float | None
    runtime_minutes: int | None
    id: str = ""
    peak_memory_gb: float | None = None

    def run_arguments(self) -> dict[str, Any]:
        from ._rl_setup import RLSetup

        outputs = _output_names(self.results)
        return {
            # An example command chains steps with &&, so it needs a shell, as
            # the console gives it. Split into argv, the && would be a literal
            # argument and the run would fail on its first word.
            "command": ["sh", "-c", self.command],
            "image": self.image,
            "outputs": outputs,
            **({"peak_memory_gb": self.peak_memory_gb} if self.peak_memory_gb else {}),
            "rl": RLSetup(environment_id=self.environment_id, mode=self.mode,
                          model=self.model, planned_tasks=self.planned_tasks,
                          example_id=self.id),
        }


@dataclass(frozen=True)
class RLEnvironment:
    """One catalog environment. ``examples`` is empty when nothing is runnable yet."""

    id: str
    name: str
    publisher: str
    summary: str
    category: str
    source_url: str
    modes: list[str]
    examples: list[RLExample]
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Any) -> "RLEnvironment":
        row = _mapping(value, "environment")
        environment_id = _required_text(row, "id")
        examples: list[RLExample] = []
        launch = row.get("launch")
        if launch is not None:
            launch = _mapping(launch, "launch")
            image, model = _required_text(launch, "image"), _required_text(launch, "model")
            planned = _required_int(launch, "default_tasks", 1)
            if planned > 10000:
                raise APIError("RL response field 'default_tasks' must be at most 10000", body=dict(launch))
            results = _strings(launch["results"], "results") if launch.get("results") is not None else []
            memory = _optional_number(launch, "peak_memory_gb")
            for item in _rows(launch.get("examples"), "examples"):
                # A command still holding its placeholder never had a runner URL
                # resolved; running it would pay for a machine that cannot start.
                if "{{" in str(item.get("command", "")):
                    continue
                minutes = item.get("runtime_minutes")
                if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1):
                    raise APIError("RL response field 'runtime_minutes' must be a positive integer", body=dict(item))
                mode = item.get("mode", "evaluate")
                if mode not in ("train", "evaluate"):
                    raise APIError("RL response field 'mode' must be train or evaluate", body=dict(item))
                examples.append(RLExample(
                    environment_id=environment_id,
                    name=_required_text(item, "name"),
                    dataset=_required_text(item, "dataset"),
                    mode=mode,
                    summary=_optional_text(item, "summary") or "",
                    command=_required_text(item, "command"),
                    image=image,
                    model=model,
                    planned_tasks=planned,
                    results=list(results),
                    baseline_pass_rate=_optional_number(item, "baseline_pass_rate"),
                    measured_change_pp=_optional_number(item, "measured_change_pp"),
                    runtime_minutes=minutes,
                    id=_optional_text(item, "id") or "",
                    peak_memory_gb=memory,
                ))
        return cls(
            id=environment_id,
            name=_required_text(row, "name"),
            publisher=_required_text(row, "publisher"),
            summary=_required_text(row, "summary"),
            category=_required_text(row, "category"),
            source_url=_required_text(row, "source_url"),
            modes=_strings(row.get("modes"), "modes"),
            examples=examples,
            raw=row,
        )


def _output_names(paths: list[str]) -> dict[str, str]:
    """Name each result file the way the console does, so none is lost.

    Results are stored after the work has run, under 1-64 characters of
    ``[a-z0-9._-]`` with no leading dot or hyphen, so each name is derived to
    meet that rule. Names ``run()`` refuses, a Windows device name such as
    ``con`` or a trailing dot, take a ``result_`` prefix as a reserved one does.
    """
    outputs: dict[str, str] = {}
    for path in paths:
        basename = re.sub(r"[^a-z0-9._-]", "_", path.rsplit("/", 1)[-1].lower())
        if (not basename or basename.startswith("nodus.") or basename[0] in ".-"
                or not portable_output_name(basename)):
            basename = "result_" + basename.rstrip(".")
        basename = basename[:56].rstrip(".")
        name, suffix = basename, 2
        while name in outputs:
            name, suffix = f"{suffix}_{basename}", suffix + 1
        outputs[name] = path
    return outputs


def _environments(body: Any) -> list[RLEnvironment]:
    rows = body.get("environments") if isinstance(body, Mapping) else None
    if not isinstance(rows, list):
        raise APIError("RL environment response must contain an environments list", body=body)
    return [RLEnvironment.from_dict(row) for row in rows]


class RL:
    """Synchronous discovery, preview, and reviewed launch operations."""

    def __init__(self, client: Any):
        self._client = client

    def events(self, workload_id: str, *, after: str | None = None, limit: int = 100) -> RLEventPage:
        """Read one task-evidence page. Pass next_cursor unchanged as after."""
        path, params = _events_request(workload_id, after, limit)
        response = self._client._request("GET", path, params=params)
        return RLEventPage.from_dict(self._client._one(response, "GET", path))

    def grading_results(self, workload_id: str, *, revision: int) -> RLGradingResults:
        """Read server-verified grading receipts for an explicit parent revision."""
        path, params = _grading_request(workload_id, revision)
        response = self._client._request("GET", path, params=params)
        return RLGradingResults.from_dict(self._client._one(response, "GET", path))

    def list_environments(self) -> list[RLEnvironment]:
        """Everything the catalog lists, with the examples you can run today."""
        path = "/v1/rl-environments"
        return _environments(self._client._one(self._client._request("GET", path), "GET", path))

    def run_example(
        self, environment_id: str, example_id: str, *, idempotency_key: str, name: str | None = None,
    ) -> "Workload":
        """Start a catalog example in one request and return its workload.

        The server builds the run the example describes. Retrying with the
        same ``idempotency_key``, example and name returns the original run.
        """
        from . import Workload

        path, body = _example_request(environment_id, example_id, idempotency_key, name)
        answered: dict[str, str] = {}
        response = self._client._request("POST", path, json=body, idempotency_key=idempotency_key,
                                         headers_out=answered)
        workload = Workload(self._client)
        workload._absorb(self._client._one(response, "POST", path))
        workload.replayed = _replayed(answered)
        if not workload.id:
            raise NodusError("RL example run returned no workload id", body=response)
        return workload

    def get_environment(self, environment_id: str) -> RLEnvironment:
        """One catalog environment and the examples you can run today."""
        path = f"/v1/rl-environments/{_catalog_id('environment_id', environment_id)}"
        return RLEnvironment.from_dict(self._client._one(self._client._request("GET", path), "GET", path))

    def example_workload(self, environment_id: str, example_id: str) -> dict[str, Any]:
        """The workload ``run_example()`` would submit, without starting it."""
        path = (f"/v1/rl-environments/{_catalog_id('environment_id', environment_id)}"
                f"/examples/{_catalog_id('example_id', example_id)}/workload")
        return _example_workload(self._client._one(self._client._request("GET", path), "GET", path))

    def summary(self, workload_id: str) -> RLSummary:
        """Per-phase results and the before-and-after comparison of an RL run."""
        path = _summary_path(workload_id)
        return RLSummary.from_dict(self._client._one(self._client._request("GET", path), "GET", path))

    def list_recipes(self) -> list[RLRecipe]:
        path = "/v1/rl-recipes"
        body = self._client._one(self._client._request("GET", path), "GET", path)
        recipes = body.get("recipes")
        if not isinstance(recipes, list):
            raise APIError("RL recipe response must contain a recipes list", body=body)
        return [RLRecipe.from_dict(row) for row in recipes]

    def preview(self, configuration: Mapping[str, Any]) -> RLRunPreview:
        path = "/v1/rl-runs/preview"
        body = self._client._request("POST", path, json=_configuration(configuration))
        return RLRunPreview.from_dict(self._client._one(body, "POST", path))

    def launch(
        self,
        *,
        configuration: Mapping[str, Any],
        review_token: str,
        idempotency_key: str,
    ) -> "Workload":
        from . import Workload

        path = "/v1/rl-runs"
        body, key = _launch_body(configuration, review_token, idempotency_key)
        answered: dict[str, str] = {}
        response = self._client._request(
            "POST",
            path,
            json=body,
            idempotency_key=key,
            headers_out=answered,
        )
        workload = Workload(self._client)
        workload._absorb(self._client._one(response, "POST", path))
        workload.replayed = _replayed(answered)
        if not workload.id:
            raise NodusError("RL launch returned no workload id", body=response)
        return workload


class AsyncRL:
    """Asynchronous discovery, preview, and reviewed launch operations."""

    def __init__(self, client: Any):
        self._client = client

    async def events(self, workload_id: str, *, after: str | None = None, limit: int = 100) -> RLEventPage:
        """Read one task-evidence page. Pass next_cursor unchanged as after."""
        path, params = _events_request(workload_id, after, limit)
        response = await self._client._request("GET", path, params=params)
        return RLEventPage.from_dict(self._client._one(response, "GET", path))

    async def grading_results(self, workload_id: str, *, revision: int) -> RLGradingResults:
        """Read server-verified grading receipts for an explicit parent revision."""
        path, params = _grading_request(workload_id, revision)
        response = await self._client._request("GET", path, params=params)
        return RLGradingResults.from_dict(self._client._one(response, "GET", path))

    async def list_environments(self) -> list[RLEnvironment]:
        """Everything the catalog lists, with the examples you can run today."""
        path = "/v1/rl-environments"
        response = await self._client._request("GET", path)
        return _environments(self._client._one(response, "GET", path))

    async def run_example(
        self, environment_id: str, example_id: str, *, idempotency_key: str, name: str | None = None,
    ) -> "AsyncWorkload":
        """Start a catalog example in one request and return its workload."""
        from . import AsyncWorkload

        path, body = _example_request(environment_id, example_id, idempotency_key, name)
        answered: dict[str, str] = {}
        response = await self._client._request("POST", path, json=body, idempotency_key=idempotency_key,
                                               headers_out=answered)
        workload = AsyncWorkload(self._client)
        workload._absorb(self._client._one(response, "POST", path))
        workload.replayed = _replayed(answered)
        if not workload.id:
            raise NodusError("RL example run returned no workload id", body=response)
        return workload

    async def get_environment(self, environment_id: str) -> RLEnvironment:
        """One catalog environment and the examples you can run today."""
        path = f"/v1/rl-environments/{_catalog_id('environment_id', environment_id)}"
        response = await self._client._request("GET", path)
        return RLEnvironment.from_dict(self._client._one(response, "GET", path))

    async def example_workload(self, environment_id: str, example_id: str) -> dict[str, Any]:
        """The workload ``run_example()`` would submit, without starting it."""
        path = (f"/v1/rl-environments/{_catalog_id('environment_id', environment_id)}"
                f"/examples/{_catalog_id('example_id', example_id)}/workload")
        response = await self._client._request("GET", path)
        return _example_workload(self._client._one(response, "GET", path))

    async def summary(self, workload_id: str) -> RLSummary:
        """Per-phase results and the before-and-after comparison of an RL run."""
        path = _summary_path(workload_id)
        response = await self._client._request("GET", path)
        return RLSummary.from_dict(self._client._one(response, "GET", path))

    async def list_recipes(self) -> list[RLRecipe]:
        path = "/v1/rl-recipes"
        response = await self._client._request("GET", path)
        body = self._client._one(response, "GET", path)
        recipes = body.get("recipes")
        if not isinstance(recipes, list):
            raise APIError("RL recipe response must contain a recipes list", body=body)
        return [RLRecipe.from_dict(row) for row in recipes]

    async def preview(self, configuration: Mapping[str, Any]) -> RLRunPreview:
        path = "/v1/rl-runs/preview"
        response = await self._client._request(
            "POST", path, json=_configuration(configuration)
        )
        return RLRunPreview.from_dict(self._client._one(response, "POST", path))

    async def launch(
        self,
        *,
        configuration: Mapping[str, Any],
        review_token: str,
        idempotency_key: str,
    ) -> "AsyncWorkload":
        from . import AsyncWorkload

        path = "/v1/rl-runs"
        body, key = _launch_body(configuration, review_token, idempotency_key)
        answered: dict[str, str] = {}
        response = await self._client._request(
            "POST",
            path,
            json=body,
            idempotency_key=key,
            headers_out=answered,
        )
        workload = AsyncWorkload(self._client)
        workload._absorb(self._client._one(response, "POST", path))
        workload.replayed = _replayed(answered)
        if not workload.id:
            raise NodusError("RL launch returned no workload id", body=response)
        return workload
