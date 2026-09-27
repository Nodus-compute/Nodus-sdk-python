"""Synthetic broker outcomes, with literal artifact bytes and replay assertions."""
import base64
from contextlib import contextmanager
import contextvars
import hashlib
import threading

import pytest

from nodus import _agent, _agent_artifacts
from nodus import agent

put_blob, get_blob = agent.put_blob, agent.get_blob
from nodus.errors import ValidationError, StepOutcomeUnknown


class Broker:
    def __init__(self):
        self.reference = None
        self.saved = bytearray()
        self.pending = None
        self.committing = False
        self.committed = False
        self.calls = []
        self.change = None

    def call(self, action, body):
        self.calls.append((action, dict(body)))
        assert body['version'] == 2
        assert body['session_token'] == 'owned-token'
        expected = {'version': 2, 'id': body['blob_id'], 'sha256': body['sha256'], 'bytes': body['bytes']}
        if self.reference is None:
            self.reference = expected
        assert self.reference == expected
        if action == 'blob_begin':
            if self.pending is not None:
                self.saved.extend(self.pending)
                self.pending = None
            if self.committing:
                assert len(self.saved) == expected['bytes']
                assert hashlib.sha256(self.saved).hexdigest() == expected['sha256']
                self.committed = True
        elif action == 'blob_put':
            assert not self.committed and self.pending is None
            assert body['offset'] == len(self.saved)
            self.pending = base64.b64decode(body['data'], validate=True)
        elif action == 'blob_commit':
            assert len(self.saved) == expected['bytes'] and self.pending is None
            self.committing = True
        status = 'committed' if self.committed else 'verifying' if self.committing else 'uploading'
        receipt = {'reference': dict(expected), 'status': status, 'next_offset': len(self.saved)}
        if action in ('blob_get', 'child_blob_get'):
            offset = body['offset']
            chunk = bytes(self.saved[offset:offset + 262144])
            receipt.update(offset=offset, data=base64.b64encode(chunk).decode(), eof=offset + len(chunk) == len(self.saved))
            if action == 'child_blob_get':
                receipt['child_run_id'] = body['child_run_id']
        if self.change:
            self.change(action, receipt)
        return receipt


class Session:
    def __init__(self, rpc):
        self.rpc = rpc
        self.scope = {'run_id': 'parent_one', 'session_token': 'owned-token', 'epoch': 1}
        self.guard = threading.Lock()
        self.failed = threading.Event()
        self.stopped = threading.Event()
        self.recovery_policy = ''

    def checkpoint(self, *args, **kwargs):
        return {}

    def healthy(self):
        if self.failed.is_set():
            raise StepOutcomeUnknown('Session failed')


@contextmanager
def assigned(rpc):
    session = Session(rpc)
    current = _agent._current.set(session)
    managed = _agent._managed.set(True)
    try:
        yield session
    finally:
        _agent._managed.reset(managed)
        _agent._current.reset(current)


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(_agent_artifacts, '_pause', lambda session, deadline, attempt: session.healthy())


