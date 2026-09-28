# Errors and troubleshooting

API and transport errors inherit `nodus.NodusError`. Inspect `.status_code`,
`.code`, `.payload`, and `.request_id`. Include the request ID when reporting an
issue. Python argument mistakes (`TypeError` / `ValueError`) are separate.

| Error | Action |
|---|---|
| `ConfigurationError` | Run `nodus login` or fix the reported configuration problem |
| `AuthenticationError` | Check deployment/key pairing, expiry, or revocation |
| `SignatureError` | Check signing secret and clock for signed requests |
| `ValidationError` | Correct the rejected request field |
| `IdempotencyConflictError` | Reuse the original payload or assign a new logical key |
| `NotFoundError` | Check workload ownership/ID. Logs may not yet be committed |
| `BudgetExceededError` | For `payment_method_required`, add a card in Billing. Otherwise inspect available credits and account funding |
| `SpendCheckUnavailableError` | Spending authorization is temporarily unavailable. Retry using the same submission key |
| `RateLimitError` | Pace requests. SDK honors bounded retry-after delays |
| `CapacityUnavailableError` | Retry later or relax feasible workload constraints |
| `APIConnectionError` / `APITimeoutError` | Preserve submission key. Outcome may be unknown |
| `AssetInUseError` | Finish or cancel dependent workloads before deleting the asset |
| `APIError` | Inspect HTTP status and response payload |

SDK request retries are finite. Capacity becoming available is not guaranteed.
A network exception does not prove a submission failed to reach the server.
See [idempotency](../guides/ci-and-idempotency.md) and
[retry policy](../concepts/reliability.md).

## Common first-run problems

- **Payment method required:** add a card in [Billing](https://console.nodus-compute.ai/?view=billing).
  This applies even when starter credits remain. Shared workspace members should
  ask their administrator. HTTP 402 with code `payment_method_required` uses the
  existing `BudgetExceededError` exception type.

- **Script not found:** put it in the container image and use an absolute path.
- **No bootstrap tool:** include `curl`, `wget`, or `python3` in the image.
- **No log yet:** inspect lifecycle events and retry when logs become available.
- **Login changed nothing:** environment variables override saved credentials.
- **Wait returned but work failed:** inspect `succeeded`, events, and logs.
- **Cancellation unconfirmed:** run `nodus cancel WORKLOAD_ID` and inspect status.
  A lost connection or force-killed process cannot confirm remote cleanup.
- **Download failed:** ensure the destination parent exists, choose a declared
  output name/stage, and retry. Integrity failures leave existing files intact.
- **Missing output methods:** upgrade with
  `pip install --upgrade nodus-compute`.

A known image without a bootstrap fetch tool can
still emit `UserWarning` before submission.

## Backend compatibility

The SDK and backend must support the same features. Upgrading the Python package
does not deploy backend changes. Login verification requires `GET /v1/me`, and
live log streaming requires `GET /v1/workloads/{id}/logs/live`. A 404 from these
routes can mean the deployment lacks the feature, even when saved credentials,
workload history, and committed logs still work.

GPU enforcement also requires matching backend support.
An older server may ignore fields it does not recognize. Successful submission
alone does not prove those constraints were applied. Confirm support with the
deployment operator before relying on a specific GPU.

If login verification is unavailable, preserve the saved credentials and retry
after the backend is updated. Use committed logs to inspect output when live
streaming is unavailable. Optimization tiers are not supported.
Existing optimization arguments are accepted for compatibility
but have no preference effect on new runs.
