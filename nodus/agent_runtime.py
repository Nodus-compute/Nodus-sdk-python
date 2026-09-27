"""Launch the synchronous entrypoint assigned by a managed agent revision."""

import argparse
import base64
import hashlib
import importlib
import re

from . import _agent
from .errors import ValidationError, StepOutcomeUnknown


_BLOB_CHUNK = 256 << 10
_BLOB_MAX = 32 << 20


def _blob_reference(value):
    if isinstance(value, dict) and 'version' in value:
        from ._agent_artifacts import reference
        return reference(value)
    if not isinstance(value, dict) or set(value) != {'id', 'sha256', 'bytes'}:
        raise ValidationError('Blob references require id, sha256 and bytes')
    digest, size = value['sha256'], value['bytes']
    if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest) or value['id'] != 'bl_' + digest:
        raise ValidationError('Blob reference identity must match its content hash')
    if type(size) is not int or not 0 <= size <= _BLOB_MAX:
        raise ValidationError('Blob size exceeds the managed blob limit')
    return dict(value)


def _blob_call(action, reference, **values):
    session = _agent._current.get()
    if not _agent._managed.get() or session is None:
        raise ValidationError('Blob operations require the assigned managed driver')
    session.healthy()
    response = session.rpc.call(action, {**session.scope, 'blob_id': reference['id'],
                                        'sha256': reference['sha256'], 'bytes': reference['bytes'], **values})
    if response.get('reference') != reference:
        raise StepOutcomeUnknown('Blob acknowledgement differs from its immutable reference')
    return response


def put_blob(data: bytes, *, storage: str = 'database') -> dict:
    """Commit bounded bytes and return their verified immutable journal reference."""
    if storage == 'object':
        from ._agent_artifacts import put
        return put(data)
    if storage != 'database':
        raise ValidationError('Blob storage must be database or object')
    if not isinstance(data, bytes) or len(data) > _BLOB_MAX:
        raise ValidationError('put_blob requires bytes of at most 32 MiB')
    digest = hashlib.sha256(data).hexdigest()
    reference = {'id': 'bl_' + digest, 'sha256': digest, 'bytes': len(data)}
    receipt = _blob_call('blob_begin', reference)
    if receipt.get('status') == 'committed':
        return reference
    if receipt.get('status') != 'uploading':
        raise StepOutcomeUnknown('Blob upload was not durably acknowledged')
    for offset in range(0, len(data), _BLOB_CHUNK):
        receipt = _blob_call('blob_put', reference, offset=offset,
                             data=base64.b64encode(data[offset:offset + _BLOB_CHUNK]).decode())
        if receipt.get('status') not in ('uploading', 'committed'):
            raise StepOutcomeUnknown('Blob chunk was not durably acknowledged')
    receipt = _blob_call('blob_commit', reference)
    if receipt.get('status') != 'committed':
        raise StepOutcomeUnknown('Blob commit was not durably acknowledged')
    return reference


def get_blob(reference: dict) -> bytes:
    """Read a committed blob after verifying its full length and content hash."""
    reference = _blob_reference(reference)
    if 'version' in reference:
        from ._agent_artifacts import get
        return get(reference)
    output = bytearray()
    while True:
        offset = len(output)
        receipt = _blob_call('blob_get', reference, offset=offset)
        try:
            chunk = base64.b64decode(receipt.get('data', ''), validate=True)
        except (TypeError, ValueError):
            raise StepOutcomeUnknown('Blob returned invalid bytes') from None
        expected = min(_BLOB_CHUNK, reference['bytes'] - offset)
        eof = offset + len(chunk) == reference['bytes']
        if receipt.get('status') != 'committed' or receipt.get('offset', 0) != offset or len(chunk) != expected or bool(receipt.get('eof', False)) != eof:
            raise StepOutcomeUnknown('Blob returned incomplete or reordered bytes')
        output.extend(chunk)
        if eof:
            if hashlib.sha256(output).hexdigest() != reference['sha256']:
                raise StepOutcomeUnknown('Blob content hash does not match its reference')
            return bytes(output)


def run(entrypoint, *, run_id: str, version: str, name: str | None = None):
    """Resume assigned work, returning after completion or an acknowledged yield."""
    token = _agent._managed.set(True)
    try:
        try:
            return _agent.resume(entrypoint, run_id=run_id, version=version, name=name)
        except _agent._YieldExecution:
            return None
    finally:
        _agent._managed.reset(token)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entrypoint', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*', args.entrypoint):
        raise ValidationError('entrypoint must be module:function')
    module, function = args.entrypoint.split(':')
    entrypoint = getattr(importlib.import_module(module), function)
    if not callable(entrypoint):
        raise ValidationError('The assigned entrypoint must be callable')
    return run(entrypoint, run_id=args.run_id, version=args.version, name=function)


if __name__ == '__main__':
    main()
