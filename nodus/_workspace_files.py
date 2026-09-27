"""Revision-guarded saved-file delivery without forwarding account credentials."""
from __future__ import annotations

import hashlib
import asyncio
from contextlib import asynccontextmanager
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ._outputs import verified_file
from .errors import APIConnectionError, NodusError, ValidationError


_HASH = re.compile(r"[a-f0-9]{64}")


def revision(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 9223372036854775807:
        raise ValidationError("storage_revision must be the observed nonnegative saved-file revision")
    return value


def _download_client(asynchronous: bool) -> httpx.Client | httpx.AsyncClient:
    kind = httpx.AsyncClient if asynchronous else httpx.Client
    return kind(timeout=60, follow_redirects=False, trust_env=False)


def _manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != 3 or not _HASH.fullmatch(str(value.get("archive_sha256", ""))):
        raise NodusError("Export has an invalid saved-file manifest")
    segments = value.get("segments")
    if not isinstance(segments, list) or not 1 <= len(segments) <= 100160:
        raise NodusError("Export has an invalid segment count")
    offset = 0
    for segment in segments:
        if not isinstance(segment, dict) or type(segment.get("bytes")) is not int or not 0 < segment["bytes"] <= 67108864 or segment["bytes"] % 512 or type(segment.get("offset")) is not int or segment["offset"] != offset or not _HASH.fullmatch(str(segment.get("sha256", ""))):
            raise NodusError("Export has an invalid saved-file segment")
        offset += segment["bytes"]
    if type(value.get("archive_bytes")) is not int or value["archive_bytes"] != offset or not 1024 <= offset <= 10536870912:
        raise NodusError("Export has an invalid archive length")
    return value


def _page(page: Any, pinned: dict[str, Any], workspace_id: str, storage_revision: int, offset: int) -> list[dict[str, Any]]:
    if not isinstance(page, dict) or page.get("workspace_id") != workspace_id or page.get("storage_revision") != storage_revision or page.get("format") != "research-archive-v3" or page.get("id") != pinned["id"] or page.get("manifest") != pinned["manifest"]:
        raise NodusError("Export revision changed during download")
    segments = page.get("segments")
    manifest = pinned["manifest"]
    if not isinstance(segments, list) or not 1 <= len(segments) <= 256 or offset + len(segments) > len(manifest["segments"]):
        raise NodusError("Export has an invalid segment page")
    for index, segment in enumerate(segments, start=offset):
        expected = manifest["segments"][index]
        if not isinstance(segment, dict) or type(segment.get("index")) is not int or segment["index"] != index or any(segment.get(key) != expected[key] for key in ("sha256", "offset", "bytes")):
            raise NodusError("Export segment does not match the saved manifest")
        address = segment.get("url")
        try:
            parsed = urlsplit(address) if isinstance(address, str) else None
            if parsed is None or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or any(ord(c) < 33 for c in address):
                raise ValueError()
            parsed.port
        except ValueError:
            raise NodusError("Export returned an invalid secure download address") from None
    end = offset + len(segments)
    if end < len(manifest["segments"]):
        if type(page.get("next_offset")) is not int or page["next_offset"] != end:
            raise NodusError("Export has an invalid next segment offset")
    elif page.get("next_offset") is not None:
        raise NodusError("Export has an unexpected extra segment page")
    return segments


def _request(objects: Any, url: str) -> httpx.Request:
    request = objects.build_request("GET", url, headers={"Accept-Encoding": "identity"})
    request.headers.pop("Authorization", None)
    request.headers.pop("Cookie", None)
    return request


def _check_segment(hasher: Any, count: int, segment: dict[str, Any]) -> None:
    if count != segment["bytes"] or hasher.hexdigest() != segment["sha256"]:
        raise NodusError("Export segment integrity check failed. Download was not saved.")


def export_files(client: Any, workspace_id: str, destination: str | os.PathLike[str], *, storage_revision: int, overwrite: bool) -> Path:
    from ._workspaces import _path
    path = _path(workspace_id) + "/files"
    revision(storage_revision)
    pinned = client._request("POST", path + "/export", json={"storage_revision": storage_revision}, max_retries=0)
    if not isinstance(pinned, dict):
        raise NodusError("Export returned no saved-file descriptor")
    manifest = _manifest(pinned.get("manifest"))
    export_path = path + "/exports/" + _path(pinned.get("id")).rsplit("/", 1)[1]
    headers = {"X-Nodus-SHA256": manifest["archive_sha256"], "Content-Length": str(manifest["archive_bytes"])}
    try:
        with verified_file(destination, headers, overwrite=overwrite) as write, _download_client(False) as objects:
            page, offset = pinned, 0
            while True:
                for segment in _page(page, pinned, workspace_id, storage_revision, offset):
                    response = objects.send(_request(objects, segment["url"]), stream=True, follow_redirects=False)
                    try:
                        if response.status_code != 200:
                            raise NodusError("Export segment is unavailable. Retry the export to refresh its download links.")
                        hasher, count = hashlib.sha256(), 0
                        for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                            count += len(chunk)
                            if count > segment["bytes"]:
                                raise NodusError("Export segment exceeds its declared length")
                            hasher.update(chunk)
                            write(chunk)
                        _check_segment(hasher, count, segment)
                    finally:
                        response.close()
                offset += len(page["segments"])
                if offset == len(manifest["segments"]):
                    break
                page = client._request("GET", export_path, params={"offset": offset})
    except httpx.HTTPError:
        raise APIConnectionError("Saved-file download interrupted. Retry the export.") from None
    return Path(destination)


async def export_files_async(client: Any, workspace_id: str, destination: str | os.PathLike[str], *, storage_revision: int, overwrite: bool) -> Path:
    from ._workspaces import _path
    path = _path(workspace_id) + "/files"
    revision(storage_revision)
    pinned = await client._request("POST", path + "/export", json={"storage_revision": storage_revision}, max_retries=0)
    if not isinstance(pinned, dict):
        raise NodusError("Export returned no saved-file descriptor")
    manifest = _manifest(pinned.get("manifest"))
    export_path = path + "/exports/" + _path(pinned.get("id")).rsplit("/", 1)[1]
    headers = {"X-Nodus-SHA256": manifest["archive_sha256"], "Content-Length": str(manifest["archive_bytes"])}
    try:
        with verified_file(destination, headers, overwrite=overwrite) as write:
            async with _download_client(True) as objects:
                page, offset = pinned, 0
                while True:
                    for segment in _page(page, pinned, workspace_id, storage_revision, offset):
                        response = await objects.send(_request(objects, segment["url"]), stream=True, follow_redirects=False)
                        try:
                            if response.status_code != 200:
                                raise NodusError("Export segment is unavailable. Retry the export to refresh its download links.")
                            hasher, count = hashlib.sha256(), 0
                            async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                                count += len(chunk)
                                if count > segment["bytes"]:
                                    raise NodusError("Export segment exceeds its declared length")
                                hasher.update(chunk)
                                write(chunk)
                            _check_segment(hasher, count, segment)
                        finally:
                            await response.aclose()
                    offset += len(page["segments"])
                    if offset == len(manifest["segments"]):
                        break
                    page = await client._request("GET", export_path, params={"offset": offset})
    except httpx.HTTPError:
        raise APIConnectionError("Saved-file download interrupted. Retry the export.") from None
    return Path(destination)


def _transfer(result: Any, manifest: dict[str, Any], workspace_id: str) -> set[int]:
    import json
    from ._workspaces import _path
    canonical = json.dumps(manifest, separators=(",", ":")).encode()
    if not isinstance(result, dict) or result.get("workspace_id") != workspace_id or result.get("manifest_sha256") != hashlib.sha256(canonical).hexdigest() or result.get("manifest_bytes") != len(canonical):
        raise NodusError("Upload intent does not match the selected project archive")
    _path(result.get("id"))
    uploaded = result.get("uploaded_segments")
    if not isinstance(uploaded, list) or any(type(index) is not int or not 0 <= index < len(manifest["segments"]) for index in uploaded):
        raise NodusError("Upload returned an invalid completed-segment list")
    if result.get("state") not in {"uploading", "queued", "verifying", "committed"}:
        raise NodusError("Upload is not active. Inspect its status before creating another upload.", body=result)
    return set(uploaded)


def _segment(archive: Any, segment: dict[str, Any]) -> bytes:
    with archive.path.open("rb") as stream:
        stream.seek(segment["offset"])
        data = stream.read(segment["bytes"])
    if len(data) != segment["bytes"] or hashlib.sha256(data).hexdigest() != segment["sha256"]:
        raise NodusError("The local project archive changed during upload")
    return data


def upload_files(client: Any, workspace_id: str, directory: str | os.PathLike[str], *, idempotency_key: str, replace_revision: int | None) -> dict[str, Any]:
    from ._workspace_archive import build_workspace_archive
    from ._workspaces import _key, _path
    path = _path(workspace_id) + "/transfers"
    body: dict[str, Any] = {"idempotency_key": _key(idempotency_key)}
    if replace_revision is not None:
        body["replace_revision"] = revision(replace_revision)
    with build_workspace_archive(directory) as archive:
        body["manifest"] = archive.manifest
        result = client._request("POST", path, json=body, max_retries=0)
        uploaded = _transfer(result, archive.manifest, workspace_id)
        if result["state"] != "uploading":
            return result
        transfer_path = path + "/" + result["id"]
        for index, segment in enumerate(archive.manifest["segments"]):
            if index in uploaded:
                continue
            target = transfer_path + "/segments/" + str(index)
            data = _segment(archive, segment)
            try:
                response = client._http.request("PUT", target, content=data, headers={"Content-Type": "application/octet-stream"}, follow_redirects=False, timeout=120)
            except httpx.HTTPError:
                raise APIConnectionError("Saved-file upload was interrupted. Retry the unchanged directory with the same key and revision.", body={"transfer_id": result["id"], "idempotency_key": idempotency_key}) from None
            if response.status_code != 204:
                client._raise("PUT", target, response)
                raise NodusError("Saved-file upload returned an unexpected response")
        return client._request("POST", transfer_path + "/finalize", json={}, max_retries=0)


async def _complete(task: Any) -> Any:
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


@asynccontextmanager
async def _prepared_archive(directory: str | os.PathLike[str]):
    from ._workspace_archive import build_workspace_archive
    manager = build_workspace_archive(directory)
    prepared = asyncio.create_task(asyncio.to_thread(manager.__enter__))
    try:
        archive = await asyncio.shield(prepared)
    except BaseException:
        try:
            await _complete(prepared)
        except BaseException:
            pass
        else:
            await _complete(asyncio.create_task(asyncio.to_thread(manager.__exit__, None, None, None)))
        raise
    try:
        yield archive
    finally:
        cleanup = asyncio.create_task(asyncio.to_thread(manager.__exit__, None, None, None))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await _complete(cleanup)
            raise


async def upload_files_async(client: Any, workspace_id: str, directory: str | os.PathLike[str], *, idempotency_key: str, replace_revision: int | None) -> dict[str, Any]:
    from ._workspaces import _key, _path
    path = _path(workspace_id) + "/transfers"
    body: dict[str, Any] = {"idempotency_key": _key(idempotency_key)}
    if replace_revision is not None:
        body["replace_revision"] = revision(replace_revision)
    async with _prepared_archive(directory) as archive:
        body["manifest"] = archive.manifest
        result = await client._request("POST", path, json=body, max_retries=0)
        uploaded = _transfer(result, archive.manifest, workspace_id)
        if result["state"] != "uploading":
            return result
        transfer_path = path + "/" + result["id"]
        for index, segment in enumerate(archive.manifest["segments"]):
            if index in uploaded:
                continue
            target = transfer_path + "/segments/" + str(index)
            data = _segment(archive, segment)
            try:
                response = await client._http.request("PUT", target, content=data, headers={"Content-Type": "application/octet-stream"}, follow_redirects=False, timeout=120)
            except httpx.HTTPError:
                raise APIConnectionError("Saved-file upload was interrupted. Retry the unchanged directory with the same key and revision.", body={"transfer_id": result["id"], "idempotency_key": idempotency_key}) from None
            if response.status_code != 204:
                client._raise("PUT", target, response)
                raise NodusError("Saved-file upload returned an unexpected response")
        return await client._request("POST", transfer_path + "/finalize", json={}, max_retries=0)
