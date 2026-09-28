from unittest.mock import AsyncMock, Mock

import pytest

import nodus
from nodus._workload_file import load_workload_file, write_workload_file


def test_template_is_runnable_and_never_overwritten(tmp_path):
    path = tmp_path / 'nodus.toml'
    assert write_workload_file(path) == path
    values = load_workload_file(path)
    assert 'budget' not in values
    assert values['command'][0] == 'python'
    with pytest.raises(FileExistsError):
        write_workload_file(path)


@pytest.mark.parametrize('stage', [False, True])
@pytest.mark.parametrize('field', ['disk_gb', 'vcpus'])
@pytest.mark.parametrize('value', [0, 32.5])
def test_resource_requirements_preserve_numbers(tmp_path, stage, field, value):
    prefix = ('[[stages]]\nid="train"\nsource={command=["python", "train.py"]}\n'
              '[stages.requirements]\n' if stage else
              'command=["python", "train.py"]\n[requirements]\n')
    path = tmp_path / 'nodus.toml'
    path.write_text(prefix + f'{field}={value}\noptimization=""\n')
    values = load_workload_file(path)
    req = values['stages'][0]['requirements'] if stage else values['requirements']
    assert req[field] == value
    assert req['optimization'] == ''


@pytest.mark.parametrize('field', ['disk_gb', 'vcpus'])
@pytest.mark.parametrize('value', ['-1', 'nan', 'inf', 'true', '"4"'])
def test_resource_requirements_reject_invalid_numbers(tmp_path, field, value):
    path = tmp_path / 'nodus.toml'
    path.write_text('command=["python", "train.py"]\n'
                    f'[requirements]\n{field}={value}\n')
    with pytest.raises(ValueError, match=field):
        load_workload_file(path)


@pytest.mark.parametrize('prefix', [
    'command=["python", "train.py"]\n',
    'command=["python", "train.py"]\n[requirements]\n',
    '[[stages]]\nid="train"\nsource={command=["python", "train.py"]}\n[stages.requirements]\n',
], ids=['top-level', 'requirements', 'stage'])
@pytest.mark.parametrize('count', [1, 2, 4, 8])
def test_workload_file_preserves_exact_gpu_count(tmp_path, prefix, count):
    path = tmp_path / 'nodus.toml'
    path.write_text(prefix + f'gpu_count={count}\n')
    values = load_workload_file(path)
    if 'stages' in values:
        values = values['stages'][0]
    requirements = values.get('requirements', values)
    assert requirements['gpu_count'] == count
    assert type(requirements['gpu_count']) is int


@pytest.mark.parametrize('prefix', [
    'command=["python", "train.py"]\n',
    'command=["python", "train.py"]\n[requirements]\n',
    '[[stages]]\nid="train"\nsource={command=["python", "train.py"]}\n[stages.requirements]\n',
], ids=['top-level', 'requirements', 'stage'])
@pytest.mark.parametrize('count', ['0', '-1', '3', '16', 'true', '8.0', '"8"'])
def test_workload_file_rejects_invalid_gpu_count(tmp_path, prefix, count):
    path = tmp_path / 'nodus.toml'
    path.write_text(prefix + f'gpu_count={count}\n')
    with pytest.raises(ValueError, match='gpu_count must be 1, 2, 4, or 8'):
        load_workload_file(path)


@pytest.mark.parametrize('body', [
    'command = ["python"]\nbudget = 0',
    'command = ["python"]\nbudget = nan',
    'command = ["python"]\nbudget = true',
    'command = ["python"]\nbudget = "5"',
    'command = [1]\nbudget = 5',
    'budget = 5',
    'command = []\nbudget = 5',
    'command = ["python"]\nextra = {secret = 1}',
    'command = ["python"]\nrequirements = {peak_memroy_gb = 20}',
    'command = ["python"]\npolicy = {data_regions = "us"}',
    'command = ["python"]\ncontinuity = {resume_on_interruption = "yes"}',
    'command = ["python"]\npeak_memory_gb = -1',
    'command = ["python"]\nfinish_by = "tomorrow"',
    'stages = [{id = "a", source = {command = ["python"]}}, {id = "a", source = {command = ["python"]}}]',
    'stages = [{id = "a", depends_on = ["missing"], source = {command = ["python"]}}]',
])
def test_invalid_file_rejected_without_submission(tmp_path, body):
    path = tmp_path / 'bad.toml'
    path.write_text(body)
    client = nodus.Client(api_key='test')
    client.run = Mock()
    with pytest.raises(ValueError):
        client.run_file(path)
    client.run.assert_not_called()
    client.close()


