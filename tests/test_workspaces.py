import json
import httpx
from test_sandboxes import SANDBOX,sync_client

def test_named_workspace_creation_attachment_and_listing():
    record={"id":"ws_repo","name":"repo","size_gb":0.1,"holder_id":None,"billing_status":"disabled_no_approved_storage_rate"}
    def handler(request):
        if request.url.path=="/v1/workspaces":
            if request.method=="POST":
                assert json.loads(request.content)=={"name":"repo","size_gb":0.1}
                return httpx.Response(201,json=record)
            return httpx.Response(200,json={"workspaces":[record]})
        assert request.url.path=="/v1/sandboxes"
        assert json.loads(request.content)["workspace"]=={"name":"repo","mount":"/workspace"}
        return httpx.Response(202,json=SANDBOX)
    with sync_client(handler) as client:
        assert client.volumes.create("repo",size_gb=0.1)==record
        client.sandboxes.create(image="python:3.12",workspace={"name":"repo","mount":"/workspace"})
        assert client.volumes.list()==[record]

def test_async_workspace_creation_and_attachment():
    import asyncio
    import nodus
    async def scenario():
        def handler(request):
            if request.url.path == "/v1/workspaces":
                if request.method == "POST":
                    assert json.loads(request.content) == {"name": "repo", "size_gb": 0.1}
                    return httpx.Response(201, json={"name": "repo"})
                return httpx.Response(200, json={"workspaces": [{"name": "repo"}]})
            assert json.loads(request.content)["workspace"] == {"name": "repo", "mount": "/project"}
            return httpx.Response(202, json=SANDBOX)
        async with nodus.AsyncClient(api_key="nk_live_test", base_url="https://nodus.invalid") as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler))
            assert await client.volumes.create("repo", size_gb=0.1) == {"name": "repo"}
            assert await client.volumes.list() == [{"name": "repo"}]
            await client.sandboxes.create(image="python:3.12", workspace={"name": "repo", "mount": "/project"})
    asyncio.run(scenario())


# The public workspace contract is a dictionary, including timestamp strings.
# New metadata must not change legacy records or require a new response model.
_RETENTION_ROWS = [
    {"id": "ws_legacy", "name": "legacy", "size_gb": 0.1},
    {"id": "ws_active", "name": "active", "size_gb": 0.1,
     "expired_at": None, "cleanup_pending": False},
    {"id": "ws_expired", "name": "expired", "size_gb": 0.1,
     "expired_at": "2026-09-24T12:49:15.123456Z", "cleanup_pending": True},
]


def _assert_retention_rows(rows):
    assert rows == _RETENTION_ROWS
    assert "expired_at" not in rows[0] and "cleanup_pending" not in rows[0]
    assert rows[0].get("expired_at") is None
    assert rows[0].get("cleanup_pending", False) is False
    assert rows[1]["expired_at"] is None
    assert rows[1]["cleanup_pending"] is False
    assert rows[2]["expired_at"] == "2026-09-24T12:49:15.123456Z"
    assert rows[2]["cleanup_pending"] is True


def _retention_handler(request):
    assert request.url.path == "/v1/workspaces"
    if request.method == "POST":
        name = json.loads(request.content)["name"]
        return httpx.Response(201, json=next(row for row in _RETENTION_ROWS if row["name"] == name))
    return httpx.Response(200, json={"workspaces": _RETENTION_ROWS})


def test_workspace_retention_metadata_preserves_raw_and_legacy_records():
    with sync_client(_retention_handler) as client:
        _assert_retention_rows([client.volumes.create(row["name"], size_gb=0.1) for row in _RETENTION_ROWS])
        _assert_retention_rows(client.volumes.list())
        rows, cursor = client.volumes.list_page()
        _assert_retention_rows(rows)
        assert cursor == ""
        _assert_retention_rows(list(client.volumes.iter()))


def test_async_workspace_retention_metadata_preserves_raw_and_legacy_records():
    import asyncio
    import nodus

    async def scenario():
        async with nodus.AsyncClient(api_key="nk_live_test", base_url="https://nodus.invalid") as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url="https://nodus.invalid", transport=httpx.MockTransport(_retention_handler))
            _assert_retention_rows([await client.volumes.create(row["name"], size_gb=0.1) for row in _RETENTION_ROWS])
            _assert_retention_rows(await client.volumes.list())
            rows, cursor = await client.volumes.list_page()
            _assert_retention_rows(rows)
            assert cursor == ""
            _assert_retention_rows([row async for row in client.volumes.iter()])

    asyncio.run(scenario())
