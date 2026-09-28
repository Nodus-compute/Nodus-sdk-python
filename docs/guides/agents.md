# Run GPU tasks from coding agents

Use Nodus when your task needs remote GPU execution, such as model evaluation,
fine-tuning, or a batch calculation. Your agent prepares the code, submits a
workload, observes its status, and retrieves declared results. A GPU does not
automatically make a small task faster or cheaper. Start with a bounded run
that checks the environment and output before scaling up.

For an MCP client, use the [MCP setup and tool reference](mcp.md). It covers
the local server, API key setup and workload validation and result retrieval tools.

## Install and authenticate

Requires Python 3.10 or newer:

```bash
python -m pip install --upgrade nodus-compute
nodus login
```

The PyPI distribution is [nodus-compute](https://pypi.org/project/nodus-compute/), the Python import is `nodus`, and
the terminal command is `nodus`. Use `python -m nodus.cli` if the terminal command
is not on your PATH. PyTorch is needed inside the remote image for the example
below, not in your local agent environment.

Have the user approve the matching login code in their browser. A payment
method must be configured in [Billing](https://console.nodus-compute.ai/?view=billing),
including when using starter credits. For unattended automation, supply
`NODUS_API_KEY` through a secret manager. See [authentication](../getting-started/authentication.md).

## Calculate on a GPU and download JSON

This example submits paid compute after the user authorizes the work. The calculation is small,
but provisioning and execution can still incur charges or fail to find capacity.

Save as `gpu_result.py`. Run one copy at a time in a dedicated directory. The
state file keeps the original request and its retry key before submission, then
stores the workload ID. Rerunning the script resumes that same logical run.

```python
import json
from pathlib import Path
import uuid

import nodus

program = """
import json
from pathlib import Path
import torch

if not torch.cuda.is_available():
    raise RuntimeError("CUDA is required")
values = torch.arange(1, 11, dtype=torch.float32, device="cuda")
total = float((values * values).sum().item())
if total != 385:
    raise RuntimeError("Unexpected calculation result")
result = {"sum_of_squares": total, "gpu": torch.cuda.get_device_name(0)}
Path("result.json").write_text(json.dumps(result), encoding="utf-8")
print(json.dumps(result), flush=True)
"""

state_path = Path("gpu-run.json")
if state_path.exists():
    state = json.loads(state_path.read_text(encoding="utf-8"))
else:
    state = {"request": {
        "image": "pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime",
        "command": ["python", "-u", "-c", program],
        "outputs": {"result": "result.json"},
        "idempotency_key": str(uuid.uuid4()),
    }}
    state_path.write_text(json.dumps(state), encoding="utf-8")

with nodus.Client() as client:
    if "id" in state:
        workload = client.get(state["id"])
    else:
        workload = client.run(**state["request"])
        state["id"] = workload.id
        state_path.write_text(json.dumps(state), encoding="utf-8")
    print("Workload:", workload.id, flush=True)
    done = workload.wait(timeout_seconds=600, progress=False)
    if not done.succeeded:
        raise RuntimeError(f"Workload {done.id} ended: {done.status}")
    print(done.logs())
    destination = Path(f"result-{done.id}.json")
    done.download_output("result", destination, overwrite=True)
    result = json.loads(destination.read_text(encoding="utf-8"))
    if result["sum_of_squares"] != 385 or not result["gpu"]:
        raise RuntimeError("Downloaded result did not match")
    print(destination, result)
```

Run it with `python gpu_result.py`. `image` selects the runtime and dependencies.
`command` starts the remote program. Here `-c` passes the program text, so no
local source upload is needed. For a script or project, [upload your code](containers-and-scripts.md)
or package it in the image. Naming a local filename in `command` does not upload it.

The `outputs` mapping declares which remote files become downloadable. Its key
`result` is the download name and `result.json` is the file your program writes.
Without declarations, non-empty `outputs/` and `results/` folders are preserved
as downloadable tar archives. Explicit declarations override that default.
See [logs and results](monitoring-and-outputs.md).

## Keep execution and retries under control

`run()` means accepted, not running or successful. `wait()` returns for completed,
failed, and cancelled workloads. Check `succeeded`, inspect the returned result,
and retain the workload ID and observed cost when reporting the outcome.

A wait timeout raises `nodus.APITimeoutError` and leaves the remote run active.
Reconnect using its saved ID to keep observing it. To stop it, call
`client.cancel(workload_id)` or `nodus cancel WORKLOAD_ID`, then check status
until terminal. A cancellation request is not confirmation that cleanup finished.
Ctrl+C during a synchronous wait requests cancellation. See [lifecycle and reliability](../concepts/reliability.md).

If submission has an uncertain outcome, reuse the exact saved request and
`idempotency_key`. Changing the payload under that key is a conflict. Do not
create a fresh key just because a request timed out. Keep `gpu-run.json` until
the run is accounted for. To intentionally start different work, use a new
directory and review the work you intend to run. See [CI and safe retries](ci-and-idempotency.md).

Customer budgets do not limit spending. The local wait timeout only limits observation.
Do not launch additional runs beyond the user's authorized scope.

## Choose resources deliberately

Omit `gpu` to let Nodus choose compatible capacity. For a specific requirement,
add `gpu="RTX 4090"` and `peak_memory_gb=16` to a new request. GPU model and
memory are separate constraints.
`model` is a workload description, not an instruction to download model weights.

Optimization tiers are not supported. Omit
`optimization` in new requests. Legacy arguments remain accepted for compatibility
but have no preference effect on new runs. Nodus uses qualified estimates of
runtime cost when every eligible configuration has comparable measurements.
Otherwise it orders compatible on-demand configurations by hourly price.
Available credits and independent price limits apply in both cases. Your explicit
GPU and resource requirements remain mandatory. This does not guarantee the
lowest total cost or shortest runtime. Consult the
[GPU models and resource options](../reference/parameters/requirements.md)
before choosing a model. Automatic selection can use newer GPUs, so use an
image that supports the selected hardware.

## Expand a verified workload

| Task | What to add | Practical benefit |
|---|---|---|
| Fine-tuning or evaluation | [Code and dataset assets](assets.md), required packages, declared metrics and checkpoints | Run your existing program with remote GPU memory and retrieve its artifacts |
| Parameter experiments | [Async submissions](async-sweeps.md) with a separate key for each run | Manage independent experiments from one process, subject to capacity and authorized total spend |
| Processing pipelines | [Explicit stages](multi-stage-workloads.md) and file handoffs | Express dependencies and collect named outputs from each step |
| Repeatable automation | [Workload files](../getting-started/workload-files.md) and [CI retry keys](ci-and-idempotency.md) | Keep the reviewed request reproducible across agents and job restarts |

Use the [parameter reference](../reference/parameters/index.md) for accepted
inputs and the [Python client reference](../reference/python/client.md) for
method signatures. Report failures and missing outputs as failures, with the
saved workload ID and relevant logs, rather than treating acceptance as delivery.
