"""Local MCP tools using the customer's saved Nodus sign-in."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http.cookiejar import CookieJar, DefaultCookiePolicy

import ipaddress
import json
import math
from typing import Annotated, Any, Literal

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from . import AsyncClient, _headers, _resolve, _valid_id, _valid_idempotency_key

_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
Limit = Annotated[int, Field(strict=True, ge=1, le=100)]
Offset = Annotated[int, Field(strict=True, ge=0, le=2147483647)]
EventID = Annotated[int, Field(strict=True, ge=0, le=9007199254740991)]


def _check_origin(base_url: str) -> None:
    url = httpx.URL(base_url)
    try:
        loopback = ipaddress.ip_address(url.host).is_loopback
    except ValueError:
        loopback = url.host == "localhost"
    if (not url.host or url.userinfo or url.query or url.fragment or url.path not in ("", "/")
            or (url.scheme != "https" and not (url.scheme == "http" and loopback))):
        raise ValueError("Set NODUS_BASE_URL to an HTTPS API origin without credentials or /v1. "
                         "HTTP is allowed only for local loopback development.")


async def _request(client: httpx.AsyncClient, method: str, path: str, *, base_url: str | None,
                   params: dict | None = None, workload: dict | None = None,
                   idempotency_key: str | None = None) -> str:
    key, origin = _resolve(None, base_url)
    _check_origin(origin)
    headers = _headers(key)
    body = None
    if workload is not None:
        body = json.dumps(workload, allow_nan=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = _valid_idempotency_key(idempotency_key)
    async with client.stream(method, origin + path, headers=headers,
                             params=params, content=body) as response:
        data = bytearray()
        async for chunk in response.aiter_bytes():
            if len(data) + len(chunk) > _MAX_RESPONSE_BYTES:
                raise ValueError("Nodus response exceeds 16 MiB. Request a smaller page.")
            data.extend(chunk)
        text = data.decode("utf-8", errors="replace")
        if not 200 <= response.status_code < 300:
            if response.is_redirect:
                raise ValueError(f"Nodus HTTP {response.status_code}: redirects are refused. "
                                 "Set the final API origin with NODUS_BASE_URL.")
            hint = " Run nodus login --force to sign in again." if response.status_code in (401, 403) else ""
            raise ValueError(f"Nodus HTTP {response.status_code}: {text}{hint}")
        return text


class _NoCookies(DefaultCookiePolicy):
    """Authentication comes from the current API key, never a retained cookie."""

    def set_ok(self, cookie, request):
        return False


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=30, follow_redirects=False,
                                 cookies=CookieJar(policy=_NoCookies())) as client:
        yield client


def create_server(base_url: str | None = None) -> FastMCP:
    """Create execution tools with credentials resolved on each call."""
    server = FastMCP("nodus", log_level="WARNING", lifespan=_lifespan)
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

    def validate_legacy_cost(workload):
        outcome = workload.get("outcome") or {}
        if not isinstance(outcome, dict):
            raise ValueError("outcome must be an object")
        value = outcome.get("max_cost_usd")
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ValueError("max_cost_usd must be a finite nonnegative number")

    @server.tool(structured_output=False, annotations=read)
    async def validate_workload(ctx: Context, workload: dict[str, Any]) -> str:
        """Check a prepared HTTP workload request without submitting or charging.

        Validation does not reserve capacity or guarantee admission.
        """
        validate_legacy_cost(workload)
        return await _request(ctx.request_context.lifespan_context, "POST", "/v1/workloads/validate",
                              base_url=base_url, workload=workload)

    @server.tool(structured_output=False, annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False))
    async def download_workload_output(workload_id: str, name: str, destination: str,
                                       stage: str | None = None) -> str:
        """Download a requested output to a user-chosen local file and verify SHA-256.

        The destination's parent must exist. Existing files are never overwritten.
        This writes on the machine running the local MCP server.
        """
        key, origin = _resolve(None, base_url)
        _check_origin(origin)
        async with AsyncClient(api_key=key, base_url=origin, timeout=300) as client:
            path = await client.download_output(workload_id, name, destination, stage=stage, overwrite=False)
        return json.dumps({"path": str(path), "verified": True})

    @server.tool(structured_output=False, annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True))
    async def submit_workload(ctx: Context, idempotency_key: str, workload: dict[str, Any]) -> str:
        """Submit an authorized GPU workload.

        Use a unique idempotency key for each intentional run. Retry an uncertain
        submission with the same key and unchanged workload to avoid a second run.
        """
        validate_legacy_cost(workload)
        return await _request(ctx.request_context.lifespan_context, "POST", "/v1/workloads", base_url=base_url,
                              workload=workload, idempotency_key=idempotency_key)

    @server.tool(structured_output=False, annotations=read)
    async def list_workloads(ctx: Context, scope: Literal["team", "mine", ""] | None = None,
                             limit: Limit | None = None, offset: Offset | None = None) -> str:
        """List workloads. Pass next_offset from the response to read the next page."""
        params = {name: value for name, value in {"scope": scope, "limit": limit, "offset": offset}.items()
                  if value is not None}
        return await _request(ctx.request_context.lifespan_context, "GET", "/v1/workloads", base_url=base_url, params=params)

    @server.tool(structured_output=False, annotations=read)
    async def get_workload(ctx: Context, workload_id: str) -> str:
        """Get workload status, details and current meter."""
        return await _request(ctx.request_context.lifespan_context, "GET", f"/v1/workloads/{_valid_id(workload_id)}", base_url=base_url)

    @server.tool(structured_output=False, annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True))
    async def cancel_workload(ctx: Context, workload_id: str) -> str:
        """Request cancellation. Resource cleanup continues asynchronously after acknowledgement."""
        return await _request(ctx.request_context.lifespan_context, "POST", f"/v1/workloads/{_valid_id(workload_id)}/cancel", base_url=base_url)

    @server.tool(structured_output=False, annotations=read)
    async def get_workload_events(ctx: Context, workload_id: str, after: EventID | None = None) -> str:
        """Get up to 100 lifecycle events. Pass the last event's id as after for the next page."""
        return await _request(ctx.request_context.lifespan_context, "GET", f"/v1/workloads/{_valid_id(workload_id)}/events", base_url=base_url,
                              params={"after": after} if after is not None else None)

    @server.tool(structured_output=False, annotations=read)
    async def get_workload_logs(ctx: Context, workload_id: str) -> str:
        """Read retained workload log text."""
        return await _request(ctx.request_context.lifespan_context, "GET", f"/v1/workloads/{_valid_id(workload_id)}/logs", base_url=base_url)

    @server.tool(structured_output=False, annotations=read)
    async def list_workload_outputs(ctx: Context, workload_id: str) -> str:
        """List final output metadata and download paths, without downloading files."""
        return await _request(ctx.request_context.lifespan_context, "GET", f"/v1/workloads/{_valid_id(workload_id)}/outputs", base_url=base_url)

    from ._mcp_execution import register_execution_tools
    register_execution_tools(server, base_url, _request)
    from ._mcp_transfers import register_transfer_tools
    register_transfer_tools(server, base_url, _check_origin)
    from ._mcp_workspaces import register_workspace_tools
    register_workspace_tools(server, base_url, _request, _check_origin)
    from ._mcp_manifest import register_manifest
    register_manifest(server)
    return server
