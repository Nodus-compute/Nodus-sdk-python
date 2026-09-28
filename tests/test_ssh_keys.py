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


# -- launch sends the key to one instance only --------------------------------------------------------------


@pytest.fixture
def default_key(tmp_path):
    ssh_dir = tmp_path / "home" / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    (ssh_dir / "id_ed25519.pub").write_text(KEY + "\n")


def launch_calls(*, asynchronous=False, ssh_key=None, wait=True):
    calls = []
    base = launch_handler(calls, itertools.repeat(INSTANCE_READY))

    def handler(request):
        if request.url.path.startswith(KEYS) and request.method != "GET":
            pytest.fail("launch must not change team SSH keys")
        if request.url.path == KEYS:
            return httpx.Response(200, json={"keys": [OTHER]})
        return base(request)

    client = (async_client if asynchronous else sync_client)(handler)
    result = client.launch("H100", ssh_key=ssh_key, idempotency_key="launch-k", poll_seconds=0.1, wait=wait)
    machine = asyncio.run(result) if asynchronous else result
    return machine, calls


@pytest.mark.parametrize("asynchronous", [False, True])
def test_launch_sends_the_default_key_to_that_instance_only(default_key, asynchronous):
    _, calls = launch_calls(asynchronous=asynchronous)
    create = next(call for call in calls if call[:2] == ("POST", BASE))
    assert create[2]["ssh_authorized_key"] == KEY + "\n"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_launch_without_a_local_key_requests_compute_without_one(asynchronous):
    # Direct compute accepts an instance without a key. SSH works once a team key is saved.
    machine, calls = launch_calls(asynchronous=asynchronous)
    create = next(call for call in calls if call[:2] == ("POST", BASE))
    assert "ssh_authorized_key" not in create[2] and create[2]["editor"] == "ssh" and create[2]["kind"] == "instance"
    assert ("POST", BASE + "/ws_inst/start", {}, "launch-k") in calls
    assert machine.id == "ws_inst"


def test_launch_keep_files_without_a_local_key_omits_it_too():
    calls = []
    client = sync_client(launch_handler(calls, itertools.repeat(INSTANCE_READY)))
    client.launch("H100", keep_files=True, wait=False)
    create = next(call for call in calls if call[:2] == ("POST", BASE))
    assert "ssh_authorized_key" not in create[2] and create[2]["editor"] == "ssh"


def test_cli_launch_without_a_local_key_says_how_to_add_one(monkeypatch, capsys):
    calls = []
    cli_client(monkeypatch, launch_handler(calls, itertools.repeat(INSTANCE_CREATING)))
    assert cli.main(["launch", "--gpu", "H100", "--name", "debug-h100"]) == 0
    out = capsys.readouterr().out
    assert GENERATE_HINT in out and "nodus ssh instance-1a2b3c4d" in out
    assert not any(call[1].endswith("/connections") for call in calls)


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


def ssh_with_keys(monkeypatch, team, *, instance_key=None):
    executed = []
    view = {**INSTANCE_READY, "configuration": {**INSTANCE_READY["configuration"]}}
    if instance_key is None:
        view["configuration"].pop("ssh_authorized_key")
    else:
        view["configuration"]["ssh_authorized_key"] = instance_key

    def handler(request):
        if request.url.path == BASE + "/ws_inst":
            return httpx.Response(200, json=view)
        if request.url.path == BASE + "/ws_inst/connections":
            return httpx.Response(200, json=SSH)
        if request.url.path == KEYS:
            return team if isinstance(team, httpx.Response) else httpx.Response(200, json={"keys": team})
        pytest.fail("unexpected " + request.url.path)

    cli_client(monkeypatch, handler)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(cli, "_exec_ssh", lambda argv: executed.append(argv) or 0)
    return executed


HINT = "If the connection is refused, add your key with: nodus ssh-key add"


@pytest.mark.parametrize("team,instance_key", [([SAVED], None), ([], KEY), ([OTHER], KEY + "\n")])
def test_nodus_ssh_is_quiet_when_the_local_key_is_admitted(monkeypatch, capsys, default_key, team, instance_key):
    executed = ssh_with_keys(monkeypatch, team, instance_key=instance_key)
    assert cli.main(["ssh", "ws_inst"]) == 0
    assert executed == [["ssh", "-p", "22022", "-l", "nodus", "--", "203.0.113.7"]]
    assert HINT not in capsys.readouterr().err


@pytest.mark.parametrize("team", [[OTHER], httpx.Response(404, text="404 page not found\n"), []])
def test_nodus_ssh_warns_before_connecting_when_the_local_key_is_not_admitted(monkeypatch, capsys, default_key, team):
    executed = ssh_with_keys(monkeypatch, team, instance_key=OTHER["public_key"])
    assert cli.main(["ssh", "ws_inst"]) == 0
    assert executed and capsys.readouterr().err.strip() == HINT


