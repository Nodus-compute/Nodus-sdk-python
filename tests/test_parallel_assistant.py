"""Normal requests delegate useful work and retain paid identities on resume."""
import base64
import hashlib
import json
import subprocess
import sys

import nodus
import pytest
from nodus.agent_runtime import run
from test_agent_steps import journal_socket


def encoded(value):
    return base64.b64encode(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).decode()


@pytest.fixture
def team(journal_socket, monkeypatch, tmp_path):
    db, state = journal_socket
    children, inputs, results, messages, received = {}, {}, {}, {}, {}
    completions, waits = [], {}
    state.update(multiple_runs=True, recovery_policy='checkpoint-v1', checkpoint_id='cp-baseline')
    monkeypatch.setenv('NODUS_AGENT_MODEL', 'nodus:claude-test')
    monkeypatch.setenv('NODUS_AGENT_MODEL_MAX_OUTPUT_TOKENS', '1024')
    monkeypatch.setenv('NODUS_AGENT_MAX_WORKERS', '4')

    def task_key(run_id):
        return 'coordinator' if run_id == 'parent' else 'child:' + run_id

    def rpc(action, body):
        owner = body['run_id']
        if action == 'child_spawn':
            identity = owner, body['spawn_key']
            replay = identity in children
            if not replay:
                child = 'ar_' + str(len(children) + 1)
                children[identity] = {'run_id': child, 'parent_run_id': owner,
                    'spawn_key': body['spawn_key'], 'group_id': 'ag_team', 'depth': 1}
                inputs[child] = body['input']
            child = children[identity]
            assert inputs[child['run_id']] == body['input']
            return {'decision': 'replay' if replay else 'admitted', 'child': child}
        if action == 'children_wait':
            identity = owner, body['wait_id']
            if identity in waits:
                return waits[identity]
            response = {'wait_id': body['wait_id'], 'mode': body['mode'], 'decision': 'waiting'}
            selected = body.get('child_run_ids', [row['run_id'] for row in children.values()])
            cancelled = state.get('cancelled', [])
            for child in cancelled:
                if child not in completions:
                    completions.append(child)
            terminal = [child for child in selected if child in results or child in cancelled]
            if body['mode'] == 'next' or len(terminal) == len(selected):
                outcomes = []
                for index, child in enumerate(completions):
                    if child not in terminal:
                        continue
                    reference = next(row for row in children.values() if row['run_id'] == child)
                    outcomes.append({'event_id': 'ce_' + child, 'sequence': index + 1,
                        'child_run_id': child, 'spawn_key': reference['spawn_key'], 'status': 'completed',
                        'result_hash': hashlib.sha256(base64.b64decode(encoded(results.get(child)))).hexdigest()})
                    if child in cancelled:
                        outcomes[-1].update(status='cancelled', reason='customer_cancelled')
                        outcomes[-1].pop('result_hash')
                after = int(body.get('after', '0'))
                page = [row for row in outcomes if row['sequence'] > after][:body.get('limit', 100)]
                if page or len(terminal) == len(selected):
                    response.update(decision='replay', outcomes=page, next_after=str(page[-1]['sequence']) if page else str(after),
                                    exhausted=len(terminal) == len(selected) and len(page) == len([row for row in outcomes if row['sequence'] > after]))
                    waits[identity] = response
            return response
        if action == 'child_cancel':
            state.setdefault('cancellations', {})[body['cancel_key']] = body['child_run_id']
            return {'decision': 'requested', 'child_run_id': body['child_run_id'],
                    'cancel_key': body['cancel_key'], 'cancel_requested_at': '2026-09-28T12:00:00Z'}
        if action == 'child_result':
            return {**{key: body[key] for key in ('child_run_id', 'result_hash')},
                    'result': encoded(results[body['child_run_id']])}
        if action == 'peer_send':
            identity = owner, body['message_key']
            recipient = next(row['run_id'] for row in children.values() if task_key(row['run_id']) == body['to_task_key'])
            if identity not in messages and recipient in state.get('closed_recipients', []):
                return 409, {'code': 'managed_agent_peer_recipient_unavailable'}
            row = {'message_id': 'pm_' + owner + ':' + body['message_key'], 'group_id': 'ag_team',
                   'from_run_id': owner, 'to_run_id': recipient, 'from_task_key': task_key(owner),
                   'to_task_key': body['to_task_key'], 'message_key': body['message_key'],
                   'payload_hash': hashlib.sha256(base64.b64decode(body['payload'])).hexdigest(),
                   'payload': body['payload']}
            replay = identity in messages
            if replay:
                row['message_id'] = messages[identity]['message_id']
                assert messages[identity] == row
            messages[identity] = row
            return {'decision': 'replay' if replay else 'accepted', **{k: v for k, v in row.items() if k != 'payload'}}
        if action == 'peer_receive':
            identity = owner, body['wait_id']
            if identity not in received:
                delivered = {row['message_id'] for rows in received.values() for row in rows}
                rows = [row for row in messages.values() if row['to_run_id'] == owner and row['message_id'] not in delivered]
                if rows:
                    received[identity] = rows[:body['limit']]
            if identity not in received:
                return {'decision': 'waiting', 'wait_id': body['wait_id']}
            return {'decision': 'replay', 'wait_id': body['wait_id'], 'messages': received[identity]}
        raise AssertionError(action)

    def response(request):
        if request['call_id'] == 'plan':
            content = json.dumps(state.get('plan', {'answer': '', 'tasks': ['Evaluate latency', 'Evaluate reliability']}))
            content = state.get('plan_text', state.get('plan_wrapper', '{}').format(content))
        elif request['run_id'] == 'parent':
            content = 'Combined latency and reliability findings'
        else:
            content = state.get('specialist_text', request['run_id'] + ' findings')
        return {'model': 'nodus:claude-test', 'content': [{'type': 'text', 'text': content}],
                'stop_reason': 'end_turn', 'usage': {'input_tokens': 20, 'output_tokens': 12}}

    state.update(rpc_handler=rpc, model_response=response)

    def drive(run_id='parent', task='Compare latency and reliability for this design'):
        from nodus.managed_parallel_assistant import main
        event = {'task': task} if run_id == 'parent' else json.loads(base64.b64decode(inputs[run_id]))
        state.update(run_id=run_id, input=event, status='active', result=None,
                     session_metadata={'peer_messages_version': 1, 'peer_group_id': 'ag_team', 'peer_task_key': task_key(run_id)})
        folder = tmp_path / run_id
        folder.mkdir(exist_ok=True)
        monkeypatch.setenv('NODUS_CHECKPOINT_DIR', str(folder))
        result = run(main, run_id=run_id, version='1')
        if result is not None:
            results[run_id] = result
            if run_id != 'parent' and run_id not in completions:
                completions.append(run_id)
        return result

    return state, drive, children, results, messages, db


