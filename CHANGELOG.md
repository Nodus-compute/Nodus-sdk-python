# Changelog

## 0.10.0

- Add the qualified `nodus:claude-assistant-v2` template. Ordinary text requests delegate useful independent work to specialists, exchange findings and save one combined answer. Greetings can answer directly. The model catalog determines availability.
- Preserve paid call, child and message identities across recovery. A refused specialist assignment requests cancellation of the accepted team without starting replacement work.
- Add `AgentMessageRecipientUnavailable` for a definitive refused peer send. Other uncertain journal outcomes continue to prevent further effects in that runtime session.

## 0.9.0

Breaking: workspaces and instances run until you stop them or account credit runs out.

- Add `client.launch(gpu)`, which rents one GPU machine and waits until SSH accepts connections. The machine needs no SSH key at launch. A local public key, when present, is admitted to that machine only, and team keys reach running machines within seconds. Retries with the same `idempotency_key` return the same machine.
- Add `client.compute.list()`, `iterate()`, `group()` and `iterate_group()`, reading the Compute list the console shows: instances and training with who launched each, and a training sweep as one item whose runs `group()` lists. The Compute list carries no cost fields.
- Add `client.ssh_keys` with `list`, `add` and `remove` for the team SSH keys every machine admits.
- Add the CLI commands `nodus launch`, `nodus ps`, `nodus ssh`, `nodus stop` and `nodus ssh-key add|ls|rm`. `nodus ssh-key add --generate` creates `~/.ssh/id_ed25519` when there is no key. `nodus ssh --print` prints the command instead of connecting.
- Add the MCP tools `launch_gpu`, `list_compute`, `stop_compute` and `add_ssh_key`.
- Requests carry a `Nodus-Client` header naming the SDK, CLI or MCP server, so the console can say what launched each machine or run.
- `client.workspaces.create`, `Workspace.configure`, `client.launch` and their asynchronous counterparts no longer take `max_hours`, `budget_usd`, `editor`, `repository` or `ref`, because the server no longer has those fields. `create` and `launch` raise `TypeError` for them, and `configure` and the MCP `create_workspace` and `configure_workspace` tools refuse them as unknown fields. There is no per-session time or spending limit. `Workspace.run(budget_usd=...)` is unchanged.
- `size_gb` is optional for `client.workspaces.create`. When it is omitted, the SDK sends no size and the server gives the project its deployment's capacity. The SDK no longer reads `capabilities()` to choose a size, and `client.launch` sends no size.
- `Workspace.ready` and `wait_until_ready` wait for both the editor and the notebook on a workspace, and for SSH on an instance. `Workspace.tool` is `ssh` for an instance and `editor` for a workspace.
- The CLI drops `nodus workspace new --max-hours`, `--budget` and `--editor`, and `nodus launch --hours`. `--size-gb` is optional. The MCP `launch_gpu` tool no longer takes `max_hours`.
- Add `RLEventEmitter` for reporting scored RL attempts from inside a workload. Events the server would reject raise `EventValidationError` before they are written, instead of after a machine is rented.
- Add `rl=RLSetup(...)` to `run()`. Mode, environment and task-count rules are checked before submission. `extra={"rl": ...}` still works.
- Add `client.rl.list_environments()`, returning the RL catalog with the examples you can run today and what was measured by running them. `RLExample.run_arguments()` supplies everything `run()` needs.
- Add `client.rl.run_example(environment_id, example_id, idempotency_key=...)`, which starts a catalog example in one request. Examples now carry an `id` and the `peak_memory_gb` they were measured with, which `run_arguments()` requests.
- Add `client.rl.summary(workload_id)`, returning each phase's scored attempts, pass rate and mean reward, and whether the baseline and evaluation scores are a like-for-like comparison, as the console's run page shows them. Add `client.rl.get_environment()` and `client.rl.example_workload()`, which returns the workload an example would submit without starting it.
- `run()` refuses output names the server cannot store. Names must be 1 to 64 lowercase letters, digits, dots, underscores or hyphens, not starting with a dot, a hyphen or `nodus.`. Such names were accepted before and the run's results were refused at upload, after the work had run.

