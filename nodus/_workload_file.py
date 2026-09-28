"""Shared workload configuration for Python and the command line."""

from __future__ import annotations

import math
import re
import shlex
from datetime import datetime
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from ._brief import _validate_assets, _validate_bucket_regions, _validate_outputs, validate_requirements, validate_checkpoint_integration, UNSUPPORTED
from ._connections import _live_refs, _group
from .requests import ContinuitySpec, Policy, Requirements, Source, StageInput, StageSpec

_TEMPLATE = '''# Edit the image and command for your workload.
image = "pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime"
command = ["python", "-c", "print(__import__('torch').cuda.get_device_name(0))"]
'''
_FIELDS = {
    'command', 'image', 'model', 'peak_memory_gb', 'optimization', 'gpu', 'gpu_count', 'gpu_interconnect',
    'budget', 'compute_class', 'continuity', 'finish_by', 'data_regions',
    'stages', 'framework', 'policy', 'requirements', 'idempotency_key',
    'source_asset_id', 'inputs', 'outputs', 'connections', 'sweep_id',
}


def _fail(name: str, message: str) -> None:
    raise ValueError(f'{name}: {message}')


def _text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        _fail(name, 'expected nonempty text')


def _asset(value: Any, name: str) -> None:
    _text(value, name)
    if not re.fullmatch(r'asset_[A-Za-z0-9-]{1,64}', value):
        _fail(name, 'expected an asset ID returned by Nodus')


def _strings(value: Any, name: str) -> None:
    if not isinstance(value, list):
        _fail(name, 'expected a list of strings')
    for item in value:
        _text(item, name)


def _table(value: Any, name: str, fields: set[str]) -> None:
    if not isinstance(value, dict):
        _fail(name, 'expected a table')
    unknown = set(value) - fields
    if unknown:
        _fail(name, 'unknown field ' + ', '.join(sorted(unknown)))


def _number(value: Any, name: str, integer: bool = False, zero: bool = False) -> None:
    allowed = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, allowed):
        _fail(name, 'expected a number' if not integer else 'expected an integer')
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (value < 0 if zero else value <= 0):
        _fail(name, 'expected a finite nonnegative number' if zero else 'expected a finite positive number')


def _command(value: Any, name: str, allow_string: bool = True) -> None:
    if allow_string and isinstance(value, str):
        _text(value, name)
        try:
            value = shlex.split(value)
        except ValueError as exc:
            _fail(name, str(exc))
    _strings(value, name)
    if not value:
        _fail(name, 'provide the command to run')


def _requirements(value: Any, name: str) -> None:
    normalized = validate_requirements(value)
    _table(value, name, set(Requirements.__annotations__))
    value.update(normalized)
    for key, item in value.items():
        field = f'{name}.{key}'
        if key == 'dataset_bytes':
            _number(item, field, integer=True, zero=True)
        elif key == 'peak_memory_gb':
            _number(item, field)
        elif key in {'disk_gb', 'vcpus'}:
            _number(item, field, zero=True)
        elif key in {'optimization', 'gpu_count'}:
            continue
        elif key == 'compute_class':
            if item not in ('vm', 'accelerator'):
                _fail(field, 'expected accelerator or vm')
        else:
            _text(item, field)


def _continuity(value: Any, name: str, allow_string: bool = True) -> None:
    if allow_string and isinstance(value, str):
        value = {'mode': value}
    _table(value, name, set(ContinuitySpec.__annotations__))
    validate_checkpoint_integration(value)
    if 'mode' in value and value['mode'] not in ('checkpointed', 'restartable', 'ephemeral'):
        _fail(name + '.mode', 'expected checkpointed, restartable, or ephemeral')
    if 'resume_on_interruption' in value and not isinstance(value['resume_on_interruption'], bool):
        _fail(name + '.resume_on_interruption', 'expected true or false')
    if 'checkpoint_paths' in value:
        field = name + '.checkpoint_paths'
        paths = value['checkpoint_paths']
        if not isinstance(paths, list):
            _fail(field, 'expected a list of paths')
        if len(paths) > 64:
            _fail(field, 'provide no more than 64 paths')
        for item in paths:
            if (not isinstance(item, str) or not item or len(item.encode('utf-8')) > 512 or
                    re.search(r'[\\:\x00-\x1f\x7f-\x9f]', item) or
                    item == '.nodus' or item.startswith('.nodus/') or
                    (item != '.' and any(part in ('', '.', '..') for part in item.split('/')))):
                _fail(field, 'use normalized relative paths outside .nodus, or . for the whole code folder')


