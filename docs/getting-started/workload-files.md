# Run from a workload file

Keep a reusable workload definition in `nodus.toml`. Start with:

```bash
nodus init
```

This creates a GPU smoke test. It does not start paid work or
overwrite an existing file. Review the file, then run:

```bash
nodus run
```

Nodus prints the workload ID, shows progress, and reports the final status and
current cost. A failed or cancelled workload exits with a nonzero code.

## Use your own image

Replace the starter configuration with your actual image and command:

```toml
image = "YOUR_REGISTRY/trainer:v1"
command = ["python", "/app/train.py"]
```

The image must contain your code and dependencies. The command is an argument
list, not a shell command.

To keep several configurations, save one as `train.toml`:

```bash
nodus run train.toml
```

For submission without waiting, use `nodus submit train.toml`. Keep the printed
ID to check status, collect logs, or cancel later.

## Use the same file in Python

```python
import nodus

with nodus.Client() as client:
    workload = client.run_file("train.toml")
    print(workload.id)
    done = workload.wait()
    if not done.succeeded:
        raise RuntimeError(f"Workload ended: {done.status}")
    print(done.logs())
```

`run_file()` returns after acceptance. The CLI `run` also waits. Both use the
same configuration and validation.

## Add options as needed

Top-level keys use the same names as [Python submission parameters](../reference/parameters/index.md).
For example, add `gpu = "H100"` before any
TOML table. Nested dictionaries use TOML tables:

```toml
image = "YOUR_REGISTRY/trainer:v1"
command = ["python", "/app/train.py"]
peak_memory_gb = 24

[requirements]
model = "LoRA-fine-tune"
```

Advanced files can use `[[stages]]` for [stage definitions](../reference/parameters/stages.md)
and nested tables for [policy](../reference/parameters/policy.md) and
[continuity](../reference/parameters/continuity.md). Explicit stages supply their
own sources, so omit top-level image and command. A workload file does not build
an image or automatically upload files from your computer.
