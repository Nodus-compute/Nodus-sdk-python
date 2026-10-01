"""Predict responses and explicit subscription consent for customer-owned pools."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
import re
from typing import Any

from .errors import APIError, ValidationError

REFRESH_STATES = {"disabled", "awaiting_refresh", "active", "stale", "paused_credits", "paused_cap", "paused_payment", "consent_required"}


def _record(value: Any, strings: tuple[str, ...] = ()) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key] for key in strings):
        raise APIError("The API returned an invalid Predict response")
    return value


def _integer(value: Any, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise APIError("The API returned an invalid Predict count or amount")


def _number(value: Any, *, nullable: bool = False, maximum: float | None = None) -> None:
    if nullable and value is None:
        return
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0 and (maximum is None or value <= maximum)
    except OverflowError:
        valid = False
    if not valid:
        raise APIError("The API returned an invalid Predict measurement")


def _time(value: Any, *, hour: bool = False) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)", value):
        raise APIError("The API returned an invalid Predict timestamp")
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        if hour and (at.minute or at.second or at.microsecond or re.search(r"\.\d*[1-9]", value)):
            raise ValueError
        return at
    except (ValueError, OverflowError):
        raise APIError("The API returned an invalid Predict timestamp") from None


@dataclass(frozen=True)
class PredictSubscription:
    """Server-reported Predict terms, including free customer-owned pools."""

    status: str
    rate_version: str
    monthly_micros: int
    period_start: str
    period_end: str
    paid_current_period: bool
    enabled_pool_count: int

    @classmethod
    def from_dict(cls, value: Any) -> PredictSubscription:
        row = _record(value, ("status", "rate_version", "period_start", "period_end"))
        for key in ("monthly_micros", "enabled_pool_count"):
            _integer(row.get(key))
        if row["status"] not in REFRESH_STATES or type(row.get("paid_current_period")) is not bool:
            raise APIError("The API returned an invalid Predict subscription")
        free = row["rate_version"] == "byoc-free-v1" and row["monthly_micros"] == 0
        if row["status"] == "active" and not row["paid_current_period"] and not free:
            raise APIError("The API returned an unpaid active Predict subscription")
        if _time(row["period_start"]) >= _time(row["period_end"]):
            raise APIError("The API returned an invalid Predict billing period")
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ForecastPoint:
    """One hourly p10, p50, and p90 demand band in device-hours."""

    hour: str
    p10: float
    p50: float
    p90: float
    queue_device_hours: float | None = None

    @classmethod
    def from_dict(cls, value: Any) -> ForecastPoint:
        row = _record(value, ("hour",))
        _time(row["hour"], hour=True)
        for key in ("p10", "p50", "p90"):
            _number(row.get(key))
        if not row["p10"] <= row["p50"] <= row["p90"]:
            raise APIError("The API returned an unordered forecast band")
        queue_hours = row.get("queue_device_hours")
        _number(queue_hours, nullable=True)
        if queue_hours is not None and queue_hours > row["p10"]:
            raise APIError("The API returned contradictory queued demand")
        return cls(row["hour"], row["p10"], row["p50"], row["p90"], queue_hours)


@dataclass(frozen=True)
class ForecastQueue:
    """Known queued demand under an immediate-start scenario, with unknown jobs explicit."""

    method: str
    observed_at: str
    known_jobs: int
    unknown_jobs: int
    known_device_hours: float
    added_device_hours: float
    outside_horizon_device_hours: float

    @classmethod
    def from_dict(cls, value: Any) -> ForecastQueue:
        row = _record(value, ("method", "observed_at"))
        if row["method"] != "retained_queue_immediate_start_v1":
            raise APIError("The API returned an unknown queue forecast method")
        _time(row["observed_at"])
        for name in ("known_jobs", "unknown_jobs"):
            _integer(row.get(name))
        for name in ("known_device_hours", "added_device_hours", "outside_horizon_device_hours"):
            _number(row.get(name))
        if not math.isclose(row["known_device_hours"], row["added_device_hours"] + row["outside_horizon_device_hours"], rel_tol=1e-9, abs_tol=1e-9):
            raise APIError("The API returned contradictory queue totals")
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ForecastSeries:
    """The server's forecast model, history coverage, and hourly bands."""

    model: str
    status: str
    as_of: str
    horizon_days: int
    history_from: str
    history_hours: int
    required_history_hours: int
    points: list[ForecastPoint]
    queue: ForecastQueue | None = None

    @classmethod
    def from_dict(cls, value: Any) -> ForecastSeries:
        row = _record(value, ("model", "status", "as_of", "history_from"))
        for key in ("horizon_days", "history_hours", "required_history_hours"):
            _integer(row.get(key))
        start = _time(row["as_of"], hour=True)
        if _time(row["history_from"], hour=True) >= start or row["horizon_days"] not in (7, 30) or row["status"] not in ("ready", "insufficient_history") or not isinstance(row.get("points"), list):
            raise APIError("The API returned an invalid forecast window")
        points = [ForecastPoint.from_dict(point) for point in row["points"]]
        expected = row["horizon_days"] * 24 if row["status"] == "ready" else 0
        if len(points) != expected or (row["status"] == "ready" and row["history_hours"] < row["required_history_hours"]):
            raise APIError("The API returned incomplete forecast evidence")
        if any(_time(point.hour, hour=True) - start != timedelta(hours=i) for i, point in enumerate(points)):
            raise APIError("The API returned noncontiguous forecast hours")
        queue = None if row.get("queue") is None else ForecastQueue.from_dict(row["queue"])
        if queue is not None:
            observed = _time(queue.observed_at)
            if not start <= observed < start + timedelta(hours=1) or any(p.queue_device_hours is None for p in points):
                raise APIError("The API returned incomplete queue forecast evidence")
            if not math.isclose(sum(p.queue_device_hours for p in points), queue.added_device_hours, rel_tol=1e-9, abs_tol=1e-9):
                raise APIError("The API returned contradictory queue contributions")
        return cls(**{key: row[key] for key in cls.__dataclass_fields__ if key not in ("points", "queue")}, points=points, queue=queue)


