"""Owned assistant admission preserves explicit spending and model choices."""

import json

import httpx
import nodus
import pytest

from test_managed_experience import client_for


def test_owned_assistant_deployment_uses_accepted_model_without_project_upload():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(201, json={'id': 'ag_assistant', 'status': 'active', 'model': 'nodus:claude-test',
                                       'assistant_template': 'nodus:claude-assistant-v1', 'model_max_output_tokens': 512})
    with client_for(handle) as client:
        assistant = client.agents.create(name='reports', budget=20,
            assistant_template='nodus:claude-assistant-v1', model='nodus:claude-test',
            model_max_output_tokens=512, idempotency_key='reports-deployment')
    assert assistant.id == 'ag_assistant'
    assert (assistant.model, assistant.assistant_template, assistant.model_max_output_tokens) == ('nodus:claude-test', 'nodus:claude-assistant-v1', 512)
    assert len(requests) == 1 and requests[0].url.path == '/v1/agents'
    body = json.loads(requests[0].content)
    assert body == {'name': 'reports', 'budget_usd': 20, 'entrypoint': 'nodus.managed_assistant:main',
                    'assistant_template': 'nodus:claude-assistant-v1', 'model': 'nodus:claude-test',
                    'model_max_output_tokens': 512}
    assert requests[0].headers['Idempotency-Key'] == 'reports-deployment'


@pytest.mark.parametrize('options', [
    {'model': None}, {'model': 'private-provider-model'}, {'model_max_output_tokens': True},
    {'model_max_output_tokens': 4097}, {'project': '.'}, {'entrypoint': 'customer:main'},
    {'secrets': {'MODEL_KEY': 'secret'}}, {'network_permissions': ['model-api']},
    {'policy': {}}, {'setup': ['python', 'customer.py']},
])
def test_owned_assistant_invalid_definition_is_rejected_before_any_http(options):
    values = {'name': 'reports', 'budget': 20, 'assistant_template': 'nodus:claude-assistant-v1',
              'model': 'nodus:claude-test', **options}
    with client_for(lambda request: pytest.fail('invalid definition reached HTTP')) as client:
        with pytest.raises(nodus.ValidationError):
            client.agents.create(**values)
