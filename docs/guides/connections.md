# External data connections

Connections are verified, team-owned references to credentials in the tenant
secret store. Supported kinds are `postgres`, `neon`, `supabase` and `wandb`.
The console shows a read-only list. Use the SDK or CLI to create, verify and
delete connections.

## Store a credential

Store a secret from a private UTF-8 file. The value is never returned by the
secret or connection read endpoints. Files contain the exact value, so omit a
trailing newline for API keys.

```sh
nodus secret set LAB_DB --from-file ./database-secret.txt
nodus connection add neon --name lab-db --secret LAB_DB --scope read --region us-east-1
nodus connection ls
nodus connection verify lab-db
```

`--secret` is an existing secret name or ID, never a credential value. Connection
names start with a letter and contain up to 128 letters, digits, underscores or
hyphens. The name `github` and prefix `conn_` are reserved.

| Kind | Secret file content | Additional connection fields |
| --- | --- | --- |
| `postgres` | A `postgres://` or `postgresql://` URL with user, password, hostname and database | None |
| `neon` | A Postgres URL, or JSON with `postgres_url` and optional `api_key` | Optional `branch` |
| `supabase` | JSON with `project_url`, `service_key` and `postgres_url` | None |
| `wandb` | The API key | Required `entity` and `project` |

Database JSON may include `ca_cert` containing a PEM certificate authority for
certificate verification. For Supabase, use the CA downloaded from the project's
database SSL settings. Its session pooler is supported when the direct database
address is unreachable over IPv4. The complete secret must fit the tenant secret
limit of 4096 UTF-8 bytes.

Database URLs support only `sslmode` and `channel_binding` query parameters.
Accepted SSL modes are `require`, `verify-ca` and `verify-full`. Every accepted
mode verifies the server certificate and hostname. TLS is also required when the
parameter is omitted. Arbitrary certificate file paths, plaintext connections,
private addresses and internal hostnames are refused.

Creating a database connection executes `SELECT 1` inside a read-only transaction.
Creating a wandb connection sends a GraphQL viewer query to `api.wandb.ai` with
the key. Verification has a five-second limit. Failure or timeout saves no
connection. Verification confirms authentication, not table permissions or
access to the selected wandb project.

Connection creation is submitted once and is not automatically retried, including
on transport errors or transient HTTP responses. If the response is lost, the
connection may already exist. Look it up with `client.connections.get("lab-db")`
or `nodus connection ls` before submitting another create request. The name is
unique within your team.

## Python

```python
from nodus import Client

with Client() as client:
    connection = client.connections.create(
        "lab-db",
        "neon",
        secret="LAB_DB",
        scope="read",
        region="us-east-1",
    )
    connections = client.connections.list()
    metadata = client.connections.get(connection["id"])
    verified = client.connections.verify(connection["id"])
    client.connections.delete(connection["id"])
```

`AsyncClient.connections` exposes the same methods with `await`. Scope is `read`,
`write` or `readwrite`. Omitting it selects `read` for database kinds and `write`
for wandb. Region is optional. Connections with no region have no location
restriction.

A connection pins one immutable secret ID and version. Rotating or revoking that
version prevents new uses. Create a new connection to adopt a new version.
Deleting a connection removes its metadata without revoking the underlying
secret. A connection referenced by an export or load cannot be deleted.

## wandb live opt-in

```sh
nodus secret set WANDB_KEY --from-file ./wandb-key.txt
nodus connection add wandb --name lab-wandb --secret WANDB_KEY --entity lab-team --project training --live
nodus connection verify lab-wandb
nodus connection rm lab-wandb
```

Only a current team administrator can enable `live_mode`, using a console session
or an API key owned by that administrator. Member keys and ownerless machine
keys cannot enable it. The action is recorded in team activity.

Only wandb supports the live flag, with `write` or `readwrite` scope. Its declared
hosts are `api.wandb.ai`, `files.wandb.ai` and `storage.googleapis.com`. Database
connections keep credentials on the control plane. Database query imports are
described below.

## Import a database query

Export a query to a normal input asset. The query runs on the control plane and
the database credential never reaches the workload.

```python
from nodus import Client

with Client() as client:
    dataset = client.assets.import_query(
        "lab-db", "SELECT id, uri, label FROM clips WHERE split='train'"
    )
    metadata = client.assets.get(dataset.id)
    print(metadata.export["row_count"])
```

Pass `dataset.id` in your workload's `inputs`, for example
`inputs={"clips": dataset.id}`. The input is a directory containing
`data.parquet`. A training program can read it with
`pandas.read_parquet(os.path.join(os.environ["NODUS_INPUT_clips"], "data.parquet"))`.
Install the appropriate Parquet reader in your training environment.

