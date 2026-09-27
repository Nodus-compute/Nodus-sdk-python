"""Running compute across instances and training, and launching one SSH-ready GPU."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from .errors import APIError, NodusError, ValidationError, WorkspaceNotReadyError
from ._workspaces import (AsyncWorkspace, Workspace, _BASE, _configuration, _default_size_gb, _fresh_key, _key,
                          _ssh_key, _uncertain, _wait_bounds)

_STATES = ("running", "history")
_TYPES = ("instance", "training", "workspace")
_LAUNCHED_BY = ("me", "agents")
_GROUP_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,255}", re.ASCII)
# Default OpenSSH public key names, tried in this order. Private key files are never read.
_DEFAULT_PUBLIC_KEYS = ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub")


def _choice(value: Any, field: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValidationError(f"{field} must be one of {', '.join(choices)}")
    return value


def _query(state: Any, type: Any, launched_by: Any, limit: Any, include_workspaces: Any = False) -> dict[str, Any]:
    params: dict[str, Any] = {"state": _choice(state, "state", _STATES)}
    if type is not None:
        params["type"] = _choice(type, "type", _TYPES)
    if launched_by is not None:
        params["launched_by"] = _choice(launched_by, "launched_by", _LAUNCHED_BY)
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValidationError("limit must be a whole number from 1 to 100")
        params["limit"] = limit
    if not isinstance(include_workspaces, bool):
        raise ValidationError("include_workspaces must be True or False")
    if include_workspaces:
        params["include"] = "workspaces"
    return params


def _items(response: Any) -> tuple[list[dict[str, Any]], str | None]:
    items = response.get("items") if isinstance(response, dict) else None
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise APIError("Compute list response is invalid", body=response)
    cursor = response.get("next_cursor")
    if cursor is not None and (not isinstance(cursor, str) or (cursor and not items)):
        raise APIError("Compute list cursor is invalid", body=response)
    return items, cursor or None


def _group_params(limit: Any, cursor: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValidationError("limit must be a whole number from 1 to 100")
        params["limit"] = limit
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 1024:
            raise ValidationError("cursor must be a next_cursor returned by Nodus")
        params["cursor"] = cursor
    return params


def _group_page(response: Any) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(response, dict) or not isinstance(response.get("group"), dict):
        raise APIError("Compute group response is invalid", body=response)
    return _items(response)


def _group_path(group_id: Any) -> str:
    if not isinstance(group_id, str) or _GROUP_ID.fullmatch(group_id) is None:
        raise ValidationError("Use a group ID returned by client.compute.list()")
    return "/v1/compute/groups/" + group_id


class Compute:
    """What is running now: instances, training (one row per sweep) and optionally workspaces."""

    def __init__(self, client: Any):
        self._client = client

    def list(self, *, state: str = "running", type: str | None = None, launched_by: str | None = None,
             limit: int | None = None, include_workspaces: bool = False) -> list[dict[str, Any]]:
        """One page of compute items as the server sends them. Use ``iterate()`` for every page.

        ``include_workspaces=True`` adds running GPU workspaces as items of type ``workspace``.
        """
        return _items(self._client._request("GET", "/v1/compute", params=_query(state, type, launched_by, limit, include_workspaces)))[0]

    def iterate(self, *, state: str = "running", type: str | None = None, launched_by: str | None = None,
                limit: int | None = None, include_workspaces: bool = False) -> Iterator[dict[str, Any]]:
        """Every compute item, following ``next_cursor``. ``limit`` sets the page size."""
        params, seen = _query(state, type, launched_by, limit, include_workspaces), set()
        while True:
            items, cursor = _items(self._client._request("GET", "/v1/compute", params=params))
            yield from items
            if cursor is None:
                return
            if cursor in seen:
                raise APIError("Compute pagination repeated a cursor")
            seen.add(cursor)
            params = {**params, "cursor": cursor}

    def group(self, group_id: str, *, limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        """One page of a training sweep: ``group``, its runs as ``items`` and ``next_cursor``."""
        return self._client._request("GET", _group_path(group_id), params=_group_params(limit, cursor))

    def iterate_group(self, group_id: str, *, limit: int | None = None) -> Iterator[dict[str, Any]]:
        """Every run in a training sweep, following ``next_cursor``. ``limit`` sets the page size."""
        path, params, seen = _group_path(group_id), _group_params(limit, None), set()
        while True:
            items, cursor = _group_page(self._client._request("GET", path, params=params))
            yield from items
            if cursor is None:
                return
            if cursor in seen:
                raise APIError("Compute group pagination repeated a cursor")
            seen.add(cursor)
            params = {**params, "cursor": cursor}


class AsyncCompute:
    """Asynchronous counterpart of :class:`Compute`."""

    def __init__(self, client: Any):
        self._client = client

    async def list(self, *, state: str = "running", type: str | None = None, launched_by: str | None = None,
                   limit: int | None = None, include_workspaces: bool = False) -> list[dict[str, Any]]:
        params = _query(state, type, launched_by, limit, include_workspaces)
        return _items(await self._client._request("GET", "/v1/compute", params=params))[0]

    async def iterate(self, *, state: str = "running", type: str | None = None, launched_by: str | None = None,
                      limit: int | None = None, include_workspaces: bool = False) -> AsyncIterator[dict[str, Any]]:
        params, seen = _query(state, type, launched_by, limit, include_workspaces), set()
        while True:
            items, cursor = _items(await self._client._request("GET", "/v1/compute", params=params))
            for item in items:
                yield item
            if cursor is None:
                return
            if cursor in seen:
                raise APIError("Compute pagination repeated a cursor")
            seen.add(cursor)
            params = {**params, "cursor": cursor}

    async def group(self, group_id: str, *, limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        return await self._client._request("GET", _group_path(group_id), params=_group_params(limit, cursor))

    async def iterate_group(self, group_id: str, *, limit: int | None = None) -> AsyncIterator[dict[str, Any]]:
        path, params, seen = _group_path(group_id), _group_params(limit, None), set()
        while True:
            items, cursor = _group_page(await self._client._request("GET", path, params=params))
            for item in items:
                yield item
            if cursor is None:
                return
            if cursor in seen:
                raise APIError("Compute group pagination repeated a cursor")
            seen.add(cursor)
            params = {**params, "cursor": cursor}


def default_public_key() -> str:
    """The first of the user's default OpenSSH public keys, or a ValidationError."""
    for name in _DEFAULT_PUBLIC_KEYS:
        path = Path.home() / ".ssh" / name
        if path.is_file():
            return _ssh_key(path.read_text())
    raise ValidationError("Pass ssh_key with an OpenSSH public key. No ~/.ssh/id_ed25519.pub, "
                          "id_ecdsa.pub or id_rsa.pub was found.")


