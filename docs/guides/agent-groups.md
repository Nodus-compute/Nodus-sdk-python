# Managed agent groups

An agent group accepts bounded batches of independent or dependent tasks for one
existing managed agent definition. Each task has its own managed run, journal,
session and writable state. Group admission requires an enabled account and
explicit quotas. Existing deployment execution limits still apply.

The [public API contract](../../openapi/openapi.yaml) describes the request fields.

In the console, open an agent under **Sandboxes** and choose **Parallel task
groups**. Create a group, submit task batches, inspect results and cancel work
from the same view. See the [parallel agents guide](https://nodus-compute.ai/docs/parallel-agents/)
for task inputs and a coordinator example that passes child results to another
child. Console access uses your existing team permissions and execution limits.

## Find existing groups

Set `NODUS_BASE_URL` to your HTTPS API origin without `/v1`. Set `NODUS_AGENT_ID`
to the existing definition. List its groups with your API key:

```sh
curl --fail --silent --show-error --get \
  --header "Authorization: Bearer ${NODUS_API_KEY}" \
  --data-urlencode 'limit=50' \
  --data-urlencode "after=${NODUS_AFTER:-}" \
  "${NODUS_BASE_URL}/v1/agents/${NODUS_AGENT_ID}/groups"
```

The response contains `groups` and `next_after`.
Set `NODUS_AFTER` to `next_after` for the next page. Groups are ordered newest
first. Each request reads a separate database snapshot. New groups appear when
you refresh the first page. A cursor from another agent is rejected.

Discovery remains available when new group admission is disabled. Existing
groups still expose their results, spending, cleanup and cancellation control.

## Create a group

Authenticate with your existing API key. Set `NODUS_BASE_URL` to your HTTPS API
origin without `/v1` and `NODUS_AGENT_ID` to an existing definition that accepts
your task inputs. Keep submission keys stable when retrying an uncertain response.

```sh
curl --fail --silent --show-error \
  --header "Authorization: Bearer ${NODUS_API_KEY}" \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: project-analysis-group' \
  --data "{\"name\":\"project-analysis\",\"agent_id\":\"${NODUS_AGENT_ID}\",\"budget_usd\":5,\"max_active\":1,\"max_held\":2}" \
  "${NODUS_BASE_URL}/v1/agent-groups"
```

Use the returned `id` as `NODUS_GROUP_ID`. The group pins the definition's current
revision unless you explicitly select an existing revision. Updating the
definition afterward does not change accepted group work.

The budget is a sublimit of the deployment's existing budget. Creating another
group does not create additional spending authority. `max_active` and `max_held`
cannot exceed the deployment's worker limit.

## Submit tasks

Submit at most 100 tasks per request, with a maximum complete body of 1 MiB.
Each input is valid JSON bounded to 256 KiB. Your agent entrypoint determines
what the input means.

```sh
curl --fail --silent --show-error \
  --header "Authorization: Bearer ${NODUS_API_KEY}" \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: project-analysis-page-1' \
  --data '{"tasks":[{"task_key":"inspect","input":{"action":"inspect"}},{"task_key":"verify","input":{"action":"verify"},"depends_on":["inspect"]}]}' \
  "${NODUS_BASE_URL}/v1/agent-groups/${NODUS_GROUP_ID}/runs"
```

Dependencies refer to task keys in the same group. They can reference existing
tasks or other tasks in the current batch, regardless of submission order.
Missing references, duplicate task keys within a batch and cycles reject the
entire batch. Existing tasks cannot gain new dependencies.

The dependent task waits until every prerequisite has committed success. A
cancelled or blocked prerequisite does not satisfy the dependency. Dependency
ordering does not copy results into another task's input or share writable
files. Retrieve each run's result through the existing managed-run API.

The batch receipt returns task keys and run IDs in request order. Retrying the
same batch key and content returns that receipt. Reusing a task key with the
same immutable content in another batch returns the original run. Changed
input, dependencies or deadline conflicts. Identity records survive payload
expiry, so a retry cannot silently start the task again.

## Exchange messages between tasks

New groups on a qualified account and runtime support peer messages
automatically. Existing groups retain their accepted capabilities. The group's
`peer_messaging` response field reports availability. Messages address an
admitted `task_key` in that same group. A task does not need to be a direct child
of the sender. Group membership and messaging do not share writable files or
grant access to another task's blobs.

Use the assigned managed entrypoint to send bounded JSON and receive a recorded
batch. For this example, submit tasks named `inspect` and `verify`. Give `inspect`
input such as `{"role": "inspect", "values": [1, 2, 3]}` and give `verify` input
`{"role": "verify"}`:

```python
import nodus

def main(event):
    if event["role"] == "inspect":
        sent = nodus.agent.send_message(
            to_task_key="verify",
            payload={"total": sum(event["values"])},
            message_key="inspection:1",
        )
        return sent.to_dict()

    messages = nodus.agent.receive_messages(wait_id="inspection:1", limit=1)
    return {"total": messages[0].payload()["total"]}
```

After deploying that entrypoint and creating its group, submit both tasks in
one batch. They exchange the total directly without a coordinator:

```sh
curl --fail --silent --show-error \
  --header "Authorization: Bearer ${NODUS_API_KEY}" \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: inspection-pair-1' \
  --data '{"tasks":[{"task_key":"inspect","input":{"role":"inspect","values":[1,2,3]}},{"task_key":"verify","input":{"role":"verify"}}]}' \
  "${NODUS_BASE_URL}/v1/agent-groups/${NODUS_GROUP_ID}/runs"
```

Keep the message key, destination and payload stable across replay. A send
returns an immutable `MessageReceipt` once the message is durably recorded.
It does not assert that the recipient has processed the payload. Retrying the
same key returns the original receipt. Changing its destination or payload
conflicts.

A new receive records a wait and yields execution. Nodus resumes the task when
its batch is recorded. Replaying the same wait ID returns the original messages
in the same order. Use a new stable wait ID for each later batch and keep its
limit unchanged. The result is a tuple of immutable `PeerMessage` objects.
Each `.payload()` call returns a fresh JSON value, so modifying it does not
alter the recorded message. Record external effects inside durable steps.

Each payload is bounded to 16 KiB of encoded JSON. A receive accepts a limit
from 1 to 32 and defaults to 10. Messages run serially outside decorated steps.
With application-state recovery enabled, Nodus captures the state folder before
recording a new send or receive. Missing group or runtime support raises
`nodus.AgentMessagesUnavailable` before a message operation starts. These
methods use the assigned session and do not require an account key in the task.

## Observe progress and limits

```sh
curl --fail --silent --show-error \
  --header "Authorization: Bearer ${NODUS_API_KEY}" \
  "${NODUS_BASE_URL}/v1/agent-groups/${NODUS_GROUP_ID}"
curl --fail --silent --show-error --get \
  --header "Authorization: Bearer ${NODUS_API_KEY}" \
  --data-urlencode 'limit=50' \
  --data-urlencode "after=${NODUS_AFTER:-}" \
  "${NODUS_BASE_URL}/v1/agent-groups/${NODUS_GROUP_ID}/runs"
```

For task listing, set `NODUS_AFTER` to `next_after` and repeat until it is empty.
Each page is a separate database snapshot. A cursor from another group is
rejected. Missing and foreign-owned groups return HTTP 404.

`status` describes group admission. `work_status` summarizes the current tasks.
`logical_runs` gives individual state counts, and `allocations` retains cleanup
liability independently of task success. A completed result does not imply that
its compute and accounting have settled.

On servers that expose group progress details, `blocked_reasons` gives sorted
public categories with logical run counts. These distinguish dependencies,
child waits, session serialization, execution and workspace capacity, budget
limits and unknown external outcomes. Unknown reasons use `attention_required`.
Cancellation reports nonterminal members as `cancelling` until their individual
cancellation completes.

`cleanup_status` is `unresolved` while resource, accounting or message publication
evidence remains unresolved, `pending` while allocation or inbox cleanup remains, and
`complete` after those obligations settle. Completed work can still have
unresolved cleanup and a nonzero `reserved_usd`. These fields describe the
current group response and do not measure productive model activity.

Messaging groups expose `peer_usage` with lifetime message and byte limits,
pending, received and undelivered counts, and inbox cleanup status. Received
means committed to the recipient journal. It does not prove the application
acted on the message. Group message limits are separate from compute budgets.

| Control | Consumed by |
| --- | --- |
| `max_pending` | All nonterminal tasks, including active, waiting and blocked work |
| `max_active` | Attempts authorized for execution in admitting, starting or running state, including retained waiting workers |
| `max_held` | Every unsettled allocation, including draining and uncertain attempts |
| `max_retained_runs` | Runs whose payloads have not expired, including completed and cancelled tasks |
| Existing workspace limits | Held or saved workspaces and their configured byte limits |

`active_authorized_attempts` can be compared with `max_active`.
`allocations.cleanup_pending_attempts` can be compared with `max_held`.
These are control-plane authorization and liability counts. They do not measure
productive model activity or prove that each runtime is healthy.

Pending defaults to 1,000 per group, bounded by account entitlement up to 10,000.
Raising a pending limit does not raise execution or storage limits. A retained
payload slot is released by the existing retention policy, not by task completion.
A run waiting for workspace capacity remains accepted and reports
`waiting_for_workspace_capacity` without discarding earlier saved state.

Other waiting reasons distinguish dependencies, group execution capacity,
allocation cleanup and budget exhaustion. `reserved_usd` includes full unsettled
attempt budgets, even when a closed marker lacks cleanup evidence. `cost_usd`
contains settled costs. Both remain within the existing deployment account.

## Cancel and recover a receipt

```sh
curl --fail --silent --show-error \
  --request POST \
  --header "Authorization: Bearer ${NODUS_API_KEY}" \
  --header 'Idempotency-Key: project-analysis-cancel' \
  "${NODUS_BASE_URL}/v1/agent-groups/${NODUS_GROUP_ID}/cancel"
```

Cancellation commits a durable fence against new group work. Compatible
executors then cancel nonterminal members and reconcile their resources.
Completed results remain available under the existing retention policy.
HTTP 202 confirms the fence, not completed physical cleanup.

Reads, cancellation and replay of accepted batch receipts remain available when
new group admission is disabled. Monitor allocation liability until cleanup is
settled. Do not use a new submission key to work around an uncertain response.

## Bound model and tool usage

On deployments with broker admission enabled for your account, group creation
can include immutable `broker_limits`. Omit the field to retain the existing
direct model and tool workflow. The broker requires separate runtime and model
qualification and is unavailable by default.

The limits contain integer `model_requests`, `input_tokens`, `output_tokens`,
`tool_requests`, `read_bytes`, `max_concurrent`, `max_retained_invocations` and
`max_retained_bytes`. They must fit your account's configured ceilings. Request,
token, read-byte and invocation-history limits cover the group's lifetime.
Increasing account limits does not reset usage. A model admission reserves its
profile's full context bound until an authoritative result settles actual usage.

Groups with these limits also expose `broker_usage`. It reports model and tool
request usage, token and read-byte reservations, dispatched active work, unknown
outcomes and fixed quota wait reasons. These are integer units, separate from
`cost_usd` and `reserved_usd`. Unknown effects retain their full reservation.
Payload expiry does not erase invocation history or grant another attempt.

See [bounded model and blob operations](managed-agents.md#use-bounded-model-and-blob-operations)
for the assigned-driver calls and replay rules.