@dataclass(frozen=True)
class ForecastCalibration:
    """Evaluation of issued forecasts, with absent calibration preserved as None."""

    from_: str
    to: str
    status: str
    expected_hours: int
    forecast_hours: int
    evaluated_hours: int
    covered_hours: int
    hourly_coverage: float | None
    pinball_p10: float | None
    pinball_p50: float | None
    pinball_p90: float | None
    complete_days: int
    covered_days: int
    daily_coverage: float | None

    @classmethod
    def from_dict(cls, value: Any) -> ForecastCalibration:
        row = _record(value, ("from", "to", "status"))
        if _time(row["from"], hour=True) >= _time(row["to"], hour=True) or row["status"] not in ("no_data", "partial", "complete"):
            raise APIError("The API returned an invalid calibration window")
        for key in ("expected_hours", "forecast_hours", "evaluated_hours", "covered_hours", "complete_days", "covered_days"):
            _integer(row.get(key))
        for key in ("hourly_coverage", "daily_coverage", "pinball_p10", "pinball_p50", "pinball_p90"):
            if key not in row:
                raise APIError("The API omitted calibration availability")
            _number(row[key], nullable=True, maximum=1 if key.endswith("coverage") else None)
        if not 0 <= row["covered_hours"] <= row["evaluated_hours"] <= row["forecast_hours"] <= row["expected_hours"] or row["covered_days"] > row["complete_days"]:
            raise APIError("The API returned contradictory calibration counts")
        expected = (_time(row["to"], hour=True) - _time(row["from"], hour=True)).total_seconds() / 3600
        if row["expected_hours"] != expected or row["complete_days"] * 24 > row["evaluated_hours"] or (row["status"] == "complete") != (row["evaluated_hours"] == expected):
            raise APIError("The API returned contradictory calibration coverage")
        for count, total, fraction in (("covered_hours", "evaluated_hours", "hourly_coverage"), ("covered_days", "complete_days", "daily_coverage")):
            if row[total] and (row[fraction] is None or not math.isclose(row[fraction], row[count] / row[total], rel_tol=1e-12, abs_tol=1e-12)):
                raise APIError("The API returned contradictory calibration ratios")
        no_data = row["evaluated_hours"] == 0
        if (row["status"] == "no_data") != no_data or any((row[key] is None) != no_data for key in ("hourly_coverage", "pinball_p10", "pinball_p50", "pinball_p90")) or (row["daily_coverage"] is None) != (row["complete_days"] == 0):
            raise APIError("The API returned contradictory calibration availability")
        return cls(from_=row["from"], **{key: row[key] for key in cls.__dataclass_fields__ if key != "from_"})


