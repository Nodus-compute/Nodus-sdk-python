"""An RL run is something you can ask for by name.

Before this, RL metadata reached the control plane through ``extra``, an
untyped dict that no signature mentions and nothing checks until the server
answers 400. These tests hold the typed parameter to the same shape the
control plane validates.
"""

from __future__ import annotations

import json

import httpx
import pytest

import nodus


def client(captured: list[dict]) -> "nodus.Client":
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/workloads" and request.method == "POST":
            captured.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "wl_1", "status": "queued"})
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    made = nodus.Client(api_key="nk_test", base_url="https://nodus.invalid")
    made._http = httpx.Client(
        base_url="https://nodus.invalid",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer nk_test"},
    )
    return made


def test_setup_is_exported() -> None:
    assert hasattr(nodus, "RLSetup")
    assert "RLSetup" in nodus.__all__


def test_run_sends_the_metadata_the_control_plane_validates() -> None:
    captured: list[dict] = []
    client(captured).run(
        command="python train.py",
        image="python:3.12",
        budget=5.0,
        rl=nodus.RLSetup(environment_id="custom", mode="evaluate",
                         model="Qwen/Qwen3-1.7B", planned_tasks=64),
    )
    rl = captured[0]["rl"]
    assert rl == {
        "schema_version": 1,
        "environment_id": "custom",
        "mode": "evaluate",
        "model": "Qwen/Qwen3-1.7B",
        "planned_tasks": 64,
    }


def test_a_mode_the_control_plane_rejects_is_refused_before_submission() -> None:
    captured: list[dict] = []
    with pytest.raises(ValueError):
        client(captured).run(
            command="python train.py", image="python:3.12", budget=5.0,
            rl=nodus.RLSetup(environment_id="custom", mode="launch",
                             model="m", planned_tasks=8),
        )
    assert captured == []


@pytest.mark.parametrize("tasks", [0, 10001])
def test_a_task_count_outside_the_accepted_range_is_refused(tasks: int) -> None:
    captured: list[dict] = []
    with pytest.raises(ValueError):
        client(captured).run(
            command="python train.py", image="python:3.12", budget=5.0,
            rl=nodus.RLSetup(environment_id="custom", mode="train",
                             model="m", planned_tasks=tasks),
        )
    assert captured == []


def test_an_environment_id_the_control_plane_rejects_is_refused() -> None:
    captured: list[dict] = []
    with pytest.raises(ValueError):
        client(captured).run(
            command="python train.py", image="python:3.12", budget=5.0,
            rl=nodus.RLSetup(environment_id="Not An Id", mode="train",
                             model="m", planned_tasks=8),
        )
    assert captured == []


def test_a_run_without_rl_sends_no_rl_field() -> None:
    captured: list[dict] = []
    client(captured).run(command="python train.py", image="python:3.12", budget=5.0)
    assert "rl" not in captured[0]


def test_extra_still_reaches_the_payload_for_a_caller_already_using_it() -> None:
    captured: list[dict] = []
    client(captured).run(
        command="python train.py", image="python:3.12", budget=5.0,
        extra={"rl": {"schema_version": 1, "environment_id": "custom",
                      "mode": "train", "model": "m", "planned_tasks": 4}},
    )
    assert captured[0]["rl"]["mode"] == "train"
