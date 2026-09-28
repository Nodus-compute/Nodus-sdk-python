# Scheduled micro-batches

Keep a queue in your scheduler. Every scheduled invocation submits at most one
ordinary GPU workload containing up to 64 requests. The workload drains that
immutable batch and writes a downloadable result. Nodus account webhooks signal
completion. This example adds no Nodus resource type or queue API.

Configure Nodus authentication on the scheduler first. Keep `queue.py`,
`worker.py` and `webhook.py` together. Store the SQLite database on durable local
storage with permissions limited to the scheduler account. Run one scheduler
process per database. Request payloads are included in workload commands and
must not contain credentials or other secrets.

Enqueue a request with an application-generated stable ID:

```sh
python queue.py --db queue.sqlite enqueue request-001 0.5
```

Invoke this command from your existing scheduler. Set a reviewed per-batch budget
and a PyTorch GPU image. This command submits paid work:

```sh
python queue.py --db queue.sqlite submit \
  --image "$PYTORCH_IMAGE"
```

The queue persists the exact batch, image and idempotency key before
submission. A retry after an unknown HTTP outcome reuses those values. Never
delete the database to clear a pending submission. New requests wait for the
next batch. Failed workloads stay associated with their batch for investigation
and are not automatically resubmitted with a new paid key.

Configure the account webhook with `client.set_webhook(url, secret=secret)` from
your application using its existing secret store. Mount `webhook.handle_event`
behind your HTTPS handler and pass the raw body plus `X-Nodus-Timestamp` and
`X-Nodus-Signature`. Return success only after the database update commits.
The handler validates the timestamp and HMAC before changing queue state.
Webhooks can arrive repeatedly. Reconcile pending batches with `client.get`
after a webhook outage.

For a completed workload, use its recorded ID to fetch the declared output:

```python
with nodus.Client() as client:
    workload = client.get(workload_id)
    workload.download_output("results", "results.json")
```

The included worker applies sigmoid to a GPU tensor as a small inference
example. Replace `process` with your own model, retaining request IDs in the
output. This example does not provide exactly-once external side effects.
Use application idempotency keys when model tools change external state.
