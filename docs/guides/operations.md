# Versioned workload and draft operations

These methods are available from the SDK source checkout and are pending a
package release. Published SDK 0.5.3 does not include `client.operations`.

Sign in with `nodus login` or configure `NODUS_API_KEY`, then use
`client.operations` on a `nodus.Client`. `nodus.AsyncClient` provides the same
methods with `await`. This interface requires a server that exposes
`/v1/operations/v1`.

Prepare a customer workload request with your image, command, resources and an
resource requirements. Call `operations.validate(workload)` to check
the request without starting compute. Submit it with
`operations.submit(workload, idempotency_key="your-stable-run-key")` when you
intend to start paid execution.

Keep the request and key for recovery. Transport retries preserve a snapshot of
the supplied workload. After an uncertain result, retry with the same request
and key. A new intentional run requires a new key. Validation does not reserve
capacity or guarantee admission.

The returned `Workload` supports observing progress and retrieving results.
Check `succeeded` after waiting. List final files with `operations.outputs(id)`
and use `client.download_output(...)` to download and verify an output.
Asynchronous submissions return `AsyncWorkload`.

## Available methods

| Method | Result |
|---|---|
| `catalog()` | `OperationCatalog` containing server definitions and argument schemas |
| `get_run_draft()` | `RunDraft` with saved form values and their revision |
| `update_run_draft(patch, expected_revision=...)` | `RunDraft` after applying the partial edit |
| `list(scope=None, limit=None, offset=None)` | `WorkloadPage` with `workloads` and `next_offset` |
| `get(workload_id)` | `Workload` with current status and meter |
| `events(workload_id, after=None)` | One page of `Event` objects |
| `logs(workload_id)` | Retained log text |
| `outputs(workload_id)` | Final `Output` metadata and download paths |
| `validate(workload)` | `WorkloadValidation` confirming validation without submission |
| `submit(workload, idempotency_key=...)` | Accepted `Workload` |
| `cancel(workload_id)` | `None` after cancellation is requested |

Omitted list arguments keep the server defaults, including team scope. Pass a
returned `next_offset` to read the next workload page. For events, pass the last
event's `seq` as `after`. Cancellation acknowledges the request while cleanup
continues asynchronously.

## Shared run forms

Use a key associated with your team membership to read and edit your saved
**New run** form. The console, personal agent and SDK share that form within
your active team. A key without a member identity receives HTTP 403.

`get_run_draft()` returns `revision`, partial `values` and `updated_at`.
`RunDraftValues` describes the saved fields. An empty form has revision zero,
empty values and no update time.

Pass a `RunDraftPatch` and the revision you read to `update_run_draft()`.
Supported fields are `name`, `command`, `image`, `gpu`, `gpu_count`, `memory_gb`,
`max_cost_usd`, `checkpoint_paths` and `result_paths`. Omit a field to leave it
unchanged. Set it to `None` to remove its saved value. The command is a form
text value. Updating the form does not submit work or authorize spending.

An identical immediate retry can return its saved revision. A conflicting edit
raises `RunDraftConflictError`. Read the latest form and review your changes
before writing again. The SDK does not apply a stale patch to a newer revision.
Validate a complete customer workload request separately before submission.

## Contract discovery

`operations.catalog()` reads definitions from the server. Each definition
includes its canonical ID, version, tool alias, argument schema, permission
scope and supported transports. These describe capabilities. Authorization is
enforced when an operation runs.

The typed methods call canonical IDs such as `workloads.submit` through the
version 1 interface. The server owns argument validation. The SDK
adds response types, retry handling and checks for invalid submission receipts.
An uncertain submission error retains the supplied key in
`error.payload["idempotency_key"]`.

See the [Python client reference](../reference/python/client.md) for workload
handles and downloads, and [HTTP request parameters](../reference/parameters/index.md)
for preparing a workload body.