@dataclass(frozen=True)
class PoolForecastSnapshot:
    """A cached forecast, owned capacity, and optional advisory market rate."""

    pool_id: str
    generated_at: str
    forecast: ForecastSeries
    calibration: ForecastCalibration
    owned_devices: int
    advisory_burst_price: dict[str, Any] | None

    @classmethod
    def from_dict(cls, value: Any) -> PoolForecastSnapshot:
        row = _record(value, ("pool_id", "generated_at"))
        _time(row["generated_at"])
        _integer(row.get("owned_devices"))
        if "advisory_burst_price" not in row:
            raise APIError("The API omitted advisory price availability")
        price = row["advisory_burst_price"]
        if price is not None:
            price = _record(price, ("observed_at", "basis"))
            _time(price["observed_at"])
            _integer(price.get("rate_micros_per_device_hour"))
            price = dict(price)
        return cls(row["pool_id"], row["generated_at"], ForecastSeries.from_dict(row.get("forecast")),
                   ForecastCalibration.from_dict(row.get("calibration")), row["owned_devices"], price)


def _envelope(value: Any) -> dict[str, Any]:
    row = _record(value, ("pool_id", "refresh_status"))
    if type(row.get("predict_enabled")) is not bool or row["refresh_status"] not in REFRESH_STATES:
        raise APIError("The API returned an invalid Predict refresh state")
    return row


@dataclass(frozen=True)
class PoolForecast:
    """Refresh state and cached forecast independent of subscription availability."""

    pool_id: str
    predict_enabled: bool
    refresh_status: str
    subscription: PredictSubscription
    snapshot: PoolForecastSnapshot | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, value: Any) -> PoolForecast:
        row = _envelope(value)
        if "snapshot" not in row:
            raise APIError("The API omitted forecast availability")
        snapshot = None if row["snapshot"] is None else PoolForecastSnapshot.from_dict(row["snapshot"])
        if snapshot is not None and snapshot.pool_id != row["pool_id"]:
            raise APIError("The API returned a forecast for a different pool")
        return cls(row["pool_id"], row["predict_enabled"], row["refresh_status"], PredictSubscription.from_dict(row.get("subscription")), snapshot, dict(row))


@dataclass(frozen=True)
class RecommendationOutcome:
    """A customer's reported action and optional reported saving, not a measured result."""

    recorded_at: str
    outcome: str
    reported_saving_micros: int | None

    @classmethod
    def from_dict(cls, value: Any) -> RecommendationOutcome:
        row = _record(value, ("recorded_at", "outcome"))
        _time(row["recorded_at"])
        if "reported_saving_micros" not in row:
            raise APIError("The API omitted reported saving availability")
        _integer(row["reported_saving_micros"], nullable=True)
        return cls(row["recorded_at"], row["outcome"], row["reported_saving_micros"])