```sh
nodus asset import-query lab-db "SELECT id, uri, label FROM clips" --format parquet
nodus asset import-query lab-db "SELECT id, uri, label FROM clips" --format csv --reuse
```

`AsyncClient.assets.import_query` and `get` provide the same interface with
`await`. `format` defaults to `parquet`. CSV exports contain a header and use an
empty field for SQL nulls. Parquet preserves nullable integer, floating point,
boolean, UTF-8 text, UTC microsecond timestamp and JSON logical types. Other
Postgres types become UTF-8 strings. Use unique nonempty column aliases.

Queries must begin with `SELECT` or `WITH`. Each export uses a read-only
transaction, a ten-minute timeout and 10000-row cursor batches. Modifying CTEs
and multiple statements are refused. The maximum is 5 GB or 50 million rows,
with no request override. Existing storage quota can impose a smaller byte
limit. A limit error includes the row count reached.

`reuse=True` may return an existing ready asset from the last 24 hours for the
same team, connection, SQL, branch and format. Only outer SQL whitespace is
ignored. Rotated or revoked credentials cannot create or reuse an export.
`reuse=False` creates a fresh asset. An optional `branch` must match the Neon
branch already configured and verified on the connection. Create a separate
connection to use another branch.

An admitted export keeps its original credential version even if the secret is
rotated during execution. Delete the export asset before removing its connection.
The HTTP API returns 202 immediately after durable admission. Poll
`GET /v1/assets/{id}` until `state` is `ready` or `failed`. Failed assets expose
an `error` message, including the reached row count for size or row limits.
They remain visible after cleanup and have zero stored bytes once their
reservation is released. Delete a failed asset when you no longer need its error.

The SDK and CLI poll automatically with short HTTP requests for up to twelve
minutes, returning the ready asset or raising an error with its asset ID.
Execution and queue time together have a ten-minute limit. If observation times
out or you interrupt the client, inspect that asset before repeating the import.
Disconnecting stops observation and leaves the admitted export owned by the
server. Server shutdown cancels active queries and attempts cleanup. After a
restart, pending work resumes while expired active work is failed and cleaned.
Temporary storage failures retain the reservation until cleanup succeeds.

Query assets retain their connection's declared region when used as workload
inputs, workload or stage source assets, or sandbox source assets. If you set
`policy.data_regions`, include each input source's declared region. A connection
without a region and a run without a region constraint remain unrestricted.

After query admission, SDK observation errors expose the admitted ID in
`error.asset_id`. Async cancellation remains `asyncio.CancelledError` and should
be re-raised after your cleanup. Some Python versions wrap that exception at a
task boundary, so use `nodus.asset_id_from_error(error)` to recover the ID from
the preserved exception chain. This returns `None` if no admission ID is
available, including when another layer discards the original exception chain.

The CLI preserves the ID on failure or interruption. Inspect the exact asset
before repeating the import:

```sh
nodus asset get asset_ID
```

This displays the current state, stored bytes, export format and row count when
available, and the safe export error for a failed asset. The Python equivalent
is `client.assets.get(asset_id)` or `await client.assets.get(asset_id)`.

## Attach live wandb to a run

Use an existing administrator-enabled wandb connection with write scope. A run
accepts one connection, by name or ID. Admission pins its metadata and exact
secret version. Rotation and connection deletion do not change an admitted run.
They prevent new admission using that connection. A connection region must match
`policy.data_regions` when that policy is supplied.

```python
from nodus import Client

with Client() as client:
    workload = client.run(
        command=["python", "train.py"],
        connections=["lab-wandb"],
        sweep_id="experiment-42",
    )
    workload.refresh()
    for link in workload.links:
        print(link.url)
```

The workload image must already contain wandb and your training dependencies.
Your script calls `wandb.init()` normally. Nodus supplies `WANDB_API_KEY`,
`WANDB_ENTITY`, `WANDB_PROJECT`, `WANDB_RUN_GROUP` and `WANDB_NAME` in the process
environment. `NODUS_CONN_LAB_WANDB_KIND` is `wandb`. The default group is the Nodus
workload ID and the name includes the workload and stage IDs. Set the same scalar
`sweep_id` on several runs to group them. This does not schedule a sweep.

The key is delivered to the authenticated execution in memory. It is absent from
payloads, image layers, Docker environment files and checkpoint artifacts.
Captured logs redact the credential, including fragments split between writes.
Managed `WANDB_*`, `NODUS_CONN_*` and `NODUS_SECRET_*` names cannot be overridden
through submit or sandbox exec environment fields.

