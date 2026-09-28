# Run your own Python script

Upload your code, choose an image containing its dependencies, and run it on a
GPU. The SDK does not install your script's dependencies automatically.

For example, save this as `hello.py`:

```python
import torch

print(torch.cuda.get_device_name(0))
```

Then submit it from Python:

```python
import nodus

with nodus.Client() as client:
    code = client.assets.upload("hello.py")
    workload = client.run(
        image="pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime",
        source_asset_id=code.id,
        command=["python", "hello.py"],
    )
    print(workload.id)
    done = workload.wait()
    if not done.succeeded:
        raise RuntimeError(f"Workload ended: {done.status}")
    print(done.logs())
```

`assets.upload()` transfers the file explicitly. `run()` uses the uploaded asset
as the source working directory. A filename in `command` alone never uploads it.
For several source files, upload an archive or import a GitHub repository. See
[code and datasets](assets.md).

## Use a custom container

When you need additional dependencies, package them with your code in an image.
For example, put this `Dockerfile` beside `hello.py`:

```dockerfile
FROM pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime
WORKDIR /app
COPY hello.py /app/hello.py
```

Build and push to a registry Nodus can pull from. Replace the namespace below:

```bash
docker build -t YOUR_REGISTRY/hello:v1 .
docker push YOUR_REGISTRY/hello:v1
```

Submit the image without a source asset:

```python
import nodus

with nodus.Client() as client:
    workload = client.run(
        image="YOUR_REGISTRY/hello:v1",
        command=["python", "/app/hello.py"],
    )
    print(workload.id)
```

Use absolute paths for code baked into the image. Pin versions or digests for
repeatability. The SDK has no registry-credential argument, so confirm access
before using a private image. Do not bake credentials into images.