def _stages(stages: Any) -> None:
    if not isinstance(stages, list) or not stages:
        _fail('stages', 'expected a nonempty list of stages')
    by_id = {}
    for index, stage in enumerate(stages):
        name = f'stages[{index}]'
        _table(stage, name, set(StageSpec.__annotations__))
        _text(stage.get('id'), name + '.id')
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', stage['id']):
            _fail(name + '.id', 'use 1 to 64 letters, digits, underscores, dots, or hyphens')
        if stage['id'] in by_id:
            _fail(name + '.id', 'stage IDs must be unique')
        by_id[stage['id']] = stage
        source = stage.get('source')
        _table(source, name + '.source', set(Source.__annotations__) | {'asset_id'})
        _command(source.get('command'), name + '.source.command', allow_string=False)
        for field in ('image', 'asset_id'):
            if field in source:
                if field == 'asset_id':
                    _asset(source[field], name + '.source.' + field)
                else:
                    _text(source[field], name + '.source.' + field)
        if 'requirements' in stage:
            _requirements(stage['requirements'], name + '.requirements')
        if 'continuity' in stage:
            _continuity(stage['continuity'], name + '.continuity', allow_string=False)
        if 'depends_on' in stage:
            _strings(stage['depends_on'], name + '.depends_on')
        if 'total_units' in stage:
            _number(stage['total_units'], name + '.total_units', integer=True, zero=True)
        if 'outputs' in stage:
            _validate_outputs(stage['outputs'])
            if not isinstance(stage['outputs'], dict):
                _fail(name + '.outputs', 'expected a table of names and paths')
            for key, item in stage['outputs'].items():
                _text(key, name + '.outputs')
                _text(item['path'] if isinstance(item, dict) else item, name + '.outputs.' + key)
        if 'inputs' in stage:
            if not isinstance(stage['inputs'], list):
                _fail(name + '.inputs', 'expected a list of inputs')
            for item in stage['inputs']:
                _table(item, name + '.inputs', set(StageInput.__annotations__))
                for key in StageInput.__annotations__:
                    _text(item.get(key), name + '.inputs.' + key)
    graph = {}
    for stage_id, stage in by_id.items():
        dependencies = set(stage.get('depends_on', []))
        for item in stage.get('inputs', []):
            upstream = by_id.get(item['from_stage'])
            if upstream is None or (upstream.get('outputs') and item['from_output'] not in upstream['outputs']):
                _fail('stages.inputs', 'input must reference an existing stage output')
            if item['from_stage'] not in dependencies:
                _fail('stages.inputs', 'include the input stage in depends_on')
        for dependency in dependencies:
            if dependency not in by_id:
                _fail('stages.depends_on', f'unknown stage {dependency!r}')
        graph[stage_id] = dependencies
    try:
        TopologicalSorter(graph).prepare()
    except CycleError:
        _fail('stages', 'dependencies contain a cycle')


def load_workload_file(path: str | Path = 'nodus.toml') -> dict[str, Any]:
    """Load and validate run arguments without contacting the API."""
    with Path(path).open('rb') as stream:
        try:
            values = tomllib.load(stream)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f'{path}: invalid TOML: {exc}') from exc
    if 'expected_runtime_hours' in values:
        _fail(str(path), UNSUPPORTED['expected_runtime_hours'])
    _table(values, str(path), _FIELDS)
    for key, value in values.items():
        if key in {'budget', 'peak_memory_gb'}:
            _number(value, key)
        elif key == 'command':
            _command(value, key)
        elif key == 'requirements':
            _requirements(value, key)
        elif key == 'continuity':
            _continuity(value, key)
        elif key == 'policy':
            _table(value, key, set(Policy.__annotations__))
            for name in ('data_regions', 'egress_allow', 'secret_refs'):
                if name in value:
                    _strings(value[name], 'policy.' + name)
        elif key == 'connections':
            _live_refs(value)
        elif key == 'sweep_id':
            _group(value)
        elif key == 'data_regions':
            _strings(value, key)
        elif key == 'inputs':
            _validate_assets(None, value)
        elif key == 'source_asset_id':
            _asset(value, key)
        elif key == 'framework':
            if value != 'train_eval':
                _fail(key, 'the supported framework is train_eval')
        elif key == 'outputs':
            _validate_outputs(value)
            if not isinstance(value, dict):
                _fail(key, 'expected a table of output names and paths')
            for name, output_path in value.items():
                _text(name, key)
                _text(output_path['path'] if isinstance(output_path, dict) else output_path, f'outputs.{name}')
        elif key == 'stages':
            _stages(value)
        elif key == 'finish_by':
            try:
                deadline = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
            except (AttributeError, TypeError, ValueError):
                _fail(key, 'expected an RFC3339 timestamp with a timezone')
            if deadline.tzinfo is None:
                _fail(key, 'include the timezone in the deadline')
        elif key in {'compute_class', 'optimization', 'gpu', 'gpu_count', 'gpu_interconnect'}:
            choice = {key: value}
            _requirements(choice, 'requirements')
            values[key] = choice[key]
        else:
            _text(value, key)
    if 'stages' in values:
        if any(key in values for key in ('command', 'image', 'source_asset_id', 'outputs')):
            _fail('stages', 'put image and command inside each stage source')
    elif 'command' not in values:
        _fail('command', 'provide the command to run')
    for key in {'model', 'compute_class', 'peak_memory_gb', 'optimization', 'gpu', 'gpu_count', 'gpu_interconnect'}:
        if key in values and key in values.get('requirements', {}):
            _fail(key, 'set this once, at the top level or in requirements')
    if 'data_regions' in values and 'data_regions' in values.get('policy', {}):
        _fail('data_regions', 'set this once, at the top level or in policy')
    if 'idempotency_key' in values and not all(32 <= ord(c) < 127 for c in values['idempotency_key']):
        _fail('idempotency_key', 'use printable ASCII without line breaks')
    _validate_bucket_regions(values.get("inputs"), values.get("policy") or {"data_regions": values.get("data_regions")})
    return values


def write_workload_file(path: str | Path = 'nodus.toml') -> Path:
    """Create a runnable starter configuration without replacing an existing file."""
    destination = Path(path)
    with destination.open('x', encoding='utf-8') as stream:
        stream.write(_TEMPLATE)
    return destination
