# Durable steps

Durable steps require a deployment with this capability enabled. Register one
run with immutable JSON input and the sandbox's pinned image manifest digest.
Then run the Python driver inside that sandbox. Registration alone starts no
compute.

```python
run = sandbox.agent_runs.create(
    run_id="invoice:42",
    name="main",
    version="1",
    image_digest="sha256:" + "a" * 64,
    input={"invoice_id": 42},
)
```

Use the actual pinned image digest in production. The account API key stays in
the caller that creates the run. The guest driver uses its private local agent
socket and receives only a capability scoped to its current run session.

```python
import nodus

@nodus.step(name="invoice.send", version="1", effect="external")
def send_invoice(invoice_id):
    receipt = send_to_invoice_service(invoice_id)
    return {"receipt": receipt}

def main(event):
    return send_invoice(
        event["invoice_id"],
        _step_id=f"invoice:{event['invoice_id']}:send",
    )

result = nodus.agent.resume(main, run_id="invoice:42", version="1")
```

The application supplies `send_to_invoice_service`. Completed results are
recorded before the step returns and replayed without invoking that function.
Use explicit stable business IDs. Changing a step name, version, input or effect
under the same ID is a conflict. Keep code outside steps deterministic.

The default `external` effect never retries an unknown outcome automatically.
If the remote service commits but its response is lost, inspect that service
before resolving the unknown step. Resolve using its current revision, a reason
and the SHA-256 digest of independently retained evidence. `completed` requires
the verified result, `no_effect` permits one more bounded attempt, and `cancelled`
stops the run. Resolution preserves the original unknown attempt.

Declare `pure` only for operations safe to repeat. Declare `idempotent` only when
the downstream service enforces the key returned by
`nodus.step_context().idempotency_key`, and provide its actual
`dedupe_seconds` guarantee. Retries keep that key and stop after three attempts.
Idempotent retries remain refused after the original deduplication window,
including after a `no_effect` resolution.

Steps and the driver are synchronous and serial. Nested steps, parallel steps,
coroutines, generators and non-JSON values are unsupported. Inputs and results
are each limited to 256 KiB of UTF-8 JSON. A run supports 10,000 steps and 64 MiB
of encrypted journal data including reserved result capacity and capabilities.
A sandbox supports 100 active or blocked runs.

Read `sandbox.agent_runs.get(run_id)` and `.steps(run_id)` for progress.
The async account client provides the same registration and observation calls.
Terminal payloads expire after 30 days. Tombstones remain and expired results
never authorize repeating a completed effect. Snapshot recovery restores files,
not arbitrary process memory. Restart the driver from its entry point so it can
replay the journal.

Use `sandbox.agent_runs.delete(run_id)` to erase payloads and permanently fence
the run. Its expired tombstone prevents that identity from being executed again.
Deletion requests cancellation of an event-owned driver. It does not terminate
the sandbox itself.

For account-authenticated events, call `sandbox.agent_events.submit` with
`source`, `event_id`, `run_id`, `name`, `version`, `image_digest`, `input` and an
explicit `command` argv for the driver. The driver must use that same run ID.
The event receipt, run and command are stored before the API acknowledges the
submission.
The original lifetime is not extended.

Repeat the same source and event ID to retrieve the same logical run. A changed
payload or command is rejected. Read `sandbox.agent_events.get(event_id,
source=source)` to distinguish queued, running, blocked, handled and expired
states. Only durable run completion means handled. Unknown external effects stay
blocked. Lost event drivers have at most three attempts against the same run.
Queued events expire after seven days. Each sandbox admits at most 1,000 queued
events and 64 MiB of retained event payload. The 100 active-run limit also applies.

This API requires account authentication. A queue consumer must acknowledge its
source only after receiving the committed event receipt. It must preserve the
same source and event ID across redelivery. Public inbound webhooks and external
queue adapters are not included in this SDK API.
