"""Hosted calls retain one paid identity across driver and acknowledgement loss."""

import json
from types import SimpleNamespace

import nodus
import pytest

from test_agent_steps import journal_socket
from nodus.agent_runtime import run


def model_entrypoint():
    @nodus.step(name='model-answer', version='1', effect='pure')
    def answer(task):
        return nodus.agent.model([{'role': 'user', 'content': task}], call_id='answer',
                                model='nodus:claude-test', max_output_tokens=128)
    def main(event):
        return answer(event['task'], _step_id='answer')
    return main


def test_hosted_model_lost_begin_and_delayed_response_preserve_identity_and_renew(journal_socket):
    _, state = journal_socket
    state.update(input={'task': 'Summarize the report'}, lost_model_begin=True,
                 model_pending_polls=4, session_seconds=.15)
    result = run(model_entrypoint(), run_id='cycle-42', version='1')
    assert result['content'] == [{'type': 'text', 'text': 'Saved answer'}]
    assert len(state['model_effects']) == 1
    requests = state['requests']
    begin = [body for action, body in requests if action == 'model_begin']
    assert len(begin) == 2 and begin[0] == begin[1]
    assert begin[0]['call_id'] == 'answer' and begin[0]['step_id'] == 'answer'
    assert begin[0]['input']['max_tokens'] == 128
    polling = [index for index, (action, _) in enumerate(requests) if action == 'model_status']
    assert any(action == 'renew' for action, _ in requests[polling[0]:polling[-1]])


def test_hosted_model_response_replays_after_driver_loses_all_acknowledgements(journal_socket):
    _, state = journal_socket
    state.update(input={'task': 'One paid request'}, lost_model_status=3,
                 recovery_policy='checkpoint-v1', checkpoint_id='cp_saved')
    main = model_entrypoint()
    with pytest.raises(nodus.StepOutcomeUnknown):
        run(main, run_id='cycle-42', version='1')
    assert state['status'] == 'active' and len(state['model_effects']) == 1
    result = run(main, run_id='cycle-42', version='1')
    assert result['content'][0]['text'] == 'Saved answer'
    assert len(state['model_effects']) == 1
    claims = [body['claim_token'] for action, body in state['requests'] if action == 'model_begin']
    assert len(set(claims)) == 2


@pytest.mark.parametrize('journal_ack_lost', [False, True])
def test_hosted_model_unknown_outcome_blocks_without_another_paid_request(journal_socket, journal_ack_lost):
    _, state = journal_socket
    state.update(input={'task': 'Do not repeat'}, model_final_state='unknown',
                 recovery_policy='checkpoint-v1', checkpoint_id='cp_saved', reject_unknown=journal_ack_lost)
    for _ in range(2):
        with pytest.raises(nodus.StepOutcomeUnknown, match='reconcil'):
            run(model_entrypoint(), run_id='cycle-42', version='1')
    assert len(state['model_effects']) == 1 and state['status'] == 'active'


def test_hosted_model_running_receipt_has_bounded_polling(journal_socket, monkeypatch):
    from nodus import _agent_models
    _, state = journal_socket
    state.update(input={'task': 'Do not wait indefinitely'}, model_pending_polls=1,
                 recovery_policy='checkpoint-v1', checkpoint_id='cp_saved')
    observed = iter((0, 241))
    monkeypatch.setattr(_agent_models, 'time', SimpleNamespace(monotonic=lambda: next(observed)), raising=False)
    with pytest.raises(nodus.StepOutcomeUnknown, match='mdl_.*answer'):
        run(model_entrypoint(), run_id='cycle-42', version='1')
    assert len(state['model_effects']) == 1
    assert not any(action == 'finish' for action, _ in state['requests'])


def test_hosted_model_does_not_start_status_after_poll_delay_reaches_limit(journal_socket, monkeypatch):
    from nodus import _agent_models
    _, state = journal_socket
    state.update(input={'task': 'Keep the accepted identity'}, model_pending_polls=1)
    observed = iter((0, 239.9, 240.1))
    monkeypatch.setattr(_agent_models, 'time', SimpleNamespace(monotonic=lambda: next(observed)))
    with pytest.raises(nodus.StepOutcomeUnknown, match='mdl_.*answer'):
        run(model_entrypoint(), run_id='cycle-42', version='1')
    assert len(state['model_effects']) == 1
    assert not any(action == 'model_status' for action, _ in state['requests'])


