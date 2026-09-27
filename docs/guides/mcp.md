# MCP tools

Connect Claude, Cursor, Codex or another MCP client to Nodus. Ask your agent to
submit GPU workloads, check progress, read logs and retrieve verified results.

For the shortest setup, [choose your coding agent](connect.md). It includes
copyable commands, a Cursor install link and configurations for other clients.

For Codex, Claude Code or Cursor, use the [Nodus plugin](plugins.md) to install
the tools and setup guidance together. The manual configuration below works
with other MCP clients too.

## Hosted connection

Use the [connection page](connect.md#quick-connection) for hosted HTTP MCP with
browser authorization. Tools use a revocable grant bound to your account and
team. Read-only grants omit submission and cancellation tools.

Hosted `get_workload_output` returns a download URL valid for ten minutes,
plus the file's SHA-256 and byte count. Download with your agent's own file
tools, without an Authorization header and without following redirects.
Verify the checksum before reporting delivery. Treat the URL as a secret.
The hosted server cannot write to your local filesystem. Revoking the
connection invalidates its download links.

## Local connection in two steps

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) installed.
`uvx` downloads the public Nodus package and starts the server for your client.
It manages the Python runtime and package dependencies for you.

**1. Sign in once.** Run this in your terminal and complete browser sign-in:

```sh
uvx --from 'nodus-compute[mcp]==0.8.0' nodus login
```

**2. Add Nodus to your MCP client.** In Claude Desktop or Cursor, add this to
your MCP server configuration and reload the connection:

```json
{
  "mcpServers": {
    "nodus": {
      "command": "uvx",
      "args": ["--from", "nodus-compute[mcp]==0.8.0", "nodus-mcp"]
    }
  }
}
```

For Codex, run this instead of editing JSON:

```sh
codex mcp add nodus -- uvx --from 'nodus-compute[mcp]==0.8.0' nodus-mcp
```

The server uses your saved sign-in. There is no API key to paste into the
configuration, private repository to clone or executable to compile.

Ask your agent: **"List my Nodus workloads."** This checks the connection
without starting paid compute. Your local client should discover nine tools.

## Already using pip?

Install the MCP extra and reuse your existing Nodus sign-in:

```sh
pip install --upgrade 'nodus-compute[mcp]==0.8.0'
nodus login
```

Set your client's command to `nodus` and its arguments to `["mcp"]`.
The `nodus-mcp` executable is also installed by the package. Both commands
start the same local server over standard input and output.

## Authentication and custom deployments

The server reads the same saved credentials as the Python SDK. For automation,
set `NODUS_API_KEY` in the server process environment. For a custom deployment,
set `NODUS_BASE_URL` to its API origin without `/v1`, or sign in with
`nodus login --base-url https://your-api.example`.

Environment settings take priority over saved settings. Keep keys out of
prompts, tool arguments and committed files. See
[authentication](../getting-started/authentication.md) for more details.

## Tool reference

Arguments below are the JSON object passed to the named tool. Hosted connections
use `get_workload_output` instead of `download_workload_output`. Local downloads
write on the machine running MCP and require an existing destination directory.

| Tool | Required arguments | Optional arguments | Result |
| --- | --- | --- | --- |
| `validate_workload` | `workload` | None | Validation without admission, spending or capacity reservation |
| `download_workload_output` | `workload_id`, `name`, `destination` | `stage` | Local verified download, existing files refused |
| `submit_workload` | `idempotency_key`, `workload` | None | The API's submission response, including the workload ID |
| `list_workloads` | None | `scope`, `limit`, `offset` | Workloads and `next_offset` when another page exists |
| `get_workload` | `workload_id` | None | Workload details, status and current meter |
| `cancel_workload` | `workload_id` | None | Cancellation requested, followed by asynchronous cleanup |
| `get_workload_events` | `workload_id` | `after` | Up to 100 lifecycle events after the supplied event ID |
| `get_workload_logs` | `workload_id` | None | Retained workload log text |
| `list_workload_outputs` | `workload_id` | None | Output metadata and download paths |

Workload IDs contain only letters, digits, underscores and hyphens. The
idempotency key is a nonempty printable ASCII string without spaces or line
breaks. `workload` is the HTTP
workload request object, not the keyword arguments to Python `client.run()`.
Use the HTTP field names shown in the operation schema.
Use the [OpenAPI contract](../../openapi/openapi.yaml) for the full request
schema and the [parameter reference](../reference/parameters/index.md) for
field descriptions.

