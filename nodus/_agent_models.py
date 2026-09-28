"""Durable hosted model calls through the assigned step's local capability."""

import base64
import json
import os
import time
import uuid

from . import _agent
from .errors import StepOutcomeUnknown, ValidationError


_MAX_INPUT = 128 << 10
_MAX_MESSAGES = 1024


def text_messages(messages):
    if not isinstance(messages, list) or not 1 <= len(messages) <= _MAX_MESSAGES:
        raise ValidationError('Hosted models require 1 to 1024 text messages')
    for message in messages:
        if (not isinstance(message, dict) or set(message) != {'role', 'content'}
                or message['role'] not in ('user', 'assistant')
                or not isinstance(message['content'], str) or not message['content']):
            raise ValidationError('Hosted messages require a user or assistant role and nonempty text content')


def request(messages, *, call_id, max_output_tokens, model=None, system=None):
    session, scope = _agent._current.get(), _agent._step_authority.get()
    if not _agent._managed.get() or session is None or scope is None:
        raise ValidationError('Hosted model access requires an executing managed step')
    call_id = _agent._id(call_id)
    model = os.environ.get('NODUS_AGENT_MODEL') if model is None else model
    if not isinstance(model, str) or not model.startswith('nodus:') or len(model) <= 6:
        raise ValidationError('Select the public Nodus model accepted by this deployment')
    _agent._id(model)
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 4096:
        raise ValidationError('Hosted model max_output_tokens must be an integer from 1 to 4096')
    text_messages(messages)
    if system is not None and (not isinstance(system, str) or not system):
        raise ValidationError('Hosted model system instructions must be nonempty text')
    payload = {'model': model, 'messages': messages, 'max_tokens': max_output_tokens}
    if system is not None:
        payload['system'] = system
    raw = base64.b64decode(_agent.encode(payload))
    if len(raw) > _MAX_INPUT:
        raise ValidationError('Hosted model input exceeds 128 KiB')
    payload = json.loads(raw)
    body = {**scope, 'call_id': call_id}
    session.healthy()
    deadline = time.monotonic() + 240
    try:
        result = session.rpc.call('model_begin', {**body, 'request_id': uuid.uuid4().hex, 'input': payload})
        identity = None
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
                if not isinstance(response, dict) or response.get('model') != model:
                    raise StepOutcomeUnknown('Hosted model response differs from the accepted model')
                try:
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
