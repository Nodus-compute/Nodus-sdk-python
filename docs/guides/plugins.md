# Nodus plugins

Install Nodus in Codex, Claude Code or Cursor to run GPU workloads from your
coding agent. The plugins include MCP tools and two skills for setup and workload execution.
Choose hosted browser sign-in or a local package that reuses saved credentials.

For a direct MCP connection or an Add to Cursor install link, use
[Connect your coding agent](connect.md). Choose one MCP installation method
per client to avoid duplicate tools.

## Hosted plugin with browser sign-in

The `nodus-hosted` plugin bundles remote MCP and skills. It requires no local
Python or uv installation. Enable one Nodus plugin or manual connection at a
time to avoid duplicate tools.

Claude Code:

```sh
claude plugin marketplace add nodus-compute/Nodus-sdk-python
claude plugin install nodus-hosted@nodus
```

Codex:

```sh
codex plugin marketplace add nodus-compute/Nodus-sdk-python
codex plugin add nodus-hosted@nodus
```

Authenticate Nodus through your client's MCP controls, then ask it to list
workloads. In Cursor, use the hosted install button on the
[connection page](https://nodus-compute.ai/connect/) or import the repository's
`plugins/nodus-hosted` package into your team marketplace.

These packages are distributed through the Nodus repository marketplace.
Public vendor directory listings require separate vendor review. No public
listing is required to use the direct install buttons or repository commands.


## Sign in once

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```sh
uvx --from 'nodus-compute[mcp]==0.7.2' nodus login
```

Complete browser sign-in on the same machine where your client runs. The
plugin reuses this saved login. No API key belongs in your plugin settings.

## Codex

Run in your terminal:

```sh
codex plugin marketplace add nodus-compute/Nodus-sdk-python
codex plugin add nodus@nodus
```

Start a new Codex task, then ask **"List my Nodus workloads."** You can also
ask **"Help me connect Nodus"** to use the setup skill.

## Claude Code

Run in your terminal:

```sh
claude plugin marketplace add nodus-compute/Nodus-sdk-python
claude plugin install nodus@nodus
```

Restart Claude Code. Ask **"List my Nodus workloads"** or run `/nodus:setup`
for connection help. `/nodus:workloads` loads the workload guide.

## Cursor

The plugin can be installed locally without a marketplace listing. Download
the [public repository ZIP](https://github.com/nodus-compute/Nodus-sdk-python/archive/refs/heads/main.zip)
and extract it. Copy its `plugins/nodus` folder to:

- macOS/Linux: `~/.cursor/plugins/local/nodus`
- Windows: `%USERPROFILE%\.cursor\plugins\local\nodus`

Copy the whole folder, including its hidden manifest directories and
`.mcp.json`. Do not nest an extra `nodus` folder inside the destination.
Restart Cursor or run **Developer: Reload Window**, then open **Customize**
and check that Nodus's skills and MCP server appear. Ask
**"List my Nodus workloads."**

If the local plugin does not appear on a managed account, ask your Cursor
admin to check **Allow Local Plugin Imports**. This is off by default on
Enterprise. An installed marketplace plugin with the same name takes
precedence over a local copy.

For teams, import `https://github.com/nodus-compute/Nodus-sdk-python` from
**Dashboard > Plugins & MCPs > Team Marketplaces > Add Marketplace > Import
from Repo**. Team marketplaces require a compatible Cursor plan. See
[Cursor's plugin documentation](https://cursor.com/docs/plugins).

Nodus does not yet have a public Cursor Marketplace listing. Installing the
local plugin or importing the repository does not depend on that listing.

## Use the tools

The first workload listing checks your connection without launching paid
compute. The plugin exposes:

- `validate_workload`
- `download_workload_output` locally or `get_workload_output` when hosted
- `submit_workload`
- `list_workloads`
- `get_workload`
- `cancel_workload`
- `get_workload_events`
- `get_workload_logs`
- `list_workload_outputs`

For a new run, give the agent your container image, command, GPU requirements
and expected outputs. For example: **"Prepare my training command for Nodus
and show me the request before submitting."** Preparing a
request does not start a workload.

The workload skill helps the agent preserve your request, avoid duplicate
submissions after uncertain responses, and check results before reporting
success. Output listing returns metadata. Use the download tool to retrieve
the requested files. See the
[MCP tool reference](mcp.md#tool-reference) for arguments and examples.

## Troubleshooting and updates

If you already configured Nodus manually, disable that duplicate MCP entry
when switching to the plugin. Keep other servers intact.

If `uvx` is missing, install uv and restart your client. GUI clients may need
the full path to `uvx` in their MCP command. Find it with `command -v uvx` on
macOS/Linux or `where.exe uvx` on Windows.

If sign-in fails, rerun the login command. A remote client must sign in on
the machine that runs the MCP server. Environment overrides take precedence
over saved settings. See [MCP authentication](mcp.md#authentication-and-custom-deployments).

Update the plugin through your client's plugin manager. For a local Cursor
installation, replace the plugin folder with the version from a fresh
download and reload. The plugin pins its MCP dependency to a tested release.
Updating a separate global Python installation does not change that pin.

## Plugin development

All three client manifests live in the public repository's `plugins/nodus`
folder and share one `.mcp.json` and the same skills. The marketplace manifests
live at `.agents/plugins/marketplace.json`, `.claude-plugin/marketplace.json`
and `.cursor-plugin/marketplace.json` in the repository root.

From a checkout, use its absolute path in place of
`nodus-compute/Nodus-sdk-python` in the Codex or Claude marketplace command.
This tests the local files without publishing them. Cursor loads the same
folder from its local plugin directory.

Validate the plugin with `claude plugin validate plugins/nodus --strict`.
Run `python scripts/verify-plugins.py` with the SDK's development dependencies
and uv installed to test the declared launch commands against a local HTTP
fixture. This check downloads the pinned public package and does not start
paid compute.

See the [Claude plugin reference](https://code.claude.com/docs/en/plugins-reference)
and [Cursor plugin reference](https://cursor.com/docs/reference/plugins)
for their manifest contracts. A public Cursor listing requires submission at
[Cursor Marketplace](https://cursor.com/marketplace/publish) and vendor review.
