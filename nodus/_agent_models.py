"""Durable hosted model calls through the assigned step's local capability."""

import base64
import hashlib
import json
import os
import time
import uuid

from . import _agent
from .errors import StepOutcomeUnknown, ValidationError


_MAX_INPUT = 128 << 10
_MAX_MESSAGES = 1024
_MAX_RESPONSE = 8 << 20
_RESPONSE_PAGE = 256 << 10


def text_messages(messages):
    if not isinstance(messages, list) or not 1 <= len(messages) <= _MAX_MESSAGES:
        raise ValidationError('Hosted models require 1 to 1024 text messages')
    for message in messages:
        if (not isinstance(message, dict) or set(message) != {'role', 'content'}
                or message['role'] not in ('user', 'assistant')
                or not isinstance(message['content'], str) or not message['content']):
            raise ValidationError('Hosted messages require a user or assistant role and nonempty text content')


def request(messages, *, call_id, max_output_tokens, model=None, system=None, response_schema=None):
    session, scope = _agent._current.get(), _agent._step_authority.get()
    if not _agent._managed.get() or session is None or scope is None:
        raise ValidationError('Hosted model access requires an executing managed step')
    call_id = _agent._id(call_id)
    model = os.environ.get('NODUS_AGENT_MODEL') if model is None else model
    if not isinstance(model, str) or not model.startswith('nodus:') or len(model) <= 6:
        raise ValidationError('Select the public Nodus model accepted by this deployment')
    _agent._id(model)
    if type(max_output_tokens) is not int or max_output_tokens < 1:
        raise ValidationError('Hosted model max_output_tokens must be a positive integer within the selected model limit')
    text_messages(messages)
    if system is not None and (not isinstance(system, str) or not system):
        raise ValidationError('Hosted model system instructions must be nonempty text')
    payload = {'model': model, 'messages': messages, 'max_tokens': max_output_tokens}
    if system is not None:
        payload['system'] = system
    if response_schema is not None:
        if (not isinstance(response_schema, dict) or response_schema.get('type') != 'object'
                or response_schema.get('additionalProperties') is not False):
            raise ValidationError('Hosted response_schema requires an object JSON schema with additionalProperties false')
        payload['output_config'] = {'format': {'type': 'json_schema', 'schema': response_schema}}
    raw = base64.b64decode(_agent.encode(payload))
    if len(raw) > _MAX_INPUT:
        raise ValidationError('Hosted model input exceeds 128 KiB')
    payload = json.loads(raw)
    body = {**scope, 'call_id': call_id, 'paged_response': True}
    session.healthy()
    deadline = time.monotonic() + 240
    try:
        result = session.rpc.call('model_begin', {**body, 'request_id': uuid.uuid4().hex, 'input': payload})
        timeout = result.get('poll_timeout_seconds')
        if timeout is not None:
            if type(timeout) is not int or timeout < 1:
                raise StepOutcomeUnknown('Hosted model response has an invalid polling allowance')
            deadline = time.monotonic() + timeout
        identity = None
        chunks, position, digest, size = [], 0, None, None
        while True:
            session.healthy()
            receipt = result.get('receipt')
            if not isinstance(receipt, dict):
                raise StepOutcomeUnknown('Hosted model receipt could not be verified')
            current = receipt.get('id')
            if not isinstance(current, str) or not _agent._ID.fullmatch(current) or receipt.get('model') != model:
                raise StepOutcomeUnknown('Hosted model receipt has an invalid identity')
            if identity is not None and current != identity:
                raise StepOutcomeUnknown('Hosted model acknowledgement changed identity')
            identity = current
            status = receipt.get('state')
            if status == 'succeeded':
                response = result.get('response')
                page = result.get('response_page')
                if page is not None:
                    if response is not None or not isinstance(page, dict):
                        raise StepOutcomeUnknown('Hosted model response page could not be verified')
                    current_hash, current_size = page.get('sha256'), page.get('bytes')
                    if (not isinstance(current_hash, str) or len(current_hash) != 64
                            or any(char not in '0123456789abcdef' for char in current_hash)
                            or type(current_size) is not int or not 0 < current_size <= _MAX_RESPONSE
                            or type(page.get('offset')) is not int or page['offset'] != position
                            or type(page.get('eof')) is not bool
                            or digest is not None and (current_hash != digest or current_size != size)):
                        raise StepOutcomeUnknown('Hosted model response page changed identity or bounds')
                    digest, size = current_hash, current_size
                    try:
                        chunk = base64.b64decode(page['data'], validate=True)
                    except (ValueError, TypeError, KeyError):
                        raise StepOutcomeUnknown('Hosted model response page has invalid bytes') from None
                    expected = min(_RESPONSE_PAGE, size - position)
                    if len(chunk) != expected or expected <= 0 or page['eof'] != (position + expected == size):
                        raise StepOutcomeUnknown('Hosted model response page is incomplete')
                    chunks.append(chunk)
                    position += len(chunk)
                    if not page['eof']:
                        session.healthy()
                        if time.monotonic() >= deadline:
                            raise StepOutcomeUnknown('Hosted model reply retrieval did not finish. Retain its call ID')
                        result = session.rpc.call('model_status', {**body, 'response_offset': position})
                        continue
                    raw_response = b''.join(chunks)
                    if hashlib.sha256(raw_response).hexdigest() != digest:
                        raise StepOutcomeUnknown('Hosted model response failed content verification')
                    try:
                        response = json.loads(raw_response)
                    except (ValueError, UnicodeError):
                        raise StepOutcomeUnknown('Hosted model response is invalid JSON') from None
                elif position:
                    raise StepOutcomeUnknown('Hosted model response changed retrieval format')
                if not isinstance(response, dict) or response.get('model') != model:
                    raise StepOutcomeUnknown('Hosted model response differs from the accepted model')
                try:
                    if page is None:
                        _agent.encode(response)
                except ValidationError:
                    raise StepOutcomeUnknown('Hosted model response exceeds the durable result limits') from None
                return response
            if status == 'unknown':
                raise StepOutcomeUnknown('Hosted model request ' + identity + ' needs reconciliation. Inspect the run and retain its call ID')
            if status == 'failed':
                raise StepOutcomeUnknown('Hosted model request ' + identity + ' was rejected. Inspect the run before submitting new work')
            if status != 'running':
                raise StepOutcomeUnknown('Hosted model receipt has an unsupported state')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StepOutcomeUnknown('Hosted model request ' + identity + ' did not finish. Inspect the run and retain call ID ' + call_id)
            session.stopped.wait(min(.25, remaining))
            if time.monotonic() >= deadline:
                raise StepOutcomeUnknown('Hosted model request ' + identity + ' did not finish. Inspect the run and retain call ID ' + call_id)
            result = session.rpc.call('model_status', body)
    except BaseException:
        session.failed.set()
        raise