@pytest.mark.parametrize('wrapper', ['{}', '```json\n{}\n```', '```\n{}\n```', ' ```json\r\n{}\r\n```\n'])
def test_normal_request_starts_children_before_join_and_combines_their_peer_findings(team, tmp_path, wrapper):
    state, drive, children, results, messages, db = team
    state['plan_wrapper'] = wrapper
    assert drive() is None
    assert len(children) == 2
    operations = [action for action, _ in state['requests']]
    assert operations.count('child_spawn') == 2 and operations.index('children_wait') > max(
        index for index, action in enumerate(operations) if action == 'child_spawn')
    assert drive('ar_1') is None
    assert drive('ar_2')['text'] == 'ar_2 findings'
    assert drive('ar_1')['text'] == 'ar_1 findings'
    result = drive()
    assert result['text'] == 'Combined latency and reliability findings'
    assert len(children) == 2
    assert len(messages) == 4
    for child, peer in [('ar_1', 'ar_2'), ('ar_2', 'ar_1')]:
        assert results[child]['peer_run_id'] == peer
        assert any(peer + ' findings' in json.dumps(row['messages']) for row in state['model_effects'])
    synthesis = state['model_effects'][-1]
    assert all(child + ' findings' in json.dumps(synthesis['messages']) for child in ('ar_1', 'ar_2'))
    saved = json.loads((tmp_path / 'parent' / 'conversation.json').read_text())
    assert saved['messages'][-1] == {'role': 'assistant', 'content': result['text']}
    assert db.execute('SELECT count(*) FROM model_calls').fetchone()[0] == 6
    assert drive() == result
    assert db.execute('SELECT count(*) FROM model_calls').fetchone()[0] == 6


def test_completion_pages_keep_finish_order_across_parent_recovery(team):
    state, drive, _, _, _, db = team
    assert drive() is None
    assert drive('ar_1') is None
    assert drive('ar_2')['peer_run_id'] == 'ar_1'
    assert drive() is None
    assert drive('ar_1')['peer_run_id'] == 'ar_2'
    assert drive()['text'] == 'Combined latency and reliability findings'
    pages = [body for action, body in state['requests'] if action == 'children_wait']
    assert [body['after'] for body in pages] == ['0', '0', '1', '0', '1']
    assert db.execute('SELECT count(*) FROM model_calls').fetchone()[0] == 6