@pytest.mark.parametrize('case', ['messages', 'bytes', 'large_task'])
def test_owned_assistant_retains_recent_complete_turns_within_request_limit(journal_socket, tmp_path, monkeypatch, case):
    from nodus.managed_assistant import main
    _, state = journal_socket
    if case == 'messages':
        history = [message for i in range(512) for message in (
            {'role': 'user', 'content': 'question ' + str(i)},
            {'role': 'assistant', 'content': 'answer ' + str(i)})]
        task, first_retained = 'Next report', 2
    elif case == 'bytes':
        history = [message for i in range(4) for message in (
            {'role': 'user', 'content': str(i) + 'é' * 20000},
            {'role': 'assistant', 'content': 'answer ' + str(i)})]
        task, first_retained = 'Next report', 2
    else:
        history = [{'role': 'user', 'content': 'old' * 20000},
                   {'role': 'assistant', 'content': 'old answer'}]
        task, first_retained = 'latest task ' + 'x' * 112000, 2
    directory = tmp_path / 'state'
    directory.mkdir()
    saved = directory / 'conversation.json'
    saved.write_text(json.dumps({'version': 1, 'messages': history}, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setenv('NODUS_CHECKPOINT_DIR', str(directory))
    monkeypatch.setenv('NODUS_AGENT_MODEL', 'nodus:claude-test')
    monkeypatch.setenv('NODUS_AGENT_MODEL_MAX_OUTPUT_TOKENS', '128')
    state.update(input={'task': task}, recovery_policy='checkpoint-v1', checkpoint_id='cp_saved')
    assert run(main, run_id='cycle-42', version='1')['text'] == 'Saved answer'
    request, = state['model_effects']
    assert request['messages'] == [*history[first_retained:], {'role': 'user', 'content': task}]
    assert len(json.dumps(request, ensure_ascii=False, separators=(',', ':')).encode()) <= 128 << 10
    assert json.loads(saved.read_text(encoding='utf-8'))['messages'] == [*request['messages'], {'role': 'assistant', 'content': 'Saved answer'}]


@pytest.mark.parametrize('stop_reason,truncated', [('end_turn', False), ('max_tokens', True)])
def test_owned_assistant_labels_and_replays_partial_answer_without_more_spend(journal_socket, tmp_path, monkeypatch, stop_reason, truncated):
    from nodus.managed_assistant import main
    _, state = journal_socket
    directory = tmp_path / 'state'
    directory.mkdir()
    monkeypatch.setenv('NODUS_CHECKPOINT_DIR', str(directory))
    monkeypatch.setenv('NODUS_AGENT_MODEL', 'nodus:claude-test')
    monkeypatch.setenv('NODUS_AGENT_MODEL_MAX_OUTPUT_TOKENS', '128')
    response = {'model': 'nodus:claude-test', 'content': [{'type': 'text', 'text': 'Saved partial text'}],
                'stop_reason': stop_reason, 'usage': {'input_tokens': 10, 'output_tokens': 128}}
    state.update(input={'task': 'Answer the report'}, recovery_policy='checkpoint-v1', model_response=response)
    first = run(main, run_id='cycle-42', version='1')
    assert first['text'] == 'Saved partial text' and first['truncated'] is truncated
    assert first['stop_reason'] == stop_reason
    saved = (directory / 'conversation.json').read_bytes()
    assert json.loads(saved)['messages'][-1] == {'role': 'assistant', 'content': 'Saved partial text'}
    assert run(main, run_id='cycle-42', version='1') == first
    assert (directory / 'conversation.json').read_bytes() == saved
    assert len(state['model_effects']) == 1


def test_owned_assistant_rejects_oversized_current_task_before_paid_call(journal_socket, tmp_path, monkeypatch):
    from nodus.managed_assistant import main
    _, state = journal_socket
    directory = tmp_path / 'state'
    directory.mkdir()
    monkeypatch.setenv('NODUS_CHECKPOINT_DIR', str(directory))
    monkeypatch.setenv('NODUS_AGENT_MODEL', 'nodus:claude-test')
    monkeypatch.setenv('NODUS_AGENT_MODEL_MAX_OUTPUT_TOKENS', '128')
    state.update(input={'task': 'x' * (128 << 10)}, recovery_policy='checkpoint-v1')
    with pytest.raises(nodus.ValidationError, match='Shorten'):
        run(main, run_id='cycle-42', version='1')
    assert not state.get('model_effects')


@pytest.mark.parametrize('history', [[{'role': 'user', 'content': 'unfinished turn'}],
                                     [{'role': 'assistant', 'content': 'wrong first role'},
                                      {'role': 'user', 'content': 'wrong second role'}]])
def test_owned_assistant_does_not_spend_with_unpaired_saved_turns(journal_socket, tmp_path, monkeypatch, history):
    from nodus.managed_assistant import main
    _, state = journal_socket
    directory = tmp_path / 'state'
    directory.mkdir()
    saved = directory / 'conversation.json'
    original = json.dumps({'version': 1, 'messages': history}).encode()
    saved.write_bytes(original)
    monkeypatch.setenv('NODUS_CHECKPOINT_DIR', str(directory))
    monkeypatch.setenv('NODUS_AGENT_MODEL', 'nodus:claude-test')
    monkeypatch.setenv('NODUS_AGENT_MODEL_MAX_OUTPUT_TOKENS', '128')
    state.update(input={'task': 'Next report'}, recovery_policy='checkpoint-v1')
    with pytest.raises(nodus.StepOutcomeUnknown):
        run(main, run_id='cycle-42', version='1')
    assert not state.get('model_effects')
    assert saved.read_bytes() == original


@pytest.mark.parametrize('mode', ['changed_identity', 'oversized_response'])
def test_hosted_model_rejects_unbound_or_oversized_response(journal_socket, mode):
    _, state = journal_socket
    state.update(input={'task': 'Verify response'}, recovery_policy='checkpoint-v1', checkpoint_id='cp_saved')
    if mode == 'changed_identity':
        state['change_model_identity'] = True
    else:
        state['model_response'] = {'model': 'nodus:claude-test', 'content': [{'type': 'text', 'text': 'x' * (256 << 10)}]}
    with pytest.raises(nodus.StepOutcomeUnknown):
        run(model_entrypoint(), run_id='cycle-42', version='1')
    assert state['status'] == 'active' and len(state['model_effects']) == 1


def test_owned_assistant_restores_conversation_for_the_next_session_run(journal_socket, tmp_path, monkeypatch):
    from nodus.managed_assistant import main
    db, state = journal_socket
    saved = tmp_path / 'state'
    saved.mkdir()
    monkeypatch.setenv('NODUS_CHECKPOINT_DIR', str(saved))
    monkeypatch.setenv('NODUS_AGENT_MODEL', 'nodus:claude-test')
    monkeypatch.setenv('NODUS_AGENT_MODEL_MAX_OUTPUT_TOKENS', '128')
    state.update(input={'task': 'Remember the first report'}, recovery_policy='checkpoint-v1', checkpoint_id='cp_baseline')
    first = run(main, run_id='cycle-42', version='1')
    assert first['text'] == 'Saved answer'
    assert any(path.is_file() for path in saved.iterdir())
    db.execute('DELETE FROM journal')
    db.commit()
    state.update(run_id='cycle-43', input={'task': 'Compare the next report'}, status='active', result=None)
    second = run(main, run_id='cycle-43', version='1')
    assert second['text'] == 'Saved answer'
    assert state['model_effects'][1]['messages'] == [
        {'role': 'user', 'content': 'Remember the first report'},
        {'role': 'assistant', 'content': 'Saved answer'},
        {'role': 'user', 'content': 'Compare the next report'},
    ]
    saved_state = json.loads(next(saved.iterdir()).read_text())
    assert saved_state['messages'][-1] == {'role': 'assistant', 'content': 'Saved answer'}


def test_owned_assistant_reuses_paid_reply_after_state_commit_fails(journal_socket, tmp_path, monkeypatch):
    from nodus.managed_assistant import main
    db, state = journal_socket
    directory = tmp_path / 'state'
    directory.mkdir()
    previous = {'version': 1, 'messages': [{'role': 'user', 'content': 'First report'},
                                          {'role': 'assistant', 'content': 'First answer'}]}
    checkpoint = json.dumps(previous).encode()
    saved = directory / 'conversation.json'
    saved.write_bytes(checkpoint)
    monkeypatch.setenv('NODUS_CHECKPOINT_DIR', str(directory))
    monkeypatch.setenv('NODUS_AGENT_MODEL', 'nodus:claude-test')
    monkeypatch.setenv('NODUS_AGENT_MODEL_MAX_OUTPUT_TOKENS', '128')
    state.update(input={'task': 'Next report'}, recovery_policy='checkpoint-v1',
                 checkpoint_id='cp_saved', checkpoint_status='failed')
    with pytest.raises(nodus.StepOutcomeUnknown):
        run(main, run_id='cycle-42', version='1')
    assert saved.read_bytes() != checkpoint and len(state['model_effects']) == 1
    # A replacement restores the committed files and fences the stopped step.
    saved.write_bytes(checkpoint)
    db.execute("UPDATE journal SET status='unknown' WHERE status='started'")
    db.commit()
    state['checkpoint_status'] = 'ready'
    assert run(main, run_id='cycle-42', version='1')['text'] == 'Saved answer'
    assert len(state['model_effects']) == 1
    restored = json.loads(saved.read_bytes())
    assert restored['messages'] == [*previous['messages'], {'role': 'user', 'content': 'Next report'},
                                    {'role': 'assistant', 'content': 'Saved answer'}]


def test_hosted_model_requires_executing_managed_step():
    with pytest.raises(nodus.ValidationError, match='managed.*step'):
        nodus.agent.model([{'role': 'user', 'content': 'task'}], call_id='answer',
                          model='nodus:claude-test', max_output_tokens=128)


@pytest.mark.parametrize('options', [
    {'max_output_tokens': True}, {'max_output_tokens': 4097},
    {'model': 'https://outside.invalid'}, {'messages': [{'role': 'user', 'content': {'url': 'file:///secret'}}]},
    {'messages': [{'role': 'user', 'content': 'x' * (128 << 10)}]},
])
def test_hosted_model_invalid_request_never_reaches_paid_boundary(journal_socket, options):
    _, state = journal_socket
    @nodus.step(name='validate', version='1', effect='pure')
    def validate():
        values = {'messages': [{'role': 'user', 'content': 'task'}], 'call_id': 'answer',
                  'model': 'nodus:claude-test', 'max_output_tokens': 128, **options}
        with pytest.raises(nodus.ValidationError):
            nodus.agent.model(**values)
        return None
    def main(event):
        return validate(_step_id='validate')
    run(main, run_id='cycle-42', version='1')
    assert not state.get('model_effects')
