# GPU workspaces

A workspace is a saved project plus a GPU machine you can open in VS Code,
JupyterLab or SSH. Create it once, start compute when you need it, and stop
compute when you are done. Saved files survive between sessions.

```python
import nodus

with nodus.Client() as client:
    ws = client.workspaces.create("kernel-lab", gpu="H100", max_hours=4)
    ws.upload("./project")
    ws.start().wait_until_ready()
    print(ws.connect("editor")["url"])
    job = ws.run("python train.py", budget_usd=5)
    ws.stop()
```

`create` saves the configuration and rents nothing. `max_hours` is required
because the session stops itself after that many hours. The GPU model alone is
a complete request for models in the console's GPU catalog, such as `H100`,
`A100`, `H200`, `B200`, `L40S`, `L4`, `A10`, `RTX A6000`, `RTX 3090`,
`RTX 4090` and `RTX 5090`: the SDK sends the memory per GPU the console
publishes for that model. Pass `gpu_memory_gb` for other models. Project
storage defaults to the deployment limit reported by
`client.workspaces.capabilities()`, or pass `size_gb`. Sessions draw on account
funding without a per-session cap unless you pass `budget_usd`. Use
`cpus=4, memory_gb=16` instead of `gpu` for a CPU-only workspace.

`start` returns as soon as compute is requested. `wait_until_ready` polls
until the configured tool accepts connections and raises
`nodus.WorkspaceNotReadyError` if the session stops or fails first. A timeout
leaves compute running. `ws.state`, `ws.connections`, `ws.session_id` and
`ws.cost_usd` reflect the last answer, and `ws.refresh()` reads again.

`ws.charge_state` reports whether the session's compute charge is estimated
or final. A stopped session can still be finalizing its charge. The CLI shows
`Finalizing cost` until the meter reports a final charge. Once final,
`ws.final_charge_usd` and `ws.cost_usd` expose the fixed compute total.
Retaining files can continue to accrue separate usage after compute stops.
An absent meter leaves the cost unavailable.

`connect("editor")` and `connect("notebook")` return a browser `url`.
`ws.ssh()` returns the `command` to run, an `ssh_config` entry and a
`vscode_url` that opens VS Code Remote on the machine. Machines reached through
a tunnel need `cloudflared` installed locally, as the answer's `requires`
field states.

`run` submits a command against the saved project as a separate workload on
its own GPU and returns a `nodus.Workload` handle. `budget_usd` is required.
The workspace keeps running. Read the job with `job.wait()` and
`client.logs(job.id)`, and list earlier jobs with `ws.workloads()`.

`stop` saves project files and releases compute. Stopping an already stopped
workspace is not an error. `wait_until_stopped` polls until compute has
stopped. Read `ws.status_message` afterwards, which states whether the final
save succeeded. `client.workspaces.get()` accepts an ID or a unique name, and
`client.workspaces.list()` returns every workspace as a handle.

`ws.configure(gpu_count=2)` changes the saved configuration for the next
session. `ws.schedule(ready_by="2026-09-28T09:00:00Z")` has compute ready by
a time, and `ws.unschedule()` cancels that. The asynchronous client exposes
the same methods on `nodus.AsyncWorkspace`.

## Saved project files

`ws.upload(directory)` replaces the saved project while the workspace is
stopped, then waits for the server to verify the files. `ws.download(path)`
saves the current files to a verified tar archive without extracting it, and
`ws.delete_files()` removes them. The explicit forms below take the storage
revision you observed, for scripts that must not act on a revision they have
not seen:

```python
import nodus

workspace_id = "YOUR_WORKSPACE_ID"

with nodus.Client() as client:
    transfer = client.workspaces.upload_files(
        workspace_id,
        "./project",
        idempotency_key="project-upload-1",
    )
    print(transfer["state"])
    print(client.workspaces.storage())
```

An upload can return `queued` or `verifying` while the server checks the files.
Those states do not mean that saved files have been published. Observe the
workspace until `pending_upload` clears. After an interrupted upload, retry the
unchanged directory with the same key and `replace_revision`, if supplied.
Replacing saved files requires their observed revision. Symbolic links and
special files are refused, and archive preparation uses temporary disk space
proportional to the project size.

```python
import nodus

workspace_id = "YOUR_WORKSPACE_ID"
saved_revision = 1  # Replace with the saved revision shown by client.workspaces.get().

with nodus.Client() as client:
    client.workspaces.export_files(
        workspace_id,
        "./saved-project.tar",
        storage_revision=saved_revision,
    )
    client.workspaces.delete_files(workspace_id, storage_revision=saved_revision)
```

Downloads verify every segment and the complete archive before publishing the
destination. They do not extract files or forward account credentials to the
download service. Existing destination files are preserved unless
`overwrite=True` is supplied. Deletion is sent once and remains guarded by the
observed revision. `client.workspaces.storage()` reports the account's saved
file usage, allowance and charges.

## Storage volumes for sandboxes

A volume is a named store that a sandbox mounts to keep selected files between
sandbox identities. Volumes are separate from GPU workspaces.

```python
with nodus.Client() as client:
    client.volumes.create("research", size_gb=0.1)
    box = client.sandboxes.create(
        image="python:3.12",
        workspace={"name": "research", "mount": "/workspace"},
    )
    command = box.exec(["sh", "-c", "echo ready > /workspace/progress.txt"])
    command.wait()
    box.terminate()
```

One live sandbox can write a volume. A conflict includes its current holder ID.
Termination can return while the final save is pending. Observe the sandbox
until it becomes terminated before attaching the same volume to another
sandbox. List volume metadata with `client.volumes.list()` and check
`last_error` and `saved_at`. `client.volumes.iter(limit=100)` reads one page
at a time, and `rows, cursor = client.volumes.list_page(limit=100)` controls
pagination until the cursor is empty.

Volume responses are dictionaries. A server may include `expired_at`, a
nullable RFC3339 timestamp for expired saved contents, and `cleanup_pending`,
a boolean indicating that cleanup remains pending. Expiration keeps the volume
name available. Periodic saves preserve the latest useful archive. Files must
fit the configured capacity. Storage billing is disabled unless the deployment
has a configured price. `client.workspaces.create(name, size_gb=...)` without
a `gpu` creates a volume and raises a `FutureWarning`.
