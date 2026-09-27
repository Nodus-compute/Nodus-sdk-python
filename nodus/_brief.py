"""Brief construction: keyword arguments in, wire payload out.

The submission schema is nested (source / requirements / outcome / continuity /
stages) because those are different concerns with different lifetimes. Callers
should not have to assemble that by hand, so ``run()`` takes flat keyword
arguments and this module does the translation in one place, shared by the
sync client, the async client, and the CLI so all three send the same bytes.
"""

from __future__ import annotations

import difflib
import inspect
import math
import os
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit, unquote
import posixpath
import shlex
import sys
import warnings
from datetime import datetime, timezone
from typing import Any

from ._rl_setup import rl_payload
from .types import WorkloadStatus
from ._connections import _live_refs, _group
from ._outputs import portable_output_name
from .requests import OutputSpec

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


def _caller_stacklevel() -> int:
    """How far up the stack the code that wrote the brief is.

    Counted, not hardcoded: ``run()`` and ``build_payload()`` sit at different
    depths, and the default filter shows one warning per location, a warning
    blamed on SDK source silences every submission after the first.
    """
    frame = inspect.currentframe()
    frame = frame.f_back if frame is not None else None  # the warning's own frame
    level = 1
    while frame is not None:
        if os.path.dirname(os.path.abspath(frame.f_code.co_filename)) != _PACKAGE_DIR:
            return level
        frame = frame.f_back
        level += 1
    return level

# The runner installs itself onto the rented host by fetching its artifact with
# curl, then wget, then a stdlib python3. An image carrying none of the three
# cannot start the work, so it bills for a host that never runs anything.
BOOTSTRAP_FETCH_TOOLS = frozenset({"curl", "wget", "python3"})

# Which of those tools the stock image ships, measured 2026-08-29. An empty set
# means the image cannot bootstrap; an image absent from this table has not been
# measured and nothing is claimed about it.
IMAGE_FETCH_TOOLS: dict[str, frozenset[str]] = {
    "ubuntu:22.04": frozenset(),
    "python:3.11-slim": frozenset({"python3"}),
    "pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime": frozenset({"python3"}),
}

DEFAULT_IMAGE = "python:3.11-slim"


def _warn_if_it_cannot_bootstrap(image: str) -> None:
    """Warn while the brief is still free, for images measured to ship no fetch tool.

    Only measured images are named. What an arbitrary tag contains is knowable
    from a registry, not from here, so an unrecognised image is left alone
    rather than guessed at, and this warns rather than refuses, because the
    tag may be a local rebuild that added one.
    """
    tools = IMAGE_FETCH_TOOLS.get(image)
    if tools is None or tools & BOOTSTRAP_FETCH_TOOLS:
        return
    warnings.warn(
        f"image {image!r} ships no curl, wget or python3, so the Nodus runner cannot "
        "install itself onto the host. Capacity may incur costs before your command starts. "
        f"Use an image carrying one of them, such as the default {DEFAULT_IMAGE!r}.",
        stacklevel=_caller_stacklevel(),
    )


def _as_command(command: list[str] | str | None) -> list[str]:
    """Argv for the workload. A string is split the way a shell would split it."""
    if isinstance(command, str):
        return shlex.split(command)
    if command:
        return list(command)
    return []


