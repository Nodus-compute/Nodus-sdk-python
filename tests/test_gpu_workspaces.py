"""GPU workspace journeys through client.workspaces, on the research workspace wire."""

from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest

import nodus
from nodus import AsyncWorkspace, Workspace
from nodus.errors import APIError, NotFoundError, ValidationError, WorkspaceNotReadyError

BASE = "/v1/research-workspaces"
REVISION = "a" * 64
CONFIGURATION = {"name": "kernel-lab", "environment": "pytorch-cuda", "editor": "vscode", "gpu": "H100",
                 "gpu_count": 1, "gpu_memory_gb": 80, "budget_usd": 0, "max_hours": 4, "size_gb": 10}
STOPPED = {
    "id": "ws_kernel", "expired_at": None, "cleanup_pending": False, "name": "kernel-lab", "size_gb": 10,
    "holder_id": None, "saved_at": None, "stored_bytes": None, "last_error": "", "saving_for_termination": False,
    "billing_status": "metered_subject_to_account_limits", "configuration": CONFIGURATION,
    "configuration_revision": REVISION, "storage_revision": 3, "storage_policy_version": "r2-standard-10gb-account-v1",
    "session": None, "meter": None, "state": "stopped",
    "status_message": "Saved project files are ready for the next session.",
    "connections": {"editor": False, "notebook": False, "ssh": False},
    "storage": {"retained_bytes": 0, "billable_bytes": 0}, "pending_upload": None,
}
SESSION = {"id": "sb_session", "state": "ready", "created_at": "2026-09-27T10:00:00Z"}
CREATING = {**STOPPED, "state": "creating", "session": {**SESSION, "state": "creating"},
            "status_message": "Finding compute and starting your tools."}
METER = {"compute_settled_usd": 0, "platform_fee_settled_usd": 0, "subscription_settled_usd": 0,
         "storage_settled_usd": 0, "model_settled_usd": 0, "compute_accruing_usd": 0.4,
         "platform_fee_accruing_usd": 0.1, "settled_usd": 0, "accruing_usd": 0.5,
         "accruing_rate_usd_hour": 2.5, "total_now_usd": 0.5, "as_of": "2026-09-27T10:12:00Z"}
RUNNING = {**STOPPED, "state": "running", "session": SESSION, "meter": METER,
           "connections": {"editor": True, "notebook": True, "ssh": True},
           "status_message": "Compute is running. Closing this page does not stop it."}
STOPPING = {**RUNNING, "state": "stopping", "connections": {"editor": False, "notebook": False, "ssh": False},
            "status_message": "Saving project files and releasing compute."}
SSH = {"transport": "tunnel", "host": "ws-abc-2222.nodus.run", "port": "22", "user": "nodus",
       "requires": "cloudflared",
       "command": "ssh -o ProxyCommand='cloudflared access ssh --hostname %h' nodus@ws-abc-2222.nodus.run",
       "vscode_url": "vscode://vscode-remote/ssh-remote+nodus@ws-abc-2222.nodus.run/workspace",
       "ssh_config": "Host nodus-kernel-lab\n  HostName ws-abc-2222.nodus.run\n  User nodus\n"
                     "  ProxyCommand cloudflared access ssh --hostname %h\n"}
RECEIPT = {"id": "wl_train", "status": "queued", "spend_usd": 0, "revision": 1}
CAPABILITIES = {"available": True, "storage_limit_bytes": 10_000_000_000, "environments": ["pytorch-cuda"],
                "gpu_counts": [1, 2, 4, 8], "editors": ["vscode", "jupyter", "ssh"],
                "storage_policy_version": "r2-standard-10gb-account-v1"}


def sync_client(handler) -> nodus.Client:
    client = nodus.Client(api_key="nk_live_test", base_url="https://nodus.invalid", max_retries=0)
    client._http.close()
    client._http = httpx.Client(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler),
                                headers={"Authorization": "Bearer nk_live_test"})
    return client


