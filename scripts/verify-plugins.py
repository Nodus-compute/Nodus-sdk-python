"""Exercise each plugin's public MCP command with an isolated saved login."""
from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading

from verification_package import package_source

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


TOOLS = {
    "submit_workload", "list_workloads", "get_workload", "cancel_workload",
    "get_workload_events", "get_workload_logs", "list_workload_outputs", "validate_workload", "download_workload_output",
    "get_operation_manifest", "get_sandbox_capabilities", "list_sandbox_templates",
    "create_sandbox", "list_sandboxes", "get_sandbox", "submit_sandbox_command",
    "get_sandbox_command", "get_sandbox_command_output", "cancel_sandbox_command",
    "sandbox_files", "sleep_sandbox", "wake_sandbox", "terminate_sandbox",
    "create_agent", "update_agent", "list_agents", "get_agent", "submit_agent_run",
    "list_agent_runs", "get_agent_run", "get_agent_run_steps", "signal_agent_run",
    "pause_agent", "resume_agent", "retry_agent_run", "cancel_agent_run",
    "upload_project", "upload_sandbox_file", "download_sandbox_file",
    "get_workspace_capabilities", "list_workspaces", "get_workspace", "create_workspace", "configure_workspace",
    "start_workspace", "stop_workspace", "get_workspace_connection", "run_in_workspace",
    "list_workspace_workloads", "list_workspace_sessions", "schedule_workspace", "cancel_workspace_schedule",
    "upload_workspace_files", "download_workspace_files",
    "launch_gpu", "list_compute", "stop_compute",
}


async def verify(plugin: Path, wheel: Path | None = None) -> None:
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization"), self.client_address))
            body = b'{"workloads":[],"next_offset":null}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="nodus-plugin-check-") as directory:
            root = Path(directory)
            credentials = root / ".nodus/config.toml"
            credentials.parent.mkdir(mode=0o700)
            credentials.write_text(
                '[default]\napi_key = "plugin-test-key"\n'
                f'base_url = "http://127.0.0.1:{server.server_port}"\n',
                encoding="utf-8",
            )
            credentials.chmod(0o600)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("NODUS_", "UV_", "PYTHON", "PIP_", "GH_", "GITHUB_"))}
            env.update(HOME=directory, USERPROFILE=directory,
                       UV_CACHE_DIR=str(root / "cache"),
                       UV_DEFAULT_INDEX="https://pypi.org/simple", UV_NO_CONFIG="1")
            for client in ("codex", "claude", "cursor"):
                manifest = json.loads((plugin / f".{client}-plugin/plugin.json").read_text())
                config = json.loads((plugin / manifest["mcpServers"]).read_text())
                launch = config["mcpServers"]["nodus"]
                if set(launch) != {"command", "args"}:
                    raise RuntimeError("Expected a credential-free command and arguments")
                version = launch["args"][1].split("==")[1]
                launch["args"][1] = package_source(version, wheel)
                command = shutil.which(launch["command"])
                if command is None:
                    raise RuntimeError(f"Install {launch['command']} before verifying plugins")
                params = StdioServerParameters(command=command, args=launch["args"],
                                               env=env, cwd=directory)
                requests.clear()
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as session:
                        initialized = await session.initialize()
                        assert initialized.serverInfo.name == "nodus"
                        names = {tool.name for tool in (await session.list_tools()).tools}
                        assert names == TOOLS, names
                        discovery = await session.call_tool("get_operation_manifest", {})
                        assert not discovery.isError, discovery
                        manifest = json.loads(discovery.content[0].text)
                        assert manifest["version"] == "v1"
                        assert {item["name"] for item in manifest["operations"]} == names - {"get_operation_manifest"}
                        for _ in range(2):
                            result = await session.call_tool("list_workloads", {"limit": 1})
                            assert not result.isError, result
                            assert json.loads(result.content[0].text)["workloads"] == []
                assert [request[:2] for request in requests] == [
                    ("/v1/workloads?limit=1", "Bearer plugin-test-key")
                ] * 2, requests
                assert len({request[2] for request in requests}) == 1, requests
                print(f"{client}: {len(names)} tools, versioned discovery, saved login, two reads, one TCP connection")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path,
                        default=Path(__file__).resolve().parents[1] / "plugins/nodus")
    parser.add_argument("--wheel", type=Path, help="test a matching unpublished wheel")
    args = parser.parse_args()
    asyncio.run(verify(args.plugin.resolve(), args.wheel))