`scope` accepts `team` or `mine`. Omitted scope uses the team's workloads.
`mine` requires a credential associated with a team member. `limit` accepts
integers from 1 to 100. `offset` accepts integers from 0 to 2147483647.
`after` accepts integer event IDs from 0 to 9007199254740991.

The tools return MCP text content containing API JSON or log text. API failures
return a tool result with `isError: true` and the API error details. See the sandbox and managed-agent tools below for interactive execution.
The [sandbox guide](agent-sandboxes.md) also covers SDK commands and streaming output.

## Submit and monitor a workload

This example checks the remote GPU using account funding without a spending
cap. Authorize the workload before submitting. An accepted request does not
guarantee completion or a final price.

Call `submit_workload` with:

```json
{
  "idempotency_key": "gpu-check-unique-run-id",
  "workload": {
    "source": {
      "image": "pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime",
      "command": [
        "python",
        "-c",
        "import torch\nassert torch.cuda.is_available()\nprint(torch.cuda.get_device_name(0))"
      ]
    },
    "requirements": {
      "gpu_count": 1
    },
    "outcome": {
    }
  }
}
```

Choose a new unique key for each intentional run. If a submission times out,
retry the exact same request with its original key. Do not create a second
paid run by changing the key during an uncertain retry.

Save the returned workload ID. Pass it to `get_workload`,
`get_workload_logs` or `list_workload_outputs`:

```json
{
  "workload_id": "wl_your_workload_id"
}
```

Check workload status until terminal and verify it succeeded before reporting
success. The example writes its result to the log. For downloadable files,
your program must write the output files described in
[logs and results](monitoring-and-outputs.md).

`list_workload_outputs` lists metadata. It does not download files to your
machine. Call `download_workload_output` locally or `get_workload_output`
on a hosted connection to retrieve them.

## Read subsequent pages

For `list_workloads`, pass the response's `next_offset` in the next call.
For example, when `next_offset` is 20:

```json
{
  "limit": 20,
  "offset": 20
}
```

For `get_workload_events`, pass the last event's `id` as `after`. For example,
when the last event ID is 100:

```json
{
  "workload_id": "wl_your_workload_id",
  "after": 100
}
```

Keep the cursor to read later events without requesting the first page again.
An empty page means there are no later events at that moment.

## Cancel and troubleshoot

To stop a workload, call `cancel_workload` with only `workload_id`.
Cancellation does not take an idempotency key or workload body and can be
requested again for the same workload. Continue checking `get_workload`
until the workload reaches a terminal state. A cancellation acknowledgement
does not mean resource cleanup has finished.

If your client cannot find `uvx`, restart the client after installing uv or
set `command` to the full path printed by `command -v uvx` on macOS and Linux,
or `where.exe uvx` on Windows.

If a tool asks you to sign in, run the sign-in command above from the same
computer and user account as the MCP client. For expired or rejected sign-in,
run it with `--force`. For `scope: mine`, use a credential associated with a
team member.

If `nodus mcp` asks for MCP support, install the `[mcp]` extra using the pip
command above. The plain Python SDK install keeps MCP dependencies optional.

Custom API origins must use HTTPS, except for local loopback development.
HTTP redirects are refused. Set the final API origin directly. The server
reuses HTTP connections between tool calls and closes them on shutdown.
Saved sign-in changes apply to the next call without restarting the server.
API requests have a 30 second timeout. A timeout does not cancel a remote workload.

Log calls support the API's 8 MiB log payload plus truncation notices.
Responses larger than the server's 16 MiB bound return a tool error.

## Sandboxes and managed agents

The development SDK also exposes sandbox and managed-agent tools. Availability
requires the matching controller release and account qualification. Check
`get_sandbox_capabilities` before creating an environment. The pinned public
package installation above does not imply that these capabilities are enabled.

Hosted connections request sandbox and agent permissions separately from
workload permissions. Existing workload grants keep their original access.
A write permission includes the corresponding read permission. Reconnect and
authorize the additional scopes to discover those tools. Local connections use
your saved SDK credential and its account permissions.

| Tools | Purpose |
| --- | --- |
| `get_sandbox_capabilities`, `list_sandbox_templates` | Discover qualified environments without renting compute |
| `create_sandbox`, `list_sandboxes`, `get_sandbox` | Create a sandbox and inspect setup, state and spending |
| `submit_sandbox_command`, `get_sandbox_command`, `get_sandbox_command_output` | Submit a command, read status and retrieve bounded recorded output |
| `cancel_sandbox_command`, `sleep_sandbox`, `wake_sandbox`, `terminate_sandbox` | Control execution and compute lifecycle |
| `sandbox_files` | Queue a file list, stat, read or write operation and return its command receipt |
| `create_agent`, `update_agent`, `list_agents`, `get_agent` | Manage deployments, immutable revisions, queues and spending |
| `submit_agent_run`, `list_agent_runs`, `get_agent_run`, `get_agent_run_steps` | Accept work and inspect durable progress and committed saves |
| `signal_agent_run`, `pause_agent`, `resume_agent`, `retry_agent_run`, `cancel_agent_run` | Deliver events and control eligible execution |