def async_client(handler) -> nodus.AsyncClient:
    client = nodus.AsyncClient(api_key="nk_live_test", base_url="https://nodus.invalid", max_retries=0)
    client._http = httpx.AsyncClient(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler),
                                     headers={"Authorization": "Bearer nk_live_test"})
    return client


def run(async_mode, scenario):
    """The scenario body awaits through call(), so both flavours run under one loop."""
    return asyncio.run(scenario())


def call(value):
    """Await when the value came from the asynchronous client."""
    if asyncio.iscoroutine(value):
        return value
    async def ready():
        return value
    return ready()


class Server:
    """Records requests and answers from a script keyed by method and path."""

    def __init__(self, answers):
        self.answers, self.requests = answers, []

    def __call__(self, request):
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body, dict(request.headers)))
        answer = self.answers[(request.method, request.url.path)]
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        status, payload = answer
        return httpx.Response(status, json=payload)


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
def test_create_start_wait_connect_run_and_stop(async_mode):
    server = Server({
        ("GET", BASE + "/capabilities"): (200, CAPABILITIES),
        ("POST", BASE): (201, STOPPED),
        ("POST", BASE + "/ws_kernel/start"): (202, CREATING),
        ("GET", BASE + "/ws_kernel"): [(200, CREATING), (200, RUNNING), (200, RUNNING), (200, RUNNING)],
        ("POST", BASE + "/ws_kernel/connections"): lambda r: (200, SSH if json.loads(r.content)["tool"] == "ssh"
                                                             else {"url": "https://ws-abc-8080.nodus.run/?tkn=t"}),
        ("POST", BASE + "/ws_kernel/workloads"): (202, RECEIPT),
        ("POST", BASE + "/ws_kernel/stop"): (202, STOPPING),
    })

    async def scenario():
        client = async_client(server) if async_mode else sync_client(server)
        with_client = client if not async_mode else None
        async with (client if async_mode else _sync_ctx(client)):
            ws = await call(client.workspaces.create("kernel-lab", gpu="H100", max_hours=4))
            assert isinstance(ws, AsyncWorkspace if async_mode else Workspace)
            assert (ws.id, ws.name, ws.state, ws.storage_revision) == ("ws_kernel", "kernel-lab", "stopped", 3)
            assert ws.session_id is None and ws.connections == {"editor": False, "notebook": False, "ssh": False}
            await call(ws.start(idempotency_key="session-1"))
            assert ws.state == "creating" and ws.session_id == "sb_session"
            await call(ws.wait_until_ready(poll_seconds=0.1, timeout_seconds=5))
            assert ws.state == "running" and ws.connections["editor"] is True and ws.cost_usd == 0.5
            editor = await call(ws.connect("editor"))
            assert editor == {"url": "https://ws-abc-8080.nodus.run/?tkn=t"}
            ssh = await call(ws.ssh())
            assert ssh["command"].startswith("ssh ") and ssh["vscode_url"].startswith("vscode://")
            job = await call(ws.run("python train.py", budget_usd=5, idempotency_key="train-1"))
            assert job.id == "wl_train" and job.status == "queued"
            await call(ws.stop(idempotency_key="stop-1"))
            assert ws.state == "stopping"
        return with_client

    run(async_mode, scenario)
    methods = [(method, path) for method, path, _, _ in server.requests]
    assert methods == [
        ("GET", BASE + "/capabilities"), ("POST", BASE), ("POST", BASE + "/ws_kernel/start"),
        ("GET", BASE + "/ws_kernel"), ("GET", BASE + "/ws_kernel"), ("POST", BASE + "/ws_kernel/connections"),
        ("POST", BASE + "/ws_kernel/connections"), ("POST", BASE + "/ws_kernel/workloads"),
        ("GET", BASE + "/ws_kernel"), ("POST", BASE + "/ws_kernel/stop"),
    ]
    created = server.requests[1][2]
    assert created == {"name": "kernel-lab", "environment": "pytorch-cuda", "editor": "vscode", "gpu": "H100",
                       "gpu_count": 1, "gpu_memory_gb": 80, "max_hours": 4, "size_gb": 10}
    start_headers, stop_headers = server.requests[2][3], server.requests[9][3]
    assert start_headers["idempotency-key"] == "session-1" and stop_headers["idempotency-key"] == "stop-1"
    assert server.requests[9][2] == {"session_id": "sb_session"}
    assert server.requests[5][2] == {"tool": "editor"} and server.requests[6][2] == {"tool": "ssh"}
    submitted = server.requests[7]
    assert submitted[2] == {"command": "python train.py", "budget_usd": 5}
    assert submitted[3]["idempotency-key"] == "train-1"


