"""Discover what can run, then run it, from code.

The fixture is the control plane's own catalog for GSM8K with its runner URL
resolved, the way GET /v1/rl-environments serves it.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import nodus

# The shape GET /v1/rl-environments serves, shared with the documentation tests.
CATALOG = json.loads((Path(__file__).parent / "fixtures" / "rl-environments.json").read_text())


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


def test_every_result_gets_a_name_run_accepts_and_the_upload_step_stores() -> None:
    # Results are stored after the work has run, under 1-64 characters of
    # [a-z0-9._-] with no leading dot or hyphen, so a name that breaks that
    # rule loses a paid run's results. run() also refuses Windows device names
    # and names that differ only in case.
    import re

    from nodus._brief import _validate_outputs

    paths = ["outputs/results.json", "results/Model.bin", "outputs/CON", "outputs/con.json",
             "out/.hidden", "out/-x", "out/final.", "out/NODUS.json", "a/Model.BIN",
             "out/" + "Long" * 20 + ".bin", "out/\u00e9.txt", "outputs/con", "outputs/result_CON"]
    catalog = _catalog_with(launch_overrides={"results": paths})
    example = nodus.RLEnvironment.from_dict(catalog["environments"][0]).examples[0]
    outputs = example.run_arguments()["outputs"]
    assert sorted(outputs.values()) == sorted(paths)
    assert outputs["results.json"] == "outputs/results.json"
    for name in outputs:
        assert re.fullmatch(r"[a-z0-9_][a-z0-9._-]{0,63}", name), name
        assert not name.startswith("nodus."), name
    _validate_outputs(outputs)


RECEIPT = {"workload_id": "wl_example", "status": "accepted", "revision": 1}


def example_client(requests: list[httpx.Request], *, replayed: bool = False) -> "nodus.Client":
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        headers = {"Idempotent-Replayed": "true"} if replayed else {}
        return httpx.Response(202, json=RECEIPT, headers=headers)

    made = nodus.Client(api_key="nk_test", base_url="https://nodus.invalid")
    made._http = httpx.Client(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler),
                              headers={"Authorization": "Bearer nk_test"})
    return made


def test_an_example_carries_its_id_and_the_gpu_memory_it_was_measured_with() -> None:
    [example] = client([]).rl.list_environments()[0].examples
    assert example.id == "gsm8k-trained"
    assert example.peak_memory_gb == 48
    assert example.run_arguments()["peak_memory_gb"] == 48


def test_one_call_starts_an_example_and_returns_its_workload() -> None:
    requests: list[httpx.Request] = []
    workload = example_client(requests).rl.run_example("gsm8k", "gsm8k-trained", idempotency_key="first-gsm8k")
    [sent] = requests
    assert sent.method == "POST"
    assert sent.url.path == "/v1/rl-environments/gsm8k/examples/gsm8k-trained/runs"
    assert sent.headers["Idempotency-Key"] == "first-gsm8k"
    assert json.loads(sent.content or b"{}") == {}
    assert workload.id == "wl_example"
    assert workload.replayed is False


def test_a_named_example_run_sends_only_the_name() -> None:
    requests: list[httpx.Request] = []
    example_client(requests, replayed=True).rl.run_example(
        "gsm8k", "gsm8k-trained", idempotency_key="named", name="my baseline")
    assert json.loads(requests[0].content) == {"name": "my baseline"}


@pytest.mark.parametrize("environment,example,key", [
    ("../workloads", "gsm8k-trained", "k"),
    ("gsm8k", "GSM8K", "k"),
    ("gsm8k", "a/b", "k"),
    ("gsm8k", "gsm8k-trained", ""),
])
def test_an_example_request_that_cannot_be_right_is_refused_before_sending(environment, example, key) -> None:
    requests: list[httpx.Request] = []
    with pytest.raises(nodus.ValidationError):
        example_client(requests).rl.run_example(environment, example, idempotency_key=key)
    assert requests == []


def test_the_async_client_starts_an_example_the_same_way() -> None:
    import asyncio

    requests: list[httpx.Request] = []

    async def go():
        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(202, json=RECEIPT, headers={"Idempotent-Replayed": "true"})

        made = nodus.AsyncClient(api_key="nk_test", base_url="https://nodus.invalid")
        made._http = httpx.AsyncClient(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler),
                                       headers={"Authorization": "Bearer nk_test"})
        return await made.rl.run_example("reasoning-gym", "chain-sum", idempotency_key="k")

    workload = asyncio.run(go())
    assert requests[0].url.path == "/v1/rl-environments/reasoning-gym/examples/chain-sum/runs"
    assert workload.id == "wl_example"
    assert workload.replayed is True
