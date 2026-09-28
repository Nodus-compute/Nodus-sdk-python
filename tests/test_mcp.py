"""Public MCP installation, saved login and customer HTTP contracts."""
import json
import os
import subprocess
import sys


def test_cli_starts_mcp_instead_of_requiring_a_private_binary():
    env = {k: v for k, v in os.environ.items() if not k.startswith("NODUS_")}
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "customer-install", "version": "1"}}}
    result = subprocess.run([sys.executable, "-m", "nodus.cli", "mcp"],
                            input=json.dumps(request) + "\n", text=True,
                            capture_output=True, env=env, timeout=15)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["result"]["serverInfo"]["name"] == "nodus"
    assert "tools" in response["result"]["capabilities"]

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session


@pytest.fixture
def api(monkeypatch):
    from nodus import _mcp
    from nodus.config import save_credentials
    save_credentials("saved-test-key", "https://api.example.test")
    requests = []
    responses = []

    def respond(request):
        requests.append(request)
        return responses.pop(0) if responses else httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient
    monkeypatch.setattr(_mcp.httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(respond), **kwargs))
    return _mcp.create_server(), requests, responses


@pytest.mark.asyncio
async def test_saved_login_and_all_tool_contracts(api):
    server, requests, responses = api
    async with create_connected_server_and_client_session(server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) >= {"submit_workload", "list_workloads", "get_workload",
                              "cancel_workload", "get_workload_events", "get_workload_logs",
                              "list_workload_outputs", "validate_workload", "download_workload_output"}
        assert tools["cancel_workload"].inputSchema["required"] == ["workload_id"]
        assert all("ctx" not in tool.inputSchema["properties"] for tool in tools.values())
        workload = {"source": {"image": "example/image", "command": ["nvidia-smi"]},
                    "outcome": {"max_cost_usd": 1}}
        await session.call_tool("submit_workload", {"idempotency_key": "unique-run", "workload": workload})
        assert requests[-1].method == "POST"
        assert requests[-1].url.path == "/v1/workloads"
        assert requests[-1].headers["idempotency-key"] == "unique-run"
        assert json.loads(requests[-1].content) == workload
        await session.call_tool("list_workloads", {"scope": "mine", "limit": 20, "offset": 40})
        assert dict(requests[-1].url.params) == {"scope": "mine", "limit": "20", "offset": "40"}
        for tool, suffix in [("get_workload", ""), ("get_workload_events", "/events"),
                             ("get_workload_logs", "/logs"), ("list_workload_outputs", "/outputs")]:
            responses.append(httpx.Response(200, text="log text" if suffix == "/logs" else '{"ok":true}'))
            args = {"workload_id": "wl_test"}
            if suffix == "/events":
                args["after"] = 100
            result = await session.call_tool(tool, args)
            assert requests[-1].method == "GET"
            assert requests[-1].url.path == "/v1/workloads/wl_test" + suffix
            assert result.content[0].text == ("log text" if suffix == "/logs" else '{"ok":true}')
            if suffix == "/events":
                assert requests[-1].url.params["after"] == "100"
        await session.call_tool("cancel_workload", {"workload_id": "wl_test"})
        assert requests[-1].method == "POST"
        assert requests[-1].url.path == "/v1/workloads/wl_test/cancel"
        assert requests[-1].content == b""
        assert "idempotency-key" not in requests[-1].headers
        assert all(r.headers["authorization"] == "Bearer saved-test-key" for r in requests)


@pytest.mark.asyncio
async def test_output_download_verifies_bytes_and_refuses_overwrite(api, tmp_path):
    import hashlib
    server, requests, responses = api
    output = b'customer training result\n'
    target = tmp_path / "result.txt"
    headers = {"Content-Length": str(len(output)), "X-Nodus-SHA256": hashlib.sha256(output).hexdigest()}
    async with create_connected_server_and_client_session(server) as session:
        responses.append(httpx.Response(200, content=output, headers=headers))
        result = await session.call_tool("download_workload_output", {"workload_id": "wl_test", "name": "result", "destination": str(target), "stage": "train"})
        assert not result.isError, result
        assert target.read_bytes() == output
        assert requests[-1].url.path == "/v1/workloads/wl_test/outputs/result"
        assert requests[-1].url.params["stage"] == "train"
        responses.append(httpx.Response(200, content=b"replacement", headers=headers))
        result = await session.call_tool("download_workload_output", {"workload_id": "wl_test", "name": "result", "destination": str(target)})
        assert result.isError
        assert target.read_bytes() == output


