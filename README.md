<div align="center">

# Nodus Python SDK

**One interface for AI workload execution and agent sandboxes.**

[![PyPI version](https://img.shields.io/pypi/v/nodus-compute)](https://pypi.org/project/nodus-compute/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://github.com/nodus-compute/Nodus-sdk-python/blob/main/pyproject.toml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)](https://github.com/nodus-compute/Nodus-sdk-python/blob/main/LICENSE)

[Documentation](https://nodus-compute.ai/docs/) · [Parameter reference](https://nodus-compute.ai/docs/reference/parameters/) · [Examples](https://github.com/nodus-compute/Nodus-sdk-python/blob/main/examples/README.md) · [Issues](https://github.com/Nodus-compute/Nodus-sdk-python/issues)

</div>

Run training, fine-tuning, and batch experiments
when your local machine lacks the GPU memory or capacity they need. Provide a
container image and resource requirements. Nodus matches the work to available
GPU capacity. Follow usage through the live meter and billing history.

Agent sandbox previews run tools in a separate environment. New
sandboxes in SDK 0.5.3 and later use the server's CPU default unless accelerator
resources are requested.
These examples require deployments
that enable CPU sandbox capacity. This preview is not generally available CPU
workload execution.
SDK 0.5.2 and earlier default ordinary sandboxes to accelerator resources.
See the [sandbox guide](https://nodus-compute.ai/docs/guides/agent-sandboxes/) before retrying an
uncertain submission across an SDK upgrade.

## 1. Install and sign in

```bash
pip install nodus-compute
nodus login
```

Get [nodus-compute on PyPI](https://pypi.org/project/nodus-compute/).
Requires Python 3.10 or newer. Upgrading an existing installation? Use
`pip install --upgrade nodus-compute`. These docs cover SDK 0.8.0.

Your browser opens Nodus sign-in. Sign in and approve the code matching your
terminal. You can then close the tab. The terminal finishes automatically and
saves your credentials. Python clients use that login without extra setup.

Running `nodus login` again reuses a valid login. Use `nodus login --force` for
a fresh sign-in.

For a machine without a browser, use `nodus login --no-browser`.
See [authentication](https://nodus-compute.ai/docs/getting-started/authentication/) for API keys and
custom deployments.

Before starting a workload, open [Billing](https://console.nodus-compute.ai/?view=billing)
and add a payment method. New accounts start with $30 in credits, but a card is
required to use them. Adding a card does not purchase credits. If you joined
a shared workspace, its administrator manages the payment method.

## 2. Run your first workload

This GPU smoke test prints the available GPU name. No local script is uploaded.
It submits paid compute without a workload spending cap. Available capacity and account
limits still determine admission.

Save this as `first_workload.py`:

<!-- test: first-workload -->
```python
import nodus

with nodus.Client() as client:
    workload = client.run(
        image="pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime",
        command=[
            "python", "-c",
            "import torch\n"
            "assert torch.cuda.is_available()\n"
            "print(torch.cuda.get_device_name(0))",
        ],
    )
    print("Workload:", workload.id)
    done = workload.wait()
    print(done.status, done.cost_now_usd)
    if not done.succeeded:
        raise RuntimeError(f"Workload {done.id} ended: {done.status}")
    print(done.logs())
```

Run it with `python first_workload.py`. It prints the workload ID and shows live logs, lifecycle events, and elapsed
time while waiting. Training workloads also show reported steps or epochs.
The final output includes status, current cost, and GPU name.

`run()` accepts the workload. `wait()` waits for a terminal status, so check
`succeeded` before using results. Ctrl+C while waiting requests cancellation
and remote resource cleanup.

The script prints the GPU name from the workload logs. For files produced by
your own program, see [logs and results](https://nodus-compute.ai/docs/guides/monitoring-and-outputs/).

## Run an agent in a sandbox

A sandbox is a durable execution environment with its own public API. It is
separate from a training or batch workload. Name it once, execute multiple
commands, stream ordered stdout and stderr frames, and reconnect with the same
name from another process.

This example requires a deployment with CPU sandbox preview enabled. Replace
the image with your published agent image, including a non-root `USER` and a
writable working directory.

```python
import nodus

with nodus.Sandbox(
    name="research-agent",
    image="ghcr.io/your-org/research-agent:1",
    requirements={"vcpus": 2, "peak_memory_gb": 4, "disk_gb": 10},
) as sandbox:
    print("Sandbox:", sandbox.id)
    process = sandbox.exec("python -c \"print('agent tool finished')\"")
    for frame in process.iter_output():
        print(frame.stream, frame.text, end="")

    done = process.wait()
    if not done.succeeded:
        raise RuntimeError(f"Command ended: {done.state}")
```

Creating a sandbox can start paid infrastructure. Nodus checks the account
payment method and available credits before billable placement.
Calling `nodus.Sandbox(name="research-agent")` reconnects to the named sandbox.
See the [sandbox guide](https://nodus-compute.ai/docs/guides/agent-sandboxes/).

The CLI mirrors the same resource and verbs.

In SDK 0.5.1, use the active name `research-agent` or the returned sandbox ID
for `NAME_OR_ID`. Use the returned ID with older releases. See
[CLI retry guidance](https://nodus-compute.ai/docs/reference/cli/#agent-sandboxes)
before retrying a request whose outcome is uncertain.

```bash
nodus sandbox new ghcr.io/your-org/research-agent:1 --name research-agent
nodus sandbox ls
nodus sandbox exec NAME_OR_ID "python -c 'print(2 + 2)'"
nodus sandbox cost NAME_OR_ID
nodus sandbox rm NAME_OR_ID
```

## Open a GPU workspace

A workspace is a saved project plus a GPU machine with VS Code, JupyterLab and
SSH. Create it once, start compute when you need it, and stop when done:

```python
import nodus

with nodus.Client() as client:
    ws = client.workspaces.create("kernel-lab", gpu="H100", max_hours=4)
    ws.start().wait_until_ready()
    print(ws.connect("editor")["url"])
    ws.stop()
```

See [GPU workspaces](https://github.com/nodus-compute/Nodus-sdk-python/blob/main/docs/workspaces.md) for uploads, SSH, background jobs
and the `nodus workspace` commands.

## Launch a GPU

`launch` rents one GPU machine, waits until SSH accepts connections and returns
its handle. It uses your `~/.ssh/id_ed25519.pub`, `id_ecdsa.pub` or
`id_rsa.pub` unless you pass `ssh_key`.
The machine stops itself after `max_hours`, which is 4 unless you set it.
Local disk is not kept after it stops. Pass `keep_files=True` to save project
files as a workspace instead.

```python
import nodus

with nodus.Client() as client:
    gpu = client.launch("H100", max_hours=2)
    print(gpu.ssh()["command"])
    gpu.stop()
```

From the terminal:

```bash
nodus launch --gpu H100 --hours 2
nodus ps
nodus ssh NAME_OR_ID
nodus stop NAME_OR_ID
```

`nodus ssh` opens the session with your OpenSSH client. Add `--print` to see
the command and config entry instead.
If the wait times out, the machine keeps running and the error names its ID.
`nodus ps` shows running instances, workspaces and training, including
machines launched with `keep_files=True`. In Python, call
`client.compute.list(include_workspaces=True)`. Both need a Nodus server that
provides the Compute list.

### SSH keys

Keys you save are admitted by every machine your team runs, including ones
already running, within a few seconds. When you launch without passing
`ssh_key`, `launch` also saves your default `~/.ssh` public key if your account
does not have it yet.

```bash
nodus ssh-key add
nodus ssh-key ls
nodus ssh-key rm FINGERPRINT
```

`nodus ssh-key add` reads `~/.ssh/id_ed25519.pub` unless you pass a path.
In Python, use `client.ssh_keys.add(public_key)`, `list()` and
`remove(fingerprint)`. Only public keys are accepted.

## Prefer the terminal?

```bash
nodus init
nodus run
```

`init` creates `nodus.toml` with the GPU smoke test. Review the
file, then `run` submits it and waits for completion. Edit the image, command,
to run your own workload. See [workload files](https://nodus-compute.ai/docs/getting-started/workload-files/).

```bash
nodus status WORKLOAD_ID
nodus logs WORKLOAD_ID
nodus cancel WORKLOAD_ID
```

## Automatic capacity selection

Nodus uses qualified estimates of runtime cost when every eligible configuration
has comparable measurements. Otherwise it orders compatible on-demand
configurations by hourly price. Available credits and independent price limits
apply in both cases. This does not guarantee the lowest total cost or shortest
runtime. Optimization tiers are not supported.
Existing optimization arguments remain accepted for backward compatibility
but have no preference effect on new runs.

Set `gpu="H100"` to require a GPU model, or omit it to let Nodus choose.
No runtime estimate is needed. See [resource options](https://nodus-compute.ai/docs/reference/parameters/requirements/).

GPU enforcement, live logs and login verification require a
compatible Nodus backend. Installing the SDK alone does not enable these server
features. See [backend compatibility](https://github.com/nodus-compute/Nodus-sdk-python/blob/main/docs/operations/errors.md#backend-compatibility) before relying on them with a custom or older deployment.

## Run your own code

Upload your script with `client.assets.upload()` or package it in a container.
Choose an image with your dependencies and pass its command to `client.run()`.

- [Run a Python script](https://nodus-compute.ai/docs/guides/containers-and-scripts/)
- [Attach code and datasets](https://nodus-compute.ai/docs/guides/assets/)
- [Train or fine-tune a model](https://nodus-compute.ai/docs/guides/gpu-workloads/)
- [Read logs and download results](https://nodus-compute.ai/docs/guides/monitoring-and-outputs/)
- [Use Nodus with a coding agent](https://nodus-compute.ai/docs/guides/agents/)
- [Measure customer-owned GPU hosts](https://nodus-compute.ai/docs/guides/pools/)
- [Run tool-driven agents in sandboxes](https://nodus-compute.ai/docs/guides/agent-sandboxes/)

For individual options, use the [Python reference](https://nodus-compute.ai/docs/reference/python/client/)
and [parameter reference](https://nodus-compute.ai/docs/reference/parameters/).
See [troubleshooting](https://nodus-compute.ai/docs/operations/errors/) if a run fails.

## Contributing

See [RELEASING.md](https://github.com/nodus-compute/Nodus-sdk-python/blob/main/RELEASING.md) for release steps. Licensed under [Apache-2.0](https://github.com/nodus-compute/Nodus-sdk-python/blob/main/LICENSE).

## Benchmark a workload

`client.benchmark()` accepts an API workload payload, `gpu_families`, `batch_sizes`, `regions`, `repetitions`, and an explicit `idempotency_key`. Reuse the same key after an uncertain response. Both synchronous and asynchronous clients return the server report.

The server divides one total cap into fixed cell allocations. Unused allocations are not redistributed. Use `{{batch_size}}` in a command argument when varying batch size. Inspect the returned workload IDs, posted ledger costs and measurements with `client.get_benchmark(id)`.

`nodus benchmark run request.json --idempotency-key customer-attempt` accepts the API JSON shape with `workload` and `matrix`. `nodus benchmark get bm_ID` prints the report. These commands require a backend with the benchmark API.

See [durable steps](https://github.com/nodus-compute/Nodus-sdk-python/blob/main/docs/durable-steps.md) for serial recorded-result replay on deployments with the capability enabled.

### Action policies and shadow readiness

`client.pools.action_policies(pool_id)` reads all four per-kind settings and the
Act kill switch. Use `set_action_policy` with explicit `kind`, `level`,
`window_cron` and `parallelism_cap` to save one policy. The sync and async clients
support the same methods. The CLI provides `pools action-policies`,
`pools action-policy` and `pools act-kill-switch`.

Approve and auto require funded Predict and active Route with current consent.
The kill switch remains available after entitlement loss. Enabling it blocks new
Act authorization while preserving cleanup. Saving a policy does not execute an
action. Predict is needed to produce recommendations.

`start_shadow` starts a future 168-hour observation cycle for an explicit policy.
`shadow_runs` and `pools shadows` expose trusted hours, elapsed gaps and
counterfactual action counts. Follow `next_cursor` with the same pool and kind.
The response reports which action kinds currently have a trusted shadow
producer. Missing observations remain gaps. Generic completed evidence does not qualify automatic actions.
A qualified cycle is evidence readiness, not permission to execute, and
counterfactual counts are neither measured savings nor completed actions.

UTC maintenance windows use five fields. Day-of-month and month must be `*`.
Minute, hour and weekday accept integers, lists, inclusive ranges or `*`.
Weekday 0 means Sunday. Steps, names and macros are unsupported.

### Act approvals and observed outcomes

`client.pools.action_proposals(pool_id)` reads retained Act proposals with optional
`kind`, `limit` and `cursor`. `approve_action_proposal` and
`reject_action_proposal` submit only the proposal identity. The CLI equivalents
are `pools action-proposals`, `pools approve-action` and `pools reject-action`.
Burst approvals remain under `pools proposals`.

Approval records intent. Dispatch checks current permissions, funding, policy,
expiry and evidence again. The inbox preserves pending, approved, applying,
uncertain, applied, failed, no-op and expired states. Only a server-observed
outcome confirms application. Measured savings remain null when unavailable and
are distinct from customer-reported savings. Default policies become approve
when funded Predict and Route are active, while explicit per-kind overrides stay
in force. Auto still requires a recent matching trusted shadow cycle.

The Route `cheaper` waiting policy needs funded Predict and current forecast
evidence of lower expected market completion cost. Missing evidence keeps the
workload waiting. Wait-tuning advice uses complete Route coverage and settled
execution outcomes to suggest bounded changes for future waits. It makes no
saving or completion-time guarantee.

### Freeze and resume saved work

`client.freeze(workload_id)` requests a freeze of checkpointed batch work with a
useful saved checkpoint and a compatible runner. `client.freeze_status` reports
whether saving and exact compute cleanup have completed. `client.resume` starts
resumption only after the workload is frozen. Each method is also available on a
`Workload` and through the async client. The CLI provides `freeze`,
`freeze-status` and `resume` with a workload ID.

The workload states `freezing` and `frozen` are nonterminal. A freeze request does
not immediately stop billing for an unresolved compute resource. The response
reports retained checkpoint bytes. Retained storage is not separately metered,
so `storage_charge_micros` is null, not an inferred zero.

Resume restarts the same customer command with saved checkpoint files. Your
training program must load its model, optimizer and progress from those files.
This does not restore arbitrary process memory or add guessed resume flags.

Observed Act monetary outcomes identify their `measurement_basis`. The basis
`observed_platform_fee_reduction_30m_v1` compares Route platform fees over equal
30-minute windows. It is not total infrastructure saving or a causal estimate.

## MCP clients

For Codex, Claude Code and Cursor, install the
[Nodus plugin](https://github.com/nodus-compute/Nodus-sdk-python/blob/main/docs/guides/plugins.md)
to add the MCP tools and setup guidance together.

Sign in once, then connect Claude, Cursor, Codex or another MCP client:

```sh
uvx --from 'nodus-compute[mcp]==0.8.0' nodus login
```

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

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if needed.
The public package starts the server and reuses your saved login. See
[MCP setup and the seven tools](https://nodus-compute.ai/docs/guides/mcp/)
for Codex setup, pip installation and examples.
