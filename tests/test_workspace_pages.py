import asyncio

import httpx
import pytest

import nodus
from nodus.errors import APIError, ValidationError
from test_sandboxes import sync_client


def pages_handler(expected, calls):
    def handler(request):
        assert request.url.path == '/v1/workspaces'
        limit = int(request.url.params['limit'])
        cursor = request.url.params.get('cursor', '')
        offset = int(cursor.removeprefix('opaque-page-')) if cursor else 0
        calls.append(cursor)
        end = min(offset + limit, len(expected))
        return httpx.Response(200, json={'workspaces': expected[offset:end],
                                        'next_cursor': f'opaque-page-{end}' if end < len(expected) else ''})
    return handler


def test_all_workspaces_beyond_old_bound_are_returned_once():
    expected = [{'id': f'ws_{i:04d}', 'name': f'workspace {i}'} for i in range(1507)]
    calls = []
    with sync_client(pages_handler(expected, calls)) as client:
        assert list(client.volumes.iter(limit=37)) == expected
        assert len(calls) == 41 and len(set(calls)) == 41
        calls.clear()
        assert client.volumes.list() == expected
        assert calls == ['', 'opaque-page-1000']
        rows, cursor = client.volumes.list_page(limit=2)
        assert rows == expected[:2] and cursor == 'opaque-page-2'


def test_async_workspace_page_traversal_matches_sync():
    expected = [{'id': f'ws_{i:04d}'} for i in range(1507)]
    async def scenario():
        calls = []
        async with nodus.AsyncClient(api_key='nk_live_test', base_url='https://nodus.invalid') as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url='https://nodus.invalid', transport=httpx.MockTransport(pages_handler(expected, calls)))
            assert [row async for row in client.volumes.iter(limit=37)] == expected
            assert len(calls) == 41
            assert await client.volumes.list() == expected
    asyncio.run(scenario())


@pytest.mark.parametrize('response', [None, {}, {'workspaces': {}}, {'workspaces': [None]},
    {'workspaces': [], 'next_cursor': 'again'}, {'workspaces': [], 'next_cursor': None}])
def test_invalid_server_page_is_not_silent_truncation(response):
    with sync_client(lambda _: httpx.Response(200, json=response)) as client:
        with pytest.raises(APIError):
            client.volumes.list()


def test_repeated_cursor_fails_instead_of_returning_duplicates_or_looping():
    count = 0
    def handler(_):
        nonlocal count
        count += 1
        return httpx.Response(200, json={'workspaces': [{'id': f'ws_{count}'}], 'next_cursor': 'same'})
    with sync_client(handler) as client:
        with pytest.raises(APIError, match='repeated'):
            client.volumes.list()
    assert count == 2


@pytest.mark.parametrize('options', [{'limit': 0}, {'limit': 1001}, {'limit': True},
    {'cursor': 1}, {'cursor': 'x' * 1025}, {'cursor': '\ud800'}])
def test_invalid_page_request_does_not_call_api(options):
    def handler(_):
        pytest.fail('invalid page request reached HTTP')
    with sync_client(handler) as client:
        with pytest.raises(ValidationError):
            client.volumes.list_page(**options)


def test_legacy_server_without_cursor_remains_a_single_page():
    expected = [{'id': 'existing_workspace'}]
    with sync_client(lambda _: httpx.Response(200, json={'workspaces': expected})) as client:
        assert client.volumes.list_page() == (expected, '')
        assert client.volumes.list() == expected
