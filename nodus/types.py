"""Enums and result objects for the Nodus API.

Nothing here names a supplier. The customer surface reports a Nodus catalog
route and the capability class behind it. Who ran the work, and on whose
hardware, is a Nodus decision and is not part of this contract.
"""

from __future__ import annotations

import math

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

__all__ = [
    "ComputeClass",
    "ContinuityMode",
    "InterruptTolerance",
    "WorkloadStatus",
    "TERMINAL",
    "TERMINAL_STATUSES",
    "MEDIA_TAR",
    "Route",
    "StageRun",
    "UnitMetrics",
    "ManifestFile",
    "Artifact",
    "Event",
    "LedgerEntry",
    "Settlement",
    "Ledger",
    "Meter",
    "CUSTOMER_CHARGE",
]


class _WireEnum(str, Enum):
    """A string enum that survives an unknown value from a newer control plane.

    Wire vocabularies are open: the control plane may introduce a status before
    this SDK knows about it. Coercing an unknown value to a plain string keeps
    the client working instead of raising on a field it only meant to display.
    """

    @classmethod
    def coerce(cls, value: Any) -> Any:
        if value is None or isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError:
            return str(value)


class ComputeClass(_WireEnum):
    """The kind of infrastructure a route provides."""

    ACCELERATOR = "accelerator"
    VM = "vm"


class ContinuityMode(_WireEnum):
    """What should happen to progress if capacity is reclaimed mid-run."""

    #: Progress is checkpointed and a replacement resumes from the last
    #: committed manifest.
    CHECKPOINTED = "checkpointed"
    #: The work is safe to start over from the beginning.
    RESTARTABLE = "restartable"
    #: Losing the run is acceptable; do not pay for durability.
    EPHEMERAL = "ephemeral"


