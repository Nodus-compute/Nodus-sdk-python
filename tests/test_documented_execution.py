"""Execute documentation through the installed SDK and a local HTTP test server.

The server simulates completion. These checks do not execute GPU containers.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import sysconfig
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import nodus
import httpx
import pytest
from test_agent_steps import journal_socket

ROOT = Path(__file__).parents[1]
DATA = b'{"sum": 6, "sum_of_squares": 385, "gpu": "Synthetic GPU"}\n'
DIGEST = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def docs_api(monkeypatch):
    calls = []
    submissions = []
    file_output = {}
    workspace_upload = {}
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as saved:
        entry = tarfile.TarInfo("result.json")
        entry.size = len(DATA)
        saved.addfile(entry, io.BytesIO(DATA))
    archive_bytes = archive.getvalue()
    segment = {"sha256": hashlib.sha256(archive_bytes).hexdigest(), "offset": 0, "bytes": len(archive_bytes)}
    manifest = {"version": 3, "archive_sha256": segment["sha256"], "archive_bytes": len(archive_bytes), "segments": [segment]}

    def download(request):
        assert str(request.url) == "https://workspace-objects.invalid/saved-project"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, content=archive_bytes)

    import nodus._workspace_files as workspace_files
    monkeypatch.setattr(workspace_files, "_download_client", lambda asynchronous: httpx.Client(transport=httpx.MockTransport(download)))
    row = {"id": "wl_docs", "status": "completed", "revision": 2,
           "spend_usd": 0.01, "meter": {"total_now_usd": 0.01},
           "route": {"sku": "nodus:test", "region": "test-region", "expected_cost_usd": 0.01}}
    sandbox = {
        "id": "sb_docs", "state": "ready", "envelope": {}, "cost_usd": 0.01,
        "network_usage": {"sent_bytes": 123, "received_bytes": 456},
        "url": "https://console.nodus-compute.ai/sandboxes/sb_docs",
        "created_at": "2026-09-13T12:00:00Z", "updated_at": "2026-09-13T12:00:01Z",
        "last_activity_at": "2026-09-13T12:00:01Z", "terminal_at": None,
    }
    execution = {
        "id": "sx_docs", "sandbox_id": "sb_docs", "state": "running",
        "spec": {"command": ["python", "agent.py"], "cwd": "", "env": {}, "timeout_s": 120, "stdin": True},
        "created_at": "2026-09-13T12:00:02Z", "updated_at": "2026-09-13T12:00:03Z",
        "dispatched_at": "2026-09-13T12:00:02Z", "deadline_at": "2026-09-13T12:02:02Z",
        "started_at": "2026-09-13T12:00:03Z", "completed_at": None, "exit_code": None,
        "failure_code": "", "output_sequence": 0, "stdout_bytes": 0, "stderr_bytes": 0,
        "final_output_sequence": None, "cancel_requested_at": None, "cancel_reason": "",
    }

    connection = {"id": "conn_docs", "name": "lab-db", "kind": "neon", "secret_id": "sec_docs",
                  "secret_version": 1, "scope": "read", "region": "us-east-1", "egress_hosts": [],
                  "live_mode": False, "verified_at": "2026-09-19T00:00:00Z",
                  "created_at": "2026-09-19T00:00:00Z", "created_by": "key_docs"}

    workspace_state = {"state": "stopped"}

    def workspace_view():
        running = workspace_state["state"] == "running"
        return {"id": "ws_docs", "name": "kernel-lab", "size_gb": 10, "holder_id": None, "saved_at": None,
                "stored_bytes": None, "last_error": "", "saving_for_termination": False,
                "billing_status": "metered_subject_to_account_limits",
                "configuration": {"name": "kernel-lab", "environment": "pytorch-cuda", "editor": "vscode", "gpu": "H100",
                                  "gpu_count": 1, "gpu_memory_gb": 80, "budget_usd": 0, "max_hours": 4, "size_gb": 10},
                "configuration_revision": "c" * 64, "storage_revision": 1,
                "storage_policy_version": "r2-standard-10gb-account-v1",
                "session": {"id": "sb_session", "state": "ready"} if running else None,
                "meter": {"compute_settled_usd": 0.5, "compute_accruing_usd": 0.25} if running else None,
                "state": workspace_state["state"],
                "status_message": "Compute is running." if running else "Saved project files are ready for the next session.",
                "connections": {"editor": running, "notebook": running, "ssh": False},
                "storage": {"retained_bytes": len(DATA), "billable_bytes": 0}, "pending_upload": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, body, status=200, headers=None):
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Content-Type", "application/json")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlsplit(self.path).path
            calls.append(("GET", path))
            if self.headers.get("Authorization") != "Bearer nk_docs":
                return self.reply({"error": "unauthorized"}, 401)
            if path == "/v1/research-workspaces/storage":
                return self.reply({"retained_bytes": len(DATA), "billable_bytes": 0})
            if path == "/v1/research-workspaces/capabilities":
                return self.reply({"available": True, "storage_limit_bytes": 10_000_000_000, "environments": ["pytorch-cuda"],
                                   "gpu_counts": [1, 2, 4, 8], "editors": ["vscode", "jupyter", "ssh"],
                                   "storage_policy_version": "r2-standard-10gb-account-v1"})
            if path == "/v1/research-workspaces":
                return self.reply({"workspaces": [workspace_view()], "next_cursor": ""})
            if path == "/v1/research-workspaces/ws_docs":
                return self.reply(workspace_view())
            if path == "/v1/sandboxes/capabilities":
                return self.reply({"available": True, "default_template": "nodus:agent-tools-v1", "templates": [
                    {"id": "nodus:agent-tools-v1", "available": True, "max_project_bytes": 1048576}]})
            if path.endswith("/execs/sx_files/stream"):
                return self.reply({"frames": [{"sequence": 1, "stream": "stdout", "offset": 0,
                    "data": base64.b64encode(json.dumps(file_output).encode()).decode()}],
                    "next_sequence": 1, "last_sequence": 1, "final_sequence": 1, "state": "completed", "done": True, "complete": True})
            if path == "/v1/pools/pool_docs/proposals":
                query = parse_qs(urlsplit(self.path).query)
                if query != {"limit": ["25"], "state": ["pending"]}:
                    return self.reply({"error": "invalid_query"}, 400)
                return self.reply({"pool_id": "pool_docs", "proposals": [], "next_cursor": None})
            if path == "/v1/pools/pool_docs/recommendations":
                query = parse_qs(urlsplit(self.path).query)
                if query.get("limit") != ["25"] or query.get("state") != ["expired"]:
                    return self.reply({"error": "invalid_query"}, 400)
                cursor = query.get("cursor", [None])[0]
                if cursor not in (None, "docs_next"):
                    return self.reply({"error": "invalid_cursor"}, 400)
                return self.reply({"pool_id": "pool_docs", "predict_enabled": False,
                    "refresh_status": "disabled", "recommendations": [],
                    "next_cursor": "docs_next" if cursor is None else None})
            if path == "/v1/connections":
                return self.reply({"connections": [connection]})
            if path == "/v1/connections/conn_docs":
                return self.reply(connection)
            if path == "/v1/assets/asset_docs":
                return self.reply({"id": "asset_docs", "state": "ready", "name": "data.parquet", "kind": "connection_query",
                                   "export": {"id": "export_docs", "connection_id": "conn_docs", "asset_id": "asset_docs", "query_hash": "a" * 64,
                                              "format": "parquet", "row_count": 10, "bytes": 256, "created_at": "2026-09-19T00:00:00Z"}})
            if path == "/v1/assets":
                return self.reply({"assets": [], "upload_idempotency": True, "max_import_bytes": 1048576})
            if path == "/v1/workloads":
                return self.reply({"workloads": [row]})
            if path == "/v1/workloads/wl_docs":
                return self.reply(row)
            if path in ("/v1/sandboxes/sb_docs/events", "/v1/sandboxes/sb_example/events"):
                after = int(parse_qs(urlsplit(self.path).query).get("after", ["0"])[0])
                return self.reply({"events": [] if after >= 1 else [{"id": 1, "event_type": "sandbox.egress_denied", "payload": {"hostname": "example.com", "count": 2, "generation": 1}}]})
            if path in ("/v1/sandboxes/sb_docs", "/v1/sandboxes/sb_example"):
                return self.reply({**sandbox, "id": path.rsplit("/", 1)[-1]})
            if path.endswith("/execs/sx_docs/stream"):
                return self.reply({
                    "frames": [{
                        "sequence": 1, "stream": "stdout", "offset": 0,
                        "data": base64.b64encode(b"tool result\n").decode(),
                        "created_at": "2026-09-13T12:00:04Z",
                    }],
                    "next_sequence": 1, "last_sequence": 1, "final_sequence": 1,
                    "state": "completed", "done": True, "complete": True,
                })
            if path.endswith("/execs/sx_docs"):
                return self.reply({**execution, "state": "completed", "exit_code": 0, "final_output_sequence": 1})
            suffix = path.removeprefix("/v1/workloads/wl_docs/")
            responses = {
                "logs": b"GPU output fixture\n",
                "events": {"events": [] if int(parse_qs(urlsplit(self.path).query).get("after", ["0"])[0]) >= 1 else [{"id": 1, "event_type": "workload.completed", "payload": {}}]},
                "artifacts": {"artifacts": []},
                "outputs": {"outputs": [{"name": "result", "stage_id": "summarize", "bytes": len(DATA), "sha256": DIGEST}]},
                "routing": {"placements": []},
                "ledger": {"entries": [], "charged_usd": 0.01, "settlement": {"status": "settled", "balance_usd": 0}},
            }
            if suffix == "outputs/result":
                return self.reply(DATA, headers={"X-Nodus-SHA256": DIGEST})
            if suffix in responses:
                return self.reply(responses[suffix])
            return self.reply({"error": "not_found", "message": path}, 404)

        def do_DELETE(self):
            path = urlsplit(self.path).path
            calls.append(("DELETE", path))
            if self.headers.get("Authorization") != "Bearer nk_docs":
                return self.reply({"error": "unauthorized"}, 401)
            if path == "/v1/connections/conn_docs":
                return self.reply(b"", 204)
            if path == "/v1/research-workspaces/ws_docs/files":
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                assert payload == {"storage_revision": 1}
                return self.reply({"workspace_id": "ws_docs", "storage_revision": 2, "deleted": True})
            return self.reply({"error": "not_found"}, 404)

        def do_PUT(self):
            path = urlsplit(self.path).path
            calls.append(("PUT", path))
            assert self.headers.get("Authorization") == "Bearer nk_docs"
            assert path.startswith("/v1/research-workspaces/ws_docs/transfers/transfer_docs/segments/")
            index = int(path.rsplit("/", 1)[1])
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            expected = workspace_upload["manifest"]["segments"][index]
            assert len(data) == expected["bytes"] and hashlib.sha256(data).hexdigest() == expected["sha256"]
            assert index not in workspace_upload["uploaded"]
            workspace_upload["uploaded"].add(index)
            return self.reply(b"", 204)

        def do_PATCH(self):
            path = urlsplit(self.path).path
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            calls.append(("PATCH", path))
            if self.headers.get("Authorization") != "Bearer nk_docs":
                return self.reply({"error": "unauthorized"}, 401)
            if path != "/v1/pools/pool_docs":
                return self.reply({"error": "not_found"}, 404)
            if payload.get("route_enabled") is True and (payload.get("accepted_route_rate_version") != "route-platform-v1" or payload.get("accepted_route_rate_micros") != 20000):
                return self.reply({"error": "route_rate_consent"}, 409)
            return self.reply({"id": "pool_docs", "name": "Research", "kind": "hosts", "state": "active",
                "route_enabled": True, "platform_rate_micros": 20000, "route_price_version": "route-platform-v1", **payload})

        def do_POST(self):
            path = urlsplit(self.path).path
            if self.headers.get("Transfer-Encoding") == "chunked":
                body = bytearray()
                while True:
                    count = int(self.rfile.readline().strip(), 16)
                    if count == 0:
                        self.rfile.readline()
                        break
                    body.extend(self.rfile.read(count))
                    self.rfile.read(2)
                raw = bytes(body)
            else:
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if path in ("/v1/assets/upload", "/v1/assets/import"):
                if self.headers.get("Authorization") != "Bearer nk_docs":
                    return self.reply({"error": "unauthorized"}, 401)
                if path == "/v1/assets/import" and json.loads(raw).get("kind") == "connection_query":
                    payload = json.loads(raw)
                    assert payload["connection_id"] == "lab-db"
                    assert payload["sql"].startswith("SELECT")
                    return self.reply({"id": "asset_docs", "state": "ready", "name": "data.parquet", "kind": "connection_query",
                                       "export": {"id": "export_docs", "connection_id": "conn_docs", "asset_id": "asset_docs", "query_hash": "a" * 64,
                                                  "format": "parquet", "row_count": 10, "bytes": 256, "created_at": "2026-09-19T00:00:00Z"}}, 201)
                return self.reply({"id": "asset_docs", "state": "ready", "name": "fixture", "kind": "upload"}, 201)
            payload = json.loads(raw or b"{}")
            calls.append(("POST", path))
            if path == "/v1/sandboxes/sb_docs/agent-runs":
                return self.reply({**payload, "status": "active", "epoch": 0}, 201)
            if path == "/v1/console/device/start":
                return self.reply({"device_code": "device-test", "user_code": "ABCD-EFGH",
                    "verification_url": "https://console.nodus-compute.ai/device?code=ABCD-EFGH", "interval": 1, "expires_in": 60})
            if path == "/v1/console/device/token":
                return self.reply({"api_key": "nk_docs", "base_url": address, "tenant": "docs-test"})
            if self.headers.get("Authorization") != "Bearer nk_docs":
                return self.reply({"error": "unauthorized"}, 401)
            if path == "/v1/research-workspaces":
                assert payload["gpu"] == "H100" and payload["max_hours"] == 4 and payload["size_gb"] == 10
                return self.reply(workspace_view(), 201)
            if path == "/v1/research-workspaces/ws_docs/start":
                assert self.headers.get("Idempotency-Key")
                workspace_state["state"] = "running"
                return self.reply(workspace_view(), 202)
            if path == "/v1/research-workspaces/ws_docs/stop":
                assert self.headers.get("Idempotency-Key") and payload == {"session_id": "sb_session"}
                workspace_state["state"] = "stopped"
                return self.reply(workspace_view(), 202)
            if path == "/v1/research-workspaces/ws_docs/connections":
                assert payload["tool"] in ("editor", "notebook", "ssh")
                return self.reply({"url": "https://ws-docs-8080.nodus.run/?tkn=docs"})
            if path == "/v1/research-workspaces/ws_docs/workloads":
                assert self.headers.get("Idempotency-Key") and payload["budget_usd"] > 0
                submissions.append(payload)
                return self.reply({"id": "wl_docs", "workload_id": "wl_docs", "status": "accepted", "revision": 1}, 202)
            if path == "/v1/research-workspaces/ws_docs/transfers":
                assert payload["idempotency_key"] == "project-upload-1" or payload["idempotency_key"].startswith("nodus-")
                workspace_upload.update(manifest=payload["manifest"], uploaded=set())
                canonical = json.dumps(payload["manifest"], separators=(",", ":")).encode()
                return self.reply({"id": "transfer_docs", "workspace_id": "ws_docs", "state": "uploading",
                    "manifest_sha256": hashlib.sha256(canonical).hexdigest(), "manifest_bytes": len(canonical), "uploaded_segments": []}, 201)
            if path == "/v1/research-workspaces/ws_docs/transfers/transfer_docs/finalize":
                assert workspace_upload["uploaded"] == set(range(len(workspace_upload["manifest"]["segments"])))
                return self.reply({"id": "transfer_docs", "workspace_id": "ws_docs", "state": "queued"}, 202)
            if path == "/v1/research-workspaces/ws_docs/files/export":
                assert payload == {"storage_revision": 1}
                return self.reply({"id": "export_docs", "workspace_id": "ws_docs", "storage_revision": 1,
                    "format": "research-archive-v3", "manifest": manifest,
                    "segments": [{"index": 0, **segment, "url": "https://workspace-objects.invalid/saved-project"}]})
            if path == "/v1/agents":
                assert payload["entrypoint"] == "agent:main"
                if "bootstrap" in payload:
                    assert payload["bootstrap"] == {"repo": "your-org/private-agent", "ref": "main"}
                    assert payload["network_permissions"] == ["github"] and "source" not in payload
                else:
                    assert payload["source"] == {"asset_id": "asset_docs"}
                return self.reply({"id": "ag_docs", "name": payload["name"], "status": "active", "budget_usd": 20,
                                   "current_revision": 1, "url": "https://console.nodus-compute.ai/?view=agent-deployments&agent=ag_docs"}, 202)
            if path == "/v1/agents/ag_docs/runs":
                assert payload["input"] == {"values": [1, 2, 3]}
                return self.reply({"id": "run_docs", "agent_id": "ag_docs", "status": "queued", "input": payload["input"]}, 202)
            if path == "/v1/sandboxes/sb_docs/files":
                file_output.clear()
                file_output.update(operation=payload["operation"], path=payload["path"])
                if payload["operation"] == "stat":
                    file_output.update(type="file", size_bytes=len(DATA))
                elif payload["operation"] == "read":
                    file_output.update(data=base64.b64encode(DATA).decode(), offset=0, next_offset=len(DATA),
                                       size_bytes=len(DATA), sha256=DIGEST, eof=True)
                return self.reply({**execution, "id": "sx_files", "state": "completed", "exit_code": 0}, 202)
            if path == "/v1/sandboxes/sb_docs/sleep":
                return self.reply({**sandbox, "state": "suspended"}, 202)
            if path == "/v1/connections":
                assert payload == {"name": "lab-db", "kind": "neon", "secret": "LAB_DB", "scope": "read", "region": "us-east-1", "live_mode": False}
                return self.reply(connection, 201)
            if path == "/v1/workloads/wl_docs/outputs/results/reload":
                return self.reply({"id": "load_docs", "workload_id": "wl_docs", "stage": "main", "generation": 1, "name": "results", "table": "eval_results", "rows": 0, "state": "pending"}, 202)
            if path == "/v1/connections/conn_docs/verify":
                return self.reply(connection)
            if path == "/v1/pools/pool_docs/enrollment-tokens":
                if payload != {"mode": "execute", "host_id": "host_docs"}:
                    return self.reply({"error": "invalid_enrollment"}, 400)
                return self.reply({"id": "pet_docs", "mode": "execute", "token": "synthetic-token", "expires_at": "2026-09-18T12:00:00Z"}, 201)
            if path == "/v1/workloads":
                submissions.append(payload)
                if not self.headers.get("Idempotency-Key"):
                    return self.reply({"error": "invalid_brief"}, 400)
                return self.reply({"id": "wl_docs", "workload_id": "wl_docs", "status": "accepted", "revision": 1}, 202)
            if path == "/v1/workspaces":
                assert payload == {"name": "research", "size_gb": 0.1}
                return self.reply({
                    "id": "ws_docs", "name": "research", "size_gb": 0.1,
                    "holder_id": None, "saved_at": None, "stored_bytes": None,
                    "last_error": "", "saving_for_termination": False,
                    "billing_status": "disabled_no_approved_storage_rate",
                }, 201)
            if path == "/v1/sandboxes":
                return self.reply(sandbox, 202)
            if path in ("/v1/sandboxes/sb_docs/exec", "/v1/sandboxes/sb_example/exec"):
                sandbox_id = path.split("/")[3]
                return self.reply({**execution, "sandbox_id": sandbox_id, "spec": {**execution["spec"], **payload}}, 202)
            if path.endswith("/execs/sx_docs/stdin"):
                data = base64.b64decode(payload.get("data", ""))
                return self.reply({"sequence": 1, "bytes": len(data), "eof": bool(payload.get("eof")), "created_at": "2026-09-13T12:00:04Z"}, 202)
            if path in ("/v1/sandboxes/sb_docs/terminate", "/v1/sandboxes/sb_example/terminate"):
                sandbox_id = path.split("/")[3]
                return self.reply({**sandbox, "id": sandbox_id, "state": "terminated"})
            if path == "/v1/workloads/wl_docs/cancel":
                return self.reply({"status": "cancel_requested"}, 202)
            return self.reply({"error": "not_found", "message": path}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    address = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("NODUS_BASE_URL", address)
    monkeypatch.setenv("NODUS_API_KEY", "nk_docs")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    try:
        yield address, calls, submissions
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def blocks():
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]:
        for number, match in enumerate(re.finditer(r'^```python\n(.*?)^```', path.read_text(), re.M | re.S)):
            yield path, number, match.group(1)


EXAMPLES = list(blocks())


@pytest.mark.parametrize("path,number,body", EXAMPLES, ids=[f"{p.relative_to(ROOT)}:{n + 1}" for p, n, _ in EXAMPLES])
def test_python_documentation_executes(path, number, body, docs_api, tmp_path, monkeypatch, request):
    if path.name == "containers-and-scripts.md" and number == 0:
        pytest.skip("container-side CUDA program requires a GPU and PyTorch image")
    monkeypatch.chdir(tmp_path)
    for name in ("hello.py", "train.py", "data.csv"):
        (tmp_path / name).write_text("test fixture")
    if path.name == "workspaces.md" and number in (0, 1):
        (tmp_path / "project").mkdir()
        (tmp_path / "project" / "train.py").write_text("print('saved')\n")
    from nodus._workload_file import write_workload_file
    write_workload_file(tmp_path / "train.toml")
    with nodus.Client() as client:
        sandbox_handle = client.sandboxes.from_id("sb_docs")
        execution_handle = sandbox_handle.exec(["python", "agent.py"], stdin=True)
        namespace = {"__name__": "__docs__", "client": client, "nodus": nodus,
                     "workload_id": "wl_docs", "pool_id": "pool_docs", "host_id": "host_docs", "allowed_regions": ["test-region"],
                     "done": client.get("wl_docs"), "workload": client.get("wl_docs"),
                     "sandbox": sandbox_handle, "execution": execution_handle}
        if path.name == "durable-steps.md" and number == 1:
            journal, state = request.getfixturevalue("journal_socket")
            state["run_id"] = "invoice:42"
            state["input"] = {"invoice_id": 42}
            namespace["send_to_invoice_service"] = lambda invoice_id: "synthetic-remote-7"
        program = compile(body.replace('"YOUR_WORKLOAD_ID"', '"wl_docs"').replace('"YOUR_WORKSPACE_ID"', '"ws_docs"'), str(path), "exec")
        local_project_example = (path.name, number) in {("agent-sandboxes.md", 0), ("managed-agents.md", 1)}
        descriptor_support = os.name == "nt" or (hasattr(os, "fwalk") and hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd)
        if local_project_example and not descriptor_support:
            with pytest.raises(nodus.ValidationError, match="POSIX filesystem"):
                exec(program, namespace)
        else:
            exec(program, namespace)
    if path.name == "workspaces.md" and number == 1:
        assert namespace["transfer"]["state"] == "queued"
        assert ("GET", "/v1/research-workspaces/storage") in docs_api[1]
    if path.name == "workspaces.md" and number == 2:
        with tarfile.open(tmp_path / "saved-project.tar") as saved:
            assert saved.extractfile("result.json").read() == DATA
        assert ("DELETE", "/v1/research-workspaces/ws_docs/files") in docs_api[1]
    # Compile embedded Python argv too, without pretending it ran on a GPU.
    for payload in docs_api[2]:
        sources = [payload.get("source", {})] + [s.get("source", {}) for s in payload.get("stages", [])]
        for source in sources:
            command = source.get("command", [])
            if command and command[0] == "python" and "-c" in command:
                ast.parse(command[command.index("-c") + 1])


@pytest.mark.parametrize("script,args", [
    ("basic.py", []),
    ("async_sweep.py", ["--run-id", "docs-check"]),
    ("ci_submit.py", ["--submission-id", "docs-check"]),
    ("multi_stage.py", ["--submission-id", "docs-check"]),
])
def test_complete_example_programs(script, args, docs_api, tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "examples" / script), *args],
                            cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "wl_docs" in result.stdout
    if script == "multi_stage.py":
        assert (tmp_path / "results/result.json").read_bytes() == DATA


@pytest.mark.parametrize("args", [
    ["--version"], ["--help"], ["run", "--help"], ["login", "--help"],
    ["init"], ["run"], ["submit"], ["list", "active"],
    ["status", "wl_docs"], ["wait", "wl_docs"],
    ["events", "wl_docs"], ["logs", "wl_docs"],
    ["artifacts", "wl_docs"], ["explain", "wl_docs"], ["ledger", "wl_docs"],
    ["download", "wl_docs"], ["workload", "outputs", "wl_docs"], ["workload", "outputs", "wl_docs", "--reload", "results", "--stage", "main"], ["cancel", "wl_docs"], ["assets"], ["upload", "hello.py"],
    ["sandbox", "new", "--name", "research-agent", "--github-repo", "your-org/private-agent", "--github-ref", "main"],
    ["agent", "deploy", "worker", "--github-repo", "your-org/private-agent", "--github-ref", "main"],
])
def test_installed_terminal_commands(args, docs_api, tmp_path):
    from nodus._workload_file import write_workload_file
    if args[0] != "init":
        write_workload_file(tmp_path / "nodus.toml")
    (tmp_path / "hello.py").write_text("print('ready')")
    executable = Path(sysconfig.get_path("scripts")) / ("nodus.exe" if os.name == "nt" else "nodus")
    result = subprocess.run([str(executable), *args], cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert "\x1b" not in result.stdout
    assert "elapsed" not in result.stderr
    if args[0] == "download":
        assert (tmp_path / "outputs/wl_docs/summarize/result").read_bytes() == DATA


def test_login_saved_credentials_and_logout_in_separate_processes(docs_api, tmp_path):
    bootstrap = (
        "import sys,pathlib,nodus.config,nodus.cli; "
        f"nodus.config.config_path=lambda:pathlib.Path({str(tmp_path / 'config.toml')!r}); "
        "raise SystemExit(nodus.cli.main(sys.argv[1:]))"
    )
    env = dict(os.environ)
    env.pop("NODUS_API_KEY", None)
    for args in (["login", "--no-browser"], ["list"], ["logout"]):
        result = subprocess.run([sys.executable, "-c", bootstrap, *args], env=env,
                                cwd=tmp_path, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert "nk_docs" not in result.stdout

    result = subprocess.run([sys.executable, "-c", bootstrap, "list"], env=env,
                            cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 2
    assert "nodus login" in result.stderr


def test_status_prints_the_public_value_without_changing_json():
    status = nodus.WorkloadStatus.COMPLETED
    assert f"Status: {status}" == "Status: completed"
    assert status == "completed"
    assert json.dumps({"status": status}) == '{"status": "completed"}'
    assert nodus.WorkloadStatus.coerce("future_status") == "future_status"
