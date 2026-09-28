"""Team SSH keys on the wire of the console SSH key handlers, and how launch and nodus ssh use them."""

from __future__ import annotations

import asyncio
import itertools
import json
import sys

import httpx
import pytest

import nodus
from nodus import cli
from nodus.errors import ValidationError
from test_compute_launch import (BASE, INSTANCE_CREATING, INSTANCE_READY, INSTANCE_STOPPED, KEY, SSH, async_client,
                                 cli_client, launch_handler, ssh_cli, sync_client)

KEYS = "/v1/ssh-keys"
FINGERPRINT = "SHA256:2PcGa8b1mUu6yfNODUSexampleFingerprint0000"
# listConsoleSSHKeys: {"keys": [ConsoleUserSSHKey]}, the key re-marshalled without its comment.
SAVED = {"fingerprint": FINGERPRINT, "public_key": " ".join(KEY.split()[:2]), "name": "laptop",
         "added_at": "2026-09-27T10:00:00Z"}
OTHER = {"fingerprint": "SHA256:other", "public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherKey",
         "name": "", "added_at": "2026-09-26T10:00:00Z"}
PRIVATE = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----\n"


def keys_handler(calls, keys=(SAVED,)):
    def handler(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path == KEYS and request.method == "GET":
            return httpx.Response(200, json={"keys": list(keys)})
        if request.url.path == KEYS and request.method == "POST":
            return httpx.Response(201, json={"fingerprint": FINGERPRINT})
        if request.url.path.startswith(KEYS + "/") and request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(418)
    return handler


# -- client.ssh_keys -------------------------------------------------------------------------------------------


def test_ssh_keys_list_add_and_remove_use_the_team_key_routes():
    calls = []
    client = sync_client(keys_handler(calls))
    assert client.ssh_keys.list() == [SAVED]
    assert client.ssh_keys.add(KEY, name="laptop") == {"fingerprint": FINGERPRINT}
    assert calls[-1] == ("POST", KEYS, {"public_key": KEY, "name": "laptop"})
    client.ssh_keys.add(KEY)
    assert calls[-1] == ("POST", KEYS, {"public_key": KEY})
    client.ssh_keys.remove(FINGERPRINT)
    assert calls[-1][:2] == ("DELETE", KEYS + "/" + FINGERPRINT)


@pytest.mark.parametrize("key", [PRIVATE, "not a key", KEY + "\n" + OTHER["public_key"], ""])
def test_ssh_keys_add_takes_exactly_one_public_key(key):
    client = sync_client(lambda request: pytest.fail("no request expected"))
    with pytest.raises(ValidationError):
        client.ssh_keys.add(key)


def test_ssh_keys_remove_escapes_the_base64_fingerprint():
    # ssh.FingerprintSHA256 is unpadded standard base64, so it can hold "/" and "+".
    seen = []
    client = sync_client(lambda request: seen.append(request.url.raw_path) or httpx.Response(204))
    client.ssh_keys.remove("SHA256:ab/c+d")
    assert seen == [b"/v1/ssh-keys/SHA256%3Aab%2Fc%2Bd"]


@pytest.mark.parametrize("fingerprint", ["../workloads", "", "SHA256:a?x=1", "MD5:aa", "SHA256:a b", None])
def test_ssh_keys_remove_refuses_a_fingerprint_that_is_not_one(fingerprint):
    client = sync_client(lambda request: pytest.fail("no request expected"))
    with pytest.raises(ValidationError):
        client.ssh_keys.remove(fingerprint)


def test_async_ssh_keys_match_sync():
    calls = []
    client = async_client(keys_handler(calls))

    async def scenario():
        listed = await client.ssh_keys.list()
        added = await client.ssh_keys.add(KEY, name="laptop")
        await client.ssh_keys.remove(FINGERPRINT)
        return listed, added

    assert asyncio.run(scenario()) == ([SAVED], {"fingerprint": FINGERPRINT})
    assert [call[:2] for call in calls] == [("GET", KEYS), ("POST", KEYS), ("DELETE", KEYS + "/" + FINGERPRINT)]


# -- launch saves the key to the team --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def default_key(tmp_path):
    ssh_dir = tmp_path / "home" / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    (ssh_dir / "id_ed25519.pub").write_text(KEY + "\n")


def launch_with_keys(keys_answer, *, asynchronous=False, ssh_key=None):
    calls = []
    views = itertools.repeat(INSTANCE_READY)
    base = launch_handler(calls, views)

    def handler(request):
        if request.url.path == KEYS:
            body = json.loads(request.content) if request.content else None
            calls.append((request.method, request.url.path, body, None))
            return keys_answer(request)
        return base(request)

    client = (async_client if asynchronous else sync_client)(handler)
    result = client.launch("H100", ssh_key=ssh_key, idempotency_key="launch-k", poll_seconds=0.1)
    if asynchronous:
        asyncio.run(result)
    return calls


@pytest.mark.parametrize("asynchronous", [False, True])
def test_launch_adds_a_local_key_the_team_does_not_have(asynchronous):
    def answer(request):
        if request.method == "GET":
            return httpx.Response(200, json={"keys": [OTHER]})
        return httpx.Response(201, json={"fingerprint": FINGERPRINT})

    calls = launch_with_keys(answer, asynchronous=asynchronous)
    assert ("POST", KEYS, {"public_key": KEY + "\n"}, None) in calls
    create = next(call for call in calls if call[:2] == ("POST", BASE))
    assert create[2]["ssh_authorized_key"] == KEY + "\n"


def test_launch_never_saves_a_key_the_caller_passed():
    # A saved key is admitted by every machine the team runs, so only the caller's own default key is saved.
    calls = launch_with_keys(lambda request: httpx.Response(200, json={"keys": []}), ssh_key=OTHER["public_key"])
    assert not any(call[1] == KEYS for call in calls)


def test_launch_does_not_re_add_a_key_the_team_already_has():
    calls = launch_with_keys(lambda request: httpx.Response(200, json={"keys": [SAVED]}))
    assert [call[0] for call in calls if call[1] == KEYS] == ["GET"]


@pytest.mark.parametrize("status", [404, 500, 403])
def test_launch_continues_with_the_instance_key_when_team_keys_are_unavailable(status):
    calls = launch_with_keys(lambda request: httpx.Response(status, json={"error": {"code": "x", "message": "x"}}))
    assert [call[0] for call in calls if call[1] == KEYS] == ["GET"]
    assert next(call for call in calls if call[:2] == ("POST", BASE))[2]["ssh_authorized_key"] == KEY + "\n"
    assert ("POST", BASE + "/ws_inst/start", {}, "launch-k") in calls


# -- CLI -------------------------------------------------------------------------------------------------------


def test_cli_ssh_key_add_reads_the_default_public_key_and_prints_the_fingerprint(monkeypatch, capsys, tmp_path):
    ssh_dir = tmp_path / "home" / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    (ssh_dir / "id_ed25519").write_text(PRIVATE)
    (ssh_dir / "id_ed25519.pub").write_text(KEY + "\n")
    calls = []
    cli_client(monkeypatch, keys_handler(calls))
    assert cli.main(["ssh-key", "add"]) == 0
    out = capsys.readouterr().out
    assert FINGERPRINT in out and "Running instances accept it within a few seconds." in out
    assert calls == [("POST", KEYS, {"public_key": KEY + "\n", "name": "id_ed25519.pub"})]


def test_cli_ssh_key_add_refuses_a_private_key_file(monkeypatch, capsys, tmp_path):
    private = tmp_path / "id_ed25519"
    private.write_text(PRIVATE)
    cli_client(monkeypatch, lambda request: pytest.fail("no request expected"))
    assert cli.main(["ssh-key", "add", str(private)]) == 2
    err = capsys.readouterr().err
    assert "public key" in err and "b3BlbnNzaC1rZXktdjEAAAAA" not in err


def test_cli_ssh_key_ls_and_rm(monkeypatch, capsys):
    calls = []
    cli_client(monkeypatch, keys_handler(calls, keys=(SAVED, {**OTHER, "name": "evil\x1b]0;x\x07"})))
    assert cli.main(["ssh-key", "ls"]) == 0
    out = capsys.readouterr().out
    assert FINGERPRINT in out and "laptop" in out and "\x1b" not in out
    assert cli.main(["ssh-key", "rm", FINGERPRINT]) == 0
    assert calls[-1][:2] == ("DELETE", KEYS + "/" + FINGERPRINT)
    assert FINGERPRINT in capsys.readouterr().out


def test_nodus_ssh_hints_when_the_server_requires_a_key(monkeypatch, capsys):
    def handler(request):
        if request.url.path == BASE + "/ws_inst":
            return httpx.Response(200, json=INSTANCE_READY)
        # writeSandboxError: a flat sandbox.Error body.
        return httpx.Response(409, json={"code": "workspace_ssh_key_required",
                                         "message": "No SSH key admits anyone to this workspace.",
                                         "fix": "Add your SSH public key under Settings, then start compute again.",
                                         "url": "https://nodus-compute.ai/docs/sandboxes/errors"})

    cli_client(monkeypatch, handler)
    assert cli.main(["ssh", "ws_inst"]) != 0
    assert "Add your key with: nodus ssh-key add" in capsys.readouterr().err


def test_nodus_ssh_hints_after_a_publickey_denial(monkeypatch, capsys):
    ran = ssh_cli(monkeypatch, SSH)
    monkeypatch.setattr(cli, "_run_ssh", lambda argv: ran.append(("ssh", argv)) or (255, True))
    assert cli.main(["ssh", "ws_inst"]) == 255
    assert ran == [("ssh", ["ssh", "-p", "22022", "-l", "nodus", "--", "203.0.113.7"])]
    assert capsys.readouterr().err.strip().splitlines()[-1] == "Add your key with: nodus ssh-key add"


def test_nodus_ssh_returns_the_session_exit_code_without_a_hint(monkeypatch, capsys):
    ssh_cli(monkeypatch, SSH)
    monkeypatch.setattr(cli, "_run_ssh", lambda argv: (0, False))
    assert cli.main(["ssh", "ws_inst"]) == 0
    assert "ssh-key" not in capsys.readouterr().err


def test_run_ssh_returns_when_ssh_exits_even_if_a_child_holds_stderr():
    import time
    started = time.monotonic()
    script = "(sleep 5) & echo 'Permission denied (publickey).' >&2; exit 0"
    assert cli._run_ssh(["sh", "-c", script]) == (0, True)
    assert time.monotonic() - started < 3


def test_run_ssh_reports_a_signal_as_a_shell_exit_code():
    assert cli._run_ssh(["sh", "-c", "kill -INT $$"])[0] == 130


def test_run_ssh_forwards_stderr_and_spots_a_publickey_denial(capfd):
    script = "import sys; sys.stderr.write('nodus@203.0.113.7: Permission denied (publickey).\\n'); sys.exit(255)"
    assert cli._run_ssh([sys.executable, "-c", script]) == (255, True)
    assert "Permission denied (publickey)." in capfd.readouterr().err
    assert cli._run_ssh([sys.executable, "-c", "import sys; sys.stderr.write('bye\\n')"]) == (0, False)


# -- MCP -------------------------------------------------------------------------------------------------------


@pytest.fixture
def api(monkeypatch):
    from nodus import _mcp
    from nodus.config import save_credentials
    save_credentials("saved-test-key", "https://api.example.test")
    requests, responses = [], []

    def respond(request):
        requests.append(request)
        return responses.pop(0) if responses else httpx.Response(201, json={"fingerprint": FINGERPRINT})

    client = httpx.AsyncClient
    monkeypatch.setattr(_mcp.httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(respond), **kwargs))
    return _mcp.create_server(), requests, responses


@pytest.mark.asyncio
async def test_mcp_add_ssh_key_posts_one_public_key(api):
    from mcp.shared.memory import create_connected_server_and_client_session
    server, requests, _ = api
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("add_ssh_key", {"public_key": KEY, "name": "agent laptop"})
        assert not result.isError, result
        assert json.loads(result.content[0].text) == {"fingerprint": FINGERPRINT}
        again = await session.call_tool("add_ssh_key", {"public_key": KEY})
        assert not again.isError, again
    assert (requests[0].method, requests[0].url.path) == ("POST", KEYS)
    assert json.loads(requests[0].content) == {"public_key": KEY, "name": "agent laptop"}
    assert json.loads(requests[1].content) == {"public_key": KEY}


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"public_key": PRIVATE}, {"public_key": "ssh-ed25519"},
                                       {"public_key": KEY, "name": "x" * 101}])
async def test_mcp_add_ssh_key_refuses_before_network(api, arguments):
    from mcp.shared.memory import create_connected_server_and_client_session
    server, requests, _ = api
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("add_ssh_key", arguments)
        assert result.isError
    assert requests == []
