"""Child controls preserve journal identities and verify bounded results."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from dataclasses import FrozenInstanceError

import httpx
import pytest

import nodus
from nodus import _agent
from nodus.agent_runtime import run


def encoded(value):
    return base64.b64encode(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).decode()


def outcome(child='child-1', sequence=1, spawn_key='part:1', result=None):
    raw = json.dumps(result, sort_keys=True, separators=(',', ':')).encode()
    return {'event_id': 'event-' + str(sequence), 'sequence': sequence, 'child_run_id': child,
            'spawn_key': spawn_key, 'status': 'completed', 'result_hash': hashlib.sha256(raw).hexdigest()}


@pytest.fixture
def coordinator(monkeypatch, tmp_path):
    state = {'calls': [], 'children': {}, 'lost': {}, 'checkpoint': False, 'wait': None,
             'result': None, 'blob': b'', 'transform': lambda action, response: response,
             'before_action': lambda action, body: None, 'cancellations': {}, 'legacy_cancel_keys': set(),
             'owners': {'child-1': 'parent', 'foreign-child': 'another-parent', 'grandchild': 'child-1'}}
    rpc_class = _agent._RPC
    monkeypatch.setenv('NODUS_AGENT_SOCKET', str(tmp_path / 'private.sock'))

    def handler(request):
        action, body = request.url.path[1:], json.loads(request.content)
        state['calls'].append((action, copy.deepcopy(body)))
        state['before_action'](action, body)
        if action in ('session', 'renew'):
            response = {'session_token': 'private-session', 'epoch': 1,
                'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
                'run': {'run_id': 'parent', 'name': 'main', 'version': '1', 'status': 'active', 'input': encoded(None)}}
            if state['checkpoint']:
                response.update(recovery_policy='checkpoint-v1', checkpoint_id='cp-baseline')
        elif action == 'checkpoint_begin':
            if body['operation'] == 'child_cancel' and body['operation_id'] in state['legacy_cancel_keys']:
                response = {'checkpoint_id': '', 'status': 'legacy_replay'}
            else:
                response = {'checkpoint_id': 'cp-' + body['operation_id'], 'status': 'ready'}
        elif action == 'child_spawn':
            key = body['spawn_key']
            replay = key in state['children']
            if not replay:
                state['children'][key] = {'run_id': 'child-' + str(len(state['children']) + 1),
                    'parent_run_id': 'parent', 'spawn_key': key, 'group_id': 'group-1', 'depth': 1}
                state['owners'][state['children'][key]['run_id']] = 'parent'
            response = {'decision': 'replay' if replay else 'admitted', 'child': state['children'][key]}
        elif action == 'children_wait':
            response = {'wait_id': body['wait_id'], 'mode': body['mode'], **copy.deepcopy(state['wait'])}
        elif action == 'child_cancel':
            key = body['cancel_key']
            replay = key in state['cancellations']
            if replay and state['cancellations'][key]['child_run_id'] != body['child_run_id']:
                return httpx.Response(409, json={'code': 'idempotency_conflict'})
            if key in state['legacy_cancel_keys'] and 'checkpoint_id' in body:
                return httpx.Response(409, json={'code': 'agent_checkpoint_conflict'})
            if not replay:
                state['cancellations'][key] = {'child_run_id': body['child_run_id'], 'cancel_key': key,
                                              'cancel_requested_at': '2026-09-24T12:00:00Z'}
            response = {'decision': 'replay' if replay else 'requested', **state['cancellations'][key]}
        elif action == 'child_result':
            if state['owners'].get(body['child_run_id']) != body['run_id']:
                return httpx.Response(404, json={'code': 'not_found'})
            response = {'child_run_id': body['child_run_id'], 'result_hash': body['result_hash'], 'result': encoded(state['result'])}
        elif action == 'child_blob_get':
            if state['owners'].get(body['child_run_id']) != body['run_id']:
                return httpx.Response(404, json={'code': 'not_found'})
            data, offset = state['blob'], body['offset']
            chunk = data[offset:offset + 262144]
            response = {'child_run_id': body['child_run_id'], 'reference': {
                'id': body['blob_id'], 'sha256': body['sha256'], 'bytes': body['bytes']},
                'status': 'committed', 'offset': offset, 'data': base64.b64encode(chunk).decode(),
                'eof': offset + len(chunk) == len(data)}
        elif action == 'finish':
            response = {'status': 'completed', 'result': body['result']}
        else:
            pytest.fail('unexpected private action: ' + action)
        if state['lost'].get(action, 0):
            state['lost'][action] -= 1
            raise httpx.ReadError('lost private-session acknowledgement', request=request)
        response = state['transform'](action, copy.deepcopy(response))
        return httpx.Response(200, json=response) if not isinstance(response, httpx.Response) else response

    def rpc():
        channel = rpc_class()
        channel.client.close()
        channel.client = httpx.Client(base_url='http://agent.local', transport=httpx.MockTransport(handler),
                                     follow_redirects=False, trust_env=False)
        return channel

    monkeypatch.setattr(_agent, '_RPC', rpc)
    def drive(function):
        def main(_):
            return function()
        return run(main, run_id='parent', version='1')
    return state, drive


def test_spawn_retries_and_reconstructed_driver_keep_one_immutable_child(coordinator):
    state, drive = coordinator
    state['lost']['child_spawn'] = 3
    def spawn():
        return nodus.agent.spawn_child({'part': 7}, spawn_key='part:7').to_dict()
    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(spawn)
    assert state['children'] == {'part:7': {'run_id': 'child-1', 'parent_run_id': 'parent', 'spawn_key': 'part:7',
                                          'group_id': 'group-1', 'depth': 1}}
    assert drive(spawn)['run_id'] == 'child-1'
    requests = [body for action, body in state['calls'] if action == 'child_spawn']
    assert len(requests) == 4 and requests[0] == requests[1] == requests[2]
    assert {body['spawn_key'] for body in requests} == {'part:7'}
    assert all(json.loads(base64.b64decode(body['input'])) == {'part': 7} for body in requests)


def test_spawn_preserves_empty_attenuation_and_checkpoint_before_admission(coordinator):
    state, drive = coordinator
    state['checkpoint'] = True
    captured = []
    def spawn():
        child = nodus.agent.spawn_child(None, spawn_key='empty', permissions={'secrets': [], 'connections': []})
        captured.append(child)
        return child.to_dict()
    drive(spawn)
    request = next(body for action, body in state['calls'] if action == 'child_spawn')
    assert request['permissions'] == {'secrets': [], 'connections': []}
    assert request['checkpoint_id'] == 'cp-empty'
    checkpoint = next(body for action, body in state['calls'] if action == 'checkpoint_begin')
    assert checkpoint['operation'] == 'spawn' and checkpoint['operation_id'] == 'empty'
    with pytest.raises(FrozenInstanceError):
        captured[0].run_id = 'other'


@pytest.mark.parametrize('change', ['parent', 'spawn', 'depth', 'decision'])
def test_spawn_refuses_receipt_for_another_identity(coordinator, change):
    state, drive = coordinator
    def corrupt(action, response):
        if action == 'child_spawn':
            if change == 'decision':
                response['decision'] = 'waiting'
            else:
                response['child'][{'parent': 'parent_run_id', 'spawn': 'spawn_key', 'depth': 'depth'}[change]] = (
                    True if change == 'depth' else 'wrong')
        return response
    state['transform'] = corrupt
    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(lambda: nodus.agent.spawn_child({}, spawn_key='part:1'))
    assert not any(action == 'finish' for action, _ in state['calls'])


def test_all_join_yields_only_after_checkpoint_and_replays_requested_order(coordinator):
    state, drive = coordinator
    state.update(checkpoint=True, wait={'decision': 'waiting'})
    def work():
        children = [nodus.agent.spawn_child(index, spawn_key=f'part:{index}') for index in (1, 2)]
        return [item.child_run_id for item in nodus.agent.await_children(children, wait_id='join')]
    assert drive(work) is None
    assert 'finish' not in [action for action, _ in state['calls']]
    request = next(body for action, body in state['calls'] if action == 'children_wait')
    assert request['child_run_ids'] == ['child-1', 'child-2'] and request['checkpoint_id'] == 'cp-join'
    state['wait'] = {'decision': 'replay', 'outcomes': [outcome(sequence=2), outcome('child-2', 1, 'part:2')],
                     'next_after': '2', 'exhausted': True}
    assert drive(work) == ['child-1', 'child-2']


def test_next_completion_page_keeps_cancelled_outcome_and_exact_cursor(coordinator):
    state, drive = coordinator
    cancelled = {'event_id': 'event-3', 'sequence': 3, 'child_run_id': 'child-3', 'spawn_key': 'part:3',
                 'status': 'cancelled', 'reason': 'customer_cancelled'}
    state['wait'] = {'decision': 'replay', 'outcomes': [cancelled], 'next_after': '3', 'exhausted': False}
    pages = []
    def work():
        page = nodus.agent.next_child_completions(after='2', wait_id='page:2', limit=10)
        pages.append(page)
        return page.next_after
    assert drive(work) == '3'
    assert pages[0].outcomes[0].status == 'cancelled' and not pages[0].exhausted
    request = next(body for action, body in state['calls'] if action == 'children_wait')
    assert request['after'] == '2' and request['limit'] == 10 and request['mode'] == 'next'


@pytest.mark.parametrize('bad', ['duplicate', 'cursor-skips', 'out-of-order', 'false-status', 'oversized',
                                 'missing-first', 'missing-middle', 'empty-unfinished'])
def test_bad_completion_page_never_advances_the_driver(coordinator, bad):
    state, drive = coordinator
    rows = [outcome(sequence=1), outcome('child-2', 2, 'part:2')]
    state['wait'] = {'decision': 'replay', 'outcomes': rows, 'next_after': '2', 'exhausted': False}
    if bad == 'duplicate':
        rows[1] = copy.deepcopy(rows[0])
    elif bad == 'cursor-skips':
        state['wait']['next_after'] = '3'
    elif bad == 'out-of-order':
        rows.reverse()
    elif bad == 'false-status':
        rows[0]['status'] = 'running'
    elif bad == 'missing-first':
        state['wait']['outcomes'] = [rows[1]]
    elif bad == 'missing-middle':
        rows[1] = outcome('child-3', 3, 'part:3')
        state['wait']['next_after'] = '3'
    elif bad == 'empty-unfinished':
        state['wait']['outcomes'] = []
        state['wait']['next_after'] = '0'
    else:
        state['wait']['padding'] = 'x' * 65536
    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(lambda: nodus.agent.next_child_completions(after='0', wait_id='page', limit=2).next_after)
    assert not any(action == 'finish' for action, _ in state['calls'])


def test_child_result_and_blob_verify_origin_and_content(coordinator):
    state, drive = coordinator
    state['result'], state['blob'] = {'answer': 42}, bytes(range(256)) * 2048
    digest = hashlib.sha256(state['blob']).hexdigest()
    reference = {'id': 'bl_' + digest, 'sha256': digest, 'bytes': len(state['blob'])}
    state['wait'] = {'decision': 'replay', 'outcomes': [outcome(result=state['result'])], 'next_after': '1', 'exhausted': True}
    def work():
        child = nodus.agent.spawn_child(None, spawn_key='part:1')
        result = nodus.agent.await_children([child], wait_id='joined')[0]
        assert nodus.agent.get_child_blob(child, reference) == state['blob']
        return nodus.agent.child_result(result)
    assert drive(work) == {'answer': 42}
    calls = [body for action, body in state['calls'] if action == 'child_blob_get']
    assert [body['offset'] for body in calls] == [0, 262144]
    assert {body['child_run_id'] for body in calls} == {'child-1'}


@pytest.mark.parametrize('bad', ['child', 'result-hash', 'blob-child', 'blob-hash'])
def test_child_reads_refuse_cross_child_or_corrupted_payload(coordinator, bad):
    state, drive = coordinator
    state.update(result={'answer': 42}, blob=b'original')
    digest = hashlib.sha256(state['blob']).hexdigest()
    reference = {'id': 'bl_' + digest, 'sha256': digest, 'bytes': 8}
    state['wait'] = {'decision': 'replay', 'outcomes': [outcome(result=state['result'])], 'next_after': '1', 'exhausted': True}
    def corrupt(action, response):
        if action == 'child_result' and bad in ('child', 'result-hash'):
            response['child_run_id' if bad == 'child' else 'result'] = 'wrong' if bad == 'child' else encoded({'answer': 0})
        if action == 'child_blob_get' and bad in ('blob-child', 'blob-hash'):
            response['child_run_id' if bad == 'blob-child' else 'data'] = 'grandchild' if bad == 'blob-child' else base64.b64encode(b'corrupt!').decode()
        return response
    state['transform'] = corrupt
    def work():
        child = nodus.agent.spawn_child(None, spawn_key='part:1')
        completed = nodus.agent.await_children([child], wait_id='joined')[0]
        return nodus.agent.get_child_blob(child, reference) if bad.startswith('blob') else nodus.agent.child_result(completed)
    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(work)


def test_cancel_receipt_reports_request_without_claiming_cleanup(coordinator):
    state, drive = coordinator
    receipts = []
    def work():
        child = nodus.agent.spawn_child(None, spawn_key='part:1')
        receipt = nodus.agent.cancel_child(child, cancel_key='stop:1')
        receipts.append(receipt)
        return receipt.child_run_id
    assert drive(work) == 'child-1'
    assert receipts[0].decision == 'requested'
    assert receipts[0].cancel_requested_at == datetime(2026, 9, 24, 12, tzinfo=timezone.utc)


def test_cancel_captures_decision_before_effect_and_replays_lost_reply(coordinator, tmp_path):
    state, drive = coordinator
    state['checkpoint'] = True
    state['lost']['child_cancel'] = 3
    decision_file = tmp_path / 'decision.json'
    decision = b'{"child":"child-1","cancel_key":"stop:1","reason":"other result accepted"}'
    decision_file.write_bytes(decision)
    captured, at_effect = {}, []
    child = {'run_id': 'child-1', 'parent_run_id': 'parent', 'spawn_key': 'part:1', 'group_id': 'group-1', 'depth': 1}

    def observe(action, body):
        if action == 'checkpoint_begin' and body['operation'] == 'child_cancel':
            captured.setdefault(body['operation_id'], decision_file.read_bytes())
        if action == 'child_cancel':
            at_effect.append(captured.get(body['cancel_key']))
    state['before_action'] = observe

    def work():
        selected = json.loads(decision_file.read_bytes())
        assert selected['child'] == child['run_id']
        return nodus.agent.cancel_child(child, cancel_key=selected['cancel_key']).to_dict()

    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(work)
    assert captured == {'stop:1': decision}
    assert at_effect == [decision] * 3
    assert not any(action == 'finish' for action, _ in state['calls'])
    # Simulate host file restoration from the captured bytes before constructing
    # a replacement driver. This does not exercise native checkpoint storage.
    decision_file.write_bytes(b'{}')
    decision_file.write_bytes(captured['stop:1'])
    replay = drive(work)
    assert replay['decision'] == 'replay' and replay['child_run_id'] == 'child-1'
    assert len(state['cancellations']) == 1
    requests = [body for action, body in state['calls'] if action == 'child_cancel']
    assert len(requests) == 4 and requests[0] == requests[1] == requests[2]
    assert {body['checkpoint_id'] for body in requests} == {'cp-stop:1'}
    assert {body['cancel_key'] for body in requests} == {'stop:1'}


def test_cancel_checkpoint_failure_prevents_the_effect(coordinator):
    state, drive = coordinator
    state['checkpoint'] = True
    state['transform'] = lambda action, response: (
        {**response, 'status': 'failed', 'failure_code': 'checkpoint_upload_failed'}
        if action == 'checkpoint_begin' else response)
    child = {'run_id': 'child-1', 'parent_run_id': 'parent', 'spawn_key': 'part:1', 'group_id': 'group-1', 'depth': 1}
    with pytest.raises(nodus.StepOutcomeUnknown, match='checkpoint_upload_failed'):
        drive(lambda: nodus.agent.cancel_child(child, cancel_key='stop:1').to_dict())
    assert [action for action, _ in state['calls']] == ['session', 'checkpoint_begin']


@pytest.mark.parametrize('wrong_target', [False, True])
def test_legacy_cancellation_marker_replays_only_the_original_target(coordinator, wrong_target):
    state, drive = coordinator
    state.update(checkpoint=True, legacy_cancel_keys={'stop:1'})
    original = {'child_run_id': 'child-1', 'cancel_key': 'stop:1', 'cancel_requested_at': '2026-09-24T12:00:00Z'}
    state['cancellations']['stop:1'] = dict(original)
    child = {'run_id': 'child-2' if wrong_target else 'child-1', 'parent_run_id': 'parent',
             'spawn_key': 'part:2' if wrong_target else 'part:1', 'group_id': 'group-1', 'depth': 1}

    def work():
        return nodus.agent.cancel_child(child, cancel_key='stop:1').to_dict()

    if wrong_target:
        with pytest.raises(nodus.StepDefinitionConflict):
            drive(work)
        assert not any(action == 'finish' for action, _ in state['calls'])
    else:
        receipt = drive(work)
        assert receipt['decision'] == 'replay' and receipt['child_run_id'] == 'child-1'
    assert state['cancellations'] == {'stop:1': original}
    actions = [action for action, _ in state['calls']]
    assert actions[:3] == ['session', 'checkpoint_begin', 'child_cancel']
    cancellation = next(body for action, body in state['calls'] if action == 'child_cancel')
    assert 'checkpoint_id' not in cancellation
    assert cancellation['child_run_id'] == child['run_id'] and cancellation['cancel_key'] == 'stop:1'


@pytest.mark.parametrize('operation,checkpoint_id', [
    ('spawn', ''), ('children_wait', ''), ('child_cancel', 'cp-forged'), ('child_cancel', None),
])
def test_legacy_marker_cannot_skip_other_checkpoints(coordinator, operation, checkpoint_id):
    state, drive = coordinator
    state['checkpoint'] = True
    state['transform'] = lambda action, response: (
        {'status': 'legacy_replay', 'checkpoint_id': checkpoint_id} if action == 'checkpoint_begin' else response)
    child = {'run_id': 'child-1', 'parent_run_id': 'parent', 'spawn_key': 'part:1', 'group_id': 'group-1', 'depth': 1}

    def work():
        if operation == 'spawn':
            return nodus.agent.spawn_child(None, spawn_key='part:1')
        if operation == 'children_wait':
            return nodus.agent.next_child_completions(wait_id='wait:1')
        return nodus.agent.cancel_child(child, cancel_key='stop:1')

    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(work)
    assert [action for action, _ in state['calls']] == ['session', 'checkpoint_begin']


@pytest.mark.parametrize('child_id', ['foreign-child', 'grandchild'])
@pytest.mark.parametrize('action', ['child_result', 'child_blob_get'])
def test_server_refuses_foreign_or_grandchild_reads_without_driver_progress(coordinator, child_id, action):
    state, drive = coordinator
    state.update(result={'private': 42}, blob=b'private bytes')
    digest = hashlib.sha256(state['blob']).hexdigest()
    reference = {'id': 'bl_' + digest, 'sha256': digest, 'bytes': len(state['blob'])}
    forged = outcome(child=child_id, result=state['result'])

    def work():
        with pytest.raises(nodus.StepOutcomeUnknown, match='not_found'):
            if action == 'child_result':
                nodus.agent.child_result(forged)
            else:
                nodus.agent.get_child_blob(forged, reference)
        return 'must not finish after scope refusal'

    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(work)
    assert [called for called, _ in state['calls']] == ['session', action]
    assert state['calls'][1][1]['child_run_id'] == child_id


def test_controls_refuse_parallel_or_nested_step_use(coordinator):
    state, drive = coordinator
    def work():
        session = _agent._current.get()
        session.guard.acquire()
        try:
            with pytest.raises(nodus.ValidationError, match='serial'):
                nodus.agent.spawn_child(None, spawn_key='part:1')
        finally:
            session.guard.release()
        token = _agent._step_context.set(object())
        try:
            with pytest.raises(nodus.ValidationError):
                nodus.agent.next_child_completions(after='0', wait_id='join')
        finally:
            _agent._step_context.reset(token)
        return None
    drive(work)
    assert not any(action.startswith('child') for action, _ in state['calls'])


def test_unavailable_children_never_fall_back_to_account_submission(coordinator):
    state, drive = coordinator
    state['transform'] = lambda action, response: (
        httpx.Response(503, json={'code': 'managed_agent_children_unavailable'}) if action == 'child_spawn' else response)
    with pytest.raises(nodus.AgentChildrenUnavailable):
        drive(lambda: nodus.agent.spawn_child(None, spawn_key='part:1'))
    assert len([action for action, _ in state['calls'] if action == 'child_spawn']) == 1


@pytest.mark.parametrize('permissions', [
    {'secrets': ['key'] * 2}, {'secrets': [str(i) for i in range(65)]},
    {'secrets': ['é' * 128]}, {'egress_allow': ['Example.com', 'example.com']},
    {'connections': ['\nprivate']}, {'secret_refs': 'all'}, {'command': []},
])
def test_invalid_permissions_refuse_before_checkpoint_or_admission(coordinator, permissions):
    state, drive = coordinator
    state['checkpoint'] = True
    with pytest.raises(nodus.ValidationError):
        drive(lambda: nodus.agent.spawn_child(None, spawn_key='part:1', permissions=permissions))
    assert [action for action, _ in state['calls']] == ['session']


@pytest.mark.parametrize('after,limit', [('01', 1), ('-1', 1), ('1e3', 1), ('0', True), ('0', 101), (str(2**63), 1)])
def test_invalid_page_bounds_refuse_before_any_control_rpc(coordinator, after, limit):
    state, drive = coordinator
    with pytest.raises(nodus.ValidationError):
        drive(lambda: nodus.agent.next_child_completions(after=after, wait_id='page', limit=limit))
    assert [action for action, _ in state['calls']] == ['session']


def test_empty_all_join_is_a_replayable_receipt_and_wrong_child_is_refused(coordinator):
    state, drive = coordinator
    state['wait'] = {'decision': 'replay', 'outcomes': [], 'next_after': '0', 'exhausted': True}
    assert drive(lambda: list(nodus.agent.await_children([], wait_id='empty'))) == []
    state['calls'].clear()
    foreign = {'run_id': 'child', 'parent_run_id': 'another-parent', 'spawn_key': 'part:1', 'group_id': 'group', 'depth': 1}
    with pytest.raises(nodus.ValidationError):
        drive(lambda: nodus.agent.await_children([foreign], wait_id='foreign'))
    assert [action for action, _ in state['calls']] == ['session']


def test_cancelled_child_is_not_silently_a_successful_null_result(coordinator):
    state, drive = coordinator
    cancelled = {'event_id': 'event-1', 'sequence': 1, 'child_run_id': 'child-1', 'spawn_key': 'part:1',
                 'status': 'cancelled', 'reason': 'parent_cancelled'}
    with pytest.raises(nodus.ValidationError):
        drive(lambda: nodus.agent.child_result(cancelled))
    assert [action for action, _ in state['calls']] == ['session']


def test_expired_child_result_does_not_reexecute_or_report_null(coordinator):
    state, drive = coordinator
    state['transform'] = lambda action, response: (
        httpx.Response(410, json={'code': 'managed_agent_child_result_expired'}) if action == 'child_result' else response)
    with pytest.raises(nodus.StepResultExpired):
        drive(lambda: nodus.agent.child_result(outcome()))
    assert [action for action, _ in state['calls']] == ['session', 'child_result']


def test_legacy_broker_returns_explicit_child_feature_unavailable(coordinator):
    state, drive = coordinator
    state['transform'] = lambda action, response: (
        httpx.Response(400, json={'error': 'agent_bridge_unavailable'}) if action == 'child_spawn' else response)
    with pytest.raises(nodus.AgentChildrenUnavailable):
        drive(lambda: nodus.agent.spawn_child(None, spawn_key='part:1'))
    assert [action for action, _ in state['calls']] == ['session', 'child_spawn']


def test_wait_identity_mismatch_does_not_acknowledge_yield(coordinator):
    state, drive = coordinator
    state['wait'] = {'decision': 'waiting', 'wait_id': 'another-wait'}
    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(lambda: nodus.agent.next_child_completions(wait_id='page'))


def test_caught_unknown_child_result_poisons_the_driver_before_finish(coordinator):
    state, drive = coordinator
    state['transform'] = lambda action, response: (
        {**response, 'result': encoded('wrong')} if action == 'child_result' else response)
    def work():
        with pytest.raises(nodus.StepOutcomeUnknown):
            nodus.agent.child_result(outcome())
        return 'must not finish'
    with pytest.raises(nodus.StepOutcomeUnknown):
        drive(work)
    assert not any(action == 'finish' for action, _ in state['calls'])
