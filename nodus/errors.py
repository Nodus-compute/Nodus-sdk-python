"""Typed exceptions for the Nodus API.

Every exception inherits :class:`NodusError`, so one ``except nodus.NodusError``
catches all of them and nothing else. The distinction that matters when writing
a handler is whether the condition can clear on its own: rate limits and
capacity pressure can clear with time, credit exhaustion requires account funding, and authentication and validation failures never clear.
"""

from __future__ import annotations

import re
from typing import Any

# Every C0 and C1 control. The server's own wording lands inside an error the
# CLI prints, and a newline there forges whole lines of output that read
# exactly like the tool's own -- on unauthenticated endpoints, before any
# credential exists. The SDK's own remedy and request-id lines are added after
# this and may still span lines.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

__all__ = [
    "NodusError",
    "WorkspaceNotReadyError",
    "StepOutcomeUnknown", "StepDefinitionConflict", "StepResultExpired", "StepFailed",
    "AgentChildrenUnavailable", "AgentBrokerUnavailable", "AgentMessagesUnavailable", "BrokerRefused",
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
    "error_from_response",
    "asset_id_from_error",
]


def asset_id_from_error(error: BaseException) -> str | None:
    """Recover an admitted query asset ID, including wrapped cancellation errors.

    Some asyncio versions replace CancelledError at a task boundary and keep
    the original exception in its context chain. Cancellation is not suppressed.
    """
    pending = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        asset_id = getattr(current, "asset_id", None)
        if isinstance(asset_id, str) and re.fullmatch(r"asset_[A-Za-z0-9-]{1,64}", asset_id):
            return asset_id
        for nested in (current.__context__, current.__cause__):
            if nested is not None:
                pending.append(nested)
    return None


