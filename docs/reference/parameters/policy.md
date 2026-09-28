# Policy and data regions

| Argument | Type | Omitted | Workload file | HTTP field |
|---|---|---|---|---|
| `data_regions` | `list[str]` | No explicit region restriction from shortcut | `data_regions` | `policy.data_regions` |
| `policy` | Dictionary | Absent unless populated | Same key or table | `policy` |

`data_regions` is a list of exact region identifiers accepted by your deployment.
An empty list adds no location restriction. There is no universal region list
or region-discovery method in this SDK. For the hosted service,
[contact Nodus](mailto:nodus.infrastructure@gmail.com) for the enabled identifiers
before setting a location restriction. For a private deployment, obtain them
from its administrator. Do not assume a cloud provider's region codes are valid.

Restricting regions narrows eligible routes and can make a workload infeasible.
The SDK forwards the identifiers without translating them.

Inside a `with nodus.Client() as client:` block:

```python
workload = client.run(
    image="pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime",
    command=["python", "-c", "print('regional workload')"],
    data_regions=[],
)
```

This example adds no region restriction. If your workload requires a particular
geography, replace the empty list with the exact approved identifiers before
submitting. Do not use unrestricted execution for a location-sensitive workload.

`policy["data_regions"]` wins over the flat `data_regions` argument. Regions
belong under policy, not requirements. Omission does not promise execution in any
particular geography. Deployment and account policies may still constrain it.
`policy.secret_refs` accepts tenant secret names or IDs. Admission pins each
version and supplies it as `NODUS_SECRET_<NAME>` in the execution environment.
`policy.egress_allow` accepts HTTPS hostnames to add to a live connection's
allowlist. These fields require an isolated execution provider.

Attach a wandb connection with `connections=["lab-wandb"]` and optionally set
`sweep_id="experiment-42"` to group runs. See [live connections](../../guides/connections.md#attach-live-wandb-to-a-run)
for credential delivery, network restrictions and captured run links.