def _rightsize(rec: dict[str, Any], evidence: dict[str, Any]) -> None:
    for key in ("recommended_devices", "expected_savings_micros"):
        _integer(rec.get(key))
    for key in ("history_hours", "horizon_hours", "scenarios_per_hour", "current_devices", "released_devices", "owned_micros_per_device_hour", "burst_micros_per_device_hour", "expected_burst_micros", "released_owned_micros"):
        _integer(evidence.get(key))
    _number(evidence.get("critical_fractile"), maximum=1)
    _number(evidence.get("expected_burst_device_hours"))
    for prefix in ("history", "horizon"):
        duration = _time(evidence.get(prefix + "_to"), hour=True) - _time(evidence.get(prefix + "_from"), hour=True)
        if duration.total_seconds() != evidence[prefix + "_hours"] * 3600 or duration <= timedelta(0):
            raise APIError("The API returned inconsistent recommendation history")
    if evidence.get("advisory_price") is not True or evidence.get("method") != "seasonal_empirical_inverse_cdf" or evidence["scenarios_per_hour"] != 4:
        raise APIError("The API returned unsupported price evidence")
    if evidence["current_devices"] - rec["recommended_devices"] != evidence["released_devices"] or evidence["released_owned_micros"] != evidence["released_devices"] * evidence["horizon_hours"] * evidence["owned_micros_per_device_hour"] or evidence["released_owned_micros"] - evidence["expected_burst_micros"] != rec["expected_savings_micros"]:
        raise APIError("The API returned inconsistent estimated savings")
    distribution = evidence.get("demand_distribution")
    if not isinstance(distribution, list) or not distribution:
        raise APIError("The API omitted demand evidence")
    total = 0
    for item in distribution:
        mass = _record(item)
        _number(mass.get("device_hours"))
        _integer(mass.get("scenario_count"))
        total += mass["scenario_count"]
    if total != evidence["horizon_hours"] * evidence["scenarios_per_hour"]:
        raise APIError("The API returned inconsistent demand evidence")


def _idle(rec: dict[str, Any], evidence: dict[str, Any]) -> None:
    _record(rec, ("host_id", "device_id", "expires_at"))
    _integer(rec.get("device_index"))
    if rec.get("advisory_only") is not True or "expected_savings_micros" not in rec or rec["expected_savings_micros"] is not None:
        raise APIError("The API returned an unsupported idle saving or action")
    for key in ("sm_util_below_pct", "consecutive_seconds", "historical_observed_hours"):
        _integer(evidence.get(key))
    start, end = _time(evidence.get("trigger_from")), _time(evidence.get("trigger_to"))
    observed = _time(evidence.get("observed_through"))
    history = _time(evidence.get("history_to"), hour=True) - _time(evidence.get("history_from"), hour=True)
    if evidence.get("method") != "sampled_allocated_sm_below_5" or evidence["sm_util_below_pct"] != 5 or type(evidence.get("foreign")) is not bool or evidence["historical_observed_hours"] != 168 or history != timedelta(hours=168):
        raise APIError("The API returned incomplete idle observation evidence")
    if (end - start).total_seconds() != evidence["consecutive_seconds"] or evidence["consecutive_seconds"] < 1800 or end > observed or _time(rec["expires_at"]) - observed != timedelta(minutes=3):
        raise APIError("The API returned inconsistent idle trigger evidence")


def _drain(rec: dict[str, Any], evidence: dict[str, Any]) -> None:
    _record(rec, ("host_id", "expires_at"))
    if rec.get("advisory_only") is not True or "expected_savings_micros" not in rec or rec["expected_savings_micros"] is not None:
        raise APIError("The API returned an unsupported drain saving or action")
    for key in ("consecutive_hours", "history_hours", "threshold_devices"):
        _integer(evidence.get(key))
    _number(evidence.get("max_p90_device_hours"))
    start = _time(evidence.get("window_from"), hour=True)
    end = _time(evidence.get("window_to"), hour=True)
    history_end = _time(evidence.get("history_to"), hour=True)
    history_start = _time(evidence.get("history_from"), hour=True)
    if evidence.get("method") != "host_seasonal_weekly_p90_below_one" or evidence.get("model") != "seasonal_weekly_v1" or evidence["history_hours"] != 672 or history_end - history_start != timedelta(hours=672):
        raise APIError("The API returned incomplete drain history")
    if evidence["threshold_devices"] != 1 or evidence["max_p90_device_hours"] >= 1 or evidence["consecutive_hours"] < 3 or (end - start).total_seconds() != evidence["consecutive_hours"] * 3600 or not history_end <= start < end or end - history_end > timedelta(days=7) or _time(rec["expires_at"]) > end:
        raise APIError("The API returned inconsistent drain window evidence")