class _sync_ctx:
    """Lets one scenario body use async with for both client flavours."""

    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client.__enter__()

    async def __aexit__(self, *exc):
        return self.client.__exit__(*exc)


def test_create_defaults_come_from_the_server_and_the_console_catalog():
    server = Server({("GET", BASE + "/capabilities"): (200, {**CAPABILITIES, "storage_limit_bytes": 20 * 1024 ** 3,
                                                                "storage_policy_version": "included-20gib-v0"}),
                     ("POST", BASE): (201, STOPPED)})
    with sync_client(server) as client:
        client.workspaces.create("rocm-lab", gpu="MI300X", gpu_memory_gb=192, gpu_count=2, editor="jupyter", max_hours=1)
        client.workspaces.create("ada-lab", gpu="nvidia l40s", max_hours=1, size_gb=5)
    body = server.requests[1][2]
    assert body["environment"] == "pytorch-rocm" and body["gpu_memory_gb"] == 192 and body["size_gb"] == 20
    assert body["gpu_count"] == 2 and body["editor"] == "jupyter"
    assert server.requests[-1][2]["gpu_memory_gb"] == 48 and server.requests[-1][2]["environment"] == "pytorch-cuda"


def test_private_keys_and_mixed_cpu_gpu_requests_never_leave_the_process():
    def handler(_):
        pytest.fail("a refused configuration must not reach the API")
    private = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"
    with sync_client(handler) as client:
        putty = "PuTTY-User-Key-File-3: ssh-ed25519\nPrivate-Lines: 1\nAAAA\n"
        for bad in (private, putty, "AAAAC3NzaC1lZDI1NTE5", "ssh-ed25519 AAAA\nPrivate-Lines: 1\n"):
            with pytest.raises(ValidationError, match="public keys"):
                client.workspaces.create("lab", gpu="H100", max_hours=1, size_gb=1, ssh_key=bad)
            with pytest.raises(ValidationError, match="public keys"):
                Workspace(client, "ws_kernel")._configure_body({"ssh_authorized_key": bad})
        good = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGx researcher\necdsa-sha2-nistp256 AAAAE2Vj other\n"
        with pytest.raises(APIError, match="revision"):
            Workspace(client, "ws_kernel")._configure_body({"ssh_authorized_key": good})
        with pytest.raises(ValidationError, match="cpus and memory_gb"):
            client.workspaces.create("lab", cpus=4, memory_gb=8, gpu_count=8, max_hours=1, size_gb=1)
        with pytest.raises(ValidationError, match="form_factor"):
            client.workspaces.create("lab", gpu="H100", form_factor="mezzanine", max_hours=1, size_gb=1)


def test_final_failures_do_not_advise_a_retry_with_the_same_key():
    server = Server({("POST", BASE + "/ws_kernel/workloads"): [(409, {"error": "idempotency_conflict"}),
                                                                (402, {"error": "insufficient_funds"})]})
    with sync_client(server) as client:
        ws = Workspace(client, "ws_kernel")
        for _ in range(2):
            with pytest.raises(nodus.NodusError) as raised:
                ws.run("python x.py", budget_usd=1)
            assert "idempotency_key" not in str(raised.value)
            assert "idempotency_key" not in (raised.value.body or {})


