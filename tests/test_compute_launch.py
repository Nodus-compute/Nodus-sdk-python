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
    "billing_status": "ephemeral_instance", "configuration": INSTANCE_CONFIGURATION,
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
SSH = {"transport": "tcp", "host": "203.0.113.7", "port": "22022", "user": "nodus",
       "command": "ssh -p 22022 nodus@203.0.113.7",
       "vscode_url": "vscode://vscode-remote/ssh-remote+nodus@203.0.113.7:22022/workspace",
       "ssh_config": "Host nodus-instance-1a2b3c4d\n  HostName 203.0.113.7\n  Port 22022\n  User nodus\n"}
CAPABILITIES = {"available": True, "storage_limit_bytes": 10_000_000_000, "environments": ["pytorch-cuda"],
                "gpu_counts": [1, 2, 4, 8], "editors": ["vscode", "jupyter", "ssh"],
                "storage_policy_version": "r2-standard-10gb-account-v1"}

LAUNCHED_BY = {"client": "claude-code", "user_id": "usr_1", "user_name": "ada@example.com", "api_key_id": "key_1",
               "api_key_name": "laptop"}
INSTANCE_ITEM = {"id": "ws_inst", "type": "instance", "name": "instance-1a2b3c4d", "gpu": "H100", "gpu_count": 1,
                 "gpu_memory_gb": 80, "state": "ready", "status_text": "Ready",
                 "created_at": "2026-09-27T10:00:00Z", "started_at": "2026-09-27T10:02:00Z", "ended_at": None,
                 "launched_by": LAUNCHED_BY, "group": None,
                 "resource": {"kind": "research_workspace", "id": "ws_inst"}}
SWEEP_ITEM = {"id": "sw_1", "type": "training", "name": "lr-sweep", "gpu": "A100", "gpu_count": 1,
              "gpu_memory_gb": 80, "state": "running", "status_text": "Training",
              "created_at": "2026-09-27T09:00:00Z", "started_at": "2026-09-27T09:01:00Z", "ended_at": None,
              "launched_by": {"client": "console", "user_id": "usr_1", "user_name": "ada@example.com", "api_key_id": None,
                              "api_key_name": None},
              "group": {"id": "sw_1", "kind": "sweep", "size": 12, "running": 9, "succeeded": 3, "failed": 0},
              "resource": {"kind": "sweep", "id": "sw_1"}}
SUMMARY = {"running": {"instance": 1, "training": 9, "workspace": 0}}
WORKSPACE_ITEM = {"id": "ws_lab", "type": "workspace", "name": "lab", "gpu": "H100", "gpu_count": 1,
                  "gpu_memory_gb": 80, "state": "ready", "status_text": "Ready",
                  "created_at": "2026-09-27T08:00:00Z", "started_at": "2026-09-27T08:02:00Z", "ended_at": None,
                  "launched_by": {"client": "cli", "user_id": "usr_1", "user_name": "ada@example.com",
                                  "api_key_id": "key_1", "api_key_name": "laptop"},
                  "group": None, "resource": {"kind": "research_workspace", "id": "ws_lab"}}


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
    assert machine.ssh()["command"] == "ssh -p 22022 nodus@203.0.113.7"


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


def test_a_repeated_launch_with_the_same_key_sends_the_same_create():
    # The server matches a repeated instance create by name and configuration, not by key.
    calls = []
    client = sync_client(launch_handler(calls, itertools.repeat(INSTANCE_READY)))
    client.launch("H100", ssh_key=KEY, idempotency_key="same-intent", wait=False)
    client.launch("H100", ssh_key=KEY, idempotency_key="same-intent", wait=False)
    client.launch("H100", ssh_key=KEY, idempotency_key="other-intent", wait=False)
    creates = [call[2] for call in calls if call[:2] == ("POST", BASE)]
    assert creates[0] == creates[1] and creates[0]["name"] != creates[2]["name"]


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
    client.compute.group("lr-sweep.v2")
    assert seen[1].url.path == "/v1/compute/groups/lr-sweep.v2"
    with pytest.raises(ValidationError):
        client.compute.group("..")
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
    assert "ssh -p 22022 nodus@203.0.113.7" in out and "ws_inst" in out
    assert "nodus ssh instance-1a2b3c4d" in out
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


