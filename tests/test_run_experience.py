import asyncio
import io
import json
import warnings

import httpx
import pytest

import nodus
from nodus import cli, config
from nodus._brief import build_payload
from nodus._workload_file import load_workload_file


@pytest.mark.parametrize('optimization', ['automatic', 'lowest_cost', 'lower_cost', 'balanced', 'faster', 'fastest'])
def test_optimization_and_gpu_share_normalized_wire(optimization):
    payload = build_payload(command='python train.py', optimization=optimization, gpu='nvidia rtx4090', budget=1)
    assert payload['requirements'] == {'optimization': optimization, 'gpu': 'RTX 4090'}


def test_defaults_and_nested_precedence():
    assert 'optimization' not in build_payload(command='python train.py', budget=1)['requirements']
    req = {'optimization': 'fastest', 'gpu': 'h100'}
    payload = build_payload(command='python train.py', optimization='faster', gpu='A100', requirements=req, budget=1)
    assert payload['requirements'] == {'optimization': 'fastest', 'gpu': 'H100'}
    assert req['gpu'] == 'h100'


@pytest.mark.parametrize('kwargs', [
    {'optimization': ''}, {'optimization': 'cheap'}, {'gpu': ''}, {'gpu': 'A100-80GB'},
    {'requirements': {'gpu': 'made-up'}},
    {'stages': [{'id': 'train', 'requirements': {'optimization': 'cheap'}}]},
])
def test_invalid_placement_rejected(kwargs):
    with pytest.raises(ValueError):
        build_payload(budget=1, **kwargs)


@pytest.mark.parametrize('kwargs', [
    {'expected_runtime_hours': 1}, {'requirements': {'expected_runtime_hours': 1}},
    {'stages': [{'id': 'train', 'requirements': {'expected_runtime_hours': 1}}]},
    {'extra': {'expected_runtime_hours': 1}},
])
def test_removed_runtime_has_actionable_error(kwargs):
    with pytest.raises((TypeError, ValueError), match='Remove expected_runtime_hours'):
        build_payload(budget=1, **kwargs)


def test_file_validation_matches_python(tmp_path):
    path = tmp_path / 'nodus.toml'
    path.write_text('command="python train.py"\ngpu="NVIDIA A6000"\noptimization="faster"\n')
    assert load_workload_file(path)['gpu'] == 'RTX A6000'
    path.write_text('command="python train.py"\nexpected_runtime_hours=1\n')
    with pytest.raises(ValueError, match='Remove expected_runtime_hours'):
        load_workload_file(path)


def test_uncapped_submission_has_no_python_warning():
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter('always')
        build_payload(command='python train.py')
    assert not seen


def test_uncapped_notice_does_not_assume_an_account_cap(monkeypatch, capsys):
    monkeypatch.setattr('sys.stderr.isatty', lambda: True)
    build_payload(command='python train.py')
    notice = capsys.readouterr().err
    assert not notice


def test_sync_and_async_run_wire_parity():
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(202, json={'id': 'wl_test', 'status': 'accepted'})
    with nodus.Client(api_key='test_key', base_url='https://example.test') as client:
        client._http.close()
        client._http = httpx.Client(base_url='https://example.test', transport=httpx.MockTransport(handler))
        client.run(command='python train.py', gpu='a100', optimization='fastest', budget=1)
    async def run():
        async with nodus.AsyncClient(api_key='test_key', base_url='https://example.test') as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url='https://example.test', transport=httpx.MockTransport(handler))
            await client.run(command='python train.py', gpu='a100', optimization='fastest', budget=1)
    asyncio.run(run())
    assert seen[0] == seen[1]
    assert seen[0]['requirements']['gpu'] == 'A100'


def test_progress_metrics_and_sanitization():
    from nodus._terminal import clean, stage_progress
    from nodus.types import StageRun
    stage = StageRun.from_dict({'metric_step': 3, 'metric_total_steps': 10, 'metric_epoch': 1.2, 'metric_total_epochs': 4})
    label, fraction = stage_progress(stage)
    assert '3/10' in label and fraction == .3
    label, fraction = stage_progress(StageRun.from_dict({'metric_epoch': 1.5}))
    assert '1.5' in label and fraction is None
    assert clean('\x1b[31mhello\x1b[0m\x1b]0;bad\x07') == 'hello'