def test_configure_clears_optional_fields_with_none_and_keeps_the_name_rule():
    configured = {**CONFIGURATION, "ref": "main", "repository": "org/repo", "ssh_authorized_key": "ssh-ed25519 AAAA"}
    server = Server({("GET", BASE + "/ws_kernel"): (200, {**STOPPED, "configuration": configured}),
                     ("PATCH", BASE + "/ws_kernel"): (200, STOPPED)})
    with sync_client(server) as client:
        ws = client.workspaces.get("ws_kernel")
        ws.configure(ref=None, ssh_authorized_key=None)
        with pytest.raises(ValidationError, match="name"):
            ws.configure(name="n" * 300)
        with pytest.raises(ValidationError, match="cleared"):
            ws.configure(gpu=None)
    assert server.requests[-1][2]["configuration"] == {**CONFIGURATION, "repository": "org/repo"}


def test_uncertain_paid_requests_carry_their_key_for_the_retry():
    server = Server({("POST", BASE + "/ws_kernel/workloads"): [(504, {"error": "gateway_timeout"}), (202, {"status": "queued"})],
                     ("GET", BASE + "/ws_kernel"): (200, RUNNING),
                     ("POST", BASE + "/ws_kernel/stop"): (502, {"error": "bad_gateway"})})
    with sync_client(server) as client:
        ws = Workspace(client, "ws_kernel")
        for call in (lambda: ws.run("python x.py", budget_usd=1), lambda: ws.run("python x.py", budget_usd=1),
                     lambda: ws.stop()):
            with pytest.raises(nodus.NodusError) as raised:
                call()
            key = raised.value.body["idempotency_key"]
            assert key.startswith("nodus-") and key in str(raised.value)
            sent = server.requests[-1][3]["idempotency-key"]
            assert sent == key


def test_stop_always_reads_the_current_session_before_acting():
    newer = {**RUNNING, "session": {**SESSION, "id": "sb_newer"}}
    server = Server({("GET", BASE + "/ws_kernel"): [(200, RUNNING), (200, newer)],
                     ("POST", BASE + "/ws_kernel/stop"): (202, STOPPING)})
    with sync_client(server) as client:
        ws = client.workspaces.get("ws_kernel")
        assert ws.session_id == "sb_session"
        ws.stop(idempotency_key="stop-9")
    assert server.requests[-1][2] == {"session_id": "sb_newer"}


def test_configure_applies_the_creation_rules_to_each_change():
    server = Server({("GET", BASE + "/ws_kernel"): (200, STOPPED)})
    with sync_client(server) as client:
        ws = client.workspaces.get("ws_kernel")
        for changes in ({"budget_usd": None}, {"budget_usd": -5}, {"budget_usd": "lots"}, {"gpu_count": 3},
                        {"max_hours": 0}, {"editor": "emacs"}, {"ssh_authorized_key": ""}):
            with pytest.raises(ValidationError):
                ws.configure(**changes)
    assert [m for m, *_ in server.requests] == ["GET"]


def test_get_by_name_accepts_dots_and_wait_loops_refuse_unbounded_arguments():
    dotted = {**STOPPED, "id": "ws_dot", "name": "kernel.lab"}
    server = Server({("GET", BASE): (200, {"workspaces": [dotted], "next_cursor": ""}),
                     ("GET", BASE + "/ws_dot"): (200, dotted)})
    with sync_client(server) as client:
        assert client.workspaces.get("kernel.lab").id == "ws_dot"
        ws = client.workspaces.get("ws_dot")
        for kwargs in ({"poll_seconds": -1}, {"poll_seconds": 0}, {"timeout_seconds": float("nan")},
                       {"poll_seconds": float("inf")}):
            with pytest.raises(ValidationError):
                ws.wait_until_ready(**kwargs)
            with pytest.raises(ValidationError):
                ws.wait_until_stopped(**kwargs)
    assert [(m, p) for m, p, *_ in server.requests] == [("GET", BASE), ("GET", BASE + "/ws_dot")]