def _defragment(rec: dict[str, Any], evidence: dict[str, Any]) -> None:
    if rec.get("advisory_only") is not True or rec.get("risk") != "high" or "expected_savings_micros" not in rec or rec["expected_savings_micros"] is not None:
        raise APIError("The API returned an unsupported placement saving or action")
    _integer(evidence.get("observed_hours"))
    _number(evidence.get("fragmentation_wait_seconds"))
    _number(evidence.get("threshold_seconds"))
    end = _time(evidence.get("history_to"), hour=True)
    start = _time(evidence.get("history_from"), hour=True)
    if evidence.get("method") != "route_fragmentation_intervals_v1" or evidence.get("packing_policy") != "fill_hosts_first" or evidence.get("foreign_jobs_movable") is not False:
        raise APIError("The API returned unsupported placement evidence")
    if evidence["observed_hours"] != 336 or end - start != timedelta(hours=336) or not 0 < evidence["threshold_seconds"] < evidence["fragmentation_wait_seconds"]:
        raise APIError("The API returned incomplete placement observations")
    if not end < _time(rec.get("expires_at")) <= end + timedelta(hours=24):
        raise APIError("The API returned inconsistent placement expiration")


def _wait_tuning(rec: dict[str, Any], evidence: dict[str, Any]) -> None:
    if rec.get("advisory_only") is not True or "expected_savings_micros" not in rec or rec["expected_savings_micros"] is not None:
        raise APIError("The API returned unsupported wait tuning savings")
    for key in ("observed_hours", "completed_executions", "wait_seconds", "runtime_seconds", "burst_spent_micros", "policy_version"):
        _integer(evidence.get(key))
    for key in ("observed_delay_pct", "waiting_budget_pct", "current_alpha", "proposed_alpha"):
        _number(evidence.get(key))
    start, end = _time(evidence.get("history_from"), hour=True), _time(evidence.get("history_to"), hour=True)
    current, proposed = evidence["current_alpha"], evidence["proposed_alpha"]
    if evidence.get("method") != "bounded_proportional_wait_v1" or evidence["observed_hours"] != 336 or end - start != timedelta(hours=336) or evidence["completed_executions"] <= 0 or evidence["runtime_seconds"] <= 0 or evidence["policy_version"] <= 0 or evidence["waiting_budget_pct"] > 100:
        raise APIError("The API returned incomplete wait tuning evidence")
    if current <= 0 or not current * .75 - 1e-12 <= proposed <= current * 1.25 + 1e-12 or current == proposed or _time(rec.get("expires_at")) != end + timedelta(hours=24):
        raise APIError("The API returned unbounded wait tuning advice")
    digest = evidence.get("source_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise APIError("The API returned invalid wait tuning provenance")


@dataclass(frozen=True)
class PoolRecommendation:
    """Advisory evidence kept separate from any customer-reported outcome."""

    id: str
    pool_id: str
    kind: str
    generated_at: str
    expires_at: str
    state: str
    recommendation: dict[str, Any]
    outcome: RecommendationOutcome | None

    @classmethod
    def from_dict(cls, value: Any) -> PoolRecommendation:
        row = _record(value, ("id", "pool_id", "kind", "generated_at", "expires_at", "state"))
        if _time(row["generated_at"]) >= _time(row["expires_at"]) or row["state"] not in ("open", "done", "expired"):
            raise APIError("The API returned an invalid recommendation state")
        recommendation = _record(row.get("recommendation"), ("kind", "level", "risk"))
        if recommendation["kind"] != row["kind"] or recommendation["level"] != "recommend" or recommendation["risk"] not in ("low", "medium", "high"):
            raise APIError("The API returned a non-advisory recommendation")
        evidence = _record(recommendation.get("evidence"))
        if row["kind"] == "rightsize":
            _rightsize(recommendation, evidence)
        elif row["kind"] == "idle_reclaim":
            _idle(recommendation, evidence)
        elif row["kind"] == "drain_window":
            _drain(recommendation, evidence)
        elif row["kind"] == "wait_tuning":
            _wait_tuning(recommendation, evidence)
        elif row["kind"] == "defragment":
            _defragment(recommendation, evidence)
        else:
            raise APIError("The API returned an unsupported recommendation kind")
        if row["kind"] in ("idle_reclaim", "drain_window", "defragment", "wait_tuning") and _time(recommendation["expires_at"]) != _time(row["expires_at"]):
            raise APIError("The API returned inconsistent recommendation expiration")
        if "outcome" not in row:
            raise APIError("The API omitted recommendation outcome availability")
        outcome = None if row["outcome"] is None else RecommendationOutcome.from_dict(row["outcome"])
        if (row["state"] == "done") != (outcome is not None):
            raise APIError("The API returned contradictory recommendation completion")
        return cls(**{key: row[key] for key in ("id", "pool_id", "kind", "generated_at", "expires_at", "state")}, recommendation=dict(recommendation), outcome=outcome)


@dataclass(frozen=True)
class PoolRecommendations:
    """A pool's available advice and current refresh state."""

    pool_id: str
    predict_enabled: bool
    refresh_status: str
    recommendations: list[PoolRecommendation]
    next_cursor: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, value: Any) -> PoolRecommendations:
        row = _envelope(value)
        if not isinstance(row.get("recommendations"), list):
            raise APIError("The API returned an invalid recommendation list")
        recommendations = [PoolRecommendation.from_dict(item) for item in row["recommendations"]]
        if any(item.pool_id != row["pool_id"] for item in recommendations):
            raise APIError("The API returned advice for a different pool")
        cursor = row.get("next_cursor")
        if cursor is not None and (not isinstance(cursor, str) or not cursor.strip() or len(cursor) > 2048):
            raise APIError("The API returned an invalid recommendation cursor")
        return cls(row["pool_id"], row["predict_enabled"], row["refresh_status"], recommendations, cursor, dict(row))


