# Nodus

Run GPU workloads with Nodus from Codex, Claude Code or Cursor. The plugin
includes nine MCP tools, browser sign-in guidance, and a workload skill
for submissions, progress, logs, cancellation and output metadata.

Follow the [installation guide](https://github.com/nodus-compute/Nodus-sdk-python/blob/main/docs/guides/plugins.md).
You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and a
Nodus account. Sign in once:

```sh
uvx --from 'nodus-compute[mcp]==0.9.0' nodus login
```

After installing the plugin, ask **"List my Nodus workloads."** This verifies
the connection without starting paid compute.

The plugin uses your saved Nodus login. No keys are embedded in the manifests.
It starts the pinned public `nodus-compute[mcp]==0.9.0` package with `uvx`.
The setup and workloads skills are shared across all three clients.

Licensed under Apache-2.0. See the
[repository license](https://github.com/nodus-compute/Nodus-sdk-python/blob/main/LICENSE).
