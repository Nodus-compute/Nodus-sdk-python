import hashlib
import json

import httpx
import pytest

import nodus
import asyncio
API_KEY = "nk_synthetic_workspace_files"
BASE_URL = "https://nodus.invalid"

BASE = "/v1/research-workspaces"

def sync_client(handler, *, max_retries=4):
    client = nodus.Client(api_key=API_KEY, base_url=BASE_URL, max_retries=max_retries)
    headers = dict(client._http.headers)
    client._http.close()
    client._http = httpx.Client(
        base_url=BASE_URL,
        headers=headers,
        transport=httpx.MockTransport(handler),
    )
    return client

async def async_client(handler, *, max_retries=4):
    client = nodus.AsyncClient(api_key=API_KEY, base_url=BASE_URL, max_retries=max_retries)
    headers = dict(client._http.headers)
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url=BASE_URL,
        headers=headers,
        transport=httpx.MockTransport(handler),
    )
    return client

def invoke(handler, asynchronous, method, *args, **kwargs):
    if asynchronous:
        async def run():
            async with await async_client(handler) as client:
                return await getattr(client.workspaces, method)(*args, **kwargs)
        return asyncio.run(run())
    with sync_client(handler) as client:
        return getattr(client.workspaces, method)(*args, **kwargs)


DATA = b'a' * 512 + b'b' * 512
SEGMENTS = [{"sha256": hashlib.sha256(DATA[i:i+512]).hexdigest(), "offset": i, "bytes": 512} for i in (0, 512)]
MANIFEST = {"version": 3, "archive_sha256": hashlib.sha256(DATA).hexdigest(), "archive_bytes": len(DATA), "inventory": {"sha256": "a" * 64, "payload_bytes": 0, "entries": 0}, "segments": SEGMENTS}


def descriptor(offset=0):
    result = {"id": "289d178c-cdec-4a67-8c27-85bdd35ec03b", "workspace_id": "ws_lab", "storage_revision": 3, "expires_at": "2099-01-01T00:00:00Z", "urls_expire_at": "2098-12-31T23:59:00Z", "format": "research-archive-v3", "manifest": MANIFEST, "segments": [{"index": offset, **SEGMENTS[offset], "url": f"https://objects.example/segment-{offset}?signature=secret"}]}
    if offset == 0:
        result["next_offset"] = 1
    return result


@pytest.mark.parametrize("asynchronous", [False, True])
def test_storage_reads_authenticated_account_usage_without_starting_compute(asynchronous):
    calls = []
    expected = {"included_bytes": 10000000000, "retained_bytes": 14, "billable_bytes": 0}
    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path == BASE + "/storage"
        assert request.headers["Authorization"] == "Bearer " + API_KEY
        return httpx.Response(200, json=expected)
    assert invoke(handler, asynchronous, "storage") == expected
    assert len(calls) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("workspace_id", ["", "../other", "ws/a", "https://receiver.example", None])
def test_file_operations_refuse_unowned_route_shapes_before_transport(asynchronous, workspace_id):
    with pytest.raises(nodus.ValidationError):
        invoke(lambda request: pytest.fail("invalid workspace ID reached transport"), asynchronous,
               "delete_files", workspace_id, storage_revision=3)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_delete_saved_files_is_revision_guarded_and_never_repeated(asynchronous, failure):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "DELETE"
        assert request.url.path == BASE + "/ws_lab/files"
        assert request.headers["Authorization"] == "Bearer " + API_KEY
        assert json.loads(request.content) == {"storage_revision": 3}
        return httpx.Response(503, json={"error": "workspace_storage_unavailable"}) if failure else httpx.Response(200, json={"workspace_id": "ws_lab", "storage_revision": 4, "deleted": True})
    if failure:
        with pytest.raises(nodus.NodusError):
            invoke(handler, asynchronous, "delete_files", "ws_lab", storage_revision=3)
    else:
        assert invoke(handler, asynchronous, "delete_files", "ws_lab", storage_revision=3)["deleted"] is True
    assert len(calls) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("revision", [-1, True, 1.5, "3"])