def test_cli_launch_interrupted_during_start_names_the_retry_key(monkeypatch, capsys, tmp_path):
    key = tmp_path / "id.pub"
    key.write_text(KEY)

    def handler(request):
        if request.url.path == BASE:
            return httpx.Response(201, json=INSTANCE_STOPPED)
        raise KeyboardInterrupt

    cli_client(monkeypatch, handler)
    assert cli.main(["launch", "--gpu", "H100", "--ssh-key", str(key), "--idempotency-key", "launch-9"]) == 130
    err = capsys.readouterr().err
    assert "--idempotency-key=launch-9" in err


def test_cli_ps_shows_running_compute_without_any_cost(monkeypatch, capsys):
    seen = []
    cli_client(monkeypatch, lambda request: seen.append(request) or httpx.Response(
        200, json={"items": [INSTANCE_ITEM, SWEEP_ITEM], "next_cursor": None, "summary": SUMMARY}))
    assert cli.main(["ps"]) == 0
    out = capsys.readouterr().out
    header = out.splitlines()[0].split()
    assert header == ["NAME", "TYPE", "GPU", "STATUS", "LAUNCHED", "BY"]
    assert "instance-1a2b3c4d" in out and "Instance" in out and "Training" in out and "H100" in out
    assert "Claude" in out and "ada@example.com" in out
    assert dict(seen[0].url.params) == {"state": "running", "include": "workspaces"}
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
    assert cli.main(["ssh", "--print", "instance-1a2b3c4d"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "ssh -p 22022 nodus@203.0.113.7"
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
    from nodus._client_identity import acting_as
    server, requests, responses = api
    # nodus mcp runs inside the CLI, whose identity must not reach MCP requests.
    with acting_as("cli"):
        await _compute_tool_calls(server, requests, responses, create_connected_server_and_client_session)
    assert requests and all(request.headers["Nodus-Client"] == "mcp" for request in requests)


async def _compute_tool_calls(server, requests, responses, create_connected_server_and_client_session):
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
        assert dict(requests[-1].url.params) == {"state": "running", "launched_by": "agents", "include": "workspaces"}

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


# -- workspaces in the compute list ----------------------------------------------------------------------------


def test_compute_list_includes_workspaces_only_when_asked():
    seen = []
    client = sync_client(lambda request: seen.append(dict(request.url.params)) or httpx.Response(
        200, json={"items": [WORKSPACE_ITEM], "next_cursor": None, "summary": SUMMARY}))
    client.compute.list()
    client.compute.list(include_workspaces=True, type="workspace")
    list(client.compute.iterate(include_workspaces=True))
    assert seen == [{"state": "running"}, {"state": "running", "type": "workspace", "include": "workspaces"},
                    {"state": "running", "include": "workspaces"}]

    async def scenario():
        aclient = async_client(lambda request: seen.append(dict(request.url.params)) or httpx.Response(
            200, json={"items": [], "next_cursor": None, "summary": SUMMARY}))
        await aclient.compute.list(include_workspaces=True)
        return [item async for item in aclient.compute.iterate(include_workspaces=True)]
    asyncio.run(scenario())
    assert seen[-2:] == [{"state": "running", "include": "workspaces"}] * 2


def test_include_workspaces_must_be_a_bool():
    client = sync_client(lambda request: pytest.fail("no request expected"))
    with pytest.raises(ValidationError):
        client.compute.list(include_workspaces="yes")


def test_cli_ps_includes_workspaces_by_default(monkeypatch, capsys):
    seen = []
    cli_client(monkeypatch, lambda request: seen.append(dict(request.url.params)) or httpx.Response(
        200, json={"items": [INSTANCE_ITEM, WORKSPACE_ITEM], "next_cursor": None, "summary": SUMMARY}))
    assert cli.main(["ps"]) == 0
    out = capsys.readouterr().out
    assert seen == [{"state": "running", "include": "workspaces"}]
    row = next(line for line in out.splitlines() if line.startswith("lab "))
    assert "Workspace" in row


def test_cli_ps_explains_a_server_without_the_compute_list(monkeypatch, capsys):
    cli_client(monkeypatch, lambda request: httpx.Response(404, text="404 page not found\n"))
    assert cli.main(["ps"]) == 2
    captured = capsys.readouterr()
    lines = captured.err.strip().splitlines()
    assert len(lines) == 1 and "does not list running compute" in lines[0] and "nodus workspace ls" in lines[0]
    assert "Traceback" not in captured.err and captured.out == ""


@pytest.mark.asyncio
async def test_mcp_list_compute_includes_workspaces_by_default(api):
    from mcp.shared.memory import create_connected_server_and_client_session
    server, requests, _ = api
    async with create_connected_server_and_client_session(server) as session:
        assert not (await session.call_tool("list_compute", {})).isError
        assert not (await session.call_tool("list_compute", {"include_workspaces": False,
                                                             "type": "instance"})).isError
    assert dict(requests[0].url.params) == {"state": "running", "include": "workspaces"}
    assert dict(requests[1].url.params) == {"state": "running", "type": "instance"}


# -- nodus ssh opens the session -------------------------------------------------------------------------------

TUNNEL = {"transport": "tunnel", "host": "ws-abc-2222.nodus.run", "port": "22", "user": "nodus",
          "requires": "cloudflared",
          "command": "ssh -o ProxyCommand='cloudflared access ssh --hostname %h' nodus@ws-abc-2222.nodus.run",
          "vscode_url": "vscode://vscode-remote/ssh-remote+nodus@ws-abc-2222.nodus.run/workspace",
          "ssh_config": "Host nodus-lab\n  HostName ws-abc-2222.nodus.run\n  User nodus\n"
                        "  ProxyCommand cloudflared access ssh --hostname %h\n"}
# workspaceSSHWebSocketConfig: no user, port or command, and a ProxyCommand this SDK does not provide.
WEBSOCKET = {"host": "nodus-ws_inst", "transport": "websocket", "workspace_id": "ws_inst", "session_id": "sb_1",
             "generation": 3,
             "ssh_config": "Host nodus-ws_inst\n    HostName ws_inst\n    User nodus\n    Port 2222\n"
                           "    ProxyCommand nodus workspaces ssh-proxy ws_inst --session sb_1 --generation 3\n"
                           "    HostKeyAlias ws_inst\n    StrictHostKeyChecking ask\n"}


def ssh_cli(monkeypatch, connection, *, installed=("ssh", "cloudflared")):
    executed = []

    def handler(request):
        if request.url.path == BASE + "/ws_inst":
            return httpx.Response(200, json=INSTANCE_READY)
        if request.url.path == BASE + "/ws_inst/connections":
            return httpx.Response(200, json=connection)
        pytest.fail("unexpected " + request.url.path)

    cli_client(monkeypatch, handler)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}" if name in installed else None)
    monkeypatch.setattr(cli.os, "execvp", lambda file, argv: executed.append((file, argv)))
    return executed


def test_nodus_ssh_execs_ssh_with_an_argv_built_from_structured_fields(monkeypatch):
    executed = ssh_cli(monkeypatch, SSH)
    assert cli.main(["ssh", "ws_inst"]) == 0
    assert executed == [("ssh", ["ssh", "-p", "22022", "-l", "nodus", "--", "203.0.113.7"])]


def test_nodus_ssh_through_a_tunnel_uses_a_fixed_proxy_command(monkeypatch):
    executed = ssh_cli(monkeypatch, TUNNEL)
    assert cli.main(["ssh", "ws_inst"]) == 0
    assert executed == [("ssh", ["ssh", "-o", "ProxyCommand=cloudflared access ssh --hostname %h", "-p", "22",
                                 "-l", "nodus", "--", "ws-abc-2222.nodus.run"])]


def test_nodus_ssh_through_a_tunnel_without_cloudflared_says_so(monkeypatch, capsys):
    executed = ssh_cli(monkeypatch, TUNNEL, installed=("ssh",))
    assert cli.main(["ssh", "ws_inst"]) == 1
    assert executed == [] and "cloudflared" in capsys.readouterr().err


def test_nodus_ssh_without_ssh_prints_the_command_and_a_hint(monkeypatch, capsys):
    executed = ssh_cli(monkeypatch, SSH, installed=())
    assert cli.main(["ssh", "ws_inst"]) == 1
    captured = capsys.readouterr()
    assert executed == [] and captured.out.strip() == "ssh -p 22022 -l nodus -- 203.0.113.7"
    assert len(captured.err.strip().splitlines()) == 1 and "OpenSSH" in captured.err


def test_nodus_ssh_refuses_a_transport_it_cannot_open(monkeypatch, capsys):
    executed = ssh_cli(monkeypatch, WEBSOCKET)
    assert cli.main(["ssh", "ws_inst"]) == 1
    captured = capsys.readouterr()
    # Server-written ssh_config can carry a ProxyCommand, so it is shown only on request, never recommended.
    assert executed == [] and "cannot open" in captured.err and "--print" in captured.err
    assert "ProxyCommand" not in captured.out + captured.err


def test_the_ssh_missing_fallback_prints_no_terminal_controls(monkeypatch, capsys):
    ssh_cli(monkeypatch, {**SSH, "user": "nodus"}, installed=())
    monkeypatch.setattr("nodus._ssh._host", lambda value: "203.0.113.7\x1b]0;owned\x07")
    assert cli.main(["ssh", "ws_inst"]) == 1
    assert "\x1b" not in capsys.readouterr().out


def test_launch_suggests_the_id_when_the_name_looks_like_a_flag(monkeypatch, capsys, tmp_path):
    key = tmp_path / "id.pub"
    key.write_text(KEY)
    ready = {**INSTANCE_READY, "name": "--print"}
    cli_client(monkeypatch, launch_handler([], itertools.chain([INSTANCE_CREATING], itertools.repeat(ready))))
    assert cli.main(["launch", "--gpu", "H100", "--ssh-key", str(key), "--poll-seconds", "0.1"]) == 0
    assert "Connect: nodus ssh ws_inst" in capsys.readouterr().out


@pytest.mark.parametrize("field,value", [
    ("host", "203.0.113.7 -oProxyCommand=touch /tmp/x"), ("host", "-oProxyCommand=id"), ("host", "a;id"),
    ("host", "host\nProxyCommand id"), ("host", "'quoted'"), ("host", "a..b"), ("host", ""), ("host", None),
    ("host", "fe80::1%a@evil.example"), ("host", "::1%`id`"), ("host", "fe80::1%eth0"),
    ("host", "evil\x1b]0;owned\x07.example"),
    ("port", "22 -o"), ("port", "0"), ("port", "65536"), ("port", "-1"), ("port", "22\n"), ("port", 22.5),
    ("user", "nodus -oProxyCommand=id"), ("user", "-l"), ("user", "root;id"), ("user", "a\nb"), ("user", '"x"'),
    ("transport", "tcp;id"),
])
def test_nodus_ssh_refuses_hostile_server_values(monkeypatch, capsys, field, value):
    executed = ssh_cli(monkeypatch, {**SSH, field: value})
    assert cli.main(["ssh", "ws_inst"]) == 1
    assert executed == []
    assert "\x1b" not in capsys.readouterr().err


@pytest.mark.parametrize("host", ["2001:db8::7", "ws-abc-2222.nodus.run", "203.0.113.7"])
def test_nodus_ssh_accepts_ipv6_names_and_ipv4(monkeypatch, host):
    executed = ssh_cli(monkeypatch, {**SSH, "host": host})
    assert cli.main(["ssh", "ws_inst"]) == 0
    assert executed[0][1][-1] == host


def test_nodus_ssh_accepts_an_integer_port(monkeypatch):
    executed = ssh_cli(monkeypatch, {**SSH, "port": 22022})
    assert cli.main(["ssh", "ws_inst"]) == 0
    assert executed[0][1][2] == "22022"


# -- Greptile review: idempotent create, MCP gpu count, CLI wait errors, group pages -----------------------------


def test_keep_files_create_sends_the_launch_key():
    calls = []
    workspace_view = {**INSTANCE_STOPPED, "size_gb": 10,
                      "configuration": {k: v for k, v in {**INSTANCE_CONFIGURATION, "size_gb": 10}.items() if k != "kind"}}
    client = sync_client(launch_handler(calls, itertools.repeat(INSTANCE_READY), create_view=workspace_view))
    client.launch("H100", ssh_key=KEY, keep_files=True, idempotency_key="keep-1", wait=False)
    create = next(call for call in calls if call[:2] == ("POST", BASE))
    assert create[3] == "keep-1" and create[2]["size_gb"] == 10 and "kind" not in create[2]


@pytest.mark.parametrize("keep_files", [False, True])
def test_a_replayed_create_of_a_running_machine_is_not_started_again(keep_files):
    # createResearchWorkspace answers a repeated key and body with the original record and Idempotent-Replayed.
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.headers.get("Idempotency-Key")))
        if request.url.path == BASE + "/capabilities":
            return httpx.Response(200, json=CAPABILITIES)
        if request.url.path == BASE and request.method == "POST":
            return httpx.Response(201, json=INSTANCE_READY, headers={"Idempotent-Replayed": "true"})
        if request.url.path == BASE + "/ws_inst":
            return httpx.Response(200, json=INSTANCE_READY)
        pytest.fail("unexpected " + request.method + " " + request.url.path)

    client = sync_client(handler)
    machine = client.launch("H100", ssh_key=KEY, keep_files=keep_files, idempotency_key="again-1", poll_seconds=0.1)
    assert machine.id == "ws_inst" and machine.ready
    assert not any(path.endswith("/start") for _, path, _ in calls)
    assert [key for method, path, key in calls if method == "POST"] == ["again-1"]