def test_advanced_file_preserves_fields(tmp_path):
    path = tmp_path / 'advanced.toml'
    path.write_text('''budget = 12
idempotency_key = "training-42"
finish_by = 2030-01-01T00:00:00Z
[requirements]
dataset_bytes = 1024
peak_memory_gb = 24
[policy]
data_regions = ["us-east"]
[continuity]
mode = "restartable"
resume_on_interruption = true
[[stages]]
id = "prepare"
total_units = 2
source = {image = "python:3.11-slim", command = ["python", "prepare.py"]}
outputs = {dataset = "data.json"}
[[stages]]
id = "train"
depends_on = ["prepare"]
source = {image = "training:latest", command = ["python", "train.py"]}
inputs = [{name = "data", from_stage = "prepare", from_output = "dataset"}]
''')
    values = load_workload_file(path)
    assert values['requirements']['dataset_bytes'] == 1024
    assert values['stages'][1]['inputs'][0]['from_output'] == 'dataset'
    with nodus.Client(api_key='test') as client:
        client.run = Mock(return_value='submitted')
        assert client.run_file(path) == 'submitted'
        client.run.assert_called_once_with(**values)


@pytest.mark.asyncio
async def test_async_run_file_submits_once(tmp_path):
    path = write_workload_file(tmp_path / 'nodus.toml')
    async with nodus.AsyncClient(api_key='test') as client:
        client.run = AsyncMock(return_value='submitted')
        assert await client.run_file(path) == 'submitted'
        client.run.assert_awaited_once_with(**load_workload_file(path))


def test_asset_inputs_and_outputs(tmp_path):
    path = tmp_path / 'assets.toml'
    path.write_text('''command = ["python", "train.py"]
source_asset_id = "asset_code"
inputs = [{name = "data", asset_id = "asset_data"}]
outputs = {weights = "weights.pt"}
budget = 5
''')
    values = load_workload_file(path)
    assert values['source_asset_id'] == 'asset_code'
    assert values['inputs'] == [{'name': 'data', 'asset_id': 'asset_data'}]
    assert values['outputs'] == {'weights': 'weights.pt'}


@pytest.mark.parametrize('extra', [
    'inputs = [{name = "data", uri = "https://example.com/data"}]',
    'inputs = [{name = "data", asset_id = 1}]',
    'inputs = [{name = "data", asset_id = "a"}, {name = "data", asset_id = "b"}]',
    'outputs = {weights = 1}',
    'source_asset_id = 1',
])
def test_invalid_asset_fields(tmp_path, extra):
    path = tmp_path / 'assets.toml'
    path.write_text('command = ["python", "train.py"]\nbudget = 5\n' + extra)
    with pytest.raises(ValueError):
        load_workload_file(path)


@pytest.mark.parametrize('body', [
    'command = ["python"]\nbudget = -inf',
    'command = ["python"]\nidempotency_key = "bad\\nkey"',
    'command = ["python"]\npeak_memory_gb = 10\nrequirements = {peak_memory_gb = 20}',
    'command = ["python"]\ndata_regions = ["us"]\npolicy = {data_regions = ["eu"]}',
    'stages = [{id = "a", depends_on = ["b"], source = {command = ["python"]}}, {id = "b", depends_on = ["a"], source = {command = ["python"]}}]',
    'stages = [{id = "a", source = {command = ["python"]}, inputs = [{name = "data", from_stage = "x", from_output = "missing"}]}]',
])
def test_conflicts_and_cycles_are_rejected(tmp_path, body):
    path = tmp_path / 'bad.toml'
    path.write_text(body)
    with pytest.raises(ValueError):
        load_workload_file(path)


def test_stage_asset_source_and_zero_units(tmp_path):
    path = tmp_path / 'stages.toml'
    path.write_text('''budget = 5
[[stages]]
id = "train"
total_units = 0
source = {asset_id = "asset_code", command = ["python", "train.py"]}
''')
    values = load_workload_file(path)
    assert values['stages'][0]['source']['asset_id'] == 'asset_code'
    assert values['stages'][0]['total_units'] == 0


def test_missing_file_is_actionable(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_workload_file(tmp_path / 'missing.toml')


def test_invalid_toml_is_actionable(tmp_path):
    path = tmp_path / 'invalid.toml'
    path.write_text('command = [')
    with pytest.raises(ValueError, match='invalid TOML'):
        load_workload_file(path)


@pytest.mark.parametrize('extra', [
    'inputs = [{name = "bad-name", asset_id = "asset_data"}]',
    'source_asset_id = "not-an-asset"',
    'framework = "unknown"',
    'inputs = [' + ','.join('{name = "data' + str(i) + '", asset_id = "asset_data"}' for i in range(9)) + ']',
])
def test_server_asset_constraints_are_checked_locally(tmp_path, extra):
    path = tmp_path / 'invalid.toml'
    path.write_text('command = ["python", "train.py"]\nbudget = 5\n' + extra)
    with pytest.raises(ValueError):
        load_workload_file(path)


def test_cached_input_flag_in_workload_file(tmp_path):
    path = tmp_path / 'cached.toml'
    path.write_text('command = ["python", "train.py"]\nbudget = 5\ninputs = [{name = "weights", asset_id = "asset_weights", cache = true}]\n')
    assert load_workload_file(path)['inputs'][0]['cache'] is True
    path.write_text(path.read_text().replace('cache = true', 'cache = "true"'))
    with pytest.raises(ValueError):
        load_workload_file(path)
