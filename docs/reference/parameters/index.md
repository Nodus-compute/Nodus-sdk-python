# Submission parameters

Choose the environment and command for your code, then set any GPU, memory,
and recovery requirements. `Client.run()` and `AsyncClient.run()` accept
the same named arguments. The table covers every explicit submission argument
and links to its accepted values, defaults, and examples.

The SDK translates these arguments into an HTTP request. Most omitted arguments
are not sent, except the default image and continuity policy.

| Python argument | HTTP location | Reference |
|---|---|---|
| `image`, `command` | `source.image`, `source.command` | [Container image and command](source.md) |
| `source_asset_id`, `inputs`, `outputs` | Source asset and stage file declarations | [Files](source.md#input-and-output-files) |
| `framework` | `framework` | [Framework execution](source.md) |
| `gpu` | `requirements.gpu` | `"A100"`, `"H100"`, `"H200"`, `"B200"`, `"A10"`, `"A10G"`, `"L4"`, `"L40"`, `"L40S"`, `"T4"`, `"V100"`, `"RTX A6000"`, `"RTX 3090"`, `"RTX 4090"`, `"RTX 5090"`. Omit to let Nodus choose. [GPU models and examples](requirements.md#gpu-model) |
| `optimization` | `requirements.optimization` | `"automatic"`, `"lowest_cost"`, `"lower_cost"`, `"balanced"`, `"faster"`, `"fastest"`. Compatibility only, omitted by default. No preference effect on new runs. [Optimization](requirements.md#optimization) |
| `model`, `compute_class`, `peak_memory_gb` | `requirements.*` | [Resources](requirements.md) |
| `requirements` | `requirements` | [Resources](requirements.md) |
| `finish_by` | `outcome.complete_by` | [Completion deadline](outcome.md) |
| `continuity` | `continuity` | [Recovery](continuity.md) |
| `connections`, `sweep_id` | Same top-level keys | [Live connections](../../guides/connections.md#attach-live-wandb-to-a-run) |
| `data_regions`, `policy` | `policy.data_regions`, `policy` | [Policy](policy.md) |
| `stages` | `stages` | [Stages](stages.md) |
| `idempotency_key` | `Idempotency-Key` header | [Safe retries](../../guides/ci-and-idempotency.md) |
| `extra` | Additional top-level fields | [Extensions](#extensions-and-validation) |

Do not pass `env`, `interrupt_tolerance`, or `expected_runtime_hours`: they are
explicitly unsupported. Unknown Python keywords raise `TypeError` before submission. Use the dictionary fields listed
in these references. Each page defines their accepted values and defaults.

## Extensions and validation

`extra: dict` adds top-level request fields that the deployed server models but
this SDK version does not expose. It defaults to no additions and has no CLI flag.
It cannot replace a key already built in the request, including `requirements`,
`outcome`, or `continuity`. Collisions raise `TypeError` before network access.
Prefer named arguments and documented typed fields.

A non-colliding name is not proof that a server supports it. Unknown server
fields may be ignored on older deployments. Verify support in the deployed
contract before using extensions. Do not send secrets in arbitrary metadata.

`expected_runtime_hours` is also rejected inside requirements and stage dictionaries.
Stage inputs have a separate supported shape. See [stages](stages.md).
Python typos raise `TypeError`, distinct from a server `nodus.ValidationError`.
