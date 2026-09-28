"""Bounded peer messages with durable send and receive replay identities."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import re
import uuid
from typing import Any

from . import _agent
from .errors import AgentMessagesUnavailable, StepOutcomeUnknown, ValidationError

_MAX_PAYLOAD = 16 << 10
_MAX_PAGE = 32
_HASH = re.compile(r'^[a-f0-9]{64}$')
_IDENTITIES = ('message_id', 'group_id', 'from_run_id', 'to_run_id',
               'from_task_key', 'to_task_key', 'message_key')
_FIELDS = {*_IDENTITIES, 'payload_hash'}


@dataclass(frozen=True)
class MessageReceipt:
    """Immutable acknowledgement of a message recorded for its group recipient."""
    message_id: str
    group_id: str
    from_run_id: str
    to_run_id: str
    from_task_key: str
    to_task_key: str
    message_key: str
    payload_hash: str

    def to_dict(self) -> dict:
        return {key: getattr(self, key) for key in (*_IDENTITIES, 'payload_hash')}


@dataclass(frozen=True)
class PeerMessage(MessageReceipt):
    """A recorded peer message with independently decoded JSON on each read."""
    _encoded_payload: str = field(repr=False)

    def payload(self) -> Any:
        """Return the verified JSON payload without changing the recorded message."""
        return _agent.decode(self._encoded_payload)

    def to_dict(self) -> dict:
        return {**super().to_dict(), 'payload': self.payload()}


@contextmanager
def _session():
    session = _agent._current.get()
    if not _agent._managed.get() or session is None or _agent._step_context.get() is not None:
        raise ValidationError('Peer messages require an assigned managed driver outside a step')
    if not session.guard.acquire(blocking=False):
        raise ValidationError('The managed driver must execute serially')
    try:
        session.healthy()
        if (type(session.peer_messages_version) is not int or session.peer_messages_version != 1
                or not isinstance(session.peer_group_id, str) or not _agent._ID.fullmatch(session.peer_group_id)
                or not isinstance(session.peer_task_key, str) or not _agent._ID.fullmatch(session.peer_task_key)):
            raise AgentMessagesUnavailable('Peer messages are unavailable for this managed group or runtime')
        yield session
    except StepOutcomeUnknown:
        session.failed.set()
        raise
    finally:
        session.guard.release()


def _identity(value: dict, session) -> dict:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValidationError('A peer message requires its complete immutable identity')
    for key in _IDENTITIES:
        _agent._id(value[key])
    if (value['group_id'] != session.peer_group_id or not isinstance(value['payload_hash'], str)
            or not _HASH.fullmatch(value['payload_hash'])):
        raise ValidationError('Peer message group or payload hash differs')
    return value


def _call(session, action, operation_id, body):
    saved = session.checkpoint(action, operation_id)
    return session.rpc.call(action, {**session.scope, 'request_id': uuid.uuid4().hex, **body, **saved})


def send_message(to_task_key: str, payload: Any, *, message_key: str) -> MessageReceipt:
    """Record bounded JSON for a task in the same group using a stable send key."""
    to_task_key, message_key = _agent._id(to_task_key), _agent._id(message_key)
    encoded = _agent.encode(payload)
    data = base64.b64decode(encoded)
    if len(data) > _MAX_PAYLOAD:
        raise ValidationError('A peer message payload must contain at most 16 KiB of JSON')
    digest = hashlib.sha256(data).hexdigest()
    body = {'to_task_key': to_task_key, 'message_key': message_key, 'payload': encoded}
    with _session() as session:
        receipt = _call(session, 'peer_send', message_key, body)
        try:
            identity = _identity({key: value for key, value in receipt.items() if key != 'decision'}, session)
            if (receipt.get('decision') not in ('accepted', 'replay')
                    or identity['from_run_id'] != session.scope['run_id']
                    or identity['from_task_key'] != session.peer_task_key
                    or identity['to_task_key'] != to_task_key or identity['message_key'] != message_key
                    or identity['payload_hash'] != digest):
                raise ValidationError('Peer send acknowledgement changed identity or content')
            return MessageReceipt(**identity)
        except ValidationError:
            raise StepOutcomeUnknown('Peer message send could not be verified') from None


def receive_messages(*, wait_id: str, limit: int = 10) -> tuple[PeerMessage, ...]:
    """Yield for a frozen inbox batch and return the same messages when replayed."""
    wait_id = _agent._id(wait_id)
    if type(limit) is not int or not 1 <= limit <= _MAX_PAGE:
        raise ValidationError('Peer message limit must be between 1 and 32')
    with _session() as session:
        receipt = _call(session, 'peer_receive', wait_id, {'wait_id': wait_id, 'limit': limit})
        try:
            if len(json.dumps(receipt, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()) > 1 << 20:
                raise ValueError('oversized message page')
            if receipt.get('wait_id') != wait_id or set(receipt) - {'decision', 'wait_id', 'messages'}:
                raise ValueError('receive identity differs')
            rows = receipt.get('messages')
            if receipt.get('decision') == 'waiting':
                if rows not in (None, []):
                    raise ValueError('waiting receipt contains messages')
                raise _agent._YieldExecution()
            if receipt.get('decision') != 'replay' or not isinstance(rows, list) or not 1 <= len(rows) <= limit:
                raise ValueError('invalid message page')
            messages = []
            for row in rows:
                if not isinstance(row, dict) or set(row) != _FIELDS | {'payload'}:
                    raise ValueError('invalid message envelope')
                identity = _identity({key: value for key, value in row.items() if key != 'payload'}, session)
                if identity['to_run_id'] != session.scope['run_id'] or identity['to_task_key'] != session.peer_task_key:
                    raise ValueError('message belongs to another recipient')
                wire = row['payload']
                if not isinstance(wire, str) or len(wire) > ((_MAX_PAYLOAD + 2) // 3) * 4:
                    raise ValueError('invalid payload size')
                data = base64.b64decode(wire, validate=True)
                if len(data) > _MAX_PAYLOAD or hashlib.sha256(data).hexdigest() != identity['payload_hash']:
                    raise ValueError('payload differs from its immutable hash')
                _agent.decode(wire)
                messages.append(PeerMessage(**identity, _encoded_payload=wire))
            if (len({item.message_id for item in messages}) != len(messages)
                    or len({(item.from_run_id, item.message_key) for item in messages}) != len(messages)):
                raise ValueError('duplicate message')
            return tuple(messages)
        except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError):
            raise StepOutcomeUnknown('Peer message batch could not be verified') from None
