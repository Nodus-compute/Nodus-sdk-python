"""Managed agent definitions and durable run submission."""

from datetime import datetime
import math
import re
import uuid

from ._agent import _id, encode
from ._assets import _id as asset_id
from ._projects import upload_project, upload_project_async, setup_command
from ._sandboxes import _valid_id
from .errors import APIError, NodusError, ValidationError


def _key(value):
    if value is None:
        return "managed-agent-" + uuid.uuid4().hex
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or value.strip() != value or any(c in value for c in "\x00\r\n"):
        raise ValidationError("Use a nonempty idempotency key of at most 200 characters")
    return value


def _event_key(value):
    if value is None:
        raise ValidationError("Durable events require an explicit idempotency key")
    return _key(value)


def _definition(name, entrypoint, budget, *, template=None, source_asset_id=None, bootstrap=None, setup=None, policy=None,
                secrets=None, network_permissions=None, requirements=None, min_workers=None, max_workers=None,
                assistant_template=None, model=None, model_max_output_tokens=None):
    if model is not None and (not isinstance(model, str) or not model.startswith('nodus:') or len(model) <= 6):
        raise ValidationError('Select a public Nodus model from the available catalog')
    if model is not None:
        _id(model)
    if model_max_output_tokens is not None and (model is None or type(model_max_output_tokens) is not int or not 1 <= model_max_output_tokens <= 4096):
        raise ValidationError('Model output limits require a model and an integer from 1 to 4096')
    if assistant_template is not None:
        if assistant_template != 'nodus:claude-assistant-v1' or model is None:
            raise ValidationError('Choose the supported assistant template and an explicit public model')
        if entrypoint not in ('agent:main', 'nodus.managed_assistant:main') or any(value is not None for value in (source_asset_id, bootstrap, setup, policy, secrets, network_permissions)):
            raise ValidationError('The owned assistant uses its installed entrypoint without project setup or credentials')
        entrypoint = 'nodus.managed_assistant:main'
    if not isinstance(entrypoint, str) or len(entrypoint) > 256 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*", entrypoint):
        raise ValidationError("entrypoint must be module:function")
    if budget is not None and (type(budget) not in (int, float) or not math.isfinite(budget) or budget < 0):
        raise ValidationError("Legacy budget must be a finite nonnegative value")
    for value, minimum in ((min_workers, 0), (max_workers, 1)):
        if value is not None and (type(value) is not int or not minimum <= value <= 100):
            raise ValidationError("Invalid worker limit")
    if min_workers is not None and max_workers is not None and min_workers > max_workers:
        raise ValidationError("min_workers cannot exceed max_workers")
    body = {"name": _id(name), "entrypoint": entrypoint}
    if budget is not None:
        body["budget_usd"] = budget
    for field, value in (("template", template), ("bootstrap", bootstrap), ("secrets", secrets), ("policy", policy),
                         ("network_permissions", network_permissions), ("requirements", requirements),
                         ("min_workers", min_workers), ("max_workers", max_workers),
                         ("assistant_template", assistant_template), ("model", model),
                         ("model_max_output_tokens", model_max_output_tokens)):
        if value is not None:
            body[field] = value
    if setup is not None:
        body["setup"] = setup_command(setup)
    if source_asset_id is not None:
        body["source"] = {"asset_id": asset_id(source_asset_id)}
    return body


def _receipt(client, response, path, key=None, expected_id=None, agent_id=None):
    try:
        body = client._one(response, "POST" if key else "GET", path)
        _valid_id(body.get("id"), "managed agent resource")
        if expected_id is not None and body["id"] != expected_id:
            raise NodusError("Response identity changed")
        if agent_id is not None and body.get("agent_id") != agent_id:
            raise NodusError("Response agent identity changed")
        return body
    except NodusError:
        if key:
            raise client._unreached(APIError, "Managed agent mutation returned an invalid receipt", key) from None
        raise APIError("Managed agent response has invalid identity") from None


class _Handle:
    def __init__(self, client, body):
        self._client = client
        self.raw = dict(body)
        self.id = _valid_id(body.get("id"), "managed agent resource")

    def __getattr__(self, name):
        fields = {"name", "status", "current_revision", "budget_usd", "cost_usd", "reserved_usd", "min_workers", "max_workers",
                  "workers", "queued", "startup", "created_at", "updated_at", "url", "agent_id", "revision", "session", "reason", "input", "result",
                  "deadline", "next_wake_at", "sandbox_id", "exec_id", "attempt", "segment",
                  "recovery_policy", "checkpoint_id", "checkpoint_status", "checkpoint_error", "last_checkpoint_at",
                  "assistant_template", "model", "model_max_output_tokens"}
        if name not in fields:
            raise AttributeError(name)
        return self.raw.get(name)