def test_wait_until_stopped_reports_a_failed_session_instead_of_waiting():
    failed = {**STOPPED, "state": "failed", "status_message": "The compute session could not continue."}
    server = Server({("GET", BASE + "/ws_kernel"): (200, failed)})
    with sync_client(server) as client:
        with pytest.raises(WorkspaceNotReadyError, match="could not continue"):
            Workspace(client, "ws_kernel").wait_until_stopped(poll_seconds=0.1, timeout_seconds=5)
    assert len(server.requests) == 1


def test_create_refuses_to_guess_memory_for_unknown_hardware_and_money():
    def handler(_):
        pytest.fail("a refused configuration must not reach the API")
    with sync_client(handler) as client:
        with pytest.raises(ValidationError, match="gpu_memory_gb"):
            client.workspaces.create("lab", gpu="Z9000", max_hours=1, size_gb=10)
        with pytest.raises(ValidationError, match="max_hours"):
            client.workspaces.create("lab", gpu="H100", size_gb=10)
        with pytest.raises(ValidationError, match="budget_usd"):
            client.workspaces.create("lab", gpu="H100", max_hours=1, size_gb=10, budget_usd=float("nan"))


def test_cpu_workspaces_send_the_vm_compute_class():
    server = Server({("POST", BASE): (201, {**STOPPED, "configuration": {**CONFIGURATION, "gpu": ""}})})
    with sync_client(server) as client:
        client.workspaces.create("cpu-lab", cpus=4, memory_gb=16, max_hours=2, size_gb=10)
    assert server.requests[-1][2] == {"name": "cpu-lab", "environment": "pytorch-cpu", "editor": "vscode",
                                      "compute_class": "vm", "vcpus": 4, "host_memory_gb": 16,
                                      "max_hours": 2, "size_gb": 10}


def test_get_accepts_an_id_or_a_unique_name():
    other = {**STOPPED, "id": "ws_other", "name": "other"}
    server = Server({
        ("GET", BASE + "/kernel-lab"): (404, {"error": "not_found", "message": "no such workspace"}),
        ("GET", BASE + "/ws_kernel"): (200, RUNNING),
        ("GET", BASE + "/missing"): (404, {"error": "not_found", "message": "no such workspace"}),
        ("GET", BASE): (200, {"workspaces": [other, STOPPED], "next_cursor": ""}),
    })
    with sync_client(server) as client:
        assert client.workspaces.get("ws_kernel").state == "running"
        by_name = client.workspaces.get("kernel-lab")
        assert by_name.id == "ws_kernel" and by_name.state == "stopped"
        with pytest.raises(NotFoundError):
            client.workspaces.get("missing")
        listed = client.workspaces.list()
        assert [ws.id for ws in listed] == ["ws_other", "ws_kernel"]
        assert all(isinstance(ws, Workspace) for ws in listed)


def test_list_follows_cursors_from_the_research_workspace_route():
    calls = []
    def handler(request):
        assert request.url.path == BASE
        calls.append(request.url.params.get("cursor", ""))
        if not calls[-1]:
            return httpx.Response(200, json={"workspaces": [STOPPED], "next_cursor": "page-2"})
        return httpx.Response(200, json={"workspaces": [{**STOPPED, "id": "ws_2"}], "next_cursor": ""})
    with sync_client(handler) as client:
        assert [ws.id for ws in client.workspaces.list()] == ["ws_kernel", "ws_2"]
    assert calls == ["", "page-2"]


