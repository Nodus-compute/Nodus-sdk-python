"""Team SSH keys: public keys every machine the team runs admits, picked up without a restart."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from .errors import APIError, ValidationError
from ._workspaces import _ssh_key

_BASE = "/v1/ssh-keys"
_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{1,128}", re.ASCII)


def one_public_key(value: Any) -> str:
    """Exactly one OpenSSH public key. Private keys and several keys are refused."""
    text = _ssh_key(value)
    if len([line for line in text.splitlines() if line.strip()]) != 1:
        raise ValidationError("Pass one SSH public key, such as the contents of ~/.ssh/id_ed25519.pub")
    return text


def _body(public_key: Any, name: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"public_key": one_public_key(public_key)}
    if name is not None:
        if not isinstance(name, str) or len(name.strip()) > 100 or "\x00" in name:
            raise ValidationError("name must be text of at most 100 characters")
        body["name"] = name
    return body


def _path(fingerprint: Any) -> str:
    if not isinstance(fingerprint, str) or _FINGERPRINT.fullmatch(fingerprint) is None:
        raise ValidationError("Use a fingerprint from ssh_keys.list(), such as \"SHA256:...\"")
    return _BASE + "/" + quote(fingerprint, safe="")


def _keys(response: Any) -> list[dict[str, Any]]:
    keys = response.get("keys") if isinstance(response, dict) else None
    if not isinstance(keys, list) or any(not isinstance(key, dict) for key in keys):
        raise APIError("SSH key list response is invalid", body=response)
    return keys


def _blob(public_key: Any) -> tuple[str, ...]:
    """The key type and data, which identify a key regardless of its comment."""
    return tuple(public_key.split()[:2]) if isinstance(public_key, str) else ()


def listed(keys: list[dict[str, Any]], public_key: str) -> bool:
    return _blob(public_key) in {_blob(key.get("public_key")) for key in keys}


class SSHKeys:
    """Your SSH public keys, admitted by every machine your team runs."""

    def __init__(self, client: Any):
        self._client = client

    def list(self) -> list[dict[str, Any]]:
        """Saved keys: ``fingerprint``, ``public_key``, ``name`` and ``added_at``."""
        return _keys(self._client._request("GET", _BASE))

    def add(self, public_key: str, *, name: str | None = None) -> dict[str, Any]:
        """Save one public key. Running machines admit it within seconds. Adding a saved key again succeeds."""
        return self._client._request("POST", _BASE, json=_body(public_key, name))

    def remove(self, fingerprint: str) -> None:
        """Remove a saved key by its fingerprint."""
        self._client._request("DELETE", _path(fingerprint))


class AsyncSSHKeys:
    """Asynchronous counterpart of :class:`SSHKeys`."""

    def __init__(self, client: Any):
        self._client = client

    async def list(self) -> list[dict[str, Any]]:
        return _keys(await self._client._request("GET", _BASE))

    async def add(self, public_key: str, *, name: str | None = None) -> dict[str, Any]:
        return await self._client._request("POST", _BASE, json=_body(public_key, name))

    async def remove(self, fingerprint: str) -> None:
        await self._client._request("DELETE", _path(fingerprint))
