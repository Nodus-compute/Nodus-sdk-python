# Install and sign in

Install [nodus-compute from PyPI](https://pypi.org/project/nodus-compute/)
with Python 3.10 or newer:

```bash
pip install nodus-compute
nodus login
```

Your browser opens the Nodus sign-in page. Sign in, check that the device code
matches your terminal, and approve. Close the tab once approved. Your terminal
updates automatically and saves the login for both the CLI and Python SDK.
There is no API URL or key to copy. Running `nodus login` again verifies and
reuses your login. It welcomes you by email when available. Use `nodus login --force` to start a fresh browser sign-in. A connection failure preserves your
saved credentials so you can retry.

Existing users can upgrade with `pip install --upgrade nodus-compute`.
Use the current package for the documented commands and workload experience.
Custom or older servers may not support every feature. See
[backend compatibility](../operations/errors.md#backend-compatibility).

New accounts start with $30 in credits and can run workloads without adding a
payment method. Available credits determine
whether a run can start. Check your balance in
[Billing](https://console.nodus-compute.ai/?view=billing). Shared workspace
members use the team's credits and administrator-controlled limits. Adding an
optional payment method does not purchase credits or increase your balance.

## Use your login in Python

```python
import nodus

with nodus.Client() as client:
    for workload in client.list(limit=5):
        print(workload.id, workload.status)
```

This lists your workloads without starting paid compute.
Continue with [your first GPU workload](../../README.md#2-run-your-first-workload).

## Without a local browser

```bash
nodus login --no-browser
```

Open the printed link on another device. Approve the matching code there.
The original terminal saves the login automatically.

## API keys for automation

Set `NODUS_API_KEY` through your secret manager. `nodus.Client()` reads it
automatically and connects to the hosted service. Never commit an API key.
For an assistant preparing or running GPU work, follow the
[coding agent guide](../guides/agents.md).

## Custom deployments

Only private deployments and local development need a different API address:

```bash
nodus login --base-url https://YOUR_NODUS_API_HOST
```

Use the API origin without `/v1`. `NODUS_BASE_URL` and
`nodus.Client(base_url=...)` remain available.

| Setting | Resolution order |
|---|---|
| API key | Explicit argument, then `NODUS_API_KEY`, then saved login |
| API URL | Explicit argument, then `NODUS_BASE_URL`, then saved login, then hosted default |

Each setting resolves independently. An environment variable overrides the saved
login, so keep custom deployment credentials and addresses paired.
Credentials are stored in `~/.nodus/config.toml`. Keep this file private. On
Windows it inherits your profile directory's permissions.

## Sign out

```bash
nodus logout
```

This removes the locally saved key. To revoke that key on the server, use the
console. Environment variables remain set until you remove them.
