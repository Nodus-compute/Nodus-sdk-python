"""Launching a GPU, listing running compute and the Nodus-Client identity, on the real wire."""

from __future__ import annotations

import asyncio
import itertools
import json

import httpx
import pytest

import nodus
from nodus import cli
from nodus.errors import ValidationError, WorkspaceNotReadyError

BASE = "/v1/research-workspaces"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyOnlyForTests agent@laptop"
INSTANCE_CONFIGURATION = {"kind": "instance", "name": "instance-1a2b3c4d", "environment": "pytorch-cuda",
                          "editor": "ssh", "gpu": "H100", "gpu_count": 1, "gpu_memory_gb": 80, "disk_gb": 100,
                          "budget_usd": 0, "max_hours": 4, "size_gb": 0, "ssh_authorized_key": KEY}
# createResearchWorkspace answers 201 with researchWorkspaceCurrentView of a new instance record.
INSTANCE_STOPPED = {
    "id": "ws_inst", "expired_at": None, "cleanup_pending": False, "name": "instance-1a2b3c4d", "size_gb": 0,
    "holder_id": None, "saved_at": None, "stored_bytes": None, "last_error": "", "saving_for_termination": False,
    "billing_status": "metered_subject_to_account_limits", "configuration": INSTANCE_CONFIGURATION,
    "configuration_revision": "b" * 64, "storage_revision": 0, "storage_policy_version": "",
    "session": None, "meter": None, "state": "stopped",
    "status_message": "Compute is stopped. Launching creates a fresh instance with local disk.",
    "connections": {"editor": False, "notebook": False, "ssh": False},
    "storage": {}, "pending_upload": None,
}
INSTANCE_SESSION = {"id": "pod_inst", "state": "creating", "created_at": "2026-09-27T10:00:00Z"}
INSTANCE_CREATING = {**INSTANCE_STOPPED, "state": "creating", "session": INSTANCE_SESSION,
                     "status_message": "Finding compute and starting your tools."}
INSTANCE_READY = {**INSTANCE_CREATING, "state": "running", "session": {**INSTANCE_SESSION, "state": "ready"},
                  "connections": {"editor": False, "notebook": False, "ssh": True}}
SSH = {"transport": "direct", "host": "203.0.113.7", "port": "22022", "user": "root",
       "command": "ssh -p 22022 root@203.0.113.7",
       "ssh_config": "Host nodus-instance-1a2b3c4d\n  HostName 203.0.113.7\n  Port 22022\n  User root\n"}
CAPABILITIES = {"available": True, "storage_limit_bytes": 10_000_000_000, "environments": ["pytorch-cuda"],
                "gpu_counts": [1, 2, 4, 8], "editors": ["vscode", "jupyter", "ssh"],
                "storage_policy_version": "r2-standard-10gb-account-v1"}

LAUNCHED_BY = {"client": "claude-code", "user_id": "usr_1", "user_name": "Ada", "api_key_id": "key_1",
               "api_key_name": "laptop"}
INSTANCE_ITEM = {"id": "ws_inst", "type": "instance", "name": "instance-1a2b3c4d", "gpu": "H100", "gpu_count": 1,
                 "gpu_memory_gb": 80, "state": "ready", "status_text": "Ready for SSH",
                 "created_at": "2026-09-27T10:00:00Z", "started_at": "2026-09-27T10:02:00Z", "ended_at": None,
                 "launched_by": LAUNCHED_BY, "group": None,
                 "resource": {"kind": "research_workspace", "id": "ws_inst"}}
SWEEP_ITEM = {"id": "sw_1", "type": "training", "name": "lr-sweep", "gpu": "A100", "gpu_count": 1,
              "gpu_memory_gb": 80, "state": "running", "status_text": "Training · step 1,840",
              "created_at": "2026-09-27T09:00:00Z", "started_at": "2026-09-27T09:01:00Z", "ended_at": None,
              "launched_by": {"client": "console", "user_id": "usr_1", "user_name": "Ada", "api_key_id": None,
                              "api_key_name": None},
              "group": {"id": "sw_1", "kind": "sweep", "size": 12, "running": 9, "succeeded": 3, "failed": 0},
              "resource": {"kind": "sweep", "id": "sw_1"}}
SUMMARY = {"running": {"instance": 1, "training": 9, "workspace": 0}}


def sync_client(handler) -> nodus.Client:
    client = nodus.Client(api_key="nk_live_test", base_url="https://nodus.invalid", max_retries=0)
    client._http._transport = httpx.MockTransport(handler)
    return client


