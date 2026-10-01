"""Predict preserves paid consent, issued evidence, and customer-reported outcomes."""
import copy
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

import nodus
from nodus import cli
from test_pools import POOL, exercise

AS_OF = "2026-09-17T12:00:00Z"
SUBSCRIPTION = {"status": "active", "rate_version": "predict-account-monthly-v1", "monthly_micros": 99000000,
    "period_start": "2026-09-01T00:00:00Z", "period_end": "2026-10-01T00:00:00Z", "paid_current_period": True, "enabled_pool_count": 1}
CALIBRATION = {"from": "2026-08-18T00:00:00Z", "to": "2026-09-17T00:00:00Z", "status": "no_data", "expected_hours": 720,
    "forecast_hours": 0, "evaluated_hours": 0, "covered_hours": 0, "hourly_coverage": None, "pinball_p10": None,
    "pinball_p50": None, "pinball_p90": None, "complete_days": 0, "covered_days": 0, "daily_coverage": None}
FORECAST = {"pool_id": "pool_test", "predict_enabled": True, "refresh_status": "active", "subscription": SUBSCRIPTION,
    "snapshot": {"pool_id": "pool_test", "generated_at": AS_OF, "owned_devices": 4, "advisory_burst_price": None,
        "calibration": CALIBRATION, "forecast": {"model": "seasonal_weekly_v1", "status": "ready", "as_of": AS_OF,
            "horizon_days": 7, "history_from": "2026-08-20T12:00:00Z", "history_hours": 672, "required_history_hours": 672,
            "points": [{"hour": (datetime(2026, 9, 17, 12, tzinfo=timezone.utc) + timedelta(hours=i)).isoformat(),
                        "p10": 1.0, "p50": 2.0, "p90": 3.0} for i in range(168)]}}}
EVIDENCE = {"method": "seasonal_empirical_inverse_cdf", "history_from": "2026-08-20T12:00:00Z", "history_to": AS_OF,
    "history_hours": 672, "horizon_from": AS_OF, "horizon_to": "2026-10-17T12:00:00Z", "horizon_hours": 720,
    "scenarios_per_hour": 4, "current_devices": 4, "released_devices": 2, "owned_micros_per_device_hour": 1000000,
    "burst_micros_per_device_hour": 3000000, "critical_fractile": 0.75, "expected_burst_device_hours": 4,
    "expected_burst_micros": 12000000, "released_owned_micros": 1440000000, "advisory_price": True,
    "demand_distribution": [{"device_hours": 2, "scenario_count": 2872}, {"device_hours": 4, "scenario_count": 8}]}
RECOMMENDATION = {"id": "rec_test", "pool_id": "pool_test", "kind": "rightsize", "generated_at": AS_OF,
    "expires_at": "2026-09-18T12:00:00Z", "state": "open", "outcome": None,
    "recommendation": {"kind": "rightsize", "level": "recommend", "risk": "medium", "recommended_devices": 2,
        "expected_savings_micros": 1428000000, "evidence": EVIDENCE}}