class NodusError(Exception):
    """Base class for everything the SDK raises."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: Any = None,
        request_id: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body
        self.request_id = request_id
        self.asset_id: str | None = None

    @property
    def code(self) -> str | None:
        """The machine-readable error code the control plane returned, if any."""
        if isinstance(self.body, dict):
            value = self.body.get("error") or self.body.get("code")
            if isinstance(value, str):
                return value
        return None

    @property
    def payload(self) -> dict[str, Any]:
        """Structured detail attached to the error. Empty when there is none."""
        if isinstance(self.body, dict):
            return self.body
        return {}


class ConfigurationError(NodusError):
    """Raised before any network call: no API key, malformed base URL.

    Nothing was sent, so nothing was charged or created.
    """


class AuthenticationError(NodusError):
    """401/403. The key is missing, unknown, revoked, or expired.

    Never retry, a rejected credential does not become valid on its own.
    """


class NotFoundError(NodusError):
    """404. No such workload for this tenant.

    Identifiers are tenant-scoped, so another tenant's id reads as absent
    rather than forbidden.
    """


class ValidationError(NodusError):
    """400/422. The brief was rejected. Retrying resends the same brief."""


class WorkspaceNotReadyError(NodusError):
    """The workspace cannot serve this request in its current state.

    Compute is still starting, still saving, already stopped, or has no SSH
    key. Read the message for the state and what changes it.
    """


class IdempotencyConflictError(NodusError):
    """409. The same Idempotency-Key was reused with a different payload.

    Not retryable: the key names one submission, and a second payload claiming
    it would destroy that identity. Resend the original payload, or mint a new
    key for the new intent.
    """


class RunDraftConflictError(NodusError):
    """409. Read the latest run draft before applying a reviewed replacement edit."""


class RateLimitError(NodusError):
    """429. Too many requests."""

    @property
    def retry_after(self) -> float | None:
        """Seconds to wait, as the server asked, when it sent a Retry-After.

        Unclamped: this is what the control plane said, not what the SDK's own
        backoff chose to do with it.
        """
        return self._retry_after

    @property
    def retry_after_header(self) -> str | None:
        """The header verbatim, seconds or an HTTP-date, as it arrived."""
        return self._retry_after_header

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        retry_after_header: str | None = None,
        **kw: Any,
    ):
        super().__init__(message, **kw)
        self._retry_after = retry_after
        self._retry_after_header = retry_after_header


class BudgetExceededError(NodusError):
    """402. The brief would breach a spend cap on the key.

    The refusal carries the whole arithmetic it was made from, so a caller can
    resubmit against real headroom rather than against the ceiling it asked for.
    """

    def _amount(self, *keys: str) -> float | None:
        for key in keys:
            value = self.payload.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return None

    @property
    def monthly_cap_usd(self) -> float | None:
        """The cap on the key for this billing period."""
        return self._amount("monthly_spend_cap_usd")

    @property
    def month_to_date_usd(self) -> float | None:
        """Settled spend so far this period."""
        return self._amount("month_to_date_usd")

    @property
    def estimated_cost_usd(self) -> float | None:
        """Cost to completion the router priced this brief at."""
        return self._amount("estimated_cost_usd")

    @property
    def accruing_usd(self) -> float | None:
        """Money open leases are running up that no charge row exists for yet."""
        return self._amount("accruing_usd")

    @property
    def in_flight_committed_usd(self) -> float | None:
        """What already-admitted work is still committed to spend."""
        return self._amount("in_flight_committed_usd")

    @property
    def headroom_usd(self) -> float | None:
        """What is left under the cap, as the control plane measured it.

        Never derived: headroom nets settled, accruing and committed money, so
        cap minus month-to-date overstates it. ``None`` means the refusal did
        not carry it.
        """
        return self._amount("remaining_headroom_usd")


class SpendCheckUnavailableError(NodusError):
    """503 on submit. The account's spend could not be measured, so nothing ran.

    Admission fails closed: nothing was created, nothing was charged, and the
    same brief is retryable exactly as sent.
    """


class CapacityUnavailableError(NodusError):
    """Reserved. No control plane path raises this today.

    Kept because removing an exported name breaks ``except`` clauses somebody
    wrote. A brief no route fits is refused at submit as a validation problem.
    a 503 is :class:`SpendCheckUnavailableError`.
    """


class SignatureError(NodusError):
    """401 on a signed request. Stale timestamp, altered body, or wrong secret.

    The code is ``invalid_signature``, which webhook delivery sends.
    """


class APIError(NodusError):
    """Any other 4xx/5xx with no more specific class."""


class AssetInUseError(APIError):
    """An asset cannot be deleted while an active workload uses it."""


class APIConnectionError(NodusError):
    """The request never reached the control plane."""


class APITimeoutError(NodusError):
    """A client-side deadline elapsed.

    The per-request ``timeout``, or ``timeout_seconds`` on ``wait()``. For
    ``wait()`` the workload is unaffected and keeps running, a client deadline
    is not a cancellation.
    """


# What to do about each rejection the control plane names, in the SDK's own
# vocabulary. A message that says only what failed leaves the reader where they
# started, and these are the codes internal/api actually writes.
_REMEDIES: dict[str, str] = {
    "missing_source": (
        "Give the workload something to run: image= and command=, or framework=, "
        "or stages=[...]."
    ),
    "invalid_compute_class": 'compute_class must be "vm" or "accelerator".',
    "invalid_continuity_mode": (
        'continuity must be "checkpointed", "restartable", or "ephemeral".'
    ),
    "invalid_complete_by": (
        'finish_by must be a datetime, or RFC3339 text such as "2026-01-02T15:04:05Z".'
    ),
    "payment_method_required": (
        "Add a payment method in Billing before running workloads, including "
        "runs using starter credits: https://console.nodus-compute.ai/?view=billing"
    ),
    "budget_exceeded": (
        "Check account funding and payment readiness in Billing."
    ),
    "spend_check_unavailable": (
        "The account spend check could not be reached, so the submission was "
        "refused rather than admitted without one: nothing was created and "
        "nothing was charged. Retry in a moment with the same brief."
    ),
    "asset_in_use": "Wait for dependent workloads to finish before deleting this asset.",
    "idempotency_conflict": (
        "That Idempotency-Key already names a different payload. Resend the "
        "original brief, or mint a new key for the new intent."
    ),
}


# Status codes whose meaning is specific enough to deserve their own class.
_BY_STATUS: dict[int, type[NodusError]] = {
    400: ValidationError,
    401: AuthenticationError,
    402: BudgetExceededError,
    403: AuthenticationError,
    404: NotFoundError,
    409: IdempotencyConflictError,
    422: ValidationError,
    429: RateLimitError,
}


def error_from_response(
    method: str,
    path: str,
    status_code: int,
    body: Any,
    *,
    retry_after: float | None = None,
    retry_after_header: str | None = None,
    request_id: str | None = None,
) -> NodusError:
    """Map an HTTP response onto the most specific exception class.

    The message carries three things: what failed, what to do about it, and
    which request it was, the last is what support can correlate on.
    """
    message = f"{method} {path} failed ({status_code})"
    if isinstance(body, dict):
        detail = body.get("message") or body.get("error")
        if isinstance(detail, str) and detail:
            message = f"{message}: {_CONTROL.sub('', detail)}"

    code = (body.get("error") or body.get("code")) if isinstance(body, dict) else None
    supplied_fix = body.get("fix") if isinstance(body, dict) else None
    remedy = supplied_fix if isinstance(supplied_fix, str) and supplied_fix else (
        _REMEDIES.get(code) if isinstance(code, str) else None
    )
    if remedy:
        message = f"{message}\n{remedy}"
    if request_id:
        # h11 passes every control byte but NUL, CR, LF, VT and FF through a
        # header value, so this strip is the only defence for the message's
        # copy. The attribute keeps the value verbatim for correlation.
        message = f"{message}\nrequest id: {_CONTROL.sub('', request_id)}"

    # A signed-request rejection is a 401 like a bad key, but it means something
    # different to the caller: the credential is fine, the signature is not.
    # Only the code the API actually writes counts.
    if status_code == 401 and code == "invalid_signature":
        cls: type[NodusError] = SignatureError
    elif status_code == 409 and isinstance(code, str) and code in {
        "workspace_files_conflict", "workspace_files_format_unsupported",
        "workspace_files_empty", "workspace_storage_inactive", "workspace_transfer_conflict",
    }:
        cls = APIError
    elif status_code == 409 and isinstance(code, str) and code in {
        "workspace_not_ready", "workspace_starting", "workspace_saving", "workspace_ssh_key_required",
    }:
        cls = WorkspaceNotReadyError
    elif status_code == 409 and code == "asset_in_use":
        cls = AssetInUseError
    elif status_code == 409 and code == "run_draft_conflict":
        cls = RunDraftConflictError
    elif status_code == 503 and code == "spend_check_unavailable":
        # The only 503 the control plane sends. Anything else at this status is
        # an infrastructure answer, not a statement about this brief.
        cls = SpendCheckUnavailableError
    else:
        cls = _BY_STATUS.get(status_code, APIError)

    kwargs: dict[str, Any] = {
        "status_code": status_code,
        "body": body,
        "request_id": request_id,
    }
    if cls is RateLimitError:
        return RateLimitError(
            message, retry_after=retry_after, retry_after_header=retry_after_header, **kwargs
        )
    return cls(message, **kwargs)


class StepOutcomeUnknown(NodusError):
    """The journal cannot establish a safe result for an external operation."""


class StepDefinitionConflict(NodusError):
    """An existing run or step identity has a different definition."""


class StepResultExpired(NodusError):
    """A completed result is no longer retained and cannot be re-executed."""


class StepFailed(NodusError):
    """The journal retains a known failure for this step."""


class AgentChildrenUnavailable(NodusError):
    """The assigned runtime or account has not enabled durable child operations."""


class AgentMessagesUnavailable(NodusError):
    """The assigned group or runtime has not enabled durable peer messages."""


class AgentBrokerUnavailable(NodusError):
    """The assigned runtime or account has not enabled the requested broker."""


class BrokerRefused(NodusError):
    """A definitive refusal before an external effect, with retry guidance."""

    def __init__(self, code: str, *, retryable: bool):
        super().__init__('Broker invocation was refused: ' + code, body={'code': code})
        self.retryable = retryable