@pytest.mark.asyncio
async def test_validate_and_submit_accept_optional_legacy_budget(api):
    server, requests, responses = api
    async with create_connected_server_and_client_session(server) as session:
        for budget in (-1, True, "10"):
            workload = {"outcome": {"max_cost_usd": budget}}
            for tool in ("validate_workload", "submit_workload"):
                args = {"workload": workload}
                if tool == "submit_workload":
                    args["idempotency_key"] = "saved-before-submit"
                result = await session.call_tool(tool, args)
                assert result.isError
        assert requests == []
        workload = {"source": {"image": "customer/image", "command": ["python", "train.py"]}}
        responses.append(httpx.Response(200, json={"valid": True, "submitted": False, "workload": workload}))
        result = await session.call_tool("validate_workload", {"workload": workload})
        assert not result.isError
        assert requests[-1].url.path == "/v1/workloads/validate"
        assert json.loads(requests[-1].content) == workload


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args", [
    ("get_workload", {"workload_id": "../secrets"}),
    ("submit_workload", {"idempotency_key": "bad\r\nkey", "workload": {"outcome": {"max_cost_usd": 10}}}),
    ("list_workloads", {"scope": "other"}),
    ("list_workloads", {"limit": 101}),
    ("list_workloads", {"limit": True}),
    ("list_workloads", {"offset": -1}),
    ("get_workload_events", {"workload_id": "wl_test", "after": 9007199254740992}),
])
async def test_invalid_arguments_never_reach_api(api, tool, args):
    server, requests, _ = api
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(tool, args)
        assert result.isError
        assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 500])
async def test_http_failures_and_redirects_are_tool_errors(api, status):
    server, requests, responses = api
    async with create_connected_server_and_client_session(server) as session:
        responses.append(httpx.Response(status, headers={"Location": "https://other.example/"},
                                        text='{"error":"denied"}'))
        result = await session.call_tool("list_workloads", {})
        assert result.isError
        assert str(status) in result.content[0].text
        assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 500])
async def test_large_success_and_error_responses_are_bounded(api, status):
    server, _, responses = api
    async with create_connected_server_and_client_session(server) as session:
        responses.append(httpx.Response(status, content=b"x" * (16 * 1024 * 1024 + 1)))
        result = await session.call_tool("get_workload_logs", {"workload_id": "wl_test"})
        assert result.isError
        assert "16 MiB" in result.content[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://example.test", "http://127.evil.test", "https://user:pass@example.test"])
async def test_unsafe_api_origin_sends_no_credentials(api, monkeypatch, url):
    server, requests, _ = api
    async with create_connected_server_and_client_session(server) as session:
        monkeypatch.setenv("NODUS_BASE_URL", url)
        result = await session.call_tool("list_workloads", {})
        assert result.isError
        assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("executable", ["nodus", "nodus-mcp"])
async def test_installed_executables_complete_real_mcp_session(executable, nodus_config):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    import threading
    import sysconfig
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    requests = []
    closed = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization"),
                             self.headers.get("Cookie"), self.client_address))
            body = b'{"workloads":[],"next_offset":null}'
            self.send_response(500 if self.path.endswith("wl_error") else 200)
            self.send_header("Set-Cookie", "session=server-session; Path=/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def finish(self):
            super().finish()
            closed.set()

        def log_message(self, *args):
            pass

    api_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=api_server.serve_forever, daemon=True)
    thread.start()
    command = Path(sysconfig.get_path("scripts")) / (executable + (".exe" if os.name == "nt" else ""))
    from nodus.config import save_credentials
    origin = f"http://127.0.0.1:{api_server.server_port}"
    save_credentials("integration-test-key", origin)
    home = str(nodus_config.parent.parent)
    params = StdioServerParameters(
        command=str(command), args=["mcp"] if executable == "nodus" else [],
        env={"HOME": home, "USERPROFILE": home,
             "NODUS_API_KEY": "", "NODUS_BASE_URL": ""})
    try:
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                result = await session.initialize()
                assert result.serverInfo.name == "nodus"
                assert {"list_workloads", "create_sandbox", "submit_agent_run", "upload_project"} <= {tool.name for tool in (await session.list_tools()).tools}
                listed = await session.call_tool("list_workloads", {})
                assert not listed.isError
                assert json.loads(listed.content[0].text) == {"workloads": [], "next_offset": None}
                failed = await session.call_tool("get_workload", {"workload_id": "wl_error"})
                assert failed.isError
                save_credentials("renewed-test-key", origin)
                refreshed = await session.call_tool("list_workloads", {})
                assert not refreshed.isError
                assert len({request[3] for request in requests}) == 1
                assert not closed.is_set()
                invalid = await session.call_tool("get_workload", {"workload_id": "../secret"})
                assert invalid.isError
        assert closed.wait(timeout=5)
    finally:
        api_server.shutdown()
        api_server.server_close()
        thread.join(timeout=5)
    assert [request[:3] for request in requests] == [
        ("/v1/workloads", "Bearer integration-test-key", None),
        ("/v1/workloads/wl_error", "Bearer integration-test-key", None),
        ("/v1/workloads", "Bearer renewed-test-key", None),
    ]


