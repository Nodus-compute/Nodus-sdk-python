import json
from pathlib import Path


def test_resource_metadata_preserves_gpu_count_and_optional_accelerator_fields():
    spec = json.loads((Path(__file__).parents[1] / 'openapi/openapi.yaml').read_text())
    schema = spec['components']['schemas']['ResourceAttributes']
    fields = schema['properties']
    assert fields['gpu_count']['type'] == 'integer'
    assert fields['accelerator_kind']['type'] == 'string'
    assert fields['accelerator_count']['type'] == 'integer'
    assert fields['accelerator_count']['minimum'] == 0
    assert 'accelerator_kind' not in schema.get('required', [])
    assert 'accelerator_count' not in schema.get('required', [])