def async_client(handler) -> nodus.AsyncClient:
    client = nodus.AsyncClient(api_key="nk_live_test", base_url="https://nodus.invalid", max_retries=0)
    client._http._transport = httpx.MockTransport(handler)
    return client


def launch_handler(calls, views, *, create_view=INSTANCE_STOPPED):
    def handler(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body, request.headers.get("Idempotency-Key")))
        if request.url.path == BASE + "/capabilities":
            return httpx.Response(200, json=CAPABILITIES)
        if request.url.path == BASE and request.method == "POST":
            return httpx.Response(201, json=create_view)
        if request.url.path == BASE + "/ws_inst/start":
            return httpx.Response(202, json=INSTANCE_CREATING)
        if request.url.path == BASE + "/ws_inst":
            return httpx.Response(200, json=next(views))
        if request.url.path == BASE + "/ws_inst/connections":
            return httpx.Response(200, json=SSH)
        pytest.fail("unexpected " + request.method + " " + request.url.path)
    return handler


# -- Nodus-Client header ---------------------------------------------------------------------------------------


def test_every_sdk_request_names_the_python_sdk():
    seen = []
    client = sync_client(lambda request: seen.append(request) or httpx.Response(200, json={"workspaces": [], "next_cursor": ""}))
    client.workspaces.list()
    assert seen[0].headers["Nodus-Client"] == "python-sdk"

    async def scenario():
        aclient = async_client(lambda request: seen.append(request) or httpx.Response(200, json={"workspaces": [], "next_cursor": ""}))
        await aclient.workspaces.list()
    asyncio.run(scenario())
    assert seen[1].headers["Nodus-Client"] == "python-sdk"


def test_cli_requests_name_the_cli_and_the_identity_does_not_leak(monkeypatch, capsys):
    seen = []
    real = cli.Client

    def build(**kwargs):
        client = real(api_key="nk_live_test", base_url="https://nodus.invalid", max_retries=0)
        client._http._transport = httpx.MockTransport(
            lambda request: seen.append(request) or httpx.Response(200, json={"items": [], "next_cursor": None, "summary": SUMMARY}))
        return client

    monkeypatch.setattr(cli, "Client", build)
    assert cli.main(["ps"]) == 0
    assert seen[0].headers["Nodus-Client"] == "cli"
    assert nodus.Client(api_key="nk_live_test", base_url="https://nodus.invalid")._http.headers["Nodus-Client"] == "python-sdk"


@pytest.mark.parametrize("name,expected", [
    ("claude-code", "claude-code"), ("Claude Desktop", "claude-code"), ("codex-mcp-client", "codex"),
    ("cursor-vscode", "cursor"), ("mcp", "mcp"), ("something-else", "mcp"),
    ("claude\r\nNodus-Client: console", "claude-code"), ("console", "mcp"), ("api", "mcp"),
])
def test_mcp_client_names_map_only_to_allowlisted_values(name, expected):
    from nodus._client_identity import mcp_client
    assert mcp_client(name) == expected


def test_an_unlisted_identity_is_refused_before_any_header_is_built():
    from nodus import _client_identity
    with pytest.raises(ValueError):
        _client_identity.identify("console")


# -- launch ----------------------------------------------------------------------------------------------------


def test_launch_creates_an_ssh_instance_starts_it_with_the_same_key_and_waits_for_ssh():
    calls = []
    views = itertools.chain([INSTANCE_CREATING], itertools.repeat(INSTANCE_READY))
    client = sync_client(launch_handler(calls, views))
    machine = client.launch("H100", ssh_key=KEY, name="instance-1a2b3c4d", idempotency_key="launch-1",
                            poll_seconds=0.1)
    assert machine.id == "ws_inst" and machine.ready
    create, start = calls[0], calls[1]
    assert create[:2] == ("POST", BASE)
    assert create[2] == {"kind": "instance", "name": "instance-1a2b3c4d", "editor": "ssh",
                         "environment": "pytorch-cuda", "gpu": "H100", "gpu_count": 1, "gpu_memory_gb": 80,
                         "max_hours": 4, "size_gb": 0, "ssh_authorized_key": KEY, "disk_gb": 100}
    assert create[3] == "launch-1"
    assert start == ("POST", BASE + "/ws_inst/start", {}, "launch-1")
    assert not any(call[1] == BASE + "/capabilities" for call in calls)
    assert machine.ssh()["command"] == "ssh -p 22022 root@203.0.113.7"


