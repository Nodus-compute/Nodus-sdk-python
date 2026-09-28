# Container image and command

`image` chooses the container environment where your code runs. A container
image packages the runtime, system libraries, and installed dependencies. For example,
`pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime` selects an environment with
PyTorch and CUDA libraries. Choose an image containing the packages your program needs.

`command` tells that environment which program to start and which arguments to
pass. In `command=["python", "train.py"]`, the first item starts Python and the
second names the script. This list is also called an argument vector, or argv.
The script must already be in the image or attached as uploaded code. Naming a
local file in `command` does not upload it.

To attach your code, upload it with `client.assets.upload()` and pass the
returned asset ID as `source_asset_id`. Nodus extracts that code into the workload
working directory. See [run your own Python script](../../guides/containers-and-scripts.md)
for a complete upload-and-run example.

In the HTTP API, `source` groups the image, command, and optional code asset.
Python callers pass `image`, `command`, and `source_asset_id` directly to
`client.run()`. The SDK builds the `source` object for you.

| Argument | Type | Default / omission | Workload file |
|---|---|---|---|
| `image` | `str` | `python:3.11-slim` for a single source | `image` |
| `command` | `list[str]` or `str` | No command is sent | `command` |
| `framework` | `"train_eval"` | Absent | `framework = "train_eval"` |

For a single-source SDK call, omitting `image` or passing an empty string selects
`python:3.11-slim`. It does not request the native runtime available when leaving
the console image field blank. For GPU workloads, pass a compatible CUDA image
with the dependencies your code needs.

Use an explicit image and command. Omitting the command is accepted by this SDK,
but is not a portable way to invoke an image entrypoint: deployment bootstrap
controls execution. It is unsuitable for a first workload.

Inside a `with nodus.Client() as client:` block:

```python
workload = client.run(
    image="pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime",
    command=["python", "-c", "print('ready')"],
)
```

A string command uses `shlex.split`. It does not invoke a shell. Prefer an argv
list. Pipes, redirects, variable expansion, and `&&` need an explicit shell, such
as `command=["sh", "-c", "python preprocess.py && python train.py"]`.

Images must contain a bootstrap fetch tool (`curl`, `wget`, or `python3`), plus
your program dependencies. Upload source files explicitly with `client.assets.upload()`. `framework` is
passed through to the control plane. It does not install a framework or replace
the need to prepare runnable code. The current compiler supports `train_eval`: it runs the same command in
prepare, train, and eval stages. Code must branch on `NODUS_STAGE_ID` and honor
the declared handoffs. Prefer explicit stages when each command differs. Do not
combine `framework` with `stages`, because framework expansion takes precedence.

When `stages` is nonempty, its stage sources replace the top-level source.
Combining it with nonempty `image`, `command`, or `source_asset_id` raises `TypeError`.

## Input and output files

| Argument | Purpose |
|---|---|
| `source_asset_id` | Uploaded/imported code asset extracted into the working directory |
| `inputs` | Named asset inputs such as `[{"name": "training", "asset_id": "ASSET_ID"}]` |
| `outputs` | Declared files such as `{"model": "model.bin"}` relative to the working directory |

`outputs` creates a single stage named `main`. Do not combine it with `stages`
or `framework`. Put `source.asset_id` on each explicit stage instead of using
`source_asset_id`. Top-level asset `inputs` can also supply staged workloads. See [code and datasets](../../guides/assets.md) for asset creation and input
paths, and [logs and results](../../guides/monitoring-and-outputs.md) for downloads.

### File declaration constraints

Use asset IDs returned by upload or import, not paths or URLs. Their format is
`asset_` followed by 1 to 64 letters, digits, or hyphens. Omitted
`source_asset_id` attaches no code asset. Omitted `inputs` attaches no named assets.

