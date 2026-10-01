"""Validate explicit Route consent and future placement settings."""
from __future__ import annotations

import math
from typing import Any
from .errors import APIError, ValidationError


def route_settings(*, wait_policy: str | None = None, wait_alpha: float | None = None,
                   waiting_budget_pct: float | None = None, burst_approval: str | None = None,
                   burst_threshold_micros: int | None = None,
                   burst_timeout_behaviour: str | None = None) -> dict[str, Any]:
    values = dict(wait_policy=wait_policy, wait_alpha=wait_alpha, waiting_budget_pct=waiting_budget_pct,
                  burst_approval=burst_approval, burst_threshold_micros=burst_threshold_micros,
                  burst_timeout_behaviour=burst_timeout_behaviour)
    for key, choices in (("wait_policy", ("never", "after_wait", "cheaper")),
                         ("burst_approval", ("auto", "above_threshold", "always")),
                         ("burst_timeout_behaviour", ("keep_waiting", "cancel"))):
        if values[key] is not None and values[key] not in choices:
            raise ValidationError(f"{key} must be one of {', '.join(choices)}")
    for key in ("wait_alpha", "waiting_budget_pct"):
        value = values[key]
        if value is not None:
            try:
                valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
            except OverflowError:
                valid = False
            if not valid or key == "waiting_budget_pct" and value > 100:
                raise ValidationError(f"{key} must be a finite nonnegative number" + (" up to 100" if key == "waiting_budget_pct" else ""))
    if burst_threshold_micros is not None and (type(burst_threshold_micros) is not int or not 0 <= burst_threshold_micros <= 2**63 - 1):
        raise ValidationError("burst_threshold_micros must be a nonnegative signed 64-bit integer")
    return {key: value for key, value in values.items() if value is not None}


def route_patch(enabled: bool, version: str | None, rate: int | None, settings: dict[str, Any]) -> dict[str, Any]:
    if type(enabled) is not bool:
        raise ValidationError("enabled must be a boolean")
    if enabled:
        if type(rate) is not int or (version, rate) not in (("route-platform-v1", 20000), ("byoc-free-v1", 0), ("private-supplier-cost-v2", 0)):
            raise ValidationError("Accept the pool's Route terms explicitly: route-platform-v1 at 20000 USD micros per device-hour, byoc-free-v1 at zero for a cloud-connected pool, or private-supplier-cost-v2 at zero for customer-owned capacity")
        return {**settings, "route_enabled": True, "accepted_route_rate_version": version, "accepted_route_rate_micros": rate}
    if version is not None or rate is not None:
        raise ValidationError("Rate consent applies only when enabling Route")
    return {**settings, "route_enabled": False}


def route_result(value: Any, pool_id: str, payload: dict[str, Any]) -> Any:
    if not isinstance(value, dict) or value.get("id") != pool_id:
        raise APIError("The API returned Route settings for a different pool")
    for key, expected in payload.items():
        if key.startswith("accepted_route_"):
            continue
        if value.get(key) != expected or type(value.get(key)) is bool and type(expected) is not bool:
            raise APIError("The API did not confirm the submitted Route settings. Refresh before trying again")
    if payload.get("route_enabled") is True:
        rate = value.get("platform_rate_micros")
        version = value.get("route_price_version")
        legacy_paid = "route_price_version" not in value and payload["accepted_route_rate_version"] == "route-platform-v1"
        if type(rate) is not int or rate != payload["accepted_route_rate_micros"] or (not legacy_paid and version != payload["accepted_route_rate_version"]):
            raise APIError("The API did not confirm the accepted Route rate")
    return value