def test_async_keep_files_create_sends_the_launch_key():
    calls = []
    client = async_client(launch_handler(calls, itertools.repeat(INSTANCE_READY)))
    asyncio.run(client.launch("H100", ssh_key=KEY, keep_files=True, idempotency_key="keep-2", wait=False))
    assert next(call for call in calls if call[:2] == ("POST", BASE))[3] == "keep-2"


@pytest.mark.asyncio
async def test_mcp_launch_takes_the_gpu_count_from_the_gpu_spec(api):
    from mcp.shared.memory import create_connected_server_and_client_session
    server, requests, responses = api
    responses.extend([httpx.Response(201, json=INSTANCE_STOPPED), httpx.Response(202, json=INSTANCE_CREATING)])
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("launch_gpu", {"idempotency_key": "k-2", "gpu": "H100:2", "ssh_key": KEY})
        assert not result.isError, result
    assert json.loads(requests[0].content)["gpu_count"] == 2


@pytest.mark.parametrize("failure", [httpx.ConnectError("offline"), httpx.ReadTimeout("slow"), "500"])
def test_cli_launch_wait_failure_names_the_machine_and_how_to_reach_it(monkeypatch, capsys, tmp_path, failure):
    key = tmp_path / "id.pub"
    key.write_text(KEY)

    def handler(request):
        if request.url.path == BASE:
            return httpx.Response(201, json=INSTANCE_STOPPED)
        if request.url.path == BASE + "/ws_inst/start":
            return httpx.Response(202, json=INSTANCE_CREATING)
        if failure == "500":
            return httpx.Response(500, json={"error": {"code": "internal", "message": "boom"}})
        raise failure

    cli_client(monkeypatch, handler)
    assert cli.main(["launch", "--gpu", "H100", "--ssh-key", str(key), "--poll-seconds", "0.1"]) != 0
    err = capsys.readouterr().err
    assert "ws_inst" in err and "nodus stop ws_inst" in err and "nodus ssh ws_inst" in err


