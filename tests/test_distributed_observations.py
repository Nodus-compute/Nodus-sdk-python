import asyncio

import httpx
import pytest

import nodus


@pytest.mark.parametrize("asynchronous", [False, True])
def test_member_logs_keep_existing_filters_and_opaque_cursor(asynchronous):
    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/logs/live"):
            return httpx.Response(200, json={"chunks": [], "next_cursor": "member-bound-next", "truncated": True})
        if request.url.path.endswith("/logs"):
            return httpx.Response(200, text="customer log text\n")
        return httpx.Response(200, json={"id": "wl_group", "status": "running", "distributed": [{"group_id": "group_a", "members": [{"member_id": "group_a:1"}]}]})

    async def run_async():
        async with nodus.AsyncClient(api_key="nk_test", base_url="https://nodus.invalid") as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler))
            workload = await client.get("wl_group")
            assert workload.raw["distributed"][0]["members"][0]["member_id"] == "group_a:1"
            assert await workload.logs(stage="train", generation=2, member_id="group_a:1") == "customer log text\n"
            page = await client.live_logs("wl_group", after="member-bound-before", member_id="group_a:1")
            assert page["next_cursor"] == "member-bound-next"

    if asynchronous:
        asyncio.run(run_async())
    else:
        with nodus.Client(api_key="nk_test", base_url="https://nodus.invalid") as client:
            client._http.close()
            client._http = httpx.Client(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler))
            workload = client.get("wl_group")
            assert workload.raw["distributed"][0]["members"][0]["member_id"] == "group_a:1"
            assert workload.logs(stage="train", generation=2, member_id="group_a:1") == "customer log text\n"
            page = client.live_logs("wl_group", after="member-bound-before", member_id="group_a:1")
            assert page["next_cursor"] == "member-bound-next"

    assert seen == [
        ("/v1/workloads/wl_group", {}),
        ("/v1/workloads/wl_group/logs", {"stage": "train", "generation": "2", "member_id": "group_a:1"}),
        ("/v1/workloads/wl_group/logs/live", {"after": "member-bound-before", "member_id": "group_a:1"}),
    ]
