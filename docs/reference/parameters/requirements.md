# Resource requirements

| Argument | Type / values | Omitted | HTTP field |
|---|---|---|---|
| `model` | Free-text workload description | No model hint | `requirements.model` |
| `compute_class` | `"accelerator"` for GPU workloads | Accelerator | `requirements.compute_class` |
| `peak_memory_gb` | Positive number in GB per GPU | No explicit memory hint | `requirements.peak_memory_gb` |
| `optimization` | `"automatic"`, `"lowest_cost"`, `"lower_cost"`, `"balanced"`, `"faster"`, `"fastest"`. Compatibility only | Not sent | `requirements.optimization` |
| `gpu` | `"A100"`, `"H100"`, `"H200"`, `"B200"`, `"A10"`, `"A10G"`, `"L4"`, `"L40"`, `"L40S"`, `"T4"`, `"V100"`, `"RTX A6000"`, `"RTX 3090"`, `"RTX 4090"`, `"RTX 5090"`. [Examples and aliases](#gpu-model) | Nodus chooses | `requirements.gpu` |
| `gpu_count` | Exactly `1`, `2`, `4`, or `8` on one machine | One GPU | `requirements.gpu_count` |
| `gpu_interconnect` | `"any"` | No topology guarantee | `requirements.gpu_interconnect` |
| `requirements` | Dictionary | Optional resource hints | `requirements` |

The workload file uses the same argument names. You do not need to predict how
long your program will run. Provide memory only when you know the requirement.
`model` describes your workload and does not download model weights.

## Optimization

Optimization tiers are not supported. New workloads use qualified estimates of
runtime cost when every eligible configuration has comparable measurements.
Otherwise Nodus orders compatible on-demand configurations by hourly price.
Available credits and independent price limits apply in both cases. This does not
guarantee the lowest total cost or shortest runtime.

Omit `optimization` in new code. The SDK accepts `automatic`, `lowest_cost`,
`lower_cost`, `balanced`, `faster`, and `fastest` for backward compatibility.
These values have no preference effect on new workload or stage routing.
The API records `automatic` for newly accepted workloads. Empty nested values
remain accepted for compatibility. The flat shortcut does not accept an empty
string.

GPU, memory, CPU, disk, image compatibility, location requirements
remain mandatory. An explicit GPU model is never replaced by another model.
Omit `gpu` to allow more compatible models. Accepted names do not establish
available capacity.

## GPU model

`gpu` is a hard requirement. Nodus never substitutes another model, including
when retrying a run. If matching capacity is unavailable, the run reports that
condition. Omit `gpu` to let Nodus choose compatible capacity.
An accepted model name does not guarantee matching capacity. GPU, memory,
and other resource requirements must all fit an available machine.

These are all accepted canonical model names. Use the Python argument shown in
`client.run()`, or the same quoted value for `gpu` in a workload file.

| GPU model | Exact Python argument |
|---|---|
| A100 | `gpu="A100"` |
| H100 | `gpu="H100"` |
| H200 | `gpu="H200"` |
| B200 | `gpu="B200"` |
| A10 | `gpu="A10"` |
| A10G | `gpu="A10G"` |
| L4 | `gpu="L4"` |
| L40 | `gpu="L40"` |
| L40S | `gpu="L40S"` |
| T4 | `gpu="T4"` |
| V100 | `gpu="V100"` |
| RTX A6000 | `gpu="RTX A6000"` |
| RTX 3090 | `gpu="RTX 3090"` |
| RTX 4090 | `gpu="RTX 4090"` |
| RTX 5090 | `gpu="RTX 5090"` |

Names are case-insensitive. Whitespace, hyphens, and underscores are ignored. An optional `NVIDIA`
prefix and compact RTX names are accepted, such as `"nvidia h100"` and
`"RTX4090"`. `"A6000"` is an alias for `"RTX A6000"`.
These names describe models, not a guarantee of current capacity.
Choose the model family and specify memory separately, such as `gpu="A100"`
with `peak_memory_gb=80`. Supplier names and machine IDs are not GPU names.

Inside a `with nodus.Client() as client:` block:

```python
workload = client.run(
    image="pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime",
    command=["python", "-c", "import torch\nprint(torch.cuda.get_device_name(0))"],
    gpu="H100",
)
```

An explicit dictionary key wins over the matching flat shortcut:
`requirements={"peak_memory_gb": 48}, peak_memory_gb=24` sends 48.
Workload files reject duplicate flat and nested settings so the choice is clear.

## Additional dictionary fields

These fields belong inside `requirements={...}` in Python or `[requirements]`
in a workload file. They are not flat `run()` arguments.

| Field | Type and units | Omitted |
|---|---|---|
| `disk_gb` | Finite nonnegative number in GB | No explicit disk requirement |
| `vcpus` | Finite nonnegative number of virtual CPUs | No explicit CPU requirement |
| `dataset_bytes` | Nonnegative integer in bytes | No dataset-size hint |
| `notes` | Text with additional workload context | No notes |

Omitted or zero disk and CPU values in a stage inherit the workload requirements.
These fields do not transfer data or install dependencies.
`nodus.Requirements(...)` provides optional static typing. The SDK validates GPU
names, compatibility values, and numeric resource bounds for both typed and
ordinary dictionaries before submission. Booleans and nonfinite numbers are
not valid resource quantities. Explicit `peak_memory_gb` must be positive.

## Multiple GPUs on one machine

Set `gpu_count=8, gpu="H100", peak_memory_gb=80` to require eight H100s on
one machine, each with at least 80 GB of memory. Supported counts are 1, 2, 4
and 8. Omission retains single-GPU behavior. A smaller allocation or a group
of machines cannot satisfy this request. Matching capacity may be unavailable.

The count does not guarantee NVLink, NVSwitch or pooled device memory.
`gpu_interconnect="any"` declares no topology constraint. Other interconnect
values are rejected because the platform cannot yet verify that guarantee.
Do not submit topology-dependent training until its topology is supported.

The displayed node hourly price covers the whole allocation, including all devices. Recovery and saved-run reuse preserve the requested count.
Nodus preserves your command arguments. Supply your own distributed launcher
and application configuration, such as `torchrun --nnodes=1 --nproc_per_node=8`.