@pytest.mark.asyncio
async def test_pool_uses_current_origin_and_supports_concurrent_calls(api):
    import asyncio
    from nodus.config import save_credentials

    server, requests, _ = api
    async with create_connected_server_and_client_session(server) as session:
        assert not (await session.call_tool("list_workloads", {})).isError
        save_credentials("second-account-key", "https://second.example.test")
        results = await asyncio.gather(*(
            session.call_tool("get_workload", {"workload_id": name})
            for name in ("wl_one", "wl_two", "wl_three")))
        assert all(not result.isError for result in results)
    assert requests[0].url.host == "api.example.test"
    assert {request.url.path for request in requests[1:]} == {
        "/v1/workloads/wl_one", "/v1/workloads/wl_two", "/v1/workloads/wl_three"}
    assert all(request.url.host == "second.example.test" for request in requests[1:])
    assert all(request.headers["authorization"] == "Bearer second-account-key"
               for request in requests[1:])


@pytest.mark.asyncio
async def test_server_owns_and_closes_the_pool_after_tool_failure(monkeypatch):
    from nodus import _mcp
    from nodus.config import save_credentials

    save_credentials("lifecycle-test-key", "https://api.example.test")
    clients = []
    client_class = httpx.AsyncClient

    def create_client(**kwargs):
        client = client_class(transport=httpx.MockTransport(
            lambda request: httpx.Response(500, text="temporary failure")), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(_mcp.httpx, "AsyncClient", create_client)
    async with create_connected_server_and_client_session(_mcp.create_server()) as session:
        for _ in range(2):
            result = await session.call_tool("list_workloads", {})
            assert result.isError
        assert len(clients) == 1
        assert not clients[0].is_closed
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_download_keeps_resolved_credential_and_origin_together(api, nodus_config, tmp_path, monkeypatch):
    import hashlib
    from pathlib import Path
    server, requests, responses = api
    original_read = Path.read_text
    changed = False

    def change_login_after_read(path, *args, **kwargs):
        nonlocal changed
        content = original_read(path, *args, **kwargs)
        if path == nodus_config and not changed:
            changed = True
            nodus_config.write_text('[default]\napi_key="other-account-key"\nbase_url="https://other.example.test"\n')
        return content

    monkeypatch.setattr(Path, "read_text", change_login_after_read)
    data = b"verified result"
    responses.append(httpx.Response(200, content=data, headers={"X-Nodus-SHA256": hashlib.sha256(data).hexdigest()}))
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("download_workload_output", {
            "workload_id": "wl_test", "name": "result", "destination": str(tmp_path / "result")})
    assert not result.isError
    assert changed
    assert [(request.url.host, request.headers["authorization"]) for request in requests] == [
        ("api.example.test", "Bearer saved-test-key")]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", [
    {"name": "worker", "entrypoint": "agent:main", "budget_usd": 20},
    {"name": "assistant", "assistant_template": "nodus:claude-assistant-v1",
     "model": "nodus:claude-haiku", "model_max_output_tokens": 1024, "budget_usd": 20},
])
async def test_sandbox_and_agent_tools_return_acceptance_without_polling(api, agent):
    server, requests, responses = api
    async with create_connected_server_and_client_session(server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert "create_sandbox" in tools and "create_agent" in tools
        assert tools["sandbox_files"].annotations.readOnlyHint is False
        assert tools["download_sandbox_file"].annotations.destructiveHint is True
        assert tools["get_sandbox"].annotations.readOnlyHint is True
        assert tools["get_agent_run"].annotations.readOnlyHint is True
        sandbox = {"template": "nodus:agent-tools-v1", "budget_usd": 5}
        operations = [
            ("create_sandbox", {"sandbox": sandbox}, "/v1/sandboxes", sandbox, {"id": "sb_one", "status": "pending"}),
            ("submit_sandbox_command", {"sandbox_id": "sb_one", "command": {"command": ["python", "example.py"]}},
             "/v1/sandboxes/sb_one/exec", {"command": ["python", "example.py"]}, {"id": "exec_one", "state": "queued"}),
            ("sandbox_files", {"sandbox_id": "sb_one", "file": {"operation": "read", "path": "result.txt"}},
             "/v1/sandboxes/sb_one/files", {"operation": "read", "path": "result.txt"}, {"id": "exec_files", "state": "queued"}),
            ("create_agent", {"agent": agent}, "/v1/agents", agent, {"id": "agent_one", "revision": 1}),
            ("submit_agent_run", {"agent_id": "agent_one", "run": {"input": {"task": "reports"}, "session": "reports"}},
             "/v1/agents/agent_one/runs", {"input": {"task": "reports"}, "session": "reports"}, {"id": "run_one", "status": "queued"}),
        ]
        for tool, arguments, path, body, receipt in operations:
            key = tool + "-same-intent"
            responses.append(httpx.Response(202, json=receipt))
            before = len(requests)
            result = await session.call_tool(tool, {**arguments, "idempotency_key": key})
            assert not result.isError, result
            assert json.loads(result.content[0].text) == receipt
            assert len(requests) == before + 1
            assert requests[-1].url.path == path
            assert requests[-1].headers["Idempotency-Key"] == key
            assert json.loads(requests[-1].content) == body


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments,method,path,query", [
    ("get_sandbox_capabilities", {}, "GET", "/v1/sandboxes/capabilities", {}),
    ("list_sandbox_templates", {}, "GET", "/v1/sandbox-templates", {}),
    ("list_sandboxes", {"name": "work", "status": "ready", "cursor": "next", "limit": 3}, "GET", "/v1/sandboxes", {"name": "work", "status": "ready", "cursor": "next", "limit": "3"}),
    ("get_sandbox", {"sandbox_id": "sb_one"}, "GET", "/v1/sandboxes/sb_one", {}),
    ("get_sandbox_command", {"sandbox_id": "sb_one", "exec_id": "exec_one"}, "GET", "/v1/sandboxes/sb_one/execs/exec_one", {}),
    ("get_sandbox_command_output", {"sandbox_id": "sb_one", "exec_id": "exec_one", "after": 3, "limit": 2}, "GET", "/v1/sandboxes/sb_one/execs/exec_one/stream", {"after": "3", "limit": "2"}),
    ("list_agents", {"limit": 2, "after": "agent_one"}, "GET", "/v1/agents", {"limit": "2", "after": "agent_one"}),
    ("get_agent", {"agent_id": "agent_one"}, "GET", "/v1/agents/agent_one", {}),
    ("list_agent_runs", {"agent_id": "agent_one", "after": "run_one", "limit": 5}, "GET", "/v1/agents/agent_one/runs", {"after": "run_one", "limit": "5"}),
    ("get_agent_run", {"agent_id": "agent_one", "run_id": "run_one"}, "GET", "/v1/agents/agent_one/runs/run_one", {}),
    ("get_agent_run_steps", {"agent_id": "agent_one", "run_id": "run_one", "after": "step_one", "limit": 5}, "GET", "/v1/agents/agent_one/runs/run_one/steps", {"after": "step_one", "limit": "5"}),
])
async def test_sandbox_and_agent_observation_never_wakes_compute(api, tool, arguments, method, path, query):
    server, requests, _ = api
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(tool, arguments)
        assert not result.isError, result
        assert len(requests) == 1
        assert requests[0].method == method and requests[0].url.path == path
        assert dict(requests[0].url.params) == query


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments,path", [
    ("sleep_sandbox", {"sandbox_id": "sb_one"}, "/v1/sandboxes/sb_one/sleep"),
    ("wake_sandbox", {"sandbox_id": "sb_one"}, "/v1/sandboxes/sb_one/wake"),
    ("terminate_sandbox", {"sandbox_id": "sb_one"}, "/v1/sandboxes/sb_one/terminate"),
    ("cancel_sandbox_command", {"sandbox_id": "sb_one", "exec_id": "exec_one"}, "/v1/sandboxes/sb_one/execs/exec_one/cancel"),
    ("pause_agent", {"agent_id": "agent_one"}, "/v1/agents/agent_one/pause"),
    ("resume_agent", {"agent_id": "agent_one"}, "/v1/agents/agent_one/resume"),
    ("retry_agent_run", {"agent_id": "agent_one", "run_id": "run_one"}, "/v1/agents/agent_one/runs/run_one/retry"),
    ("cancel_agent_run", {"agent_id": "agent_one", "run_id": "run_one"}, "/v1/agents/agent_one/runs/run_one/cancel"),
])
async def test_new_controls_require_stable_keys_and_never_wait_for_cleanup(api, tool, arguments, path):
    server, requests, _ = api
    async with create_connected_server_and_client_session(server) as session:
        missing = await session.call_tool(tool, arguments)
        assert missing.isError and not requests
        result = await session.call_tool(tool, {**arguments, "idempotency_key": "customer-control"})
        assert not result.isError, result
        assert len(requests) == 1 and requests[0].url.path == path
        assert requests[0].headers["Idempotency-Key"] == "customer-control"
        assert json.loads(requests[0].content) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("definition", [
    {"name": "worker", "entrypoint": "agent:main", "budget_usd": 20},
    {"name": "assistant", "assistant_template": "nodus:claude-assistant-v1",
     "model": "nodus:claude-haiku", "model_max_output_tokens": 1024, "budget_usd": 20},
])
async def test_agent_update_and_signal_preserve_revision_and_input(api, definition):
    server, requests, _ = api
    async with create_connected_server_and_client_session(server) as session:
        update = {"expected_revision": 3, "definition": definition}
        result = await session.call_tool("update_agent", {"agent_id": "agent_one", "update": update, "idempotency_key": "revision-4"})
        assert not result.isError, result
        assert requests[-1].method == "PATCH" and json.loads(requests[-1].content) == update
        signal = {"name": "reports", "input": {"approved": True}}
        result = await session.call_tool("signal_agent_run", {"agent_id": "agent_one", "run_id": "run_one", "signal": signal, "idempotency_key": "event-1"})
        assert not result.isError, result
        assert requests[-1].url.path == "/v1/agents/agent_one/runs/run_one/signals"
        assert json.loads(requests[-1].content) == signal


