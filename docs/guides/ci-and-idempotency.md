# CI and safe retries

Provide `NODUS_API_KEY` through your CI secret manager.
Use a stable ID for one logical submission, preserved across job retries:

Save the [complete Python example](../../examples/ci_submit.py) as `ci_submit.py`
in your current directory. Example scripts are not installed by pip. Then run:

```bash
python ci_submit.py --submission-id YOUR_PIPELINE_RUN_ID
```

Each `run()` gets a fresh UUID unless `idempotency_key` is set. That UUID protects
only retries inside that call. Application retries and restarted CI jobs need
the same explicit key and exactly the same brief to avoid duplicate paid work.
A different payload under the same key raises `IdempotencyConflictError`.

An explicit `idempotency_key` must be a nonempty string containing printable
ASCII characters without spaces or line breaks, such as `"training-run-123"`.
The same character rules apply to `client.cancel(..., idempotency_key=...)`.
Cancellation generates a fresh key for each call when you omit it.

`run()` returns an accepted handle. Log its ID before waiting. `wait()` returns
on all terminal states. A CI job must inspect `done.succeeded`, as the example
does, to fail on a failed or cancelled workload.

A submission timeout or connection failure can leave the outcome unknown.
Retry with the original key. For automatically generated keys, transport errors
expose the submission key in `error.payload`. Retain it if recovering manually.
Do not assume a network exception means the server created nothing.

Cancellation is a separate idempotent request: `client.cancel(workload_id)`.
Choose explicitly whether a CI timeout should cancel remote work or permit it
to finish. See [reliability](../concepts/reliability.md).