RECOMMENDATIONS = {"pool_id": "pool_test", "predict_enabled": True, "refresh_status": "active", "recommendations": [RECOMMENDATION]}
OUTCOME = {"recorded_at": AS_OF, "outcome": "Changed my scheduler capacity manually", "reported_saving_micros": None}


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("status", ["active", "disabled", "paused_credits", "paused_cap", "paused_payment", "consent_required", "stale"])
def test_forecast_keeps_cached_evidence_and_unknown_calibration(asynchronous, status):
    body = {**FORECAST, "refresh_status": status, "predict_enabled": status != "disabled"}
    def handler(req):
        assert (req.method, req.url.path) == ("GET", "/v1/pools/pool_test/forecast")
        assert dict(req.url.params) == {"horizon": "7"}
        return httpx.Response(200, json=body)
    result = exercise(handler, asynchronous, lambda pools: pools.forecast("pool_test", horizon=7))
    assert isinstance(result, nodus.PoolForecast)
    assert result.refresh_status == status
    assert result.subscription.monthly_micros == 99000000
    assert result.snapshot.forecast.points[0].p50 == 2
    assert result.snapshot.calibration.hourly_coverage is None
    assert result.snapshot.advisory_burst_price is None


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("free_version", [None, "byoc-free-v1"])
def test_forecast_without_snapshot_does_not_invent_a_series(asynchronous, free_version):
    subscription = {**SUBSCRIPTION, "rate_version": free_version, "monthly_micros": 0, "paid_current_period": False} if free_version else SUBSCRIPTION
    result = exercise(lambda req: httpx.Response(200, json={**FORECAST, "snapshot": None, "subscription": subscription, "refresh_status": "awaiting_refresh"}),
                      asynchronous, lambda pools: pools.forecast("pool_test"))
    assert result.snapshot is None
    assert result.subscription.monthly_micros == subscription["monthly_micros"]
    assert result.subscription.paid_current_period is subscription["paid_current_period"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_recommendations_keep_estimates_separate_from_reported_outcome(asynchronous):
    def handler(req):
        assert req.url.path == "/v1/pools/pool_test/recommendations"
        return httpx.Response(200, json=RECOMMENDATIONS)
    result = exercise(handler, asynchronous, lambda pools: pools.recommendations("pool_test"))
    recommendation = result.recommendations[0]
    assert recommendation.outcome is None
    assert recommendation.recommendation["expected_savings_micros"] == 1428000000
    assert recommendation.recommendation["evidence"] == EVIDENCE


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("saving", [None, 0, 1000000])
def test_done_only_sends_explicit_customer_report(asynchronous, saving):
    def handler(req):
        assert (req.method, req.url.path) == ("POST", "/v1/pools/pool_test/recommendations/rec_test/done")
        expected = {"outcome": OUTCOME["outcome"]}
        if saving is not None:
            expected["reported_saving_micros"] = saving
        assert json.loads(req.content) == expected
        return httpx.Response(200, json={**OUTCOME, "reported_saving_micros": saving})
    result = exercise(handler, asynchronous, lambda pools: pools.recommendation_done("pool_test", "rec_test", OUTCOME["outcome"], reported_saving_micros=saving))
    assert result.reported_saving_micros == saving


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_predict_activation_requires_and_sends_exact_consent(asynchronous, enabled):
    def handler(req):
        assert (req.method, req.url.path) == ("PATCH", "/v1/pools/pool_test")
        expected = {"predict_enabled": enabled}
        if enabled:
            expected.update(accepted_predict_rate_version="predict-account-monthly-v1", accepted_predict_monthly_micros=99000000)
        assert json.loads(req.content) == expected
        return httpx.Response(200, json={**POOL, "predict_enabled": enabled})
    kwargs = {"accepted_rate_version": "predict-account-monthly-v1", "accepted_monthly_micros": 99000000} if enabled else {}
    assert exercise(handler, asynchronous, lambda pools: pools.set_predict("pool_test", enabled, **kwargs)).predict_enabled is enabled


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("operation,args,kwargs", [
    ("set_predict", ("pool_test", True), {}),
    ("set_predict", ("pool_test", "yes"), {}),
    ("set_predict", ("pool_test", True), {"accepted_rate_version": "v1", "accepted_monthly_micros": True}),
    ("set_predict", ("pool_test", False), {"accepted_monthly_micros": 99000000}),
    ("forecast", ("pool_test",), {"horizon": 8}),
    ("forecast", ("pool_test",), {"horizon": True}),
    ("recommendation_done", ("pool_test", "../other", "done"), {}),
    ("recommendation_done", ("pool_test", "rec_test", ""), {}),
    ("recommendation_done", ("pool_test", "rec_test", "界" * 1000), {}),
    ("recommendation_done", ("pool_test", "rec_test", "done"), {"reported_saving_micros": -1}),
])
def test_invalid_predict_requests_never_reach_network(asynchronous, operation, args, kwargs):
    with pytest.raises(nodus.ValidationError):
        exercise(lambda req: pytest.fail("invalid input reached network"), asynchronous,
                 lambda pools: getattr(pools, operation)(*args, **kwargs))


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("defect", ["price_bool", "unordered_band", "negative_band", "false_calibration", "incomplete_points", "huge_band", "unpaid_active", "free_nonzero", "private_nonzero", "route_policy_zero", "unknown_free", "legacy_unpaid_zero"])
def test_malformed_forecast_evidence_is_refused(asynchronous, defect):
    body = copy.deepcopy(FORECAST)
    if defect == "unpaid_active": body["subscription"]["paid_current_period"] = False
    if defect == "free_nonzero": body["subscription"].update(rate_version="byoc-free-v1", monthly_micros=1, paid_current_period=False)
    if defect == "private_nonzero": body["subscription"].update(rate_version="private-supplier-cost-v2", monthly_micros=1, paid_current_period=False)
    if defect == "route_policy_zero": body["subscription"].update(rate_version="private-supplier-cost-v2", monthly_micros=0, paid_current_period=False)
    if defect == "unknown_free": body["subscription"].update(rate_version="unknown-free-policy", monthly_micros=0, paid_current_period=False)
    if defect == "legacy_unpaid_zero": body["subscription"].update(monthly_micros=0, paid_current_period=False)
    if defect == "price_bool": body["subscription"]["monthly_micros"] = True
    if defect == "unordered_band": body["snapshot"]["forecast"]["points"][0]["p50"] = 10
    if defect == "huge_band": body["snapshot"]["forecast"]["points"][0]["p10"] = 10**1000
    if defect == "negative_band": body["snapshot"]["forecast"]["points"][0]["p10"] = -1
    if defect == "false_calibration": body["snapshot"]["calibration"]["hourly_coverage"] = 0
    if defect == "incomplete_points": body["snapshot"]["forecast"]["points"].pop()
    with pytest.raises(nodus.APIError):
        exercise(lambda req: httpx.Response(200, json=body), asynchronous, lambda pools: pools.forecast("pool_test"))


def test_predict_cli_requires_explicit_price_and_never_invents_reported_savings(monkeypatch, capsys):
    calls = []
    def factory(**kwargs):
        client = nodus.Client(api_key="nk_test", base_url="https://nodus.invalid")
        def handler(req):
            calls.append(req)
            if req.method == "PATCH": return httpx.Response(200, json={**POOL, "predict_enabled": True})
            if req.url.path.endswith("/done"): return httpx.Response(200, json=OUTCOME)
            return httpx.Response(200, json=FORECAST if req.url.path.endswith("/forecast") else RECOMMENDATIONS)
        client._http = httpx.Client(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler))
        return client
    monkeypatch.setattr(cli, "Client", factory)
    assert cli.main(["pools", "predict", "pool_test", "on"]) == 2
    assert calls == []
    assert cli.main(["pools", "predict", "pool_test", "on", "--accept-rate-version", "predict-account-monthly-v1", "--accept-monthly-micros", "99000000"]) == 0
    assert cli.main(["pools", "forecast", "pool_test", "--horizon", "7", "--json"]) == 0
    assert cli.main(["pools", "recommendations", "pool_test", "--state", "open", "--limit", "25", "--cursor", "opaque_cursor", "--json"]) == 0
    assert dict(calls[-1].url.params) == {"state": "open", "limit": "25", "cursor": "opaque_cursor"}
    assert cli.main(["pools", "mark-done", "pool_test", "rec_test", "--outcome", OUTCOME["outcome"]]) == 0
    assert json.loads(calls[-1].content) == {"outcome": OUTCOME["outcome"]}


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("defect", ["status", "ratio", "window", "days"])
def test_calibration_cannot_overstate_issued_outcomes(asynchronous, defect):
    body = copy.deepcopy(FORECAST)
    calibration = body["snapshot"]["calibration"]
    calibration.update(status="partial", forecast_hours=24, evaluated_hours=24, covered_hours=12,
                       hourly_coverage=0.5, pinball_p10=0.1, pinball_p50=0.1, pinball_p90=0.1,
                       complete_days=1, covered_days=0, daily_coverage=0)
    if defect == "status": calibration["status"] = "complete"
    if defect == "ratio": calibration["hourly_coverage"] = 1
    if defect == "window": calibration["expected_hours"] = 24
    if defect == "days": calibration["complete_days"] = 2
    with pytest.raises(nodus.APIError):
        exercise(lambda req: httpx.Response(200, json=body), asynchronous, lambda pools: pools.forecast("pool_test"))


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("defect", ["missing_price", "saving", "released_cost", "count", "history", "advisory"])
def test_inconsistent_rightsize_evidence_cannot_be_presented_as_savings(asynchronous, defect):
    body = copy.deepcopy(RECOMMENDATIONS)
    rec = body["recommendations"][0]["recommendation"]
    evidence = rec["evidence"]
    if defect == "missing_price": del evidence["burst_micros_per_device_hour"]
    if defect == "saving": rec["expected_savings_micros"] += 1
    if defect == "released_cost": evidence["owned_micros_per_device_hour"] += 1
    if defect == "count": evidence["released_devices"] += 1
    if defect == "history": evidence["history_hours"] = "<img src=x>"
    if defect == "advisory": evidence["advisory_price"] = False
    with pytest.raises(nodus.APIError):
        exercise(lambda req: httpx.Response(200, json=body), asynchronous, lambda pools: pools.recommendations("pool_test"))


