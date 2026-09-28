"""Report RL progress to Nodus from inside a running workload.

A workload reports one scored attempt per line on standard output, which the
control plane parses into the live task feed and the before-and-after
comparison. This module is that wire format, so a caller neither copies it nor
reimplements it:

    from nodus import RLEventEmitter

    events = RLEventEmitter(os.environ["NODUS_WORKLOAD_ID"])
    events.task_started("evaluation", task_id)
    events.task_completed("evaluation", task_id, outcome="passed", reward=1.0)

Every field is validated before anything is written. An event the control
plane would reject raises here instead, on the caller's machine, rather than
after a machine has been rented.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import threading
import unicodedata
from decimal import Decimal
from dataclasses import dataclass
from typing import Any, Callable, Literal, TextIO


PREFIX = "nodus.rl_event "
MAX_EVENT_BYTES = 16 * 1024
MAX_ATTEMPT = 1_000_000
PHASES = frozenset(("baseline", "training", "evaluation"))
OUTCOMES = frozenset(("passed", "failed", "error"))
GO_JSON_ESCAPES = str.maketrans(
    {
        "<": "\\u003c",
        ">": "\\u003e",
        "&": "\\u0026",
        "\u2028": "\\u2028",
        "\u2029": "\\u2029",
    }
)


class EventValidationError(ValueError):
    """Raised before output when an event violates the Nodus wire contract."""


@dataclass(frozen=True)
class RawTraceFields:
    """Trace text that a caller explicitly chooses to send to Nodus."""

    input: str | None = None
    output: str | None = None
    verifier: str | None = None


@dataclass(frozen=True)
class EpisodeScore:
    """A customer's explicit correctness result for one successful episode."""

    outcome: Literal["passed", "failed"]
    reward: float | None = None
    message: str | None = None


