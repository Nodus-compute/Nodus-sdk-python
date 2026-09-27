"""Peer controls preserve customer messages through driver reconstruction."""
import base64
import copy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import hashlib
import json

import httpx
import pytest

import nodus
from nodus import _agent
from nodus.agent_runtime import run


def encoded(value):
    return base64.b64encode(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).decode()


def message(key='update:1', payload=None):
    wire = encoded({'finding': [42]} if payload is None else payload)
    return {'message_id': 'pm-' + key, 'group_id': 'group-1', 'from_run_id': 'sender',
            'to_run_id': 'receiver', 'from_task_key': 'inspect', 'to_task_key': 'verify',
            'message_key': key, 'payload_hash': hashlib.sha256(base64.b64decode(wire)).hexdigest(),
            'payload': wire}


@pytest.fixture
def peers(monkeypatch, tmp_path):
    state = {'calls': [], 'sends': {}, 'waits': {}, 'lost': {}, 'checkpoint': False,
             'capability': 1, 'transform': lambda action, response: response}
    rpc_class = _agent._RPC
    monkeypatch.setenv('NODUS_AGENT_SOCKET', str(tmp_path / 'private.sock'))

    def handler(request):
        action, body = request.url.path[1:], json.loads(request.content)
        state['calls'].append((action, copy.deepcopy(body)))
        if action in ('session', 'renew'):
            task = 'inspect' if body['run_id'] == 'sender' else 'verify'
            response = {'session_token': 'private-session', 'epoch': 7,
                        'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
                        'run': {'run_id': body['run_id'], 'name': 'main', 'version': '1',
                                'status': 'active', 'input': encoded(None)}}
            if state['capability'] is not None:
                response.update(peer_messages_version=state['capability'],
                                peer_group_id='group-1', peer_task_key=task)
            if state['checkpoint']:
                response.update(recovery_policy='checkpoint-v1', checkpoint_id='cp-baseline')
        elif action == 'checkpoint_begin':
            response = {'checkpoint_id': 'cp-' + body['operation_id'], 'status': 'ready'}
        elif action == 'peer_send':
            identity = body['run_id'], body['message_key']
            content = body['to_task_key'], body['payload']
            replay = identity in state['sends']
            if replay and state['sends'][identity][0] != content:
                return httpx.Response(409, json={'code': 'idempotency_conflict'})
            if not replay:
                receipt = message(body['message_key'])
                receipt.update(to_task_key=body['to_task_key'],
                               payload_hash=hashlib.sha256(base64.b64decode(body['payload'])).hexdigest())
                del receipt['payload']
                state['sends'][identity] = content, receipt
            response = {'decision': 'replay' if replay else 'accepted', **state['sends'][identity][1]}
        elif action == 'peer_receive':
            identity = body['run_id'], body['wait_id']
            if identity not in state['waits']:
                state['waits'][identity] = body['limit'], {'decision': 'waiting', 'wait_id': body['wait_id']}
            limit, response = state['waits'][identity]
            if limit != body['limit']:
                return httpx.Response(409, json={'code': 'idempotency_conflict'})
        elif action == 'finish':
            response = {'status': 'completed', 'result': body['result']}
        else:
            pytest.fail('unexpected private action: ' + action)
        if state['lost'].get(action, 0):
            state['lost'][action] -= 1
            raise httpx.ReadError('lost private acknowledgement', request=request)
        response = state['transform'](action, copy.deepcopy(response))
        return response if isinstance(response, httpx.Response) else httpx.Response(200, json=response)

    def rpc():
        channel = rpc_class()
        channel.client.close()
        channel.client = httpx.Client(base_url='http://agent.local', transport=httpx.MockTransport(handler),
                                     follow_redirects=False, trust_env=False)
        return channel

    monkeypatch.setattr(_agent, '_RPC', rpc)

    def drive(function, run_id='sender'):
        def main(_):
            return function()
        return run(main, run_id=run_id, version='1')

    return state, drive


def test_send_lost_reply_replays_one_message_and_original_receipt(peers):
    state, drive = peers
    state.update(checkpoint=True)
    state['lost']['peer_send'] = 3
    receipts = []

    def send():
        receipt = nodus.agent.send_message('verify', {'finding': [42]}, message_key='update:1')
        receipts.append(receipt)
        return receipt.to_dict()

    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(send)
    assert len(state['sends']) == 1
    session_request = next(body for action, body in state['calls'] if action == 'session')
    assert type(session_request['peer_messages_version']) is int and session_request['peer_messages_version'] == 1
    original = drive(send)
    assert drive(send) == original
    assert original == {key: value for key, value in message().items() if key != 'payload'}
    requests = [body for action, body in state['calls'] if action == 'peer_send']
    assert requests[0] == requests[1] == requests[2]
    assert all(body['checkpoint_id'] == 'cp-update:1' for body in requests)
    assert all(body['run_id'] == 'sender' and body['session_token'] == 'private-session'
               and body['epoch'] == 7 for body in requests)
    checkpoint = next(body for action, body in state['calls'] if action == 'checkpoint_begin')
    assert checkpoint['operation'] == 'peer_send' and checkpoint['operation_id'] == 'update:1'
    with pytest.raises(FrozenInstanceError):
        receipts[0].message_key = 'changed'