class ManagedAgents:
    def __init__(self, client):
        self._client = client

    def create(self, *, name, budget=None, project=None, entrypoint="agent:main", idempotency_key=None, **options):
        """Deploy a versioned Python agent."""
        key = _key(idempotency_key)
        body = _definition(name, entrypoint, budget, **options)
        if project is not None:
            if body.get('assistant_template'):
                raise ValidationError('The owned assistant does not upload a project')
            if "source" in body or "bootstrap" in body:
                raise ValidationError("Choose a local project or a source attachment")
            body["source"] = {"asset_id": upload_project(self._client, project, ("agent", key))}
        path = "/v1/agents"
        response = self._client._request("POST", path, json=body, idempotency_key=key)
        return ManagedAgent(self._client, _receipt(self._client, response, path, key))

    def get(self, agent_id):
        path = "/v1/agents/" + _valid_id(agent_id, "agent")
        return ManagedAgent(self._client, _receipt(self._client, self._client._request("GET", path), path, expected_id=agent_id))

    def list_page(self, *, limit=50, after=None):
        response = self._client._request("GET", "/v1/agents", params=_page(limit, after))
        return _listing(response, "agents", ManagedAgent, self._client)

    def list(self, **options):
        return self.list_page(**options)[0]

    def iterate(self):
        after = None
        while True:
            rows, next_after = self.list_page(after=after)
            yield from rows
            if not next_after or next_after == after:
                return
            after = next_after


class ManagedAgent(_Handle):
    def revisions(self, *, after=None, limit=50):
        """Read a page of immutable deployment revisions."""
        return self._client._request("GET", "/v1/agents/" + self.id + "/revisions", params=_page(limit, after))

    def revision(self, number):
        """Read the original definition for one deployment revision."""
        if type(number) is not int or number < 1:
            raise ValidationError("revision must be a positive integer")
        return self._client._request("GET", "/v1/agents/" + self.id + "/revisions/" + str(number))

    @property
    def runs(self):
        return ManagedRuns(self._client, self.id)

    def submit(self, input, *, idempotency_key, session=None, deadline=None):
        """Durably enqueue one input using the caller's stable event key."""
        return self.runs.submit(input, idempotency_key=idempotency_key, session=session, deadline=deadline)

    def _control(self, operation, key):
        path = "/v1/agents/" + self.id + "/" + operation
        key = _key(key)
        response = self._client._request("POST", path, json={}, idempotency_key=key)
        self.raw = _receipt(self._client, response, path, key, expected_id=self.id)
        return self

    def pause(self, *, idempotency_key=None):
        """Pause admission of new work without changing the agent identity."""
        return self._control("pause", idempotency_key)

    def resume(self, *, idempotency_key=None):
        """Allow queued work to acquire authorized compute."""
        return self._control("resume", idempotency_key)

    def refresh(self):
        self.raw = self._client.agents.get(self.id).raw
        return self

    def update(self, *, expected_revision, name, budget=None, entrypoint="agent:main", idempotency_key=None, **options):
        """Create a revision only if the observed revision is still current."""
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValidationError("expected_revision must be positive")
        body = {"expected_revision": expected_revision, "definition": _definition(name, entrypoint, budget, **options)}
        path = "/v1/agents/" + self.id
        key = _key(idempotency_key)
        response = self._client._request("PATCH", path, json=body, idempotency_key=key)
        self.raw = _receipt(self._client, response, path, key, expected_id=self.id)
        return self


def _submission(input, session, deadline):
    encode(input)
    body = {"input": input}
    if session is not None:
        body["session"] = _id(session)
    if deadline is not None:
        if not isinstance(deadline, datetime) or deadline.tzinfo is None:
            raise ValidationError("deadline must be a timezone-aware datetime")
        body["deadline"] = deadline.isoformat()
    return body


class ManagedRuns:
    def __init__(self, client, agent_id):
        self._client, self.agent_id = client, agent_id
        self._path = "/v1/agents/" + _valid_id(agent_id, "agent") + "/runs"

    def submit(self, input, *, idempotency_key, session=None, deadline=None):
        key = _event_key(idempotency_key)
        body = _submission(input, session, deadline)
        response = self._client._request("POST", self._path, json=body, idempotency_key=key)
        return ManagedRun(self._client, _receipt(self._client, response, self._path, key, agent_id=self.agent_id))

    def get(self, run_id):
        path = self._path + "/" + _valid_id(run_id, "run")
        response = self._client._request("GET", path)
        return ManagedRun(self._client, _receipt(self._client, response, path, expected_id=run_id, agent_id=self.agent_id))

    def list_page(self, *, limit=50, after=None):
        response = self._client._request("GET", self._path, params=_page(limit, after))
        return _listing(response, "runs", ManagedRun, self._client)

    def list(self, **options):
        return self.list_page(**options)[0]

    def iterate(self):
        after = None
        while True:
            rows, next_after = self.list_page(after=after)
            yield from rows
            if not next_after or next_after == after:
                return
            after = next_after


