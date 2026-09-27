# Python client reference

`Client(api_key=None, base_url=None, timeout=30.0, max_retries=2)` pools HTTP
connections. With no arguments it uses your saved login and the hosted service.
Prefer a `with` block. Otherwise call `close()`.
`AsyncClient` uses `async with` or `await aclose()` and mirrors the methods below.

| Method | Result / behavior |
|---|---|
| `run(**brief)` | Accepted `Workload`. [all parameters](../parameters/index.md) |
| `run_file(path="nodus.toml")` | Accepted `Workload` from a [workload file](../../getting-started/workload-files.md) |
| `assets` | [Upload, import, list, and delete code or dataset assets](../../guides/assets.md) |
| `operations` | [Typed version 1 workload and draft operations with contract discovery](../../guides/operations.md) |
| `pools` | [Measure customer-owned GPU hosts, review forecasts, and configure Route](../../guides/pools.md) |
| `sandboxes` | [Create, reconnect to, list, and control agent sandboxes](../../guides/agent-sandboxes.md) |
| `workspaces` | [Create, start, connect to, run jobs in, and stop GPU workspaces](../../workspaces.md) |
| `volumes` | [Named storage volumes that sandboxes mount](../../workspaces.md#storage-volumes-for-sandboxes) |
| `get(id)` | Refreshed `Workload` |
| `list(limit=50, offset=0, status=None, scope=None)` | One page of workloads |
| `list_page(limit=50, offset=0, status=None, scope=None)` | `(workloads, next_offset)` |
| `iter_workloads(page_size=50, status=None, scope=None)` | Iterator over offset-based pages |
| `wait(id, poll_seconds=2.0, timeout_seconds=None, progress=None, on_update=None)` | Terminal workload. Inspect `succeeded` |
| `cancel(id, idempotency_key=None)` | Request cancellation. Returns `None` |
| `events(id, after=0)` | One page of `Event` objects |
| `iter_events(id, after=0)` | Iterator over event history |
| `stream_events(id, poll_seconds=2.0)` | Poll events until terminal |
| `artifacts(id)` | List of `Artifact` manifests |
| `logs(id, stage=None, generation=None)` | Committed log text |
| `live_logs(id, after="")` | Live log chunks, cursor, and truncation state |
| `outputs(id)` | List of `Output` objects |
| `download_output(id, name, destination, stage=None, overwrite=True)` | Verified local `Path` |
| `routing(id)` | Placement-history dictionaries ordered by stage ID and generation |
| `ledger(id)` | `Ledger` |
| `set_webhook(url, secret=None)` | Webhook configuration response dictionary |
| `get_webhook()` / `delete_webhook()` | Read configuration / remove it |
| `healthz()` / `readyz()` | Deployment health/readiness dictionaries |

Optional settings after resource IDs are keyword-only. For `download_output`,
`name` and `destination` can also be positional. Status filters accept
`nodus.WorkloadStatus` members, strings, comma-separated strings, or lists.
Accepted status strings are `accepted`, `planning`, `reserving`, `provisioning`,
`running`, `recovering`, `completed`, `failed`, and `cancelled`. The `active`
preset selects nonterminal states and `terminal` selects `completed`, `failed`,
and `cancelled`. Omit `status` for no status filter.
Unknown statuses raise `ValueError`. Pagination uses offsets. Concurrent new
submissions can shift pages. It is not a consistent historical snapshot.

## Sandbox resources

`nodus.Sandbox(name=..., image=...)` is the direct get-or-create form. Reusing
the same name reattaches without an image. It is a context manager that
terminates on exit. Call `close()` to release only the local HTTP client while
keeping the remote sandbox alive.

`client.sandboxes.create(...)` accepts an image, resource requirements, a
network policy, lifecycle, reservation, and continuity settings. It
returns an accepted `Sandbox` handle. Read `sandbox.state` or call
`sandbox.refresh()` before assuming the environment is ready.

`client.sandboxes.from_id(ID)` reconnects to a sandbox. `list()` returns one
cursor-based page and `list_page()` also returns `next_cursor`. Iterating over
`client.sandboxes` follows every page. The asynchronous client provides the
same methods and `client.sandboxes.iterate()`.

`sandbox.exec(command, ...)` accepts shell command text or an argument vector,
queues a process, and returns `SandboxExec`.
`execution.iter_output()` yields ordered `SandboxOutputFrame` objects with
`stream`, `data`, and decoded `text`. `execution.write(data, eof=False)` sends
stdin when it was enabled at execution creation. `execution.wait()` returns
when the process is terminal. Check `succeeded` and `exit_code`. Call
`sandbox.terminate()` to stop future execution and request resource cleanup.

Sandbox calls use idempotency keys for create, exec, stdin, and terminate.
Supply a stable key when your application retries after an uncertain response.
Generated keys protect the SDK transport retries within one method call.
If create, exec, or terminate returns an invalid receipt, the SDK raises
`APIError` with the sent key in `error.payload["idempotency_key"]`. Reuse that
key with the original arguments to recover the same operation.

`Workload` offers `refresh`, `wait`, `cancel`, events, logs, artifacts, outputs,
download, routing, and ledger methods without repeating the ID. Reads and waits
with `refresh()` and `wait()` update it in place. Useful attributes are `id`, `status`,
`succeeded`, `is_terminal`, `route`, `stages`, `meter`, `cost_now_usd`, `links`, and `raw`.
Each `WorkloadLink` has `kind` and `url`. Captured wandb links are available before completion.
Unknown server enum values remain strings for forward compatibility.

`workload.download(destination=None)` downloads all published customer outputs,
including automatically collected folder archives when no files were declared,
and returns a list of local `Path` objects. The default directory is
`outputs/WORKLOAD_ID`, with each file at `STAGE/NAME`. `await workload.download()` is the asynchronous equivalent.
Use `download_output(name, destination, stage=...)` for one specific file.

## Method arguments

| Argument | Meaning and default |
|---|---|
| `timeout` | HTTP request timeout in seconds, default `30.0`. Separate from a workload deadline or wait timeout. [Retry behavior](../../concepts/reliability.md#retry-behavior) |
| `max_retries` | Additional request attempts, default `2`, giving up to three total attempts. Downloads do not retry automatically |
| `limit`, `page_size` | Workloads requested per page, default `50` |
| `offset` | Number of workloads to skip, default `0`. `list_page()` returns the next offset, or `None` at the end |
| `poll_seconds` | Finite nonnegative seconds between successful polls, default `2.0` |
| `timeout_seconds` | Finite nonnegative local wait duration in seconds, default `None` for no deadline. A timeout leaves the workload running |
| `on_update` | Optional synchronous callback called with each successful workload read during `wait()`, including the terminal read. Available on sync and async clients |
| `progress` | `None` detects an interactive terminal, `True` enables output, `False` waits silently. [Live display](../../guides/monitoring-and-outputs.md#live-display) |
| `events(after)`, `iter_events(after)` | Numeric sequence of the last event seen, default `0`. Returns events with later `seq` values, oldest first. `events()` returns at most 100 per page |
| `live_logs(after)` | Opaque `next_cursor` string from the previous response, default `""` for the first page. This is not an event sequence. [Live log response](../../guides/monitoring-and-outputs.md#live-display) |
| `logs(stage)` | Stage ID to select, default `None` for no stage filter |
| `logs(generation)` | Stage attempt number to select, default `None` for no generation filter. Use a positive generation from the returned artifacts or live logs |
| `download_output(name)` | Declared output name, not its path in the container |
| `download_output(destination)` | Local file path with an existing parent directory |
| `download_output(stage)` | Stage ID to disambiguate an output name published by multiple stages, default `None` |
| `download_output(overwrite)` | `True` replaces the destination only after integrity verification. `False` refuses an existing target |
| `idempotency_key` | Stable key for a logical submission or cancellation. Omission creates a fresh key per call. [Character rules and retries](../../guides/ci-and-idempotency.md) |
| `scope` | `"mine"` or `"team"`. Omission sends no scope filter. [Personal and team history](#personal-and-team-history) |

For all declared files, `workload.download()` creates directories and refuses
to overwrite existing files. Use a new destination directory for another copy.

## Models

| Type | Useful fields |
|---|---|
| `Event` | `seq`, `id`, `type`, `payload`, `created_at` |
| `StageRun` | `id`, `status`, `completed_units`, `total_units`, optional `last_loss`, `metric_rate`, `metric_step`, `metric_total_steps`, `metric_epoch`, `metric_total_epochs` |
| `Artifact` | `manifest_id`, `stage_id`, `generation`, `sequence`, `final`, `files`, `outputs` |
| `ManifestFile` | `uri`, `sha256`, `bytes`, `media`, `is_tar` |
| `Output` | `name`, `stage_id`, `sha256`, `bytes`, `download` |
| `Route` | `sku`, `compute_class`, `fit_class`, `region`, `memory_gb`, `resources`, prices and estimated cost |
| `Meter` | `settled_usd`, `accruing_usd`, `total_now_usd`, `accruing_rate_usd_hour`, `as_of`, `compute_settled_usd`, `platform_fee_settled_usd`, `subscription_settled_usd`, `compute_accruing_usd`, `platform_fee_accruing_usd` |
| `Ledger` | `entries`, `charged_usd`, `settlement` |

`Event` has `type` and `payload`, not a `message` attribute. Output download
helpers use the authenticated API endpoint. Treat returned `download` as
server metadata rather than a URL to which you should forward credentials.

`Route.sku` is a catalog identifier, not a GPU model. When available,
`route.resources.get("accelerator")` reports the device model and
`route.resources.get("device_memory_gb")` reports its memory in GB. Missing
metadata does not prove that no GPU was used. The terminal shows `Not reported`
when it cannot identify the compute from the response. List responses may omit
the route, so use `client.get(ID)` or `workload.refresh()` for current details.

## Optional typed request dictionaries

`Source`, `Requirements`, `Policy`, `ContinuitySpec`, `StageInput`, and `StageSpec`
are `TypedDict` helpers exported by `nodus`. They support autocomplete and static
analysis while producing ordinary dictionaries:

```python
import nodus

requirements = nodus.Requirements(compute_class="accelerator", peak_memory_gb=24)
source = nodus.Source(image="pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime", command=["python", "-c", "print(1)"])
stage = nodus.StageSpec(id="example", source=source)
# Supply requirements= and stages=[stage] to client.run(...).
```

They do not add runtime validation or defaults. Existing plain dictionaries remain
supported. Stage source commands are argv lists, not shell strings.

## Personal and team history

Use `client.list(scope="mine")` for your submissions or `scope="team"` for the
team. Scope also works with `list_page()` and `iter_workloads()` and combines
with status filters. Personal history requires a member-associated credential.
Listed workloads expose `owner_user_id`, which can be absent for shared keys or
older submissions. Scope filters history and does not change team access.
