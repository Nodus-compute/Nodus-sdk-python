# Runnable examples

Authenticate first using the [setup guide](../docs/getting-started/authentication.md).
Save an example file in your current directory, then run its command below.
Example files are not installed by pip. These examples submit paid workloads.
Billing and capacity are checked at submission.
All programs use only standard Python plus the Nodus SDK. Submission requires a
running Nodus deployment. No local workload simulator is included.

| Example | Command | Purpose |
|---|---|---|
| [Basic](basic.py) | `python basic.py` | Print the GPU name |
| [Async sweep](async_sweep.py) | `python async_sweep.py --run-id sweep-001` | Three bounded concurrent submissions |
| [Multi-stage](multi_stage.py) | `python multi_stage.py --submission-id pipeline-001` | Produce, consume, and download a declared output |
| [CI](ci_submit.py) | `python ci_submit.py --submission-id build-001` | Stable submission key and terminal success handling |

Reuse a submission/run ID only for an identical logical request. IDs must be
unique for new work. Ctrl+C while waiting requests cancellation of submitted workloads with known IDs.
If cancellation fails, use `nodus cancel WORKLOAD_ID` and verify its status.
For training code packaged in your own image, see the
[GPU guide](../docs/guides/gpu-workloads.md).
