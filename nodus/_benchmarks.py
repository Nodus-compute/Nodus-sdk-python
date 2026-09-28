"""Benchmark request validation without client-side budget allocation."""
from __future__ import annotations

import math
from typing import Any


def request_payload(workload: dict[str, Any], gpu_families: list[str],
                    batch_sizes: list[int], regions: list[str], repetitions: int,
                    budget: float, idempotency_key: str) -> dict[str, Any]:
    from . import _valid_idempotency_key
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValueError("an explicit idempotency_key is required")
    _valid_idempotency_key(idempotency_key)
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float))):
        raise ValueError("budget must be a finite positive USD amount")
    try:
        valid_budget = budget is None or math.isfinite(budget) and budget >= 0
    except OverflowError:
        valid_budget = False
    if not valid_budget:
        raise ValueError("budget must be a finite positive USD amount")
    if not isinstance(workload, dict):
        raise ValueError("workload must be an API workload payload")
    return {"workload": workload, **({"budget_usd": budget} if budget is not None else {}),
            "matrix": {"gpu_families": gpu_families, "batch_sizes": batch_sizes,
                       "regions": regions, "repetitions": repetitions}}