IDLE = {"kind": "idle_reclaim", "level": "recommend", "risk": "high", "advisory_only": True,
    "host_id": "host_test", "device_id": "dev_test", "device_index": 0, "expected_savings_micros": None,
    "expires_at": "2026-09-17T12:03:00Z", "evidence": {"method": "sampled_allocated_sm_below_5", "sm_util_below_pct": 5,
        "trigger_from": "2026-09-17T11:30:00Z", "trigger_to": AS_OF, "consecutive_seconds": 1800,
        "observed_through": AS_OF, "foreign": True, "history_from": "2026-09-10T12:00:00Z", "history_to": AS_OF,
        "historical_observed_hours": 168}}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_idle_advice_preserves_device_evidence_without_inventing_savings(asynchronous):
    row = {**RECOMMENDATION, "kind": "idle_reclaim", "expires_at": IDLE["expires_at"], "recommendation": IDLE}
    result = exercise(lambda req: httpx.Response(200, json={**RECOMMENDATIONS, "recommendations": [row]}), asynchronous,
                      lambda pools: pools.recommendations("pool_test"))
    assert result.recommendations[0].recommendation["expected_savings_micros"] is None
    assert result.recommendations[0].recommendation["evidence"]["foreign"] is True


DRAIN = {"kind": "drain_window", "level": "recommend", "risk": "high", "advisory_only": True,
    "host_id": "host_test", "expected_savings_micros": None, "expires_at": "2026-09-17T13:00:00Z",
    "evidence": {"method": "host_seasonal_weekly_p90_below_one", "window_from": "2026-09-17T13:00:00Z",
        "window_to": "2026-09-17T16:00:00Z", "consecutive_hours": 3, "threshold_devices": 1,
        "max_p90_device_hours": 0.5, "history_from": "2026-08-20T12:00:00Z", "history_to": AS_OF,
        "history_hours": 672, "model": "seasonal_weekly_v1"}}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_drain_window_is_advice_with_no_invented_saving(asynchronous):
    row = {**RECOMMENDATION, "kind": "drain_window", "expires_at": DRAIN["expires_at"], "recommendation": DRAIN}
    result = exercise(lambda req: httpx.Response(200, json={**RECOMMENDATIONS, "recommendations": [row]}), asynchronous,
                      lambda pools: pools.recommendations("pool_test"))
    assert result.recommendations[0].recommendation["expected_savings_micros"] is None
    assert result.recommendations[0].recommendation["evidence"]["consecutive_hours"] == 3


