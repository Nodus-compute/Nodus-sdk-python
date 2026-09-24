"""Shared drafts retain exact edits and reject stale revision writes."""

import json
from typing import get_type_hints

import httpx
import pytest

import nodus
from test_operations import exercise


# Shared form edits preserve omission and deletion at the HTTP boundary.
# Exact wire values expose changed budgets or overwritten revisions on retry.
@pytest.mark.parametrize("asynchronous", [False, True])
def test_empty_run_draft_does_not_invent_values(asynchronous):
    def handler(request):
        assert (request.method, request.url.path) == ("POST", "/v1/operations/v1/run_draft.get")
        assert json.loads(request.content) == {}
        return httpx.Response(200, json={"revision": 0, "values": {}, "updated_at": None})

    draft = exercise(asynchronous, handler, lambda operations: operations.get_run_draft())
    assert isinstance(draft, nodus.RunDraft)
    assert draft.revision == 0 and draft.values == {} and draft.updated_at is None
    assert "max_cost_usd" not in draft.values


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("patch,saved", [
    ({"command": "python train.py", "max_cost_usd": 2.5},
     {"command": "python train.py", "max_cost_usd": 2.5, "image": "python:3.12"}),
    ({"gpu": None, "max_cost_usd": None}, {"command": "python train.py", "image": "python:3.12"}),
    ({"vcpus": 2.5, "disk_gb": 80.5, "data_regions": ["us-east-1"]},
     {"vcpus": 2.5, "disk_gb": 80.5, "data_regions": ["us-east-1"], "image": "python:3.12"}),
    ({"vcpus": None, "disk_gb": None, "data_regions": []}, {"data_regions": [], "image": "python:3.12"}),
    ({"data_regions": None}, {"image": "python:3.12"}),
    ({"name": "", "checkpoint_paths": [], "result_paths": ["results/model.bin"]},
     {"name": "", "checkpoint_paths": [], "result_paths": ["results/model.bin"], "image": "python:3.12"}),
    ({"vcpus": 2.5, "disk_gb": 80.5, "data_regions": ["us-east-1", "eu-west-1"]},
     {"vcpus": 2.5, "disk_gb": 80.5, "data_regions": ["us-east-1", "eu-west-1"], "max_cost_usd": 3}),
    ({"vcpus": None, "disk_gb": None, "data_regions": None}, {"max_cost_usd": 3}),
    ({"data_regions": []}, {"data_regions": [], "disk_gb": 80.5}),
    ({"data_regions": ["é" * 64, *[f"region-{index}" for index in range(31)]]},
     {"data_regions": ["é" * 64, *[f"region-{index}" for index in range(31)]]}),
    # The server preserves raw JSON integers that round to a finite float64.
    ({"vcpus": (1 << 1024) - (1 << 971) + 1}, {"vcpus": (1 << 1024) - (1 << 971) + 1}),
])
def test_run_draft_patch_preserves_omission_and_null(asynchronous, patch, saved):
    expected = {"expected_revision": 4, "patch": patch}

    def handler(request):
        assert (request.method, request.url.path) == ("POST", "/v1/operations/v1/run_draft.update")
        assert json.loads(request.content) == expected
        assert "Idempotency-Key" not in request.headers
        return httpx.Response(200, json={"revision": 5, "values": saved, "updated_at": "2026-09-21T12:00:00Z"})

    draft = exercise(asynchronous, handler, lambda operations: operations.update_run_draft(patch, expected_revision=4))
    assert isinstance(draft, nodus.RunDraft)
    assert draft.revision == 5 and draft.values == saved and draft.updated_at == "2026-09-21T12:00:00Z"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_run_draft_conflict_never_rebases_or_submits(asynchronous):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/v1/operations/v1/run_draft.update"
        return httpx.Response(409, json={"error": "run_draft_conflict", "message": "Read the current draft before editing"})

    with pytest.raises(nodus.RunDraftConflictError):
        exercise(asynchronous, handler, lambda operations: operations.update_run_draft(
            {"max_cost_usd": 3}, expected_revision=2), max_retries=2)
    assert len(requests) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_run_draft_retry_freezes_patch_and_revision(asynchronous, monkeypatch):
    patch = {"max_cost_usd": 3, "checkpoint_paths": ["state"]}
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            patch["max_cost_usd"] = 300
            patch["checkpoint_paths"].append("private")
            return httpx.Response(503, json={"error": "unavailable", "message": "Retry"})
        return httpx.Response(200, json={"revision": 3, "values": {"max_cost_usd": 3, "checkpoint_paths": ["state"]}})

    monkeypatch.setattr(nodus._Transport, "_backoff", staticmethod(lambda attempt, response: 0))
    draft = exercise(asynchronous, handler, lambda operations: operations.update_run_draft(
        patch, expected_revision=2), max_retries=1)
    assert draft.revision == 3 and draft.updated_at is None
    assert len(requests) == 2 and requests[0].content == requests[1].content
    assert json.loads(requests[1].content) == {"expected_revision": 2, "patch": {"max_cost_usd": 3, "checkpoint_paths": ["state"]}}


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("receipt", [
    {}, {"revision": True, "values": {}}, {"revision": -1, "values": {}},
    {"revision": 1, "values": None}, {"revision": 1, "values": [], "updated_at": None},
    {"revision": 1, "values": {}, "updated_at": {"secret": "not a timestamp"}},
])
def test_invalid_run_draft_receipt_is_rejected(asynchronous, receipt):
    with pytest.raises(nodus.APIError):
        exercise(asynchronous, lambda request: httpx.Response(200, json=receipt), lambda operations: operations.get_run_draft())


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("revision,patch", [
    (True, {"name": "run"}), (-1, {"name": "run"}), ("0", {"name": "run"}),
    (0, []), (0, {"max_cost_usd": float("nan")}),
])
def test_invalid_local_run_draft_arguments_never_reach_transport(asynchronous, revision, patch):
    requests = []
    with pytest.raises(nodus.ValidationError):
        exercise(asynchronous, lambda request: requests.append(request), lambda operations: operations.update_run_draft(
            patch, expected_revision=revision))
    assert requests == []


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("values", [
    {"max_cost_usd": "3"}, {"max_cost_usd": None}, {"max_cost_usd": True},
    {"gpu_count": True}, {"gpu_count": 1.5}, {"result_paths": "results"},
    {"checkpoint_paths": [None]}, {"name": False}, {"memory_gb": float("inf")},
    {"max_cost_usd": float("nan")},
    {"vcpus": True}, {"vcpus": 0}, {"vcpus": -1}, {"vcpus": "4"}, {"vcpus": None},
    {"disk_gb": "80"}, {"disk_gb": 0}, {"disk_gb": float("inf")}, {"vcpus": float("nan")}, {"disk_gb": 10 ** 309},
    {"data_regions": None}, {"data_regions": "us-east-1"}, {"data_regions": [None]}, {"data_regions": [{}]},
    {"data_regions": ["us-east-1", "us-east-1"]}, {"data_regions": [""]},
    {"data_regions": ["us,east"]}, {"data_regions": ["us\x7feast"]}, {"data_regions": ["us\neast"]},
    {"data_regions": ["us\u00a0east"]}, {"data_regions": ["é" * 65]}, {"data_regions": ["\ud800"]},
    {"data_regions": [f"region-{index}" for index in range(33)]},
])
def test_invalid_saved_field_types_are_rejected(asynchronous, values):
    body = json.dumps({"revision": 1, "values": values})
    with pytest.raises(nodus.APIError):
        exercise(asynchronous, lambda request: httpx.Response(200, content=body,
            headers={"Content-Type": "application/json"}), lambda operations: operations.get_run_draft())


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("operation", ["get", "update"])
@pytest.mark.parametrize("unknown", [{"typo": {"unexpected": True}}, {"max_cost": 300}])
def test_unknown_saved_fields_cannot_become_typed_draft_receipts(asynchronous, operation, unknown):
    requests = []

    def handler(request):
        requests.append(request)
        assert (request.method, request.url.path) == ("POST", f"/v1/operations/v1/run_draft.{operation}")
        assert json.loads(request.content) == ({} if operation == "get" else {
            "expected_revision": 2, "patch": {"max_cost_usd": 3}})
        return httpx.Response(200, json={"revision": 3, "values": {"max_cost_usd": 3, **unknown}})

    with pytest.raises(nodus.APIError, match="invalid run draft field"):
        exercise(asynchronous, handler, lambda operations: operations.get_run_draft() if operation == "get"
            else operations.update_run_draft({"max_cost_usd": 3}, expected_revision=2), max_retries=2)
    assert len(requests) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_supported_saved_fields_remain_typed_without_defaults(asynchronous):
    values = {"name": "Research run", "command": "python train.py", "image": "python:3.12", "gpu": "H100",
              "gpu_count": 2, "memory_gb": 80, "max_cost_usd": 3.5,
              "vcpus": 4, "disk_gb": 100, "data_regions": ["us-east-1"],
              "checkpoint_paths": ["state"], "result_paths": ["results/model.bin"]}
    draft = exercise(asynchronous, lambda request: httpx.Response(200, json={"revision": 3, "values": values}),
                     lambda operations: operations.get_run_draft())
    assert isinstance(draft, nodus.RunDraft)
    assert draft.values == values
    for name, expected in {"vcpus": float, "disk_gb": float, "data_regions": list[str]}.items():
        assert get_type_hints(nodus.RunDraftValues)[name] == expected
        assert get_type_hints(nodus.RunDraftPatch)[name] == expected | None


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("revision", [1, 2, 4, 9007199254740992])
def test_update_receipt_must_confirm_exact_next_revision(asynchronous, revision):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"revision": revision, "values": {"max_cost_usd": 3}})

    with pytest.raises(nodus.APIError):
        exercise(asynchronous, handler, lambda operations: operations.update_run_draft(
            {"max_cost_usd": 3}, expected_revision=2))
    assert len(requests) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_draft_receipt_revision_is_exactly_representable(asynchronous):
    with pytest.raises(nodus.APIError):
        exercise(asynchronous, lambda request: httpx.Response(200,
            json={"revision": 9007199254740992, "values": {}}), lambda operations: operations.get_run_draft())


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("patch,values", [
    ({"max_cost_usd": 3}, {}),
    ({"max_cost_usd": 3}, {"max_cost_usd": 30}),
    ({"max_cost_usd": None}, {"max_cost_usd": 3}),
    ({"checkpoint_paths": []}, {"checkpoint_paths": ["state"]}),
    ({"name": ""}, {}),
    ({"data_regions": ["us-east-1"]}, {"data_regions": ["eu-west-1"]}),
    ({"disk_gb": 100}, {"disk_gb": 1000}),
])
def test_update_receipt_must_confirm_each_exact_patch_value(asynchronous, patch, values):
    with pytest.raises(nodus.APIError):
        exercise(asynchronous, lambda request: httpx.Response(200, json={"revision": 3, "values": values}),
            lambda operations: operations.update_run_draft(patch, expected_revision=2))
