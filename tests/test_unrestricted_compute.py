import json

import httpx
import nodus
import pytest


@pytest.mark.parametrize('operation', ['sandbox', 'agent', 'benchmark'])
def test_compute_submission_needs_no_budget(operation):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(202, json={'id': {'sandbox': 'sb_one', 'agent': 'ag_one', 'benchmark': 'bm_one'}[operation], 'state': 'creating', 'status': 'active'})
    with nodus.Client(api_key='test', base_url='https://nodus.invalid') as client:
        client._http.close()
        client._http = httpx.Client(base_url='https://nodus.invalid', transport=httpx.MockTransport(handle))
        if operation == 'sandbox':
            client.sandboxes.create(idempotency_key='same-request')
        elif operation == 'agent':
            client.agents.create(name='worker', entrypoint='agent:main', idempotency_key='same-request')
        else:
            client.benchmark(workload={'source': {'command': ['true']}}, gpu_families=['A100'], batch_sizes=[1], regions=['us'], repetitions=1, idempotency_key='same-request')
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert 'budget_usd' not in body
    assert 'max_cost_usd' not in body.get('outcome', {})
    assert requests[0].headers['Idempotency-Key'] == 'same-request'


@pytest.mark.parametrize('budget', [0, 5])
def test_legacy_budget_only_constructor_preserves_managed_template(budget, monkeypatch):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(202, json={'id': 'sb_legacy', 'state': 'creating'})
    with nodus.Client(api_key='test', base_url='https://nodus.invalid') as client:
        client._http.close()
        client._http = httpx.Client(base_url='https://nodus.invalid', transport=httpx.MockTransport(handle))
        monkeypatch.setattr(nodus, 'Client', lambda: client)
        sandbox = nodus.Sandbox(budget=budget, idempotency_key='legacy-request')
        assert sandbox.id == 'sb_legacy'
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body['template'] == 'nodus:agent-tools-v1'
    assert body['outcome']['max_cost_usd'] == budget
    assert requests[0].headers['Idempotency-Key'] == 'legacy-request'