Live runs use an isolated network namespace. HTTPS can reach the union of the
connection's declared hosts and `policy.egress_allow`. HTTP, unlisted hosts,
private addresses and direct sockets are blocked. Proxy denials appear in
`workload.egress_denied` events. Additional tenant secret references can be
specified with `policy.secret_refs` and are pinned at admission. Deployments
without an enabled isolated execution provider refuse admission before acquiring
capacity. Explicit private pool placement is not supported for live runs.

Run links printed by wandb are validated against the pinned entity and project.
They appear in `workload.links`, workload detail and list responses, and the
console run row. `nodus run` prints a newly captured URL while waiting, including
with `--plain`. `nodus workload get` also displays captured links. Python callers
can use `workload.wait(on_update=callback)` to observe new links on each poll.
The sync and async clients support the same live fields.

A CLI workload file uses the same fields:

```toml
command = ["python", "train.py"]
connections = ["lab-wandb"]
sweep_id = "experiment-42"

[policy]
egress_allow = ["metrics.example.com"]
```

Sandboxes accept `connections=["lab-wandb"]` in `client.sandboxes.create` and
`Sandbox(...)`. Credentials follow the existing authenticated guest boot and
recovery channel. Reconnect preserves the admitted connection references and
secret versions. Sandbox live connections enforce the same HTTPS host union.

## Load results into a database

Declare a CSV, JSONL or Parquet result file with a database sink. Use an active
Postgres, Neon or Supabase connection with `write` or `readwrite` scope. Its
region must be allowed by the workload's `data_regions`, when supplied.

```python
from nodus import Client

with Client() as client:
    workload = client.run(
        command=["python", "train.py"],
        outputs={
            "results": {
                "path": "results.jsonl",
                "sink": {"connection": "lab-db", "table": "eval_results"},
            }
        },
    )
    workload.wait()
    for output in workload.outputs():
        print(output.name, output.sink_state, output.sink_rows, output.sink_error)
```

Your command writes the declared file. After upload, Nodus loads it on the
control plane using the credential version pinned when the workload was
admitted. Credentials never enter the workload. Secret rotation or revocation
prevents new admissions but preserves already admitted loads. A connection
referenced by saved sink outputs cannot be deleted because reload needs it.

Load state is `pending`, `loading`, `loaded` or `failed`. Workload completion
and output downloads remain available if a database load fails. Inspect the
value-free `sink_error`, correct the target schema or permissions, and retry:

```python
from nodus import Client

with Client() as client:
    workload = client.get("YOUR_WORKLOAD_ID")
    workload.reload_output("results", stage="main")
```

```bash
nodus workload outputs wl_example
nodus workload outputs wl_example --reload results --stage main
```

Table names are single PostgreSQL identifiers without a schema prefix.
Uppercase letters fold to lowercase. Names beginning with `nodus_` are reserved.
Two outputs in the same stage must use different connection and table targets.
Each file column must be a distinct identifier and cannot begin with `nodus_`.

CSV requires a header. Its columns load as `text` and empty cells become SQL
NULL. JSONL requires one object per line. Strings, booleans and numbers become
`text`, `boolean` and `numeric`. Nested objects and arrays become `jsonb`.
Missing keys and JSON null become SQL NULL. A column must keep one non-null
type across the file. Parquet supports flat nullable boolean, integer, float,
text, binary, date, timestamp, decimal and JSON columns. Unsigned integers up to
32 bits load as `bigint`, and unsigned 64-bit integers load as exact `numeric`.

Files are limited to 5 GB, 50 million rows and 256 columns. CSV records, JSONL
lines, Parquet pages and Parquet footers are limited to 8 MiB. Parquet row groups
must fit the reader's 128 MiB decoded-data allowance. A header-only CSV, an empty
JSONL file or a zero-row Parquet file loads zero rows. A CSV without a header
fails. Empty JSONL has no inferred file columns.

Nodus creates a missing table in `public`, or checks that the existing table
contains all file columns with compatible types. Every row also carries
`nodus_workload_id`, `nodus_generation`, `nodus_stage` and `nodus_loaded_at`.
Those four columns must have types `text`, `integer`, `text` and `timestamptz`.
One transaction replaces earlier rows for the same workload and stage. A failed
replacement preserves the prior successful rows. Successful newer generations
fence older loads for the same target table, even when the newer output has zero
rows. Metadata updates do not advance this fence. Reloading does not duplicate rows.

The `public.nodus_runs` table holds workload, stage and generation metadata,
including status, GPU, GPU count, region, customer charge, start and end times,
optional sweep ID, and load time. Final status and charges are updated after
completion. Supplier details are excluded. Join results to metadata with:

```sql
SELECT e.*, r.cost_usd, r.gpu, r.region
FROM eval_results e
JOIN nodus_runs r
  ON e.nodus_workload_id = r.workload_id
 AND e.nodus_stage = r.stage
 AND e.nodus_generation = r.generation
```