@pytest.mark.parametrize("asynchronous", [False, True])
def test_malformed_drain_duration_is_a_wire_error_not_an_overflow(asynchronous):
    recommendation = copy.deepcopy(DRAIN)
    recommendation["evidence"]["consecutive_hours"] = 2**63 - 1
    row = {**RECOMMENDATION, "kind": "drain_window", "expires_at": DRAIN["expires_at"], "recommendation": recommendation}
    with pytest.raises(nodus.APIError):
        exercise(lambda req: httpx.Response(200, json={**RECOMMENDATIONS, "recommendations": [row]}), asynchronous,
                 lambda pools: pools.recommendations("pool_test"))


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("operation", ["forecast", "recommendations", "set_predict"])
def test_predict_response_belongs_to_the_requested_pool(asynchronous, operation):
    if operation == "set_predict":
        body = {**POOL, "id": "pool_other"}
        action = lambda pools: pools.set_predict("pool_test", False)
    elif operation == "forecast":
        body = copy.deepcopy(FORECAST)
        body["pool_id"] = body["snapshot"]["pool_id"] = "pool_other"
        action = lambda pools: pools.forecast("pool_test")
    else:
        body = copy.deepcopy(RECOMMENDATIONS)
        body["pool_id"] = body["recommendations"][0]["pool_id"] = "pool_other"
        action = lambda pools: pools.recommendations("pool_test")
    with pytest.raises(nodus.APIError):
        exercise(lambda req: httpx.Response(200, json=body), asynchronous, action)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("field,value", [("outcome", "A different change"), ("reported_saving_micros", 0)])
