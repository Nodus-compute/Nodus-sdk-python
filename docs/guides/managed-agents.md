# Managed durable agents

Managed agent methods require SDK 0.7.0 or later. Availability depends on
account admission and deployment qualification.

Managed agents require both a qualified tools environment and account admission
on the deployment. They are a gated CPU capability. Their presence in the SDK
does not establish production qualification or general CPU workload support.
Authenticate with `nodus login` or provide `NODUS_API_KEY` through your secret
manager. Never put that account key in the uploaded project.

For bounded task batches with independent state and dependency ordering, see
[agent groups](agent-groups.md).

## Deploy and submit

Save this synchronous entrypoint as `agent.py` in your project:

```python
import nodus

@nodus.step(name="calculate", version="1", effect="pure")
def calculate(values):
    return sum(values)

def main(event):
    total = calculate(event["values"], _step_id="calculate")
    return {"total": total}
```

Deploy the project and submit input from your authenticated client:

```python
import nodus

with nodus.Client() as client:
    agent = client.agents.create(
        name="calculator",
        project=".",
        entrypoint="agent:main",
        max_workers=4,
        idempotency_key="calculator-deployment-001",
    )
    run = agent.submit(
        {"values": [1, 2, 3]},
        idempotency_key="calculation-001",
    )
    print(agent.id, agent.url, run.id, run.status)
```

The $20 limit is an explicit authorization across the agent's workers. The
server controls cost accounting and reserved exposure. Defaults are supplied
by the server. Omitted scaling settings use zero minimum workers and four
maximum workers. Deployment acceptance does not mean a worker is ready.

Use `setup` to persist a dependency installation command with the revision.
The CLI provides `--setup`. Enable the matching package network permissions.
Advanced deployments can supply `policy` with explicit network allowlists.
Use `agent.revisions(after=...)` and `agent.revision(number)` to inspect
immutable definitions. `agent.update(expected_revision=..., ...)` admits a
new definition only if the observed current revision still matches.

Read `client.agents.get(agent_id)` and `agent.runs.get(run_id)` to observe
progress and the returned `result`. Use `.list_page(after=...)` or `.iterate()`
on `client.agents` and `agent.runs` to traverse history. `agent.pause()` stops
new dispatch and `agent.resume()` allows it again. `run.cancel()` requests
cancellation. Each mutation accepts an idempotency key. Run submission and
signals require an explicit key so uncertain responses can be retried safely.
When the server reports exhausted retries, `run.retry()` explicitly requests
another attempt within the existing authorization. It does not resolve an
unknown external effect.

The CLI exposes the same operations. `nodus agent deploy NAME`
uploads the current directory. Add `--project PATH` to select another project
or `--source-asset-id ID` to reuse one already uploaded. Use `nodus agent list`,
`detail`, `submit`, `runs`, `signal`, `pause`, `resume` and `cancel` to operate
the deployment. `submit` accepts JSON through `--input` or `--input-file` and
requires `--idempotency-key`.

Connect GitHub in the console and grant access to deploy from a public or private
repository. This uses the existing connection without passing GitHub credentials
to the CLI:

```bash
nodus agent deploy worker \
  --github-repo your-org/private-agent \
  --github-ref main \

```

Omit `--github-ref` to use the repository default branch. The CLI adds the GitHub
network permission and runs `--setup` after checkout when provided. Choose one
of `--github-repo`, `--project` or `--source-asset-id`. Repository subdirectory
selection is not supported. Use a dotted Python module in `--entrypoint` when
your agent is inside a package.

Inputs can include `session="customer-42"` to request ordered processing for
that session. A timezone-aware `deadline` is optional. Use a new event key for
new input. Retrying the same event key with changed input is a conflict.

`AsyncClient.agents` exposes the same operations. Await methods that perform
requests and use `async for` with `.iterate()`.

## Observe lifecycle and cleanup

Read lifecycle observations through the authenticated HTTP endpoint. Set
`NODUS_API_KEY` through your secret manager, `NODUS_BASE_URL` to your HTTPS API
origin without `/v1`, and `NODUS_AGENT_ID` to the definition's `agent.id`.

```bash
curl --fail --silent --show-error --get \
  --header "Authorization: Bearer ${NODUS_API_KEY}" \
  --data-urlencode "limit=50" \
  --data-urlencode "after=${NODUS_AFTER:-}" \
  "${NODUS_BASE_URL}/v1/agents/${NODUS_AGENT_ID}/observations"
```