@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
def test_finished_stage_without_metrics_reports_status(status):
    from nodus._terminal import stage_progress
    from nodus.types import StageRun
    assert stage_progress(StageRun.from_dict({'status': status})) == (status.capitalize(), None)


def test_finished_list_does_not_promise_compute(monkeypatch, capsys):
    client = nodus.Client(api_key='test_key', base_url='https://example.test')
    workload = nodus.Workload(client)
    workload._absorb({'id': 'wl_test', 'status': 'cancelled'})
    monkeypatch.setattr(cli, 'Client', lambda **kwargs: client)
    monkeypatch.setattr(client, 'list', lambda **kwargs: [workload])
    assert cli.main(['list']) == 0
    output = capsys.readouterr().out
    assert 'Not reported' in output
    assert 'Pending' not in output


def test_login_missing_identity_endpoint_is_actionable(monkeypatch, capsys):
    monkeypatch.setattr(config, 'read_credentials', lambda: ('test_key', 'https://example.test'))
    monkeypatch.setattr(nodus, 'read_credentials', config.read_credentials)
    def request(*args, **kwargs):
        raise nodus.NotFoundError('not found', status_code=404)
    monkeypatch.setattr(nodus.Client, '_request', request)
    assert cli.main(['login']) == 2
    message = capsys.readouterr().err
    assert 'sign-in verification' in message
    assert 'saved sign-in is unchanged' in message


def test_login_reuses_valid_key(monkeypatch, capsys):
    monkeypatch.setattr(config, 'read_credentials', lambda: ('test_key', 'https://example.test'))
    monkeypatch.setattr(nodus, 'read_credentials', config.read_credentials)
    monkeypatch.setattr(config, 'ensure_writable', lambda: pytest.fail('reuse must not write'))
    def request(self, method, path, **kwargs):
        assert (method, path) == ('GET', '/v1/me')
        return {'email': 'person@example.test', 'tenant': 'ten_hidden', 'key_id': 'key_test'}
    monkeypatch.setattr(nodus.Client, '_request', request)
    assert cli.main(['login']) == 0
    out = capsys.readouterr().out
    assert 'Already signed in as person@example.test' in out
    assert 'ten_hidden' not in out


def test_login_network_failure_preserves_key(monkeypatch, capsys):
    monkeypatch.setattr(config, 'read_credentials', lambda: ('test_key', 'https://example.test'))
    monkeypatch.setattr(nodus, 'read_credentials', config.read_credentials)
    monkeypatch.setattr(config, 'ensure_writable', lambda: pytest.fail('must not replace on network failure'))
    def request(*args, **kwargs):
        raise nodus.APIConnectionError('offline')
    monkeypatch.setattr(nodus.Client, '_request', request)
    assert cli.main(['login']) == 2
    assert capsys.readouterr().err


@pytest.mark.parametrize('asynchronous', [False, True])
def test_wait_shows_events_logs_and_truthful_progress(asynchronous, capsys):
    requests = []
    polls = 0
    def handler(request):
        nonlocal polls
        requests.append(request.url.path)
        if request.url.path.endswith('/events'):
            return httpx.Response(200, json={'events': [{'id': 1, 'event_type': 'workload.running', 'payload': {}}]})
        if request.url.path.endswith('/logs/live'):
            return httpx.Response(200, json={'chunks': [{'id': 1, 'stage_id': 'main', 'generation': 1, 'text': 'training output [bold]literal[/bold]\n'}], 'next_cursor': '1', 'truncated': False})
        polls += 1
        return httpx.Response(200, json={'id': 'wl_test', 'status': 'completed' if polls > 1 else 'running', 'stages': [{'id': 'main', 'metric_step': 3, 'metric_total_steps': 10}]})
    if asynchronous:
        async def run():
            async with nodus.AsyncClient(api_key='test_key', base_url='https://example.test') as client:
                await client._http.aclose()
                client._http = httpx.AsyncClient(base_url='https://example.test', transport=httpx.MockTransport(handler))
                await client.wait('wl_test', progress=True, poll_seconds=.001)
        asyncio.run(run())
    else:
        with nodus.Client(api_key='test_key', base_url='https://example.test') as client:
            client._http.close()
            client._http = httpx.Client(base_url='https://example.test', transport=httpx.MockTransport(handler))
            client.wait('wl_test', progress=True, poll_seconds=.001)
    output = capsys.readouterr()
    assert not output.out
    assert '3/10' in output.err and '30%' in output.err
    assert output.err.count('training output [bold]literal[/bold]') == 1
    assert 'Running' in output.err


