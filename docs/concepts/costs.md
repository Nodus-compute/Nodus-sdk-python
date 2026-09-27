# Observed cost and billing

Nodus records usage as your work runs. Ordinary workloads, sandboxes and
benchmarks use account funding without per-run spending caps. Payment
requirements and available team credits apply. Explicit managed fleet budgets
and accepted per-attempt allocations remain enforced.

Nodus checks that available credits cover the selected capacity's initial
billing window. It renews funding as work continues. Work can stop when the
account cannot fund additional usage. An accepted request does not guarantee
completion or a final price.

| Value | Meaning |
|---|---|
| Available credits | Team credit balance after charges and pending reservations |
| `workload.cost_now_usd` | Current estimate while pending, then the fixed lifetime compute charge |
| `workload.spend_usd` | Settled workload charges |
| `workload.meter.as_of` | Timestamp of the live meter |
| `workload.meter.charge_state` | `estimated` while metered costs are pending, then `final` |
| `workload.meter.final_charge_usd` | Immutable aggregate lifetime compute charge, absent while pending |
| `ledger.charged_usd` | Settled customer charge |
| `ledger.settlement.balance_usd` | Compatibility field fixed at zero |

The meter separates compute, platform fees, storage, model and subscription
charges. Use its aggregate totals to follow spending. Stopping compute and
final settlement can take time. Pending usage retains its credit reservation
until accounting is complete.

Run completion and cost finalization are separate. A completed metered run can
remain `estimated` while required cost records are unavailable. Show
**Finalizing cost** during this period. Once `charge_state` is `final`, use
`final_charge_usd` for its fixed lifetime compute amount. The ordinary workload meter
totals cover the current billing month. Later accounting updates do not change
the final compute charge.

Retaining files can continue to incur usage charges after compute stops. These
charges are excluded from `final_charge_usd`. Period and account totals, the
ledger and usage invoices can include retained-file usage without changing
the fixed compute charge.

Sandbox handles expose `charge_state` and `final_charge_usd` directly.
`sandbox.cost_usd` remains the recorded charge and can be zero while finalizing.
Older billing contracts and servers can omit the additive finality fields.

```python
workload = client.get(workload_id)
print(workload.cost_now_usd)
ledger = workload.ledger()
print(ledger.charged_usd, ledger.settlement.status)
```

Route estimates are planning information. Use the live meter while running and
the ledger after settlement. Legacy `budget`, `budget_usd` and
`outcome.max_cost_usd` fields on ordinary workload and sandbox submissions are
accepted for compatibility and do not set a spending limit. Explicit managed
fleet and attempt allocations still apply. Cancel work or stop unused compute
when you are finished.