def test_object_upload_waits_for_external_verification_and_replays(no_backoff):
    broker = Broker()
    payload = b'a' * 262144 + b'independently expected tail\n'
    expected = {'version': 2, 'id': 'bl2_' + hashlib.sha256(payload).hexdigest(),
                'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)}
    with assigned(broker):
        assert put_blob(payload, storage='object') == expected
        assert get_blob(expected) == payload
    assert bytes(broker.saved) == payload and broker.committed
    assert [body['offset'] for action, body in broker.calls if action == 'blob_put'] == [0, 262144]
    calls = len(broker.calls)
    with assigned(broker):
        assert put_blob(payload, storage='object') == expected
    assert [action for action, _ in broker.calls[calls:]] == ['blob_begin']


def test_lost_put_reply_recovers_deterministic_identity_in_new_session(no_backoff):
    broker = Broker()
    payload = b'recover my artifact'
    original = broker.call
    def lose(action, body):
        result = original(action, body)
        if action == 'blob_put':
            raise StepOutcomeUnknown('Lost response after durable chunk acceptance')
        return result
    broker.call = lose
    with assigned(broker) as session:
        with pytest.raises(StepOutcomeUnknown, match='Lost response'):
            put_blob(payload, storage='object')
        assert session.failed.is_set()
    identity = dict(broker.reference)
    broker.call = original
    with assigned(broker):
        assert put_blob(payload, storage='object') == identity
        assert get_blob(identity) == payload
    assert len([1 for action, _ in broker.calls if action == 'blob_put']) == 1


@pytest.mark.parametrize('change', [
    lambda r: r.update(next_offset=True),
    lambda r: r.update(next_offset=1),
    lambda r: r.update(status='committed'),
    lambda r: r['reference'].update(bytes=3.0),
    lambda r: r['reference'].update(version=True),
    lambda r: r['reference'].update(id='bl_legacy'),
])
def test_malformed_upload_receipt_poisoned_before_more_writes(no_backoff, change):
    broker = Broker()
    broker.change = lambda action, receipt: change(receipt)
    with assigned(broker) as session:
        with pytest.raises(StepOutcomeUnknown):
            put_blob(b'abc', storage='object')
        assert session.failed.is_set()
    assert [action for action, _ in broker.calls] == ['blob_begin']


def test_empty_object_commits_without_a_chunk(no_backoff):
    broker = Broker()
    with assigned(broker):
        result = put_blob(b'', storage='object')
        assert result['sha256'] == 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
        assert get_blob(result) == b''
    assert all(action != 'blob_put' for action, _ in broker.calls)


def test_artifact_in_own_step_reuses_guard_but_copied_thread_context_cannot(no_backoff):
    broker = Broker()
    with assigned(broker) as session:
        session.guard.acquire()
        context = object()
        token = _agent._step_context.set(context)
        session._step_owner = (threading.get_ident(), context)
        errors = []
        copied = contextvars.copy_context()
        def parallel():
            try:
                put_blob(b'forbidden', storage='object')
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=lambda: copied.run(parallel))
        thread.start()
        thread.join(1)
        try:
            assert not thread.is_alive()
            assert len(errors) == 1 and isinstance(errors[0], ValidationError)
            assert broker.calls == []
            assert put_blob(b'owning step result', storage='object')['version'] == 2
            assert session.guard.locked()
        finally:
            _agent._step_context.reset(token)
            session.guard.release()


def test_public_object_upload_commits_inside_actual_step(no_backoff):
    import nodus
    broker = Broker()
    original = broker.call
    def step_call(action, body):
        if action == 'claim':
            return {'decision': 'execute', 'step_id': body['step_id'], 'claim_token': 'claim_one', 'external_key': 'external_one'}
        if action == 'complete':
            return {'decision': 'replay', 'step_id': body['step_id'], 'result': body['result']}
        return original(action, body)
    broker.call = step_call
    @nodus.step(name='publish', version='1', effect='pure')
    def publish():
        return nodus.agent.put_blob(b'actual step artifact', storage='object')
    with assigned(broker) as session:
        result = publish(_step_id='publish_one')
        assert result['version'] == 2 and broker.committed
        assert not session.guard.locked() and session._step_owner is None


def test_parallel_driver_and_unknown_storage_refuse_before_rpc():
    broker = Broker()
    with assigned(broker) as session:
        session.guard.acquire()
        try:
            with pytest.raises(ValidationError, match='serially'):
                put_blob(b'no', storage='object')
        finally:
            session.guard.release()
        with pytest.raises(ValidationError):
            put_blob(b'no', storage='unknown')
    assert broker.calls == []


@pytest.mark.parametrize('mutation', [
    lambda r: r.update(eof=1),
    lambda r: r.update(offset=True),
    lambda r: r.update(data=base64.b64encode(b'bad').decode()),
    lambda r: r.update(next_offset=0),
])
def test_download_verifies_complete_receipt_and_hash(no_backoff, mutation):
    broker = Broker()
    with assigned(broker):
        ref = put_blob(b'yes', storage='object')
        broker.change = lambda action, receipt: mutation(receipt) if action == 'blob_get' else None
        with pytest.raises(StepOutcomeUnknown):
            get_blob(ref)


def test_child_object_read_keeps_direct_child_target(no_backoff):
    from nodus._agent_children import get_child_blob
    broker = Broker()
    with assigned(broker):
        ref = put_blob(b'scoped child result', storage='object')
        child = {'run_id': 'child_one', 'parent_run_id': 'parent_one', 'spawn_key': 'part_one', 'group_id': 'group_one', 'depth': 1}
        assert get_child_blob(child, ref) == b'scoped child result'
    call = broker.calls[-1]
    assert call[0] == 'child_blob_get' and call[1]['version'] == 2
    assert call[1]['child_run_id'] == 'child_one' and call[1]['run_id'] == 'parent_one'