def test_wait_disabled_has_no_observation_requests(capsys):
    def handler(request):
        assert request.url.path == '/v1/workloads/wl_test'
        return httpx.Response(200, json={'id': 'wl_test', 'status': 'completed'})
    with nodus.Client(api_key='test_key', base_url='https://example.test') as client:
        client._http.close()
        client._http = httpx.Client(base_url='https://example.test', transport=httpx.MockTransport(handler))
        client.wait('wl_test', progress=False)
    assert not capsys.readouterr().err


def test_cli_structured_status_and_list(monkeypatch, capsys):
    client = nodus.Client(api_key='test_key', base_url='https://example.test')
    workload = nodus.Workload(client)
    workload._absorb({'id': 'wl_test', 'status': 'running', 'spend_usd': 1.23})
    monkeypatch.setattr(cli, 'Client', lambda **kwargs: client)
    monkeypatch.setattr(client, 'list', lambda **kwargs: [workload])
    monkeypatch.setattr(client, 'get', lambda wid: workload)
    assert cli.main(['list']) == 0
    output = capsys.readouterr().out
    assert 'Run' in output and 'Status' in output and 'Cost' in output
    assert cli.main(['status', 'wl_test']) == 0
    output = capsys.readouterr().out
    assert 'Status' in output and 'Cost' in output


def test_live_chunks_replayed_as_a_page_are_not_duplicated(capsys):
    from nodus._terminal import RunProgress
    body = {'chunks': [{'id': 1, 'text': 'first\n'}, {'id': 2, 'text': 'second\n'}], 'next_cursor': '2'}
    with RunProgress('wl_test', True) as display:
        display.accept('logs', body)
        display.accept('logs', body)
    output = capsys.readouterr().err
    assert output.count('first') == 1 and output.count('second') == 1


def test_legacy_logs_fallback_is_incremental(capsys):
    from nodus._terminal import RunProgress
    with RunProgress('wl_test', True) as display:
        display.unavailable('logs', 404)
        assert any(kind == 'saved_logs' for kind, _, _ in display.requests())
        display.accept_saved_logs('one\n', 'main', '1')
        display.accept_saved_logs('one\ntwo\n', 'main', '1')
        display.accept_saved_logs('restarted\n', 'main', '2')
    output = capsys.readouterr().err
    assert output.count('one') == 1 and output.count('two') == 1
    assert 'restarted' in output


def test_wait_progress_cleanup_does_not_redirect_global_streams(monkeypatch):
    import sys
    from nodus._terminal import RunProgress
    class TTY(io.StringIO):
        def isatty(self):
            return True
    stream = TTY()
    monkeypatch.setenv('TERM', 'xterm-256color')
    monkeypatch.setattr(sys, 'stderr', stream)
    stdout = sys.stdout
    with RunProgress('wl_test', None) as display:
        assert display.enabled
        assert sys.stdout is stdout and sys.stderr is stream
    assert sys.stdout is stdout and sys.stderr is stream


def test_cli_transient_logs_error_does_not_claim_missing(monkeypatch, capsys):
    client = nodus.Client(api_key='test_key', base_url='https://example.test')
    monkeypatch.setattr(cli, 'Client', lambda **kwargs: client)
    def fail(*args, **kwargs):
        raise nodus.APIError('GET /v1/workloads/wl_test/logs failed (502)', status_code=502)
    monkeypatch.setattr(client, 'logs', fail)
    assert cli.main(['logs', 'wl_test']) == 2
    err = capsys.readouterr().err
    assert 'temporarily' in err and 'No logs' not in err and '/v1/' not in err


@pytest.mark.parametrize('key', ['optimization', 'gpu'])
def test_null_nested_choice_is_rejected(key):
    with pytest.raises(ValueError):
        build_payload(requirements={key: None}, budget=1)


def test_explain_does_not_present_runtime_as_a_budget_requirement(monkeypatch, capsys):
    client = nodus.Client(api_key='test_key', base_url='https://example.test')
    workload = nodus.Workload(client)
    workload._absorb({'id': 'wl_test', 'status': 'running', 'route': {'sku': 'nodus:H100', 'expected_hours': 3, 'expected_cost_usd': 12}})
    monkeypatch.setattr(cli, 'Client', lambda **kwargs: client)
    monkeypatch.setattr(client, 'get', lambda wid: workload)
    assert cli.main(['explain', 'wl_test']) == 0
    out = capsys.readouterr().out
    assert 'expected hours' not in out and 'budget is checked against' not in out
    assert 'limit' in out