def test_nodus_ssh_warns_when_there_is_no_local_key(monkeypatch, capsys):
    executed = ssh_with_keys(monkeypatch, [SAVED])
    assert cli.main(["ssh", "ws_inst"]) == 0
    assert executed and capsys.readouterr().err.strip() == HINT


def test_nodus_ssh_returns_the_ssh_exit_code(monkeypatch, default_key):
    ssh_with_keys(monkeypatch, [SAVED])
    monkeypatch.setattr(cli, "_exec_ssh", lambda argv: 255)
    assert cli.main(["ssh", "ws_inst"]) == 255


def test_exec_ssh_replaces_the_process_on_posix_and_waits_on_windows(monkeypatch):
    replaced, waited = [], []
    monkeypatch.setattr(cli.os, "execvp", lambda file, argv: replaced.append((file, argv)))
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: waited.append(argv) or 7)
    monkeypatch.setattr(cli.os, "name", "posix")
    cli._exec_ssh(["ssh", "host"])
    assert replaced == [("ssh", ["ssh", "host"])] and waited == []
    monkeypatch.setattr(cli.os, "name", "nt")
    assert cli._exec_ssh(["ssh", "host"]) == 7 and waited == [["ssh", "host"]]


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


def test_a_broken_default_key_file_is_refused_before_any_request(tmp_path):
    ssh_dir = tmp_path / "home" / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    (ssh_dir / "id_ed25519.pub").write_text(PRIVATE)
    (ssh_dir / "id_rsa.pub").write_text(KEY + "\n")
    client = sync_client(lambda request: pytest.fail("no request expected"))
    with pytest.raises(ValidationError, match="ssh_key"):
        client.launch("H100")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_launch_without_a_key_does_not_wait_for_ssh(asynchronous):
    calls = []
    client = (async_client if asynchronous else sync_client)(
        launch_handler(calls, itertools.repeat(INSTANCE_CREATING)))
    result = client.launch("H100", poll_seconds=0.1, timeout_seconds=5)
    machine = asyncio.run(result) if asynchronous else result
    assert machine.state == "creating"
    assert [call[1] for call in calls] == [BASE, BASE + "/ws_inst/start"]


# -- keyless recovery ------------------------------------------------------------------------------------------

GENERATE_HINT = "No SSH key found. Create and add one with: nodus ssh-key add --generate"


def test_cli_ssh_key_add_without_any_key_points_to_generate(monkeypatch, capsys):
    cli_client(monkeypatch, lambda request: pytest.fail("no request expected"))
    assert cli.main(["ssh-key", "add"]) == 2
    assert GENERATE_HINT in capsys.readouterr().err


def fake_keygen(monkeypatch, runs):
    def run(argv, **kwargs):
        runs.append(argv)
        target = argv[argv.index("-f") + 1]
        with open(target, "w") as private:
            private.write(PRIVATE)
        with open(target + ".pub", "w") as public:
            public.write(KEY + "\n")
        return type("Done", (), {"returncode": 0})()
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name if name == "ssh-keygen" else None)


def test_cli_ssh_key_add_generate_creates_a_key_and_adds_its_public_half(monkeypatch, capsys, tmp_path):
    import os
    import stat
    runs, calls = [], []
    fake_keygen(monkeypatch, runs)
    cli_client(monkeypatch, keys_handler(calls))
    assert cli.main(["ssh-key", "add", "--generate"]) == 0
    home = tmp_path / "home" / ".ssh"
    assert runs == [["/usr/bin/ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(home / "id_ed25519"), "-C", "nodus",
                     "-q"]]
    if os.name != "nt":
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert calls == [("POST", KEYS, {"public_key": KEY + "\n", "name": "id_ed25519.pub"})]
    assert "PRIVATE" not in json.dumps(calls) and FINGERPRINT in capsys.readouterr().out


def test_cli_ssh_key_add_generate_uses_an_existing_default_key(monkeypatch, capsys, default_key):
    runs, calls = [], []
    fake_keygen(monkeypatch, runs)
    cli_client(monkeypatch, keys_handler(calls))
    assert cli.main(["ssh-key", "add", "--generate"]) == 0
    assert runs == [] and calls[0][2]["public_key"] == KEY + "\n"


def test_cli_ssh_key_add_generate_never_overwrites_a_private_key(monkeypatch, capsys, tmp_path):
    ssh_dir = tmp_path / "home" / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    (ssh_dir / "id_ed25519").write_text(PRIVATE)
    runs = []
    fake_keygen(monkeypatch, runs)
    cli_client(monkeypatch, lambda request: pytest.fail("no request expected"))
    assert cli.main(["ssh-key", "add", "--generate"]) == 2
    assert runs == [] and (ssh_dir / "id_ed25519").read_text() == PRIVATE
    assert "id_ed25519" in capsys.readouterr().err