@pytest.mark.asyncio
async def test_new_tools_reject_missing_authority_before_network(api):
    server, requests, _ = api
    async with create_connected_server_and_client_session(server) as session:
        for tool, payload in (("create_sandbox", "sandbox"), ("create_agent", "agent")):
            for budget in (True, -1, "20"):
                result = await session.call_tool(tool, {payload: {"budget_usd": budget}, "idempotency_key": "explicit-intent"})
                assert result.isError
        for arguments in (
            {"sandbox_id": "../secrets"},
            {"sandbox_id": "sb_one", "exec_id": "../exec"},
        ):
            tool = "get_sandbox_command" if "exec_id" in arguments else "get_sandbox"
            assert (await session.call_tool(tool, arguments)).isError
        assert requests == []


@pytest.mark.asyncio
async def test_local_project_tool_packages_exclusions_and_binds_upload_identity(api, tmp_path):
    import hashlib
    import io
    import tarfile
    server, requests, responses = api
    (tmp_path / "agent.py").write_bytes(b"print('exact bytes')\n")
    (tmp_path / ".env").write_text("SECRET=do-not-upload")
    asset_digest = "a" * 64
    async with create_connected_server_and_client_session(server) as session:
        for _ in range(2):
            responses.extend([
                httpx.Response(200, json={"max_project_bytes": 100000}),
                httpx.Response(200, json={"max_import_bytes": 100000, "upload_idempotency": True}),
                httpx.Response(201, json={"id": "asset_project", "state": "ready", "sha256": asset_digest, "stored_bytes": 10240}),
            ])
            result = await session.call_tool("upload_project", {"project": str(tmp_path), "idempotency_key": "project-one"})
            assert not result.isError, result
            uploaded = json.loads(result.content[0].text)
            assert uploaded["asset_id"] == "asset_project" and uploaded["sha256"] == asset_digest
        uploads = [r for r in requests if r.method == "POST"]
        assert len(uploads) == 2
        assert uploads[0].content == uploads[1].content
        for upload in uploads:
            assert upload.url.path == "/v1/assets/upload"
            assert upload.headers["Idempotency-Key"] == "project-one"
            assert upload.headers["X-Nodus-SHA256"] == hashlib.sha256(upload.content).hexdigest()
            with tarfile.open(fileobj=io.BytesIO(upload.content), mode="r:gz") as archive:
                assert archive.getnames() == ["agent.py"]
                assert archive.extractfile("agent.py").read() == b"print('exact bytes')\n"
        assert not any(r.url.path == "/v1/sandboxes" for r in requests)