## 0.8.0

- GPU names accept memory and count suffixes, as in `A100-40GB`, `A100-80GB:4` and `H100:2`, for `create` and `run`.
- `client.workspaces` manages GPU workspaces. `create` returns a `Workspace` handle with `start`, `wait_until_ready`, `connect`, `ssh`, `run`, `upload`, `download`, `configure`, `schedule` and `stop`, with an asynchronous counterpart.
- Named sandbox storage volumes move to `client.volumes`, including `list`, `list_page` and `iter`. `client.workspaces.list()` now returns GPU workspaces, and `client.workspaces.create(name, size_gb=...)` without a GPU still creates a volume and raises a `FutureWarning`.
- The local MCP server gains every workspace operation plus `upload_workspace_files` and `download_workspace_files`.
- `nodus workspace` commands create, start, connect, run, upload, download and stop workspaces from the terminal.
- `nodus.WorkspaceNotReadyError` names a workspace that is still starting, saving, stopped or missing an SSH key.

## 0.7.2

- Advertise hosted assistant template, model and output limits in MCP creation and update tools.
- Synchronize the public operation schemas and current installation commands with this release.

## 0.7.1

- Add the hosted Claude assistant template with an explicit budget and model selection on qualified deployments.
- Add `nodus.agent.model` for durable hosted model calls within recorded steps, with bounded polling and actionable uncertain outcomes.
- Preserve assistant conversation state through committed checkpoints and reuse recorded model responses after recovery.
- Bound saved conversation requests and identify partial answers when a model reaches its output limit.

## 0.7.0

- Add qualified tools-template sandbox creation, immutable project upload, verified file transfers, setup progress and explicit sleep or wake. These capabilities require deployment qualification and account admission.
- Add managed agent deployment, durable run submission, checkpoint status, signals and controls through the SDK and CLI. Applications still serialize and load their recovery state.
- Add sandbox and managed agent MCP operations with explicit budgets, stable mutation keys and separate observation tools.
- Support secure local transfers on Windows NTFS through the universal wheel. Reject reparse points, unsafe paths and conflicting download names.
- Preserve immutable project asset identity when creation is retried from a new client after a lost upload or create response.
- Keep installed-wheel CLI verification in the same isolated environment as the tested package.

## 0.6.0

- Remove `nodus.Devbox`, the `nodus devbox` CLI and sandbox creation profiles. Use `nodus.Sandbox` or `client.sandboxes.create` with explicit resource, lifecycle and network settings.
- Keep named sandbox reconnect, terminal sessions, repository bootstrap and explicit workspaces available to agent sandboxes. Existing sandbox IDs remain readable and can be terminated.

## 0.5.3

- Use the server CPU default for ordinary sandboxes without GPU requirements, and preserve stored resources when reconnecting by name. CPU sandboxes require a deployment with the preview enabled.
- Preserve explicit GPU and compute-class requirements. Document request identity when retrying an uncertain submission across SDK upgrades.
- Align agent installation, plugin and automation package pins with SDK 0.5.3. Validate built wheels without a fixed version in CI.

## 0.5.2

- Validate prepared MCP workload requests without starting compute and require an explicit positive submission budget.
- Download requested outputs through the local MCP server with checksum verification and no overwrites.
- Repair installer-managed agent connections and skills with backups, and diagnose local setup without signing in.
- Package a hosted MCP plugin with browser authorization and a GitHub Action that preserves submission identity across retries and verifies results.

## 0.5.1

