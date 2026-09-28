"""Customer journeys for the first-class sandbox API."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

import nodus


def test_public_sdk_exposes_agent_sandboxes_without_devbox_constructor():
    assert not hasattr(nodus, "Devbox")
    assert "Devbox" not in nodus.__all__
    assert nodus.Sandbox and nodus.Sandboxes and nodus.AsyncSandboxes


@pytest.mark.parametrize("entrypoint", ["sync", "async", "constructor"])
def test_profile_creation_is_rejected_before_http(entrypoint, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(202, json=SANDBOX)

    async def run():
        async with nodus.AsyncClient(api_key="test", base_url="https://nodus.invalid") as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler))
            with pytest.raises(TypeError, match="profile"):
                await client.sandboxes.create(image="customer:qualified", profile="devbox")

    if entrypoint == "async":
        asyncio.run(run())
    else:
        with sync_client(handler) as client:
            monkeypatch.setattr(nodus, "Client", lambda: client)
            with pytest.raises(TypeError, match="profile"):
                if entrypoint == "constructor":
                    nodus.Sandbox(image="customer:qualified", profile="devbox")
                else:
                    client.sandboxes.create(image="customer:qualified", profile="devbox")
    assert calls == []


SANDBOX = {
    "id": "sb_agent",
    "state": "ready",
    "envelope": {
        "name": "agent-session",
        "source": {"image": "python:3.12"},
        "requirements": {
            "compute_class": "accelerator",
            "gpu": "L40S",
            "vcpus": 4,
            "peak_memory_gb": 32,
            "disk_gb": 50,
        },
        "outcome": {"max_cost_usd": 3},
        "policy": {"network": "allowlist", "egress_allow": ["api.example.com"]},
        "lifecycle": {"idle_timeout_s": 300, "max_lifetime_s": 3600, "on_idle": "terminate"},
        "reservation": {"wake": "on_demand", "max_idle_s": 300, "min_ready": 0, "max_concurrent": 1, "release": "idle"},
        "continuity": {"mode": "ephemeral"},
    },
    "created_at": "2026-09-13T12:00:00Z",
    "updated_at": "2026-09-13T12:00:01Z",
    "last_activity_at": "2026-09-13T12:00:01Z",
    "terminal_at": None,
    "cost_usd": 0.0,
    "url": "https://console.nodus-compute.ai/sandboxes/sb_agent",
}

EXEC = {
    "id": "sx_python",
    "sandbox_id": "sb_agent",
    "spec": {
        "command": ["python", "-c", "print('hello')"],
        "cwd": "/workspace",
        "env": {"MODE": "live"},
        "timeout_s": 120,
        "stdin": True,
    },
    "state": "running",
    "created_at": "2026-09-13T12:00:02Z",
    "updated_at": "2026-09-13T12:00:03Z",
    "dispatched_at": "2026-09-13T12:00:02Z",
    "deadline_at": "2026-09-13T12:02:02Z",
    "started_at": "2026-09-13T12:00:03Z",
    "completed_at": None,
    "exit_code": None,
    "failure_code": "",
    "output_sequence": 0,
    "stdout_bytes": 0,
    "stderr_bytes": 0,
    "final_output_sequence": None,
    "cancel_requested_at": None,
    "cancel_reason": "",
}


def sync_client(handler) -> nodus.Client:
    client = nodus.Client(api_key="nk_live_test", base_url="https://nodus.invalid")
    client._http = httpx.Client(
        base_url="https://nodus.invalid",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer nk_live_test"},
    )
    return client


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
def test_existing_profile_handle_preserves_identity_and_cleanup(async_mode):
    legacy = {**SANDBOX, "envelope": {**SANDBOX["envelope"], "profile": "devbox"}}
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            assert request.url.path == "/v1/sandboxes/sb_agent"
            return httpx.Response(200, json=legacy)
        assert request.method == "POST" and request.url.path == "/v1/sandboxes/sb_agent/terminate"
        assert request.headers["Idempotency-Key"] == "cleanup-existing"
        return httpx.Response(200, json={**legacy, "state": "terminated"})

    async def run():
        async with nodus.AsyncClient(api_key="test", base_url="https://nodus.invalid") as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler))
            box = await client.sandboxes.from_id("sb_agent")
            assert box.envelope == legacy["envelope"]
            assert (await box.terminate(idempotency_key="cleanup-existing")).is_terminal

    if async_mode:
        asyncio.run(run())
    else:
        with sync_client(handler) as client:
            box = client.sandboxes.from_id("sb_agent")
            assert box.envelope == legacy["envelope"]
            assert box.terminate(idempotency_key="cleanup-existing").is_terminal
    assert calls == [("GET", "/v1/sandboxes/sb_agent"), ("POST", "/v1/sandboxes/sb_agent/terminate")]


def test_create_exec_stream_stdin_wait_and_terminate_journey():
    calls = []
    exec_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal exec_reads
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, dict(request.url.params), body, request.headers.get("Idempotency-Key")))
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            return httpx.Response(202, json=SANDBOX)
        if request.method == "POST" and request.url.path == "/v1/sandboxes/sb_agent/exec":
            return httpx.Response(202, json=EXEC)
        if request.method == "POST" and request.url.path.endswith("/stdin"):
            return httpx.Response(202, json={"sequence": 1, "bytes": 6, "eof": True, "created_at": "2026-09-13T12:00:04Z"})
        if request.method == "GET" and request.url.path.endswith("/stream"):
            return httpx.Response(200, json={
                "frames": [
                    {"sequence": 1, "stream": "stdout", "offset": 0, "data": base64.b64encode(b"hello\n").decode(), "created_at": "2026-09-13T12:00:05Z"},
                    {"sequence": 2, "stream": "stderr", "offset": 0, "data": base64.b64encode(b"trace\n").decode(), "created_at": "2026-09-13T12:00:05Z"},
                ],
                "next_sequence": 2,
                "last_sequence": 2,
                "final_sequence": 2,
                "state": "completed",
                "done": True,
                "complete": True,
            })
        if request.method == "GET" and request.url.path.endswith("/execs/sx_python"):
            exec_reads += 1
            return httpx.Response(200, json={**EXEC, "state": "completed", "exit_code": 0, "final_output_sequence": 2})
        if request.method == "POST" and request.url.path.endswith("/terminate"):
            return httpx.Response(200, json={**SANDBOX, "state": "terminated"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    with sync_client(handler) as client:
        sandbox = client.sandboxes.create(
            image="python:3.12",
            name="agent-session",
            budget=3,
            requirements={"compute_class": "accelerator", "gpu": "L40S", "vcpus": 4, "peak_memory_gb": 32, "disk_gb": 50},
            policy={"network": "allowlist", "egress_allow": ["api.example.com"]},
            lifecycle={"idle_timeout_s": 300, "max_lifetime_s": 3600, "on_idle": "terminate"},
            idempotency_key="create-agent-session",
        )
        assert sandbox.state == nodus.SandboxState.READY
        process = sandbox.exec(
            ["python", "-c", "print('hello')"],
            cwd="/workspace",
            env={"MODE": "live"},
            timeout_seconds=120,
            stdin=True,
            idempotency_key="exec-agent-step",
        )
        receipt = process.write("input\n", eof=True, idempotency_key="stdin-agent-step")
        frames = list(process.iter_output())
        done = process.wait(timeout_seconds=5)
        stopped = sandbox.terminate(idempotency_key="terminate-agent-session")

    assert sandbox.id == "sb_agent"
    assert sandbox.cost_usd == 0
    assert process.id == "sx_python"
    assert process.command == ["python", "-c", "print('hello')"]
    assert receipt.bytes == 6 and receipt.eof is True
    assert [frame.data for frame in frames] == [b"hello\n", b"trace\n"]
    assert [frame.stream for frame in frames] == ["stdout", "stderr"]
    assert done.exit_code == 0 and done.succeeded
    assert stopped is sandbox and sandbox.state == nodus.SandboxState.TERMINATED
    assert exec_reads == 1

    create = calls[0]
    assert create[3]["outcome"] == {"max_cost_usd": 3}
    assert create[3]["requirements"]["compute_class"] == "accelerator"
    assert "supplier" not in json.dumps(create[3]).lower()
    assert create[4] == "create-agent-session"
    exec_call = calls[1]
    assert exec_call[3]["command"] == ["python", "-c", "print('hello')"]
    assert exec_call[4] == "exec-agent-step"
    stdin_call = calls[2]
    assert base64.b64decode(stdin_call[3]["data"]) == b"input\n"
    assert stdin_call[3]["eof"] is True
    assert stdin_call[4] == "stdin-agent-step"


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(("options", "expected"), [
    ({}, {}),
    ({"requirements": {"vcpus": 2, "peak_memory_gb": 4}}, {"vcpus": 2, "peak_memory_gb": 4}),
    ({"requirements": {"gpu": "L40S"}}, {"gpu": "L40S", "compute_class": "accelerator"}),
    ({"requirements": {"gpu_count": 2}}, {"gpu_count": 2, "compute_class": "accelerator"}),
    ({"requirements": {"gpu_count": 0}}, {"gpu_count": 0, "compute_class": "accelerator"}),
    ({"requirements": {"gpu_interconnect": "nvlink"}}, {"gpu_interconnect": "nvlink", "compute_class": "accelerator"}),
    ({"requirements": {"gpu_count": None}}, {"gpu_count": None}),
    ({"requirements": {"compute_class": "vm"}}, {"compute_class": "vm"}),
    ({"requirements": {"compute_class": "accelerator"}}, {"compute_class": "accelerator"}),
])
def test_create_compute_requirements_preserve_caller_intent(async_mode, options, expected):
    original = json.loads(json.dumps(options))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        assert body["requirements"] == expected
        assert body["outcome"] == {"max_cost_usd": 3}
        assert request.headers["Idempotency-Key"] == "compute-intent"
        return httpx.Response(202, json=SANDBOX)

    async def run():
        client = nodus.AsyncClient(api_key="nk_live_test", base_url="https://nodus.invalid")
        client._http = httpx.AsyncClient(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler))
        async with client:
            await client.sandboxes.create(image="python:3.12", budget=3, idempotency_key="compute-intent", **options)

    if async_mode:
        asyncio.run(run())
    else:
        with sync_client(handler) as client:
            client.sandboxes.create(image="python:3.12", budget=3, idempotency_key="compute-intent", **options)
    assert len(calls) == 1
    assert options == original


def test_top_level_sandbox_is_get_or_create_and_context_managed(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            return httpx.Response(202, json=SANDBOX)
        if request.method == "POST" and request.url.path.endswith("/exec"):
            return httpx.Response(202, json=EXEC)
        if request.method == "POST" and request.url.path.endswith("/terminate"):
            return httpx.Response(200, json={**SANDBOX, "state": "terminated"})
        raise AssertionError(request.url)

    client = sync_client(handler)
    monkeypatch.setattr(nodus, "Client", lambda: client)

    with nodus.Sandbox(name="agent-session", image="python:3.12", budget=3) as sandbox:
        process = sandbox.exec("printf 'hello\\n'")

    assert process.id == "sx_python"
    assert sandbox.state == nodus.SandboxState.TERMINATED
    assert calls[0][2]["name"] == "agent-session"
    assert calls[1][2]["command"] == ["/bin/sh", "-lc", "printf 'hello\\n'"]
    assert calls[2][:2] == ("POST", "/v1/sandboxes/sb_agent/terminate")


def test_top_level_sandbox_can_reattach_by_name_without_an_image(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"name": "agent-session", "requirements": {}}
        return httpx.Response(202, json=SANDBOX)

    client = sync_client(handler)
    monkeypatch.setattr(nodus, "Client", lambda: client)
    sandbox = nodus.Sandbox(name="agent-session")
    assert sandbox.id == "sb_agent"
    client.close()


def test_top_level_sandbox_requires_an_image_or_name():
    try:
        nodus.Sandbox()
    except nodus.ValidationError as error:
        assert "image or name" in str(error)
    else:
        raise AssertionError("an unaddressable sandbox was created")


def test_reconnect_list_and_non_following_output_page():
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/v1/sandboxes":
            return httpx.Response(200, json={"sandboxes": [SANDBOX], "next_cursor": "next-page"})
        if request.url.path == "/v1/sandboxes/sb_agent":
            return httpx.Response(200, json=SANDBOX)
        if request.url.path.endswith("/stream"):
            return httpx.Response(200, json={"frames": [], "next_sequence": 7, "last_sequence": 7, "final_sequence": None, "state": "running", "done": False, "complete": False})
        raise AssertionError(request.url)

    with sync_client(handler) as client:
        rows, cursor = client.sandboxes.list_page(limit=25, name="agent-session", state="ready")
        same = client.sandboxes.from_id("sb_agent")
        execution = nodus.SandboxExec(client, "sb_agent")
        execution._absorb(EXEC)
        page = execution.output(after=6, limit=4, wait=False)

    assert rows[0].id == same.id == "sb_agent"
    assert cursor == "next-page"
    assert page.next_sequence == 7 and not page.done
    assert paths == [
        ("/v1/sandboxes", {"limit": "25", "name": "agent-session", "status": "ready"}),
        ("/v1/sandboxes/sb_agent", {}),
        ("/v1/sandboxes/sb_agent/execs/sx_python/stream", {"after": "6", "limit": "4", "wait": "false"}),
    ]


def test_async_sandbox_journey_matches_sync_surface():
    calls = []

    async def go():
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.method == "POST" and request.url.path == "/v1/sandboxes":
                return httpx.Response(202, json=SANDBOX)
            if request.method == "POST" and request.url.path.endswith("/exec"):
                return httpx.Response(202, json=EXEC)
            if request.method == "GET" and request.url.path.endswith("/stream"):
                return httpx.Response(200, json={"frames": [], "next_sequence": 0, "last_sequence": 0, "final_sequence": 0, "state": "completed", "done": True, "complete": True})
            if request.method == "GET" and request.url.path.endswith("/execs/sx_python"):
                return httpx.Response(200, json={**EXEC, "state": "completed", "exit_code": 0})
            if request.method == "POST" and request.url.path.endswith("/terminate"):
                return httpx.Response(200, json={**SANDBOX, "state": "terminated"})
            raise AssertionError(request.url)

        client = nodus.AsyncClient(api_key="nk_live_test", base_url="https://nodus.invalid")
        client._http = httpx.AsyncClient(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler))
        async with client:
            sandbox = await client.sandboxes.create(image="python:3.12", budget=3)
            process = await sandbox.exec(["python", "-c", "print('hello')"])
            assert [frame async for frame in process.iter_output()] == []
            await process.wait(timeout_seconds=5)
            await sandbox.terminate()
            return sandbox, process

    sandbox, process = asyncio.run(go())
    assert sandbox.state == nodus.SandboxState.TERMINATED
    assert process.succeeded
    assert calls == [
        ("POST", "/v1/sandboxes"),
        ("POST", "/v1/sandboxes/sb_agent/exec"),
        ("GET", "/v1/sandboxes/sb_agent/execs/sx_python/stream"),
        ("GET", "/v1/sandboxes/sb_agent/execs/sx_python"),
        ("POST", "/v1/sandboxes/sb_agent/terminate"),
    ]


def test_sandbox_error_uses_the_public_code_and_fix():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={
            "code": "payment_method_required",
            "message": "A payment method is required.",
            "fix": "Add a payment method in Billing before creating a sandbox.",
            "url": "https://nodus-compute.ai/docs/",
        })

    with sync_client(handler) as client:
        try:
            client.sandboxes.create(image="python:3.12", budget=2)
        except nodus.BudgetExceededError as error:
            assert "Add a payment method in Billing before creating a sandbox." in str(error)
            assert error.payload["code"] == "payment_method_required"
        else:
            raise AssertionError("the rejected sandbox was reported as accepted")


def test_failure_guidance_is_loaded_and_cleared_on_refresh():
    failure = {"code": "no_capable_capacity", "message": "No compatible capacity became available before the placement deadline.", "fix": "Retry later or reduce the requested resources."}
    responses = iter([dict(SANDBOX, state="failed", failure=failure), dict(SANDBOX, state="terminated")])
    with sync_client(lambda request: httpx.Response(200, json=next(responses))) as client:
        box = client.sandboxes.from_id("sb_agent")
        assert box.failure == failure
        box.refresh()
        assert box.failure is None


def test_async_failure_guidance_uses_the_server_fields():
    failure = {"code": "boot_timeout", "message": "The sandbox did not become ready before its boot deadline.", "fix": "Check the image and resource requirements, then create a new sandbox."}
    async def run():
        client = nodus.AsyncClient(api_key="nk_live_test", base_url="https://nodus.invalid")
        client._http = httpx.AsyncClient(base_url="https://nodus.invalid", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=dict(SANDBOX, state="failed", failure=failure))))
        async with client:
            box = await client.sandboxes.from_id("sb_agent")
            assert box.failure == failure
    asyncio.run(run())


def test_network_usage_and_denied_events_survive_refresh():
    usage = {"sent_bytes": 123, "received_bytes": 456}
    event = {"id": 72, "event_id": "denied-event", "event_type": "sandbox.egress_denied", "payload": {"hostname": "example.com", "count": 2, "generation": 1}}
    def handler(request):
        if request.url.path.endswith("/events"):
            assert request.url.params["after"] == "71"
            return httpx.Response(200, json={"events": [event]})
        return httpx.Response(200, json=dict(SANDBOX, network_usage=usage))
    with sync_client(handler) as client:
        box = client.sandboxes.from_id("sb_agent")
        assert box.network_usage == usage
        box.network_usage["sent_bytes"] = 0
        box.refresh()
        assert box.network_usage == usage
        events = box.events(after=71)
        assert len(events) == 1
        assert events[0].seq == 72
        assert events[0].type == "sandbox.egress_denied"
        assert events[0].payload == event["payload"]


def test_async_network_usage_and_events_preserve_legacy_responses():
    usage = {"sent_bytes": 9007199254740993, "received_bytes": 27}
    responses = iter([dict(SANDBOX, network_usage=usage), dict(SANDBOX), {"events": None}])
    async def run():
        client = nodus.AsyncClient(api_key="nk_live_test", base_url="https://nodus.invalid")
        client._http = httpx.AsyncClient(base_url="https://nodus.invalid", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=next(responses))))
        async with client:
            box = await client.sandboxes.from_id("sb_agent")
            assert box.network_usage == usage
            await box.refresh()
            assert box.network_usage is None
            assert await box.events() == []
    asyncio.run(run())

@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
def test_sandbox_charge_finality_preserves_recorded_cost_and_fixed_lifetime_total(async_mode):
    responses = [
        {**SANDBOX, "state": "terminated", "cost_usd": 0, "charge_state": "estimated"},
        {**SANDBOX, "state": "terminated", "cost_usd": 0.20, "charge_state": "final", "final_charge_usd": 1.25},
        SANDBOX,
    ]

    def handler(request):
        return httpx.Response(200, json=responses.pop(0))

    def check(box, expected_state, recorded, final):
        assert box.charge_state == expected_state
        assert box.cost_usd == recorded
        assert box.final_charge_usd == final

    async def run():
        async with nodus.AsyncClient(api_key="test", base_url="https://nodus.invalid") as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler))
            box = await client.sandboxes.from_id("sb_agent")
            check(box, "estimated", 0, None)
            await box.refresh()
            check(box, "final", 0.20, 1.25)
            await box.refresh()
            check(box, "", 0, None)

    if async_mode:
        asyncio.run(run())
    else:
        with sync_client(handler) as client:
            box = client.sandboxes.from_id("sb_agent")
            check(box, "estimated", 0, None)
            box.refresh()
            check(box, "final", 0.20, 1.25)
            box.refresh()
            check(box, "", 0, None)

@pytest.mark.parametrize("state,final", [("estimated", None), ("final", 1.25)])
def test_sandbox_constructor_replay_preserves_wire_charge_finality(state, final, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST" and request.url.path == "/v1/sandboxes"
        assert request.headers["Idempotency-Key"] == "same-admission"
        body = {**SANDBOX, "state": "creating", "cost_usd": 0,
                "charge_state": state}
        if final is not None:
            body["final_charge_usd"] = final
        return httpx.Response(202, json=body, headers={"Idempotent-Replayed": "true"})

    client = sync_client(handler)
    monkeypatch.setattr(nodus, "Client", lambda: client)
    box = nodus.Sandbox(image="python:3.12", idempotency_key="same-admission")
    try:
        assert box.charge_state == state
        assert box.final_charge_usd == final
        assert box.cost_usd == 0
        assert box.replayed
        assert len(calls) == 1
    finally:
        box.close()
