"""Bounded child references and serial controls for assigned managed drivers."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid
from typing import Any

from . import _agent
from .errors import StepOutcomeUnknown, ValidationError

_MAX_PAGE = 100
_MAX_METADATA = 64 << 10
_MAX_SEQUENCE = (1 << 63) - 1
_HASH = re.compile(r'^[a-f0-9]{64}$')
_CURSOR = re.compile(r'^(0|[1-9][0-9]{0,18})$')


@dataclass(frozen=True)
class ChildReference:
    """An immutable direct child identity, independent of physical attempts."""
    run_id: str
    parent_run_id: str
    spawn_key: str
    group_id: str
    depth: int

    def to_dict(self) -> dict:
        return asdict(self)

    def cancel(self, *, cancel_key: str) -> ChildCancellation:
        return cancel_child(self, cancel_key=cancel_key)

    def get_blob(self, reference: dict) -> bytes:
        return get_child_blob(self, reference)


@dataclass(frozen=True)
class ChildOutcome:
    """A terminal child event whose result is fetched and verified separately."""
    event_id: str
    sequence: int
    child_run_id: str
    spawn_key: str
    status: str
    result_hash: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict:
        return {key: value for key, value in asdict(self).items() if value is not None}

    def result(self) -> Any:
        return child_result(self)

    def get_blob(self, reference: dict) -> bytes:
        return get_child_blob(self, reference)


@dataclass(frozen=True)
class ChildCompletions:
    """A frozen page and cursor that can be replayed without consuming events."""
    outcomes: tuple[ChildOutcome, ...]
    next_after: str
    exhausted: bool


@dataclass(frozen=True)
class ChildCancellation:
    """Acknowledgement of a cancellation request, separate from final cleanup."""
    decision: str
    child_run_id: str
    cancel_key: str
    cancel_requested_at: datetime

    def to_dict(self) -> dict:
        return {**asdict(self), 'cancel_requested_at': self.cancel_requested_at.isoformat()}


@contextmanager
def _session():
    session = _agent._current.get()
    if not _agent._managed.get() or session is None or _agent._step_context.get() is not None:
        raise ValidationError('Child controls require an assigned managed driver outside a step')
    if not session.guard.acquire(blocking=False):
        raise ValidationError('The managed driver must execute serially')
    try:
        session.healthy()
        yield session
    except StepOutcomeUnknown:
        session.failed.set()
        raise
    finally:
        session.guard.release()


def _reference(value) -> ChildReference:
    if isinstance(value, ChildReference):
        value = value.to_dict()
    if not isinstance(value, dict) or set(value) != {'run_id', 'parent_run_id', 'spawn_key', 'group_id', 'depth'}:
        raise ValidationError('A child requires its complete immutable reference')
    for key in ('run_id', 'parent_run_id', 'spawn_key', 'group_id'):
        _agent._id(value[key])
    if type(value['depth']) is not int or not 1 <= value['depth'] <= 8 or value['run_id'] == value['parent_run_id']:
        raise ValidationError('Child lineage is invalid')
    return ChildReference(**value)


def _outcome(value) -> ChildOutcome:
    if isinstance(value, ChildOutcome):
        value = value.to_dict()
    if not isinstance(value, dict) or set(value) - {
            'event_id', 'sequence', 'child_run_id', 'spawn_key', 'status', 'result_hash', 'reason'}:
        raise ValidationError('A child outcome requires a bounded terminal event')
    for key in ('event_id', 'child_run_id', 'spawn_key'):
        _agent._id(value.get(key))
    sequence = value.get('sequence')
    if type(sequence) is not int or not 1 <= sequence <= _MAX_SEQUENCE:
        raise ValidationError('Child event sequence is invalid')
    status, digest, reason = value.get('status'), value.get('result_hash'), value.get('reason')
    if status == 'completed':
        if not isinstance(digest, str) or not _HASH.fullmatch(digest) or reason not in (None, ''):
            raise ValidationError('Completed child requires its committed result hash')
    elif status == 'cancelled':
        if digest not in (None, ''):
            raise ValidationError('Cancelled children do not have successful results')
        _agent._id(reason)
    else:
        raise ValidationError('Child outcome is not terminal')
    return ChildOutcome(value['event_id'], sequence, value['child_run_id'], value['spawn_key'], status,
                        digest or None, reason or None)


def _cursor(value) -> str:
    if not isinstance(value, str) or not _CURSOR.fullmatch(value) or int(value) > _MAX_SEQUENCE:
        raise ValidationError('Child cursor must be a canonical nonnegative sequence string')
    return value


def _owned(child: ChildReference, session):
    if child.parent_run_id != session.scope['run_id']:
        raise ValidationError('Child reference belongs to another parent')


def _call(session, action, body, *, checkpoint=None):
    saved = session.checkpoint(*checkpoint) if checkpoint else {}
    request = {**session.scope, 'request_id': uuid.uuid4().hex, **body, **saved}
    if len(json.dumps(request, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()) > 768 << 10:
        raise ValidationError('Child request exceeds the private channel limit')
    return session.rpc.call(action, request)


def spawn_child(input: Any, *, spawn_key: str, deadline: datetime | None = None,
                permissions: dict | None = None) -> ChildReference:
    """Admit one independently executing child using a stable replay key."""
    body = {'spawn_key': _agent._id(spawn_key), 'input': _agent.encode(input)}
    if deadline is not None:
        if not isinstance(deadline, datetime) or deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValidationError('Child deadline requires a timezone-aware datetime')
        body['deadline'] = deadline.astimezone(timezone.utc).isoformat()
    if permissions is not None:
        if not isinstance(permissions, dict) or set(permissions) - {'secrets', 'secret_refs', 'connections', 'egress_allow'}:
            raise ValidationError('Child permissions may only attenuate secrets, secret_refs, connections and egress_allow')
        selected = {}
        for key, values in permissions.items():
            if values is not None and (not isinstance(values, list) or len(values) > 64 or any(
                    not isinstance(value, str) or not value or value.strip() != value
                    or any(control in value for control in ('\x00', '\r', '\n')) for value in values)):
                raise ValidationError('Child permission fields require lists of nonempty strings or None')
            canonical = [value.lower() if key == 'egress_allow' else value for value in values] if values is not None else None
            if canonical is not None and len(set(canonical)) != len(canonical):
                raise ValidationError('Child permission lists must contain distinct values')
            try:
                if values is not None and any(len(value.encode('utf-8')) > 255 for value in values):
                    raise UnicodeError()
            except UnicodeError:
                raise ValidationError('Child permission values must contain at most 255 UTF-8 bytes') from None
            selected[key] = None if values is None else list(values)
        # Keep the complete envelope bounded before creating a checkpoint.
        _agent.encode(selected)
        body['permissions'] = selected
    with _session() as session:
        receipt = _call(session, 'child_spawn', body, checkpoint=('spawn', spawn_key))
        try:
            child = _reference(receipt.get('child'))
            _owned(child, session)
            if receipt.get('decision') not in ('admitted', 'replay') or child.spawn_key != spawn_key:
                raise ValidationError('Unexpected child admission identity')
        except ValidationError:
            raise StepOutcomeUnknown('Child admission was not durably acknowledged for the requested identity') from None
        return child


def _wait(session, body, expected=None) -> ChildCompletions:
    receipt = _call(session, 'children_wait', body, checkpoint=('children_wait', body['wait_id']))
    try:
        if len(json.dumps(receipt, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()) > _MAX_METADATA:
            raise ValueError('oversized metadata')
        if receipt.get('wait_id') != body['wait_id'] or receipt.get('mode') != body['mode']:
            raise ValueError('wait identity differs')
        if receipt.get('decision') == 'waiting':
            if receipt.get('outcomes'):
                raise ValueError('waiting receipt contains outcomes')
            raise _agent._YieldExecution()
        if receipt.get('decision') != 'replay' or type(receipt.get('exhausted')) is not bool:
            raise ValueError('wait not acknowledged')
        rows = receipt.get('outcomes')
        if not isinstance(rows, list) or len(rows) > body.get('limit', _MAX_PAGE):
            raise ValueError('invalid page size')
        outcomes = tuple(_outcome(row) for row in rows)
        for key in ('event_id', 'sequence', 'child_run_id', 'spawn_key'):
            if len({getattr(item, key) for item in outcomes}) != len(outcomes):
                raise ValueError('duplicate outcome')
        cursor = _cursor(receipt.get('next_after'))
        if body['mode'] == 'all':
            if [(item.child_run_id, item.spawn_key) for item in outcomes] != [(item.run_id, item.spawn_key) for item in expected]:
                raise ValueError('outcomes differ from requested children')
        else:
            sequences = [item.sequence for item in outcomes]
            if sequences != list(range(int(body['after']) + 1, int(body['after']) + 1 + len(outcomes))):
                raise ValueError('page skipped or reordered immutable events')
            if cursor != (str(sequences[-1]) if sequences else body['after']):
                raise ValueError('page cursor skips events')
            if not outcomes and not receipt['exhausted']:
                raise ValueError('empty nonterminal page')
        return ChildCompletions(outcomes, cursor, receipt['exhausted'])
    except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError):
        raise StepOutcomeUnknown('Child completion page could not be verified') from None


def await_children(children, *, wait_id: str) -> tuple[ChildOutcome, ...]:
    """Yield until all selected direct children have committed terminal outcomes."""
    if not isinstance(children, (list, tuple)) or len(children) > _MAX_PAGE:
        raise ValidationError('A child join accepts at most 100 explicit references')
    references = tuple(_reference(child) for child in children)
    if len({child.run_id for child in references}) != len(references):
        raise ValidationError('A child join requires distinct references')
    body = {'wait_id': _agent._id(wait_id), 'mode': 'all', 'child_run_ids': [child.run_id for child in references]}
    with _session() as session:
        for child in references:
            _owned(child, session)
        return _wait(session, body, references).outcomes


def next_child_completions(*, after: str = '0', wait_id: str, limit: int = 100) -> ChildCompletions:
    """Yield for the next frozen completion page after a parent-scoped cursor."""
    if type(limit) is not int or not 1 <= limit <= _MAX_PAGE:
        raise ValidationError('Child completion limit must be between 1 and 100')
    body = {'wait_id': _agent._id(wait_id), 'mode': 'next', 'after': _cursor(after), 'limit': limit}
    with _session() as session:
        return _wait(session, body)


def cancel_child(child: ChildReference | dict, *, cancel_key: str) -> ChildCancellation:
    """Request cancellation of a direct child's subtree without claiming cleanup."""
    child, cancel_key = _reference(child), _agent._id(cancel_key)
    with _session() as session:
        _owned(child, session)
        receipt = _call(session, 'child_cancel', {'child_run_id': child.run_id, 'cancel_key': cancel_key},
                        checkpoint=('child_cancel', cancel_key))
        try:
            raw = receipt.get('cancel_requested_at')
            when = datetime.fromisoformat(raw.replace('Z', '+00:00')) if isinstance(raw, str) else None
            if (receipt.get('decision') not in ('requested', 'replay') or receipt.get('child_run_id') != child.run_id
                    or receipt.get('cancel_key') != cancel_key or when is None or when.utcoffset() is None):
                raise ValueError('invalid cancellation receipt')
        except (ValueError, TypeError, OverflowError):
            raise StepOutcomeUnknown('Child cancellation request was not durably acknowledged') from None
        return ChildCancellation(receipt['decision'], child.run_id, cancel_key, when)


