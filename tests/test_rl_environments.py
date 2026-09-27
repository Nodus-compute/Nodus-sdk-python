"""Discover what can run, then run it, from code.

The fixture is the control plane's own catalog for GSM8K with its runner URL
resolved, the way GET /v1/rl-environments serves it.
"""

from __future__ import annotations

import json

import httpx
import pytest

import nodus

CATALOG = json.loads('{"schema_version": 1, "reviewed_at": "2026-09-19", "environments": [{"id": "gsm8k", "name": "GSM8K", "publisher": "OpenAI", "summary": "Eight thousand grade-school maths word problems, each with a worked solution and a single numeric answer.", "category": "Reasoning", "source_url": "https://github.com/openai/grade-school-math", "code_license": "MIT", "data_license": "MIT", "reward_type": "deterministic programmatic", "reward_description": "The final number is compared with the reference answer. No model judges the result.", "modes": ["train", "evaluate"], "readiness": "research", "requirements": ["Pin the dataset revision used for the train and test splits", "Keep held-out problems out of the training split", "Record the decoding settings used for both scores"], "launch": {"image": "pytorch/pytorch:2.14.0-cuda12.6-cudnn9-runtime", "model": "Qwen/Qwen3-1.7B", "default_tasks": 64, "measured_on": "A40, seed 42, greedy scoring, 2026-09-24", "note": "The change was measured by running this example. Training moves the score because the model writes out its working, so its attempts differ enough to learn from.", "results": ["outputs/results.json"], "examples": [{"dataset": "openai/gsm8k", "name": "GSM8K, trained", "mode": "train", "summary": "Score 64 grade-school word problems, train a LoRA adapter with GRPO, then score the same 64 again.", "baseline_pass_rate": 0.7969, "trained_pass_rate": 0.875, "measured_change_pp": 7.8, "runtime_minutes": 60, "command": "export NODUS_RL_EXAMPLE_BASE=https://api.nodus-compute.ai/v1/artifacts/rl-example && python3 -m pip install -q --break-system-packages transformers accelerate trl peft datasets && python3 -c \\"import os,urllib.request as u;[u.urlretrieve(os.environ[\'NODUS_RL_EXAMPLE_BASE\']+\'/\'+n,n) for n in (\'nodus_rl_events.py\',\'gsm8k_trainer.py\')]\\" && python3 gsm8k_trainer.py --model Qwen/Qwen3-1.7B --train-tasks 512 --heldout-tasks 64 --steps 150 --seed 42 --output outputs/results.json"}]}}, {"id": "listed-only", "name": "Listed only", "publisher": "Fixture", "summary": "No launch block.", "category": "Reasoning", "source_url": "https://github.com/example/listed", "code_license": "MIT", "data_license": "MIT", "reward_type": "deterministic", "reward_description": "Exact match.", "modes": ["evaluate"], "readiness": "research", "requirements": ["Not wired yet"]}]}')


def client(captured: list[dict], catalog: object = CATALOG) -> "nodus.Client":
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/rl-environments" and request.method == "GET":
            return httpx.Response(200, json=catalog)
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


def test_environments_and_their_examples_are_typed() -> None:
    environments = client([]).rl.list_environments()
    assert [e.id for e in environments] == ["gsm8k", "listed-only"]
    gsm8k = environments[0]
    assert gsm8k.name == "GSM8K"
    assert gsm8k.modes == ["train", "evaluate"]
    [example] = gsm8k.examples
    assert example.environment_id == "gsm8k"
    assert example.name == "GSM8K, trained"
    assert example.mode == "train"
    assert example.image == "pytorch/pytorch:2.14.0-cuda12.6-cudnn9-runtime"
    assert example.model == "Qwen/Qwen3-1.7B"
    assert example.planned_tasks == 64
    assert example.results == ["outputs/results.json"]
    assert example.baseline_pass_rate == pytest.approx(0.7969)
    assert example.measured_change_pp == pytest.approx(7.8)
    assert example.runtime_minutes == 60
    assert "gsm8k_trainer.py" in example.command


def test_an_environment_with_nothing_runnable_has_no_examples() -> None:
    listed = client([]).rl.list_environments()[1]
    assert listed.examples == []


def test_an_example_supplies_everything_run_needs_and_never_a_budget() -> None:
    [example] = client([]).rl.list_environments()[0].examples
    arguments = example.run_arguments()
    # The SDK never picks a spending number on the caller's behalf.
    assert "budget" not in arguments
    # The same shell wrapping the console applies, so && chains run.
    assert arguments["command"] == ["sh", "-c", example.command]
    assert arguments["image"] == example.image
    assert arguments["outputs"] == {"results.json": "outputs/results.json"}
    assert arguments["rl"] == nodus.RLSetup(
        environment_id="gsm8k", mode="train", model="Qwen/Qwen3-1.7B", planned_tasks=64
    )


def test_running_an_example_sends_what_the_console_would() -> None:
    captured: list[dict] = []
    made = client(captured)
    [example] = made.rl.list_environments()[0].examples
    made.run(**example.run_arguments())
    sent = captured[0]
    assert sent["rl"] == {
        "schema_version": 1,
        "environment_id": "gsm8k",
        "mode": "train",
        "model": "Qwen/Qwen3-1.7B",
        "planned_tasks": 64,
    }
    source = sent["stages"][0]["source"]
    assert source["image"] == example.image
    assert source["command"] == ["sh", "-c", example.command]
    assert sent["stages"][0]["outputs"] == {"results.json": "outputs/results.json"}


def test_a_response_without_an_environments_list_is_an_error() -> None:
    with pytest.raises(nodus.APIError):
        client([], catalog={"schema_version": 1}).rl.list_environments()


def _catalog_with(example_overrides=None, launch_overrides=None):
    catalog = json.loads(json.dumps(CATALOG))
    launch = catalog["environments"][0]["launch"]
    launch.update(launch_overrides or {})
    launch["examples"][0].update(example_overrides or {})
    return catalog


def test_a_launch_without_declared_results_still_lists_its_examples() -> None:
    catalog = _catalog_with()
    del catalog["environments"][0]["launch"]["results"]
    [example] = client([], catalog=catalog).rl.list_environments()[0].examples
    assert example.results == []
    assert example.run_arguments()["outputs"] == {}


def test_output_names_follow_the_console_so_no_result_is_lost() -> None:
    catalog = _catalog_with(launch_overrides={"results": ["a/results.json", "b/results.json", "out/nodus.meta"]})
    [example] = client([], catalog=catalog).rl.list_environments()[0].examples
    assert example.run_arguments()["outputs"] == {
        "results.json": "a/results.json",
        "2_results.json": "b/results.json",
        "result_nodus.meta": "out/nodus.meta",
    }


def test_a_task_count_the_server_would_reject_fails_when_the_catalog_is_read() -> None:
    catalog = _catalog_with(launch_overrides={"default_tasks": 20000})
    with pytest.raises(nodus.APIError):
        client([], catalog=catalog).rl.list_environments()


def test_an_example_whose_runner_url_was_never_resolved_is_not_offered() -> None:
    catalog = _catalog_with(example_overrides={"command": "export B={{RL_EXAMPLE_BASE}} && python3 run.py"})
    assert client([], catalog=catalog).rl.list_environments()[0].examples == []
