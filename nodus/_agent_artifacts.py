"""Explicit object-backed artifact operations for an assigned managed driver."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import random
import re
import threading
import time

from . import _agent
from .errors import ValidationError, StepOutcomeUnknown

CHUNK_BYTES = 256 << 10
MAX_BYTES = 32 << 20
_UPLOAD_TIMEOUT = 30 * 60


def reference(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {'version', 'id', 'sha256', 'bytes'}:
        raise ValidationError('Object artifact references require version, id, sha256 and bytes')
    if type(value['version']) is not int or value['version'] != 2:
        raise ValidationError('Unsupported object artifact version')
    digest, size = value['sha256'], value['bytes']
    if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest) or value['id'] != 'bl2_' + digest:
        raise ValidationError('Object artifact identity must match its content hash')
    if type(size) is not int or not 0 <= size <= MAX_BYTES:
        raise ValidationError('Object artifact size exceeds 32 MiB')
    return dict(value)


@contextmanager
def _session():
    session = _agent._current.get()
    if not _agent._managed.get() or session is None:
        raise ValidationError('Object artifacts require the assigned managed driver')
    step = _agent._step_context.get()
    acquired = False
    if step is not None:
        owner = getattr(session, '_step_owner', None)
        if owner is None or owner[0] != threading.get_ident() or owner[1] is not step:
            raise ValidationError('Object artifacts must use the owning serial step')
    else:
        acquired = session.guard.acquire(blocking=False)
        if not acquired:
            raise ValidationError('The managed driver must execute serially')
    try:
        session.healthy()
        yield session
    except StepOutcomeUnknown:
        session.failed.set()
        raise
    finally:
        if acquired:
            session.guard.release()


def _receipt(value, expected):
    try:
        if not isinstance(value, dict) or reference(value.get('reference')) != expected:
            raise ValueError('different reference')
        status, offset = value.get('status'), value.get('next_offset')
        if status not in ('uploading', 'verifying', 'committed') or type(offset) is not int:
            raise ValueError('invalid progress')
        if not 0 <= offset <= expected['bytes'] or offset % CHUNK_BYTES and offset != expected['bytes']:
            raise ValueError('invalid offset')
        if status in ('verifying', 'committed') and offset != expected['bytes']:
            raise ValueError('incomplete verification')
    except (ValidationError, ValueError, TypeError):
        raise StepOutcomeUnknown('Object artifact acknowledgement differs from its immutable reference') from None
    return value


def _call(session, action, expected, **values):
    session.healthy()
    response = session.rpc.call(action, {**session.scope, 'version': 2, 'blob_id': expected['id'],
                                        'sha256': expected['sha256'], 'bytes': expected['bytes'], **values})
    return _receipt(response, expected)


def _pause(session, deadline, attempt):
    remaining = deadline - time.monotonic()
    if remaining <= 0 or session.stopped.is_set():
        raise StepOutcomeUnknown('Object artifact acknowledgement is pending, retry the same bytes after resuming')
    session.healthy()
    delay = min(1.0, .05 * 2 ** min(attempt, 5)) * random.uniform(.75, 1.25)
    if session.stopped.wait(min(remaining, delay)):
        raise StepOutcomeUnknown('Object artifact upload stopped before acknowledgement')


def put(data: bytes) -> dict:
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        raise ValidationError('put_blob requires bytes of at most 32 MiB')
    digest = hashlib.sha256(data).hexdigest()
    expected = {'version': 2, 'id': 'bl2_' + digest, 'sha256': digest, 'bytes': len(data)}
    with _session() as session:
        deadline = time.monotonic() + _UPLOAD_TIMEOUT
        receipt = _call(session, 'blob_begin', expected)
        offset = receipt['next_offset']
        while receipt['status'] == 'uploading' and offset < len(data):
            if time.monotonic() >= deadline:
                raise StepOutcomeUnknown('Object artifact acknowledgement is pending, retry the same bytes after resuming')
            target = min(offset + CHUNK_BYTES, len(data))
            receipt = _call(session, 'blob_put', expected, offset=offset,
                            data=base64.b64encode(data[offset:target]).decode('ascii'))
            attempt = 0
            while receipt['next_offset'] < target:
                if receipt['next_offset'] != offset or receipt['status'] != 'uploading':
                    raise StepOutcomeUnknown('Object artifact verification moved behind its acknowledged offset')
                _pause(session, deadline, attempt)
                attempt += 1
                receipt = _call(session, 'blob_begin', expected)
            offset = receipt['next_offset']
        if receipt['status'] == 'committed':
            return expected
        receipt = _call(session, 'blob_commit', expected)
        attempt = 0
        while receipt['status'] != 'committed':
            if receipt['status'] != 'verifying':
                raise StepOutcomeUnknown('Object artifact commit was not durably acknowledged')
            _pause(session, deadline, attempt)
            attempt += 1
            receipt = _call(session, 'blob_begin', expected)
        return expected


def checked_chunk(receipt, expected, offset):
    _receipt(receipt, expected)
    try:
        encoded = receipt.get('data', '')
        if not isinstance(encoded, str) or len(encoded) > ((CHUNK_BYTES + 2) // 3) * 4:
            raise ValueError('oversized chunk')
        chunk = base64.b64decode(encoded, validate=True)
    except (TypeError, ValueError):
        raise StepOutcomeUnknown('Object artifact returned invalid bytes') from None
    size = min(CHUNK_BYTES, expected['bytes'] - offset)
    eof = offset + len(chunk) == expected['bytes']
    if (receipt.get('status') != 'committed' or type(receipt.get('offset', 0)) is not int
            or receipt.get('offset', 0) != offset or len(chunk) != size
            or type(receipt.get('eof', False)) is not bool or receipt.get('eof', False) != eof):
        raise StepOutcomeUnknown('Object artifact returned incomplete or reordered bytes')
    return chunk, eof


def get(value: dict) -> bytes:
    expected = reference(value)
    with _session() as session:
        output = bytearray()
        while True:
            receipt = _call(session, 'blob_get', expected, offset=len(output))
            chunk, eof = checked_chunk(receipt, expected, len(output))
            output.extend(chunk)
            if eof:
                if hashlib.sha256(output).hexdigest() != expected['sha256']:
                    raise StepOutcomeUnknown('Object artifact content hash differs from its reference')
                return bytes(output)
