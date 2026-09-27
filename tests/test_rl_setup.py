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


# The control plane's rules, exactly. A value it rejects must fail here, before
# submission, not as a 400 after it.
@pytest.mark.parametrize("environment_id", ["-abc", "a" * 65, "custom\n", "--", ""])
def test_environment_ids_the_server_rejects_are_refused(environment_id: str) -> None:
    captured: list[dict] = []
    with pytest.raises(ValueError):
        client(captured).run(command="python train.py", image="python:3.12",
                             rl=nodus.RLSetup(environment_id=environment_id, mode="train",
                                              model="m", planned_tasks=8))
    assert captured == []


@pytest.mark.parametrize("model", [" gpt", "gpt ", "a\nb", "x\x00y", "é" * 129])
def test_models_the_server_rejects_are_refused(model: str) -> None:
    captured: list[dict] = []
    with pytest.raises(ValueError):
        client(captured).run(command="python train.py", image="python:3.12",
                             rl=nodus.RLSetup(environment_id="custom", mode="train",
                                              model=model, planned_tasks=8))
    assert captured == []


def test_rl_and_extra_rl_together_are_refused_rather_than_one_silently_winning() -> None:
    captured: list[dict] = []
    with pytest.raises(ValueError):
        client(captured).run(
            command="python train.py", image="python:3.12",
            rl=nodus.RLSetup(environment_id="custom", mode="train", model="m", planned_tasks=8),
            extra={"rl": {"schema_version": 1, "environment_id": "gsm8k", "mode": "train",
                          "model": "m", "planned_tasks": 8}},
        )
    assert captured == []


@pytest.mark.parametrize("mapping", [
    {"environment_id": "custom", "mode": "train", "model": "m", "planned_tasks": 8, "recipe": {"id": "x"}},
    {"environment_id": "custom", "mode": "train", "model": "m", "planned_tasks": 8, "schema_version": 2},
])
def test_a_mapping_with_fields_rl_cannot_carry_is_refused(mapping: dict) -> None:
    captured: list[dict] = []
    with pytest.raises(ValueError):
        client(captured).run(command="python train.py", image="python:3.12", rl=mapping)
    assert captured == []


def test_an_example_id_is_carried_and_checked_like_the_server_does() -> None:
    setup = nodus.RLSetup(environment_id="gsm8k", mode="train", model="Qwen/Qwen3-1.7B",
                          planned_tasks=64, example_id="gsm8k-trained")
    assert setup.to_payload()["example_id"] == "gsm8k-trained"
    assert "example_id" not in nodus.RLSetup(environment_id="custom", mode="train", model="m", planned_tasks=1).to_payload()
    with pytest.raises(ValueError):
        nodus.RLSetup(environment_id="gsm8k", mode="train", model="m", planned_tasks=1, example_id="Bad ID").to_payload()


def test_an_example_run_records_which_example_it_came_from() -> None:
    from nodus._rl_setup import rl_payload

    assert rl_payload({"environment_id": "gsm8k", "mode": "train", "model": "m", "planned_tasks": 1,
                       "example_id": "gsm8k-trained"})["example_id"] == "gsm8k-trained"
