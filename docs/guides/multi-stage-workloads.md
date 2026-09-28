# Multi-stage workloads and final outputs

Use an explicit stage list when work has multiple steps or dependencies.
For one command, declare downloadable files with `outputs={"result": "result.json"}`
directly on `client.run()`. Multiple stages can reference output names without
sharing a machine or filesystem.

Save the [complete Python example](../../examples/multi_stage.py) as `multi_stage.py`
in your current directory. Example scripts are not installed by pip. Then run:

```bash
python multi_stage.py --submission-id pipeline-001
```

The first stage writes `numbers.json`. The second reads the resolved upstream
input from `NODUS_INPUT_numbers`, writes `result.json`, and declares it as
`result`. After successful completion, the example downloads that output to
`results/result.json`. The example uses GPU capacity and small inputs to demonstrate file transfer.
Replace the stage commands with your training and evaluation code for real work.
Stages execute serially, including stages without dependencies.

This example requires the deployment's runner to support declared outputs and
resolved input environment variables. It illustrates the runtime contract. It
is not a promise that every deployed runner revision supports every feature.

See [`examples/multi_stage.py`](../../examples/multi_stage.py) for the code and
[every stage field](../reference/parameters/stages.md) for inheritance and rules.
No top-level `image` or `command` is passed when stages provide their own sources.
