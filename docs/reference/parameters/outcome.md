# Completion deadline

| Argument | Type | Omitted | Workload file | HTTP field |
|---|---|---|---|---|
| `finish_by` | RFC3339 string or `datetime` | No completion deadline | `finish_by` | `outcome.complete_by` |

Payment and available credits apply. Legacy `budget` and `outcome.max_cost_usd`
fields are accepted for compatibility and do not impose spending limits.

Inside a `with nodus.Client() as client:` block:

```python
from datetime import datetime, timedelta, timezone

workload = client.run(
    image="pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime",
    command=["python", "-c", "print('deadline example')"],
    finish_by=datetime.now(timezone.utc) + timedelta(hours=2),
)
```

Use timezone-aware datetimes. A naive datetime is interpreted in the submitting
machine's local timezone before conversion to UTC. A string passes through for
server validation. Use a future timestamp including an offset or `Z`.

`finish_by` expresses a completion deadline. `wait(timeout_seconds=...)` only
bounds local observation and does not cancel the workload.
See [costs](../../concepts/costs.md) for observed spend and settlement.