def test_launch_names_an_instance_like_the_console_and_reads_the_default_public_key(tmp_path):
    ssh_dir = tmp_path / "home" / ".ssh"
    ssh_dir.mkdir(parents=True)
    (ssh_dir / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n")
    (ssh_dir / "id_ed25519.pub").write_text(KEY + "\n")
    calls = []
    client = sync_client(launch_handler(calls, itertools.repeat(INSTANCE_READY)))
    client.launch("H100:2", wait=False)
    body = calls[0][2]
    assert body["name"].startswith("instance-") and len(body["name"]) == len("instance-") + 8
    assert body["ssh_authorized_key"] == KEY + "\n" and "PRIVATE" not in json.dumps(body)
    assert body["gpu_count"] == 2
    assert [call[1] for call in calls] == [BASE, BASE + "/ws_inst/start"]


def test_launch_without_any_public_key_is_refused_before_the_network():
    client = sync_client(lambda request: pytest.fail("no request expected"))
    with pytest.raises(ValidationError, match="ssh_key"):
        client.launch("H100")


def test_launch_requires_a_gpu():
    client = sync_client(lambda request: pytest.fail("no request expected"))
    with pytest.raises(ValidationError, match="gpu"):
        client.launch(ssh_key=KEY)


def test_launch_keep_files_creates_a_saved_workspace_and_starts_it():
    calls = []
    workspace_view = {**INSTANCE_STOPPED, "size_gb": 10,
                      "configuration": {**INSTANCE_CONFIGURATION, "kind": "", "size_gb": 10},
                      "status_message": "Saved project files are ready for the next session."}
    workspace_view["configuration"].pop("kind")
    ready = {**workspace_view, "state": "running", "session": INSTANCE_SESSION,
             "connections": {"editor": False, "notebook": False, "ssh": True}}
    client = sync_client(launch_handler(calls, itertools.repeat(ready), create_view=workspace_view))
    machine = client.launch("H100", ssh_key=KEY, name="lab", keep_files=True, idempotency_key="launch-2",
                            poll_seconds=0.1)
    assert machine.ready
    create = next(call for call in calls if call[:2] == ("POST", BASE))
    assert "kind" not in create[2]
    assert create[2]["editor"] == "ssh" and create[2]["size_gb"] == 10 and create[2]["max_hours"] == 4
    assert create[2]["disk_gb"] == 100 and create[2]["ssh_authorized_key"] == KEY
    assert ("POST", BASE + "/ws_inst/start", {}, "launch-2") in calls


def test_a_launch_timeout_leaves_the_machine_running_and_names_it():
    calls = []
    client = sync_client(launch_handler(calls, itertools.repeat(INSTANCE_CREATING)))
    with pytest.raises(WorkspaceNotReadyError) as raised:
        client.launch("H100", ssh_key=KEY, timeout_seconds=0, poll_seconds=0.1)
    message = str(raised.value)
    assert "ws_inst" in message and "still running" in message
    assert not any(call[1].endswith("/stop") for call in calls)


def test_a_launch_whose_start_is_uncertain_carries_the_key_for_the_retry():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == BASE:
            return httpx.Response(201, json=INSTANCE_STOPPED)
        return httpx.Response(503, json={"error": {"code": "unavailable", "message": "try again"}})

    client = sync_client(handler)
    with pytest.raises(nodus.NodusError) as raised:
        client.launch("H100", ssh_key=KEY, idempotency_key="launch-3")
    message = str(raised.value)
    # A second launch would create and start a second machine, so the retry names this one.
    assert "launch-3" in message and "ws_inst" in message and "not launch again" in message
    assert raised.value.body["workspace_id"] == "ws_inst"
    assert [request.url.path for request in calls] == [BASE, BASE + "/ws_inst/start"]


def test_async_launch_matches_the_sync_request_sequence():
    calls = []
    views = itertools.chain([INSTANCE_CREATING], itertools.repeat(INSTANCE_READY))
    client = async_client(launch_handler(calls, views))

    async def scenario():
        machine = await client.launch("H100", ssh_key=KEY, name="instance-1a2b3c4d", idempotency_key="launch-1",
                                      poll_seconds=0.1)
        return machine, await machine.ssh()

    machine, ssh = asyncio.run(scenario())
    assert machine.ready and ssh["command"].startswith("ssh ")
    assert calls[0][2]["kind"] == "instance" and calls[1][:2] == ("POST", BASE + "/ws_inst/start")


# -- compute listing -------------------------------------------------------------------------------------------


def test_compute_list_sends_the_contract_filters_and_returns_items():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"items": [INSTANCE_ITEM], "next_cursor": None, "summary": SUMMARY})

    client = sync_client(handler)
    assert client.compute.list() == [INSTANCE_ITEM]
    assert seen[0].url.path == "/v1/compute" and dict(seen[0].url.params) == {"state": "running"}
    client.compute.list(state="history", type="training", launched_by="agents", limit=20)
    assert dict(seen[1].url.params) == {"state": "history", "type": "training", "launched_by": "agents", "limit": "20"}