class RLEventEmitter:
    """Write validated newline-delimited events for one caller trial."""

    def __init__(
        self,
        trial_namespace: str,
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self._trial_namespace = _identity_text(
            "trial_namespace", trial_namespace, limit=256
        )
        self._stdout = stdout if stdout is not None else sys.stdout
        self._stderr = stderr if stderr is not None else sys.stderr
        self._lock = threading.Lock()

    def task_started(
        self,
        phase: str,
        task_id: str,
        attempt: int = 1,
        *,
        message: str | None = None,
        raw_trace: RawTraceFields | None = None,
    ) -> dict[str, Any]:
        """Emit the start of one task attempt."""
        event = self._event(
            phase=phase,
            task_id=task_id,
            attempt=attempt,
            kind="task_started",
            message=message,
            raw_trace=raw_trace,
        )
        self._emit(event)
        return event

    def task_completed(
        self,
        phase: str,
        task_id: str,
        attempt: int = 1,
        *,
        outcome: Literal["passed", "failed", "error"],
        reward: float | None = None,
        duration_ms: float | None = None,
        message: str | None = None,
        raw_trace: RawTraceFields | None = None,
    ) -> dict[str, Any]:
        """Emit the terminal result of one task attempt."""
        event = self._event(
            phase=phase,
            task_id=task_id,
            attempt=attempt,
            kind="task_completed",
            outcome=outcome,
            reward=reward,
            duration_ms=duration_ms,
            message=message,
            raw_trace=raw_trace,
        )
        self._emit(event)
        return event

    def task_exception(
        self,
        phase: str,
        task_id: str,
        attempt: int = 1,
        *,
        exception: BaseException,
        duration_ms: float | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        """Report an error without copying exception text or a traceback."""
        event = self._event(
            phase=phase,
            task_id=task_id,
            attempt=attempt,
            kind="task_completed",
            outcome="error",
            duration_ms=duration_ms,
            message=message,
        )
        # The runner scans stderr for events too. An exception name is a plain
        # identifier almost always; anything else is quoted so it stays on this
        # one line and cannot begin a forged event.
        name = type(exception).__name__
        if not name.isidentifier():
            name = repr(name)
        diagnostic = f"RL task {event['task_id']} raised {name}\n"
        line = _encode_line(event)
        with self._lock:
            self._stderr.write(diagnostic)
            self._stderr.flush()
            self._stdout.write(line)
            self._stdout.flush()
        return event

    def prime_episode_completed(
        self,
        episode: Any,
        *,
        phase: str,
        score_episode: Callable[[Any], EpisodeScore],
        attempt: int = 1,
        raw_trace: RawTraceFields | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        """Emit a current Prime Verifiers Episode as one completed task."""
        if not callable(score_episode):
            raise EventValidationError("score_episode must be callable")
        task = getattr(episode, "task", None)
        task_id = getattr(task, "key", None) or getattr(task, "hash", None)
        if not task_id:
            raise EventValidationError(
                "Prime Episode requires a stable task.key or task.hash"
            )
        duration_ms = _episode_duration_ms(episode)
        if getattr(episode, "ok", False) is not True:
            return self.task_completed(
                phase,
                task_id,
                attempt,
                outcome="error",
                duration_ms=duration_ms,
                message=error_message,
                raw_trace=raw_trace,
            )
        score = score_episode(episode)
        if not isinstance(score, EpisodeScore):
            raise EventValidationError(
                "score_episode must return EpisodeScore with an explicit outcome"
            )
        return self.task_completed(
            phase,
            task_id,
            attempt,
            outcome=score.outcome,
            reward=score.reward,
            duration_ms=duration_ms,
            message=score.message,
            raw_trace=raw_trace,
        )

    def _event(
        self,
        *,
        phase: str,
        task_id: str,
        attempt: int,
        kind: Literal["task_started", "task_completed"],
        outcome: str | None = None,
        reward: float | None = None,
        duration_ms: float | None = None,
        message: str | None = None,
        raw_trace: RawTraceFields | None = None,
    ) -> dict[str, Any]:
        phase = _phase(phase)
        task_id = _identity_text("task_id", task_id, limit=128)
        attempt = _attempt(attempt)
        if kind == "task_started" and any(
            value is not None for value in (outcome, reward, duration_ms)
        ):
            raise EventValidationError(
                "task_started cannot include outcome, reward, or duration_ms"
            )
        if kind == "task_completed" and outcome not in OUTCOMES:
            raise EventValidationError(
                "outcome must be passed, failed, or error for task_completed"
            )
        event: dict[str, Any] = {
            "event_id": _event_id(
                self._trial_namespace, task_id, attempt, phase, kind
            ),
            "phase": phase,
            "task_id": task_id,
            "attempt": attempt,
            "kind": kind,
        }
        if outcome is not None:
            event["outcome"] = outcome
        if reward is not None:
            event["reward"] = _finite_number("reward", reward)
        if duration_ms is not None:
            duration = _finite_number("duration_ms", duration_ms)
            if duration < 0:
                raise EventValidationError("duration_ms must be at least zero")
            event["duration_ms"] = duration
        if message is not None:
            event["message"] = _payload_text("message", message, limit=1024)
        if raw_trace is not None:
            if not isinstance(raw_trace, RawTraceFields):
                raise EventValidationError("raw_trace must be RawTraceFields")
            for field in ("input", "output", "verifier"):
                value = getattr(raw_trace, field)
                if value is not None:
                    event[field] = _payload_text(field, value, limit=4096)
        _encode_line(event)
        return event

    def _emit(self, event: dict[str, Any]) -> None:
        line = _encode_line(event)
        with self._lock:
            self._stdout.write(line)
            self._stdout.flush()


def _event_id(
    trial_namespace: str,
    task_id: str,
    attempt: int,
    phase: str,
    kind: str,
) -> str:
    fields = (trial_namespace, task_id, str(attempt), phase, kind)
    identity = "".join(f"{len(field.encode('utf-8'))}:{field}" for field in fields)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"nre_{digest[:32]}"


def _phase(value: Any) -> str:
    if not isinstance(value, str) or value not in PHASES:
        raise EventValidationError("phase must be baseline, training, or evaluation")
    return value


def _attempt(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EventValidationError("attempt must be an integer")
    if value < 1 or value > MAX_ATTEMPT:
        raise EventValidationError(f"attempt must be between 1 and {MAX_ATTEMPT}")
    return value


def _identity_text(field: str, value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        raise EventValidationError(f"{field} must be text")
    if not value or value.strip() != value:
        raise EventValidationError(f"{field} must be nonempty without edge whitespace")
    try:
        encoded_value = value.encode("utf-8")
    except UnicodeEncodeError:
        raise EventValidationError(f"{field} must be valid UTF-8 text") from None
    if len(encoded_value) > limit:
        raise EventValidationError(f"{field} exceeds {limit} UTF-8 bytes")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise EventValidationError(f"{field} contains a control character")
    return value


def _payload_text(field: str, value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        raise EventValidationError(f"{field} must be text")
    if not value:
        raise EventValidationError(f"{field} must not be empty when supplied")
    try:
        encoded_value = value.encode("utf-8")
    except UnicodeEncodeError:
        raise EventValidationError(f"{field} must be valid UTF-8 text") from None
    if len(encoded_value) > limit:
        raise EventValidationError(f"{field} exceeds {limit} UTF-8 bytes")
    if any(
        unicodedata.category(char) == "Cc" and char not in "\n\r\t"
        for char in value
    ):
        raise EventValidationError(f"{field} contains an unsupported control character")
    return value


def _finite_number(field: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventValidationError(f"{field} must be a finite number")
    try:
        number = float(value)
    except OverflowError:
        raise EventValidationError(f"{field} must be a finite number") from None
    if not math.isfinite(number):
        raise EventValidationError(f"{field} must be a finite number")
    return number


def _go_number_growth(value: float) -> int:
    """How many bytes longer the runner's re-encoding of a number is.

    The runner decodes each event and encodes it again the way Go does, which
    writes plain digits for magnitudes from 1e-6 up to 1e21. Python switches to
    an exponent at 1e16, so 1e16 grows from five bytes to seventeen. Measuring
    the Python text alone would accept an event the runner then drops.
    """
    text = json.dumps(value)
    if ("e" in text or "E" in text) and (value == 0 or 1e-6 <= abs(value) < 1e21):
        return max(0, len(format(Decimal(repr(value)), "f")) - len(text))
    return 0


def _encode_line(event: dict[str, Any]) -> str:
    encoded_text = json.dumps(
        event,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).translate(GO_JSON_ESCAPES)
    encoded = encoded_text.encode("utf-8")
    growth = sum(_go_number_growth(value) for value in event.values() if isinstance(value, float))
    if len(encoded) + growth > MAX_EVENT_BYTES:
        raise EventValidationError(
            f"event JSON exceeds the {MAX_EVENT_BYTES} byte limit"
        )
    return PREFIX + encoded_text + "\n"


def _episode_duration_ms(episode: Any) -> float | None:
    starts: list[float] = []
    ends: list[float] = []
    for trace in getattr(episode, "traces", ()):
        timing = getattr(trace, "timing", None)
        if timing is None:
            continue
        start = getattr(timing, "start", None)
        if _is_finite_number(start):
            starts.append(float(start))
        for phase in ("boot", "setup", "agent", "finalize", "scoring"):
            end = getattr(getattr(timing, phase, None), "end", None)
            if _is_finite_number(end) and float(end) > 0:
                ends.append(float(end))
    if not starts or not ends:
        return None
    return max(0.0, (max(ends) - min(starts)) * 1000.0)


def _is_finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )
