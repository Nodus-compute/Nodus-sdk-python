"""Local MCP handlers for GPU workspaces, plus file transfers through the SDK."""

import json
from typing import Any

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from . import AsyncClient, _resolve, _valid_id, _valid_idempotency_key
from ._workspaces import AsyncWorkspace, _CONFIGURATION_FIELDS, _job, _tool


def _configuration(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("The workspace configuration must be an object.")
    unknown = sorted(set(body) - _CONFIGURATION_FIELDS)
    if unknown:
        raise ValueError("Unknown workspace configuration field: " + ", ".join(unknown))
    return body


def _session(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("session_id must name the running compute session shown by get_workspace.")
    return value


def register_workspace_tools(server, base_url, request, check_origin):
    """Register fixed handlers for every workspace operation the public contract lists."""
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
    destructive = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)

    async def call(ctx, method, path, body=None, key=None):
        return await request(ctx.request_context.lifespan_context, method, path, base_url=base_url,
                             workload=body, idempotency_key=key)

    def path(workspace_id, suffix=""):
        return "/v1/research-workspaces/" + _valid_id(workspace_id) + suffix

    def client():
        key, origin = _resolve(None, base_url)
        check_origin(origin)
        return AsyncClient(api_key=key, base_url=origin, timeout=300)

    @server.tool(structured_output=False, annotations=read)
    async def get_workspace_capabilities(ctx: Context) -> str:
        """Discover workspace environments, GPU counts, editors and storage limits."""
        return await call(ctx, "GET", "/v1/research-workspaces/capabilities")

    @server.tool(structured_output=False, annotations=read)
    async def list_workspaces(ctx: Context) -> str:
        """List workspaces with their state and connections."""
        return await call(ctx, "GET", "/v1/research-workspaces")

    @server.tool(structured_output=False, annotations=read)
    async def get_workspace(ctx: Context, workspace_id: str) -> str:
        """Read one workspace: state, session, connections, storage and pending upload."""
        return await call(ctx, "GET", path(workspace_id))

    @server.tool(structured_output=False, annotations=write)
    async def create_workspace(ctx: Context, idempotency_key: str, workspace: dict[str, Any]) -> str:
        """Save a workspace configuration. Creating does not rent compute. start_workspace does."""
        return await call(ctx, "POST", "/v1/research-workspaces", _configuration(workspace), idempotency_key)

    @server.tool(structured_output=False, annotations=write)
    async def configure_workspace(ctx: Context, workspace_id: str, idempotency_key: str, configuration_revision: str,
                                  configuration: dict[str, Any]) -> str:
        """Replace the saved configuration, guarded by the revision read from get_workspace."""
        body = {"configuration_revision": configuration_revision, "configuration": _configuration(configuration)}
        return await call(ctx, "PATCH", path(workspace_id), body, idempotency_key)

    @server.tool(structured_output=False, annotations=write)
    async def start_workspace(ctx: Context, workspace_id: str, idempotency_key: str) -> str:
        """Rent compute and restore saved files. Returns before the tools are ready."""
        return await call(ctx, "POST", path(workspace_id, "/start"), {}, idempotency_key)

    @server.tool(structured_output=False, annotations=destructive)
    async def stop_workspace(ctx: Context, workspace_id: str, session_id: str, idempotency_key: str) -> str:
        """Save project files and release compute for the named session."""
        return await call(ctx, "POST", path(workspace_id, "/stop"), {"session_id": _session(session_id)},
                          idempotency_key)

    @server.tool(structured_output=False, annotations=read)
    async def get_workspace_connection(ctx: Context, workspace_id: str, tool: str) -> str:
        """Open a connection: a browser url for editor or notebook, SSH details for ssh."""
        return await call(ctx, "POST", path(workspace_id, "/connections"), {"tool": _tool(tool)})

    @server.tool(structured_output=False, annotations=write)
    async def run_in_workspace(ctx: Context, workspace_id: str, idempotency_key: str, job: dict[str, Any]) -> str:
        """Run a command against the saved project on separate GPU compute, as a workload."""
        if not isinstance(job, dict):
            raise ValueError("job must be an object with command and budget_usd.")
        if "budget_usd" not in job:
            raise ValueError("job.budget_usd is required: the spending limit for this run.")
        body = _job(job.get("command"), budget_usd=job.get("budget_usd"), gpu=job.get("gpu"),
                    gpu_count=job.get("gpu_count"), gpu_memory_gb=job.get("gpu_memory_gb"))
        return await call(ctx, "POST", path(workspace_id, "/workloads"), body, idempotency_key)

    @server.tool(structured_output=False, annotations=read)
    async def list_workspace_workloads(ctx: Context, workspace_id: str) -> str:
        """List workloads submitted from this workspace."""
        return await call(ctx, "GET", path(workspace_id, "/workloads"))

    @server.tool(structured_output=False, annotations=read)
    async def list_workspace_sessions(ctx: Context, workspace_id: str) -> str:
        """List past and current compute sessions."""
        return await call(ctx, "GET", path(workspace_id, "/sessions"))

    @server.tool(structured_output=False, annotations=write)
    async def schedule_workspace(ctx: Context, workspace_id: str, idempotency_key: str, schedule: dict[str, Any]) -> str:
        """Have compute ready by an RFC 3339 time, and optionally stop at another."""
        if not isinstance(schedule, dict) or not isinstance(schedule.get("ready_by"), str):
            raise ValueError("schedule.ready_by is required.")
        return await call(ctx, "POST", path(workspace_id, "/schedule"), schedule, idempotency_key)

    @server.tool(structured_output=False, annotations=write)
    async def cancel_workspace_schedule(ctx: Context, workspace_id: str, idempotency_key: str) -> str:
        """Cancel a schedule. Running compute is not affected."""
        return await call(ctx, "DELETE", path(workspace_id, "/schedule"), None, idempotency_key)

    @server.tool(structured_output=False, annotations=write)
    async def upload_workspace_files(workspace_id: str, directory: str, idempotency_key: str) -> str:
        """Replace the saved project with a local folder while stopped, then wait for verification."""
        key, identifier = _valid_idempotency_key(idempotency_key), _valid_id(workspace_id)
        async with client() as api:
            upload = await AsyncWorkspace(api, identifier).upload(directory, idempotency_key=key)
        return json.dumps({"workspace_id": identifier, "upload": upload})

    @server.tool(structured_output=False, annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True))
    async def download_workspace_files(workspace_id: str, destination: str) -> str:
        """Save the current project files to a local tar archive. Existing files are never overwritten."""
        identifier = _valid_id(workspace_id)
        async with client() as api:
            saved = await AsyncWorkspace(api, identifier).download(destination, overwrite=False)
        return json.dumps({"workspace_id": identifier, "path": str(saved), "verified": True})