`inputs` accepts at most eight dictionaries, each containing `name` and
`asset_id`, with an optional boolean `cache` flag. Names must be unique, start with a letter, and contain at most 64
letters, digits, or underscores. For example, `training_data` is valid and
`training-data` is not. The input directory is exposed as `NODUS_INPUT_training_data`.

Set `cache: True` to allow reuse of verified input content within your team and
execution region. Cache storage uses workspace size and count limits. The first
workload that fills a cache remains its storage billing owner, including after
termination, subject to available credits. Storage billing stays disabled
until a storage rate is configured. An unavailable cache falls back to the
ordinary input. This flag does not stream external bucket data.

Output names are 1 to 64 lowercase letters, digits, dots, underscores, or
hyphens. They cannot begin with a dot, a hyphen, or `nodus.`, which Nodus
reserves for its own files. The server refuses other names at submission,
because results are stored under their output name after the work has run.
Names cannot end in a dot. Reserved file names `CON`, `PRN`, `AUX`, `NUL`, `COM1` through `COM9`, and
`LPT1` through `LPT9` are rejected without regard to case, including names with
extensions such as `CON.txt`. Stage IDs with declared outputs follow these
same portability rules in addition to the [stage ID rules](stages.md).

Output paths identify files inside the workload working directory. Use `/` for
subdirectories, such as `results/model.bin`. Absolute paths, backslashes,
colons, control characters, empty path components, and `.` or `..` components
are rejected. When a stage has no declared outputs, non-empty `outputs/` and
`results/` folders are automatically preserved as `outputs.tar` and `results.tar`.
Explicit output mappings replace this default. Save complete model bundles in a
default folder, or declare files elsewhere. See
[logs and results](../../guides/monitoring-and-outputs.md) for collection exclusions
and downloads.

## Stream a bucket object

A bucket input supplies a sequential file path through `NODUS_INPUT_<name>`.
The runner reads directly from the declared regional S3 or Google Cloud Storage
endpoint while your command consumes the file. It does not import the corpus
into Nodus storage. Read through EOF to verify the declared byte count and
SHA-256 digest. Seeking and reopening the stream are not supported.
The example descriptor below represents an object containing the three bytes `abc`.
Replace its location, byte count and digest with your own object metadata.

```python
job = client.run(
    command=["python", "train.py"],
    data_regions=["us-east-1"],
    inputs=[{
        "name": "corpus",
        "bucket": {
            "uri": "s3://training-bucket/corpus.jsonl",
            "region": "us-east-1",
            "bytes": 3,
            "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            "credential_source": "team_webhook",
        },
    }],
)
```

Use `gs://bucket/object` and the exact Google Cloud region for GCS. The execution
region must exactly match the input region. Do not put signed URLs, access keys,
or tokens in the workload definition. Bucket inputs do not accept `cache`.
Imported assets and OCI image layers use the separate optional digest cache.

A team administrator must configure the existing team webhook to handle a
synchronous `input.credentials.request`. Nodus signs this request using the
webhook timestamp and HMAC headers. The request identifies the workload, stage,
generation, input name and object descriptor. Return HTTP 200 with the same
`object` and a `credentials` object containing `expires_at` and either S3
`access_key_id`, `secret_access_key`, `session_token`, or GCS `access_token`.
Scope the temporary credential to reading the declared object and set its
expiry within one hour. Never log the response body.

Nodus keeps the credential only in memory and transfers it to the authenticated
runner over TLS. A failed, expired, unread or corrupt stream prevents successful
completion. Recovery requests a fresh credential and starts the stream from the
beginning. Your program remains responsible for loading its own saved progress
and skipping data already processed. Data received before EOF is not yet fully
verified against the declared digest.

## Database result sinks

An output value can be a path string or a dictionary containing `path` and
`sink`, where `sink` contains `connection` and `table`. String paths retain their
existing behavior. Database sinks support CSV, JSONL and flat Parquet files.
See [database result loading](../../guides/connections.md#load-results-into-a-database)
for types, limits, generation replacement and reload.