def test_receive_yields_then_replays_the_frozen_page_with_defensive_payloads(peers):
    state, drive = peers
    state['checkpoint'] = True
    pages = []

    def receive():
        page = nodus.agent.receive_messages(wait_id='updates:1', limit=2)
        pages.append(page)
        return [item.payload() for item in page]

    assert drive(receive, 'receiver') is None
    assert not any(action == 'finish' for action, _ in state['calls'])
    request = next(body for action, body in state['calls'] if action == 'peer_receive')
    assert request['checkpoint_id'] == 'cp-updates:1'
    checkpoint = next(body for action, body in state['calls'] if action == 'checkpoint_begin')
    assert checkpoint['operation'] == 'peer_receive' and checkpoint['operation_id'] == 'updates:1'
    state['waits'][('receiver', 'updates:1')] = 2, {
        'decision': 'replay', 'wait_id': 'updates:1', 'messages': [message()]}
    state['lost']['peer_receive'] = 3
    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(receive, 'receiver')
    assert drive(receive, 'receiver') == [{'finding': [42]}]
    changed = pages[0][0].payload()
    changed['finding'].append(0)
    assert drive(receive, 'receiver') == [{'finding': [42]}]
    assert pages[0] == pages[1] and isinstance(pages[0], tuple)
    with pytest.raises(FrozenInstanceError):
        pages[0][0].message_key = 'changed'


@pytest.mark.parametrize('capability', [None, 0, 2, True, '1'])
@pytest.mark.parametrize('operation', ['send', 'receive'])
def test_unnegotiated_peer_support_refuses_before_checkpoint_or_effect(peers, capability, operation):
    state, drive = peers
    state.update(capability=capability, checkpoint=True)
    with pytest.raises(nodus.AgentMessagesUnavailable):
        drive(lambda: nodus.agent.send_message('verify', None, message_key='one') if operation == 'send'
              else nodus.agent.receive_messages(wait_id='one'))
    assert [action for action, _ in state['calls']] == ['session']


@pytest.mark.parametrize('field,value', [('peer_group_id', None), ('peer_group_id', 'foreign group'),
                                        ('peer_task_key', None), ('peer_task_key', 3)])
def test_incomplete_peer_scope_cannot_authorize_a_message(peers, field, value):
    state, drive = peers
    state['transform'] = lambda action, response: {**response, field: value} if action == 'session' else response
    with pytest.raises(nodus.AgentMessagesUnavailable):
        drive(lambda: nodus.agent.send_message('verify', None, message_key='one'))
    assert [action for action, _ in state['calls']] == ['session']


@pytest.mark.parametrize('change', ['group_id', 'from_run_id', 'from_task_key', 'to_task_key',
                                   'message_key', 'payload_hash', 'message_id', 'decision', 'to_run_id'])
def test_send_refuses_corrupt_receipts_and_fences_caught_errors(peers, change):
    state, drive = peers

    def corrupt(action, response):
        if action == 'peer_send':
            response[change] = 'wrong' if change not in ('message_id', 'to_run_id') else ''
        return response

    state['transform'] = corrupt

    def send():
        try:
            nodus.agent.send_message('verify', {'finding': [42]}, message_key='update:1')
        except nodus.StepOutcomeUnknown:
            pass
        return 'must not finish'

    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(send)
    assert not any(action == 'finish' for action, _ in state['calls'])


@pytest.mark.parametrize('change', ['group', 'recipient-run', 'recipient-task', 'payload', 'duplicate-id',
                                   'duplicate-key', 'empty', 'oversized-page', 'wait-id', 'waiting-with-messages'])
def test_receive_never_exposes_corrupt_or_cross_scope_messages(peers, change):
    state, drive = peers
    row = message()
    response = {'decision': 'replay', 'wait_id': 'page', 'messages': [row]}
    if change in ('group', 'recipient-run', 'recipient-task'):
        row[{'group': 'group_id', 'recipient-run': 'to_run_id', 'recipient-task': 'to_task_key'}[change]] = 'foreign'
    elif change == 'payload':
        row['payload'] = encoded({'finding': [0]})
    elif change in ('duplicate-id', 'duplicate-key'):
        other = copy.deepcopy(row)
        other['message_key' if change == 'duplicate-id' else 'message_id'] = 'other'
        response['messages'].append(other)
    elif change == 'empty':
        response['messages'] = []
    elif change == 'oversized-page':
        response['messages'] = [message('key:' + str(index)) for index in range(3)]
    elif change == 'wait-id':
        response['wait_id'] = 'other'
    else:
        response['decision'] = 'waiting'
    state['waits'][('receiver', 'page')] = 2, response
    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(lambda: nodus.agent.receive_messages(wait_id='page', limit=2), 'receiver')
    assert not any(action == 'finish' for action, _ in state['calls'])


