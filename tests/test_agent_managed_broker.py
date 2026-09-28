"""Fixed broker integration keeps replay identity and uncertainty explicit."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json

import httpx
import pytest

import nodus
from nodus import _agent
from nodus.agent_runtime import run


def encoded(value):
    return base64.b64encode(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).decode()


def terminal(receipt, value, state='completed'):
    raw = json.dumps(value, separators=(',', ':')).encode()
    return {**receipt, 'state': state, 'result': base64.b64encode(raw).decode(), 'result_sha256': hashlib.sha256(raw).hexdigest()}


@pytest.fixture
def broker(monkeypatch, tmp_path):
    state = {'calls': [], 'receipts': {}, 'intents': {}, 'lost': 0, 'checkpoint': False,
             'model_state': 'completed', 'transform': lambda action, value: value, 'unavailable': False}
    rpc_class = _agent._RPC
    monkeypatch.setenv('NODUS_AGENT_SOCKET', str(tmp_path / 'private.sock'))
    def handle(request):
        action, body = request.url.path[1:], json.loads(request.content)
        state['calls'].append((action, copy.deepcopy(body)))
        if action in ('session', 'renew'):
            value = {'session_token': 'private', 'epoch': 1,
                     'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
                     'run': {'run_id': 'parent', 'name': 'main', 'version': '1', 'status': 'active', 'input': encoded(None)}}
            if state['checkpoint']:
                value.update(recovery_policy='checkpoint-v1', checkpoint_id='baseline')
        elif action == 'checkpoint_begin':
            value = {'checkpoint_id': 'capture-' + body['operation_id'], 'status': 'ready'}
        elif action in ('broker_invoke', 'broker_status'):
            if state['unavailable']:
                return httpx.Response(400, json={'code': 'agent_bridge_unavailable'})
            assert body['protocol_version'] == 1
            key = body['invocation_key']
            if action == 'broker_status':
                if key not in state['receipts']:
                    return httpx.Response(404, json={'code': 'not_found'})
                value = state['receipts'][key]
            else:
                intent = {k: body[k] for k in ('kind', 'profile_id', 'prompt', 'max_output_tokens', 'blob') if k in body}
                if key in state['intents'] and state['intents'][key] != intent:
                    return httpx.Response(409, json={'code': 'idempotency_conflict'})
                if key not in state['receipts']:
                    state['intents'][key] = intent
                    value = {'protocol_version': 1, 'invocation_id': 'bi_' + key,
                             'invocation_key': key, 'kind': body['kind'], 'state': state['model_state']}
                    if body['kind'] == 'nodus-completion-v1':
                        if state['model_state'] == 'completed':
                            value = terminal(value, {'text': 'verified', 'finish_reason': 'stop'})
                        elif state['model_state'] == 'rejected':
                            value = terminal(value, {'error': {'code': 'model_capacity_exceeded', 'retryable': True}}, 'rejected')
                    else:
                        blob = body['blob']
                        chunk = {'reference': blob['reference'], 'status': 'committed', 'offset': blob['offset'],
                                 'data': base64.b64encode(b'seventeen bytes!!').decode(), 'eof': True}
                        if 'child_run_id' in blob:
                            chunk['child_run_id'] = blob['child_run_id']
                        value = terminal(value, chunk)
                    state['receipts'][key] = value
                value = state['receipts'][key]
                if state['lost']:
                    state['lost'] -= 1
                    raise httpx.ReadError('lost accepted reply', request=request)
        elif action == 'finish':
            value = {'status': 'completed', 'result': body['result']}
        else:
            pytest.fail('unexpected RPC ' + action)
        return httpx.Response(200, json=state['transform'](action, copy.deepcopy(value)))
    def rpc():
        value = rpc_class()
        value.client.close()
        value.client = httpx.Client(base_url='http://private', transport=httpx.MockTransport(handle), trust_env=False)
        return value
    monkeypatch.setattr(_agent, '_RPC', rpc)
    def drive(fn):
        def main(_):
            return fn()
        return run(main, run_id='parent', version='1')
    return state, drive


def complete(**kwargs):
    return nodus.agent.complete_model('question', profile_id='profile', invocation_key='stable', max_output_tokens=8, **kwargs)


def test_broker_capture_precedes_admission_and_lost_reply_keeps_exact_identity(broker):
    state, drive = broker
    state['checkpoint'] = True
    state['lost'] = 3
    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(complete)
    assert len(state['receipts']) == 1
    assert drive(complete) == {'text': 'verified', 'finish_reason': 'stop'}
    actions = [a for a, _ in state['calls']]
    assert actions.index('broker_status') < actions.index('checkpoint_begin') < actions.index('broker_invoke')
    invokes = [b for a, b in state['calls'] if a == 'broker_invoke']
    assert invokes[0] == invokes[1] == invokes[2]
    assert {b['invocation_key'] for b in invokes} == {'stable'}
    assert {b['checkpoint_id'] for b in invokes} == {'capture-stable'}
    captures = [b for a, b in state['calls'] if a == 'checkpoint_begin' and b['operation'] == 'broker']
    assert len(captures) == 2
    assert all(b['operation'] == 'broker' and b['operation_id'] == 'stable' for b in captures)
    assert all('api_key' not in b and 'url' not in b for _, b in state['calls'])


def test_completed_status_cannot_bypass_immutable_intent_check(broker):
    _, drive = broker
    drive(complete)
    with pytest.raises(nodus.StepDefinitionConflict):
        drive(lambda: nodus.agent.complete_model('changed', profile_id='profile', invocation_key='stable', max_output_tokens=8))


def test_old_guest_refuses_before_checkpoint_or_broker_admission(broker):
    state, drive = broker
    state.update(checkpoint=True, unavailable=True)
    with pytest.raises(nodus.AgentBrokerUnavailable):
        drive(complete)
    assert [a for a, _ in state['calls']] == ['session', 'broker_status']


def test_unknown_never_reissues_and_local_refusal_is_visible(broker):
    state, drive = broker
    state['model_state'] = 'unknown'
    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(complete)
    assert len([a for a, _ in state['calls'] if a == 'broker_invoke']) == 1
    assert not any(a == 'finish' for a, _ in state['calls'])
    state['receipts'].clear()
    state['model_state'] = 'rejected'
    with pytest.raises(nodus.BrokerRefused) as error:
        drive(complete)
    assert error.value.code == 'model_capacity_exceeded' and error.value.retryable


def test_wait_poll_preserves_key_and_verifies_complete_result(broker, monkeypatch):
    state, drive = broker
    state['model_state'] = 'waiting'
    def transform(action, value):
        if action == 'broker_status' and value.get('state') == 'waiting':
            value = terminal(value, {'text': 'after quota', 'finish_reason': 'length'})
        return value
    state['transform'] = transform
    assert drive(complete) == {'text': 'after quota', 'finish_reason': 'length'}
    assert len([a for a, _ in state['calls'] if a == 'broker_invoke']) == 1
    assert {b['invocation_key'] for a, b in state['calls'] if a == 'broker_status'} == {'stable'}


@pytest.mark.parametrize('change', [
    lambda r: {**r, 'invocation_id': 'different'},
    lambda r: {**r, 'protocol_version': True},
    lambda r: {**r, 'kind': 'nodus-blob-read-v1'},
    lambda r: {**r, 'result_sha256': '0' * 64},
    lambda r: terminal(r, {'text': 'bad', 'finish_reason': None}),
])
def test_invalid_broker_result_never_finishes_run(broker, change):
    state, drive = broker
    # A changed invocation id is detected against the initial durable read.
    drive(complete)
    state['calls'].clear()
    state['transform'] = lambda action, value: change(value) if action == 'broker_invoke' else value
    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(complete)
    assert not any(a == 'finish' for a, _ in state['calls'])


def test_blob_final_chunk_preserves_exact_owner_reference(broker):
    state, drive = broker
    reference = {'id': 'bl_' + 'a' * 64, 'sha256': 'a' * 64, 'bytes': 262144 + 17}
    assert drive(lambda: len(nodus.agent.read_blob_chunk(reference, invocation_key='tail', offset=262144, child_run_id='direct'))) == 17
    body = next(b for a, b in state['calls'] if a == 'broker_invoke')
    assert body['blob'] == {'reference': reference, 'offset': 262144, 'child_run_id': 'direct'}
    state['transform'] = lambda action, value: terminal(value, {'reference': reference, 'status': 'committed', 'offset': 262144, 'data': base64.b64encode(b'seventeen bytes!!').decode(), 'eof': True, 'child_run_id': 'foreign'}) if action == 'broker_invoke' else value
    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(lambda: nodus.agent.read_blob_chunk(reference, invocation_key='tail', offset=262144, child_run_id='direct'))


def test_failed_checkpoint_cannot_accept_broker_effect(broker):
    state, drive = broker
    state['checkpoint'] = True
    state['transform'] = lambda action, value: {**value, 'status': 'failed', 'failure_code': 'save_failed'} if action == 'checkpoint_begin' else value
    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(complete)
    assert not state['receipts']
    assert not any(a == 'broker_invoke' for a, _ in state['calls'])


def test_broker_result_above_ordinary_step_limit_is_still_bounded(broker):
    state, drive = broker
    state['transform'] = lambda action, value: terminal(value, {'text': 'x' * 300000, 'finish_reason': 'length'}) if action == 'broker_invoke' else value
    assert drive(lambda: len(complete()['text'])) == 300000
    state['calls'].clear()
    state['transform'] = lambda action, value: terminal(value, {'text': 'x' * (512 << 10), 'finish_reason': 'length'}) if action == 'broker_invoke' else value
    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(lambda: len(complete()['text']))
    assert not any(a == 'finish' for a, _ in state['calls'])


def test_broker_validation_never_consumes_an_identity(broker):
    state, drive = broker
    for bad in (True, 0, 8193):
        with pytest.raises(nodus.ValidationError):
            drive(lambda: nodus.agent.complete_model('question', profile_id='profile', invocation_key='stable', max_output_tokens=bad))
    with pytest.raises(nodus.ValidationError):
        drive(lambda: complete(timeout=float('inf')))
    assert not any(a.startswith('broker_') for a, _ in state['calls'])


@pytest.mark.parametrize('code', [[], {}, None, True, 1])
def test_malformed_refusal_cannot_finish_after_application_catch(broker, code):
    state, drive = broker
    state['model_state'] = 'rejected'
    state['transform'] = lambda action, value: terminal(value, {'error': {'code': code, 'retryable': True}}, 'rejected') if action == 'broker_invoke' else value
    caught = []
    def driver():
        try:
            complete()
        except Exception as error:
            caught.append(type(error).__name__)
        return {'incorrectly_finished': True}
    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(driver)
    assert caught == ['StepOutcomeUnknown']
    assert not any(action == 'finish' for action, _ in state['calls'])