Leave `NODUS_AFTER` unset for the first page. Set it to the returned `next_after`
and repeat until that value is empty. Each page has its own `observed_at` time
and may reflect newer state. `logical_runs` covers all runs of the definition.
`allocations` includes retained attempts and resources, including warm attempts
not assigned to a run, while `runs` contains the current page. The endpoint returns
404 for definitions that do not exist or belong to another tenant.

Allocation counts can overlap. A completed run can still have pending cleanup.
In `managed-agent-observations-v1`, `productive_active_agents` and each run's
`first_useful_command_at` are always null, with their status fields set to
`measurement_unavailable`. Lifecycle observations do not independently verify
productive work or accepted task outputs. Check those against your workload's
expected results.

## Hosted Claude assistant

Hosted model methods require SDK 0.7.1 or later. Accounts admitted to hosted
model access can deploy the installed text assistant
without uploading a project or providing a model API key. Pass an enabled public
Nodus model identifier from your account's model catalog:

```python
def deploy_assistant(client, model):
    return client.agents.create(
        name="report-assistant",
        assistant_template="nodus:claude-assistant-v1",
        model=model,
        idempotency_key="report-assistant-001",
    )

def submit_report(assistant, task, *, idempotency_key):
    return assistant.submit(
        {"task": task},
        session="report-conversation",
        idempotency_key=idempotency_key,
    )
```

Give each new task a unique idempotency key. Reuse that key only when retrying
the same task.

