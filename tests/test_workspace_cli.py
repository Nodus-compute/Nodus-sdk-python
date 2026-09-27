"""CLI journeys for GPU workspaces."""

from __future__ import annotations

import itertools
import json

import httpx
import pytest

import nodus
from nodus import cli
from test_gpu_workspaces import BASE, CAPABILITIES, CREATING, RECEIPT, RUNNING, SSH, STOPPED, STOPPING


def client_factory(handler):
    def build(**_kwargs):
        client = nodus.Client(api_key="nk_live_test", base_url="https://nodus.invalid", max_retries=0)
        client._http = httpx.Client(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler),
                                    headers={"Authorization": "Bearer nk_live_test"})
        return client
    return build


def test_workspace_cli_creates_starts_connects_runs_and_stops(monkeypatch, capsys):
    calls = []
    views = itertools.chain([STOPPED, CREATING], itertools.repeat(RUNNING))

    def handler(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body, request.headers.get("Idempotency-Key")))
        if request.url.path == BASE + "/capabilities":
            return httpx.Response(200, json=CAPABILITIES)
        if request.url.path == BASE and request.method == "POST":
            return httpx.Response(201, json=STOPPED)
        if request.url.path == BASE:
            return httpx.Response(200, json={"workspaces": [RUNNING], "next_cursor": ""})
        if request.url.path == BASE + "/ws_kernel":
            return httpx.Response(200, json=next(views))
        if request.url.path == BASE + "/ws_kernel/start":
            return httpx.Response(202, json=CREATING)
        if request.url.path == BASE + "/ws_kernel/connections":
            return httpx.Response(200, json=SSH if body["tool"] == "ssh" else {"url": "https://ws-abc-8080.nodus.run/?tkn=t"})
        if request.url.path == BASE + "/ws_kernel/workloads":
            return httpx.Response(202, json=RECEIPT)
        if request.url.path == BASE + "/ws_kernel/stop":
            return httpx.Response(202, json=STOPPING)
        pytest.fail("unexpected " + request.url.path)

    monkeypatch.setattr(cli, "Client", client_factory(handler))
    assert cli.main(["workspace", "new", "kernel-lab", "--gpu", "H100", "--max-hours", "4"]) == 0
    assert capsys.readouterr().out.strip() == "ws_kernel"
    assert calls[1][2]["gpu"] == "H100" and calls[1][2]["max_hours"] == 4 and calls[1][2]["size_gb"] == 10
    assert cli.main(["workspace", "start", "--idempotency-key", "session-1", "--wait", "--poll-seconds", "0.1", "ws_kernel"]) == 0
    out = capsys.readouterr().out
    started = [call for call in calls if call[1] == BASE + "/ws_kernel/start"]
    assert "running" in out and started[0][3] == "session-1"
    assert cli.main(["workspace", "ls"]) == 0
    assert "ws_kernel" in capsys.readouterr().out
    assert cli.main(["workspace", "connect", "ws_kernel", "--tool", "editor"]) == 0
    assert capsys.readouterr().out.strip() == "https://ws-abc-8080.nodus.run/?tkn=t"
    assert cli.main(["workspace", "ssh", "ws_kernel"]) == 0
    ssh_out = capsys.readouterr().out
    assert ssh_out.startswith("ssh ") and "Host nodus-kernel-lab" in ssh_out and "vscode://" in ssh_out
    assert cli.main(["workspace", "run", "--idempotency-key", "train-1", "--budget", "5", "ws_kernel", "python train.py"]) == 0
    assert capsys.readouterr().out.strip() == "wl_train"
    assert calls[-1][2] == {"command": "python train.py", "budget_usd": 5.0} and calls[-1][3] == "train-1"
    assert cli.main(["workspace", "stop", "--idempotency-key", "stop-1", "ws_kernel"]) == 0
    assert capsys.readouterr().out.strip() == "ws_kernel stopping"
    assert calls[-1][2] == {"session_id": "sb_session"} and calls[-1][3] == "stop-1"


def test_workspace_cli_run_requires_a_budget_and_new_requires_hours():
    with pytest.raises(SystemExit) as error:
        cli.build_parser().parse_args(["workspace", "run", "ws_kernel", "python x.py"])
    assert error.value.code == 2
    with pytest.raises(SystemExit) as error:
        cli.build_parser().parse_args(["workspace", "new", "lab", "--gpu", "H100"])
    assert error.value.code == 2


def test_workspace_cli_upload_and_download_use_the_handle(monkeypatch, capsys, tmp_path):
    seen = {}

    def upload(self, directory, *, idempotency_key=None, poll_seconds=2.0, timeout_seconds=600.0):
        seen["upload"] = (self.id, str(directory), idempotency_key)
        return {"id": "tr_1", "state": "verifying"}

    def download(self, destination, *, overwrite=False):
        seen["download"] = (self.id, str(destination), overwrite)
        return destination

    monkeypatch.setattr(nodus.Workspace, "upload", upload)
    monkeypatch.setattr(nodus.Workspace, "download", download)
    monkeypatch.setattr(cli, "Client", client_factory(lambda request: httpx.Response(200, json=STOPPED)))
    assert cli.main(["workspace", "upload", "--idempotency-key", "up-1", "ws_kernel", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "ws_kernel verifying"
    assert cli.main(["workspace", "download", "ws_kernel", str(tmp_path / "saved.tar")]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "saved.tar")
    assert seen == {"upload": ("ws_kernel", str(tmp_path), "up-1"),
                    "download": ("ws_kernel", str(tmp_path / "saved.tar"), False)}


def test_workspace_cli_not_ready_is_a_plain_message(monkeypatch, capsys):
    def handler(request):
        return httpx.Response(409, json={"error": "workspace_not_ready", "message": "This workspace is not ready to connect.",
                                         "fix": "Wait for the GPU and its tools to finish starting."})
    monkeypatch.setattr(cli, "Client", client_factory(handler))
    assert cli.main(["workspace", "connect", "ws_kernel"]) == 2
    err = capsys.readouterr().err
    assert "not ready to connect" in err and "finish starting" in err


@pytest.mark.parametrize("state,meter,expected", [
    ("stopped", {"charge_state": "estimated", "total_now_usd": 0}, "Finalizing cost"),
    ("failed", {"charge_state": "estimated", "total_now_usd": 0.2}, "Finalizing cost"),
    ("running", {"charge_state": "estimated", "total_now_usd": 0.2}, "$0.20"),
    ("stopped", {"charge_state": "final", "final_charge_usd": 1.25, "total_now_usd": 1.25}, "$1.25"),
    ("stopped", {"charge_state": "final", "final_charge_usd": 0, "total_now_usd": 0}, "$0.00"),
    ("stopped", {"total_now_usd": 0.2}, "$0.20"),
    ("stopped", None, "-"),
    ("stopped", {"charge_state": "final", "total_now_usd": 0.2}, "Not available"),
])
def test_workspace_cli_lists_pending_final_and_legacy_cost(monkeypatch, capsys, state, meter, expected):
    view = {**STOPPED, "state": state, "meter": meter,
            "session": {"id": "sb_session", "state": "ready" if state == "running" else "terminated"}}
    def handler(request):
        assert request.method == "GET" and request.url.path == BASE
        return httpx.Response(200, json={"workspaces": [view], "next_cursor": ""})
    monkeypatch.setattr(cli, "Client", client_factory(handler))
    assert cli.main(["workspace", "ls"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.endswith("  " + expected)
    if expected in ("Finalizing cost", "Not available"):
        assert "$" not in out
