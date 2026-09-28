# GPU training and fine-tuning

Start by verifying CUDA in a known PyTorch image:

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
        peak_memory_gb=24,
    )
    print(workload.id)
    done = workload.wait()
    if not done.succeeded:
        raise RuntimeError("GPU smoke test did not complete successfully")
```

A feasible route needs
to fit your account, region policy, and available capacity.

For training, [build an image](containers-and-scripts.md) containing your code,
framework dependencies, and data-access logic. Then run its actual command:

```python
import nodus

with nodus.Client() as client:
    workload = client.run(
        image="YOUR_REGISTRY/trainer:v1",
        command=["python", "/app/train.py", "--epochs", "3"],
        model="LoRA-fine-tune",
        peak_memory_gb=24,
    )
    print(workload.id)
    done = workload.wait()
    if not done.succeeded:
        raise RuntimeError(f"Training ended: {done.status}")
    print(done.logs())
```

This is a template: `/app/train.py` and `--epochs` belong to your application.
`model` is a sizing hint, not a model download. Choose enough GPU memory for your program.

Declare final model files with `outputs` when you need SDK downloads. See
[multi-stage workloads](multi-stage-workloads.md). Nodus handles placement and
execution. Advanced application integrations are documented separately in the
[parameter reference](../reference/parameters/index.md).

## Eight H100s for single-node pretraining

Use an image containing your training code and its distributed dependencies.
Keep the application's training arguments in your command:

```python
with nodus.Client() as client:
    workload = client.run(
        image="YOUR_REGISTRY/trainer:v1",
        command=[
            "torchrun", "--nnodes=1", "--nproc_per_node=8",
            "/app/pretrain.py", "--config", "/app/pretrain.yaml",
        ],
        gpu="H100",
        gpu_count=8,
        peak_memory_gb=80,
    )
    print(workload.id)
```

This example requires eight H100s on one machine with at least 80 GB per GPU.
Capacity is not
guaranteed. Eight devices do not imply NVLink, NVSwitch or pooled memory.
Nodus preserves the distributed command and does not rewrite your training
arguments. See [resource requirements](../reference/parameters/requirements.md)
for count and topology validation.