@pytest.mark.parametrize("options", [{"state": "all"}, {"type": "job"}, {"launched_by": "claude"},
                                     {"limit": 0}, {"limit": 101}, {"limit": True}])
def test_compute_list_refuses_values_the_endpoint_does_not_accept(options):
    client = sync_client(lambda request: pytest.fail("no request expected"))
    with pytest.raises(ValidationError):
        client.compute.list(**options)


def test_compute_iterate_follows_the_cursor_and_stops_on_a_repeat():
    pages = {None: {"items": [INSTANCE_ITEM], "next_cursor": "c2", "summary": SUMMARY},
             "c2": {"items": [SWEEP_ITEM], "next_cursor": None, "summary": SUMMARY}}
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    client = sync_client(handler)
    assert list(client.compute.iterate(type="training")) == [INSTANCE_ITEM, SWEEP_ITEM]
    assert seen == [{"state": "running", "type": "training"}, {"state": "running", "type": "training", "cursor": "c2"}]

    looping = sync_client(lambda request: httpx.Response(200, json={"items": [INSTANCE_ITEM], "next_cursor": "same",
                                                                     "summary": SUMMARY}))
    with pytest.raises(nodus.errors.APIError, match="cursor"):
        list(looping.compute.iterate())


def test_compute_group_reads_one_sweep():
    group = {"group": SWEEP_ITEM["group"], "items": [{**SWEEP_ITEM, "id": "wl_1", "group": None,
                                                     "resource": {"kind": "workload", "id": "wl_1"}}]}
    seen = []
    client = sync_client(lambda request: seen.append(request) or httpx.Response(200, json=group))
    assert client.compute.group("sw_1") == group
    assert seen[0].url.path == "/v1/compute/groups/sw_1"
    with pytest.raises(ValidationError):
        client.compute.group("../workloads")


def test_async_compute_matches_sync():
    pages = {None: {"items": [INSTANCE_ITEM], "next_cursor": "c2", "summary": SUMMARY},
             "c2": {"items": [SWEEP_ITEM], "next_cursor": None, "summary": SUMMARY}}
    client = async_client(lambda request: httpx.Response(200, json=pages[request.url.params.get("cursor")]))

    async def scenario():
        first = await client.compute.list()
        every = [item async for item in client.compute.iterate()]
        return first, every

    first, every = asyncio.run(scenario())
    assert first == [INSTANCE_ITEM] and every == [INSTANCE_ITEM, SWEEP_ITEM]


# -- CLI -------------------------------------------------------------------------------------------------------


def cli_client(monkeypatch, handler):
    def build(**_kwargs):
        client = nodus.Client(api_key="nk_live_test", base_url="https://nodus.invalid", max_retries=0)
        client._http._transport = httpx.MockTransport(handler)
        return client
    monkeypatch.setattr(cli, "Client", build)


def test_cli_launch_prints_the_ssh_command_when_ready(monkeypatch, capsys, tmp_path):
    key = tmp_path / "id.pub"
    key.write_text(KEY + "\n")
    calls = []
    views = itertools.chain([INSTANCE_CREATING], itertools.repeat(INSTANCE_READY))
    cli_client(monkeypatch, launch_handler(calls, views))
    assert cli.main(["launch", "--gpu", "H100", "--gpus", "2", "--disk", "200", "--env", "pytorch-cuda",
                     "--ssh-key", str(key), "--name", "instance-1a2b3c4d", "--hours", "6",
                     "--poll-seconds", "0.1"]) == 0
    out = capsys.readouterr().out
    assert "ssh -p 22022 root@203.0.113.7" in out and "ws_inst" in out
    body = calls[0][2]
    assert body["gpu_count"] == 2 and body["disk_gb"] == 200 and body["max_hours"] == 6
    assert body["environment"] == "pytorch-cuda" and body["kind"] == "instance"


