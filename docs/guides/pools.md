# Use your own GPU hosts

Sign in with `nodus login` or configure `NODUS_API_KEY`. Pools register
customer-owned GPU hosts for free, read-only measurement on deployments where
Compute is enabled. Your existing scheduler continues running your workloads.
Predict adds forecasts and advisory recommendations with pool-specific terms. Route
requires separate execution enrollment and explicit price consent.

Create a pool with `nodus pools create Research`. The command prints its pool
ID. Run `nodus pools token POOL_ID`, replacing `POOL_ID` with that returned ID,
to issue an observe enrollment token. This command prints a secret. Keep the
token out of shared logs and source control. Each token can enroll one host
and expires after 24 hours.

Use the Compute enrollment panel's installation instructions on your Linux
host from your infrastructure provider or data center. After enrollment, `nodus pools hosts POOL_ID` shows each host's ID, name,
health, agent mode, and device count. Hosts become lost when their heartbeat
has been absent for three minutes. Use the console to inspect their devices.

## Python methods

`client.pools` and `AsyncClient.pools` expose the same methods. Await methods
on the asynchronous client. IDs always come from the server.

| Method | Result |
|---|---|
| `create(name)` | A `Pool` configured for read-only measurement |
| `list()` | All pools owned by the authenticated team |
| `get(pool_id)` | A `Pool` with its current configuration |
| `update(pool_id, name=..., owned_cost_micros_per_hour=...)` | Updated `Pool`. Supply at least one setting |
| `enrollment_token(pool_id, mode="observe", host_id=None)` | An `EnrollmentToken` with `id`, `token`, `mode`, and `expires_at` |
| `utilization(pool_id, from_=..., to=..., bucket=...)` | `PoolUtilization` with a summary, host summaries, and time buckets |
| `set_route(pool_id, enabled, accepted_rate_version=..., accepted_rate_micros=...)` | Updated `Pool` with explicit price consent when enabling |
| `update_route_settings(pool_id, wait_policy=..., wait_alpha=...)` | Updated future placement settings |
| `hosts(pool_id)` | `PoolHost` objects with health, inventory, and `HostDevice` objects |
| `drain_host(pool_id, host_id)` | The host marked draining, without stopping customer processes |
| `remove_host(pool_id, host_id)` | Revokes the host credential and removes the host, preserving historical measurements |

`Pool.owned_cost_micros_per_hour` is your supplied hardware cost in USD micros
per hour. It does not create a charge. Missing server cost fields remain
`None`. Full pool and host metadata is available through their `raw` fields.
The token value is accessible through `EnrollmentToken.token` and is excluded
from its printed representation.

Pool requests do not automatically retry or follow redirects. After an
uncertain create response, inspect the pool list before creating another.
An uncertain token response may have issued a token whose secret was lost.
Issue a new token only when you intend to create another credential. Host
removal does not uninstall the agent or stop programs on the machine.


## Read the utilization ledger

Run `nodus pools utilization POOL_ID` to see measured allocated, busy, and
busy-of-allocated percentages. Add `--json` for host buckets and observed
foreign device IDs. Queued and fragmentation durations are workload-seconds. Other durations
are device-seconds. Unknown readings are `None` in Python and `null` in JSON.
A measured zero is distinct from an unknown reading.

The default window is the last seven days of complete UTC hours. Use `--from`
and `--to` with RFC 3339 timestamps to choose a window of at most 31 days.
Both boundaries must align to a UTC hour. The end is exclusive. Use
`--bucket hour` or `--bucket day` to choose the grouping. The corresponding
Python keywords are `from_`, `to`, and `bucket`. Omitting them leaves defaults
to the server.

`PoolUtilization.summary` contains `UtilizationMetrics`. Each entry in
`hosts` contains a host summary and `buckets`. Every bucket carries `start`,
`end`, `metrics`, and `foreign_device_ids`. The response's `from_`, `to`, and
`bucket` identify the measured window. The unchanged JSON is in `raw`.

`data_status` is `complete`, `partial`, or `no_data`. Partial coverage hides
derived totals and percentages. Foreign device IDs still identify allocation
observed during a partial bucket. Summaries cover retained ready device time. A complete summary does not claim
continuous coverage of every host hour. Empty buckets report `no_data`.
Missing history is never counted as idle.
Fragmentation, queued, and burst metrics require retained Route evidence and
appear in the pool summary. Host rows keep these fields unknown. Gaps,
ambiguous queue retries, and unsettled execution keep affected values unknown.
`summary.burst_cost_micros` contains exact settled customer cost in USD micros
when each relevant burst execution falls wholly inside the requested window.
A burst crossing a window boundary leaves cost unknown instead of prorating it.


## Forecasts and advisory recommendations