@pytest.mark.asyncio
async def test_local_download_verifies_bytes_and_retains_read_keys_across_retries(api, tmp_path):
    import base64
    import hashlib
    server, requests, responses = api
    output = bytes(range(256)) * 1200
    digest = hashlib.sha256(output).hexdigest()
    async with create_connected_server_and_client_session(server) as session:
        for target in (tmp_path / "one.bin", tmp_path / "two.bin"):
            operations = [{"operation": "stat", "path": "out.bin", "type": "file", "size_bytes": len(output)}]
            for offset in range(0, len(output), 262144):
                chunk = output[offset:offset + 262144]
                operations.append({"operation": "read", "path": "out.bin", "data": base64.b64encode(chunk).decode(),
                                   "size_bytes": len(output), "sha256": digest, "offset": offset,
                                   "next_offset": offset + len(chunk), "eof": offset + len(chunk) == len(output)})
            for index, body in enumerate(operations):
                responses.extend([
                    httpx.Response(202, json={"id": "exec_" + str(index), "sandbox_id": "sb_one", "state": "completed", "exit_code": 0}),
                    httpx.Response(200, json={"frames": [{"sequence": 1, "stream": "stdout", "data": base64.b64encode(json.dumps(body).encode()).decode()}],
                                             "next_sequence": 1, "last_sequence": 1, "done": True, "complete": True}),
                ])
            result = await session.call_tool("download_sandbox_file", {"sandbox_id": "sb_one", "path": "out.bin", "destination": str(target), "idempotency_key": "download-one"})
            assert not result.isError, result
            assert target.read_bytes() == output
            assert json.loads(result.content[0].text)["verified"] is True
        posts = [r for r in requests if r.method == "POST"]
        assert len(posts) == 6
        assert [r.headers["Idempotency-Key"] for r in posts[:3]] == [r.headers["Idempotency-Key"] for r in posts[3:]]
        assert len({r.headers["Idempotency-Key"] for r in posts}) == 3