@pytest.mark.parametrize('change', ['target', 'payload', 'limit'])
def test_reusing_a_key_for_different_content_conflicts(peers, change):
    state, drive = peers
    if change == 'limit':
        assert drive(lambda: nodus.agent.receive_messages(wait_id='one', limit=1), 'receiver') is None
        with pytest.raises(nodus.StepDefinitionConflict):
            drive(lambda: nodus.agent.receive_messages(wait_id='one', limit=2), 'receiver')
    else:
        drive(lambda: nodus.agent.send_message('verify', 42, message_key='one').to_dict())
        with pytest.raises(nodus.StepDefinitionConflict):
            drive(lambda: nodus.agent.send_message('other' if change == 'target' else 'verify',
                  0 if change == 'payload' else 42, message_key='one'))


@pytest.mark.parametrize('kwargs', [{'to_task_key': ''}, {'message_key': 'bad key'},
                                   {'payload': float('nan')}, {'payload': 'x' * 16383}])
def test_invalid_send_does_not_checkpoint_or_send(peers, kwargs):
    state, drive = peers
    values = {'to_task_key': 'verify', 'payload': None, 'message_key': 'one', **kwargs}
    with pytest.raises(nodus.ValidationError):
        drive(lambda: nodus.agent.send_message(**values))
    assert [action for action, _ in state['calls']] == ['session']


@pytest.mark.parametrize('limit', [0, 33, True, '1'])
def test_invalid_receive_limit_does_not_checkpoint_or_receive(peers, limit):
    state, drive = peers
    with pytest.raises(nodus.ValidationError):
        drive(lambda: nodus.agent.receive_messages(wait_id='one', limit=limit))
    assert [action for action, _ in state['calls']] == ['session']


def test_maximum_message_and_full_batch_are_available_without_truncation(peers):
    state, drive = peers
    payload = 'x' * 16382
    sent = drive(lambda: nodus.agent.send_message('verify', payload, message_key='maximum').to_dict())
    assert sent['payload_hash'] == hashlib.sha256(('"' + payload + '"').encode()).hexdigest()
    rows = [message('key:' + str(index), payload) for index in range(32)]
    state['waits'][('receiver', 'full')] = 32, {'decision': 'replay', 'wait_id': 'full', 'messages': rows}

    def receive():
        batch = nodus.agent.receive_messages(wait_id='full', limit=32)
        assert [item.payload() for item in batch] == [payload] * 32
        return len(batch)

    assert drive(receive, 'receiver') == 32


@pytest.mark.parametrize('data', [b'"' + b'x' * 16383 + b'"', b'{"a":1,"a":2}', b'NaN', b'not-json'])
def test_hashed_but_invalid_or_oversized_incoming_json_stops_the_driver(peers, data):
    state, drive = peers
    row = message()
    row.update(payload=base64.b64encode(data).decode(), payload_hash=hashlib.sha256(data).hexdigest())
    state['waits'][('receiver', 'page')] = 10, {'decision': 'replay', 'wait_id': 'page', 'messages': [row]}
    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(lambda: nodus.agent.receive_messages(wait_id='page'), 'receiver')
    assert not any(action == 'finish' for action, _ in state['calls'])


def test_peer_controls_require_assigned_serial_driver_outside_steps(peers):
    state, drive = peers
    with pytest.raises(nodus.ValidationError):
        nodus.agent.send_message('verify', None, message_key='one')

    def check():
        session = _agent._current.get()
        session.guard.acquire()
        try:
            with pytest.raises(nodus.ValidationError):
                nodus.agent.receive_messages(wait_id='one')
        finally:
            session.guard.release()
        token = _agent._step_context.set(object())
        try:
            with pytest.raises(nodus.ValidationError):
                nodus.agent.send_message('verify', None, message_key='one')
        finally:
            _agent._step_context.reset(token)
        return None

    drive(check)
    assert not any(action.startswith('peer_') for action, _ in state['calls'])


@pytest.mark.parametrize('response', [httpx.Response(503, json={'code': 'managed_agent_peer_messages_unavailable'}),
                                    httpx.Response(400, json={'error': 'agent_bridge_unavailable'})])
def test_runtime_refusal_reports_unavailable_without_retrying(peers, response):
    state, drive = peers
    state['transform'] = lambda action, result: response if action == 'peer_send' else result
    with pytest.raises(nodus.AgentMessagesUnavailable):
        drive(lambda: nodus.agent.send_message('verify', None, message_key='one'))
    assert [action for action, _ in state['calls']] == ['session', 'peer_send']