def test_file_deletion_refuses_invalid_revision_before_transport(asynchronous, revision):
    calls = []
    with pytest.raises(nodus.ValidationError):
        invoke(lambda request: calls.append(request), asynchronous, "delete_files", "ws_lab", storage_revision=revision)
    assert calls == []


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("problem", [None, "corrupt", "redirect", "changed_page", "too_long"])
def test_export_streams_pinned_segments_without_credentials_and_preserves_destination(asynchronous, problem, tmp_path, monkeypatch):
    import nodus._workspace_files as files
    calls = []
    def objects(request):
        calls.append(request)
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert request.headers["Accept-Encoding"] == "identity"
        index = int(request.url.path[-1])
        if problem == "redirect":
            return httpx.Response(302, headers={"Location": "https://receiver.example/secret"})
        payload = b'x' * 512 if problem == "corrupt" else DATA[index*512:(index+1)*512]
        if problem == "too_long": payload += b'x'
        return httpx.Response(200, content=payload, headers={"Set-Cookie": "private=never-forward"})
    def control(request):
        assert request.headers["Authorization"] == "Bearer " + API_KEY
        if request.method == "POST":
            assert request.url.path == BASE + "/ws_lab/files/export"
            assert json.loads(request.content) == {"storage_revision": 3}
            return httpx.Response(200, json=descriptor())
        assert request.url.path == BASE + "/ws_lab/files/exports/289d178c-cdec-4a67-8c27-85bdd35ec03b"
        assert request.url.params["offset"] == "1"
        page = descriptor(1)
        if problem == "changed_page": page["storage_revision"] = 4
        return httpx.Response(200, json=page)
    monkeypatch.setattr(files, "_download_client", lambda asynchronous: httpx.AsyncClient(transport=httpx.MockTransport(objects)) if asynchronous else httpx.Client(transport=httpx.MockTransport(objects)))
    destination = tmp_path / "saved.tar"
    destination.write_bytes(b"original")
    if problem:
        with pytest.raises(nodus.NodusError) as error:
            invoke(control, asynchronous, "export_files", "ws_lab", destination, storage_revision=3, overwrite=True)
        assert "signature=secret" not in str(error.value)
        assert destination.read_bytes() == b"original"
        assert all(request.url.host == "objects.example" for request in calls)
    else:
        assert invoke(control, asynchronous, "export_files", "ws_lab", destination, storage_revision=3, overwrite=True) == destination
        assert destination.read_bytes() == DATA
    assert not list(tmp_path.glob(".nodus-download-*"))


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("problem", [None, "upload_timeout", "existing_transfer", "different_manifest"])
def test_folder_upload_uses_canonical_segments_and_retains_explicit_intent(asynchronous, problem, tmp_path):
    directory = tmp_path / "project"
    directory.mkdir()
    (directory / "train.py").write_text("print('saved')\n")
    requests, chunks = [], []
    transfer_id = "f609c808-cd77-4a74-9c0b-262363b65484"
    status = None
    manifest = None
    def control(request):
        nonlocal status, manifest
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer " + API_KEY
        if request.url.path == BASE + "/ws_lab/transfers":
            body = json.loads(request.content)
            assert body["idempotency_key"] == "project-upload-1"
            assert body["replace_revision"] == 3
            manifest = body["manifest"]
            canonical = json.dumps(manifest, separators=(",", ":")).encode()
            status = {"id": transfer_id, "workspace_id": "ws_lab", "state": "uploading", "base_revision": 3, "manifest_sha256": hashlib.sha256(canonical).hexdigest(), "manifest_bytes": len(canonical), "uploaded_segments": []}
            if problem == "different_manifest": status["manifest_sha256"] = "0" * 64
            if problem == "existing_transfer": status["uploaded_segments"] = list(range(len(manifest["segments"])))
            return httpx.Response(201, json=status)
        if request.method == "PUT":
            assert request.headers["Content-Type"] == "application/octet-stream"
            assert request.headers["Content-Length"] == str(len(request.content))
            index = int(request.url.path.rsplit("/", 1)[1])
            expected = manifest["segments"][index]
            assert len(request.content) == expected["bytes"]
            assert hashlib.sha256(request.content).hexdigest() == expected["sha256"]
            chunks.append(request.content)
            if problem == "upload_timeout": raise httpx.ReadTimeout("unknown segment reply", request=request)
            return httpx.Response(204)
        assert request.url.path == BASE + "/ws_lab/transfers/" + transfer_id + "/finalize"
        assert request.method == "POST"
        return httpx.Response(202, json={**status, "state": "queued"})
    if problem in {"upload_timeout", "different_manifest"}:
        with pytest.raises(nodus.NodusError):
            invoke(control, asynchronous, "upload_files", "ws_lab", directory, idempotency_key="project-upload-1", replace_revision=3)
        assert not any(r.url.path.endswith("/finalize") for r in requests)
        assert len(chunks) <= 1
    else:
        result = invoke(control, asynchronous, "upload_files", "ws_lab", directory, idempotency_key="project-upload-1", replace_revision=3)
        assert result["state"] == "queued"
        if problem == "existing_transfer": assert chunks == []
        else: assert hashlib.sha256(b"".join(chunks)).hexdigest() == manifest["archive_sha256"]


def test_async_upload_preparation_yields_and_cancellation_cleans_archive(tmp_path, monkeypatch):
    import asyncio
    import threading
    from contextlib import contextmanager
    import nodus._workspace_archive as archive
    directory = tmp_path / "project"
    directory.mkdir()
    (directory / "train.py").write_text("print('saved')\n")
    started, release = threading.Event(), threading.Event()
    paths = []
    original = archive.build_workspace_archive
    @contextmanager
    def delayed(path):
        started.set()
        assert release.wait(2), "archive preparation blocked the event loop"
        with original(path) as result:
            paths.append(result.path)
            yield result
    monkeypatch.setattr(archive, "build_workspace_archive", delayed)
    async def run():
        async with await async_client(lambda request: pytest.fail("cancelled preparation cannot upload")) as client:
            upload = asyncio.create_task(client.workspaces.upload_files("ws_lab", directory, idempotency_key="cancel-test"))
            try:
                for _ in range(100):
                    if started.is_set(): break
                    await asyncio.sleep(0.005)
                assert started.is_set()
                assert not upload.done()
                upload.cancel()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await upload
    asyncio.run(run())
    assert len(paths) == 1
    assert not paths[0].exists()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("code", ["workspace_files_conflict", "workspace_files_format_unsupported", "workspace_files_empty", "workspace_storage_inactive", "workspace_transfer_conflict"])
def test_file_state_conflicts_are_actionable_api_errors(asynchronous, code):
    def handler(request):
        return httpx.Response(409, json={"error": code, "message": "Refresh the saved-file state."})
    with pytest.raises(nodus.APIError) as error:
        invoke(handler, asynchronous, "delete_files", "ws_lab", storage_revision=3)
    assert error.value.code == code