@pytest.mark.asyncio
async def test_local_discovery_matches_bundled_public_operation_contract(api):
    from importlib.resources import files
    server, requests, _ = api
    manifest = json.loads(files("nodus").joinpath("_operation_manifest.json").read_text())
    async with create_connected_server_and_client_session(server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        for operation in manifest["operations"]:
            if "local_mcp" not in operation["transports"]:
                continue
            tool = tools[operation["name"]]
            assert tool.inputSchema == operation["inputSchema"]
            assert tool.annotations.model_dump(exclude_none=True) == operation["annotations"]
        result = await session.call_tool("get_operation_manifest", {})
        assert not result.isError, result
        catalog = json.loads(result.content[0].text)
        assert catalog["version"] == "v1"
        names = {row["name"] for row in catalog["operations"]}
        assert {"create_agent", "sandbox_files", "upload_project", "download_sandbox_file"} <= names
        assert "get_workload_output" not in names
        assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"tenant_id": "other-team"}, {"idempotency_key": "x" * 201}])
async def test_local_invocation_enforces_the_advertised_contract(api, changes):
    server, requests, _ = api
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("create_sandbox", {"sandbox": {"budget_usd": 5},
                                         "idempotency_key": "same-intent", **changes})
        assert result.isError
        assert not requests


@pytest.mark.asyncio
async def test_legacy_workload_optional_nulls_keep_the_local_contract(api):
    server, requests, _ = api
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("list_workloads", {"scope": None, "limit": None, "offset": None})
        assert not result.isError, result
        assert dict(requests[-1].url.params) == {}
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        manifest = json.loads((await session.call_tool("get_operation_manifest", {})).content[0].text)
        workload = next(row for row in manifest["operations"] if row["name"] == "list_workloads")
        assert workload["inputSchema"] == tools["list_workloads"].inputSchema


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments", [
    ("create_sandbox", {"sandbox": {"budget_usd": 1, "BUDGET_USD": 50}}),
    ("create_agent", {"agent": {"name": "worker", "entrypoint": "agent:main", "budget_usd": 1, "BUDGET_USD": 50}}),
    ("update_agent", {"agent_id": "agent_one", "update": {"expected_revision": 1, "definition": {"name": "worker", "entrypoint": "agent:main", "budget_usd": 1, "BUDGET_USD": 50}}}),
])
async def test_case_aliased_budgets_cannot_raise_authorized_spending(api, tool, arguments):
    server, requests, _ = api
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(tool, {**arguments, "idempotency_key": "authorized-one-dollar"})
        assert result.isError
        assert requests == []


WORKSPACE_VIEW = {"id": "ws_lab", "name": "lab", "state": "stopped", "storage_revision": 0,
                  "configuration": {"name": "lab", "gpu": "H100", "size_gb": 10},
                  "configuration_revision": "a" * 64, "session": None, "meter": None,
                  "connections": {"editor": False, "notebook": False, "ssh": False}, "pending_upload": None}


