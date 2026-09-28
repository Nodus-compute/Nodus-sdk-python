# Concurrent experiments

`AsyncClient` has the same operations as `Client`. Await ordinary methods. Use
`async for` with iterator/event-stream methods. Bound concurrent submission and
polling to avoid an unbounded number of API calls.

Save the [complete Python example](../../examples/async_sweep.py) as `async_sweep.py`
in your current directory. Example scripts are not installed by pip. Then run:

```bash
python async_sweep.py --run-id experiment-001
```

This submits three self-contained workloads and permits two active tasks at a
time. Each run records its own usage against account funding. The semaphore limits
client scheduling and does not cap spending.

Reuse the same `--run-id` only to retry the identical experiment. A new experiment
needs a new ID. Changing payloads under old keys causes an idempotency conflict.
Ctrl+C while waiting requests cancellation for each submitted workload with a
known ID. A failed cancellation still requires `nodus cancel WORKLOAD_ID`.