def test_manual_outcome_acknowledgement_matches_customer_report(asynchronous, field, value):
    with pytest.raises(nodus.APIError):
        exercise(lambda req: httpx.Response(200, json={**OUTCOME, field: value}), asynchronous,
                 lambda pools: pools.recommendation_done("pool_test", "rec_test", OUTCOME["outcome"]))


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("draft", [IDLE, DRAIN])
def test_advice_expiry_cannot_differ_from_wrapper(asynchronous, draft):
    row = {**RECOMMENDATION, "kind": draft["kind"], "recommendation": draft}
    with pytest.raises(nodus.APIError):
        exercise(lambda req: httpx.Response(200, json={**RECOMMENDATIONS, "recommendations": [row]}), asynchronous,
                 lambda pools: pools.recommendations("pool_test"))


@pytest.mark.parametrize("asynchronous", [False, True])
def test_manual_outcome_acknowledgement_preserves_exact_submitted_whitespace(asynchronous):
    report = " Manual scheduler change "
    result = exercise(lambda req: httpx.Response(200, json={**OUTCOME, "outcome": report}), asynchronous,
                      lambda pools: pools.recommendation_done("pool_test", "rec_test", report))
    assert result.outcome == report


DEFRAGMENT = {"kind": "defragment", "level": "recommend", "risk": "high", "advisory_only": True,
    "expected_savings_micros": None, "expires_at": "2026-09-18T12:00:00Z",
    "evidence": {"method": "route_fragmentation_intervals_v1", "history_from": "2026-09-03T12:00:00Z",
        "history_to": AS_OF, "observed_hours": 336, "fragmentation_wait_seconds": 7200.5,
        "threshold_seconds": 3600, "packing_policy": "fill_hosts_first", "foreign_jobs_movable": False}}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_recommendation_pages_preserve_opaque_cursor_and_state(asynchronous):
    def handler(req):
        assert dict(req.url.params) == {"limit": "25", "cursor": "opaque_cursor", "state": "expired"}
        return httpx.Response(200, json={**RECOMMENDATIONS, "next_cursor": "next_page"})
    result = exercise(handler, asynchronous, lambda pools: pools.recommendations("pool_test", limit=25, cursor="opaque_cursor", state="expired"))
    assert result.next_cursor == "next_page"
    assert result.recommendations[0].id == "rec_test"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("options", [{"limit": 0}, {"limit": 101}, {"limit": True}, {"cursor": ""}, {"cursor": "a" * 2049}, {"cursor": 3}, {"state": "unknown"}])
def test_invalid_recommendation_page_arguments_never_reach_transport(asynchronous, options):
    def unexpected(req):
        pytest.fail("invalid page arguments reached the API")
    with pytest.raises(nodus.ValidationError):
        exercise(unexpected, asynchronous, lambda pools: pools.recommendations("pool_test", **options))


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("cursor", ["", 5, [], "a" * 2049])
def test_invalid_returned_cursor_is_a_wire_error(asynchronous, cursor):
    with pytest.raises(nodus.APIError):
        exercise(lambda req: httpx.Response(200, json={**RECOMMENDATIONS, "next_cursor": cursor}), asynchronous,
                 lambda pools: pools.recommendations("pool_test"))


@pytest.mark.parametrize("asynchronous", [False, True])
def test_defragment_advice_preserves_observed_wait_and_no_savings(asynchronous):
    row = {**RECOMMENDATION, "kind": "defragment", "recommendation": DEFRAGMENT}
    result = exercise(lambda req: httpx.Response(200, json={**RECOMMENDATIONS, "recommendations": [row], "next_cursor": None}), asynchronous,
                      lambda pools: pools.recommendations("pool_test"))
    assert result.next_cursor is None
    assert result.recommendations[0].recommendation["expected_savings_micros"] is None
    assert result.recommendations[0].recommendation["evidence"]["fragmentation_wait_seconds"] == 7200.5
    assert result.recommendations[0].recommendation["evidence"]["foreign_jobs_movable"] is False


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("field,value", [("observed_hours", 335), ("fragmentation_wait_seconds", 3600), ("threshold_seconds", 0),
    ("foreign_jobs_movable", True), ("packing_policy", "move_foreign"), ("method", "inferred_utilization"), ("history_from", AS_OF)])
