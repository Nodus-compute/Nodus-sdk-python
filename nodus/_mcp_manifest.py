"""Public discovery metadata for explicitly registered local MCP handlers."""

from importlib.resources import files
import json

from mcp.types import ToolAnnotations

from ._operations import _catalog


_LOCAL_OPERATIONS = {
    "validate_workload": ("local.workloads.validate", "workloads:read"),
    "submit_workload": ("local.workloads.submit", "workloads:write"),
    "list_workloads": ("local.workloads.list", "workloads:read"),
    "get_workload": ("local.workloads.get", "workloads:read"),
    "cancel_workload": ("local.workloads.cancel", "workloads:write"),
    "get_workload_events": ("local.workloads.events", "workloads:read"),
    "get_workload_logs": ("local.workloads.logs", "workloads:read"),
    "list_workload_outputs": ("local.workloads.outputs", "workloads:read"),
    "download_workload_output": ("local.workload_download", "workloads:read"),
    "upload_project": ("local.project_upload", "sandboxes:write"),
    "upload_sandbox_file": ("local.sandbox_upload", "sandboxes:write"),
    "download_sandbox_file": ("local.sandbox_download", "sandboxes:write"),
    "upload_workspace_files": ("local.workspace_upload", "workspaces:write"),
    "download_workspace_files": ("local.workspace_download", "workspaces:read"),
}
_KEYED_TRANSFERS = {"upload_project", "upload_sandbox_file", "download_sandbox_file", "upload_workspace_files"}


def register_manifest(server):
    """Apply bundled schemas only to handlers explicitly present in the local server."""
    manifest = json.loads(files("nodus").joinpath("_operation_manifest.json").read_text(encoding="utf-8"))
    _catalog(manifest)
    local = []
    for definition in manifest["operations"]:
        if "local_mcp" not in definition["transports"]:
            continue
        tool = server._tool_manager.get_tool(definition["name"])
        if tool is None:
            raise ValueError("Bundled operation has no explicit local handler: " + definition["name"])
        tool.parameters = definition["inputSchema"]
        tool.description = definition["description"]
        tool.annotations = ToolAnnotations.model_validate(definition["annotations"])
        local.append(definition)
    for name, (identifier, scope) in _LOCAL_OPERATIONS.items():
        tool = server._tool_manager.get_tool(name)
        tool.parameters["additionalProperties"] = False
        if name in _KEYED_TRANSFERS:
            tool.parameters["properties"]["idempotency_key"] = {
                "type": "string", "minLength": 1, "maxLength": 256, "pattern": "^[!-~]+$"}
        local.append({"id": identifier, "name": name, "version": "v1", "description": tool.description,
                      "inputSchema": tool.parameters, "annotations": tool.annotations.model_dump(exclude_none=True),
                      "required_scope": scope, "transports": ["local_mcp"]})
    catalog = json.dumps({"version": "v1", "operations": local}, allow_nan=False)

    @server.tool(structured_output=False, annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def get_operation_manifest() -> str:
        """Read versioned operation schemas for this local server without starting compute."""
        return catalog

    server._tool_manager.get_tool("get_operation_manifest").parameters["additionalProperties"] = False
    # FastMCP otherwise relies only on its function model, which ignores extra fields.
    server._mcp_server.call_tool(validate_input=True)(server.call_tool)
