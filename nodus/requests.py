"""Typed dictionaries for advanced workload arguments.

These types provide editor completion and static checking. They construct
ordinary dictionaries: they do not add runtime validation, apply defaults, or
change the existing ``Client.run`` and ``AsyncClient.run`` wire format.
Use the short ``run`` arguments for a single workload and these types when
assembling reusable requirements or a multi-stage graph.
"""

from typing import Literal, TypedDict

from .types import ComputeClass, ContinuityMode

__all__ = ["Source", "Requirements", "Placement", "Policy", "ContinuitySpec", "StageInput", "StageSpec", "OutputSink", "OutputSpec"]


class Source(TypedDict, total=False):
    """Container image and argument vector for a stage.

    ``command`` is an argv list, not a shell string. Include an explicit
    command for portable execution across deployment modes.
    """

    asset_id: str
    image: str
    command: list[str]


class Requirements(TypedDict, total=False):
    """Workload fit signals, without selecting a supplier or instance SKU.

    Memory is in GB and dataset size in bytes. All fields
    are optional. Omitted compute class defaults to accelerator on the API.
    ``gpu_count`` selects GPUs per machine. ``total_gpu_count`` requests the
    total with automatic allocation, subject to server admission support.
    These two count fields cannot be combined.
    """

    model: str
    compute_class: Literal["vm", "accelerator"] | ComputeClass
    dataset_bytes: int
    gpu: str
    gpu_count: Literal[1, 2, 4, 8]
    total_gpu_count: int
    gpu_interconnect: Literal["any"]
    peak_memory_gb: float
    optimization: Literal["", "automatic", "lowest_cost", "lower_cost", "balanced", "faster", "fastest"]
    disk_gb: float
    vcpus: float
    notes: str


class Placement(TypedDict, total=False):
    """Select one customer pool or skip private pools with prefer="any".

    Use exactly one field. Omission lets Nodus prefer eligible private capacity.
    """

    pool: str
    prefer: Literal["any"]


class Policy(TypedDict, total=False):
    """Placement constraints. Region identifiers depend on available capacity."""

    data_regions: list[str]
    secret_refs: list[str]
    egress_allow: list[str]


class ContinuitySpec(TypedDict, total=False):
    """Interruption behavior for a workload or stage.

    The top-level SDK default is checkpointed with resumption enabled.
    Omitted stage continuity inherits from the workload. Stage values are
    passed through for the server to resolve.
    Missing or empty workload checkpoint_paths defaults to ["state"] on the
    server. Missing or empty stage paths inherit workload paths. ["."] opts
    into the whole code folder. NODUS_CHECKPOINT_DIR remains the state folder.
    Application checkpoint preparation is opt-in through integration="auto"
    or "hf-trainer-v1". Omission or "none" leaves it disabled for new workloads.
    """

    mode: Literal["checkpointed", "restartable", "ephemeral"] | ContinuityMode
    resume_on_interruption: bool
    checkpoint_paths: list[str]
    integration: Literal["auto", "none", "hf-trainer-v1"]


class StageInput(TypedDict):
    """Named input supplied by an upstream stage's declared output.

    ``from_stage`` is the upstream stage ID. ``from_output`` is a key in that
    stage's ``outputs`` mapping. ``name`` identifies the downstream input.
    """

    name: str
    from_stage: str
    from_output: str


class OutputSink(TypedDict):
    """Database connection reference and destination table."""

    connection: str
    table: str


class OutputSpec(TypedDict):
    """A result file to load into a connected database."""

    path: str
    sink: OutputSink


class _StageRequired(TypedDict):
    id: str


class StageSpec(_StageRequired, total=False):
    """One stage in a workload DAG. ``id`` must be unique within the graph.

    ``depends_on`` lists upstream stage IDs. ``outputs`` maps output names
    to paths relative to the stage's working directory. ``total_units``
    describes progress units, not the number of GPUs or replicas.
    """

    source: Source
    inputs: list[StageInput]
    continuity: ContinuitySpec
    requirements: Requirements
    depends_on: list[str]
    total_units: int
    outputs: dict[str, str | OutputSpec]