def test_compute_group_pages_and_iterate_group_follows_the_cursor():
    member = {**SWEEP_ITEM, "id": "wl_1", "group": None, "resource": {"kind": "workload", "id": "wl_1"}}
    pages = {None: {"group": SWEEP_ITEM["group"], "items": [member], "next_cursor": "g2"},
             "g2": {"group": SWEEP_ITEM["group"], "items": [{**member, "id": "wl_2"}], "next_cursor": None}}
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    client = sync_client(handler)
    assert client.compute.group("sw_1", limit=1, cursor="g2")["items"][0]["id"] == "wl_2"
    assert seen[-1] == {"limit": "1", "cursor": "g2"}
    assert [item["id"] for item in client.compute.iterate_group("sw_1", limit=1)] == ["wl_1", "wl_2"]
    assert seen[-2:] == [{"limit": "1"}, {"limit": "1", "cursor": "g2"}]
    with pytest.raises(ValidationError):
        client.compute.group("sw_1", limit=0)

    async def scenario():
        aclient = async_client(handler)
        first = await aclient.compute.group("sw_1", cursor="g2")
        return first, [item["id"] async for item in aclient.compute.iterate_group("sw_1")]

    first, every = asyncio.run(scenario())
    assert first["items"][0]["id"] == "wl_2" and every == ["wl_1", "wl_2"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_a_replayed_keep_files_create_that_is_stopped_is_never_restarted(asynchronous):
    # A workspace start does not replay by key, so restarting a replayed stopped workspace could rent again.
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == BASE + "/capabilities":
            return httpx.Response(200, json=CAPABILITIES)
        if request.url.path == BASE and request.method == "POST":
            return httpx.Response(201, json=INSTANCE_STOPPED, headers={"Idempotent-Replayed": "true"})
        pytest.fail("unexpected " + request.method + " " + request.url.path)

    client = (async_client if asynchronous else sync_client)(handler)
    with pytest.raises(WorkspaceNotReadyError) as raised:
        result = client.launch("H100", ssh_key=KEY, keep_files=True, idempotency_key="done-1")
        if asynchronous:
            asyncio.run(result)
    assert "ws_inst" in str(raised.value) and "start" in str(raised.value)
    assert not any(path.endswith("/start") for _, path in calls)
