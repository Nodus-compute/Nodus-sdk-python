import shlex
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nodus import cli


@pytest.mark.parametrize("filename", ["nodus.toml", "training.toml", "my training.toml"])
def test_init_guidance_names_created_file(filename, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", filename]) == 0
    assert (tmp_path / filename).is_file()
    output = capsys.readouterr().out
    if filename == "nodus.toml":
        assert "then run nodus run." in output
    else:
        command = next(line for line in output.splitlines() if line.startswith("nodus run "))
        assert shlex.split(command) == ["nodus", "run", filename]
        assert f'"{filename}"' in command


@pytest.mark.parametrize("filename", ["Sam's $training`file.toml", "line\nbreak.toml", "-training.toml", r"\\server\training.toml"])
def test_init_metacharacter_guidance_does_not_offer_unsafe_command(filename, monkeypatch, capsys):
    monkeypatch.setattr(cli, "write_workload_file", lambda path: path)
    assert cli._cmd_init(SimpleNamespace(file=filename)) == 0
    output = capsys.readouterr().out
    assert "pass its filename to nodus run" in output
    assert not any(line.startswith("nodus run ") for line in output.splitlines())


@pytest.mark.parametrize("status,message", [
    (None, "No runs yet. Start with nodus init, then nodus run."),
    ("active", "No active runs."),
    ("failed", "No runs with status failed."),
    ("mine", "No runs submitted by you."),
    ("team", "No runs for this team."),
])
def test_empty_list_guidance_matches_filter(status, message, monkeypatch, capsys):
    client = MagicMock()
    client.__enter__.return_value = client
    client.list.return_value = []
    monkeypatch.setattr(cli, "Client", lambda **kwargs: client)
    args = SimpleNamespace(base_url=None, status=status, limit=20, json=False, plain=True)
    assert cli._cmd_list(args) == 0
    output = capsys.readouterr().out
    assert message in output
    if status:
        assert "Start with" not in output
    filters = {"scope": status} if status in ("mine", "team") else {"status": status}
    client.list.assert_called_once_with(limit=20, **filters)
@pytest.mark.parametrize('data,expected', [
    ({'resources': {'accelerator': 'H100', 'device_memory_gb': 80, 'gpu_count': 8}}, '8 × H100 (80 GB per GPU)'),
    ({'offer_id': 'nodus:compute-0-any', 'fit_class': 'compute'}, 'Not reported'),
    ({'resources': {'accelerator': '\x1b[31mRTX\n4090', 'device_memory_gb': 24}}, 'RTX 4090 (24 GB)'),
])
def test_status_and_list_use_same_reported_compute(data, expected, monkeypatch, capsys):
    from nodus._terminal import workload_rows
    from nodus.types import Route
    workload = SimpleNamespace(id='wl_fixture', status='completed', route=Route.from_dict(data),
                               cost_now_usd=0, stages=[], raw={})
    assert dict(workload_rows(workload))['Compute'] == expected
    client = MagicMock()
    client.__enter__.return_value = client
    client.list.return_value = [workload]
    monkeypatch.setattr(cli, 'Client', lambda **kwargs: client)
    args = SimpleNamespace(base_url=None, status=None, limit=20, json=False, plain=True)
    assert cli._cmd_list(args) == 0
    output = capsys.readouterr().out
    assert expected in output
    assert 'nodus:compute' not in output
    assert '\x1b' not in output
    client.get.assert_not_called()


def test_status_reports_whole_node_hourly_price():
    from nodus._terminal import workload_rows
    from nodus.types import Route
    route = Route.from_dict({'price_usd_hour': 16, 'resources': {'accelerator': 'H100', 'device_memory_gb': 80, 'gpu_count': 8}})
    workload = SimpleNamespace(id='wl_fixture', status='running', route=route,
                               cost_now_usd=0, stages=[], raw={})
    assert dict(workload_rows(workload))['Node hourly price'] == '$16.0000/hour'
    assert any('8 × H100 (80 GB per GPU)' in line for line in cli._fmt_route(route))

@pytest.mark.parametrize("command", [["--plain", "status", "wl_bill"], ["--plain", "list"]])
def test_terminal_workload_cli_waits_for_final_cost(command, monkeypatch, capsys):
    import httpx
    import nodus

    finalized = False

    def factory(**kwargs):
        def handler(request):
            body = {"id": "wl_bill", "status": "completed", "spend_usd": 0.20,
                    "meter": {"settled_usd": 0.20, "accruing_usd": 0.10,
                              "charge_state": "final" if finalized else "estimated"}}
            if finalized:
                body["meter"]["final_charge_usd"] = 1.25
            return httpx.Response(200, json={"workloads": [body]} if request.url.path == "/v1/workloads" else body)
        client = nodus.Client(api_key="test", base_url="https://nodus.invalid")
        client._http = httpx.Client(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler))
        return client

    monkeypatch.setattr(cli, "Client", factory)
    assert cli.main(command) == 0
    pending = capsys.readouterr().out
    assert "Finalizing cost" in pending and "$0.30" not in pending
    finalized = True
    assert cli.main(command) == 0
    final = capsys.readouterr().out
    assert "$1.25" in final and "Finalizing" not in final
