"""Explicit MCP adapters for sandbox and managed execution APIs."""

import math
from typing import Annotated, Any

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from . import _valid_id

PageLimit = Annotated[int, Field(strict=True, ge=1, le=100)]
FrameLimit = Annotated[int, Field(strict=True, ge=1, le=16)]
Sequence = Annotated[int, Field(strict=True, ge=0, le=9007199254740991)]


def _budget(body):
    value = body.get("budget_usd")
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError("budget_usd must be a finite nonnegative number.")


def register_execution_tools(server, base_url, request):
    """Register fixed handlers without executing server-advertised operations."""
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
    destructive = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)

    async def call(ctx, method, path, body=None, key=None, **params):
        return await request(ctx.request_context.lifespan_context, method, path, base_url=base_url,
                             workload=body, idempotency_key=key,
                             params={name: value for name, value in params.items() if value is not None} or None)

    def sandbox_path(sandbox_id, exec_id=None):
        path = "/v1/sandboxes/" + _valid_id(sandbox_id)
        return path if exec_id is None else path + "/execs/" + _valid_id(exec_id)

    def agent_path(agent_id, run_id=None):
        path = "/v1/agents/" + _valid_id(agent_id)
        return path if run_id is None else path + "/runs/" + _valid_id(run_id)

    @server.tool(structured_output=False, annotations=read)
    async def get_sandbox_capabilities(ctx: Context) -> str:
        """Discover qualified sandbox capabilities without starting compute."""
        return await call(ctx, "GET", "/v1/sandboxes/capabilities")

    @server.tool(structured_output=False, annotations=read)
    async def list_sandbox_templates(ctx: Context) -> str:
        """List available immutable environment templates."""
        return await call(ctx, "GET", "/v1/sandbox-templates")

    @server.tool(structured_output=False, annotations=write)
    async def create_sandbox(ctx: Context, idempotency_key: str, sandbox: dict[str, Any]) -> str:
        """Create and return the admission receipt."""
        _budget(sandbox)
        return await call(ctx, "POST", "/v1/sandboxes", sandbox, idempotency_key)

    @server.tool(structured_output=False, annotations=read)
    async def list_sandboxes(ctx: Context, limit: PageLimit | None = None, cursor: str | None = None,
                             name: str | None = None, status: str | None = None) -> str:
        """List sandbox metadata without waking compute. Continue with next_cursor."""
        return await call(ctx, "GET", "/v1/sandboxes", limit=limit, cursor=cursor, name=name, status=status)

    @server.tool(structured_output=False, annotations=read)
    async def get_sandbox(ctx: Context, sandbox_id: str) -> str:
        """Inspect setup, state and spending without waking the sandbox."""
        return await call(ctx, "GET", sandbox_path(sandbox_id))

    @server.tool(structured_output=False, annotations=write)
    async def submit_sandbox_command(ctx: Context, sandbox_id: str, idempotency_key: str,
                                      command: dict[str, Any]) -> str:
        """Submit an HTTP command request and return its durable execution receipt."""
        return await call(ctx, "POST", sandbox_path(sandbox_id) + "/exec", command, idempotency_key)

    @server.tool(structured_output=False, annotations=read)
    async def get_sandbox_command(ctx: Context, sandbox_id: str, exec_id: str) -> str:
        """Read command status without waiting for completion or waking compute."""
        return await call(ctx, "GET", sandbox_path(sandbox_id, exec_id))

    @server.tool(structured_output=False, annotations=read)
    async def get_sandbox_command_output(ctx: Context, sandbox_id: str, exec_id: str,
                                         after: Sequence | None = None, limit: FrameLimit | None = None) -> str:
        """Read a bounded page of recorded output frames without waiting."""
        return await call(ctx, "GET", sandbox_path(sandbox_id, exec_id) + "/stream", after=after, limit=limit)

    @server.tool(structured_output=False, annotations=destructive)
    async def cancel_sandbox_command(ctx: Context, sandbox_id: str, exec_id: str, idempotency_key: str) -> str:
        """Request cancellation with a stable key and return before cleanup completes."""
        return await call(ctx, "POST", sandbox_path(sandbox_id, exec_id) + "/cancel", {}, idempotency_key)

    @server.tool(structured_output=False, annotations=write)
    async def sandbox_files(ctx: Context, sandbox_id: str, idempotency_key: str, file: dict[str, Any]) -> str:
        """Queue a live file operation and return its execution receipt. Reads may wake paid compute."""
        return await call(ctx, "POST", sandbox_path(sandbox_id) + "/files", file, idempotency_key)

    @server.tool(structured_output=False, annotations=write)
    async def sleep_sandbox(ctx: Context, sandbox_id: str, idempotency_key: str) -> str:
        """Request sandbox sleep while preserving its saved project."""
        return await call(ctx, "POST", sandbox_path(sandbox_id) + "/sleep", {}, idempotency_key)

    @server.tool(structured_output=False, annotations=write)
    async def wake_sandbox(ctx: Context, sandbox_id: str, idempotency_key: str) -> str:
        """Request sandbox wake using its existing resource configuration."""
        return await call(ctx, "POST", sandbox_path(sandbox_id) + "/wake", {}, idempotency_key)

    @server.tool(structured_output=False, annotations=destructive)
    async def terminate_sandbox(ctx: Context, sandbox_id: str, idempotency_key: str) -> str:
        """Request compute termination. Saved project deletion remains separate."""
        return await call(ctx, "POST", sandbox_path(sandbox_id) + "/terminate", {}, idempotency_key)

    @server.tool(structured_output=False, annotations=write)
    async def create_agent(ctx: Context, idempotency_key: str, agent: dict[str, Any]) -> str:
        """Create a managed deployment with a stable idempotency key."""
        _budget(agent)
        return await call(ctx, "POST", "/v1/agents", agent, idempotency_key)

    @server.tool(structured_output=False, annotations=write)
    async def update_agent(ctx: Context, agent_id: str, idempotency_key: str, update: dict[str, Any]) -> str:
        """Publish a revision using expected_revision and a complete definition."""
        if type(update.get("expected_revision")) is not int or update["expected_revision"] < 1:
            raise ValueError("Provide a positive expected_revision from the current deployment.")
        definition = update.get("definition")
        if not isinstance(definition, dict):
            raise ValueError("Provide the complete deployment definition.")
        _budget(definition)
        return await call(ctx, "PATCH", agent_path(agent_id), update, idempotency_key)

    @server.tool(structured_output=False, annotations=read)
    async def list_agents(ctx: Context, limit: PageLimit | None = None, after: str | None = None) -> str:
        """List managed deployments. Continue with the response's next_after."""
        return await call(ctx, "GET", "/v1/agents", limit=limit, after=after)

    @server.tool(structured_output=False, annotations=read)
    async def get_agent(ctx: Context, agent_id: str) -> str:
        """Read deployment revision, queue, workers and spending."""
        return await call(ctx, "GET", agent_path(agent_id))

    @server.tool(structured_output=False, annotations=write)
    async def submit_agent_run(ctx: Context, agent_id: str, idempotency_key: str, run: dict[str, Any]) -> str:
        """Accept input durably including with zero workers."""
        return await call(ctx, "POST", agent_path(agent_id) + "/runs", run, idempotency_key)

    @server.tool(structured_output=False, annotations=read)
    async def list_agent_runs(ctx: Context, agent_id: str, limit: PageLimit | None = None, after: str | None = None) -> str:
        """List accepted runs without requesting execution."""
        return await call(ctx, "GET", agent_path(agent_id) + "/runs", limit=limit, after=after)

    @server.tool(structured_output=False, annotations=read)
    async def get_agent_run(ctx: Context, agent_id: str, run_id: str) -> str:
        """Inspect run progress, waiting, recovery, committed saves and actionable failures."""
        return await call(ctx, "GET", agent_path(agent_id, run_id))

    @server.tool(structured_output=False, annotations=read)
    async def get_agent_run_steps(ctx: Context, agent_id: str, run_id: str,
                                   limit: PageLimit | None = None, after: str | None = None) -> str:
        """Read a bounded page of journaled step outcomes."""
        return await call(ctx, "GET", agent_path(agent_id, run_id) + "/steps", limit=limit, after=after)

    @server.tool(structured_output=False, annotations=write)
    async def signal_agent_run(ctx: Context, agent_id: str, run_id: str, idempotency_key: str,
                                signal: dict[str, Any]) -> str:
        """Durably deliver a named event using the same key for uncertain retries."""
        return await call(ctx, "POST", agent_path(agent_id, run_id) + "/signals", signal, idempotency_key)

    @server.tool(structured_output=False, annotations=write)
    async def pause_agent(ctx: Context, agent_id: str, idempotency_key: str) -> str:
        """Pause deployment dispatch while preserving accepted work."""
        return await call(ctx, "POST", agent_path(agent_id) + "/pause", {}, idempotency_key)

    @server.tool(structured_output=False, annotations=write)
    async def resume_agent(ctx: Context, agent_id: str, idempotency_key: str) -> str:
        """Resume deployment dispatch within its worker limits."""
        return await call(ctx, "POST", agent_path(agent_id) + "/resume", {}, idempotency_key)

    @server.tool(structured_output=False, annotations=write)
    async def retry_agent_run(ctx: Context, agent_id: str, run_id: str, idempotency_key: str) -> str:
        """Request an eligible retry. Uncertain external effects still require explicit reconciliation."""
        return await call(ctx, "POST", agent_path(agent_id, run_id) + "/retry", {}, idempotency_key)

    @server.tool(structured_output=False, annotations=destructive)
    async def cancel_agent_run(ctx: Context, agent_id: str, run_id: str, idempotency_key: str) -> str:
        """Revoke run execution and return while resource cleanup continues."""
        return await call(ctx, "POST", agent_path(agent_id, run_id) + "/cancel", {}, idempotency_key)
