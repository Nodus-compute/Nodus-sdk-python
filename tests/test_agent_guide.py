import hashlib
import json
from pathlib import Path
import re

import httpx
import nodus
from nodus._brief import build_payload
import pytest


@pytest.fixture
def agent_script(tmp_path, monkeypatch):
    guide = Path(__file__).parents[1] / 'docs' / 'guides' / 'agents.md'
    blocks = re.findall(r'^```python\n(.*?)^```', guide.read_text(encoding='utf-8'), re.MULTILINE | re.DOTALL)
    assert len(blocks) == 1
    script = compile(blocks[0], str(guide), 'exec')
    monkeypatch.chdir(tmp_path)
    state_path = tmp_path / 'gpu-run.json'
    calls = []
    settings = {'result': {'sum_of_squares': 385, 'gpu': 'Synthetic GPU'}, 'lose_submission': False, 'status': 'completed'}
    saved_client = nodus.Client

    def reject_real_transport(*args, **kwargs):
        raise AssertionError('Only the synthetic mock transport is allowed')

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', reject_real_transport)

    def handler(request):
        calls.append((request.method, request.url.path))
        assert request.url.host == 'agent-guide.test'
        if request.method == 'POST' and request.url.path == '/v1/workloads':
            assert state_path.exists(), 'Request and key must be saved before submission'
            state = json.loads(state_path.read_text(encoding='utf-8'))
            assert 'id' not in state
            saved_request = state['request']
            assert saved_request['idempotency_key']
            assert request.headers['Idempotency-Key'] == saved_request['idempotency_key']
            assert json.loads(request.content) == build_payload(**{key: value for key, value in saved_request.items() if key != 'idempotency_key'})
            if settings['lose_submission']:
                raise httpx.ReadTimeout('Synthetic lost submission response', request=request)
            return httpx.Response(202, json={'id': 'wl_agent_guide', 'status': 'accepted'})
        if request.method == 'GET':
            state = json.loads(state_path.read_text(encoding='utf-8'))
            assert state['id'] == 'wl_agent_guide', 'Accepted ID must be saved before observation'
            if request.url.path == '/v1/workloads/wl_agent_guide':
                return httpx.Response(200, json={'id': state['id'], 'status': settings['status']})
            if request.url.path == '/v1/workloads/wl_agent_guide/logs':
                return httpx.Response(200, text='Synthetic GPU result log')
            if request.url.path == '/v1/workloads/wl_agent_guide/outputs/result':
                raw = json.dumps(settings['result']).encode()
                return httpx.Response(200, content=raw, headers={'X-Nodus-SHA256': hashlib.sha256(raw).hexdigest()})
        raise AssertionError(f'Unexpected synthetic request {request.method} {request.url.path}')

    def client_factory():
        client = saved_client(api_key='synthetic-key', base_url='https://agent-guide.test', max_retries=0)
        client._http.close()
        client._http = httpx.Client(base_url='https://agent-guide.test', transport=httpx.MockTransport(handler))
        return client

    monkeypatch.setattr(nodus, 'Client', client_factory)

    def run():
        exec(script, {'__name__': '__main__'})

    return run, calls, settings, state_path


def test_agent_guide_persists_request_then_resumes_without_submission(agent_script, tmp_path, capsys):
    run, calls, settings, state_path = agent_script
    run()
    state = json.loads(state_path.read_text(encoding='utf-8'))
    assert state['id'] == 'wl_agent_guide'
    assert 'budget' not in state['request']
    assert json.loads((tmp_path / 'result-wl_agent_guide.json').read_text()) == settings['result']
    assert 'Synthetic GPU result log' in capsys.readouterr().out
    assert calls.count(('POST', '/v1/workloads')) == 1
    calls.clear()
    run()
    assert calls[0] == ('GET', '/v1/workloads/wl_agent_guide')
    assert all(method == 'GET' for method, path in calls)
    assert json.loads(state_path.read_text(encoding='utf-8')) == state


def test_agent_guide_retries_uncertain_submission_with_original_saved_request(agent_script):
    run, calls, settings, state_path = agent_script
    settings['lose_submission'] = True
    with pytest.raises(nodus.APITimeoutError):
        run()
    original = json.loads(state_path.read_text(encoding='utf-8'))
    assert 'id' not in original
    assert calls == [('POST', '/v1/workloads')]
    settings['lose_submission'] = False
    run()
    recovered = json.loads(state_path.read_text(encoding='utf-8'))
    assert recovered['request'] == original['request']
    assert recovered['id'] == 'wl_agent_guide'


@pytest.mark.parametrize('result', [{'sum_of_squares': 384, 'gpu': 'Synthetic GPU'}, {'sum_of_squares': 385, 'gpu': ''}])
def test_agent_guide_rejects_incorrect_downloaded_json(agent_script, result):
    run, calls, settings, state_path = agent_script
    settings['result'] = result
    with pytest.raises(RuntimeError, match='Downloaded result did not match'):
        run()
    assert ('GET', '/v1/workloads/wl_agent_guide/outputs/result') in calls
    assert json.loads(state_path.read_text())['id'] == 'wl_agent_guide'


def test_agent_guide_does_not_download_after_failed_workload(agent_script):
    run, calls, settings, state_path = agent_script
    settings['status'] = 'failed'
    with pytest.raises(RuntimeError, match='ended: failed'):
        run()
    assert ('GET', '/v1/workloads/wl_agent_guide/outputs/result') not in calls
    assert json.loads(state_path.read_text())['id'] == 'wl_agent_guide'