def child_result(outcome: ChildOutcome | dict) -> Any:
    """Fetch and verify a completed direct child's committed JSON result."""
    outcome = _outcome(outcome)
    if outcome.status != 'completed':
        raise ValidationError('A cancelled child does not have a successful result')
    with _session() as session:
        receipt = _call(session, 'child_result', {'child_run_id': outcome.child_run_id, 'result_hash': outcome.result_hash})
        if receipt.get('child_run_id') != outcome.child_run_id or receipt.get('result_hash') != outcome.result_hash:
            raise StepOutcomeUnknown('Child result identity differs from its completion reference')
        value = _agent.decode(receipt.get('result'))
        raw = base64.b64decode(receipt['result'], validate=True)
        if hashlib.sha256(raw).hexdigest() != outcome.result_hash:
            raise StepOutcomeUnknown('Child result content hash differs from its completion reference')
        return value


def get_child_blob(child: ChildReference | ChildOutcome | dict, reference: dict) -> bytes:
    """Read bytes committed in this direct child's own blob scope."""
    from .agent_runtime import _blob_reference, _BLOB_CHUNK
    reference = _blob_reference(reference)
    if isinstance(child, ChildOutcome) or isinstance(child, dict) and 'child_run_id' in child:
        child_id, owned = _outcome(child).child_run_id, None
    else:
        owned = _reference(child)
        child_id = owned.run_id
    with _session() as session:
        if owned is not None:
            _owned(owned, session)
        output = bytearray()
        while True:
            offset = len(output)
            receipt = _call(session, 'child_blob_get', {'child_run_id': child_id, 'blob_id': reference['id'],
                'sha256': reference['sha256'], 'bytes': reference['bytes'], 'offset': offset,
                **({'version': reference['version']} if 'version' in reference else {})})
            if receipt.get('child_run_id') != child_id or receipt.get('reference') != reference:
                raise StepOutcomeUnknown('Child blob identity differs from its scoped reference')
            if 'version' in reference:
                from ._agent_artifacts import checked_chunk
                chunk, eof = checked_chunk(receipt, reference, offset)
                output.extend(chunk)
                if eof:
                    if hashlib.sha256(output).hexdigest() != reference['sha256']:
                        raise StepOutcomeUnknown('Child artifact content hash differs from its reference')
                    return bytes(output)
                continue
            try:
                encoded = receipt.get('data', '')
                if not isinstance(encoded, str) or len(encoded) > ((_BLOB_CHUNK + 2) // 3) * 4:
                    raise ValueError('invalid chunk')
                chunk = base64.b64decode(encoded, validate=True)
            except (TypeError, ValueError):
                raise StepOutcomeUnknown('Child blob returned invalid bytes') from None
            expected = min(_BLOB_CHUNK, reference['bytes'] - offset)
            eof = offset + len(chunk) == reference['bytes']
            if (receipt.get('status') != 'committed' or type(receipt.get('offset', 0)) is not int
                    or receipt.get('offset', 0) != offset or len(chunk) != expected
                    or type(receipt.get('eof', False)) is not bool or receipt.get('eof', False) != eof):
                raise StepOutcomeUnknown('Child blob returned incomplete or reordered bytes')
            output.extend(chunk)
            if eof:
                if hashlib.sha256(output).hexdigest() != reference['sha256']:
                    raise StepOutcomeUnknown('Child blob content hash differs from its reference')
                return bytes(output)