def test_cli_launch_no_wait_prints_the_id_and_how_to_connect(monkeypatch, capsys, tmp_path):
    key = tmp_path / "id.pub"
    key.write_text(KEY)
    calls = []
    cli_client(monkeypatch, launch_handler(calls, itertools.repeat(INSTANCE_CREATING)))
    assert cli.main(["launch", "--gpu", "H100", "--ssh-key", str(key), "--no-wait"]) == 0
    out = capsys.readouterr().out
    assert "ws_inst" in out and "nodus ssh ws_inst" in out
    assert [call[1] for call in calls] == [BASE, BASE + "/ws_inst/start"]


def test_cli_launch_timeout_says_the_machine_is_still_running(monkeypatch, capsys, tmp_path):
    key = tmp_path / "id.pub"
    key.write_text(KEY)
    cli_client(monkeypatch, launch_handler([], itertools.repeat(INSTANCE_CREATING)))
    assert cli.main(["launch", "--gpu", "H100", "--ssh-key", str(key), "--timeout", "0"]) == 2
    err = capsys.readouterr().err
    assert "ws_inst" in err and "still running" in err


def test_cli_ps_shows_running_compute_without_any_cost(monkeypatch, capsys):
    seen = []
    cli_client(monkeypatch, lambda request: seen.append(request) or httpx.Response(
        200, json={"items": [INSTANCE_ITEM, SWEEP_ITEM], "next_cursor": None, "summary": SUMMARY}))
    assert cli.main(["ps"]) == 0
    out = capsys.readouterr().out
    header = out.splitlines()[0].split()
    assert header == ["NAME", "TYPE", "GPU", "STATUS", "LAUNCHED", "BY"]
    assert "instance-1a2b3c4d" in out and "Instance" in out and "Training" in out and "H100" in out
    assert "Claude" in out and "Ada" in out
    assert "$" not in out and "cost" not in out.lower() and "usd" not in out.lower()
    assert dict(seen[0].url.params) == {"state": "running"}
    before = len(seen)
    assert cli.main(["ps", "--all"]) == 0
    capsys.readouterr()
    assert [request.url.params["state"] for request in seen[before:]] == ["running", "history"]


