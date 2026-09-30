import json

import httpx
import nodus
import pytest
from nodus._brief import build_payload
from nodus._workload_file import load_workload_file


@pytest.mark.parametrize("count", [1, 2, 4, 8])
def test_exact_gpu_count_preserves_distributed_command(count):
    command = ["torchrun", "--standalone", f"--nproc_per_node={count}", "train.py"]
    payload = build_payload(command=command, gpu="H100", gpu_count=count, peak_memory_gb=80)
    assert payload["requirements"]["gpu_count"] == count
    assert payload["requirements"]["gpu"] == "H100"
    assert payload["requirements"]["peak_memory_gb"] == 80
    assert payload["source"]["command"] == command


@pytest.mark.parametrize("count", [0, -1, 3, 16, True, 8.0, "8"])
def test_invalid_gpu_count_is_rejected_before_submission(count):
    with pytest.raises(ValueError, match="gpu_count"):
        build_payload(command=["python", "train.py"], gpu_count=count)


def test_omitted_count_and_unverified_topology():
    assert "gpu_count" not in build_payload(command=["python", "train.py"])["requirements"]
    with pytest.raises(ValueError, match="topology"):
        build_payload(command=["python", "train.py"], gpu_count=8, gpu_interconnect="nvlink")


def test_total_gpu_count_preserves_customer_program_without_pin_to_one_machine():
    command = ["python", "train.py", "--config", "distributed.yaml"]
    payload = build_payload(command=command, gpu="H100", total_gpu_count=4)
    assert payload["requirements"] == {"gpu": "H100", "total_gpu_count": 4}
    assert payload["source"]["command"] == command


@pytest.mark.parametrize("count", [0, -1, 2049, True, 4.0, "4"])
def test_invalid_total_gpu_count_is_rejected_before_submission(count):
    with pytest.raises(ValueError, match="total_gpu_count"):
        build_payload(command=["python", "train.py"], requirements={"total_gpu_count": count})


@pytest.mark.parametrize("shape", [{"gpu_count": 1}, {"node_count": 4}, {"compute_class": "vm"}])
def test_total_gpu_count_does_not_override_explicit_machine_or_cpu_requirements(shape):
    with pytest.raises(ValueError, match="total_gpu_count"):
        build_payload(command=["python", "train.py"], requirements={"total_gpu_count": 4, **shape})


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_total_gpu_count_reaches_sync_and_async_submission(asynchronous):
    sent = []

    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/v1/workloads"
        sent.append(json.loads(request.content))
        return httpx.Response(202, json={"workload_id": "wl_fixture"})

    if asynchronous:
        async with nodus.AsyncClient(api_key="fixture", base_url="https://nodus.invalid") as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler))
            await client.run(command=["python", "train.py"], gpu="H100", total_gpu_count=7)
    else:
        with nodus.Client(api_key="fixture", base_url="https://nodus.invalid") as client:
            client._http.close()
            client._http = httpx.Client(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler))
            client.run(command=["python", "train.py"], gpu="H100", total_gpu_count=7)
    assert len(sent) == 1
    assert sent[0]["requirements"] == {"gpu": "H100", "total_gpu_count": 7}
    assert sent[0]["source"]["command"] == ["python", "train.py"]


@pytest.mark.parametrize("shape", ["total_gpu_count=7\n", "[requirements]\ntotal_gpu_count=7\n"])
def test_workload_file_preserves_total_gpu_intent(tmp_path, shape):
    source = tmp_path / "workload.toml"
    source.write_text('command=["python", "train.py"]\n' + shape)
    assert build_payload(**load_workload_file(source))["requirements"] == {"total_gpu_count": 7}
