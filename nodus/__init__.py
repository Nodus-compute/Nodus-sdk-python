r"""Run GPU workloads, observe progress, and download collected outputs.

Sign in with ``nodus login`` or configure ``NODUS_API_KEY`` for automation.
Upload local code explicitly or include it in the selected container image.

    import nodus

    with nodus.Client() as client:
        workload = client.run(
            image="pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime",
            command=["python", "-c", "import torch\nprint(torch.cuda.get_device_name(0))"],
        )
        print(workload.id)
        done = workload.wait()
        if not done.succeeded:
            raise RuntimeError(f"Workload ended: {done.status}")
        print(done.logs())
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import math
import os
import re
import time
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from importlib.metadata import PackageNotFoundError, version as _distribution_version
from typing import Any, AsyncIterator, Iterator, Callable
from pathlib import Path

from ._freeze import WorkloadFreeze
from ._outputs import download_path, verified_file, output_destinations
from ._assets import Asset, Assets, AsyncAssets
from ._rl_setup import RLSetup
from ._rl_events import (
    EpisodeScore,
    EventValidationError,
    RawTraceFields,
    RLEventEmitter,
)
from ._rl import (
    RLEnvironment,
    RLExample,
    AsyncRL, RL, RLRecipe, RLRunPreview, RLEvent, RLEventRow, RLEventPage,
    RLGradingReceipt, RLGradingResults,
)
from ._secrets import Secrets, AsyncSecrets
from ._connections import Connections, AsyncConnections
from ._workspaces import Workspaces, AsyncWorkspaces, Workspace, AsyncWorkspace
from ._compute import Compute, AsyncCompute
from ._volumes import Volumes, AsyncVolumes
from ._operations import Operations, AsyncOperations, OperationDefinition, OperationCatalog, WorkloadPage, WorkloadValidation, RunDraft, RunDraftValues, RunDraftPatch

from ._pool_predict import PredictSubscription, ForecastPoint, ForecastQueue, ForecastSeries, ForecastCalibration, PoolForecastSnapshot, PoolForecast, PoolRecommendation, PoolRecommendations, RecommendationOutcome
from ._pool_act_proposals import PoolActOutcome, PoolActProposal, PoolActProposals
from ._pool_actions import PoolActionPolicy, PoolActionSettings, PoolShadowRun, PoolShadowRuns
from ._pool_proposals import PoolProposal, PoolProposals
from ._pools import Pool, PoolHost, HostDevice, EnrollmentToken, Pools, AsyncPools, PoolUtilization, HostUtilization, UtilizationBucket, UtilizationMetrics
from ._sandboxes import (
    AsyncSandbox,
    AsyncSandboxExec,
    AsyncSandboxes,
    Sandbox,
    SandboxExec,
    SandboxExecState,
    SandboxInputReceipt,
    SandboxOutputFrame,
    SandboxOutputPage,
    SandboxState,
    Sandboxes,
)

import httpx

from ._terminal import RunProgress
from ._brief import build_payload, status_filter
from .requests import OutputSink, OutputSpec, Source, Requirements, Placement, Policy, ContinuitySpec, StageInput, StageSpec
from .config import _is_header_safe, read_credentials
from .errors import (
    APIConnectionError,
    APIError,
    AssetInUseError,
    APITimeoutError,
    AuthenticationError,
    BudgetExceededError,
    CapacityUnavailableError,
    ConfigurationError,
    IdempotencyConflictError,
    RunDraftConflictError,
    NodusError,
    NotFoundError,
    RateLimitError,
    SignatureError,
    SpendCheckUnavailableError,
    ValidationError,
    WorkspaceNotReadyError,
    error_from_response,
    asset_id_from_error,
)
from .types import (
    TERMINAL,
    TERMINAL_STATUSES,
    Artifact,
    ComputeClass,
    ContinuityMode,
    Event,
    InterruptTolerance,
    Ledger,
    LedgerEntry,
    ManifestFile,
    Meter,
    Output,
    Route,
    Settlement,
    StageRun,
    UnitMetrics,
    WorkloadStatus,
    WorkloadLink,
)

try:
    # The distribution is nodus_compute; the import name alone would resolve to a
    # different project that happens to be installed, or to nothing.
    __version__ = _distribution_version("nodus_compute")
except PackageNotFoundError:
    # Imported from a source tree that was never installed. Not a release
    # number, and deliberately not one that could be mistaken for one.
    __version__ = "0.0.0+source"

__all__ = [
    "agent", "step", "step_context", "StepOutcomeUnknown", "StepDefinitionConflict", "StepResultExpired", "StepFailed",
    "Asset",
    "RL",
    "AsyncRL",
    "RLRecipe",
    "RLRunPreview",
    "RLSetup",
    "RLEnvironment",
    "RLExample",
    "RLEventEmitter",
    "EpisodeScore",
    "RawTraceFields",
    "EventValidationError",
    "RLEvent",
    "RLEventRow",
    "RLEventPage",
    "RLGradingReceipt",
    "RLGradingResults",
    "Pool",
    "PoolHost",
    "HostDevice",
    "EnrollmentToken",
    "Pools",
    "AsyncPools",
    "PredictSubscription",
    "ForecastPoint",
    "ForecastSeries",
    "ForecastQueue",
    "ForecastCalibration",
    "PoolForecastSnapshot",
    "PoolForecast",
    "PoolRecommendation",
    "PoolRecommendations",
    "PoolProposal",
    "PoolProposals",
    "PoolActOutcome", "PoolActProposal", "PoolActProposals",
    "WorkloadFreeze",
    "PoolActionPolicy", "PoolActionSettings", "PoolShadowRun", "PoolShadowRuns",
    "RecommendationOutcome",
    "PoolUtilization",
    "HostUtilization",
    "UtilizationBucket",
    "UtilizationMetrics",
    "Client",
    "AsyncClient",
    "Operations", "AsyncOperations", "OperationDefinition", "OperationCatalog", "WorkloadPage", "WorkloadValidation",
    "RunDraft", "RunDraftValues", "RunDraftPatch",
    "Workload",
    "WorkloadLink",
    "AsyncWorkload",
    "Sandboxes",
    "Connections",
    "AsyncConnections",
    "Secrets",
    "AsyncSecrets",
    "Sandbox",
    "Workspace",
    "Compute",
    "AsyncCompute",
    "AsyncWorkspace",
    "Workspaces",
    "AsyncWorkspaces",
    "Volumes",
    "AsyncVolumes",
    "WorkspaceNotReadyError",
    "SandboxExec",
    "AsyncSandboxes",
    "AsyncSandbox",
    "AsyncSandboxExec",
    "SandboxState",
    "SandboxExecState",
    "SandboxOutputFrame",
    "SandboxOutputPage",
    "SandboxInputReceipt",
    "Route",
    "StageRun",
    "UnitMetrics",
    "Output",
    "Source",
    "Requirements",
    "Placement",
    "Policy",
    "ContinuitySpec",
    "StageInput",
    "StageSpec",
    "OutputSink",
    "OutputSpec",
    "Artifact",
    "ManifestFile",
    "Event",
    "Ledger",
    "LedgerEntry",
    "Settlement",
    "Meter",
    "ComputeClass",
    "ContinuityMode",
    "InterruptTolerance",
    "WorkloadStatus",
    "TERMINAL",
    "TERMINAL_STATUSES",
    "NodusError",
    "ConfigurationError",
    "AuthenticationError",
    "NotFoundError",
    "ValidationError",
    "IdempotencyConflictError",
    "RunDraftConflictError",
    "RateLimitError",
    "BudgetExceededError",
    "CapacityUnavailableError",
    "SpendCheckUnavailableError",
    "SignatureError",
    "APIError",
    "AssetInUseError",
    "APIConnectionError",
    "APITimeoutError",
    "asset_id_from_error",
    "__version__",
]

# Statuses worth another attempt, and the methods safe to replay. A submission
# carries an Idempotency-Key so POST /v1/workloads is replay-safe too.
_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_RETRY_METHODS = frozenset({"GET", "PUT", "DELETE", "POST"})


# Longest a single backoff will honour a Retry-After for. A larger hint is real
# and worth keeping on the error, but sleeping ten minutes inside one call is
# indistinguishable to the caller from a hang.
_RETRY_AFTER_CAP = 300.0


# Ceiling on one log read: the endpoint sends whatever the job printed, so
# without one the caller's memory is sized by someone else's print statements.
_LOG_MAX_BYTES = 64 * 1024 * 1024


def _retry_after(resp: httpx.Response) -> float | None:
    """Seconds the server asked for, in either spelling the header allows.

    Returned as sent, not clamped: what to do about a very long hint is the
    caller's decision, and :func:`_retry_after_backoff` makes the SDK's own.
    """
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        pass
    else:
        # float() accepts "inf" and "nan"; honouring either turns a header the
        # server chooses into a hang. Only finite seconds count.
        return max(0.0, seconds) if math.isfinite(seconds) else None
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    now = datetime.now(when.tzinfo)
    return max(0.0, (when - now).total_seconds())


def _retry_after_backoff(resp: httpx.Response) -> float | None:
    """The hint a retry will actually wait for."""
    seconds = _retry_after(resp)
    if seconds is None or seconds <= 0:
        return None
    return min(seconds, _RETRY_AFTER_CAP)


# Ceiling on how long a wait holds off after a failed poll. A wait is cheap to
# keep alive and expensive to abandon, so the interval widens — but not so far
# that a finished workload sits unnoticed.
_WAIT_BACKOFF_CAP = 30.0


def _is_transient(exc: BaseException) -> bool:
    """True when another poll could plausibly succeed.

    The same rule the transport retries a single request by, so a wait and a
    lone call never disagree about which failures clear on their own.
    """
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return True
    return isinstance(exc, NodusError) and exc.status_code in _RETRY_STATUSES


class _WaitPolicy:
    """How a long poll answers failure. One copy, so sync and async cannot drift.

    A workload outlives any single request, and keeps billing whatever the
    client believes, so a transient failure widens the interval instead of
    ending the wait. A permanent one, a revoked key, an unknown id, is raised
    immediately, because waiting on it is a program that hangs instead of
    failing.
    """

    def __init__(self, poll_seconds: float, timeout_seconds: float | None = None):
        for name, value in (("poll_seconds", poll_seconds), ("timeout_seconds", timeout_seconds)):
            if name == "timeout_seconds" and value is None:
                continue
            try:
                valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError(f"{name} must be finite and nonnegative")
        self.poll_seconds = poll_seconds
        self.deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        self.failures = 0
        self.last: NodusError | None = None

    def failed(self, exc: NodusError) -> float:
        """Seconds to hold off after a failed poll. Re-raises what will never clear."""
        if not _is_transient(exc):
            raise exc
        self.failures += 1
        self.last = exc
        hinted = getattr(exc, "retry_after", None)
        if hinted:
            return min(float(hinted), _WAIT_BACKOFF_CAP)
        widened = max(self.poll_seconds, 0.05) * 2 ** min(self.failures - 1, 10)
        return min(_WAIT_BACKOFF_CAP, widened)

    def polled(self) -> float:
        """Seconds until the next poll, after one that answered."""
        self.failures, self.last = 0, None
        return self.poll_seconds

    def hold(self, delay: float, workload_id: str) -> float:
        """Seconds to sleep, or the caller's bound expiring as an error.

        Never sleeps past that bound: a widened backoff must not overshoot the
        deadline the caller asked for.
        """
        if self.deadline is None:
            return delay
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise APITimeoutError(f"workload {workload_id} did not finish in time") from self.last
        return max(0.0, min(delay, left))


def _setup_help(missing: list[str]) -> str:
    """What to tell someone whose client cannot be built yet.

    It names the settings, says where their values come from, and gives the
    lines to run. There is no built-in address to fall back on: a guessed one
    is either a domain that does not exist or an account the caller is not on,
    and both answer a setup mistake with a network error.

    ASCII only. This lands on a terminal, and a Windows console on a legacy
    code page turns a typographic ellipsis into a replacement character,
    mojibake in the one message whose whole job is to be read and copied.
    """
    return (
        "You are not signed in to Nodus. Run: nodus login\n"
        "For automation, set NODUS_API_KEY in your secret manager."
    )


# Hosted API shared with the Nodus console. Explicit, environment and saved
# endpoints still take precedence for private deployments and local development.
DEFAULT_BASE_URL = "https://d1a0b732w6344o.cloudfront.net"


def _current_hosted_url(url: str) -> str:
    if url == "https://nodus-api-74it.onrender.com":
        return DEFAULT_BASE_URL
    return url


def _check_scheme(url: str, stacklevel: int) -> None:
    scheme = url.split("://", 1)[0].lower()
    if scheme not in ("http", "https"):
        raise ConfigurationError(
            f"base_url must start with http:// or https://, got {url!r}. "
            "Use --base-url only for a custom deployment."
        )
    if scheme == "http" and not _is_loopback(url):
        warnings.warn(
            f"base_url {url!r} is http://, so the API key is sent in cleartext to "
            "anyone on the path. Use https:// for anything but a control plane "
            "running on this machine.",
            stacklevel=stacklevel,
        )


def _check_header_safe(name: str, value: str) -> None:
    if not _is_header_safe(value):
        raise ConfigurationError(
            f"{name} contains a character that cannot be sent: it must be "
            "printable ASCII with no spaces or line breaks. Check for a "
            "newline picked up from a file, or a smart quote pasted from a "
            "browser."
        )


def _resolve(api_key: str | None, base_url: str | None) -> tuple[str, str]:
    """Both settings, highest source first, decided one setting at a time.

    Env above the file so a stale login can never outrank what CI injected.
    per setting because a stored key against an env address is the normal way
    to point the same account at staging.
    """
    key = (api_key or os.environ.get("NODUS_API_KEY") or "").strip()
    url = (base_url or os.environ.get("NODUS_BASE_URL") or "").strip().rstrip("/")
    if not key or not url:
        stored_key, stored_url = read_credentials()
        key = key or stored_key.strip()
        url = url or stored_url.strip().rstrip("/")
    url = _current_hosted_url(url or DEFAULT_BASE_URL)
    missing = [
        name
        for name, value in (("NODUS_BASE_URL", url), ("NODUS_API_KEY", key))
        if not value
    ]
    if missing:
        raise ConfigurationError(_setup_help(missing))
    _check_header_safe("NODUS_BASE_URL", url)
    _check_header_safe("NODUS_API_KEY", key)
    _check_scheme(url, stacklevel=4)
    return key, url


def _resolve_base_url(base_url: str | None) -> str:
    """The address alone, for ``nodus login``, which has no key yet."""
    url = (base_url or os.environ.get("NODUS_BASE_URL") or "").strip().rstrip("/")
    if not url:
        url = read_credentials()[1].strip().rstrip("/")
    if not url:
        url = DEFAULT_BASE_URL
    url = _current_hosted_url(url)
    _check_header_safe("NODUS_BASE_URL", url)
    _check_scheme(url, stacklevel=3)
    return url


def _is_loopback(url: str) -> bool:
    """Whether this address never leaves the machine."""
    host = (httpx.URL(url).host or "").lower()
    return host in ("localhost", "::1") or host.startswith("127.")


#: What an identifier may contain. Everything the control plane mints is inside
#: this; everything outside it is a URL, not an id.
_WORKLOAD_ID = re.compile(r"\A[A-Za-z0-9_-]+\Z")


def _valid_id(workload_id: Any) -> str:
    """One path segment, checked before it can become part of a URL.

    httpx resolves dot segments, so an id carrying ``/`` or ``..`` addresses a
    different endpoint, the webhook secret included. Server-supplied ids are
    checked too: a handle refreshes on the id it is holding.
    """
    if isinstance(workload_id, str) and _WORKLOAD_ID.match(workload_id):
        return workload_id
    raise ValidationError(
        f"{workload_id!r} is not a workload id: an id is letters, digits, "
        "underscores and hyphens. It becomes one segment of /v1/workloads/{id}, "
        "where a slash or a dot segment addresses a different endpoint entirely."
    )


def _valid_idempotency_key(key: str) -> str:
    """A key that can survive being a header.

    Non-ASCII cannot be encoded into one and CRLF makes a header the server
    rejects on every attempt, either failure surfaces from inside the
    transport, after the retry budget, naming neither the key nor the call.
    """
    if _is_header_safe(key):
        return key
    raise ValidationError(
        f"idempotency_key {key!r} cannot be sent as a header: it must be "
        "printable ASCII with no spaces or line breaks."
    )


def _was_replayed(headers: dict[str, str]) -> bool:
    """Whether the control plane answered from an idempotency record.

    Anything but a case-insensitive "true" reads as a fresh submission:
    claiming a replay that did not happen is the reading that loses a run.
    """
    for name, value in headers.items():
        if name.lower() == "idempotent-replayed":
            return value.strip().lower() == "true"
    return False


def _redact(key: str) -> str:
    """Enough of the key to tell which credential is configured, never enough to use."""
    return f"{key[:6]}...{key[-4:]}" if len(key) > 12 else "***"


def _headers(api_key: str, client: str | None = None) -> dict[str, str]:
    from . import _client_identity
    return {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": f"nodus-python/{__version__}",
        _client_identity.HEADER: _client_identity.identify(client or _client_identity.current()),
    }


# Identity, not value: a handle is a live view of one workload, so two handles
# on the same id are two things to refresh, and equality on the fields would
# make every blank handle equal to every other one.
@dataclass(eq=False)
class _WorkloadState:
    """Fields shared by the sync and async workload handles."""

    id: str = ""
    owner_user_id: str | None = None
    status: Any = None
    route: Route | None = None
    #: Settled charges only. What a running workload costs is ``cost_now_usd``.
    spend_usd: float = 0.0
    budget_usd: float = 0.0
    #: Settled plus accruing, as of an instant. ``None`` until a read sends one.
    meter: Meter | None = None
    revision: int = 1
    stages: list[StageRun] = field(default_factory=list)
    links: list[WorkloadLink] = field(default_factory=list)
    sink_error: str = ""
    unit_metrics: UnitMetrics | None = None
    #: True when the control plane answered from an idempotency record: the
    #: submission already existed, this call did not create a second run.
    replayed: bool = False
    created_at: Any = None
    updated_at: Any = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def _absorb(self, d: dict[str, Any], *, authoritative: bool = False) -> None:
        """Update in place, preserving omitted fields in partial responses.

        In place rather than returning a new handle: after ``wait()`` the caller
        reads attributes off the object it already has, without another round
        trip and without having to rebind.

        An absent key is not a key set to nothing. A list row carries no route
        and no stages, and a body that clobbered them would leave the handle
        claiming the workload has none, worse for ``status``, where None is
        never terminal and a wait polling it can never end.
        """
        from .types import _dt, _int, _num, _obj, _rows  # local import: internal helpers

        # Submit answers with "workload_id" and every read endpoint with "id".
        # Both spellings name the same workload.
        d = _obj(d)
        self.id = d.get("id") or d.get("workload_id") or self.id
        if "owner_user_id" in d:
            self.owner_user_id = d["owner_user_id"]
        # Detail responses omit sink_error once no failed load remains.
        if authoritative or "sink_error" in d:
            self.sink_error = str(d.get("sink_error") or "")
        if "status" in d:
            self.status = WorkloadStatus.coerce(d.get("status"))
        self.route = Route.from_dict(d.get("route")) or self.route
        if "spend_usd" in d:
            self.spend_usd = _num(d.get("spend_usd"))
        self.meter = Meter.from_dict(d.get("meter")) or self.meter
        outcome = _obj(_obj(d.get("payload")).get("outcome"))
        ceiling = d.get("budget_usd") or outcome.get("max_cost_usd")
        if ceiling is not None:
            self.budget_usd = _num(ceiling, self.budget_usd)
        self.revision = _int(d.get("revision"), self.revision) or self.revision
        if "unit_metrics" in d:
            self.unit_metrics = UnitMetrics.from_dict(d["unit_metrics"])
        if "stages" in d:
            self.stages = [StageRun.from_dict(s) for s in _rows(d.get("stages"))]
        if "links" in d:
            self.links = [link for row in _rows(d.get("links")) if (link := WorkloadLink.from_dict(row)) is not None]
        self.created_at = _dt(d.get("created_at")) or self.created_at
        self.updated_at = _dt(d.get("updated_at")) or self.updated_at
        self.raw = d

    @property
    def cost_now_usd(self) -> float:
        """Return the fixed lifetime compute charge when final, otherwise an estimate.

        The pending estimate includes unposted usage. Check
        ``meter.charge_state`` to distinguish pending metered costs from final.
        """
        if self.meter is None:
            return self.spend_usd
        if self.meter.final_charge_usd is not None:
            return self.meter.final_charge_usd
        return max(self.spend_usd, self.meter.settled_usd) + self.meter.accruing_usd

    @property
    def is_terminal(self) -> bool:
        """True once the status can no longer change."""
        return getattr(self.status, "value", self.status) in TERMINAL

    @property
    def succeeded(self) -> bool:
        """True only for COMPLETED. ``is_terminal`` is also true for failure."""
        return getattr(self.status, "value", self.status) == WorkloadStatus.COMPLETED.value


class _Transport:
    """Shared request policy: auth, error mapping, retry with backoff."""

    def __init__(self, max_retries: int):
        self.max_retries = max(0, int(max_retries))

    def _raise(self, method: str, path: str, resp: httpx.Response) -> None:
        try:
            body: Any = resp.json()
        except Exception:
            body = resp.text
        raise error_from_response(
            method,
            path,
            resp.status_code,
            body,
            retry_after=_retry_after(resp),
            retry_after_header=resp.headers.get("Retry-After"),
            request_id=resp.headers.get("X-Request-Id"),
        )

    def _should_retry(self, method: str, status: int, attempt: int) -> bool:
        return (
            attempt < self.max_retries
            and method.upper() in _RETRY_METHODS
            and status in _RETRY_STATUSES
        )

    @staticmethod
    def _backoff(attempt: int, resp: httpx.Response | None) -> float:
        if resp is not None:
            hinted = _retry_after_backoff(resp)
            if hinted:
                return hinted
        return min(8.0, 0.5 * (2**attempt))

    def _hold(self, attempt: int, resp: httpx.Response | None, slept: float) -> float | None:
        """Seconds to wait before the next attempt, or None to stop retrying.

        The ceiling bounds the whole call, not each sleep inside it: a cap a
        retry loop can multiply is not a cap.
        """
        left = _RETRY_AFTER_CAP - slept
        if left <= 0:
            return None
        return min(self._backoff(attempt, resp), left)

    @staticmethod
    def _too_big(path: str, max_bytes: int) -> NodusError:
        return NodusError(
            f"the log at {path} is larger than {max_bytes} bytes and was not read. "
            "Narrow it with stage= and generation=, which is one attempt's output "
            "rather than every attempt's."
        )

    @staticmethod
    def _unreached(
        cls: type[NodusError], message: str, idempotency_key: str | None
    ) -> NodusError:
        """A failure that leaves the caller unable to say what happened.

        The request may have arrived, in which case the workload exists and is
        billing. The key travels on the error so a retry can be the same
        submission instead of a second paid one.
        """
        if not idempotency_key:
            return cls(message)
        return cls(
            f"{message}\nIt may still have reached the control plane, in which case "
            "the workload exists and is billing. Retry with "
            f"idempotency_key={idempotency_key!r} so the retry is the same "
            "submission rather than a second paid one.",
            body={"idempotency_key": idempotency_key},
        )

    @staticmethod
    def _one(body: Any, method: str, path: str) -> dict[str, Any]:
        """A read that must have answered with an object.

        An empty body absorbed as ``{}`` leaves a handle with no status, which
        no wait can ever see finish.
        """
        if not isinstance(body, dict) or not body:
            raise NodusError(f"{method} {path} returned an empty body", body=body)
        return body


class Client(_Transport):
    """Synchronous Nodus client.

    One instance per process is enough, the underlying transport pools
    connections, so constructing a client per call throws that away. The client
    is safe to share across threads. The workload handles it returns are not.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
    ):
        super().__init__(max_retries)
        key, self.base_url = _resolve(api_key, base_url)
        # The credential lives only in the transport: an attribute holding it
        # is copied out by every automatic dump — logs, debuggers, trackers.
        self._api_key_shown = _redact(key)
        self._http = httpx.Client(
            base_url=self.base_url, timeout=timeout, headers=_headers(key)
        )

    @property
    def api_key(self) -> str:
        """The configured key, redacted. What was sent is in the transport."""
        return self._api_key_shown

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        text: bool = False,
        raw: bool = False,
        content: bytes | None = None,
        headers_out: dict[str, str] | None = None,
        max_bytes: int | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> Any:
        headers = {"Idempotency-Key": _valid_idempotency_key(idempotency_key)} if idempotency_key else {}
        retries = self.max_retries if max_retries is None else max_retries
        request_options = {"timeout": timeout} if timeout is not None else {}
        attempt = 0
        # One waiting budget for the whole call, spent across every retry.
        slept = 0.0
        while True:
            delay: float | None = None
            capped: bytes | None = None
            try:
                if max_bytes is None:
                    resp = self._http.request(
                        method, path, json=json, content=content, headers=headers, params=params, **request_options
                    )
                else:
                    with self._http.stream(
                        method, path, json=json, content=content, headers=headers, params=params, **request_options
                    ) as resp:
                        if resp.status_code < 400:
                            chunks, total = [], 0
                            for chunk in resp.iter_bytes():
                                total += len(chunk)
                                if total > max_bytes:
                                    raise self._too_big(path, max_bytes)
                                chunks.append(chunk)
                            capped = b"".join(chunks)
                        else:
                            resp.read()
            except httpx.TimeoutException as exc:
                if attempt < retries:
                    delay = self._hold(attempt, None, slept)
                if delay is not None:
                    time.sleep(delay)
                    slept += delay
                    attempt += 1
                    continue
                raise self._unreached(
                    APITimeoutError, f"{method} {path} timed out", idempotency_key
                ) from exc
            except httpx.HTTPError as exc:
                if attempt < retries:
                    delay = self._hold(attempt, None, slept)
                if delay is not None:
                    time.sleep(delay)
                    slept += delay
                    attempt += 1
                    continue
                raise self._unreached(
                    APIConnectionError,
                    f"{method} {path} failed to connect: {exc}",
                    idempotency_key,
                ) from exc

            if 300 <= resp.status_code < 400:
                raise self._unreached(
                    APIError,
                    f"{method} {path} returned HTTP {resp.status_code}. "
                    "Redirects are refused. Configure the final API origin.",
                    idempotency_key,
                )

            if resp.status_code >= 400:
                if attempt < retries and self._should_retry(method, resp.status_code, attempt):
                    delay = self._hold(attempt, resp, slept)
                    if delay is not None:
                        time.sleep(delay)
                        slept += delay
                        attempt += 1
                        continue
                self._raise(method, path, resp)

            if headers_out is not None:
                headers_out.update(resp.headers)
            if capped is not None:
                return capped if raw else capped.decode("utf-8", "replace")
            if raw:
                return resp.content
            if not resp.content:
                return "" if text else None
            # The log endpoint answers text/plain, because its whole purpose is
            # to be read. Everything else is JSON.
            if text:
                return resp.text
            try:
                return resp.json()
            except ValueError:
                raise self._unreached(
                    APIError, f"{method} {path} returned invalid JSON", idempotency_key
                ) from None

    # -- workloads ---------------------------------------------------------

    def run(
        self,
        *,
        command: list[str] | str | None = None,
        image: str | None = None,
        source_asset_id: str | None = None,
        inputs: list[dict[str, Any]] | None = None,
        outputs: dict[str, str | OutputSpec] | None = None,
        model: str | None = None,
        peak_memory_gb: float | None = None,
        optimization: str | None = None,
        gpu: str | None = None,
        gpu_count: int | None = None,
        gpu_interconnect: str | None = None,
        budget: float | None = None,
        compute_class: ComputeClass | str | None = None,
        continuity: ContinuityMode | str | ContinuitySpec | dict[str, Any] | None = None,
        finish_by: datetime | str | None = None,
        data_regions: list[str] | None = None,
        connections: list[str] | None = None,
        sweep_id: str | None = None,
        stages: list[StageSpec] | list[dict[str, Any]] | None = None,
        framework: str | None = None,
        policy: Policy | dict[str, Any] | None = None,
        requirements: Requirements | dict[str, Any] | None = None,
        placement: Placement | dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        rl: "RLSetup | dict[str, Any] | None" = None,
        extra: dict[str, Any] | None = None,
        **unknown: Any,
    ) -> "Workload":
        """Submit a brief, requirements and outcomes, never a machine.

        Returns as soon as the workload is accepted. It is not yet placed. Call
        ``client.wait(wl.id)`` to block until it reaches a terminal state.

        ``budget`` is a deprecated compatibility field and does not limit spending. ``finish_by`` takes a datetime or RFC3339
        text. ``extra`` is merged into the payload for a field the control plane
        models and this SDK version does not. Any other keyword is refused
        rather than sent, because the server drops what it does not recognise.

        ``idempotency_key`` defaults to a fresh value per call, which covers
        this call's own retries and nothing beyond it. To make an
        application-level retry loop safe, pass a key derived from the thing you
        are running so a resubmission cannot become a second paid workload.

        A raised :class:`APITimeoutError` or :class:`APIConnectionError` does
        not mean nothing was submitted. Retry with the key on ``err.payload``,
        the one that was sent, so the retry cannot become a second paid run.
        """
        payload = build_payload(
            command=command,
            image=image,
            source_asset_id=source_asset_id,
            inputs=inputs,
            outputs=outputs,
            model=model,
            peak_memory_gb=peak_memory_gb,
            optimization=optimization,
            gpu=gpu,
            gpu_count=gpu_count,
            gpu_interconnect=gpu_interconnect,
            budget=budget,
            compute_class=compute_class,
            continuity=continuity,
            finish_by=finish_by,
            data_regions=data_regions,
            connections=connections,
            sweep_id=sweep_id,
            stages=stages,
            framework=framework,
            policy=policy,
            requirements=requirements,
            placement=placement,
            rl=rl,
            extra=extra,
            **unknown,
        )
        answered: dict[str, str] = {}
        res = self._request(
            "POST",
            "/v1/workloads",
            json=payload,
            idempotency_key=idempotency_key or f"nodus-{uuid.uuid4()}",
            headers_out=answered,
        )
        wl = Workload(self)
        wl._absorb(res or {})
        wl.replayed = _was_replayed(answered)
        if not wl.id:
            raise NodusError("submit returned no workload id", body=res)
        return wl

    def benchmark(self, *, workload: dict[str, Any], gpu_families: list[str],
                  batch_sizes: list[int], regions: list[str], repetitions: int,
                  budget: float | None = None, idempotency_key: str) -> dict[str, Any]:
        """Submit a hardware matrix.

        Reuse the explicit idempotency key after any uncertain response.
        Returned costs are supplied by the server.
        """
        from ._benchmarks import request_payload
        payload = request_payload(workload, gpu_families, batch_sizes, regions,
                                  repetitions, budget, idempotency_key)
        return self._one(self._request("POST", "/v1/benchmarks", json=payload,
                         idempotency_key=idempotency_key), "POST", "/v1/benchmarks")

    def get_benchmark(self, benchmark_id: str) -> dict[str, Any]:
        """Read cell statuses, measurements and workload links."""
        path = f"/v1/benchmarks/{_valid_id(benchmark_id)}"
        return self._one(self._request("GET", path), "GET", path)

    @property
    def pools(self) -> Pools:
        """Manage customer-owned pools and observe-only hosts."""
        return Pools(self)

    @property
    def operations(self) -> Operations:
        """Call typed workload operations through the version 1 interface."""
        return Operations(self)

    @property
    def assets(self) -> Assets:
        """Upload and import code or data for workloads."""
        return Assets(self)

    @property
    def rl(self) -> RL:
        """Discover, preview, and explicitly launch reviewed RL recipes."""
        return RL(self)

    @property
    def connections(self) -> Connections:
        """Manage verified external connections."""
        return Connections(self)

    @property
    def secrets(self) -> Secrets:
        """Manage tenant secrets without reading their values."""
        return Secrets(self)

    @property
    def workspaces(self) -> Workspaces:
        """GPU workspaces: create, start, connect, run jobs, move files, stop."""
        return Workspaces(self)

    @property
    def compute(self) -> Compute:
        """Running instances and training, as the Compute page lists them."""
        return Compute(self)

    def launch(self, gpu: str | None = None, *, gpu_count: int = 1, gpu_memory_gb: float | None = None,
               disk_gb: int = 100, environment: str | None = None, ssh_key: str | None = None,
               name: str | None = None, max_hours: int = 4, keep_files: bool = False, wait: bool = True,
               timeout_seconds: float = 900.0, poll_seconds: float = 5.0,
               idempotency_key: str | None = None) -> Workspace:
        """Rent one GPU machine and wait until SSH accepts connections. Returns its handle.

        Local disk only unless ``keep_files=True``, which saves project files as a workspace. The machine
        stops itself after ``max_hours`` (4 unless set). ``ssh_key`` defaults to ``~/.ssh/id_ed25519.pub``,
        ``id_ecdsa.pub`` or ``id_rsa.pub``. A wait that times out leaves the machine running.
        """
        from ._compute import launch
        return launch(self, gpu, gpu_count=gpu_count, gpu_memory_gb=gpu_memory_gb, disk_gb=disk_gb,
                      environment=environment, ssh_key=ssh_key, name=name, max_hours=max_hours,
                      keep_files=keep_files, wait=wait, timeout_seconds=timeout_seconds,
                      poll_seconds=poll_seconds, idempotency_key=idempotency_key)

    @property
    def volumes(self) -> Volumes:
        """Named storage volumes that sandboxes mount between sessions."""
        return Volumes(self)

    @property
    def agents(self):
        """Deploy managed agents and submit durable work."""
        from ._managed_agents import ManagedAgents
        return ManagedAgents(self)

    @property
    def sandboxes(self) -> Sandboxes:
        """Create and reconnect to durable agent execution environments."""
        return Sandboxes(self)

    def run_file(self, path: str | Path = "nodus.toml") -> "Workload":
        """Submit a validated workload file. Each call submits one workload."""
        from ._workload_file import load_workload_file

        return self.run(**load_workload_file(path))

    def get(self, workload_id: str) -> "Workload":
        path = f"/v1/workloads/{_valid_id(workload_id)}"
        wl = Workload(self)
        wl._absorb(self._one(self._request("GET", path), "GET", path), authoritative=True)
        return wl

    def list(
        self, *, limit: int = 50, offset: int = 0, status: Any = None, scope: str | None = None
    ) -> list["Workload"]:
        """The first page only, use ``iter_workloads()`` for all of them.

        Newest first. ``status`` accepts a member, a wire string, a list of
        either, or the presets ``"active"`` and ``"terminal"``.
        """
        items, _ = self.list_page(limit=limit, offset=offset, status=status, scope=scope)
        return items

    def list_page(
        self, *, limit: int = 50, offset: int = 0, status: Any = None, scope: str | None = None
    ) -> tuple[list["Workload"], int | None]:
        """A page plus the next offset, or ``None`` when the page is the last."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if scope is not None:
            if scope not in ("mine", "team"):
                raise ValueError("scope must be 'mine' or 'team'")
            params["scope"] = scope
        wire = status_filter(status)
        if wire:
            params["status"] = wire
        res = self._request("GET", "/v1/workloads", params=params) or {}
        out = []
        for row in res.get("workloads") or []:
            wl = Workload(self)
            wl._absorb(row)
            out.append(wl)
        return out, res.get("next_offset")

    def iter_workloads(self, *, page_size: int = 50, status: Any = None, scope: str | None = None) -> Iterator["Workload"]:
        """Page lazily so a long history never has to be held in memory.

        Stops when the next offset does not advance: an offset that repeats is
        a stall, and following it re-reads the same page forever.
        """
        offset = 0
        while True:
            page, nxt = self.list_page(limit=page_size, offset=offset, status=status, scope=scope)
            for wl in page:
                yield wl
            if nxt is None or nxt <= offset:
                return
            offset = nxt

    def freeze(self, workload_id: str) -> WorkloadFreeze:
        """Request saved-file freeze. Completion waits for checkpoint and exact cleanup."""
        result = WorkloadFreeze.from_dict(self._request("POST", f"/v1/workloads/{_valid_id(workload_id)}/freeze", json={}), workload_id)
        if result.state not in {"requested", "releasing", "frozen"}:
            raise APIError("The API did not confirm workload freeze intent")
        return result

    def freeze_status(self, workload_id: str) -> WorkloadFreeze:
        """Read retained checkpoint bytes without inferring a storage price."""
        return WorkloadFreeze.from_dict(self._request("GET", f"/v1/workloads/{_valid_id(workload_id)}/freeze"), workload_id)

    def resume(self, workload_id: str) -> WorkloadFreeze:
        """Resume frozen work with its saved files. The customer command must load them."""
        result = WorkloadFreeze.from_dict(self._request("POST", f"/v1/workloads/{_valid_id(workload_id)}/resume", json={}), workload_id)
        if result.state not in {"resuming", "resumed"}:
            raise APIError("The API did not confirm workload resumption")
        return result

    def cancel(self, workload_id: str, *, idempotency_key: str | None = None) -> None:
        self._request(
            "POST",
            f"/v1/workloads/{_valid_id(workload_id)}/cancel",
            # Fresh per call: one key for the life of the workload makes every
            # later cancel a replay of the first, answered without being done.
            idempotency_key=idempotency_key or f"cancel-{workload_id}-{uuid.uuid4()}",
        )

    def events(self, workload_id: str, *, after: int = 0) -> list[Event]:
        """One page of lifecycle events, oldest first.

        The server returns at most 100 and does not say whether more follow.
        Pass ``after=`` the last ``seq`` you saw, or use ``iter_events()``.
        """
        res = self._request("GET", f"/v1/workloads/{_valid_id(workload_id)}/events", params={"after": after})
        return [Event.from_dict(e) for e in (res or {}).get("events") or []]

    def iter_events(self, workload_id: str, *, after: int = 0) -> Iterator[Event]:
        """Every event recorded so far, walking past the server's page cap.

        Stops when the sequence does not advance: that batch is the same batch
        again, and following it reads it forever.
        """
        while True:
            batch = self.events(workload_id, after=after)
            if not batch:
                return
            highest = after
            for ev in batch:
                highest = max(highest, ev.seq)
                yield ev
            if highest <= after:
                return
            after = highest

    def artifacts(self, workload_id: str) -> list[Artifact]:
        res = self._request("GET", f"/v1/workloads/{_valid_id(workload_id)}/artifacts")
        return [Artifact.from_dict(a) for a in (res or {}).get("artifacts") or []]

    def outputs(self, workload_id: str) -> list[Output]:
        """List customer outputs from completed stages."""
        res = self._request("GET", f"/v1/workloads/{_valid_id(workload_id)}/outputs")
        return [Output.from_dict(row) for row in (res or {}).get("outputs") or []]

    def reload_output(self, workload_id: str, name: str, *, stage: str | None = None) -> dict[str, Any]:
        """Retry loading a saved output into its admitted database sink."""
        path = download_path(_valid_id(workload_id), name) + "/reload"
        return self._request("POST", path, params={"stage": stage} if stage is not None else None)

    def routing(self, workload_id: str) -> list[dict[str, Any]]:
        """Return placement history ordered by stage ID, then generation."""
        res = self._request("GET", f"/v1/workloads/{_valid_id(workload_id)}/routing")
        return (res or {}).get("placements") or []

    def download_output(
        self, workload_id: str, name: str, destination: str | os.PathLike[str],
        *, stage: str | None = None, overwrite: bool = True,
    ) -> Path:
        """Stream an output to destination and verify its SHA-256 before replacing it.

        Pass stage when multiple stages publish the same name. Failed transfers
        leave an existing destination intact. Retry the call to restart.
        """
        path = download_path(_valid_id(workload_id), name)
        params = {"stage": stage} if stage is not None else None
        try:
            with self._http.stream("GET", path, params=params, follow_redirects=False,
                                       headers={"Accept-Encoding": "identity"}) as resp:
                if resp.status_code != 200:
                    body = bytearray()
                    for chunk in resp.iter_bytes(chunk_size=16 * 1024):
                        body.extend(chunk[:64 * 1024 - len(body)])
                        if len(body) >= 64 * 1024:
                            break
                    error = httpx.Response(resp.status_code, headers=resp.headers,
                                           content=bytes(body), request=resp.request)
                    self._raise("GET", path, error)
                with verified_file(destination, resp.headers, overwrite=overwrite) as write:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                        write(chunk)
        except httpx.TimeoutException as exc:
            raise APITimeoutError("Output download timed out. Retry to restart.") from exc
        except httpx.HTTPError as exc:
            raise APIConnectionError("Output download interrupted. Retry to restart.") from exc
        return Path(destination)

    def ledger(self, workload_id: str) -> Ledger:
        return Ledger.from_dict(self._request("GET", f"/v1/workloads/{_valid_id(workload_id)}/ledger"))

    def logs(
        self, workload_id: str, *, stage: str | None = None, generation: int | None = None
    ) -> str:
        """What the submitted program printed, read back out of a committed artifact.

        Not a live stream: the log is collected as a named output and the
        control plane recomputes its digest before it agrees the run produced
        it, so this lags the process by a checkpoint. That is the point, what
        comes back is evidence rather than a tail.

        Narrowing by stage and generation matters after a reclaim: a stage that
        was interrupted has several generations, and their logs are different
        stories about the same work.
        """
        params: dict[str, Any] = {}
        if stage:
            params["stage"] = stage
        # `is not None`, so an explicit generation=0 is sent and refused rather
        # than read as "no filter": a caller that computed 0 has a bug.
        if generation is not None:
            params["generation"] = generation
        return self._request(
            "GET",
            f"/v1/workloads/{_valid_id(workload_id)}/logs",
            params=params or None,
            text=True,
            max_bytes=_LOG_MAX_BYTES,
        ) or ""

    def wait(
        self, workload_id: str, *, poll_seconds: float = 2.0,
        timeout_seconds: float | None = None, progress: bool | None = None,
        on_update: Callable[[Any], None] | None = None,
    ) -> "Workload":
        """Wait for completion with optional live output on stderr.

        Progress is automatic in a terminal. Ctrl+C requests cancellation.
        A timeout ends observation and leaves the remote workload running.
        ``on_update`` receives each successful workload read before completion.
        """
        _valid_id(workload_id)
        policy = _WaitPolicy(poll_seconds, timeout_seconds)
        with _cancel_wait_on_interrupt(self, workload_id), RunProgress(workload_id, progress) as display:
            while True:
                try:
                    wl = self.get(workload_id)
                except NodusError as exc:
                    delay = policy.failed(exc)
                else:
                    if on_update is not None:
                        on_update(wl)
                    display.update(wl)
                    for kind, path, params in display.requests():
                        if kind == "saved_logs" and display.live_available and not display.live_empty:
                            continue
                        while True:
                            try:
                                timeout = 2.0 if policy.deadline is None else min(2.0, policy.deadline - time.monotonic())
                                if timeout <= 0:
                                    break
                                headers = {}
                                body = self._request(
                                    "GET", path, params=params, timeout=timeout, max_retries=0,
                                    text=kind == "saved_logs", headers_out=headers,
                                    max_bytes=_LOG_MAX_BYTES if kind == "saved_logs" else None,
                                )
                                if kind == "saved_logs":
                                    display.accept_saved_logs(body, headers.get("x-nodus-stage-id", ""), headers.get("x-nodus-generation", ""))
                                else:
                                    advanced = display.accept(kind, body)
                                    if wl.is_terminal and advanced:
                                        params = {"after": display.event_after if kind == "events" else display.log_after}
                                        continue
                            except NodusError as exc:
                                display.unavailable(kind, exc.status_code)
                            except ValueError:
                                display.unavailable(kind, None)
                            break
                    if wl.is_terminal:
                        return wl
                    delay = policy.polled()
                time.sleep(policy.hold(delay, workload_id))

    def live_logs(self, workload_id: str, *, after: str = "") -> dict[str, Any]:
        """Read new live output chunks using the previous next_cursor."""
        path = f"/v1/workloads/{_valid_id(workload_id)}/logs/live"
        return self._one(self._request("GET", path, params={"after": after}), "GET", path)

    def stream_events(self, workload_id: str, *, poll_seconds: float = 2.0) -> Iterator[Event]:
        """Yield events as they occur, stopping at the terminal event.

        Follows the same policy as ``wait()``: the stream outlives a transient
        failure, and ends on one that will never clear.
        """
        after = 0
        policy = _WaitPolicy(poll_seconds)
        while True:
            try:
                batch = self.events(workload_id, after=after)
                # Whether it is over is only worth a request when the events ran
                # out: a stream that runs for hours would otherwise double its
                # load to ask a question the next batch answers anyway.
                if not batch and self.get(workload_id).is_terminal:
                    return
            except NodusError as exc:
                time.sleep(policy.failed(exc))
                continue
            for ev in batch:
                after = max(after, ev.seq)
                yield ev
            time.sleep(policy.polled())

    # -- webhooks / health -------------------------------------------------

    def set_webhook(self, url: str, *, secret: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"url": url}
        if secret:
            body["secret"] = secret
        return self._request("PUT", "/v1/webhooks", json=body) or {}

    def get_webhook(self) -> dict[str, Any]:
        return self._request("GET", "/v1/webhooks") or {}

    def delete_webhook(self) -> None:
        self._request("DELETE", "/v1/webhooks")

    def healthz(self) -> dict[str, Any]:
        return self._request("GET", "/healthz") or {}

    def readyz(self) -> dict[str, Any]:
        return self._request("GET", "/readyz") or {}


@contextmanager
def _cancel_wait_on_interrupt(client: Client, workload_id: str):
    try:
        yield
    except KeyboardInterrupt as interrupt:
        failure = None
        try:
            client.cancel(workload_id)
        except (NodusError, KeyboardInterrupt) as exc:
            failure = exc
            message = (
                f"Cancellation not confirmed for {workload_id!r}. "
                "Call client.cancel(workload_id) against the same deployment."
            )
            if hasattr(interrupt, "add_note"):
                interrupt.add_note(message)
            try:
                warnings.warn(message, RuntimeWarning, stacklevel=2)
            except RuntimeWarning:
                pass
        interrupt._nodus_cancellation = (workload_id, failure)
        raise


class Workload(_WorkloadState):
    """A handle on one submitted brief.

    Mutable: ``refresh()`` and ``wait()`` update this instance in place and also
    return it, so after either call you can read ``status``, ``route`` and
    ``spend_usd`` without a second round trip. The consequence is that a handle
    is not safe to share across threads, give each one its own from
    ``client.get()``.
    """

    def __init__(self, client: Client):
        super().__init__()
        self._client = client

    def refresh(self) -> "Workload":
        path = f"/v1/workloads/{_valid_id(self.id)}"
        self._absorb(self._client._one(self._client._request("GET", path), "GET", path), authoritative=True)
        return self

    def wait(self, *, poll_seconds: float = 2.0, timeout_seconds: float | None = None,
             progress: bool | None = None, on_update: Callable[[Any], None] | None = None) -> "Workload":
        """Wait in place. A timeout leaves the remote workload running."""
        done = self._client.wait(self.id, poll_seconds=poll_seconds,
                                 timeout_seconds=timeout_seconds, progress=progress, on_update=on_update)
        self._absorb(done.raw, authoritative=True)
        return self

    def events(self, *, after: int = 0) -> list[Event]:
        return self._client.events(self.id, after=after)

    def iter_events(self, *, after: int = 0) -> Iterator[Event]:
        return self._client.iter_events(self.id, after=after)

    def stream_events(self, *, poll_seconds: float = 2.0) -> Iterator[Event]:
        return self._client.stream_events(self.id, poll_seconds=poll_seconds)

    def artifacts(self) -> list[Artifact]:
        return self._client.artifacts(self.id)

    def outputs(self) -> list[Output]:
        """List customer outputs from completed stages."""
        return self._client.outputs(self.id)

    def reload_output(self, name: str, *, stage: str | None = None) -> dict[str, Any]:
        """Retry loading one saved output into its database sink."""
        return self._client.reload_output(self.id, name, stage=stage)

    def routing(self) -> list[dict[str, Any]]:
        """Read this workload's placement history."""
        return self._client.routing(self.id)

    def download(self, destination: str | os.PathLike[str] | None = None) -> list[Path]:
        """Download collected outputs into stage folders, refusing existing files."""
        root = Path(destination) if destination is not None else Path("outputs") / _valid_id(self.id)
        planned = output_destinations(root, self.outputs())
        saved = []
        for output, path in planned:
            path.parent.mkdir(parents=True, exist_ok=True)
            saved.append(self.download_output(output.name, path, stage=output.stage_id, overwrite=False))
        return saved

    def download_output(
        self, name: str, destination: str | os.PathLike[str], *, stage: str | None = None, overwrite: bool = True
    ) -> Path:
        """Download a named output and verify it before replacing destination."""
        return self._client.download_output(self.id, name, destination, stage=stage, overwrite=overwrite)

    def ledger(self) -> Ledger:
        return self._client.ledger(self.id)

    def logs(self, *, stage: str | None = None, generation: int | None = None) -> str:
        """This workload's log. See :meth:`Client.logs`."""
        return self._client.logs(self.id, stage=stage, generation=generation)

    def freeze(self) -> WorkloadFreeze:
        return self._client.freeze(self.id)

    def freeze_status(self) -> WorkloadFreeze:
        return self._client.freeze_status(self.id)

    def resume(self) -> WorkloadFreeze:
        return self._client.resume(self.id)

    def cancel(self) -> None:
        self._client.cancel(self.id)


