"""Fixed, quota-owned model and blob reads for assigned managed drivers."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import math
import re
import time
import uuid

from . import _agent
from .errors import BrokerRefused, StepOutcomeUnknown, StepResultExpired, ValidationError

_MAX_RESULT = 512 << 10
_MAX_INTENT = 64 << 10
_CHUNK = 256 << 10
_HASH = re.compile(r'^[a-f0-9]{64}$')
_MODEL = 'nodus-completion-v1'
_BLOB = 'nodus-blob-read-v1'


@contextmanager
def _session():
    session = _agent._current.get()
    if not _agent._managed.get() or session is None or _agent._step_context.get() is not None:
        raise ValidationError('Broker operations require an assigned managed driver outside a step')
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


def _decode(receipt):
    value, digest = receipt.get('result'), receipt.get('result_sha256')
    try:
        if not isinstance(value, str) or len(value) > ((_MAX_RESULT + 2) // 3) * 4:
            raise ValueError('invalid result size')
        raw = base64.b64decode(value, validate=True)
        if len(raw) > _MAX_RESULT or not isinstance(digest, str) or not _HASH.fullmatch(digest) or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('invalid result hash')
        def pairs(items):
            out = {}
            for key, item in items:
                if key in out:
                    raise ValueError('duplicate result key')
                out[key] = item
            return out
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite result')))
        if not isinstance(result, dict):
            raise ValueError('invalid result shape')
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise StepOutcomeUnknown('Broker result could not be verified') from exc


def _invoke(intent, invocation_key, timeout):
    key = _agent._id(invocation_key)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 900:
        raise ValidationError('Broker timeout must be between zero and 900 seconds')
    try:
        raw = json.dumps(intent, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, UnicodeError, TypeError) as exc:
        raise ValidationError('Broker request must contain bounded UTF-8 values') from exc
    if len(raw) > _MAX_INTENT:
        raise ValidationError('Broker request exceeds 64 KiB')
    with _session() as session:
        read = {**session.scope, 'protocol_version': 1, 'request_id': uuid.uuid4().hex, 'invocation_key': key}
        # This authoritative read detects an older local action transport before
        # checkpoint capture. A completed receipt still goes through invoke so
        # the server checks the immutable intent and checkpoint on every replay.
        known_id = None
        try:
            known = session.rpc.call('broker_status', read)
            known_id = known.get('invocation_id')
            if (type(known.get('protocol_version')) is not int or known['protocol_version'] != 1
                    or not isinstance(known_id, str) or not _agent._ID.fullmatch(known_id)
                    or known.get('invocation_key') != key):
                raise StepOutcomeUnknown('Broker status could not be verified')
        except _agent._BrokerMissing:
            pass
        checkpoint = session.checkpoint('broker', key)
        request = {**read, **intent, **checkpoint}
        receipt = session.rpc.call('broker_invoke', request)
        deadline = time.monotonic() + timeout
        identity = known_id
        while True:
            session.healthy()
            current = receipt.get('invocation_id')
            if (type(receipt.get('protocol_version')) is not int or receipt['protocol_version'] != 1
                    or not isinstance(current, str) or not _agent._ID.fullmatch(current)
                    or identity is not None and current != identity
                    or receipt.get('invocation_key') != key or receipt.get('kind') != intent['kind']):
                raise StepOutcomeUnknown('Broker receipt changed identity or protocol')
            identity = current
            state = receipt.get('state')
            if state in ('completed', 'rejected'):
                result = _decode(receipt)
                if state == 'rejected':
                    problem = result.get('error')
                    allowed = {'model_capacity_exceeded': True, 'blob_unavailable': False}
                    if (set(result) != {'error'} or not isinstance(problem, dict) or set(problem) != {'code', 'retryable'}
                            or not isinstance(problem.get('code'), str) or problem['code'] not in allowed
                            or type(problem.get('retryable')) is not bool
                            or problem['retryable'] is not allowed[problem['code']]
                            or intent['kind'] == _MODEL and problem['code'] != 'model_capacity_exceeded'
                            or intent['kind'] == _BLOB and problem['code'] != 'blob_unavailable'):
                        raise StepOutcomeUnknown('Broker refusal could not be verified')
                    raise BrokerRefused(problem['code'], retryable=problem['retryable'])
                return result
            if receipt.get('result') or receipt.get('result_sha256'):
                raise StepOutcomeUnknown('Unfinished broker invocation returned a result')
            if state == 'expired':
                raise StepResultExpired('Broker result is no longer retained')
            if state == 'cancelled':
                raise BrokerRefused('broker_cancelled_before_dispatch', retryable=True)
            if state == 'unknown':
                raise StepOutcomeUnknown('Broker outcome is unknown and cannot be retried with a new invocation key')
            if state not in ('waiting', 'reserved', 'claimed', 'dispatched') or time.monotonic() >= deadline:
                raise StepOutcomeUnknown('Broker invocation did not finish within its observation deadline')
            # Renewal uses the same serial private transport during this pause.
            if session.stopped.wait(.25):
                raise StepOutcomeUnknown('Broker observation stopped')
            try:
                receipt = session.rpc.call('broker_status', read)
            except _agent._BrokerMissing:
                raise StepOutcomeUnknown('Accepted broker identity is unavailable') from None


def complete_model(prompt: str, *, profile_id: str, invocation_key: str,
                   max_output_tokens: int, timeout: float = 900) -> dict:
    """Request one bounded completion through an enabled, qualified model profile.

    Reuse the invocation key when replaying the same request. Quota waits retain
    the managed workload's compute. Unknown outcomes never trigger another call.
    """
    if not isinstance(prompt, str) or not prompt or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 8192:
        raise ValidationError('Completion requires text and an output limit between 1 and 8192 tokens')
    result = _invoke({'kind': _MODEL, 'profile_id': _agent._id(profile_id), 'prompt': prompt,
                      'max_output_tokens': max_output_tokens}, invocation_key, timeout)
    if set(result) != {'text', 'finish_reason'} or not isinstance(result['text'], str) or result['finish_reason'] not in ('stop', 'length'):
        _invalid_result('Completion result could not be verified')
    return result


def read_blob_chunk(reference: dict, *, invocation_key: str, offset: int = 0,
                    child_run_id: str | None = None, timeout: float = 900) -> bytes:
    """Read one quota-owned chunk from this run or an exact direct child's blob."""
    if not isinstance(reference, dict) or set(reference) != {'id', 'sha256', 'bytes'}:
        raise ValidationError('Broker reads require an exact database blob reference')
    digest, size = reference['sha256'], reference['bytes']
    if (not isinstance(digest, str) or not _HASH.fullmatch(digest) or reference['id'] != 'bl_' + digest
            or type(size) is not int or not 0 <= size <= 32 << 20
            or type(offset) is not int or not 0 <= offset <= size or offset % _CHUNK):
        raise ValidationError('Blob reference or chunk offset is invalid')
    blob = {'reference': dict(reference), 'offset': offset}
    if child_run_id is not None:
        blob['child_run_id'] = _agent._id(child_run_id)
    result = _invoke({'kind': _BLOB, 'blob': blob}, invocation_key, timeout)
    try:
        if (not isinstance(result.get('reference'), dict) or type(result['reference'].get('bytes')) is not int
                or set(result) - {'reference', 'status', 'offset', 'data', 'eof', 'child_run_id'}
                or result.get('reference') != reference or result.get('status') != 'committed'
                or type(result.get('offset', 0)) is not int or result.get('offset', 0) != offset
                or result.get('child_run_id') != child_run_id):
            raise ValueError('changed reference')
        data = result.get('data', '')
        if not isinstance(data, str) or len(data) > ((_CHUNK + 2) // 3) * 4:
            raise ValueError('invalid data')
        raw = base64.b64decode(data, validate=True)
        if (len(raw) != min(_CHUNK, size - offset) or type(result.get('eof', False)) is not bool
                or result.get('eof', False) != (offset + len(raw) == size)):
            raise ValueError('incomplete chunk')
        return raw
    except (ValueError, TypeError) as exc:
        _invalid_result('Broker blob result could not be verified', exc)


def _invalid_result(message, cause=None):
    session = _agent._current.get()
    if session is not None:
        session.failed.set()
    raise StepOutcomeUnknown(message) from cause