def test_wait_until_ready_fails_fast_when_compute_stops_or_fails():
    failed = {**STOPPED, "state": "failed", "session": {**SESSION, "state": "failed"},
              "status_message": "The compute session could not continue. Your last successful save is retained."}
    server = Server({("GET", BASE + "/ws_kernel"): [(200, CREATING), (200, failed)]})
    with sync_client(server) as client:
        ws = client.workspaces.get("ws_kernel")
        with pytest.raises(WorkspaceNotReadyError, match="could not continue"):
            ws.wait_until_ready(poll_seconds=0.1, timeout_seconds=5)
        assert ws.state == "failed"


def test_wait_until_ready_times_out_and_leaves_compute_running():
    server = Server({("GET", BASE + "/ws_kernel"): (200, CREATING)})
    with sync_client(server) as client:
        ws = client.workspaces.get("ws_kernel")
        with pytest.raises(WorkspaceNotReadyError, match="still starting"):
            ws.wait_until_ready(poll_seconds=0.1, timeout_seconds=0)
        assert ws.state == "creating"
    assert all(method == "GET" for method, *_ in server.requests)


def test_wait_until_ready_honours_the_configured_tool():
    ssh_only = {**RUNNING, "configuration": {**CONFIGURATION, "editor": "ssh", "ssh_authorized_key": "ssh-ed25519 AAAA"},
                "connections": {"editor": False, "notebook": False, "ssh": True}}
    server = Server({("GET", BASE + "/ws_kernel"): (200, ssh_only)})
    with sync_client(server) as client:
        ws = client.workspaces.get("ws_kernel")
        assert ws.wait_until_ready(poll_seconds=0.1, timeout_seconds=1) is ws
        assert ws.ready is True


def test_stop_without_a_session_refreshes_first_and_is_a_no_op_when_already_stopped():
    server = Server({("GET", BASE + "/ws_kernel"): [(200, RUNNING), (200, STOPPED)],
                     ("POST", BASE + "/ws_kernel/stop"): (202, STOPPING)})
    with sync_client(server) as client:
        ws = Workspace(client, "ws_kernel")
        assert ws.stop(idempotency_key="stop-2").state == "stopping"
        assert server.requests[-1][2] == {"session_id": "sb_session"}
        stopped = Workspace(client, "ws_kernel").stop(idempotency_key="stop-3")
        assert stopped.state == "stopped"
    assert [m for m, *_ in server.requests] == ["GET", "POST", "GET"]


def test_stop_and_start_generate_a_retry_key_when_none_is_given():
    server = Server({("POST", BASE + "/ws_kernel/start"): (202, CREATING),
                     ("GET", BASE + "/ws_kernel"): (200, RUNNING),
                     ("POST", BASE + "/ws_kernel/stop"): (202, STOPPING)})
    with sync_client(server) as client:
        ws = Workspace(client, "ws_kernel")
        ws.start()
        ws.stop()
    keys = [headers["idempotency-key"] for method, _, _, headers in server.requests if method == "POST"]
    assert len(keys) == 2 and all(key.startswith("nodus-") for key in keys) and keys[0] != keys[1]


def test_connect_reports_not_ready_with_the_server_guidance():
    server = Server({("POST", BASE + "/ws_kernel/connections"): (409, {
        "error": "workspace_not_ready", "message": "This workspace is not ready to connect.",
        "fix": "Wait for the GPU and its tools to finish starting."})})
    with sync_client(server) as client:
        with pytest.raises(WorkspaceNotReadyError, match="not ready to connect"):
            Workspace(client, "ws_kernel").connect("notebook")
        with pytest.raises(ValidationError):
            Workspace(client, "ws_kernel").connect("terminal")
    assert len(server.requests) == 1