- Resolve sandbox command targets by active name or exact ID, and accept devbox IDs with profile checks. List sandbox names and distinguish missing resources from unavailable endpoints.
- Preserve request keys after uncertain sandbox or devbox mutations. Use `--idempotency-key` to retry the same operation, and show failed execution IDs and reasons even when no logs exist.
- Retain accepted execution IDs when observation fails. Reject malformed mutation receipts with the original retry key, and expose sandbox error codes from the server's response envelope.
- Explain code and dataset attachment parameters when `assets` or `asset_id` is supplied, and reject asset aliases inside `extra` before submitting work.
- Read every initially available sandbox output page without following later writes when `follow=False`.
- Reject negative and nonfinite workload observation durations before making requests. Preserve zero durations and unbounded `timeout_seconds=None`.

## 0.5.0

- Manage write-only tenant secrets and verified Postgres, Neon, Supabase and wandb connections with sync and async clients and CLI commands. Connections pin an immutable credential version, scope and optional region. Secret values are limited to 4096 UTF-8 bytes.
- Import read-only database queries as ordinary Parquet or CSV input assets without sending database credentials to the workload. Exports are limited to ten minutes, 5 GB and 50 million rows, with existing storage quota also enforced. Optional reuse selects a matching ready export from the last 24 hours.
- Observe admitted query exports with bounded polling for up to twelve minutes. Recover an admitted asset ID after observation errors or cancellation with `nodus.asset_id_from_error`, and inspect its state and safe failure message with `nodus asset get`.
- Declare database output sinks alongside legacy output paths in Python and top-level or staged TOML workload files. CSV, JSONL and Parquet loads require write scope and expose load state, row counts and safe errors. Retry a saved output with `reload_output` or `nodus workload outputs --reload`.
- Limit each sink file to 5 GB, 50 million rows and 256 columns. CSV records, JSONL lines, Parquet pages and footers have an 8 MiB limit, and decoded Parquet row groups have a 128 MiB limit. A failed sink load leaves the output downloadable and does not change workload completion.
- Attach one administrator-enabled live wandb connection to a workload or sandbox with pinned credentials. Group workloads with an optional `sweep_id` and observe captured run links through sync and async handles, `nodus run` and `nodus workload get`.
- Restrict live connection traffic to HTTPS on declared hosts and additional `policy.egress_allow` hosts. Private destinations and explicit private pool placement are unsupported. Reserved credential environment names cannot be overridden.

## 0.4.2

- Reuse HTTP connections across MCP tool calls and close the pool on server shutdown.
- Refresh saved credentials and API origins on each call without retaining response cookies.

## 0.4.1

- Install the local MCP server from the public `nodus-compute[mcp]` package.
- Connect AI clients with `nodus-mcp` or `nodus mcp` and reuse the saved `nodus login` session.
- Provide seven workload tools with retry-safe submission, cancellation, logs, events and outputs.

- Submit benchmark matrices with an explicit total budget and idempotency key, then inspect the server report through the SDK or CLI.
- Add registered sandbox services, bounded HTTP requests and a scheduled micro-batch example.

- Expose server-reported per-unit latency and posted cost through `workload.unit_metrics`.

- Read pool forecasts, issued calibration, and paginated advisory recommendations with sync and async clients.
- Require explicit account monthly price consent before enabling Predict.
- Record manual recommendation outcomes without turning estimates into measured savings.
- Add pool forecast, recommendation, Predict, and manual outcome CLI commands.

- Expose reported sandbox network bytes through `network_usage`.
- Read sandbox lifecycle and denied-host events with `sandbox.events(after=...)`.
- Preserve failure guidance and network usage on directly created handles.
- Document non-root sandbox images and allowlisted HTTP destinations.

## 0.4.0

- Add a first-class Sandbox API for long-lived agent execution environments.
- Create and reconnect to sandboxes through `client.sandboxes`.
- Create or reattach directly with `nodus.Sandbox(name=...)` and terminate it
  automatically with a context manager.
- Run shell command text and use matching `nodus sandbox` CLI commands.
- Execute multiple commands with ordered stdout and stderr frames, stdin, status polling, and termination.
- Send accelerator requirements by default for the currently supported customer sandbox offering.
- Publish the complete Sandbox HTTP contract alongside the SDK.

## 0.3.7