@pytest.mark.parametrize('force,different', [(True, False), (False, True)])
def test_fresh_login_does_not_send_stored_key_to_other_deployment(force, different, monkeypatch):
    monkeypatch.setattr(config, 'read_credentials', lambda: ('stored_key', 'https://old.example.test'))
    monkeypatch.setattr(nodus, 'read_credentials', config.read_credentials)
    monkeypatch.setattr(nodus.Client, '_request', lambda *a, **kw: pytest.fail('must not send stored key'))
    class StopBeforeMutation(Exception):
        pass
    def stop():
        raise StopBeforeMutation()
    monkeypatch.setattr(config, 'ensure_writable', stop)
    args = ['login', '--base-url', 'https://new.example.test' if different else 'https://old.example.test']
    if force:
        args.append('--force')
    with pytest.raises(StopBeforeMutation):
        cli.main(args)


def test_invalid_saved_key_starts_fresh_flow(monkeypatch):
    monkeypatch.setattr(config, 'read_credentials', lambda: ('stored_key', 'https://example.test'))
    monkeypatch.setattr(nodus, 'read_credentials', config.read_credentials)
    def unauthorized(*args, **kwargs):
        raise nodus.AuthenticationError('expired', status_code=401)
    monkeypatch.setattr(nodus.Client, '_request', unauthorized)
    class FreshFlow(Exception):
        pass
    def stop():
        raise FreshFlow()
    monkeypatch.setattr(config, 'ensure_writable', stop)
    with pytest.raises(FreshFlow):
        cli.main(['login'])


def test_async_cancel_failure_preserves_cancellation_with_guidance():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.method == 'POST':
            return httpx.Response(403, json={'error': 'forbidden'})
        raise asyncio.CancelledError()
    async def run():
        async with nodus.AsyncClient(api_key='test_key', base_url='https://example.test') as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url='https://example.test', transport=httpx.MockTransport(handler))
            with pytest.raises(asyncio.CancelledError) as raised:
                await client.wait('wl_test', progress=False)
            assert calls[-1].endswith('/cancel')
            if hasattr(raised.value, 'add_note'):
                assert 'Cancellation not confirmed' in ' '.join(raised.value.__notes__)
    asyncio.run(run())


def test_no_color_is_respected(monkeypatch):
    from nodus._terminal import console
    monkeypatch.setenv('NO_COLOR', '1')
    assert console().no_color


def test_recovered_observation_clears_temporary_notice():
    from nodus._terminal import RunProgress
    with RunProgress('wl_test', True) as display:
        display.unavailable('events', 503)
        display.accept('events', {'events': []})
        assert not display.notice


@pytest.mark.parametrize('asynchronous', [False, True])
def test_terminal_wait_drains_every_live_log_page(asynchronous, capsys):
    cursors = []
    def handler(request):
        if request.url.path.endswith('/logs/live'):
            cursor = request.url.params.get('after', '')
            cursors.append(cursor)
            if cursor == '':
                chunks = [{'id': n, 'text': f'line {n}\n'} for n in range(1, 17)]
                following = 'MTY'
            elif cursor == 'MTY':
                chunks = [{'id': 17, 'text': 'final output\n'}]
                following = 'MTc'
            else:
                chunks, following = [], 'MTc'
            return httpx.Response(200, json={'chunks': chunks, 'next_cursor': following})
        if request.url.path.endswith('/events'):
            return httpx.Response(200, json={'events': []})
        return httpx.Response(200, json={'id': 'wl_test', 'status': 'completed'})
    if asynchronous:
        async def run():
            async with nodus.AsyncClient(api_key='test_key', base_url='https://example.test') as client:
                await client._http.aclose()
                client._http = httpx.AsyncClient(base_url='https://example.test', transport=httpx.MockTransport(handler))
                await client.wait('wl_test', progress=True)
        asyncio.run(run())
    else:
        with nodus.Client(api_key='test_key', base_url='https://example.test') as client:
            client._http.close()
            client._http = httpx.Client(base_url='https://example.test', transport=httpx.MockTransport(handler))
            client.wait('wl_test', progress=True)
    assert cursors == ['', 'MTY', 'MTc']
    assert capsys.readouterr().err.count('final output') == 1