def test_cli_ps_json_prints_the_items(monkeypatch, capsys):
    cli_client(monkeypatch, lambda request: httpx.Response(
        200, json={"items": [INSTANCE_ITEM], "next_cursor": None, "summary": SUMMARY}))
    assert cli.main(["ps", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [INSTANCE_ITEM]


def test_cli_ps_strips_terminal_controls_from_server_text(monkeypatch, capsys):
    hostile = {**INSTANCE_ITEM, "name": "evil\x1b]0;owned\x07name"}
    cli_client(monkeypatch, lambda request: httpx.Response(
        200, json={"items": [hostile], "next_cursor": None, "summary": SUMMARY}))
    assert cli.main(["ps"]) == 0
    assert "\x1b" not in capsys.readouterr().out


def test_cli_ssh_and_stop_find_an_instance_by_name(monkeypatch, capsys):
    calls = []

    def handler(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, dict(request.url.params), body))
        if request.url.path == BASE + "/instance-1a2b3c4d":
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "not found"}})
        if request.url.path == BASE and request.url.params.get("kind") == "instance":
            return httpx.Response(200, json={"workspaces": [INSTANCE_READY], "next_cursor": ""})
        if request.url.path == BASE:
            return httpx.Response(200, json={"workspaces": [], "next_cursor": ""})
        if request.url.path == BASE + "/ws_inst":
            return httpx.Response(200, json=INSTANCE_READY)
        if request.url.path == BASE + "/ws_inst/connections":
            return httpx.Response(200, json=SSH)
        if request.url.path == BASE + "/ws_inst/stop":
            return httpx.Response(202, json={**INSTANCE_READY, "state": "stopping"})
        pytest.fail("unexpected " + request.url.path)

    cli_client(monkeypatch, handler)
    assert cli.main(["ssh", "instance-1a2b3c4d"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "ssh -p 22022 root@203.0.113.7"
    assert cli.main(["stop", "--idempotency-key", "stop-1", "instance-1a2b3c4d"]) == 0
    assert capsys.readouterr().out.strip() == "ws_inst stopping"
    assert calls[-1][1:] == (BASE + "/ws_inst/stop", {}, {"session_id": "pod_inst"})


# -- MCP -------------------------------------------------------------------------------------------------------


@pytest.fixture
def api(monkeypatch):
    from nodus import _mcp
    from nodus.config import save_credentials
    save_credentials("saved-test-key", "https://api.example.test")
    requests, responses = [], []

    def respond(request):
        requests.append(request)
        return responses.pop(0) if responses else httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient
    monkeypatch.setattr(_mcp.httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(respond), **kwargs))
    return _mcp.create_server(), requests, responses


@pytest.mark.asyncio
@pytest.mark.parametrize("client_name,expected", [(None, "mcp"), ("claude-code", "claude-code"),
                                                  ("codex-mcp-client", "codex"), ("Cursor", "cursor")])
async def test_mcp_requests_name_the_calling_agent(api, client_name, expected):
    from mcp.shared.memory import create_connected_server_and_client_session
    from mcp.types import Implementation
    server, requests, _ = api
    info = Implementation(name=client_name, version="1.0") if client_name else None
    async with create_connected_server_and_client_session(server, client_info=info) as session:
        result = await session.call_tool("list_workspaces", {})
        assert not result.isError, result
    assert requests[-1].headers["Nodus-Client"] == expected


@pytest.mark.asyncio
async def test_mcp_compute_tools_send_the_launch_list_and_stop_bodies(api):
    from mcp.shared.memory import create_connected_server_and_client_session
    server, requests, responses = api
    async with create_connected_server_and_client_session(server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert tools["list_compute"].annotations.readOnlyHint is True
        assert tools["stop_compute"].annotations.destructiveHint is True
        assert "idempotency_key" in tools["launch_gpu"].inputSchema["required"]

        responses.extend([httpx.Response(201, json=INSTANCE_STOPPED), httpx.Response(202, json=INSTANCE_CREATING)])
        result = await session.call_tool("launch_gpu", {"idempotency_key": "agent-launch-1", "gpu": "H100",
                                                        "ssh_key": KEY, "name": "instance-1a2b3c4d"})
        assert not result.isError, result
        create, start = requests[-2], requests[-1]
        assert (create.method, create.url.path) == ("POST", BASE)
        assert json.loads(create.content) == {"kind": "instance", "name": "instance-1a2b3c4d", "editor": "ssh",
                                              "environment": "pytorch-cuda", "gpu": "H100", "gpu_count": 1,
                                              "gpu_memory_gb": 80, "max_hours": 4, "size_gb": 0,
                                              "ssh_authorized_key": KEY, "disk_gb": 100}
        assert (start.method, start.url.path, json.loads(start.content)) == ("POST", BASE + "/ws_inst/start", {})
        assert create.headers["Idempotency-Key"] == start.headers["Idempotency-Key"] == "agent-launch-1"
        assert json.loads(result.content[0].text)["id"] == "ws_inst"

        responses.append(httpx.Response(200, json={"items": [INSTANCE_ITEM], "next_cursor": None, "summary": SUMMARY}))
        result = await session.call_tool("list_compute", {"state": "running", "launched_by": "agents"})
        assert not result.isError, result
        assert requests[-1].url.path == "/v1/compute"
        assert dict(requests[-1].url.params) == {"state": "running", "launched_by": "agents"}

        responses.extend([httpx.Response(200, json=INSTANCE_READY),
                          httpx.Response(202, json={**INSTANCE_READY, "state": "stopping"})])
        result = await session.call_tool("stop_compute", {"id": "ws_inst", "idempotency_key": "agent-stop-1"})
        assert not result.isError, result
        stop = requests[-1]
        assert (stop.method, stop.url.path, json.loads(stop.content)) == ("POST", BASE + "/ws_inst/stop",
                                                                          {"session_id": "pod_inst"})
        assert stop.headers["Idempotency-Key"] == "agent-stop-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments", [
    ("launch_gpu", {"gpu": "H100", "ssh_key": KEY}),
    ("launch_gpu", {"idempotency_key": "k", "gpu": "H100", "ssh_key": "-----BEGIN OPENSSH PRIVATE KEY-----"}),
    ("launch_gpu", {"idempotency_key": "k", "gpu": "H100", "ssh_key": KEY, "max_hours": 0}),
    ("list_compute", {"state": "everything"}),
    ("stop_compute", {"id": "../x", "idempotency_key": "k"}),
])
async def test_mcp_compute_arguments_are_refused_before_network(api, tool, arguments):
    from mcp.shared.memory import create_connected_server_and_client_session
    server, requests, _ = api
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(tool, arguments)
        assert result.isError
    assert requests == []