@pytest.mark.asyncio
async def test_workspace_tools_follow_the_published_contract_and_routes(api):
    server, requests, responses = api
    workspace = {"name": "lab", "environment": "pytorch-cuda", "gpu": "H100", "gpu_count": 1, "gpu_memory_gb": 80}
    job = {"command": "python train.py", "budget_usd": 5}
    async with create_connected_server_and_client_session(server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert tools["stop_workspace"].annotations.destructiveHint is True
        assert tools["get_workspace"].annotations.readOnlyHint is True
        assert tools["create_workspace"].inputSchema["required"] == ["workspace", "idempotency_key"]
        writes = [
            ("create_workspace", {"workspace": workspace}, "POST", "/v1/research-workspaces", workspace, WORKSPACE_VIEW),
            ("start_workspace", {"workspace_id": "ws_lab"}, "POST", "/v1/research-workspaces/ws_lab/start", {}, {**WORKSPACE_VIEW, "state": "creating"}),
            ("run_in_workspace", {"workspace_id": "ws_lab", "job": job}, "POST", "/v1/research-workspaces/ws_lab/workloads", job, {"id": "wl_1", "status": "queued"}),
            ("stop_workspace", {"workspace_id": "ws_lab", "session_id": "sb_1"}, "POST", "/v1/research-workspaces/ws_lab/stop", {"session_id": "sb_1"}, {**WORKSPACE_VIEW, "state": "stopping"}),
            ("configure_workspace", {"workspace_id": "ws_lab", "configuration_revision": "a" * 64, "configuration": workspace}, "PATCH", "/v1/research-workspaces/ws_lab", {"configuration_revision": "a" * 64, "configuration": workspace}, WORKSPACE_VIEW),
            ("schedule_workspace", {"workspace_id": "ws_lab", "schedule": {"ready_by": "2026-09-28T09:00:00Z"}}, "POST", "/v1/research-workspaces/ws_lab/schedule", {"ready_by": "2026-09-28T09:00:00Z"}, {"id": "plan_1"}),
            ("cancel_workspace_schedule", {"workspace_id": "ws_lab"}, "DELETE", "/v1/research-workspaces/ws_lab/schedule", None, WORKSPACE_VIEW),
        ]
        for tool, arguments, method, path, body, receipt in writes:
            key = tool + "-same-intent"
            responses.append(httpx.Response(202, json=receipt))
            before = len(requests)
            result = await session.call_tool(tool, {**arguments, "idempotency_key": key})
            assert not result.isError, result
            assert json.loads(result.content[0].text) == receipt
            assert len(requests) == before + 1 and requests[-1].method == method and requests[-1].url.path == path
            assert requests[-1].headers["Idempotency-Key"] == key
            assert (json.loads(requests[-1].content) if requests[-1].content else None) == body
        reads = [
            ("get_workspace_capabilities", {}, "GET", "/v1/research-workspaces/capabilities"),
            ("list_workspaces", {}, "GET", "/v1/research-workspaces"),
            ("get_workspace", {"workspace_id": "ws_lab"}, "GET", "/v1/research-workspaces/ws_lab"),
            ("get_workspace_connection", {"workspace_id": "ws_lab", "tool": "ssh"}, "POST", "/v1/research-workspaces/ws_lab/connections"),
            ("list_workspace_workloads", {"workspace_id": "ws_lab"}, "GET", "/v1/research-workspaces/ws_lab/workloads"),
            ("list_workspace_sessions", {"workspace_id": "ws_lab"}, "GET", "/v1/research-workspaces/ws_lab/sessions"),
        ]
        for tool, arguments, method, path in reads:
            before = len(requests)
            result = await session.call_tool(tool, arguments)
            assert not result.isError, result
            assert len(requests) == before + 1 and requests[-1].method == method and requests[-1].url.path == path
            assert "Idempotency-Key" not in requests[-1].headers
        assert json.loads(requests[-1 - 2].content) == {"tool": "ssh"}


@pytest.mark.asyncio
async def test_workspace_delete_advertises_client_confirmation_and_destructive_keyed_operation(api):
    server, requests, responses = api
    receipt = {"id": "ws_lab", "name": "lab", "deleted": True, "deleted_at": "2026-09-28T10:00:00Z"}
    async with create_connected_server_and_client_session(server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        tool = tools["delete_workspace"]
        assert tool.annotations.destructiveHint is True and tool.annotations.readOnlyHint is False
        assert tool.annotations.idempotentHint is True
        assert "confirm" in tool.description.lower()
        manifest = json.loads((await session.call_tool("get_operation_manifest", {})).content[0].text)
        operation = next(row for row in manifest["operations"] if row["name"] == "delete_workspace")
        assert operation["id"] == "workspaces.delete" and operation["required_scope"] == "workspaces:write"
        responses.append(httpx.Response(200, json=receipt))
        result = await session.call_tool("delete_workspace", {"workspace_id": "ws_lab", "idempotency_key": "delete-lab"})
        assert not result.isError and json.loads(result.content[0].text) == receipt
        assert len(requests) == 1 and requests[0].method == "DELETE"
        assert requests[0].url.path == "/v1/research-workspaces/ws_lab" and requests[0].content == b""
        assert requests[0].headers["Idempotency-Key"] == "delete-lab"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments", [
    ("run_in_workspace", {"workspace_id": "ws_lab", "job": {"command": "python x.py", "budget_usd": 0}, "idempotency_key": "k"}),
    ("run_in_workspace", {"workspace_id": "ws_lab", "job": {"command": "python x.py"}, "idempotency_key": "k"}),
    ("run_in_workspace", {"workspace_id": "ws_lab", "job": {"command": "", "budget_usd": 1}, "idempotency_key": "k"}),
    ("create_workspace", {"workspace": {"name": "lab", "gpu": "H100", "colour": "blue"}, "idempotency_key": "k"}),
    ("get_workspace_connection", {"workspace_id": "ws_lab", "tool": "terminal"}),
    ("stop_workspace", {"workspace_id": "ws_lab", "idempotency_key": "k"}),
    ("start_workspace", {"workspace_id": "../other", "idempotency_key": "k"}),
])
async def test_workspace_tool_arguments_are_refused_before_network(api, tool, arguments):
    server, requests, _ = api
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(tool, arguments)
        assert result.isError
        assert requests == []


