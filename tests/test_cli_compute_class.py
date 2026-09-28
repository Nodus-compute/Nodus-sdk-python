"""Workload files provide validated settings to short CLI commands."""
import json
from types import SimpleNamespace

import httpx
import pytest

import nodus
from nodus import cli


@pytest.mark.parametrize('command', ['submit', 'run'])
def test_file_submission_and_observation(command, monkeypatch, tmp_path, capsys):
    sent = []
    methods = []
    def handler(req):
        methods.append(req.method)
        if req.method == 'POST':
            sent.append(json.loads(req.content))
            return httpx.Response(202, json={'id': 'wl_test', 'status': 'accepted'})
        return httpx.Response(200, json={'id': 'wl_test', 'status': 'completed'})
    client = nodus.Client(api_key='nk_test', base_url='https://nodus.invalid')
    client._http = httpx.Client(base_url='https://nodus.invalid', transport=httpx.MockTransport(handler))
    monkeypatch.setattr(cli, 'Client', lambda **kwargs: client)
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'nodus.toml').write_text('image = "training:v1"\ncommand = ["python", "train.py"]\nbudget = 5\n')
    assert cli.main([command]) == 0
    assert sent[0]['source']['command'] == ['python', 'train.py']
    assert sent[0]['requirements'].get('compute_class') in (None, 'accelerator')
    assert methods == (['POST', 'GET'] if command == 'run' else ['POST'])
    output = capsys.readouterr().out
    assert output.startswith('wl_test\n')
    assert ('Completed' in output) == (command == 'run')


@pytest.mark.parametrize('content', [None, 'image = ', 'budget = -1\n', 'unknown = true\n'])
def test_invalid_config_fails_before_client(content, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    if content is not None:
        (tmp_path / 'nodus.toml').write_text(content)
    def forbidden(**kwargs):
        pytest.fail('Client must not be constructed for invalid configuration')
    monkeypatch.setattr(cli, 'Client', forbidden)
    assert cli.main(['run']) == 2
    output = capsys.readouterr()
    assert 'Error:' in output.err
    assert 'Traceback' not in output.err


def test_init_never_overwrites(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(['init']) == 0
    path = tmp_path / 'nodus.toml'
    content = path.read_text()
    assert 'image' in content and 'budget' not in content
    assert cli.main(['init']) == 2
    assert path.read_text() == content
    assert 'Error:' in capsys.readouterr().err


@pytest.mark.parametrize('argv', [['get', 'wl_test'], ['run', '--image', 'image'], ['run', '--', 'python', 'train.py'], ['list', '--status', 'active']])
def test_removed_cli_surface_rejected(argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


def test_grouped_help_has_no_get_alias():
    output = cli.build_parser().format_help()
    for title in ['Setup:', 'Run:', 'Monitor:', 'Results:', 'Advanced:']:
        assert title in output
    assert 'get,' not in output


def test_download_prints_paths(monkeypatch, tmp_path, capsys):
    from contextlib import nullcontext
    path = tmp_path / 'model.bin'
    client = SimpleNamespace(get=lambda workload_id: SimpleNamespace(download=lambda: [path]))
    monkeypatch.setattr(cli, 'Client', lambda **kwargs: nullcontext(client))
    assert cli.main(['download', 'wl_test']) == 0
    assert str(path) in capsys.readouterr().out


def test_upload_streams_file_and_assets_sanitizes_rows(monkeypatch, tmp_path, capsys):
    path = tmp_path / "data.jsonl"
    path.write_bytes(b'{"text":"hello"}\n')
    requests = []
    def handler(req):
        requests.append(req)
        if req.method == "POST":
            assert req.url.path == "/v1/assets/upload"
            assert req.url.params["name"] == path.name
            assert req.content == path.read_bytes()
            return httpx.Response(201, json={"id": "asset_data", "state": "ready"})
        return httpx.Response(200, json={"max_import_bytes": 1000, "assets": [
            {"id": "asset_data", "state": "ready", "name": "data\nforged\x1b[31m"}
        ]})
    def make_client(**kwargs):
        client = nodus.Client(api_key="nk_test", base_url="https://nodus.invalid")
        client._http = httpx.Client(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler))
        return client
    monkeypatch.setattr(cli, "Client", make_client)
    assert cli.main(["upload", str(path)]) == 0
    assert capsys.readouterr().out == "asset_data\n"
    assert cli.main(["assets"]) == 0
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 2
    assert "\x1b" not in output
    assert len(requests) == 3


def test_missing_upload_fails_before_client(monkeypatch, tmp_path, capsys):
    def forbidden(**kwargs):
        pytest.fail("Missing file must fail before constructing a client")
    monkeypatch.setattr(cli, "Client", forbidden)
    assert cli.main(["upload", str(tmp_path / "absent")]) == 2
    assert "exists on this computer" in capsys.readouterr().err