def default_name(prefix: str, key: str) -> str:
    """The console's name shape, derived from the key: the server matches a repeated create by name."""
    return f"{prefix}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


def instance_body(gpu: str | None, *, gpu_count: int | None, gpu_memory_gb: float | None, disk_gb: int,
                  environment: str | None, ssh_key: str | None, name: str, max_hours: int) -> dict[str, Any]:
    """A POST /v1/research-workspaces body for an SSH instance with local disk only."""
    body = _configuration(name, gpu=gpu, gpu_count=gpu_count,
                          gpu_memory_gb=gpu_memory_gb, environment=environment, editor="ssh", max_hours=max_hours,
                          size_gb=None, budget_usd=None, ssh_key=ssh_key or default_public_key(), cpus=None,
                          memory_gb=None, disk_gb=disk_gb, repository=None, ref=None, runtime_id=None,
                          form_factor=None)
    return {"kind": "instance", **body, "size_gb": 0}


def workspace_body(gpu: str | None, *, gpu_count: int | None, gpu_memory_gb: float | None, disk_gb: int,
                   environment: str | None, ssh_key: str | None, name: str, max_hours: int) -> dict[str, Any]:
    """A POST /v1/research-workspaces body for an SSH workspace that keeps project files."""
    return _configuration(name, gpu=gpu, gpu_count=gpu_count, gpu_memory_gb=gpu_memory_gb, environment=environment,
                          editor="ssh", max_hours=max_hours, size_gb=None, budget_usd=None,
                          ssh_key=ssh_key or default_public_key(), cpus=None, memory_gb=None, disk_gb=disk_gb,
                          repository=None, ref=None, runtime_id=None, form_factor=None)


def _check_gpu(gpu: Any) -> None:
    if gpu is None:
        raise ValidationError("Choose a gpu such as \"H100\" or \"H100:2\"")


def _refuse_replayed_stop(machine: Any, keep_files: bool, headers: dict[str, str]) -> None:
    """A replayed workspace that is stopped may have run already. Its start does not replay by key."""
    from . import _was_replayed
    if keep_files and machine.state == "stopped" and _was_replayed(headers):
        raise WorkspaceNotReadyError(
            f"{machine.id} was already created with this idempotency key and is stopped. It may have run and "
            f"been stopped since, so it is not started again. Start it with nodus workspace start {machine.id} "
            "or launch with a new key.", body=machine.raw)