@pytest.mark.asyncio
async def test_local_workspace_upload_and_download_use_verified_transfers(api, tmp_path, monkeypatch):
    server, requests, responses = api
    project = tmp_path / "project"
    project.mkdir()
    (project / "train.py").write_text("print('hi')\n")
    seen = {}

    async def upload(self, directory, *, idempotency_key=None, poll_seconds=2.0, timeout_seconds=600.0):
        seen["upload"] = (self.id, str(directory), idempotency_key)
        return {"id": "tr_1", "state": "verifying"}

    async def download(self, destination, *, overwrite=False):
        seen["download"] = (self.id, str(destination), overwrite)
        return destination

    from nodus import _workspaces
    monkeypatch.setattr(_workspaces.AsyncWorkspace, "upload", upload)
    monkeypatch.setattr(_workspaces.AsyncWorkspace, "download", download)
    async with create_connected_server_and_client_session(server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert tools["upload_workspace_files"].inputSchema["additionalProperties"] is False
        assert "idempotency_key" in tools["upload_workspace_files"].inputSchema["required"]
        result = await session.call_tool("upload_workspace_files", {"workspace_id": "ws_lab", "directory": str(project), "idempotency_key": "upload-one"})
        assert not result.isError, result
        assert json.loads(result.content[0].text) == {"workspace_id": "ws_lab", "upload": {"id": "tr_1", "state": "verifying"}}
        result = await session.call_tool("download_workspace_files", {"workspace_id": "ws_lab", "destination": str(tmp_path / "saved.tar")})
        assert not result.isError, result
        assert json.loads(result.content[0].text) == {"workspace_id": "ws_lab", "path": str(tmp_path / "saved.tar"), "verified": True}
        catalog = json.loads((await session.call_tool("get_operation_manifest", {})).content[0].text)
        names = {row["name"]: row for row in catalog["operations"]}
        assert names["upload_workspace_files"]["required_scope"] == "workspaces:write"
        assert names["download_workspace_files"]["required_scope"] == "workspaces:read"
        assert "local_mcp" in names["create_workspace"]["transports"]
    assert seen == {"upload": ("ws_lab", str(project), "upload-one"),
                    "download": ("ws_lab", str(tmp_path / "saved.tar"), False)}
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("max_hours", 4), ("budget_usd", 5), ("editor", "vscode"),
                                         ("repository", "org/repo"), ("ref", "main")])
async def test_workspace_tools_refuse_lifetime_budget_editor_and_repository_fields(api, field, value):
    server, requests, _ = api
    workspace = {"name": "lab", "environment": "pytorch-cuda", "gpu": "H100", "gpu_count": 1,
                 "gpu_memory_gb": 80, "size_gb": 10, field: value}
    async with create_connected_server_and_client_session(server) as session:
        for tool, arguments in (("create_workspace", {"workspace": workspace}),
                                ("configure_workspace", {"workspace_id": "ws_lab", "configuration_revision": "a" * 64,
                                                         "configuration": workspace})):
            result = await session.call_tool(tool, {**arguments, "idempotency_key": "k"})
            assert result.isError, (tool, result)
    assert requests == []


@pytest.mark.parametrize("field", ["max_hours", "budget_usd", "editor", "repository", "ref"])
def test_workspace_configuration_allowlist_excludes_lifetime_budget_editor_and_repository(field):
    from nodus._mcp_workspaces import _configuration
    with pytest.raises(ValueError, match="Unknown workspace configuration field: " + field):
        _configuration({"name": "lab", "gpu": "H100", field: 1})