def test_greeting_answers_without_starting_a_child(team):
    state, drive, children, _, messages, _ = team
    state['plan'] = {'answer': 'Hi!', 'tasks': []}
    assert drive(task='hi')['text'] == 'Hi!'
    assert not children and not messages
    assert len(state['model_effects']) == 1


@pytest.mark.parametrize('plan', [
    {'answer': '', 'tasks': ['one']},
    {'answer': '', 'tasks': ['one', 'two', 'three', 'four', 'five']},
    {'answer': 'already done', 'tasks': ['one', 'two']},
])
def test_unusable_plan_never_spawns_or_reissues_its_paid_call(team, plan):
    state, drive, children, _, _, db = team
    state['plan'] = plan
    for _ in range(2):
        with pytest.raises(nodus.StepOutcomeUnknown):
            drive()
    assert not children
    assert db.execute('SELECT count(*) FROM model_calls').fetchone()[0] == 1


@pytest.mark.parametrize('wrapper', [
    'Here is the plan:\n```json\n{}\n```',
    '```json\n{}\n```\nAnother answer',
    '```json\n{}',
    '```python\n{}\n```',
    '```json\n{}\n```\n```json\n{{"answer":"other","tasks":[]}}\n```',
])
def test_ambiguous_or_incomplete_fences_never_spawn_or_recharge(team, wrapper):
    state, drive, children, _, _, db = team
    state['plan_wrapper'] = wrapper
    for _ in range(2):
        with pytest.raises(nodus.StepOutcomeUnknown):
            drive()
    assert not children
    assert db.execute('SELECT count(*) FROM model_calls').fetchone()[0] == 1


def test_cancelled_specialist_releases_the_peer_wait_instead_of_deadlocking_the_team(team):
    state, drive, _, results, _, _ = team
    assert drive() is None
    state['cancelled'] = ['ar_1']
    assert drive('ar_2') is None
    for _ in range(2):
        with pytest.raises(nodus.StepOutcomeUnknown):
            drive()
    assert set(state['cancellations'].values()) == {'ar_2'}
    assert len(state['cancellations']) == 1
    assert not results


def test_early_peer_finding_is_retained_until_the_coordinator_assignment_arrives(team):
    _, drive, _, _, messages, _ = team
    assert drive() is None
    assignment = messages.pop(('parent', 'assignment:ar_2'))
    assert drive('ar_1') is None
    assert drive('ar_2') is None
    messages[('parent', 'assignment:ar_2')] = assignment
    assert drive('ar_2')['peer_run_id'] == 'ar_1'


def test_cancel_before_initial_assignment_stops_other_specialists_without_replanning(team):
    state, drive, children, _, messages, db = team
    state['closed_recipients'] = ['ar_1']
    for _ in range(2):
        with pytest.raises(nodus.StepOutcomeUnknown, match='asked to stop'):
            drive()
    assert set(state['cancellations'].values()) == {'ar_1', 'ar_2'}
    assert len(state['cancellations']) == 2
    assert len(children) == 2 and not messages
    assert db.execute('SELECT count(*) FROM model_calls').fetchone()[0] == 1


def test_capacity_validation_survives_python_optimization():
    result = subprocess.run([sys.executable, '-O', '-m', 'pytest', __file__, '-q', '-k', 'unusable'],
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr


def test_oversized_request_is_rejected_before_paid_planning_or_child_admission(team):
    state, drive, children, _, _, db = team
    with pytest.raises(nodus.ValidationError, match='32 KiB'):
        drive(task='x' * 126000)
    assert not children
    assert db.execute('SELECT count(*) FROM model_calls').fetchone()[0] == 0


@pytest.mark.parametrize('task', ['x' * 32700, '"\n' * 4500], ids=['ascii-boundary', 'escaped-content'])
def test_accepted_input_leaves_room_for_full_peer_review_and_synthesis(team, task):
    state, drive, _, _, _, db = team
    state['specialist_text'] = 'Findings: ' + '\x1f' * 10000
    assert drive(task=task) is None
    assert drive('ar_1') is None
    assert drive('ar_2')['peer_run_id'] == 'ar_1'
    assert drive('ar_1')['peer_run_id'] == 'ar_2'
    assert drive(task=task)['text'] == 'Combined latency and reliability findings'
    for payload in state['model_effects']:
        assert len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()) <= 128 << 10
    assert db.execute('SELECT count(*) FROM model_calls').fetchone()[0] == 6