Observe measurements remain free. Cloud-connected BYOCompute pools also have
free Predict and Route under `byoc-free-v1`. Their subscription reports
`monthly_micros: 0` and `paid_current_period: false`, even when active. This
does not change a paid subscription covering other pools in the account.
For a cloud-connected pool, supply `byoc-free-v1` and zero to
`set_predict` or `set_route` when enabling. The equivalent CLI flags are
`--accept-rate-version byoc-free-v1` with `--accept-monthly-micros 0` for
Predict or `--accept-rate-micros 0` for Route. Cloud provider charges and
separately authorized market capacity still apply.

For other pools, Predict costs **$99 per account per UTC
calendar month**, with no additional pool or device fee. The first activation
charges the full current month without proration. Enabling another pool in an
already-active period adds no charge. An active period can reflect an accepted
postpaid charge and does not mean an invoice has been paid. Disable Predict on every pool to stop
future renewal. Disabling does not refund the current period.

Read `client.pools.forecast(pool_id, horizon=7)` or use
`nodus pools forecast POOL_ID --horizon 7 --json` to inspect the server's
current subscription rate and cached forecast. The supported horizons are
7 and 30 days. Omit `horizon` to use the server default of 30 days. Existing
cached forecasts and recommendations remain readable without a refresh
charge when Predict is disabled or paused. Only administrators can change
the subscription or record recommendation outcomes.

A forecast response contains `subscription`, `refresh_status`, and `snapshot`.
A missing snapshot remains `None`. The snapshot identifies its model, creation
time, history coverage, hourly p10, p50, and p90 device-hour bands, owned device
count, and any advisory market price. Four complete weeks of measured history
are required. The weekly seasonal baseline is identified explicitly. Missing
history does not become zero demand, and the SDK does not invent a learned
model or a market price.

When Route provides known queued demand, `snapshot.forecast.queue` records its
known device-hours and the count of jobs with unknown runtimes. Each point's
`queue_device_hours` identifies the contribution added to its band. This assumes
known queued jobs start next hour and is not a placement promise. The evidence
separates demand included in the selected horizon from demand beyond it.
Older snapshots can omit this evidence.

The `calibration` object evaluates predictions issued before their target
hours against subsequently observed outcomes. Unknown coverage and pinball
losses remain `None`. Hourly coverage is the observed fraction within the
issued p10 to p90 band. Daily coverage counts fully evaluated UTC days whose
every hour fell within the band. A nominal hourly band does not guarantee
that a whole day falls inside it.

Enable Predict only after reviewing the returned price. Python callers use
`set_predict(pool_id, True, accepted_rate_version=...,
accepted_monthly_micros=...)`, supplying the exact rate version and integer
USD micros they accept. The SDK has no default consent or amount. For a paid
Predict subscription, the CLI is:

```sh
nodus pools predict POOL_ID on \
  --accept-rate-version predict-account-monthly-v1 \
  --accept-monthly-micros 99000000
```

Disable with `client.pools.set_predict(pool_id, False)` or
`nodus pools predict POOL_ID off`. After an uncertain subscription response,
refresh the forecast response before deciding whether to try again.

Read advice with `client.pools.recommendations(pool_id)` or
`nodus pools recommendations POOL_ID --json`. Each recommendation includes
its expiration, advisory evidence, state, and any customer-reported outcome.
Rightsizing evidence exposes released device count, owned hourly cost,
expected burst device-hours, advisory burst price, and estimated savings.
These are scenario estimates, not measured savings or a guaranteed workload
completion price. Idle-reclaim advice identifies a host and device, sampled
low-utilization allocation, its recent trigger interval, and whether foreign
allocation was observed. Its estimated saving is unavailable. Nodus does not
identify or stop a customer process through this advice. Drain-window advice
identifies a host and a prospective UTC interval with at least three
consecutive hours whose forecast p90 demand is below one device. Its evidence
requires four complete weeks of host history. It is not an availability
guarantee, does not estimate a saving, and does not drain the host.
Placement-consolidation advice requires 336 complete hours of Route
observations and measured waits for requests needing multiple devices.
It reports workload-seconds above the stated policy threshold. Its packing
policy fills hosts first, excludes moving foreign jobs, and does not claim
an estimated saving. Missing Route observations produce no such advice.

Advice is paginated, with up to 100 records per response. Follow
`next_cursor` with the same pool and optional state filter:

```python
page = client.pools.recommendations(pool_id, state="expired", limit=25)
while page.next_cursor is not None:
    page = client.pools.recommendations(
        pool_id, state="expired", limit=25, cursor=page.next_cursor
    )
```

Omit `state` for all records, or use `open`, `done`, or `expired`.
The CLI accepts the same `--state`, `--limit`, and `--cursor` options and
prints the next cursor when older records remain.

After making a change yourself, record it with
`client.pools.recommendation_done(pool_id, recommendation_id, outcome)` or:

```sh
nodus pools mark-done POOL_ID RECOMMENDATION_ID \
  --outcome "Reduced capacity in our scheduler"
```

The returned `RecommendationOutcome` is explicitly customer-reported. Leave
`reported_saving_micros` unset when your saving is unknown. If you have an
independently assessed amount, pass nonnegative integer USD micros in Python
or add `--reported-saving-micros` in the CLI. Neither the SDK nor the console
copies an estimated saving into your reported outcome. Recording an outcome
does not execute a host action.