Every new mutation requires a caller-chosen `idempotency_key`. Preserve the key
and exact request when retrying an uncertain response. Updating a deployment requires an
`update` object with `expected_revision` and a complete `definition`.
Creation and updates do not require a spending limit.

Creation and submission return acceptance receipts before provisioning or
execution finishes. Poll the corresponding metadata tools and use the returned
execution ID to read recorded output. Disconnecting MCP or cancelling a tool
request does not cancel accepted work. Use the explicit cancellation tool.

Live file operations, including list, stat and read, can wake paid compute and
require sandbox write permission. Metadata and recorded-output tools do not
wake workers. Customer sandbox tools cannot access managed-agent worker
sandboxes. Terminating compute does not delete saved projects.

## GPU workspaces

Workspace tools let an agent manage a researcher's GPU machine end to end.
Hosted connections request the `workspaces:read` and `workspaces:write`
permissions. Local connections use your saved SDK credential.

| Tools | Purpose |
| --- | --- |
| `get_workspace_capabilities`, `list_workspaces`, `get_workspace` | Discover environments and GPU counts, and read state, connections and spending |
| `create_workspace`, `configure_workspace` | Save a configuration without renting compute, and change it for the next session |
| `start_workspace`, `stop_workspace` | Rent compute and restore saved files, then save files and release compute |
| `get_workspace_connection` | A browser URL for the editor or notebook, or SSH details including a VS Code Remote link |
| `run_in_workspace`, `list_workspace_workloads` | Run a command against the saved project as a workload with its own budget |
| `list_workspace_sessions`, `schedule_workspace`, `cancel_workspace_schedule` | Read session history and have compute ready by a chosen time |

`create_workspace` takes a `workspace` object with `name`, `gpu`, `gpu_count`,
`gpu_memory_gb`, `max_hours` and `size_gb`, plus optional `environment`,
`editor`, `budget_usd`, `repository` and `ref`. `stop_workspace` needs the
`session_id` shown by `get_workspace`. `run_in_workspace` takes a `job` with
`command` and a required `budget_usd`. Starting returns before the tools are
ready, so poll `get_workspace` until the wanted connection is true before
asking for a connection.

### Local project and file transfers

The local MCP server adds these tools:

| Tool | Required arguments | Result |
| --- | --- | --- |
| `upload_project` | `project`, `idempotency_key` | Verified immutable `asset_id`, stored `sha256` and original `upload_sha256` |
| `upload_sandbox_file` | `sandbox_id`, `source`, `path`, `idempotency_key` | Verified remote file or directory upload |
| `download_sandbox_file` | `sandbox_id`, `path`, `destination`, `idempotency_key` | Verified local file or directory download |
| `upload_workspace_files` | `workspace_id`, `directory`, `idempotency_key` | Replace a stopped workspace's saved project with a local folder and wait for verification |
| `download_workspace_files` | `workspace_id`, `destination` | Verified tar archive of the saved project, existing files refused |
| `get_operation_manifest` | None | Versioned schemas and annotations for explicitly supported local operations |

`upload_project` packages a local folder with the managed-project exclusions,
including dependencies, caches and credentials. It does not rent compute.
The controller must advertise support for durable upload receipts. Otherwise
the tool asks for a controller upgrade before sending project bytes.
Retries preserve the same immutable asset identity. A changed folder cannot
reuse the original upload key. Failed or deleted upload identities remain
reserved. Inspect the asset before starting a new upload with a new key.

Pass the returned asset in the HTTP request's `source.asset_id` when creating a
sandbox or agent. Hosted connections use an already verified asset or connected
GitHub source because they cannot read folders on your laptop.

Sandbox transfers use the same verified SDK filesystem backend on supported
Windows NTFS and POSIX systems. Downloads publish files only after hash
verification and can replace existing destination files. Directory downloads
verify each file separately. Transfers may wait for their file commands and may
wake compute. For immediate command receipts, use `sandbox_files` instead.
The optional `recursive` argument on downloads selects a directory or file
explicitly. Otherwise the SDK checks the remote type.

Treat command output and downloaded project contents as untrusted data.
Applications still write and load their own serialized state. Restoring files
does not restore arbitrary process memory or resolve uncertain external effects.