def predict_patch(enabled: bool, rate: str | None, amount: int | None) -> dict[str, Any]:
    if type(enabled) is not bool or (not enabled and (rate is not None or amount is not None)):
        raise ValidationError("Use a boolean Predict setting and consent only when enabling")
    payload: dict[str, Any] = {"predict_enabled": enabled}
    if enabled:
        if not isinstance(rate, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", rate) or type(amount) is not int or not 0 <= amount <= 2**63 - 1:
            raise ValidationError("Enabling Predict requires an explicitly accepted rate version and monthly USD micros")
        payload.update(accepted_predict_rate_version=rate, accepted_predict_monthly_micros=amount)
    return payload


def done_payload(outcome: str, saving: int | None) -> dict[str, Any]:
    if not isinstance(outcome, str) or not outcome.strip() or len(outcome.encode("utf-8")) > 2000:
        raise ValidationError("Describe the manual outcome in at most 2000 UTF-8 bytes")
    payload: dict[str, Any] = {"outcome": outcome}
    if saving is not None:
        if type(saving) is not int or not 0 <= saving <= 2**63 - 1:
            raise ValidationError("Reported saving must be nonnegative integer USD micros")
        payload["reported_saving_micros"] = saving
    return payload


def horizon_query(horizon: int | None) -> dict[str, int]:
    if horizon is not None and (type(horizon) is not int or horizon not in (7, 30)):
        raise ValidationError("Forecast horizon must be 7 or 30 days")
    return {} if horizon is None else {"horizon": horizon}


def recommendation_query(limit: int | None, cursor: str | None, state: str | None) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if limit is not None:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValidationError("Recommendation page limit must be an integer from 1 to 100")
        query["limit"] = limit
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor.strip() or len(cursor) > 2048:
            raise ValidationError("Use a nonempty recommendation cursor returned by the API")
        query["cursor"] = cursor
    if state is not None:
        if state not in ("open", "done", "expired"):
            raise ValidationError("Recommendation state must be open, done, or expired")
        query["state"] = state
    return query
