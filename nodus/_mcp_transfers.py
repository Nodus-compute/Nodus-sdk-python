"""Local MCP transfers use the SDK's verified filesystem workflows."""

import json

from mcp.types import ToolAnnotations

from ._client_identity import acting_as, mcp_caller
from . import AsyncClient, AsyncSandbox, _resolve, _valid_id, _valid_idempotency_key
from ._assets import _id as _asset_id, _upload_headers
from ._projects import archive_project, project_limit
from ._sandbox_files import _hash


def register_transfer_tools(server, base_url, check_origin):
    """Register local filesystem tools with explicit paths and retry identities."""
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)

    def client():
        key, origin = _resolve(None, base_url)
        check_origin(origin)
        with acting_as(mcp_caller()):
            return AsyncClient(api_key=key, base_url=origin, timeout=300)

    @server.tool(structured_output=False, annotations=write)
    async def upload_project(project: str, idempotency_key: str) -> str:
        """Package a local folder into a verified immutable asset without renting compute."""
        key = _valid_idempotency_key(idempotency_key)
        async with client() as api:
            limit = project_limit(await api._request("GET", "/v1/sandboxes/capabilities"))
            with archive_project(project, limit) as archive:
                upload_digest = _upload_headers(archive, key)["X-Nodus-SHA256"]
                asset = await api.assets.upload(archive, idempotency_key=key)
            if asset.state != "ready":
                raise ValueError("Project upload is not ready. Inspect the asset before submitting compute.")
            return json.dumps({"asset_id": _asset_id(asset.id), "sha256": _hash(asset.raw.get("sha256")),
                               "upload_sha256": upload_digest, "stored_bytes": asset.stored_bytes})

    @server.tool(structured_output=False, annotations=write)
    async def upload_sandbox_file(sandbox_id: str, source: str, path: str, idempotency_key: str) -> str:
        """Upload a local file or folder with verified bytes. This may wake paid sandbox compute."""
        key, identifier = _valid_idempotency_key(idempotency_key), _valid_id(sandbox_id)
        async with client() as api:
            result = await AsyncSandbox(api, identifier).files.upload(source, path, idempotency_key=key)
        return json.dumps({"path": path, "verified": True, "result": result})

    @server.tool(structured_output=False, annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True))
    async def download_sandbox_file(sandbox_id: str, path: str, destination: str,
                                     idempotency_key: str, recursive: bool | None = None) -> str:
        """Download verified files to an explicit local path. Live reads may wake paid compute."""
        key, identifier = _valid_idempotency_key(idempotency_key), _valid_id(sandbox_id)
        async with client() as api:
            result = await AsyncSandbox(api, identifier).files.download(
                path, destination, recursive=recursive, idempotency_key=key)
        return json.dumps({"path": str(result), "verified": True})