class InterruptTolerance(_WireEnum):
    """How much interruption risk an execution envelope carries.

    Read-only: the control plane derives it from ``continuity`` and models no
    field a caller could set, so ``run()`` does not accept it.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class WorkloadStatus(_WireEnum):
    """Lifecycle, in order."""

    def __str__(self) -> str:
        return self.value

    ACCEPTED = "accepted"
    PLANNING = "planning"
    RESERVING = "reserving"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    RECOVERING = "recovering"
    FREEZING = "freezing"
    FROZEN = "frozen"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: The states at which a workload stops changing and ``wait()`` returns.
TERMINAL_STATUSES = frozenset(
    {WorkloadStatus.COMPLETED, WorkloadStatus.FAILED, WorkloadStatus.CANCELLED}
)

#: Wire values of the terminal states, for comparing against raw strings.
TERMINAL = frozenset(s.value for s in TERMINAL_STATUSES)


def _num(value: Any, default: float = 0.0) -> float:
    """A float from a field nothing here controls.

    A raw ``float()`` on wire data can raise mid-poll with an error no failure
    policy catches. NaN and infinity pass numeric checks yet compare false
    against every budget, so non-finite is refused along with unparseable.
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _int(value: Any, default: int = 0) -> int:
    """A count from the same untrusted place. See :func:`_num`."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(_num(value, float(default)))
    except (TypeError, ValueError, OverflowError):
        return default


def _text(value: Any) -> str:
    """Text from a field nothing here controls, or "" when it is not text.

    A digest or a name that arrives as a number or a list is not that field:
    "" reads as absent rather than rendering the wrong type's repr, and
    slicing it cannot raise mid-listing. See :func:`_num`.
    """
    return value if isinstance(value, str) else ""


def _obj(value: Any) -> dict[str, Any]:
    """A mapping, or an empty one: a free-form field is not always an object."""
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list[Any]:
    """A list, or an empty one: iterating a string yields its characters."""
    return value if isinstance(value, list) else []


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class Route:
    """The Nodus catalog route chosen for a workload.

    Cost and runtime estimates are ``None`` when the API has no trustworthy
    total. Estimates do not guarantee completion within a spending limit.
    """

    sku: str = ""
    compute_class: Any = None
    fit_class: str = ""
    region: str = ""
    memory_gb: float = 0.0
    price_usd_hour: float = 0.0
    expected_cost_usd: float | None = None
    expected_hours: float | None = None
    remaining_budget_usd: float = 0.0
    interruptible: bool = False
    resources: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Route | None":
        if not isinstance(d, dict) or not d:
            return None
        return cls(
            sku=_text(d.get("offer_id")) or _text(d.get("sku")),
            compute_class=ComputeClass.coerce(d.get("compute_class")),
            fit_class=_text(d.get("fit_class")),
            region=_text(d.get("region")),
            memory_gb=_num(d.get("memory_gb")),
            price_usd_hour=_num(d.get("price_usd_hour")),
            expected_cost_usd=_num(d.get("expected_cost_usd"), None),
            expected_hours=_num(d.get("expected_hours"), None),
            remaining_budget_usd=_num(d.get("remaining_budget_usd")),
            interruptible=bool(d.get("interruptible")),
            resources=_obj(d.get("resources")),
            raw=d,
        )


@dataclass
class StageRun:
    """Progress for one stage of a multi-stage workload."""

    id: str = ""
    status: Any = None
    continuity_mode: Any = None
    completed_units: int = 0
    total_units: int = 0
    latest_manifest: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    last_loss: float | None = None
    metric_rate: float | None = None
    metric_step: int | None = None
    metric_total_steps: int | None = None
    metric_epoch: float | None = None
    metric_total_epochs: float | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StageRun":
        d = _obj(d)
        return cls(
            id=_text(d.get("id")),
            status=WorkloadStatus.coerce(d.get("status")),
            continuity_mode=ContinuityMode.coerce(d.get("continuity_mode")),
            completed_units=_int(d.get("completed_units")),
            total_units=_int(d.get("total_units")),
            latest_manifest=d.get("latest_manifest"),
            raw=d,
            last_loss=_num(d.get("last_loss"), None),
            metric_rate=_num(d.get("metric_rate"), None),
            metric_step=(int(d["metric_step"]) if isinstance(d.get("metric_step"), int)
                         and not isinstance(d["metric_step"], bool) else None),
            metric_total_steps=(d["metric_total_steps"] if isinstance(d.get("metric_total_steps"), int)
                                and not isinstance(d["metric_total_steps"], bool) else None),
            metric_epoch=_num(d.get("metric_epoch"), None),
            metric_total_epochs=_num(d.get("metric_total_epochs"), None),
        )


@dataclass
class Output:
    """A downloadable customer output from a completed stage."""

    name: str = ""
    stage_id: str = ""
    sha256: str = ""
    bytes: int = 0
    download: str = ""
    sink_state: str = ""
    sink_rows: int | None = None
    sink_error: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Output":
        d = _obj(d)
        return cls(name=_text(d.get("name")), stage_id=_text(d.get("stage_id")),
                   sha256=_text(d.get("sha256")), bytes=_int(d.get("bytes")),
                   download=_text(d.get("download")), sink_state=_text(d.get("sink_state")),
                   sink_rows=_int(d["sink_rows"]) if d.get("sink_rows") is not None else None,
                   sink_error=_text(d.get("sink_error")), raw=d)


#: An object holding a tar of a checkpoint subtree rather than a single file.
MEDIA_TAR = "application/x-tar"


@dataclass
class ManifestFile:
    """One object named by a committed manifest.

    ``uri`` is a key in Nodus-held storage, never a supplier URL: checkpoints
    outlive the capacity that produced them, which is the whole point of them
    being manifests. ``sha256`` is the digest the control plane recomputed
    before it agreed to write the manifest.
    """

    uri: str = ""
    sha256: str = ""
    bytes: int = 0
    #: Empty means one raw file, restored by copying it back.
    media: str = ""

    @property
    def is_tar(self) -> bool:
        """True when this object must be extracted rather than copied."""
        return self.media == MEDIA_TAR

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ManifestFile":
        d = _obj(d)
        return cls(
            uri=_text(d.get("uri")),
            sha256=_text(d.get("sha256")),
            bytes=_int(d.get("bytes")),
            media=_text(d.get("media")),
        )


@dataclass
class Artifact:
    """One committed manifest: a checkpoint, or a stage's final outputs.

    This is the row shape ``GET /v1/workloads/{id}/artifacts`` actually returns.
    The endpoint lists *manifests*, not files, a manifest names many objects,
    which is why the digest and the bytes live on :class:`ManifestFile` under
    :attr:`files` and :attr:`outputs` rather than on the artifact itself.

    There is deliberately no ``verified`` flag. A manifest is written only after
    the control plane recomputes the SHA-256 of every object it names, so a row
    appearing here means those digests matched at commit time. But the response
    carries no per-row verification state, and an SDK that reported one would be
    asserting a check it never saw. Compare :attr:`ManifestFile.sha256` against
    bytes you have fetched if you need verification you performed yourself.

    :attr:`final` marks the manifest that completed the stage. Intermediate
    checkpoints exist to be restored from, not to be consumed downstream.
    """

    manifest_id: str = ""
    stage_id: str = ""
    generation: int = 0
    sequence: int = 0
    final: bool = False
    created_at: datetime | None = None
    #: Checkpoint objects, in the order the manifest names them.
    files: list[ManifestFile] = field(default_factory=list)
    #: Declared stage outputs by name — what a downstream stage reads.
    outputs: dict[str, ManifestFile] = field(default_factory=dict)
    #: The manifest body, for fields this SDK version does not model yet.
    manifest: dict[str, Any] = field(default_factory=dict, repr=False)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # An output is a file a manifest names, not a manifest, so the outputs and
    # logs endpoints are a separate type; neither is modelled here yet.

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Artifact":
        # stage_id, generation and sequence appear both on the row and inside
        # the manifest. Prefer the row: it is what the store indexed on, so it is
        # what a filter or a sort on this list would have used.
        d = _obj(d)
        man = _obj(d.get("manifest"))
        return cls(
            manifest_id=_text(d.get("manifest_id")),
            stage_id=_text(d.get("stage_id")) or _text(man.get("stage_id")),
            generation=_int(d.get("generation") or man.get("generation")),
            sequence=_int(d.get("sequence") or man.get("sequence")),
            final=bool(man.get("final")),
            created_at=_dt(d.get("created_at")),
            files=[ManifestFile.from_dict(f) for f in _rows(man.get("files"))],
            outputs={
                name: ManifestFile.from_dict(f)
                for name, f in _obj(man.get("outputs")).items()
            },
            manifest=man,
            raw=d,
        )


@dataclass
class Event:
    """One lifecycle event."""

    seq: int = 0
    id: str = ""
    type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        d = _obj(d)
        return cls(
            seq=_int(d.get("id")),
            id=_text(d.get("event_id")),
            type=_text(d.get("event_type")) or _text(d.get("type")),
            payload=_obj(d.get("payload")),
            created_at=_dt(d.get("created_at")),
            raw=d,
        )


@dataclass
class LedgerEntry:
    id: str = ""
    entry_type: str = ""
    debit_usd: float = 0.0
    credit_usd: float = 0.0
    currency: str = "USD"
    evidence: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LedgerEntry":
        d = _obj(d)
        return cls(
            id=_text(d.get("id")),
            entry_type=_text(d.get("entry_type")),
            debit_usd=_num(d.get("debit_usd")),
            credit_usd=_num(d.get("credit_usd")),
            currency=_text(d.get("currency")) or "USD",
            evidence=_obj(d.get("evidence")),
            created_at=_dt(d.get("created_at")),
        )


#: The entry type that moves a customer's money. Everything else on a ledger is
#: bookkeeping between Nodus and the run.
CUSTOMER_CHARGE = "customer_charge"


@dataclass
class Settlement:
    """Whether the books for one workload are closed, and what is left on them.

    ``balance_usd`` is a residual, not a price: a settlement that closed
    cleanly balances at $0.00. What the run cost is :attr:`Ledger.charged_usd`.
    """

    status: str = "none"
    balance_usd: float = 0.0
    correlation_id: str = ""
    closed_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Settlement":
        d = _obj(d)
        return cls(
            status=_text(d.get("status")) or "none",
            balance_usd=_num(d.get("balance_usd")),
            correlation_id=_text(d.get("correlation_id")),
            closed_at=_dt(d.get("closed_at")),
            raw=d,
        )


@dataclass
class Ledger:
    """Billing evidence for one workload.

    Entries are scrubbed of supply-plane detail before they reach this surface.
    """

    entries: list[LedgerEntry] = field(default_factory=list)
    settlement: Settlement = field(default_factory=Settlement)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def charged_usd(self) -> float:
        """Gross compute and platform fee credits charged for this workload.

        This matches ``spend_usd``. Refund debits and supplier accounting
        remain separate from this posted charge total.
        """
        return sum(
            e.credit_usd for e in self.entries
            if e.entry_type in (CUSTOMER_CHARGE, "platform_fee")
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Ledger":
        d = _obj(d)
        return cls(
            entries=[LedgerEntry.from_dict(e) for e in _rows(d.get("entries"))],
            settlement=Settlement.from_dict(d.get("settlement")),
            raw=d,
        )


@dataclass
class Meter:
    """Server-reported month-scoped costs and optional component readings.

    ``final_charge_usd`` is the fixed lifetime compute total once ``charge_state``
    is final. It excludes retained-file usage and is absent while costs are pending.
    """

    settled_usd: float = 0.0
    accruing_usd: float = 0.0
    accruing_rate_usd_hour: float = 0.0
    total_now_usd: float = 0.0
    as_of: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    compute_settled_usd: float = 0.0
    platform_fee_settled_usd: float = 0.0
    subscription_settled_usd: float = 0.0
    compute_accruing_usd: float = 0.0
    platform_fee_accruing_usd: float = 0.0
    final_charge_usd: float | None = None
    charge_state: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Meter | None":
        if not isinstance(d, dict) or not d:
            return None
        return cls(
            compute_settled_usd=_num(d.get("compute_settled_usd")),
            platform_fee_settled_usd=_num(d.get("platform_fee_settled_usd")),
            subscription_settled_usd=_num(d.get("subscription_settled_usd")),
            compute_accruing_usd=_num(d.get("compute_accruing_usd")),
            platform_fee_accruing_usd=_num(d.get("platform_fee_accruing_usd")),
            settled_usd=_num(d.get("settled_usd")),
            accruing_usd=_num(d.get("accruing_usd")),
            accruing_rate_usd_hour=_num(d.get("accruing_rate_usd_hour")),
            total_now_usd=_num(d.get("total_now_usd")),
            as_of=_dt(d.get("as_of")),
            raw=d,
            final_charge_usd=_num(d.get("final_charge_usd"), None),
            charge_state=_text(d.get("charge_state")),
        )


@dataclass
class UnitMetrics:
    """Server-observed unit latency and posted cost per completed unit."""

    units_completed: int | None = None
    p50_ms: float | None = None
    p95_ms: float | None = None
    cost_per_unit_usd: float | None = None
    dropped_observations: int | None = None

    @classmethod
    def from_dict(cls, value: Any) -> "UnitMetrics | None":
        if not isinstance(value, dict) or not value:
            return None
        def number(name: str) -> float | None:
            item = value.get(name)
            if type(item) not in (int, float):
                return None
            try:
                parsed = float(item)
            except OverflowError:
                return None
            return parsed if math.isfinite(parsed) and parsed >= 0 else None
        def count(name: str) -> int | None:
            item = value.get(name)
            return item if type(item) is int and item >= 0 else None
        return cls(count("units_completed"), number("p50_ms"), number("p95_ms"),
                   number("cost_per_unit_usd"), count("dropped_observations"))


@dataclass(frozen=True)
class WorkloadLink:
    """A validated live run link captured from workload output."""

    kind: str
    url: str

    @classmethod
    def from_dict(cls, value: Any) -> "WorkloadLink | None":
        import re
        value = _obj(value)
        url = value.get("url")
        token = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
        if value.get("kind") != "wandb" or not isinstance(url, str) or not re.fullmatch(r"https://wandb\.ai/" + token + "/" + token + "/runs/" + token, url):
            return None
        return cls(kind="wandb", url=url)