class ManagedRun(_Handle):
    def retry(self, *, idempotency_key=None):
        """Request another authorized attempt for a run blocked by exhausted retries."""
        path, key = self._path() + "/retry", _key(idempotency_key)
        response = self._client._request("POST", path, json={}, idempotency_key=key)
        self.raw = _receipt(self._client, response, path, key, expected_id=self.id, agent_id=self.raw["agent_id"])
        return self

    def _path(self):
        return "/v1/agents/" + _valid_id(self.raw.get("agent_id"), "agent") + "/runs/" + self.id

    def steps(self, *, after="", limit=100):
        """Read the journal's step outcomes and reconciliation revisions."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValidationError("limit must be between 1 and 100")
        return self._client._request("GET", self._path() + "/steps", params={"after": _id(after) if after else "", "limit": limit})

    def resolve(self, *, step_id, expected_revision, decision, reason, evidence_digest, result=None, checkpoint_id=None, idempotency_key=None):
        """Resolve an uncertain external effect using independently verified evidence."""
        from ._agent_runs import _resolve
        body = _resolve(step_id, expected_revision, decision, reason, evidence_digest, result)
        if checkpoint_id is not None:
            if decision != "completed":
                raise ValidationError("Only a completed resolution can select application state")
            body["checkpoint_id"] = _id(checkpoint_id)
        return self._client._request("POST", self._path() + "/resolve", json=body, idempotency_key=_key(idempotency_key))

    def signal(self, name, input=None, *, idempotency_key):
        """Durably deliver a named event to this run."""
        encode(input)
        key = _event_key(idempotency_key)
        path = self._path() + "/signals"
        response = self._client._request("POST", path, json={"name": _id(name), "input": input}, idempotency_key=key)
        self.raw = _receipt(self._client, response, path, key, expected_id=self.id, agent_id=self.raw["agent_id"])
        return self

    def cancel(self, *, idempotency_key=None):
        """Request cancellation of this run and its active execution."""
        path, key = self._path() + "/cancel", _key(idempotency_key)
        response = self._client._request("POST", path, json={}, idempotency_key=key)
        self.raw = _receipt(self._client, response, path, key, expected_id=self.id, agent_id=self.raw["agent_id"])
        return self

    def refresh(self):
        self.raw = self._client.agents.get(self.raw["agent_id"]).runs.get(self.id).raw
        return self


def _page(limit, after):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValidationError("limit must be between 1 and 100")
    return {"limit": limit, **({"after": _valid_id(after, "pagination cursor")} if after else {})}


def _listing(response, field, cls, client):
    rows = response.get(field) if isinstance(response, dict) else None
    if not isinstance(rows, list):
        raise APIError("Managed agent list response is invalid")
    after = response.get("next_after")
    if after:
        _valid_id(after, "pagination cursor")
    return [cls(client, row) for row in rows], after or None


class AsyncManagedAgents(ManagedAgents):
    async def create(self, *, name, budget=None, project=None, entrypoint="agent:main", idempotency_key=None, **options):
        key = _key(idempotency_key)
        body = _definition(name, entrypoint, budget, **options)
        if project is not None:
            if body.get('assistant_template'):
                raise ValidationError('The owned assistant does not upload a project')
            if "source" in body or "bootstrap" in body:
                raise ValidationError("Choose a local project or a source attachment")
            body["source"] = {"asset_id": await upload_project_async(self._client, project, ("agent", key))}
        path = "/v1/agents"
        response = await self._client._request("POST", path, json=body, idempotency_key=key)
        return AsyncManagedAgent(self._client, _receipt(self._client, response, path, key))

    async def get(self, agent_id):
        path = "/v1/agents/" + _valid_id(agent_id, "agent")
        response = await self._client._request("GET", path)
        return AsyncManagedAgent(self._client, _receipt(self._client, response, path, expected_id=agent_id))

    async def list_page(self, *, limit=50, after=None):
        response = await self._client._request("GET", "/v1/agents", params=_page(limit, after))
        return _listing(response, "agents", AsyncManagedAgent, self._client)

    async def list(self, **options):
        return (await self.list_page(**options))[0]

    async def iterate(self):
        after = None
        while True:
            rows, next_after = await self.list_page(after=after)
            for row in rows:
                yield row
            if not next_after or next_after == after:
                return
            after = next_after


class AsyncManagedAgent(ManagedAgent):
    async def revisions(self, *, after=None, limit=50):
        """Read a page of immutable deployment revisions."""
        return await self._client._request("GET", "/v1/agents/" + self.id + "/revisions", params=_page(limit, after))

    async def revision(self, number):
        """Read the original definition for one deployment revision."""
        if type(number) is not int or number < 1:
            raise ValidationError("revision must be a positive integer")
        return await self._client._request("GET", "/v1/agents/" + self.id + "/revisions/" + str(number))

    @property
    def runs(self):
        return AsyncManagedRuns(self._client, self.id)

    async def _control(self, operation, key):
        path = "/v1/agents/" + self.id + "/" + operation
        key = _key(key)
        response = await self._client._request("POST", path, json={}, idempotency_key=key)
        self.raw = _receipt(self._client, response, path, key, expected_id=self.id)
        return self

    async def refresh(self):
        self.raw = (await self._client.agents.get(self.id)).raw
        return self

    async def update(self, *, expected_revision, name, budget=None, entrypoint="agent:main", idempotency_key=None, **options):
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValidationError("expected_revision must be positive")
        body = {"expected_revision": expected_revision, "definition": _definition(name, entrypoint, budget, **options)}
        path, key = "/v1/agents/" + self.id, _key(idempotency_key)
        response = await self._client._request("PATCH", path, json=body, idempotency_key=key)
        self.raw = _receipt(self._client, response, path, key, expected_id=self.id)
        return self


class AsyncManagedRuns(ManagedRuns):
    async def submit(self, input, *, idempotency_key, session=None, deadline=None):
        key = _event_key(idempotency_key)
        body = _submission(input, session, deadline)
        response = await self._client._request("POST", self._path, json=body, idempotency_key=key)
        return AsyncManagedRun(self._client, _receipt(self._client, response, self._path, key, agent_id=self.agent_id))

    async def get(self, run_id):
        path = self._path + "/" + _valid_id(run_id, "run")
        response = await self._client._request("GET", path)
        return AsyncManagedRun(self._client, _receipt(self._client, response, path, expected_id=run_id, agent_id=self.agent_id))

    async def list_page(self, *, limit=50, after=None):
        response = await self._client._request("GET", self._path, params=_page(limit, after))
        return _listing(response, "runs", AsyncManagedRun, self._client)

    async def list(self, **options):
        return (await self.list_page(**options))[0]

    async def iterate(self):
        after = None
        while True:
            rows, next_after = await self.list_page(after=after)
            for row in rows:
                yield row
            if not next_after or next_after == after:
                return
            after = next_after


class AsyncManagedRun(ManagedRun):
    async def retry(self, *, idempotency_key=None):
        """Request another authorized attempt for a run blocked by exhausted retries."""
        path, key = self._path() + "/retry", _key(idempotency_key)
        response = await self._client._request("POST", path, json={}, idempotency_key=key)
        self.raw = _receipt(self._client, response, path, key, expected_id=self.id, agent_id=self.raw["agent_id"])
        return self

    async def steps(self, *, after="", limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValidationError("limit must be between 1 and 100")
        return await self._client._request("GET", self._path() + "/steps", params={"after": _id(after) if after else "", "limit": limit})

    async def resolve(self, *, step_id, expected_revision, decision, reason, evidence_digest, result=None, checkpoint_id=None, idempotency_key=None):
        from ._agent_runs import _resolve
        body = _resolve(step_id, expected_revision, decision, reason, evidence_digest, result)
        if checkpoint_id is not None:
            if decision != "completed":
                raise ValidationError("Only a completed resolution can select application state")
            body["checkpoint_id"] = _id(checkpoint_id)
        return await self._client._request("POST", self._path() + "/resolve", json=body, idempotency_key=_key(idempotency_key))

    async def signal(self, name, input=None, *, idempotency_key):
        encode(input)
        path, key = self._path() + "/signals", _event_key(idempotency_key)
        response = await self._client._request("POST", path, json={"name": _id(name), "input": input}, idempotency_key=key)
        self.raw = _receipt(self._client, response, path, key, expected_id=self.id, agent_id=self.raw["agent_id"])
        return self

    async def cancel(self, *, idempotency_key=None):
        path, key = self._path() + "/cancel", _key(idempotency_key)
        response = await self._client._request("POST", path, json={}, idempotency_key=key)
        self.raw = _receipt(self._client, response, path, key, expected_id=self.id, agent_id=self.raw["agent_id"])
        return self

    async def refresh(self):
        agent = await self._client.agents.get(self.raw["agent_id"])
        self.raw = (await agent.runs.get(self.id)).raw
        return self