- Omit optimization preferences by default and accept the automatic routing value.
- Keep legacy optimization arguments compatible without a preference effect on new runs.
- Explain that optimization tiers are coming later and new runs use the cheapest compatible on-demand capacity.

- Add typed checkpoint paths and workload-file validation for selecting saved training state.

## 0.3.6

- Use the live-verified CUDA 12.8 runtime in introductory GPU examples and generated workload files.
- Link the official PyPI package directly from the standalone agent guide.
- Preserve submission recovery keys when the server returns malformed JSON.
- Explain that optimization preferences can broaden while explicit GPU requirements remain mandatory.

## 0.3.5

- Link the official PyPI package and coding agent guide from installation docs.
- Add a complete agent workflow with saved retry keys and verified output files.
- Publish agent documentation links in package metadata and clarify routing preferences.
- Keep runtime API and CLI behavior unchanged from 0.3.4.

## 0.3.4

- Show the correct next command after creating a custom workload file.
- Distinguish empty filtered workload lists from an empty account history.
- Display reported GPU metadata instead of internal catalog placeholders.

## 0.3.3

- Validate resource quantities and stage progress counts before submission.
- Accept hyphenated and underscored GPU aliases and document every GPU choice.
- Explain GPU and optimization compatibility and container execution clearly.
- Explain required payment methods before the first workload.
- Preserve unavailable route cost and runtime estimates as `None`.
- Accept typed disk and CPU requirements and inherited stage preferences in workload files.
- Preserve the CLI recovery key when a submission response is lost or uncertain.
- Reject invalid budgets and numeric CLI flags with actionable errors.
- Show small nonzero costs without rounding them to a free run.
- Use a compatible progress spinner in legacy Windows terminal encodings.
- Validate deeply nested stage dependencies without exhausting the Python stack.
- Clarify backend compatibility, CLI ranges, and submission recovery in the docs.
- Verify installed release artifacts with one isolated command and after PyPI publication.

## 0.3.2

- Make README links usable from PyPI and align CLI documentation with SDK 0.3.x.
- Explain optional budgets, account limits, and pending cost accounting accurately.
- Clarify log pagination, status refresh, cancellation, and output download behavior.
- Remove an asset lookup entry that the hosted API does not serve.

## 0.3.1

- Show finished stage status when training metrics are unavailable.
- Label missing compute details without implying that a finished run is pending.
- Explain unavailable sign-in verification without referring to a missing run.

## 0.3.0

- Add optional optimization preferences and a strict GPU model requirement.
- Remove customer runtime estimates. Existing callers should remove that argument.
- Add Rich terminal tables, status summaries, live logs, and reported training progress.
- Reuse valid logins, show email, and add `login --force` for a fresh sign-in.
- Replace noisy budget warnings and raw CLI failures with concise guidance.
- Add `wait(progress=...)` and cursor-based `live_logs()` for sync and async clients.
- Support hard workload and account caps with the compatible backend release.


## 0.2.1

- Update the default hosted API endpoint.
- Move saved settings and explicit URLs for the retired hosted endpoint to the
  current endpoint. Custom deployment URLs keep their existing behavior.

## 0.2.0

- Simplify the CLI around workload files and short commands. Replace `get` with
  `status`, use `wait` to observe, and separate blocking `run` from `submit`.
- Add `init`, `download`, `upload`, and `assets`. Remove inline submission flags.
- Share validated TOML configuration between the CLI and sync or async Python.
- Add code and dataset uploads, GitHub and Hugging Face dataset imports, and URL imports.
- Add source assets, named inputs, and simple output declarations to Python submissions.
- Download all declared outputs with integrity checks and safe stage directories.
- Add personal and team workload listing with member attribution.
- Organize documentation from first workload to data, results, and advanced pipelines.


## 0.1.3

- Print workload statuses as readable values such as `completed`.
- Execute documentation examples and terminal commands in package CI.

- Connect to hosted Nodus with `nodus login` without an API URL.
- Show terminal activity while browser approval is pending.
- Keep custom endpoint overrides and saved configuration precedence.
- Lead the quickstart and workload guides with Python examples.


