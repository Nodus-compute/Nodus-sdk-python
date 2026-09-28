import asyncio
import json

import httpx
import pytest

import nodus
from nodus._brief import build_payload


def test_single_source_assets_and_outputs_compile_to_real_stage():
    payload = build_payload(image="image:v1", command=["python", "train.py"], budget=5,
                            source_asset_id="asset_code", inputs=[{"name": "data", "asset_id": "asset_data"}],
                            outputs={"model": "model.bin"})
    assert "source" not in payload
    assert payload["stages"] == [{"id": "main", "source": {"image": "image:v1", "command": ["python", "train.py"], "asset_id": "asset_code"}, "outputs": {"model": "model.bin"}}]
    assert payload["inputs"] == [{"name": "data", "asset_id": "asset_data"}]
    assert payload["outcome"] == {"max_cost_usd": 5}


@pytest.mark.parametrize("options", [
    {"source_asset_id": "bad/id"},
    {"inputs": [{"name": "data", "uri": "https://example.com"}]},
    {"outputs": {"model": "../outside"}},
    {"outputs": {"../model": "model.bin"}},
    {"outputs": {"model": "/absolute"}},
    {"outputs": {"CON": "model.bin"}},
    {"outputs": {"model.": "model.bin"}},
    {"outputs": {"model": "model.bin", "MODEL": "another.bin"}},
    # The server stores results only under 1-64 of [a-z0-9._-] with no leading
    # dot or hyphen, and refuses anything else at submission.
    {"outputs": {"Model": "model.bin"}},
    {"outputs": {"-model": "model.bin"}},
    {"outputs": {".model": "model.bin"}},
    {"outputs": {"a" * 65: "model.bin"}},
    {"outputs": {"nodus.meta": "model.bin"}},
    {"stages": [{"id": "CON", "outputs": {"model": "model.bin"}}]},
    {"outputs": {"model": "model.bin"}, "framework": "train_eval"},
    {"stages": [{"id": "train"}], "source_asset_id": "asset_code"},
])
def test_invalid_new_fields_fail_before_submission(options):
    with pytest.raises((ValueError, TypeError)):
        build_payload(budget=5, **options)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_python_run_sends_new_fields(asynchronous):
    import json
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(202, json={"id": "wl_test", "status": "accepted"})
    cls = nodus.AsyncClient if asynchronous else nodus.Client
    client = cls(api_key="nk_test", base_url="https://nodus.invalid")
    http = httpx.AsyncClient if asynchronous else httpx.Client
    client._http = http(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler))
    args = dict(command=["python", "train.py"], budget=5, source_asset_id="asset_code",
                inputs=[{"name": "data", "asset_id": "asset_data", "cache": True}], outputs={"model": "model.bin"})
    if asynchronous:
        async def run():
            async with client:
                return await client.run(**args)
        result = asyncio.run(run())
    else:
        with client:
            result = client.run(**args)
    assert result.id == "wl_test"
    assert seen[0]["stages"][0]["source"]["asset_id"] == "asset_code"
    assert seen[0]["inputs"][0]["asset_id"] == "asset_data"
    assert seen[0]["inputs"][0]["cache"] is True

@pytest.mark.parametrize("cache", [True, False])
def test_input_cache_flag_survives_submission(cache):
    payload = build_payload(command=["python", "train.py"], budget=5,
                            inputs=[{"name": "weights", "asset_id": "asset_weights", "cache": cache}])
    assert payload["inputs"] == [{"name": "weights", "asset_id": "asset_weights", "cache": cache}]


@pytest.mark.parametrize("cache", [1, "true", None])
def test_input_cache_requires_explicit_boolean(cache):
    with pytest.raises(ValueError):
        build_payload(command=["python", "train.py"], budget=5,
                      inputs=[{"name": "weights", "asset_id": "asset_weights", "cache": cache}])


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("options", [
    {"assets": ["private-asset-value"]},
    {"asset_id": "private-asset-value"},
    {"extra": {"assets": ["private-asset-value"]}},
    {"extra": {"asset_id": "private-asset-value"}},
    {"extra": {"source_asset_id": "private-asset-value"}},
])
def test_asset_alias_refusal_guides_a_valid_submission(asynchronous, options):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(202, json={
            "id": "wl_test", "workload_id": "wl_test", "status": "accepted", "revision": 1,
        })

    cls = nodus.AsyncClient if asynchronous else nodus.Client
    client = cls(api_key="nk_test", base_url="https://nodus.invalid")
    http = httpx.AsyncClient if asynchronous else httpx.Client
    arguments = {"command": ["python", "train.py"], "budget": 5}
    corrected = {
        **arguments,
        "source_asset_id": "asset_code",
        "inputs": [{"name": "training", "asset_id": "asset_data"}],
    }

    def check_refusal(error):
        message = str(error)
        assert "source_asset_id=asset.id" in message
        assert 'inputs=[{"name": "training", "asset_id": dataset.id}]' in message
        assert "Pass extra=" not in message
        assert "private-asset-value" not in message
        assert not requests

    if asynchronous:
        async def run():
            await client._http.aclose()
            client._http = http(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler))
            async with client:
                with pytest.raises(TypeError) as raised:
                    await client.run(**arguments, **options)
                check_refusal(raised.value)
                return await client.run(**corrected)
        result = asyncio.run(run())
    else:
        client._http.close()
        client._http = http(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler))
        with client:
            with pytest.raises(TypeError) as raised:
                client.run(**arguments, **options)
            check_refusal(raised.value)
            result = client.run(**corrected)

    assert result.id == "wl_test"
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/workloads"
    body = json.loads(requests[0].content)
    assert body["source"] == {
        "image": "python:3.11-slim", "command": ["python", "train.py"], "asset_id": "asset_code",
    }
    assert body["inputs"] == [{"name": "training", "asset_id": "asset_data"}]
    assert body["outcome"] == {"max_cost_usd": 5}
    assert not {"assets", "asset_id", "source_asset_id"} & body.keys()