def test_run_forwards_hardware_overrides_and_returns_a_workload_handle():
    server = Server({("POST", BASE + "/ws_kernel/workloads"): (202, RECEIPT),
                     ("GET", "/v1/workloads/wl_train"): (200, {**RECEIPT, "status": "completed"}),
                     ("GET", BASE + "/ws_kernel/workloads"): (200, {"workloads": [RECEIPT], "pending_submissions": [],
                                                                    "source_storage": {}})})
    with sync_client(server) as client:
        ws = Workspace(client, "ws_kernel")
        job = ws.run("python train.py --epochs 3", budget_usd=12.5, gpu="A100", gpu_count=2, gpu_memory_gb=80,
                     idempotency_key="train-2")
        assert isinstance(job, nodus.Workload)
        assert job.refresh().status == "completed"
        assert ws.workloads() == [RECEIPT]
    assert server.requests[0][2] == {"command": "python train.py --epochs 3", "budget_usd": 12.5, "gpu": "A100",
                                     "gpu_count": 2, "gpu_memory_gb": 80}


def test_run_requires_a_positive_finite_budget_before_any_request():
    def handler(_):
        pytest.fail("an unpriced job must not reach the API")
    with sync_client(handler) as client:
        for budget in (0, -1, float("inf"), True, "5"):
            with pytest.raises(ValidationError, match="budget_usd"):
                Workspace(client, "ws_kernel").run("python x.py", budget_usd=budget)
        with pytest.raises(ValidationError, match="command"):
            Workspace(client, "ws_kernel").run("   ", budget_usd=1)


def test_upload_replaces_the_current_revision_and_waits_for_verification(tmp_path):
    (tmp_path / "train.py").write_text("print('hi')\n")
    pending = {"id": "tr_1", "workspace_id": "ws_kernel", "state": "verifying", "base_revision": 3,
               "manifest_sha256": "0" * 64, "manifest_bytes": 10}
    transfers = []
    def handler(request):
        path = request.url.path
        if path == BASE + "/ws_kernel":
            answers = [{**STOPPED, "pending_upload": pending}, {**STOPPED, "storage_revision": 4}]
            return httpx.Response(200, json=answers[min(len(transfers) - 1, 1)] if transfers else STOPPED)
        if path == BASE + "/ws_kernel/transfers":
            body = json.loads(request.content)
            transfers.append(body)
            canonical = json.dumps(body["manifest"], separators=(",", ":")).encode()
            return httpx.Response(201, json={"id": "tr_1", "state": "uploading", "uploaded_segments": [],
                                             "workspace_id": "ws_kernel", "manifest_bytes": len(canonical),
                                             "manifest_sha256": hashlib.sha256(canonical).hexdigest()})
        if path.startswith(BASE + "/ws_kernel/transfers/tr_1/segments/"):
            transfers.append("segment")
            return httpx.Response(204)
        if path == BASE + "/ws_kernel/transfers/tr_1/finalize":
            transfers.append("finalize")
            return httpx.Response(200, json={"id": "tr_1", "state": "verifying", "workspace_id": "ws_kernel"})
        pytest.fail("unexpected " + path)
    with sync_client(handler) as client:
        ws = client.workspaces.get("ws_kernel")
        result = ws.upload(tmp_path, idempotency_key="upload-1", poll_seconds=0.1, timeout_seconds=5)
        assert result["state"] == "verifying"
        assert ws.storage_revision == 4 and ws.pending_upload is None
    assert transfers[0]["idempotency_key"] == "upload-1" and transfers[0]["replace_revision"] == 3
    assert transfers[-2:] == ["segment", "finalize"]


def test_upload_refuses_while_compute_is_running_before_packaging(tmp_path):
    server = Server({("GET", BASE + "/ws_kernel"): (200, RUNNING)})
    with sync_client(server) as client:
        with pytest.raises(WorkspaceNotReadyError, match="Stop compute"):
            client.workspaces.get("ws_kernel").upload(tmp_path, idempotency_key="upload-2")
    assert [m for m, *_ in server.requests] == ["GET", "GET"]