def _as_timestamp(value: datetime | str | None) -> str | None:
    """RFC3339 for the wire. A naive datetime is read as local time, as Python does."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _enum_value(v: Any) -> Any:
    """Accept an enum member or its wire string interchangeably."""
    return getattr(v, "value", v)


_ASSET_ATTACHMENT_GUIDANCE = (
    "Attach source code with source_asset_id=asset.id and datasets with "
    'inputs=[{"name": "training", "asset_id": dataset.id}]. '
    "Pass these as run() keyword arguments. Asset aliases in extra do not attach files."
)

# Brief fields the control plane does not model, and what to reach for instead.
# Sending one costs a caller the constraint they believe they set.
UNSUPPORTED: dict[str, str] = {
    "assets": _ASSET_ATTACHMENT_GUIDANCE,
    "asset_id": _ASSET_ATTACHMENT_GUIDANCE,
    "expected_runtime_hours": "Remove expected_runtime_hours. Nodus estimates runtime automatically.",
    "interrupt_tolerance": (
        "the control plane does not model this yet: it derives the envelope's "
        "tolerance from continuity, so declaring 'low' here yields the opposite. "
        "Use continuity= to say what an interruption should cost you."
    ),
    "env": (
        "the control plane does not model this yet: a workload's source is an "
        "image and a command, so environment never reaches the host. Bake the "
        "values into the image, or pass them in the command."
    ),
}


def _reject_unknown(unknown: dict[str, Any], known: tuple[str, ...]) -> None:
    """Refuse a keyword this SDK does not model, naming what it looked like.

    The control plane ignores fields it does not know, so a forwarded typo is
    accepted and runs: a misspelled resource requirement can select a machine
    that does not fit the workload. :data:`UNSUPPORTED` names the fields that
    deserve a better refusal than "unknown".
    """
    if not unknown:
        return
    named = sorted(set(unknown) & set(UNSUPPORTED))
    if named:
        raise TypeError(
            "\n".join(f"{name}=: {UNSUPPORTED[name]}" for name in named)
        )
    parts = []
    for name in sorted(unknown):
        near = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
        parts.append(f"{name!r}" + (f" (did you mean {near[0]!r}?)" if near else ""))
    raise TypeError(
        "unknown brief field: "
        + ", ".join(parts)
        + ". The control plane ignores fields it does not model, so this would "
        "have been submitted and silently dropped. Use a documented run() keyword. "
        "Use extra={...} only for a confirmed server field that this SDK version does not model."
    )


def build_payload(
    *,
    image: str | None = None,
    source_asset_id: str | None = None,
    inputs: list[dict[str, Any]] | None = None,
    outputs: dict[str, str | OutputSpec] | None = None,
    command: list[str] | str | None = None,
    requirements: dict[str, Any] | None = None,
    placement: dict[str, Any] | None = None,
    model: str | None = None,
    compute_class: Any = None,
    peak_memory_gb: float | None = None,
    optimization: str | None = None,
    gpu: str | None = None,
    gpu_count: int | None = None,
    gpu_interconnect: str | None = None,
    budget: float | None = None,
    finish_by: datetime | str | None = None,
    continuity: Any = None,
    data_regions: list[str] | None = None,
    connections: list[str] | None = None,
    sweep_id: str | None = None,
    stages: list[dict[str, Any]] | None = None,
    framework: str | None = None,
    policy: dict[str, Any] | None = None,
    rl: Any = None,
    extra: dict[str, Any] | None = None,
    **unknown: Any,
) -> dict[str, Any]:
    """Build the ``POST /v1/workloads`` body from a flat brief.

    ``extra`` is merged last, for a field the control plane models and this SDK
    version does not. Anything else is refused rather than forwarded.
    """
    _reject_unknown(unknown, BRIEF_FIELDS)
    _validate_assets(source_asset_id, inputs)
    _validate_outputs(outputs)
    for stage in stages or []:
        if isinstance(stage, dict) and isinstance(stage.get("continuity"), dict):
            validate_checkpoint_integration(stage["continuity"])
        if isinstance(stage, dict) and stage.get("outputs"):
            _validate_outputs(stage["outputs"])
            if not portable_output_name(stage.get("id")):
                raise ValueError("Stages with outputs must use portable file names as IDs.")
    if outputs is not None and (stages or framework):
        raise TypeError("outputs cannot be combined with stages or framework. Declare outputs on each stage.")
    req: dict[str, Any] = dict(requirements or {})
    if model is not None:
        req.setdefault("model", model)
    if compute_class is not None:
        req.setdefault("compute_class", _enum_value(compute_class))
    if peak_memory_gb is not None:
        req.setdefault("peak_memory_gb", peak_memory_gb)
    if "optimization" not in req and optimization is not None:
        if optimization == "":
            raise ValueError("optimization must be one of: " + ", ".join(OPTIMIZATIONS))
        req["optimization"] = optimization
    if gpu is not None:
        req.setdefault("gpu", gpu)
    if gpu_count is not None:
        req.setdefault("gpu_count", gpu_count)
    if gpu_interconnect is not None:
        req.setdefault("gpu_interconnect", gpu_interconnect)
    req = validate_requirements(req)

    # Data residency lives in policy: the envelope reads Policy.DataRegions, and
    # Requirements has no such field. An explicit policy= wins over the shortcut.
    pol: dict[str, Any] = dict(policy or {})
    if data_regions:
        pol.setdefault("data_regions", list(data_regions))

    _validate_bucket_regions(inputs, pol)

    outcome: dict[str, Any] = {}
    if budget is not None:
        try:
            amount = float(budget)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("Legacy budget must be a finite non-negative number.") from None
        if isinstance(budget, bool) or not math.isfinite(amount) or amount < 0:
            raise ValueError("Legacy budget must be a finite non-negative number.")
        outcome["max_cost_usd"] = amount
    deadline = _as_timestamp(finish_by)
    if deadline:
        outcome["complete_by"] = deadline

    # Checkpointed by default: losing a long run to a reclaim is the expensive
    # failure, and the caller has to opt out of durability rather than into it.
    if continuity is None:
        cont: dict[str, Any] = {"mode": "checkpointed", "resume_on_interruption": True}
    elif isinstance(continuity, dict):
        # Same default as the string form: an absent flag reads as true on
        # arrival, handing "ephemeral" durability it declines. A written flag is kept.
        cont = dict(continuity)
        mode = _enum_value(cont.get("mode", "checkpointed"))
        cont["mode"] = mode
        cont.setdefault("resume_on_interruption", mode != "ephemeral")
    else:
        mode = _enum_value(continuity)
        cont = {"mode": mode, "resume_on_interruption": mode != "ephemeral"}
    validate_checkpoint_integration(cont)

    payload: dict[str, Any] = {
        "requirements": req,
        "outcome": outcome,
        "continuity": cont,
    }

    if stages:
        # A staged brief has a source per stage, so a top-level one has nowhere
        # to go: dropping it would run something other than what the brief says.
        discarded = sorted(
            name
            for name, value in (
                ("image", image), ("command", command), ("source_asset_id", source_asset_id)
            )
            if value
        )
        if discarded:
            raise TypeError(
                "stages= replaces the top-level source, so "
                + ", ".join(f"{n}=" for n in discarded)
                + " would be dropped rather than run. Put them on the stage that "
                "needs them: stages=[{'id': ..., 'source': {'image': ..., "
                "'command': [...]}}]."
            )
        payload["stages"] = [dict(s) for s in stages]
    else:
        src: dict[str, Any] = {"image": image or DEFAULT_IMAGE}
        cmd = _as_command(command)
        if cmd:
            src["command"] = cmd
        if source_asset_id is not None:
            src["asset_id"] = source_asset_id
        if outputs is not None:
            payload["stages"] = [{"id": "main", "source": src, "outputs": dict(outputs)}]
        else:
            payload["source"] = src

    if inputs is not None:
        payload["inputs"] = [dict(value) for value in inputs]

    if framework:
        payload["framework"] = framework
    if pol:
        payload["policy"] = pol

    if placement is not None:
        payload["placement"] = validate_placement(placement)
    if connections is not None:
        payload["connections"] = connections
    if sweep_id is not None:
        payload["sweep_id"] = sweep_id
    _merge_extra(payload, extra)
    if "connections" in payload:
        payload["connections"] = _live_refs(payload["connections"])
    if "sweep_id" in payload:
        payload["sweep_id"] = _group(payload["sweep_id"])
    if payload.get("placement") is not None:
        payload["placement"] = validate_placement(payload["placement"])
    if "expected_runtime_hours" in payload:
        raise ValueError(UNSUPPORTED["expected_runtime_hours"])
    for stage in payload.get("stages", []):
        if "total_units" in stage:
            total_units = stage["total_units"]
            if isinstance(total_units, bool) or not isinstance(total_units, int) or total_units < 0:
                raise ValueError("stage total_units must be a nonnegative integer")
        if "expected_runtime_hours" in stage:
            raise ValueError(UNSUPPORTED["expected_runtime_hours"])
        if "requirements" in stage:
            stage["requirements"] = validate_requirements(stage["requirements"])
    if rl is not None:
        # extra is merged earlier, so without this check one of the two would
        # silently win.
        if "rl" in payload:
            raise ValueError("Pass RL settings once: use rl= or extra={'rl': ...}, not both")
        payload["rl"] = rl_payload(rl)
    _warn_about_the_money(payload)
    return payload


def validate_checkpoint_integration(continuity: dict[str, Any]) -> None:
    """Validate an explicit application checkpoint integration selection."""
    if 'integration' in continuity and continuity['integration'] not in ('auto', 'none', 'hf-trainer-v1'):
        raise ValueError('continuity.integration must be auto, none, or hf-trainer-v1')


def validate_placement(placement: Any) -> dict[str, str]:
    if not isinstance(placement, dict):
        raise ValueError("placement must contain one pool or prefer='any'")
    if set(placement) == {"prefer"} and placement["prefer"] == "any":
        return dict(placement)
    if set(placement) == {"pool"}:
        pool = placement["pool"]
        if isinstance(pool, str) and 0 < len(pool.encode("utf-8")) <= 200 and pool.strip() == pool and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in pool):
            return dict(placement)
    raise ValueError("placement must name one nonempty pool or set prefer='any', with no extra fields")


OPTIMIZATIONS = ("automatic", "lowest_cost", "lower_cost", "balanced", "faster", "fastest")
GPU_FAMILIES = ("A100", "H100", "H200", "B200", "A10", "A10G", "L4", "L40", "L40S", "T4", "V100", "RTX A6000", "RTX 3090", "RTX 4090", "RTX 5090")


def validate_requirements(requirements: dict[str, Any]) -> dict[str, Any]:
    """Validate customer placement choices without mutating their dictionary."""
    if not isinstance(requirements, dict):
        raise ValueError("requirements must be a dictionary")
    result = dict(requirements)
    if "expected_runtime_hours" in result:
        raise ValueError(UNSUPPORTED["expected_runtime_hours"])
    if "optimization" in result and result["optimization"] not in ("", *OPTIMIZATIONS):
        raise ValueError("optimization must be one of: " + ", ".join(OPTIMIZATIONS))
    for field in ("peak_memory_gb", "disk_gb", "vcpus", "dataset_bytes"):
        if field not in result:
            continue
        value = result[field]
        allowed = (int,) if field == "dataset_bytes" else (int, float)
        bound = "positive" if field == "peak_memory_gb" else "nonnegative"
        kind = "integer" if field == "dataset_bytes" else "number"
        message = f"{field} must be a finite {bound} {kind}"
        if isinstance(value, bool) or not isinstance(value, allowed):
            raise ValueError(message)
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite or (value <= 0 if field == "peak_memory_gb" else value < 0):
            raise ValueError(message)
    if "gpu_count" in result:
        value = result["gpu_count"]
        if type(value) is not int or value not in (1, 2, 4, 8):
            raise ValueError("gpu_count must be 1, 2, 4, or 8 on one machine")
    if result.get("gpu_interconnect", "") not in ("", "any"):
        raise ValueError("gpu_interconnect requires verified topology that is not currently available")
    if result.get("compute_class") == "vm" and any(k in result for k in ("gpu", "gpu_count", "gpu_interconnect")):
        raise ValueError("GPU configuration requires accelerator compute")
    if "gpu" in result:
        value = result["gpu"]
        compact = re.sub(r"[\s_-]+", "", value.upper()) if isinstance(value, str) else ""
        compact = compact.removeprefix("NVIDIA")
        if compact == "A6000":
            compact = "RTXA6000"
        canonical = next((family for family in GPU_FAMILIES if family.replace(" ", "") == compact), None)
        if canonical is None:
            raise ValueError("gpu must name a supported GPU family: " + ", ".join(GPU_FAMILIES) + ". Use peak_memory_gb for memory requirements.")
        result["gpu"] = canonical
    return result


def _validate_assets(source_asset_id: str | None, inputs: list[dict[str, Any]] | None) -> None:
    def valid_id(value: Any) -> bool:
        return isinstance(value, str) and re.fullmatch(r"asset_[A-Za-z0-9-]{1,64}", value) is not None
    if source_asset_id is not None and not valid_id(source_asset_id):
        raise ValueError("source_asset_id must be an asset ID returned by Nodus.")
    if inputs is None:
        return
    if not isinstance(inputs, list) or len(inputs) > 8:
        raise ValueError("inputs must be a list of at most eight named assets.")
    names: set[str] = set()
    for value in inputs:
        if not isinstance(value, dict) or "name" not in value:
            raise ValueError("Each input requires a name and either asset_id or bucket.")
        name = value["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) or name in names:
            raise ValueError("Input names must be unique identifiers starting with a letter.")
        if "bucket" in value:
            if set(value) != {"name", "bucket"}:
                raise ValueError("Bucket inputs cannot contain asset_id, cache or credentials.")
            _validate_bucket(value["bucket"])
        else:
            if not {"name", "asset_id"} <= set(value) or not set(value) <= {"name", "asset_id", "cache"}:
                raise ValueError("Each asset input requires name and asset_id.")
            if "cache" in value and type(value["cache"]) is not bool:
                raise ValueError("Input cache must be a boolean.")
            if not valid_id(value["asset_id"]):
                raise ValueError("Input asset_id must be an asset ID returned by Nodus.")
        names.add(name)


def _validate_bucket(value: Any) -> None:
    fields = {"uri", "region", "bytes", "sha256", "credential_source"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Bucket inputs require uri, region, bytes, sha256 and credential_source only. Never supply credentials.")
    if value["credential_source"] != "team_webhook":
        raise ValueError("Bucket credential_source must be team_webhook.")
    if type(value["bytes"]) is not int or not 0 <= value["bytes"] <= 1 << 40:
        raise ValueError("Bucket bytes must be an integer from zero through 1 TiB.")
    if not isinstance(value["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"]):
        raise ValueError("Bucket sha256 must be a lowercase SHA-256 digest.")
    if not isinstance(value["uri"], str) or not isinstance(value["region"], str):
        raise ValueError("Bucket URI and region must be strings.")
    uri = value["uri"]
    parsed = urlsplit(uri)
    path = unquote(parsed.path)
    region_pattern = r"[a-z]{2}(?:-[a-z]+)+-[0-9]+" if parsed.scheme == "s3" else r"[a-z]+-[a-z]+[0-9]+"
    if (parsed.scheme not in {"s3", "gs"} or "?" in uri or "#" in uri or parsed.username is not None
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", parsed.netloc) or ".." in parsed.netloc
            or len(path) < 2 or len(path) > 4096 or not path.startswith("/") or posixpath.normpath(path) != path
            or any(c in path for c in ("\\", "\r", "\n", "\x00"))
            or not re.fullmatch(region_pattern, value["region"]) or value["region"].startswith("cn-")):
        raise ValueError("Bucket URI must be a canonical s3:// or gs:// object without credentials or query parameters, in its exact region.")


def _validate_bucket_regions(inputs: list[dict[str, Any]] | None, policy: dict[str, Any]) -> None:
    for value in inputs or []:
        if "bucket" in value and policy.get("data_regions") != [value["bucket"]["region"]]:
            raise ValueError("Bucket inputs require data_regions containing only the exact bucket region.")


def _validate_outputs(outputs: dict[str, str | OutputSpec] | None) -> None:
    if outputs is None:
        return
    if not isinstance(outputs, dict):
        raise TypeError("outputs must map output names to relative file paths.")
    if len({name.casefold() for name in outputs if isinstance(name, str)}) != len(outputs):
        raise ValueError("Output names must be distinct on case-insensitive filesystems.")
    targets = set()
    for name, path in outputs.items():
        if isinstance(path, dict):
            if set(path) - {"path", "sink"} or "path" not in path:
                raise ValueError("Output declarations require path and an optional sink.")
            sink = path.get("sink")
            if sink is not None:
                if (not isinstance(sink, dict) or set(sink) != {"connection", "table"}
                        or not isinstance(sink.get("connection"), str) or not sink["connection"]
                        or not isinstance(sink.get("table"), str)
                        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", sink["table"])
                        or sink["table"].lower().startswith("nodus_")):
                    raise ValueError("Output sinks require a connection and one non-reserved PostgreSQL table identifier.")
                if not isinstance(path["path"], str) or PurePosixPath(path["path"]).suffix.lower() not in (".csv", ".jsonl", ".parquet"):
                    raise ValueError("Sink files must be CSV, JSONL or Parquet.")
                target = (sink["connection"], sink["table"].lower())
                if target in targets:
                    raise ValueError("Outputs in one stage must use distinct sink tables.")
                targets.add(target)
            path = path["path"]
        if not portable_output_name(name):
            raise ValueError("Output names must be portable file names using letters, digits, dots, underscores or hyphens.")
        if (not isinstance(path, str) or not path or "\\" in path or ":" in path
                or any(ord(c) < 32 for c in path) or PurePosixPath(path).is_absolute()
                or any(part in ("", ".", "..") for part in path.split("/"))):
            raise ValueError("Output paths must name files inside the workload working directory.")


def _merge_extra(payload: dict[str, Any], extra: dict[str, Any] | None) -> None:
    """Add fields this SDK version does not model. Never replace one it does.

    A key that collides with the built brief would overwrite it, ``outcome``
    included, which is where the cost ceiling lives.
    """
    if not extra:
        return
    asset_aliases = sorted(set(extra) & {"assets", "asset_id", "source_asset_id"})
    if asset_aliases:
        raise TypeError(
            "extra= contains unsupported asset fields: "
            + ", ".join(asset_aliases)
            + ". "
            + _ASSET_ATTACHMENT_GUIDANCE
        )
    clashes = sorted(set(extra) & set(payload))
    if clashes:
        raise TypeError(
            "extra= would replace "
            + ", ".join(repr(k) for k in clashes)
            + ", which this brief already built"
            + (", including the cost ceiling in 'outcome'" if "outcome" in clashes else "")
            + ". Pass the value through the keyword that builds it, or drop it "
            "from extra=. The extra argument is for fields the control plane models and this "
            "SDK version does not."
        )
    payload.update(extra)


def _warn_about_the_money(payload: dict[str, Any]) -> None:
    """Money warnings, read off the payload as it will be sent.

    After the merge, not before: a warning drawn from a draft can describe a
    submission that never happens.
    """
    source = payload.get("source") or {}
    if source.get("image"):
        _warn_if_it_cannot_bootstrap(source["image"])
    for stage in payload.get("stages") or []:
        named = (stage.get("source") or {}).get("image")
        if named:
            _warn_if_it_cannot_bootstrap(named)


#: The keywords a brief may name, read off the translator so the two cannot drift.
BRIEF_FIELDS: tuple[str, ...] = tuple(
    name
    for name, p in inspect.signature(build_payload).parameters.items()
    if p.kind is inspect.Parameter.KEYWORD_ONLY
)


#: The presets the control plane expands into concrete statuses server-side.
STATUS_PRESETS: tuple[str, ...] = ("active", "terminal")

#: Every token a status filter may name.
STATUS_FILTERS: tuple[str, ...] = tuple(
    sorted({s.value for s in WorkloadStatus}.union(STATUS_PRESETS))
)


def _one_status(value: Any) -> str:
    """One filter token, checked against the vocabulary the server expands.

    Unrecognised tokens are dropped on arrival, and a filter that expands to
    nothing lists the whole account.
    """
    wire = str(_enum_value(value)).strip()
    if wire in STATUS_FILTERS:
        return wire
    near = difflib.get_close_matches(wire, STATUS_FILTERS, n=1, cutoff=0.6)
    raise ValueError(
        f"{wire!r} is not a workload status"
        + (f" (did you mean {near[0]!r}?)" if near else "")
        + ". The control plane ignores a filter token it does not know, and a "
        "filter that matches nothing is no filter, so this would have listed "
        "every workload on the account. Statuses: " + ", ".join(STATUS_FILTERS) + "."
    )


def status_filter(status: Any) -> str | None:
    """Normalise a status filter into the wire form.

    Accepts a member, a wire string, a comma-joined string, a list of either,
    or the presets ``"active"`` and ``"terminal"``. A token the server could
    not expand is a ValueError here rather than a listing of everything.
    """
    if status is None:
        return None
    if isinstance(status, (list, tuple, set, frozenset)):
        tokens = [s for s in status if s is not None]
    elif isinstance(status, str):
        tokens = [t for t in status.split(",") if t.strip()]
    else:
        tokens = [status]
    return ",".join(_one_status(t) for t in tokens) or None
