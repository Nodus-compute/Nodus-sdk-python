"""Explicit stable step identities and serial recorded-result replay."""
from __future__ import annotations

from dataclasses import dataclass
import functools
import inspect
import threading
import uuid

from . import _agent
from .errors import ValidationError, StepOutcomeUnknown, StepFailed, StepResultExpired

@dataclass(frozen=True)
class StepContext:
    idempotency_key: str


def step_context() -> StepContext:
    """Return this invocation's stable downstream idempotency key."""
    value = _agent._step_context.get()
    if value is None:
        raise ValidationError('step_context is only available inside a running step')
    return value


def step(*, name: str, version: str, effect: str = 'external', dedupe_seconds: int | None = None):
    """Record a synchronous JSON result under an explicit business step ID."""
    name, version = _agent._id(name), _agent._id(version)
    if effect not in ('external', 'pure', 'idempotent'):
        raise ValidationError('effect must be external, pure or idempotent')
    if effect == 'idempotent':
        if type(dedupe_seconds) is not int or not 1 <= dedupe_seconds <= 30 * 86400:
            raise ValidationError('idempotent steps require the downstream deduplication period in seconds')
    elif dedupe_seconds is not None:
        raise ValidationError('dedupe_seconds requires idempotent effect')

    def decorate(function):
        if inspect.iscoroutinefunction(function) or inspect.isgeneratorfunction(function):
            raise ValidationError('Steps must be synchronous functions returning JSON')
        @functools.wraps(function)
        def invoke(*args, _step_id=None, **kwargs):
            step_id = _agent._id(_step_id)
            session = _agent._current.get()
            if session is None:
                raise ValidationError('Call steps from nodus.agent.resume')
            if not session.guard.acquire(blocking=False):
                raise ValidationError('Nested and parallel steps are not supported')
            executed = False
            try:
                encoded = _agent.encode({'args': args, 'kwargs': kwargs})
                for _ in range(3):
                    session.healthy()
                    request = {**session.scope, 'request_id': uuid.uuid4().hex, 'step_id': step_id,
                               'name': name, 'version': version, 'effect': effect,
                               'encoding': 'json-v1', 'input': encoded}
                    if dedupe_seconds is not None:
                        request['dedupe_seconds'] = dedupe_seconds
                    receipt = session.rpc.call('claim', request)
                    decision = receipt.get('decision')
                    if receipt.get('step_id') != step_id:
                        raise StepOutcomeUnknown('Journal returned a different step identity')
                    if decision == 'replay':
                        return _agent.decode(receipt.get('result'))
                    if decision == 'expired':
                        raise StepResultExpired('Completed step result expired')
                    if decision == 'failed':
                        raise StepFailed('Step has a recorded failure')
                    if decision != 'execute':
                        raise StepOutcomeUnknown('Step has unresolved or competing execution')
                    token, external = receipt.get('claim_token'), receipt.get('external_key')
                    if not isinstance(token, str) or not token or not isinstance(external, str) or not external:
                        raise StepOutcomeUnknown('Journal execution grant is incomplete')
                    scoped = {**session.scope, 'step_id': step_id, 'claim_token': token}
                    context = StepContext(external)
                    context_token = _agent._step_context.set(context)
                    previous_owner = getattr(session, '_step_owner', None)
                    session._step_owner = (threading.get_ident(), context)
                    authority_token = _agent._step_authority.set(scoped)
                    try:
                        executed = True
                        result = function(*args, **kwargs)
                    except BaseException as error:
                        try:
                            session.rpc.call('unknown', {**scoped, 'request_id': uuid.uuid4().hex, 'code': 'outcome_unknown'})
                        except BaseException:
                            if session.failed.is_set() and isinstance(error, StepOutcomeUnknown):
                                raise error from None
                            raise
                        if session.failed.is_set() and isinstance(error, StepOutcomeUnknown):
                            raise
                        if session.recovery_policy:
                            session.failed.set()
                            raise StepOutcomeUnknown('Step state must be restored before another attempt') from None
                        if effect != 'external' and isinstance(error, Exception):
                            continue
                        raise StepOutcomeUnknown('The external step outcome is unknown') from None
                    finally:
                        session._step_owner = previous_owner
                        _agent._step_authority.reset(authority_token)
                        _agent._step_context.reset(context_token)
                    try:
                        if inspect.isawaitable(result):
                            if inspect.iscoroutine(result):
                                result.close()
                            raise ValidationError('Steps must return JSON synchronously')
                        result_bytes = _agent.encode(result)
                    except ValidationError:
                        session.rpc.call('unknown', {**scoped, 'request_id': uuid.uuid4().hex, 'code': 'outcome_unknown'})
                        raise StepOutcomeUnknown('Step result cannot be recorded within the journal limits') from None
                    session.healthy()
                    checkpoint = session.checkpoint('step', step_id, step_id=step_id, claim_token=token)
                    done = session.rpc.call('complete', {**scoped, 'request_id': uuid.uuid4().hex, 'result': result_bytes, **checkpoint})
                    if done.get('decision') != 'replay' or done.get('step_id') != step_id or done.get('result') != result_bytes:
                        raise StepOutcomeUnknown('Step completion was not durably acknowledged')
                    return _agent.decode(done['result'])
                raise StepOutcomeUnknown('Step attempt limit reached')
            except BaseException:
                if executed and session.recovery_policy:
                    session.failed.set()
                raise
            finally:
                session.guard.release()
        return invoke
    return decorate