def test_download_exports_the_current_revision(tmp_path, monkeypatch):
    seen = {}
    def export(client, workspace_id, destination, *, storage_revision, overwrite):
        seen.update(workspace_id=workspace_id, storage_revision=storage_revision, overwrite=overwrite)
        return tmp_path / "out.tar"
    monkeypatch.setattr("nodus._workspace_files.export_files", export)
    server = Server({("GET", BASE + "/ws_kernel"): (200, STOPPED)})
    with sync_client(server) as client:
        assert client.workspaces.get("ws_kernel").download(tmp_path / "out.tar") == tmp_path / "out.tar"
    assert seen == {"workspace_id": "ws_kernel", "storage_revision": 3, "overwrite": False}


def test_configure_sends_the_observed_revision_and_absorbs_the_answer():
    updated = {**STOPPED, "configuration": {**CONFIGURATION, "gpu_count": 2}, "configuration_revision": "b" * 64}
    server = Server({("GET", BASE + "/ws_kernel"): (200, STOPPED), ("PATCH", BASE + "/ws_kernel"): (200, updated)})
    with sync_client(server) as client:
        ws = client.workspaces.get("ws_kernel")
        assert ws.configure(gpu_count=2).configuration["gpu_count"] == 2
        assert ws.configuration_revision == "b" * 64
        with pytest.raises(ValidationError, match="unknown"):
            ws.configure(colour="blue")
    assert server.requests[-1][2] == {"configuration_revision": REVISION,
                                      "configuration": {**CONFIGURATION, "gpu_count": 2}}


def test_schedule_and_sessions_use_their_routes():
    plan = {"id": "plan_1", "ready_by": "2026-09-28T09:00:00Z", "stop_at": "2026-09-28T17:00:00Z"}
    server = Server({("POST", BASE + "/ws_kernel/schedule"): (201, plan),
                     ("DELETE", BASE + "/ws_kernel/schedule"): (200, STOPPED),
                     ("GET", BASE + "/ws_kernel/sessions"): (200, {"sessions": [SESSION]}),
                     ("GET", BASE + "/storage"): (200, {"retained_bytes": 5})})
    with sync_client(server) as client:
        ws = Workspace(client, "ws_kernel")
        assert ws.schedule(ready_by="2026-09-28T09:00:00Z", stop_at="2026-09-28T17:00:00Z",
                           idempotency_key="plan-1") == plan
        assert ws.unschedule().state == "stopped"
        assert ws.sessions() == [SESSION]
        assert client.workspaces.storage() == {"retained_bytes": 5}
    assert server.requests[0][2] == {"ready_by": "2026-09-28T09:00:00Z", "stop_at": "2026-09-28T17:00:00Z"}
    assert server.requests[0][3]["idempotency-key"] == "plan-1"
    assert server.requests[1][3]["idempotency-key"].startswith("nodus-")


def test_invalid_view_is_an_api_error_not_a_silent_handle():
    server = Server({("GET", BASE + "/ws_kernel"): (200, {"workspaces": []})})
    with sync_client(server) as client:
        with pytest.raises(APIError):
            client.workspaces.get("ws_kernel")


def test_legacy_volume_create_moves_to_client_volumes_with_a_warning():
    record = {"id": "ws_repo", "name": "repo", "size_gb": 0.1, "holder_id": None}
    server = Server({("POST", "/v1/workspaces"): (201, record),
                     ("GET", "/v1/workspaces"): (200, {"workspaces": [record]})})
    with sync_client(server) as client:
        assert client.volumes.create("repo", size_gb=0.1) == record
        assert client.volumes.list() == [record]
        with pytest.warns(FutureWarning, match="client.volumes"):
            assert client.workspaces.create("repo", size_gb=0.1) == record
        with pytest.raises(ValidationError, match="gpu"):
            client.workspaces.create("repo", size_gb=0.1, gpu_count=8)
