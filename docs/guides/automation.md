# Connect workflows and CI

Use a Nodus API key from your workflow's secret store. Keep the image, command,
GPU requirements and output paths in a reviewed
[workload file](../getting-started/workload-files.md). Your image must already
contain your code and dependencies. These recipes do not upload the checkout.

## GitHub Actions

Add a repository or environment secret named `NODUS_API_KEY`. Commit your
`nodus.toml` containing the work you authorize. Run this workflow
manually when you intend to start paid compute:

```yaml
name: Run training on Nodus
on:
  workflow_dispatch:
permissions:
  contents: read
jobs:
  training:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: nodus-compute/Nodus-sdk-python/actions/run@v0.7.2
        id: nodus
        with:
          api-key: ${{ secrets.NODUS_API_KEY }}
          workload: nodus.toml
          output-directory: nodus-results
      - uses: actions/upload-artifact@v4
        with:
          name: nodus-results
          path: nodus-results/
          if-no-files-found: error
```

The [action](../../actions/run/action.yml) submits the file, observes the
workload and downloads outputs into stage folders. It verifies each download's
SHA-256, refuses existing files and fails if a declared output is missing.
A failed workload or incomplete download fails the step. It exposes
`workload-id`, `status`, `output-directory` and `output-count` as step outputs.
The workload ID is also printed as soon as submission succeeds.
`api-url` defaults to the Nodus hosted API origin. Set it explicitly for a
custom deployment. The action never uses a saved local account or API address.

A rerun of the same GitHub run, job and workload file reuses the submission
key. A new workflow run starts a new intentional submission. Preserve the
workload file on retries. A changed request with the same key is refused by
the API. Matrix jobs or multiple calls using the same workload file must supply
an explicit `idempotency-key` that distinguishes each intentional run and stays
the same across its retries.

`wait-timeout` defaults to 3600 seconds. It limits observation, not workload
spending or runtime. A timeout or cancelled GitHub job does not cancel Nodus
compute. Inspect the recorded workload in the console and cancel it explicitly
if needed. Charges continue until execution stops.

For stable automation, pin the action to the reviewed release commit instead
of a moving branch. GitHub's workflow permissions and environment approval
controls govern who can access the secret and start the workflow.

## Other CI runners and scheduled jobs

Install the SDK and supply `NODUS_API_KEY` through your runner's secret store.
Set a top-level `idempotency_key` in your workload file once for the intended
run. Preserve that key and file for uncertain retries:

```sh
pip install 'nodus-compute==0.7.2'
nodus run nodus.toml --plain
```

Save the returned workload ID. Download its results with:

```sh
nodus download "$NODUS_WORKLOAD_ID"
```

The download command writes to `outputs/<workload-id>/`. Do not regenerate the
submission key on a retry. Check the command's exit status and verify the
workload succeeded before treating its results as complete. See the
[CLI reference](../reference/cli.md) for status, logs and cancellation.

## HTTP workflow tools

Tools such as n8n and Make can call the customer HTTP API using their generic
HTTP request steps. This uses the [OpenAPI contract](../../openapi/openapi.yaml)
and does not require a Nodus-specific connector.

1. Store your API key in the tool's credential store and send it as a Bearer
   credential only to your Nodus API origin.
2. Prepare the workload JSON with your image, command, GPU requirements,
   output paths.
3. Call `POST /v1/workloads/validate`. Validation does not start compute or
   reserve capacity. Require `valid: true` before continuing.
4. Save a unique key for the intentional run. Call `POST /v1/workloads` with
   that value in `Idempotency-Key`, then save the returned workload ID.
5. Poll `GET /v1/workloads/{id}`. Continue to results only after `completed`.
   Handle any terminal status other than `completed` as a failure.
6. List `GET /v1/workloads/{id}/outputs`. Download each required file from its
   returned path on the same API origin. Verify its SHA-256 and byte count
   before using it in the next step.

Disable automatic redirect following for authenticated requests. Retry an
uncertain submission with its original key and unchanged body. Configure an
error branch that records the workload ID so a workflow timeout can be
investigated without launching duplicate compute.

## Workflows with remote MCP

If the workflow platform supports HTTP MCP and OAuth, add the hosted URL from
[Connect your coding agent](connect.md#quick-connection). Approve its access in
your browser. Use read-only access for reporting workflows and write access
only where submitting or cancelling work is intended. Access expires after
30 days and can be revoked in Connected agents.

For unattended jobs that cannot refresh a browser connection, use the API-key
workflow above. The [MCP reference](mcp.md) documents validation, observation
and output retrieval tools.
