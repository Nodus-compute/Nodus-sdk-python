# Code and datasets

Assets let you upload code and attach datasets without rebuilding a container.
The image still supplies Python, libraries, and system dependencies.

## Upload or import

Inside a `with nodus.Client() as client:` block:

```python
code = client.assets.upload("train.py")
```

Uploads accept a file or archive. Imports provide alternatives:

| Call | Source |
|---|---|
| `client.assets.import_github("owner/repo", ref="main")` | Source repository |
| `client.assets.import_huggingface("owner/dataset")` | Hugging Face dataset, not model weights |
| `client.assets.import_url("https://example.com/data.csv")` | Direct HTTPS download |

These calls return an `Asset` after the transfer completes. Keep its `id` to
reuse it in later workloads. GitHub and Hugging Face imports accept `token=` for
private access. Supply tokens through your secret manager. Do not put them in
workload files. A signed HTTPS link can authorize a private URL download.

## Attach code and data

```python
import nodus

with nodus.Client() as client:
    code = client.assets.upload("train.py")
    dataset = client.assets.upload("data.csv")
    workload = client.run(
        image="YOUR_REGISTRY/trainer:v1",
        source_asset_id=code.id,
        command=["python", "train.py"],
        inputs=[{"name": "training", "asset_id": dataset.id}],
        outputs={"model": "model.bin"},
    )
    print(workload.id)
```

The source asset is extracted into the working directory. Each input is extracted
into a directory exposed to your program as `NODUS_INPUT_<name>`. In this example,
`data.csv` is inside the directory named by `NODUS_INPUT_training`.

Pass the returned asset IDs through `source_asset_id` and named `inputs` as shown
above. `client.run()` does not accept `assets` or a top-level `asset_id`. Putting
these fields, or `source_asset_id`, inside `extra` does not attach files and is
rejected before submission.

Your program must write `model.bin` in its working directory for the declared
`model` output to be available. After successful completion, call
`workload.download()` to retrieve it. See [logs and results](monitoring-and-outputs.md).

Use at most eight named inputs. Direct input URIs and arbitrary environment
variable injection are not supported. For dependencies between workload stages,
use [stage input references](../reference/parameters/stages.md).

## From the terminal

```bash
nodus upload train.py
nodus assets
```

`upload` prints the asset ID to reuse as `source_asset_id` in a workload file.
`assets` lists your uploads and imports.

## Manage stored assets

`client.assets.list()` returns up to the 500 most recent assets. `client.assets.delete(asset_id)`
removes an asset when you no longer need it. Keep assets required by pending
workloads. `AsyncClient.assets` exposes the same methods with `await`.