## 0.1.2

- Focus customer onboarding and examples on GPU workloads and clear installation steps.
- Cancel the current workload on Ctrl+C during synchronous waits and attached CLI
  observation. Show terminal activity and distinguish absent logs from missing work.

- Login-first README, canonical task guides and parameter references, plus
  executable documentation checks. Existing wiki paths link to the new guides.
- Sync and async `outputs()`, `download_output()`, and `routing()` methods.
  Downloads stream to a temporary file and verify SHA-256 and declared length
  before replacing the destination. Response-provided URLs are never followed.
- Optional typed request dictionaries and stage loss, rate, and step metrics.
- `nodus run --compute-class vm|accelerator` selects the compute category.
  Existing calls and defaults are unchanged.
- Public OpenAPI contract copy and a cross-repository synchronization command.

- `nodus login`: approve a short code in the browser once and the SDK writes
  `~/.nodus/config.toml` itself, no key to copy. Supply your deployment API address with `--base-url`.
  `--no-browser` prints the address instead of opening it. `nodus logout`
  removes the stored key, names the `key_id` to revoke, and says that revoking
  it is a separate act in the console. **Verified against a local test double.
  Not yet run against a deployed control plane.**
- The config file is proven writable *before* the exchange starts, because the
  console mints the key inside the call that releases it, a write that failed
  afterwards would leave a live key nobody had a copy of. If a write fails
  anyway, the key is printed once so it can be stored or revoked.
- A key the client could never send, one carrying a control character, a
  space, or non-ASCII, is refused at login rather than stored, and
  `save_credentials` refuses it for any caller: a stored key that cannot
  travel in a request header fails every later command, after only ever
  having been shown redacted. Control characters (C0 and C1 alike) are
  refused in every stored value. Non-ASCII text such as a tenant name is not.
  When that refusal meets a value already in the file, a foreign section's
  included, it names the file and the way out, not just the offending key.
- A wire string that arrives as the wrong JSON type reads as absent instead
  of crashing a listing: `nodus artifacts` no longer raises on a digest sent
  as a number, and `nodus explain` no longer raises on a non-numeric device
  memory. The sign-in page address is never opened if it carries any control
  character, the C1 range included.
- Both commands say so on stderr when `NODUS_API_KEY` (or `NODUS_BASE_URL`) is
  set: it outranks the file, so "signed in" and "logged out" would otherwise
  both be wrong.
- `Client()` and `AsyncClient()` read that file when nothing else supplies a
  setting. Precedence is resolved per setting, highest first: explicit
  argument, environment, then the file, so a stale login cannot outrank what
  CI injected.
- `ConfigurationError` now offers `nodus login` alongside the two exports.

## 0.1.1, 2026-09-02

Documentation and packaging. No change to what the client sends or raises.

- Every example in the README is executed before it is written down, and
  `tests/test_readme_examples.py` parses every `nodus ...` line in the README
  with the real CLI parser. The two lines the CLI rejected are corrected:
  `nodus status` → `nodus get`, and `nodus run --command "..."` →
  `nodus run --budget 20 -- python train.py`.
- README: `event.message` (no such attribute) → `event.type` / `event.payload`.
  "only `command` is required" corrected, nothing is, the default image fills
  in. The log-lags-by-a-checkpoint caveat now sits next to `logs()`. The error
  table lists every exception class and whether it clears on its own. The
  LICENSE link is absolute so it resolves on the PyPI page.
- The suite passes against an installed wheel, and CI now installs the built
  wheel into a fresh venv and runs the tests against it from outside the source
  tree, the artifact customers download is the artifact tested.
- The paid end-to-end journey test cancels its workload in a `finally`, so a
  failed assertion cannot leave a live run billing.
- Supply chain: the PyPI publish action is pinned to a release commit SHA and
  the CI jobs run with `contents: read`.

## 0.1.0, 2026-09-02

First public release.