class AsyncClient(_Transport):
    """Asynchronous Nodus client. Mirrors :class:`Client` with ``await``."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
    ):
        super().__init__(max_retries)
        key, self.base_url = _resolve(api_key, base_url)
        self._api_key_shown = _redact(key)
        self._http = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, headers=_headers(key)
        )

    @property
    def api_key(self) -> str:
        """The configured key, redacted. What was sent is in the transport."""
        return self._api_key_shown

    @property
    def operations(self) -> AsyncOperations:
        """Call typed workload operations through the version 1 interface."""
        return AsyncOperations(self)

    @property
    def connections(self) -> AsyncConnections:
        """Manage verified external connections."""
        return AsyncConnections(self)

    @property
    def secrets(self) -> AsyncSecrets:
        """Manage tenant secrets without reading their values."""
        return AsyncSecrets(self)

    @property
    def workspaces(self) -> AsyncWorkspaces:
        """GPU workspaces: create, start, connect, run jobs, move files, stop."""
        return AsyncWorkspaces(self)

    @property
    def compute(self) -> AsyncCompute:
        """Running instances and training, as the Compute page lists them."""
        return AsyncCompute(self)

    async def launch(self, gpu: str | None = None, *, gpu_count: int = 1, gpu_memory_gb: float | None = None,
                     disk_gb: int = 100, environment: str | None = None, ssh_key: str | None = None,
                     name: str | None = None, max_hours: int = 4, keep_files: bool = False, wait: bool = True,
                     timeout_seconds: float = 900.0, poll_seconds: float = 5.0,
                     idempotency_key: str | None = None) -> AsyncWorkspace:
        """Asynchronous counterpart of :meth:`Client.launch`."""
        from ._compute import launch_async
        return await launch_async(self, gpu, gpu_count=gpu_count, gpu_memory_gb=gpu_memory_gb, disk_gb=disk_gb,
                                  environment=environment, ssh_key=ssh_key, name=name, max_hours=max_hours,
                                  keep_files=keep_files, wait=wait, timeout_seconds=timeout_seconds,
                                  poll_seconds=poll_seconds, idempotency_key=idempotency_key)

    @property
    def volumes(self) -> AsyncVolumes:
        """Named storage volumes that sandboxes mount between sessions."""
        return AsyncVolumes(self)

    @property
    def agents(self):
        """Deploy managed agents and submit durable work asynchronously."""
        from ._managed_agents import AsyncManagedAgents
        return AsyncManagedAgents(self)

    @property
    def sandboxes(self) -> AsyncSandboxes:
        """Create and reconnect to durable agent execution environments."""
        return AsyncSandboxes(self)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        text: bool = False,
        raw: bool = False,
        content: bytes | None = None,
        headers_out: dict[str, str] | None = None,
        max_bytes: int | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> Any:
        headers = {"Idempotency-Key": _valid_idempotency_key(idempotency_key)} if idempotency_key else {}
        retries = self.max_retries if max_retries is None else max_retries
        request_options = {"timeout": timeout} if timeout is not None else {}
        attempt = 0
        # One waiting budget for the whole call, spent across every retry.
        slept = 0.0
        while True:
            delay: float | None = None
            capped: bytes | None = None
            try:
                if max_bytes is None:
                    resp = await self._http.request(
                        method, path, json=json, content=content, headers=headers, params=params, **request_options
                    )
                else:
                    async with self._http.stream(
                        method, path, json=json, content=content, headers=headers, params=params, **request_options
                    ) as resp:
                        if resp.status_code < 400:
                            chunks, total = [], 0
                            async for chunk in resp.aiter_bytes():
                                total += len(chunk)
                                if total > max_bytes:
                                    raise self._too_big(path, max_bytes)
                                chunks.append(chunk)
                            capped = b"".join(chunks)
                        else:
                            await resp.aread()
            except httpx.TimeoutException as exc:
                if attempt < retries:
                    delay = self._hold(attempt, None, slept)
                if delay is not None:
                    await asyncio.sleep(delay)
                    slept += delay
                    attempt += 1
                    continue
                raise self._unreached(
                    APITimeoutError, f"{method} {path} timed out", idempotency_key
                ) from exc
            except httpx.HTTPError as exc:
                if attempt < retries:
                    delay = self._hold(attempt, None, slept)
                if delay is not None:
                    await asyncio.sleep(delay)
                    slept += delay
                    attempt += 1
                    continue
                raise self._unreached(
                    APIConnectionError,
                    f"{method} {path} failed to connect: {exc}",
                    idempotency_key,
                ) from exc

            if 300 <= resp.status_code < 400:
                raise self._unreached(
                    APIError,
                    f"{method} {path} returned HTTP {resp.status_code}. "
                    "Redirects are refused. Configure the final API origin.",
                    idempotency_key,
                )

            if resp.status_code >= 400:
                if attempt < retries and self._should_retry(method, resp.status_code, attempt):
                    delay = self._hold(attempt, resp, slept)
                    if delay is not None:
                        await asyncio.sleep(delay)
                        slept += delay
                        attempt += 1
                        continue
                self._raise(method, path, resp)

            if headers_out is not None:
                headers_out.update(resp.headers)
            if capped is not None:
                return capped if raw else capped.decode("utf-8", "replace")
            if raw:
                return resp.content
            if not resp.content:
                return "" if text else None
            # The log endpoint answers text/plain, because its whole purpose is
            # to be read. Everything else is JSON.
            if text:
                return resp.text
            try:
                return resp.json()
            except ValueError:
                raise self._unreached(
                    APIError, f"{method} {path} returned invalid JSON", idempotency_key
                ) from None

    async def run(
        self,
        *,
        command: list[str] | str | None = None,
        image: str | None = None,
        source_asset_id: str | None = None,
        inputs: list[dict[str, Any]] | None = None,
        outputs: dict[str, str | OutputSpec] | None = None,
        model: str | None = None,
        peak_memory_gb: float | None = None,
        optimization: str | None = None,
        gpu: str | None = None,
        gpu_count: int | None = None,
        gpu_interconnect: str | None = None,
        budget: float | None = None,
        compute_class: ComputeClass | str | None = None,
        continuity: ContinuityMode | str | ContinuitySpec | dict[str, Any] | None = None,
        finish_by: datetime | str | None = None,
        data_regions: list[str] | None = None,
        connections: list[str] | None = None,
        sweep_id: str | None = None,
        stages: list[StageSpec] | list[dict[str, Any]] | None = None,
        framework: str | None = None,
        policy: Policy | dict[str, Any] | None = None,
        requirements: Requirements | dict[str, Any] | None = None,
        placement: Placement | dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        rl: "RLSetup | dict[str, Any] | None" = None,
        extra: dict[str, Any] | None = None,
        **unknown: Any,
    ) -> "AsyncWorkload":
        """Submit a brief. Same arguments and same meaning as :meth:`Client.run`."""
        payload = build_payload(
            command=command,
            image=image,
            source_asset_id=source_asset_id,
            inputs=inputs,
            outputs=outputs,
            model=model,
            peak_memory_gb=peak_memory_gb,
            optimization=optimization,
            gpu=gpu,
            gpu_count=gpu_count,
            gpu_interconnect=gpu_interconnect,
            budget=budget,
            compute_class=compute_class,
            continuity=continuity,
            finish_by=finish_by,
            data_regions=data_regions,
            connections=connections,
            sweep_id=sweep_id,
            stages=stages,
            framework=framework,
            policy=policy,
            requirements=requirements,
            placement=placement,
            rl=rl,
            extra=extra,
            **unknown,
        )
        answered: dict[str, str] = {}
        res = await self._request(
            "POST",
            "/v1/workloads",
            json=payload,
            idempotency_key=idempotency_key or f"nodus-{uuid.uuid4()}",
            headers_out=answered,
        )
        wl = AsyncWorkload(self)
        wl._absorb(res or {})
        wl.replayed = _was_replayed(answered)
        if not wl.id:
            raise NodusError("submit returned no workload id", body=res)
        return wl

    async def benchmark(self, *, workload: dict[str, Any], gpu_families: list[str],
                        batch_sizes: list[int], regions: list[str], repetitions: int,
                        budget: float | None = None, idempotency_key: str) -> dict[str, Any]:
        """Submit a hardware matrix."""
        from ._benchmarks import request_payload
        payload = request_payload(workload, gpu_families, batch_sizes, regions,
                                  repetitions, budget, idempotency_key)
        return self._one(await self._request("POST", "/v1/benchmarks", json=payload,
                         idempotency_key=idempotency_key), "POST", "/v1/benchmarks")

    async def get_benchmark(self, benchmark_id: str) -> dict[str, Any]:
        """Read cell statuses, measurements and workload links."""
        path = f"/v1/benchmarks/{_valid_id(benchmark_id)}"
        return self._one(await self._request("GET", path), "GET", path)

    @property
    def pools(self) -> AsyncPools:
        """Manage customer-owned pools and observe-only hosts asynchronously."""
        return AsyncPools(self)

    @property
    def assets(self) -> AsyncAssets:
        """Upload and import code or data for workloads."""
        return AsyncAssets(self)

    @property
    def rl(self) -> AsyncRL:
        """Discover, preview, and explicitly launch reviewed RL recipes."""
        return AsyncRL(self)

    async def run_file(self, path: str | Path = "nodus.toml") -> "AsyncWorkload":
        """Submit a validated workload file. Each call submits one workload."""
        from ._workload_file import load_workload_file

        return await self.run(**load_workload_file(path))

    async def get(self, workload_id: str) -> "AsyncWorkload":
        path = f"/v1/workloads/{_valid_id(workload_id)}"
        wl = AsyncWorkload(self)
        wl._absorb(self._one(await self._request("GET", path), "GET", path), authoritative=True)
        return wl

    async def list(
        self, *, limit: int = 50, offset: int = 0, status: Any = None, scope: str | None = None
    ) -> list["AsyncWorkload"]:
        """The first page only, use ``iter_workloads()`` for all of them.

        Newest first. ``status`` accepts a member, a wire string, a list of
        either, or the presets ``"active"`` and ``"terminal"``.
        """
        items, _ = await self.list_page(limit=limit, offset=offset, status=status, scope=scope)
        return items

    async def list_page(
        self, *, limit: int = 50, offset: int = 0, status: Any = None, scope: str | None = None
    ) -> tuple[list["AsyncWorkload"], int | None]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if scope is not None:
            if scope not in ("mine", "team"):
                raise ValueError("scope must be 'mine' or 'team'")
            params["scope"] = scope
        wire = status_filter(status)
        if wire:
            params["status"] = wire
        res = await self._request("GET", "/v1/workloads", params=params) or {}
        out = []
        for row in res.get("workloads") or []:
            wl = AsyncWorkload(self)
            wl._absorb(row)
            out.append(wl)
        return out, res.get("next_offset")

    async def iter_workloads(
        self, *, page_size: int = 50, status: Any = None, scope: str | None = None
    ) -> AsyncIterator["AsyncWorkload"]:
        """Page lazily so a long history never has to be held in memory.

        Stops when the next offset does not advance: an offset that repeats is
        a stall, and following it re-reads the same page forever.
        """
        offset = 0
        while True:
            page, nxt = await self.list_page(limit=page_size, offset=offset, status=status, scope=scope)
            for wl in page:
                yield wl
            if nxt is None or nxt <= offset:
                return
            offset = nxt

    async def freeze(self, workload_id: str) -> WorkloadFreeze:
        """Request saved-file freeze. Completion waits for checkpoint and exact cleanup."""
        result = WorkloadFreeze.from_dict(await self._request("POST", f"/v1/workloads/{_valid_id(workload_id)}/freeze", json={}), workload_id)
        if result.state not in {"requested", "releasing", "frozen"}:
            raise APIError("The API did not confirm workload freeze intent")
        return result

    async def freeze_status(self, workload_id: str) -> WorkloadFreeze:
        """Read retained checkpoint bytes without inferring a storage price."""
        return WorkloadFreeze.from_dict(await self._request("GET", f"/v1/workloads/{_valid_id(workload_id)}/freeze"), workload_id)

    async def resume(self, workload_id: str) -> WorkloadFreeze:
        """Resume frozen work with its saved files. The customer command must load them."""
        result = WorkloadFreeze.from_dict(await self._request("POST", f"/v1/workloads/{_valid_id(workload_id)}/resume", json={}), workload_id)
        if result.state not in {"resuming", "resumed"}:
            raise APIError("The API did not confirm workload resumption")
        return result

    async def cancel(self, workload_id: str, *, idempotency_key: str | None = None) -> None:
        await self._request(
            "POST",
            f"/v1/workloads/{_valid_id(workload_id)}/cancel",
            # Fresh per call: one key for the life of the workload makes every
            # later cancel a replay of the first, answered without being done.
            idempotency_key=idempotency_key or f"cancel-{workload_id}-{uuid.uuid4()}",
        )

    async def events(self, workload_id: str, *, after: int = 0) -> list[Event]:
        """One page of lifecycle events, oldest first.

        The server returns at most 100 and does not say whether more follow.
        Pass ``after=`` the last ``seq`` you saw, or use ``iter_events()``.
        """
        res = await self._request(
            "GET", f"/v1/workloads/{_valid_id(workload_id)}/events", params={"after": after}
        )
        return [Event.from_dict(e) for e in (res or {}).get("events") or []]

    async def iter_events(self, workload_id: str, *, after: int = 0) -> AsyncIterator[Event]:
        """Every event recorded so far, walking past the server's page cap.

        Stops on a sequence that does not advance. See :meth:`Client.iter_events`.
        """
        while True:
            batch = await self.events(workload_id, after=after)
            if not batch:
                return
            highest = after
            for ev in batch:
                highest = max(highest, ev.seq)
                yield ev
            if highest <= after:
                return
            after = highest

    async def artifacts(self, workload_id: str) -> list[Artifact]:
        res = await self._request("GET", f"/v1/workloads/{_valid_id(workload_id)}/artifacts")
        return [Artifact.from_dict(a) for a in (res or {}).get("artifacts") or []]

    async def outputs(self, workload_id: str) -> list[Output]:
        """List customer outputs from completed stages."""
        res = await self._request("GET", f"/v1/workloads/{_valid_id(workload_id)}/outputs")
        return [Output.from_dict(row) for row in (res or {}).get("outputs") or []]

    async def reload_output(self, workload_id: str, name: str, *, stage: str | None = None) -> dict[str, Any]:
        """Retry loading a saved output into its admitted database sink."""
        path = download_path(_valid_id(workload_id), name) + "/reload"
        return await self._request("POST", path, params={"stage": stage} if stage is not None else None)

    async def routing(self, workload_id: str) -> list[dict[str, Any]]:
        """Return placement history ordered by stage ID, then generation."""
        res = await self._request("GET", f"/v1/workloads/{_valid_id(workload_id)}/routing")
        return (res or {}).get("placements") or []

    async def download_output(
        self, workload_id: str, name: str, destination: str | os.PathLike[str],
        *, stage: str | None = None, overwrite: bool = True,
    ) -> Path:
        """Stream an output to destination and verify its SHA-256 before replacing it.

        Pass stage when multiple stages publish the same name. Failed transfers
        leave an existing destination intact. Retry the call to restart.
        """
        path = download_path(_valid_id(workload_id), name)
        params = {"stage": stage} if stage is not None else None
        try:
            async with self._http.stream("GET", path, params=params, follow_redirects=False,
                                       headers={"Accept-Encoding": "identity"}) as resp:
                if resp.status_code != 200:
                    body = bytearray()
                    async for chunk in resp.aiter_bytes(chunk_size=16 * 1024):
                        body.extend(chunk[:64 * 1024 - len(body)])
                        if len(body) >= 64 * 1024:
                            break
                    error = httpx.Response(resp.status_code, headers=resp.headers,
                                           content=bytes(body), request=resp.request)
                    self._raise("GET", path, error)
                with verified_file(destination, resp.headers, overwrite=overwrite) as write:
                    async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                        write(chunk)
        except httpx.TimeoutException as exc:
            raise APITimeoutError("Output download timed out. Retry to restart.") from exc
        except httpx.HTTPError as exc:
            raise APIConnectionError("Output download interrupted. Retry to restart.") from exc
        return Path(destination)

    async def ledger(self, workload_id: str) -> Ledger:
        return Ledger.from_dict(await self._request("GET", f"/v1/workloads/{_valid_id(workload_id)}/ledger"))

    async def logs(
        self, workload_id: str, *, stage: str | None = None, generation: int | None = None
    ) -> str:
        """What the submitted program printed. See :meth:`Client.logs`."""
        params: dict[str, Any] = {}
        if stage:
            params["stage"] = stage
        if generation is not None:
            params["generation"] = generation
        return await self._request(
            "GET",
            f"/v1/workloads/{_valid_id(workload_id)}/logs",
            params=params or None,
            text=True,
            max_bytes=_LOG_MAX_BYTES,
        ) or ""

    async def wait(
        self, workload_id: str, *, poll_seconds: float = 2.0,
        timeout_seconds: float | None = None, progress: bool | None = None,
        on_update: Callable[[Any], None] | None = None,
    ) -> "AsyncWorkload":
        """Wait with live output. Task cancellation requests remote cancellation."""
        _valid_id(workload_id)
        policy = _WaitPolicy(poll_seconds, timeout_seconds)
        try:
            with RunProgress(workload_id, progress) as display:
                while True:
                    try:
                        wl = await self.get(workload_id)
                    except NodusError as exc:
                        delay = policy.failed(exc)
                    else:
                        if on_update is not None:
                            on_update(wl)
                        display.update(wl)
                        for kind, path, params in display.requests():
                            if kind == "saved_logs" and display.live_available and not display.live_empty:
                                continue
                            while True:
                                try:
                                    timeout = 2.0 if policy.deadline is None else min(2.0, policy.deadline - time.monotonic())
                                    if timeout <= 0:
                                        break
                                    headers = {}
                                    body = await self._request(
                                        "GET", path, params=params, timeout=timeout, max_retries=0,
                                        text=kind == "saved_logs", headers_out=headers,
                                        max_bytes=_LOG_MAX_BYTES if kind == "saved_logs" else None,
                                    )
                                    if kind == "saved_logs":
                                        display.accept_saved_logs(body, headers.get("x-nodus-stage-id", ""), headers.get("x-nodus-generation", ""))
                                    else:
                                        advanced = display.accept(kind, body)
                                        if wl.is_terminal and advanced:
                                            params = {"after": display.event_after if kind == "events" else display.log_after}
                                            continue
                                except NodusError as exc:
                                    display.unavailable(kind, exc.status_code)
                                except ValueError:
                                    display.unavailable(kind, None)
                                break
                        if wl.is_terminal:
                            return wl
                        delay = policy.polled()
                    await asyncio.sleep(policy.hold(delay, workload_id))
        except (asyncio.CancelledError, KeyboardInterrupt) as interrupt:
            try:
                await asyncio.shield(self.cancel(workload_id))
            except (NodusError, asyncio.CancelledError):
                if hasattr(interrupt, "add_note"):
                    interrupt.add_note(f"Cancellation not confirmed for {workload_id!r}. Call client.cancel(workload_id) against the same deployment.")
            raise

    async def live_logs(self, workload_id: str, *, after: str = "") -> dict[str, Any]:
        """Read new live output chunks using the previous next_cursor."""
        path = f"/v1/workloads/{_valid_id(workload_id)}/logs/live"
        return self._one(await self._request("GET", path, params={"after": after}), "GET", path)

    async def stream_events(
        self, workload_id: str, *, poll_seconds: float = 2.0
    ) -> AsyncIterator[Event]:
        """Yield events until the terminal one. Same policy as :meth:`Client.stream_events`."""
        after = 0
        policy = _WaitPolicy(poll_seconds)
        while True:
            try:
                batch = await self.events(workload_id, after=after)
                if not batch and (await self.get(workload_id)).is_terminal:
                    return
            except NodusError as exc:
                await asyncio.sleep(policy.failed(exc))
                continue
            for ev in batch:
                after = max(after, ev.seq)
                yield ev
            await asyncio.sleep(policy.polled())

    # -- webhooks / health -------------------------------------------------

    async def set_webhook(self, url: str, *, secret: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"url": url}
        if secret:
            body["secret"] = secret
        return await self._request("PUT", "/v1/webhooks", json=body) or {}

    async def get_webhook(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/webhooks") or {}

    async def delete_webhook(self) -> None:
        await self._request("DELETE", "/v1/webhooks")

    async def healthz(self) -> dict[str, Any]:
        return await self._request("GET", "/healthz") or {}

    async def readyz(self) -> dict[str, Any]:
        return await self._request("GET", "/readyz") or {}


class AsyncWorkload(_WorkloadState):
    """Async handle. Identical attributes to :class:`Workload`. Methods await."""

    def __init__(self, client: AsyncClient):
        super().__init__()
        self._client = client

    async def refresh(self) -> "AsyncWorkload":
        path = f"/v1/workloads/{_valid_id(self.id)}"
        self._absorb(self._client._one(await self._client._request("GET", path), "GET", path), authoritative=True)
        return self

    async def wait(self, *, poll_seconds: float = 2.0, timeout_seconds: float | None = None,
                   progress: bool | None = None, on_update: Callable[[Any], None] | None = None) -> "AsyncWorkload":
        """Wait in place with the same behavior as AsyncClient.wait."""
        done = await self._client.wait(self.id, poll_seconds=poll_seconds,
                                       timeout_seconds=timeout_seconds, progress=progress, on_update=on_update)
        self._absorb(done.raw, authoritative=True)
        return self

    async def events(self, *, after: int = 0) -> list[Event]:
        return await self._client.events(self.id, after=after)

    def iter_events(self, *, after: int = 0) -> AsyncIterator[Event]:
        return self._client.iter_events(self.id, after=after)

    def stream_events(self, *, poll_seconds: float = 2.0) -> AsyncIterator[Event]:
        return self._client.stream_events(self.id, poll_seconds=poll_seconds)

    async def artifacts(self) -> list[Artifact]:
        return await self._client.artifacts(self.id)

    async def outputs(self) -> list[Output]:
        """List customer outputs from completed stages."""
        return await self._client.outputs(self.id)

    async def reload_output(self, name: str, *, stage: str | None = None) -> dict[str, Any]:
        """Retry loading one saved output into its database sink."""
        return await self._client.reload_output(self.id, name, stage=stage)

    async def routing(self) -> list[dict[str, Any]]:
        """Read this workload's placement history."""
        return await self._client.routing(self.id)

    async def download(self, destination: str | os.PathLike[str] | None = None) -> list[Path]:
        """Download collected outputs into stage folders, refusing existing files."""
        root = Path(destination) if destination is not None else Path("outputs") / _valid_id(self.id)
        planned = output_destinations(root, await self.outputs())
        saved = []
        for output, path in planned:
            path.parent.mkdir(parents=True, exist_ok=True)
            saved.append(await self.download_output(output.name, path, stage=output.stage_id, overwrite=False))
        return saved

    async def download_output(
        self, name: str, destination: str | os.PathLike[str], *, stage: str | None = None, overwrite: bool = True
    ) -> Path:
        """Download a named output and verify it before replacing destination."""
        return await self._client.download_output(self.id, name, destination, stage=stage, overwrite=overwrite)

    async def ledger(self) -> Ledger:
        return await self._client.ledger(self.id)

    async def logs(self, *, stage: str | None = None, generation: int | None = None) -> str:
        """This workload's log. See :meth:`Client.logs`."""
        return await self._client.logs(self.id, stage=stage, generation=generation)

    async def freeze(self) -> WorkloadFreeze:
        return await self._client.freeze(self.id)

    async def freeze_status(self) -> WorkloadFreeze:
        return await self._client.freeze_status(self.id)

    async def resume(self) -> WorkloadFreeze:
        return await self._client.resume(self.id)

    async def cancel(self) -> None:
        await self._client.cancel(self.id)


from . import _agent as agent
from ._steps import step, step_context
from .errors import StepOutcomeUnknown, StepDefinitionConflict, StepResultExpired, StepFailed
from .errors import AgentChildrenUnavailable
from ._agent_children import ChildReference, ChildOutcome, ChildCompletions, ChildCancellation
__all__.extend(['AgentChildrenUnavailable', 'ChildReference', 'ChildOutcome', 'ChildCompletions', 'ChildCancellation'])

from .errors import AgentMessagesUnavailable
from ._agent_messages import MessageReceipt, PeerMessage
__all__.extend(['AgentMessagesUnavailable', 'MessageReceipt', 'PeerMessage'])

from ._agent_runs import AgentRun
__all__.append("AgentRun")

from .errors import AgentBrokerUnavailable, BrokerRefused
__all__.extend(['AgentBrokerUnavailable', 'BrokerRefused'])
