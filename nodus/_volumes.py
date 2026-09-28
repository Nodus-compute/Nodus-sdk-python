"""Named storage volumes that sandboxes mount between sessions."""
from __future__ import annotations

from typing import Any, Iterator, AsyncIterator

from .errors import APIError, ValidationError


def _page_params(limit, cursor):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValidationError("Workspace page limit must be between 1 and 1000")
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or len(cursor.encode('utf-8')) > 1024:
                raise ValueError('invalid cursor')
        except (ValueError, UnicodeError):
            raise ValidationError("Workspace cursor must be a string of at most 1024 bytes") from None
    return {"limit": limit, **({"cursor": cursor} if cursor else {})}


def _page(response):
    if not isinstance(response, dict):
        raise APIError("Workspace list response is invalid")
    rows, cursor = response.get("workspaces"), response.get("next_cursor", "")
    if not isinstance(rows, list) or len(rows) > 1000 or any(not isinstance(row, dict) for row in rows):
        raise APIError("Workspace list response is invalid")
    try:
        _page_params(1000, cursor)
        if cursor is None or cursor and not rows:
            raise ValidationError("Invalid page progress")
    except ValidationError:
        raise APIError("Workspace list cursor is invalid") from None
    return rows, cursor


class Volumes:
    """Named volumes a sandbox mounts with ``workspace={"name": ..., "mount": ...}``."""

    def __init__(self, client: Any):
        self._client = client

    def create(self, name: str, *, size_gb: float) -> dict[str, Any]:
        """Create a named volume within the server capacity limit."""
        return self._client._request("POST", "/v1/workspaces", json={"name": name, "size_gb": size_gb})

    def list(self) -> list[dict[str, Any]]:
        """Read owned volume metadata and current writer identities."""
        return list(self.iter())

    def list_page(self, *, limit: int = 1000, cursor: str | None = None) -> tuple[list[dict[str, Any]], str]:
        """Read one page and its opaque continuation cursor."""
        return _page(self._client._request("GET", "/v1/workspaces", params=_page_params(limit, cursor)))

    def iter(self, *, limit: int = 1000) -> Iterator[dict[str, Any]]:
        """Yield volume records one page at a time."""
        cursor = None
        seen = set()
        while True:
            rows, next_cursor = self.list_page(limit=limit, cursor=cursor)
            if next_cursor and next_cursor in seen:
                raise APIError("Workspace pagination repeated a cursor")
            yield from rows
            if not next_cursor:
                return
            seen.add(next_cursor)
            cursor = next_cursor


class AsyncVolumes:
    def __init__(self, client: Any):
        self._client = client

    async def create(self, name: str, *, size_gb: float) -> dict[str, Any]:
        """Create a named volume within the server capacity limit."""
        return await self._client._request("POST", "/v1/workspaces", json={"name": name, "size_gb": size_gb})

    async def list(self) -> list[dict[str, Any]]:
        """Read owned volume metadata and current writer identities."""
        return [row async for row in self.iter()]

    async def list_page(self, *, limit: int = 1000, cursor: str | None = None) -> tuple[list[dict[str, Any]], str]:
        """Read one page and its opaque continuation cursor."""
        return _page(await self._client._request("GET", "/v1/workspaces", params=_page_params(limit, cursor)))

    async def iter(self, *, limit: int = 1000) -> AsyncIterator[dict[str, Any]]:
        """Yield volume records one page at a time."""
        cursor = None
        seen = set()
        while True:
            rows, next_cursor = await self.list_page(limit=limit, cursor=cursor)
            if next_cursor and next_cursor in seen:
                raise APIError("Workspace pagination repeated a cursor")
            for row in rows:
                yield row
            if not next_cursor:
                return
            seen.add(next_cursor)
            cursor = next_cursor