@pytest.mark.parametrize('asynchronous', [False, True])
def test_terminal_empty_live_logs_fall_back_to_saved_output(asynchronous, capsys):
    saved = []
    def handler(request):
        if request.url.path.endswith('/logs/live'):
            return httpx.Response(200, json={'chunks': [], 'next_cursor': ''})
        if request.url.path.endswith('/logs'):
            saved.append(True)
            return httpx.Response(200, text='older runner output\n', headers={'X-Nodus-Stage-Id': 'main', 'X-Nodus-Generation': '1'})
        if request.url.path.endswith('/events'):
            return httpx.Response(200, json={'events': []})
        return httpx.Response(200, json={'id': 'wl_test', 'status': 'completed'})
    if asynchronous:
        async def run():
            async with nodus.AsyncClient(api_key='test_key', base_url='https://example.test') as client:
                await client._http.aclose()
                client._http = httpx.AsyncClient(base_url='https://example.test', transport=httpx.MockTransport(handler))
                await client.wait('wl_test', progress=True)
        asyncio.run(run())
    else:
        with nodus.Client(api_key='test_key', base_url='https://example.test') as client:
            client._http.close()
            client._http = httpx.Client(base_url='https://example.test', transport=httpx.MockTransport(handler))
            client.wait('wl_test', progress=True)
    assert saved == [True]
    assert capsys.readouterr().err.count('older runner output') == 1


@pytest.mark.parametrize('asynchronous', [False, True])
def test_saved_log_observation_stops_reading_at_byte_limit(asynchronous, monkeypatch):
    monkeypatch.setattr(nodus, '_LOG_MAX_BYTES', 1024)
    read = []
    class Chunks(httpx.SyncByteStream, httpx.AsyncByteStream):
        def __iter__(self):
            for index in range(4096):
                read.append(index)
                yield b'x' * 1024
        async def __aiter__(self):
            for chunk in self:
                yield chunk
    def handler(request):
        if request.url.path.endswith('/logs/live'):
            return httpx.Response(200, json={'chunks': [], 'next_cursor': ''})
        if request.url.path.endswith('/logs'):
            return httpx.Response(200, stream=Chunks())
        if request.url.path.endswith('/events'):
            return httpx.Response(200, json={'events': []})
        return httpx.Response(200, json={'id': 'wl_test', 'status': 'completed'})
    if asynchronous:
        async def run():
            async with nodus.AsyncClient(api_key='test_key', base_url='https://example.test') as client:
                await client._http.aclose()
                client._http = httpx.AsyncClient(base_url='https://example.test', transport=httpx.MockTransport(handler))
                await client.wait('wl_test', progress=True)
        asyncio.run(run())
    else:
        with nodus.Client(api_key='test_key', base_url='https://example.test') as client:
            client._http.close()
            client._http = httpx.Client(base_url='https://example.test', transport=httpx.MockTransport(handler))
            client.wait('wl_test', progress=True)
    assert len(read) == 2
@pytest.mark.parametrize('data,expected', [
    ({'offer_id': 'nodus:compute-0-any', 'fit_class': 'compute'}, 'Not reported'),
    ({'offer_id': 'nodus:H100-80-any'}, 'Not reported'),
    ({'resources': {'accelerator': 'RTX 4090', 'device_memory_gb': 24}}, 'RTX 4090 (24 GB)'),
    ({'fit_class': 'H100', 'compute_class': 'accelerator', 'memory_gb': 80}, 'H100 (80 GB)'),
    ({'fit_class': 'H100', 'memory_gb': 80}, 'H100'),
    ({'resources': {'accelerator': 'RTX 4090', 'device_memory_gb': 24}, 'memory_gb': 80}, 'RTX 4090 (24 GB)'),
    ({'resources': {'accelerator': '\x1b[31mRTX\n4090\x1b[0m', 'device_memory_gb': 24}}, 'RTX 4090 (24 GB)'),
    ({'resources': {'accelerator': 'compute', 'device_memory_gb': 24}}, 'Not reported'),
    ({'resources': {'accelerator': 'RTX 4090', 'device_memory_gb': float('nan')}}, 'RTX 4090'),
])
def test_compute_label_reports_only_observed_metadata(data, expected):
    from nodus._terminal import compute_label
    from nodus.types import Route
    route = Route.from_dict(data)
    original_sku = route.sku
    assert compute_label(route) == expected
    assert route.sku == original_sku


def test_compute_label_absent_route():
    from nodus._terminal import compute_label
    assert compute_label(None) == 'Not reported'