def test_cli_ssh_key_add_generate_needs_ssh_keygen(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("nothing to run"))
    cli_client(monkeypatch, lambda request: pytest.fail("no request expected"))
    assert cli.main(["ssh-key", "add", "--generate"]) == 2
    assert "ssh-keygen" in capsys.readouterr().err


def test_nodus_ssh_still_connects_when_the_local_key_file_is_malformed(monkeypatch, capsys, tmp_path):
    ssh_dir = tmp_path / "home" / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    (ssh_dir / "id_ed25519.pub").write_text("garbage")
    executed = ssh_with_keys(monkeypatch, [SAVED])
    assert cli.main(["ssh", "ws_inst"]) == 0
    assert executed and capsys.readouterr().err.strip() == HINT


def keyless_launch(team, views, *, asynchronous=False, cli_argv=None, monkeypatch=None):
    calls = []
    base = launch_handler(calls, views)

    def handler(request):
        if request.url.path == KEYS:
            calls.append((request.method, request.url.path, None, None))
            return team if isinstance(team, httpx.Response) else httpx.Response(200, json={"keys": team})
        return base(request)

    if cli_argv is not None:
        cli_client(monkeypatch, handler)
        return cli.main(cli_argv), calls
    client = (async_client if asynchronous else sync_client)(handler)
    result = client.launch("H100", poll_seconds=0.1, timeout_seconds=5)
    return (asyncio.run(result) if asynchronous else result), calls


@pytest.mark.parametrize("asynchronous", [False, True])
def test_keyless_launch_waits_for_ssh_when_the_team_has_keys(asynchronous):
    views = itertools.chain([INSTANCE_CREATING], itertools.repeat(INSTANCE_READY))
    machine, calls = keyless_launch([OTHER], views, asynchronous=asynchronous)
    assert machine.ready and calls.count(("GET", BASE + "/ws_inst", None, None)) >= 2


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("missing", [False, True])
def test_keyless_launch_skips_the_wait_without_any_key(asynchronous, missing):
    team = httpx.Response(404, text="404 page not found\n") if missing else []
    machine, calls = keyless_launch(team, itertools.repeat(INSTANCE_CREATING), asynchronous=asynchronous)
    assert machine.state == "creating" and not any(call[1] == BASE + "/ws_inst" for call in calls)


def test_cli_keyless_launch_with_team_keys_prints_the_ready_command(monkeypatch, capsys):
    views = itertools.chain([INSTANCE_CREATING], itertools.repeat(INSTANCE_READY))
    code, _ = keyless_launch([OTHER], views, cli_argv=["launch", "--gpu", "H100", "--poll-seconds", "0.1"],
                             monkeypatch=monkeypatch)
    out = capsys.readouterr().out
    assert code == 0 and "Connect: nodus ssh instance-1a2b3c4d" in out and "ssh -p 22022 nodus@203.0.113.7" in out
    assert GENERATE_HINT not in out


@pytest.mark.parametrize("team", [[], httpx.Response(500, json={"error": {"code": "x", "message": "x"}})])
def test_cli_keyless_launch_without_team_keys_points_to_generate(monkeypatch, capsys, team):
    code, calls = keyless_launch(team, itertools.repeat(INSTANCE_CREATING),
                                 cli_argv=["launch", "--gpu", "H100"], monkeypatch=monkeypatch)
    out = capsys.readouterr().out
    assert code == 0 and GENERATE_HINT in out and "nodus ssh instance-1a2b3c4d" in out
    assert not any(call[1].endswith("/connections") for call in calls)


def test_cli_ssh_key_add_generate_refuses_a_dangling_symlink(monkeypatch, capsys, tmp_path):
    import os
    if not hasattr(os, "symlink") or os.name == "nt":
        pytest.skip("symlinks need privileges on Windows")
    ssh_dir = tmp_path / "home" / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    (ssh_dir / "id_ed25519").symlink_to(tmp_path / "elsewhere")
    runs = []
    fake_keygen(monkeypatch, runs)
    cli_client(monkeypatch, lambda request: pytest.fail("no request expected"))
    assert cli.main(["ssh-key", "add", "--generate"]) == 2
    assert runs == [] and not (tmp_path / "elsewhere").exists()


def test_cli_ssh_key_add_generate_reports_a_failed_ssh_keygen(monkeypatch, capsys):
    import subprocess
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/ssh-keygen")
    seen = {}

    def fail(argv, **kwargs):
        seen.update(kwargs)
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(cli.subprocess, "run", fail)
    cli_client(monkeypatch, lambda request: pytest.fail("no request expected"))
    assert cli.main(["ssh-key", "add", "--generate"]) == 2
    assert "ssh-keygen" in capsys.readouterr().err
    # A key created between the check and ssh-keygen makes it ask to overwrite. No input answers no.
    assert seen.get("stdin") is subprocess.DEVNULL