The assistant returns `text`, `model`, `stop_reason`, `truncated` and reported
`usage` in the run result. `truncated` is true when the model reaches its output
limit. The partial answer is saved and charged once. Submit a follow-up task to
continue when needed. The assistant does not automatically make another call.
It saves recent conversation history in `NODUS_CHECKPOINT_DIR`. Reusing the
session keeps its ordered recent turns. Older complete turns are dropped when
needed to fit the 128 KiB request and 1024 message limits. The current task is
never shortened. An oversized task is rejected before a model call. Different
sessions have separate state.
The installed assistant answers text tasks and does not execute tools or browse.
Its [entrypoint](https://github.com/nodus-compute/Nodus-sdk-python/blob/main/nodus/managed_assistant.py) writes and flushes state before
the journal commits its answer. It does not restore arbitrary process memory.

Compute, model and retained storage charges
are separate and use the available account credits. The controller
supplies the accepted model and output limit. You can set
`model_max_output_tokens` explicitly at deployment, up to 4096 and the enabled
model's limit. The SDK does not supply a missing limit or calculate charges.

Custom managed entrypoints can call `nodus.agent.model(messages, call_id=...,
max_output_tokens=...)` inside a decorated step. Messages contain `role` and text
`content`. Optional `system` supplies text instructions. The helper uses the
deployment's accepted model unless `model` is explicitly supplied. The server
requires that explicit model to match the accepted deployment. Keep each call ID
and request stable across retries. Request inputs are bounded to 128 KiB and
responses to 256 KiB.

A pure step may read this durable model response and write application state.
Reentering that step retrieves the accepted response without making another
provider call. This property does not apply to direct external API calls, which
retain the [external effect contract](../durable-steps.md). Short status polls
allow session renewal while a hosted request runs. Unknown outcomes block for
reconciliation. Keep the existing call identity and inspect the run instead of
submitting the same work under a new ID.

The helper stops scheduling new status polls after 240 seconds. An in-flight
broker request may finish later under its transport timeout and retry limits.
This polling limit is not a total wall-clock execution deadline.

## Wait without keeping a worker busy

Inside `main`, call `nodus.agent.wait_for_event("approval", wait_id="approval:1")`
to durably wait for an event. Deliver it with
`run.signal("approval", {"accepted": True}, idempotency_key="approval-event-1")`.
The wait replays the recorded input when the driver resumes.

Use `nodus.agent.sleep_until(when, wait_id="wake:1")` with a timezone-aware
datetime for a timer. Keep the time and wait ID stable across replay.
`nodus.agent.continue_as_new(next_input, continuation_id="page:2")` commits
the next segment's input and ends the current execution. Do not catch these
execution yields with `except BaseException`.

The managed runtime starts the assigned entrypoint and connects the scoped
durable session automatically. Application code does not call `resume` or
select sandbox IDs. Waits and continuations run outside decorated steps.
Keep code outside steps deterministic. Record model calls and external effects
with the effect contracts in [durable steps](../durable-steps.md).

## Coordinate child agents

Child controls require separately enabled child support for the account and
assigned runtime. Unavailable child actions or disabled child admission raise
`nodus.AgentChildrenUnavailable`. A controller that cannot capture child
checkpoints, or an incompatible session, can instead raise
`nodus.StepOutcomeUnknown`. These methods use the assigned private session.
Do not place an account API key inside the coordinator or its children.

Inside a grouped managed entrypoint, spawn children with stable keys, then wait
for their recorded outcomes. Each child executes the same pinned definition
with its own input and private state:

```python
def main(event):
    if event["role"] == "worker":
        return {"total": sum(event["values"])}

    children = [
        nodus.agent.spawn_child(
            {"role": "worker", "values": values},
            spawn_key=f"part:{index}",
        )
        for index, values in enumerate(event["parts"])
    ]
    outcomes = nodus.agent.await_children(children, wait_id="all-parts")
    return [
        {"child": item.child_run_id, "status": item.status,
         "result": item.result() if item.status == "completed" else None}
        for item in outcomes
    ]
```

Keep each key and its input stable across replay. Changing input, deadline or
permissions under the same spawn key conflicts. Child controls run serially
outside decorated steps.
Children execute independently. Do not use threads or `asyncio.gather` to issue
concurrent controls through the parent session.

An all-child wait accepts at most 100 distinct direct-child references and
returns outcomes in the requested order. For incremental delivery, use
`nodus.agent.next_child_completions(after=cursor, wait_id=page_id, limit=100)`.
The frozen page exposes `outcomes`, `next_after` and `exhausted`. Start with cursor
`"0"`, use a stable wait ID for each page and advance with `next_after`. Apply
external effects inside ordinary durable steps so replaying a page does not
repeat an unrecorded effect. An exhausted page describes the children admitted
when that page was recorded. Spawning more children requires a new wait ID.

`item.result()` fetches and verifies one completed child's JSON result.
Cancelled outcomes retain their status and reason. An expired result raises
`nodus.StepResultExpired`. After observing a child's terminal outcome, use
`child.get_blob(reference)` or `item.get_blob(reference)` for bytes committed in
that direct child's own scope.
Forwarding a grandchild's blob reference does not grant the parent access to it.

`child.cancel(cancel_key="stop:1")` returns a cancellation-request receipt.
With application-state recovery enabled, it captures the parent's state before
a new cancellation request. A saved older receipt replays its original decision.
Keep the cancellation key and target stable on replay.
It does not assert that execution or resource cleanup has finished. Join the
children to observe their terminal outcomes before finishing the parent.
Child references are immutable and provide `.to_dict()` for carrying their
identities in a continuation's JSON input.

Optional `permissions` can narrow `secrets`, `secret_refs`, `connections` and
`egress_allow`. Omitted fields and `None` inherit the parent's permission.
An empty list removes that capability. Removing named secrets does not remove
separately declared connection credentials. The server refuses any broadening
or removal that makes mandatory setup invalid. Children share the existing
group and deployment spending limits.

For messages between admitted tasks in the same group, see
[peer messaging](agent-groups.md#exchange-messages-between-tasks). Peer messages
use their own send and receive keys and are available automatically on qualified
new groups.

## Recovery boundaries

New deployments admitted with qualified application-state recovery use
`checkpoint-v1`. Check `run.recovery_policy` to see the accepted policy. Existing
deployments retain their recovery policy when updated, and each run retains its
accepted code revision.

With `checkpoint-v1`, Nodus saves the files in `NODUS_CHECKPOINT_DIR` before
recording a completed step, entering a durable wait, continuing as new or
finishing the run. The step result and its verified state version commit
together. On a replacement attempt, Nodus restores that exact committed state
before starting the entrypoint. Completed steps replay their recorded results.
Uncommitted changes in the state folder are discarded before retrying.

Your application must serialize its in-memory state into that folder and load
those files on restart. Finish, flush and close every state writer before a step
returns or the driver calls a durable wait or continuation. Join background
writers first. Nodus checks for changed files during capture but does not freeze
background processes or provide an atomic filesystem snapshot. If a save fails,
let the driver exit so recovery can restore committed state before another
attempt. An empty baseline does not claim saved progress, and an empty folder
cannot replace an earlier useful recovery point.

Inspect `run.checkpoint_id` and `run.last_checkpoint_at` for committed state.
`run.checkpoint_status` and `run.checkpoint_error` describe the latest capture.
The `ready` status means files are verified but the associated step or wait has
not committed yet. Empty baselines have no `last_checkpoint_at`. The console
shows the last useful save separately from pending saves and gives a next action
when a capture fails. CLI run output includes the same fields.

The driver uses bounded checkpoint waits when the server supports them and the
session has enough time for renewal. Older servers retain periodic polling.
Both paths require the same durable acknowledgement before a step continues.

Project files, recovery state and downloadable outputs serve different purposes.
Files outside `NODUS_CHECKPOINT_DIR` are not part of these state commits. Rebuild
dependencies on replacement compute. Revisions without `checkpoint-v1` retain
explicit file recovery, with file saves separate from step completion. Neither
file restoration nor replay restores arbitrary process memory.

JSON inputs and results are bounded. Inside the managed driver,
`nodus.agent.put_blob(data)` commits up to 32 MiB of bytes and returns an immutable
reference suitable for a step result. `nodus.agent.get_blob(reference)` checks
the complete content hash and length before returning bytes. Blobs are scoped
to the logical run and remain usable across worker replacement and continuations.
Run and account storage limits still apply. Blob operations commit independently.
Record their verified references in step results when they are needed for replay.

On deployments with object artifact storage enabled, pass `storage="object"`
to `nodus.agent.put_blob(data)`. The call waits for complete verification and
returns a versioned reference. The default remains `storage="database"`, and
`get_blob` accepts both reference formats. Object uploads also support use
inside a serial step. A lost acknowledgement preserves the deterministic
reference, so resume with the same bytes to recover that upload. The 32 MiB
limit and account storage allowance still apply. Unsupported deployments
refuse this option without switching storage formats. Direct-child reads
accept the versioned reference through `child.get_blob(reference)`.

Unknown external effects remain blocked for explicit resolution. File recovery
cannot determine whether a remote API call succeeded when its response was lost.
Inspect `run.steps()` and independently verify the remote effect, then use
`run.resolve()` with the observed step revision, decision, reason and evidence
digest. With `checkpoint-v1`, a `completed` decision also requires an explicit
`checkpoint_id` whose files match the recorded result. Choose the run's current
committed checkpoint or the verified candidate returned for that uncertain step.
Step entries expose the candidate's `checkpoint_id` and `checkpoint_status`.
Do not select an older checkpoint that would discard other completed progress.
Omit `checkpoint_id` for `no_effect` or `cancelled` decisions, which retain the
current committed state.

The CLI provides `nodus agent steps` and `nodus agent resolve --checkpoint-id`
with the same evidence requirements. The console requires an explicit saved-state
selection and confirmation for completed effects. Finite tests do not guarantee
completion of every run.

## Use bounded model and blob operations

The fixed broker calls require separate account admission, group limits and a
qualified model and runtime integration. They are unavailable by default.
`nodus.AgentBrokerUnavailable` reports disabled or unsupported broker transport.
Your existing direct model and tool workflows keep their existing behavior.

Within an assigned grouped entrypoint, outside decorated steps, request one
completion using an available profile ID supplied for your account:

```python
import nodus

def main(event):
    result = nodus.agent.complete_model(
        "Summarize the observed experiment results.",
        profile_id=event["completion_profile"],
        invocation_key="summarize:1",
        max_output_tokens=256,
    )
    return {"summary": result["text"]}
```

Keep the invocation key, prompt, profile and output limit identical on replay.
A repeated key returns its recorded result. Changed input is a conflict.
`finish_reason` is `stop` or `length`. The call accepts no destination URL or
provider credential. It uses the assigned private session, so never put your
account API key in the agent project.

`nodus.agent.read_blob_chunk(reference, invocation_key="read:1", offset=0)`
reads up to 256 KiB from a committed database blob reference with `id`, `sha256`
and `bytes` fields. Add `child_run_id` to read a completed direct child's blob.
Other runs and grandchildren are outside this scope. Offsets are multiples of
256 KiB. Use a distinct stable invocation key for each chunk.

The SDK verifies each result's identity and hash. Both methods poll within a
bounded `timeout`, which defaults to 900 seconds and cannot exceed that value.
Polling continues session renewal. Waiting for broker quota keeps the current
compute allocation held.

`nodus.BrokerRefused` means a definitive refusal and exposes `code` and
`retryable`. A local capacity refusal uses one request attempt and releases its
unused token reservation. Replaying that key returns the same refusal. An
explicit new attempt uses a new key and consumes the remaining allowance.
`nodus.StepOutcomeUnknown` means the outcome could not be established. Do not
catch it to create a new invocation key or resend the model request.