def test_defragment_rejects_incomplete_or_unsafe_evidence(asynchronous, field, value):
    draft = copy.deepcopy(DEFRAGMENT)
    draft["evidence"][field] = value
    row = {**RECOMMENDATION, "kind": "defragment", "recommendation": draft}
    with pytest.raises(nodus.APIError):
        exercise(lambda req: httpx.Response(200, json={**RECOMMENDATIONS, "recommendations": [row]}), asynchronous,
                 lambda pools: pools.recommendations("pool_test"))

@pytest.mark.parametrize("asynchronous", [False, True])
def test_drain_advice_remains_valid_inside_its_retained_window(asynchronous):
    draft = copy.deepcopy(DRAIN)
    draft["expires_at"] = "2026-09-17T15:00:00Z"
    draft["evidence"]["window_from"] = AS_OF
    draft["evidence"]["window_to"] = "2026-09-17T15:00:00Z"
    row = {**RECOMMENDATION, "kind": "drain_window", "expires_at": draft["expires_at"], "recommendation": draft}
    page = exercise(lambda req: httpx.Response(200, json={**RECOMMENDATIONS, "recommendations": [row]}), asynchronous,
        lambda pools: pools.recommendations("pool_test"))
    assert page.recommendations[0].recommendation["evidence"]["window_from"] == AS_OF

WAIT_TUNING = {"kind": "wait_tuning", "level": "recommend", "risk": "high", "advisory_only": True,
    "expected_savings_micros": None, "expires_at": "2026-09-18T12:00:00Z",
    "evidence": {"method": "bounded_proportional_wait_v1", "history_from": "2026-09-03T12:00:00Z", "history_to": AS_OF,
        "observed_hours": 336, "completed_executions": 2, "wait_seconds": 200, "runtime_seconds": 1000,
        "burst_spent_micros": 1000000, "observed_delay_pct": 20.0, "waiting_budget_pct": 10.0,
        "current_alpha": 0.1, "proposed_alpha": 0.075, "policy_version": 1, "source_sha256": "a" * 64}}

@pytest.mark.parametrize("asynchronous", [False, True])
def test_wait_tuning_keeps_settled_evidence_without_invented_saving(asynchronous):
    row = {**RECOMMENDATION, "kind": "wait_tuning", "expires_at": WAIT_TUNING["expires_at"], "recommendation": WAIT_TUNING}
    page = exercise(lambda req: httpx.Response(200, json={**RECOMMENDATIONS, "recommendations": [row]}), asynchronous, lambda p: p.recommendations("pool_test"))
    assert page.recommendations[0].recommendation["expected_savings_micros"] is None
    assert page.recommendations[0].recommendation["evidence"]["completed_executions"] == 2


def queued_forecast():
    series = copy.deepcopy(FORECAST["snapshot"]["forecast"])
    for point in series["points"]:
        point["queue_device_hours"] = 0
    series["points"][1].update(p10=3, p50=4, p90=5, queue_device_hours=2)
    series["queue"] = {"method": "retained_queue_immediate_start_v1", "observed_at": AS_OF,
        "known_jobs": 1, "unknown_jobs": 2, "known_device_hours": 3,
        "added_device_hours": 2, "outside_horizon_device_hours": 1}
    return series


def test_forecast_preserves_queue_contribution_and_unknown_jobs():
    series = nodus.ForecastSeries.from_dict(queued_forecast())
    assert series.queue.unknown_jobs == 2
    assert series.queue.added_device_hours == 2
    assert series.points[1].queue_device_hours == 2
    assert nodus.ForecastSeries.from_dict(FORECAST["snapshot"]["forecast"]).queue is None


@pytest.mark.parametrize("field,value", [("unknown_jobs", -1), ("known_jobs", True),
    ("method", "guessed"), ("added_device_hours", 4), ("known_device_hours", float("inf")),
    ("observed_at", "2026-09-17T13:00:00Z")])
def test_forecast_rejects_contradictory_queue_evidence(field, value):
    series = queued_forecast()
    series["queue"][field] = value
    with pytest.raises(nodus.APIError):
        nodus.ForecastSeries.from_dict(series)