All these Python methods are also available on `AsyncClient.pools` and must
be awaited. Forecasts return `PoolForecast`, recommendations return
`PoolRecommendations`, and their unchanged response JSON is available in
`raw`. Subscription changes return `Pool`.


## Enable Route with explicit consent

Cloud-connected pools use free Route as described above. For other pools,
an account admin can enable Route at **$0.02 per active customer device-hour**,
including optimization and apply. Your private hosts have no supplier rental
charge. Market capacity has separate compute charges. Enabling Route does not
create a Predict subscription or change an observe host's execution permission.

First choose an existing host from `client.pools.hosts(pool_id)`. Use the
Compute Hosts panel's **Enable execution** action to obtain a fresh token and
pinned installation command. Run that command in a root Bash shell on the
same Linux host. The installation rotates the host credential and installs
the execution service. You can prepare execution hosts before enabling Route.
The equivalent token request is explicit:

```python
token = client.pools.enrollment_token(
    pool_id,
    mode="execute",
    host_id=host_id,
)
```

Tokens are single-use secrets. Use `token.token` only when providing it to
the installation prompt. Do not log it or put it in command history. A token
response does not mean the host has installed execution support.

After reviewing the rate, enable Route:

```python
pool = client.pools.set_route(
    pool_id,
    True,
    accepted_rate_version="route-platform-v1",
    accepted_rate_micros=20000,
)
```

The same consent through the CLI is:

```bash
nodus pools route POOL_ID on \
  --accept-rate-version route-platform-v1 \
  --accept-rate-micros 20000
```

Disabling with `client.pools.set_route(pool_id, False)` or
`nodus pools route POOL_ID off` stops new admission. Existing work, accepted
terms, and exact cleanup remain tracked. Send Route changes separately from
Predict, pool name, and owned hardware cost updates.

`update_route_settings` accepts the following optional fields. Supply at least
one. These fields may also accompany `set_route` in one request.

| Field | Values |
|---|---|
| `wait_policy` | `never` keeps waiting for private capacity and never uses market fallback. `after_wait` allows fallback after waiting. `cheaper` may allow early fallback with active Predict entitlement and usable forecast evidence |
| `wait_alpha` | Finite number at least zero. New pools default to 0.1 |
| `waiting_budget_pct` | Number from 0 through 100 |
| `burst_approval` | `auto`, `above_threshold`, or `always` |
| `burst_threshold_micros` | Nonnegative USD micros |
| `burst_timeout_behaviour` | `keep_waiting` or `cancel` |

The `cheaper` policy compares a current market quote's expected cost to completion
with the pool's forecast opportunity cost. It requires enabled Predict entitlement,
a positive owned hardware cost, a trusted runtime estimate of at most 30 days,
and a ready forecast no more than two hours old. Missing or stale evidence does
not authorize early fallback. Burst approval and workload spending controls
still apply.

```python
client.pools.update_route_settings(
    pool_id,
    wait_policy="after_wait",
    wait_alpha=0.1,
    burst_approval="always",
)
```

## Choose private or market placement

Omit `placement` to prefer eligible private capacity. Set one pool explicitly,
or use `prefer="any"` to skip private pools. Do not set both fields.

```python
workload = client.run(
    command=["python", "train.py"],
    gpu_count=1,
    placement=nodus.Placement(pool=pool_id),
)
```

Use `placement=nodus.Placement(prefer="any")` for market capacity. Your image,
GPU requirements, spending controls, and output selection still apply.
Unavailable, disabled, or inaccessible explicit pools are rejected. Accepted
submission does not mean execution has started. Observe progress and retrieve
results as for other workloads. The same arguments work with `AsyncClient`.

## Burst approval inbox

A burst proposal requests market fallback for one submitted workload stage and
execution generation. It requires Route, but not Predict. An account admin can
approve or reject the immutable proposed amount. Approval records intent and
does not itself rent capacity. Nodus rechecks the current quote, Route authority,
spending controls, and original expiry before admitting new work. The quoted
expected cost is not an absolute billing cap. Workload spending controls remain
separate.

```python
page = client.pools.proposals(pool_id, state="pending", limit=25)
for proposal in page.proposals:
    print(proposal.id, proposal.expected_cost_micros, proposal.expires_at)
```

Follow `page.next_cursor` with the same pool and state filter to read older
proposals. Amounts use USD micros. Review the server-reported amount before
calling `client.pools.approve_proposal(pool_id, proposal_id)` or
`client.pools.reject_proposal(pool_id, proposal_id)`.

`approved` means recorded intent. `applying` means the same execution generation
claimed that approval. Only `applied` means a matching winning execution was
observed. `expired`, `rejected`, and `no_op` retain their reason and do not silently
renew approval. An execution dispatched before expiry may be observed afterward.
A timeout does not prove a decision failed. Refresh the inbox before retrying.

The same methods are available on `AsyncClient.pools` with `await`.