def _start_uncertain(machine: Any, error: NodusError, key: str) -> NodusError:
    """Point the retry at this machine's start: another launch would rent a second machine."""
    if not _uncertain(error):
        return error
    if isinstance(error.body, dict):
        error.body = {**error.body, "workspace_id": machine.id}
    error.message = (f"{error.message}\nThe start of {machine.id} may have been accepted. Do not launch again. Retry "
                     f"client.workspaces.get({machine.id!r}).start(idempotency_key={key!r}) or stop it.")
    error.args = (error.message,)
    return error


def _timed_out(machine: Any, error: WorkspaceNotReadyError) -> WorkspaceNotReadyError:
    """A wait that ran out of time, restated with the machine's ID. Compute is left running."""
    if machine._stopped_early():
        return error
    return WorkspaceNotReadyError(
        f"{machine.name or machine.id} ({machine.id}) is still running but SSH is not ready yet ({machine.state}). "
        f"It keeps running until max_hours. Wait with client.workspaces.get({machine.id!r}).wait_until_ready() "
        "or release it with .stop().", body=machine.raw)


def launch(client: Any, gpu: str | None, *, gpu_count: int | None, gpu_memory_gb: float | None, disk_gb: int,
           environment: str | None, ssh_key: str | None, name: str | None, max_hours: int, keep_files: bool,
           wait: bool, timeout_seconds: float, poll_seconds: float, idempotency_key: str | None) -> Workspace:
    _check_gpu(gpu)
    _wait_bounds(poll_seconds, timeout_seconds)
    key = _key(idempotency_key or _fresh_key())
    if keep_files:
        body = workspace_body(gpu, gpu_count=gpu_count, gpu_memory_gb=gpu_memory_gb, disk_gb=disk_gb,
                              environment=environment, ssh_key=ssh_key, name=name or default_name("workspace", key),
                              max_hours=max_hours)
        body["size_gb"] = _default_size_gb(client.workspaces.capabilities())
    else:
        body = instance_body(gpu, gpu_count=gpu_count, gpu_memory_gb=gpu_memory_gb, disk_gb=disk_gb,
                             environment=environment, ssh_key=ssh_key, name=name or default_name("instance", key),
                             max_hours=max_hours)
    machine, answered = Workspace(client), {}
    # The server replays a repeated key and body as the original record. Older servers match an instance by name.
    machine._absorb(client._request("POST", _BASE, json=body, idempotency_key=key, headers_out=answered))
    _refuse_replayed_stop(machine, keep_files, answered)
    if machine.state == "stopped":
        try:
            machine.start(idempotency_key=key)
        except NodusError as error:
            raise _start_uncertain(machine, error, key) from None
    if wait:
        try:
            machine.wait_until_ready(poll_seconds=poll_seconds, timeout_seconds=timeout_seconds)
        except WorkspaceNotReadyError as error:
            raise _timed_out(machine, error) from None
    return machine


async def launch_async(client: Any, gpu: str | None, *, gpu_count: int | None, gpu_memory_gb: float | None, disk_gb: int,
                       environment: str | None, ssh_key: str | None, name: str | None, max_hours: int,
                       keep_files: bool, wait: bool, timeout_seconds: float, poll_seconds: float,
                       idempotency_key: str | None) -> AsyncWorkspace:
    _check_gpu(gpu)
    _wait_bounds(poll_seconds, timeout_seconds)
    key = _key(idempotency_key or _fresh_key())
    if keep_files:
        body = workspace_body(gpu, gpu_count=gpu_count, gpu_memory_gb=gpu_memory_gb, disk_gb=disk_gb,
                              environment=environment, ssh_key=ssh_key, name=name or default_name("workspace", key),
                              max_hours=max_hours)
        body["size_gb"] = _default_size_gb(await client.workspaces.capabilities())
    else:
        body = instance_body(gpu, gpu_count=gpu_count, gpu_memory_gb=gpu_memory_gb, disk_gb=disk_gb,
                             environment=environment, ssh_key=ssh_key, name=name or default_name("instance", key),
                             max_hours=max_hours)
    machine, answered = AsyncWorkspace(client), {}
    machine._absorb(await client._request("POST", _BASE, json=body, idempotency_key=key, headers_out=answered))
    _refuse_replayed_stop(machine, keep_files, answered)
    if machine.state == "stopped":
        try:
            await machine.start(idempotency_key=key)
        except NodusError as error:
            raise _start_uncertain(machine, error, key) from None
    if wait:
        try:
            await machine.wait_until_ready(poll_seconds=poll_seconds, timeout_seconds=timeout_seconds)
        except WorkspaceNotReadyError as error:
            raise _timed_out(machine, error) from None
    return machine
