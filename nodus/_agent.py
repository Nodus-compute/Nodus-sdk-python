"""Serial replay driver using the sandbox's private local capability channel."""
from __future__ import annotations

import base64
import contextvars
from datetime import datetime, timezone
import inspect
import json
import os
import re
import threading
import time
import uuid
from typing import Any

import httpx
from .errors import NodusError, ValidationError, StepOutcomeUnknown, StepDefinitionConflict, StepResultExpired, StepFailed, AgentChildrenUnavailable, AgentBrokerUnavailable, AgentMessagesUnavailable

_ID = re.compile(r'^[A-Za-z0-9:_.-]{1,128}$')
_MAX = 256 << 10
_current = contextvars.ContextVar('nodus_agent_session', default=None)
_step_context = contextvars.ContextVar('nodus_agent_step', default=None)
_step_authority = contextvars.ContextVar('nodus_agent_step_authority', default=None)
_managed = contextvars.ContextVar('nodus_managed_driver', default=False)


def _id(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValidationError('Agent identities must contain 1 to 128 ASCII letters, digits, colon, underscore, period or hyphen')
    return value


def encode(value: Any) -> str:
    def check(item):
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                raise ValidationError('Agent JSON objects require string keys')
            for child in item.values():
                check(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValidationError('Agent payloads must be JSON values')
    try:
        check(value)
        data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValidationError('Agent payload must be bounded finite JSON') from exc
    if len(data) > _MAX:
        raise ValidationError('Agent input or result exceeds 256 KiB')
    return base64.b64encode(data).decode('ascii')


def decode(value: Any) -> Any:
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = item
        return result
    try:
        if not isinstance(value, str) or len(value) > ((_MAX + 2) // 3) * 4:
            raise ValueError('invalid result')
        data = base64.b64decode(value, validate=True)
        if len(data) > _MAX:
            raise ValueError('result too large')
        decoded = json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
        encode(decoded)
        return decoded
    except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError) as exc:
        raise StepOutcomeUnknown('The durable result could not be verified') from exc


class _BrokerMissing(Exception):
    pass


class _RPC:
    def __init__(self):
        path = os.environ.get('NODUS_AGENT_SOCKET', '')
        if not path or not os.path.isabs(path) or '\x00' in path:
            raise ValidationError('Run this agent inside a sandbox with its private agent socket')
        self.client = httpx.Client(transport=httpx.HTTPTransport(uds=path), base_url='http://agent.local',
                                   trust_env=False, follow_redirects=False, timeout=10)
        self.lock = threading.Lock()

    def close(self):
        self.client.close()

    def call(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        # The guest broker accepts one RPC at a time, including session renewal.
        with self.lock:
            return self._call(action, body)

    def _call(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(3):
            try:
                response = self.client.post('/' + action, json=body)
            except httpx.TransportError:
                if attempt == 2:
                    raise StepOutcomeUnknown('Journal acknowledgement unavailable') from None
                continue
            if action in ('broker_invoke', 'broker_status'):
                try:
                    refusal = response.json()
                except ValueError:
                    refusal = None
                code = (refusal.get('code') or refusal.get('error')) if isinstance(refusal, dict) else None
                if action == 'broker_status' and response.status_code == 404 and code == 'not_found':
                    raise _BrokerMissing()
                if (response.status_code == 400 and code == 'agent_bridge_unavailable'
                        or response.status_code in (409, 503) and code in (
                            'managed_broker_unavailable', 'managed_broker_runtime_unqualified', 'managed_broker_profile_unqualified')):
                    raise AgentBrokerUnavailable('The requested broker is unavailable for this managed runtime or account')
            if action in ('child_spawn', 'children_wait', 'child_cancel', 'child_result', 'child_blob_get'):
                try:
                    refusal = response.json()
                except ValueError:
                    refusal = None
                code = (refusal.get('code') or refusal.get('error')) if isinstance(refusal, dict) else None
                if (response.status_code == 503 and code == 'managed_agent_children_unavailable'
                        or response.status_code == 400 and code == 'agent_bridge_unavailable'):
                    raise AgentChildrenUnavailable('Durable child operations are unavailable for this managed runtime')
            if action in ('peer_send', 'peer_receive'):
                try:
                    refusal = response.json()
                except ValueError:
                    refusal = None
                code = (refusal.get('code') or refusal.get('error')) if isinstance(refusal, dict) else None
                if (response.status_code == 503 and code == 'managed_agent_peer_messages_unavailable'
                        or response.status_code == 400 and code == 'agent_bridge_unavailable'):
                    raise AgentMessagesUnavailable('Peer messages are unavailable for this managed group or runtime')
            if response.status_code >= 500:
                if attempt == 2:
                    raise StepOutcomeUnknown('Journal acknowledgement unavailable')
                continue
            try:
                value = response.json()
            except ValueError:
                raise StepOutcomeUnknown('Invalid journal response') from None
            if not isinstance(value, dict):
                raise StepOutcomeUnknown('Invalid journal response')
            if response.status_code >= 300:
                code = value.get('code') or value.get('error')
                if not isinstance(code, str):
                    code = 'unavailable'
                error = {'step_definition_conflict': StepDefinitionConflict,
                         'idempotency_conflict': StepDefinitionConflict,
                         'step_result_expired': StepResultExpired,
                         'managed_agent_child_result_expired': StepResultExpired,
                         'managed_agent_child_blob_expired': StepResultExpired,
                         'managed_artifact_expired': StepResultExpired,
                         'step_failed': StepFailed}.get(code, StepOutcomeUnknown)
                raise error('Agent journal refused operation: ' + (code if isinstance(code, str) and _ID.fullmatch(code) else 'unavailable'))
            return value
        raise StepOutcomeUnknown('Journal acknowledgement unavailable')


class _Session:
    def __init__(self, rpc: _RPC, receipt: dict[str, Any], run_id: str):
        token, epoch = receipt.get('session_token'), receipt.get('epoch')
        if not isinstance(token, str) or not token or type(epoch) is not int or epoch <= 0:
            raise StepOutcomeUnknown('Invalid session receipt')
        self.recovery_policy = receipt.get('recovery_policy', '')
        if self.recovery_policy not in ('', 'checkpoint-v1') or self.recovery_policy and not _managed.get():
            raise StepOutcomeUnknown('Unsupported managed recovery policy')
        self.checkpoint_id = receipt.get('checkpoint_id')
        if self.checkpoint_id is not None and (not isinstance(self.checkpoint_id, str) or not _ID.fullmatch(self.checkpoint_id)):
            raise StepOutcomeUnknown('Invalid checkpoint identity')
        self.rpc = rpc
        self.scope = {'run_id': run_id, 'session_token': token, 'epoch': epoch}
        self.peer_messages_version = receipt.get('peer_messages_version')
        self.peer_group_id = receipt.get('peer_group_id')
        self.peer_task_key = receipt.get('peer_task_key')
        self.guard = threading.Lock()
        self.stopped = threading.Event()
        self.failed = threading.Event()
        self.expiry = receipt.get('expires_at')
        self.thread = threading.Thread(target=self._renew, daemon=True)
        self.thread.start()

    def _renew(self):
        while not self.stopped.is_set():
            try:
                expiry = datetime.fromisoformat(self.expiry.replace('Z', '+00:00'))
                remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
                if remaining <= 0:
                    self.failed.set()
                    return
                if self.stopped.wait(min(10, max(.05, remaining / 3))):
                    return
                receipt = self.rpc.call('renew', self.scope)
                if receipt.get('epoch') != self.scope['epoch'] or receipt.get('session_token') != self.scope['session_token']:
                    raise StepOutcomeUnknown('Session ownership changed')
                self.expiry = receipt.get('expires_at')
            except Exception:
                self.failed.set()
                return

    def healthy(self):
        if self.failed.is_set():
            raise StepOutcomeUnknown('Agent session authority could not be renewed')

    def checkpoint(self, operation: str, operation_id: str, **values) -> dict[str, str]:
        try:
            return self._checkpoint(operation, operation_id, **values)
        except BaseException:
            self.failed.set()
            raise

    def _checkpoint(self, operation: str, operation_id: str, **values) -> dict[str, str]:
        if not self.recovery_policy:
            return {}
        self.healthy()
        receipt = self.rpc.call('checkpoint_begin', {**self.scope, 'request_id': uuid.uuid4().hex,
                                'operation': operation, 'operation_id': operation_id, **values})
        checkpoint_id = receipt.get('checkpoint_id')
        if operation == 'child_cancel' and receipt.get('status') == 'legacy_replay' and checkpoint_id == '':
            # Only an existing cancellation may predate checkpoint capture.
            # The effect endpoint still verifies its original key and target.
            return {}
        if not isinstance(checkpoint_id, str) or not _ID.fullmatch(checkpoint_id):
            raise StepOutcomeUnknown('Invalid checkpoint identity')
        deadline = time.monotonic() + 45 * 60
        while True:
            self.healthy()
            if receipt.get('checkpoint_id') != checkpoint_id:
                raise StepOutcomeUnknown('Checkpoint acknowledgement changed identity')
            status = receipt.get('status')
            if status == 'committed' or status == 'ready' and operation != 'baseline':
                return {'checkpoint_id': checkpoint_id}
            if status == 'failed':
                code = receipt.get('failure_code')
                if not isinstance(code, str) or not _ID.fullmatch(code):
                    code = 'agent_checkpoint_failed'
                raise StepOutcomeUnknown('State checkpoint failed: ' + code)
            if status not in ('pending', 'ready') or time.monotonic() >= deadline:
                raise StepOutcomeUnknown('State checkpoint was not durably acknowledged')
            # Keep a pause when an older or saturated server returns immediately.
            if self.stopped.wait(.25):
                raise StepOutcomeUnknown('Checkpoint observation stopped')
            request = {**self.scope, 'checkpoint_id': checkpoint_id}
            wait_max = receipt.get('status_wait_max_ms')
            if type(wait_max) is int and 0 < wait_max <= 4000:
                try:
                    expiry = datetime.fromisoformat(self.expiry.replace('Z', '+00:00'))
                    remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
                except (AttributeError, TypeError, ValueError):
                    remaining = 0
                # The private transport serializes renewal and up to three
                # ten-second attempts. Leave renewal headroom after all of them.
                wait_ms = min(wait_max, max(0, int((deadline - time.monotonic()) * 1000)))
                if remaining >= 40 and wait_ms > 0:
                    request.update(wait_ms=wait_ms, known_status=status)
            receipt = self.rpc.call('checkpoint_status', request)

    def close(self):
        self.stopped.set()
        self.thread.join(31)
        self.rpc.close()


def resume(entrypoint, *, run_id: str, version: str, name: str | None = None):
    """Replay a registered run's completed steps using its original JSON input."""
    if _current.get() is not None or inspect.iscoroutinefunction(entrypoint):
        raise ValidationError('Agent resume supports one synchronous serial driver')
    run_id, version = _id(run_id), _id(version)
    name = _id(name or entrypoint.__name__)
    rpc = _RPC()
    session = None
    context_token = None
    try:
        receipt = rpc.call('session', {'run_id': run_id, 'request_id': uuid.uuid4().hex, 'peer_messages_version': 1})
        run = receipt.get('run')
        if not isinstance(run, dict) or run.get('run_id') != run_id or run.get('name') != name or run.get('version') != version:
            raise StepDefinitionConflict('Entrypoint identity differs from the registered run')
        if run.get('status') == 'expired':
            raise StepResultExpired('Agent run payload expired')
        if run.get('status') == 'completed':
            return decode(run.get('result'))
        if run.get('status') not in ('active', 'blocked'):
            raise StepOutcomeUnknown('Agent run cannot resume')
        original = decode(run.get('input'))
        session = _Session(rpc, receipt, run_id)
        context_token = _current.set(session)
        if session.recovery_policy and not session.checkpoint_id:
            session.checkpoint('baseline', run_id)
        result = entrypoint(original)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise ValidationError('Agent entrypoint must return JSON synchronously')
        session.healthy()
        encoded = encode(result)
        checkpoint = session.checkpoint('finish', run_id)
        done = rpc.call('finish', {**session.scope, 'request_id': uuid.uuid4().hex, 'result': encoded, **checkpoint})
        if done.get('status') != 'completed' or done.get('result') != encoded:
            raise StepOutcomeUnknown('Run completion was not durably acknowledged')
        return decode(done['result'])
    finally:
        if context_token is not None:
            _current.reset(context_token)
        if session is not None:
            session.close()
        else:
            rpc.close()


class _YieldExecution(BaseException):
    """End an acknowledged wait or continuation without completing the run."""


def _control(action, body):
    session = _current.get()
    if not _managed.get() or session is None or _step_context.get() is not None:
        raise ValidationError('Durable waits require a managed driver outside a step')
    if not session.guard.acquire(blocking=False):
        raise ValidationError('The managed driver must execute serially')
    try:
        session.healthy()
        operation_id = body['wait_id'] if action == 'wait' else body['continuation_id']
        checkpoint = session.checkpoint(action, operation_id)
        return session.rpc.call(action, {**session.scope, 'request_id': uuid.uuid4().hex, **body, **checkpoint})
    finally:
        session.guard.release()


def wait_for_event(name: str, *, wait_id: str):
    """Release compute until the named event is durably available for replay."""
    receipt = _control('wait', {'wait_id': _id(wait_id), 'kind': 'signal', 'signal': _id(name)})
    if receipt.get('decision') == 'waiting':
        raise _YieldExecution()
    if receipt.get('decision') != 'replay':
        raise StepOutcomeUnknown('Event wait was not durably acknowledged')
    return decode(receipt.get('result'))


def sleep_until(when: datetime, *, wait_id: str):
    """Release compute until an explicit timezone-aware wake time."""
    if not isinstance(when, datetime) or when.tzinfo is None:
        raise ValidationError('sleep_until requires a timezone-aware datetime')
    receipt = _control('wait', {'wait_id': _id(wait_id), 'kind': 'timer', 'wake_at': when.isoformat()})
    if receipt.get('decision') == 'waiting':
        raise _YieldExecution()
    if receipt.get('decision') != 'replay':
        raise StepOutcomeUnknown('Timer wait was not durably acknowledged')
    return decode(receipt['result']) if receipt.get('result') else None


def continue_as_new(input: Any, *, continuation_id: str):
    """Commit the next segment input and release this execution."""
    receipt = _control('continue', {'continuation_id': _id(continuation_id), 'input': encode(input)})
    if receipt.get('decision') != 'continued':
        raise StepOutcomeUnknown('Continuation was not durably acknowledged')
    raise _YieldExecution()


def put_blob(data: bytes, *, storage: str = 'database') -> dict:
    """Commit large bytes and return an immutable reference suitable for a step result."""
    from .agent_runtime import put_blob as put
    return put(data, storage=storage)


def get_blob(reference: dict) -> bytes:
    """Read and verify bytes referenced by a committed managed blob."""
    from .agent_runtime import get_blob as get
    return get(reference)


from ._agent_children import (
    ChildReference, ChildOutcome, ChildCompletions, ChildCancellation,
    spawn_child, await_children, next_child_completions, cancel_child, child_result, get_child_blob,
)

from ._agent_messages import MessageReceipt, PeerMessage, send_message, receive_messages

from ._agent_broker import complete_model, read_blob_chunk
def model(messages: list[dict[str, str]], *, call_id: str, max_output_tokens: int,
          model: str | None = None, system: str | None = None) -> dict:
    """Read a durable hosted model response under a stable call ID inside a managed step."""
    from ._agent_models import request
    return request(messages, call_id=call_id, max_output_tokens=max_output_tokens,
                   model=model, system=system)
