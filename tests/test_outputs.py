"""Output delivery follows internal/api/outputs.go's tenant-scoped endpoint."""
import asyncio
import hashlib

import httpx
import pytest

import nodus

DATA = b'model weights\x00\xff'
DIGEST = hashlib.sha256(DATA).hexdigest()
ROW = {'name': 'model', 'stage_id': 'train', 'sha256': DIGEST,
       'bytes': len(DATA), 'download': '/v1/workloads/wl_test/outputs/model?stage=train'}


def exercise(handler, asynchronous, action):
    cls = nodus.AsyncClient if asynchronous else nodus.Client
    client = cls(api_key='nk_test', base_url='https://nodus.invalid')
    http = httpx.AsyncClient if asynchronous else httpx.Client
    client._http = http(base_url='https://nodus.invalid', transport=httpx.MockTransport(handler),
                        headers={'Authorization': 'Bearer nk_test'})
    if asynchronous:
        async def run():
            async with client:
                return await action(client)
        return asyncio.run(run())
    with client:
        return action(client)


@pytest.mark.parametrize('asynchronous', [False, True])
def test_list_outputs(asynchronous):
    def handler(req):
        assert req.url.path == '/v1/workloads/wl_test/outputs'
        return httpx.Response(200, json={'workload_id': 'wl_test', 'outputs': [ROW]})
    rows = exercise(handler, asynchronous, lambda c: c.outputs('wl_test'))
    assert isinstance(rows[0], nodus.Output)
    assert rows[0].name == 'model'
    assert rows[0].stage_id == 'train'
    assert rows[0].sha256 == DIGEST
    assert rows[0].bytes == len(DATA)


@pytest.mark.parametrize('asynchronous', [False, True])
def test_download_output(asynchronous, tmp_path):
    def handler(req):
        assert req.url.path == '/v1/workloads/wl_test/outputs/model'
        assert req.url.params['stage'] == 'train'
        return httpx.Response(200, content=DATA, headers={'X-Nodus-SHA256': DIGEST})
    dest = tmp_path / 'model.bin'
    assert exercise(handler, asynchronous, lambda c: c.download_output('wl_test', 'model', dest, stage='train')) == dest
    assert dest.read_bytes() == DATA
    assert list(tmp_path.glob(".nodus-download-*")) == []


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('headers', [{'X-Nodus-SHA256': '0' * 64}, {},
                                     {'X-Nodus-SHA256': DIGEST, 'Content-Length': '999'}])
def test_failed_integrity_preserves_destination(asynchronous, headers, tmp_path):
    dest = tmp_path / 'model.bin'
    dest.write_bytes(b'existing')
    with pytest.raises(nodus.NodusError):
        exercise(lambda req: httpx.Response(200, content=DATA, headers=headers), asynchronous,
                 lambda c: c.download_output('wl_test', 'model', dest))
    assert dest.read_bytes() == b'existing'
    assert list(tmp_path.glob(".nodus-download-*")) == []


@pytest.mark.parametrize('asynchronous', [False, True])
def test_download_does_not_follow_redirect(asynchronous, tmp_path):
    calls = []
    def handler(req):
        calls.append(req.url.host)
        return httpx.Response(302, headers={'Location': 'https://attacker.invalid/steal'})
    with pytest.raises(nodus.NodusError):
        exercise(handler, asynchronous, lambda c: c.download_output('wl_test', 'model', tmp_path / 'model'))
    assert calls == ['nodus.invalid']


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('name', ['..', '../model', 'https://attacker.invalid/x', 'model?x=1'])
def test_invalid_output_name_rejected_before_request(asynchronous, name, tmp_path):
    def handler(req):
        pytest.fail('invalid output name reached transport')
    with pytest.raises((ValueError, nodus.NodusError)):
        exercise(handler, asynchronous, lambda c: c.download_output('wl_test', name, tmp_path / 'model'))

@pytest.mark.parametrize('asynchronous', [False, True])
def test_routing_empty(asynchronous):
    def handler(req):
        assert req.url.path == '/v1/workloads/wl_test/routing'
        return httpx.Response(200, json={'workload_id': 'wl_test', 'placements': []})
    assert exercise(handler, asynchronous, lambda c: c.routing('wl_test')) == []


def test_stage_metrics_preserve_missing_and_zero():
    stage = nodus.StageRun.from_dict({'id': 'main', 'last_loss': 0.0, 'metric_rate': 12.5, 'metric_step': 0})
    assert (stage.last_loss, stage.metric_rate, stage.metric_step) == (0.0, 12.5, 0)
    stage = nodus.StageRun.from_dict({'id': 'main'})
    assert (stage.last_loss, stage.metric_rate, stage.metric_step) == (None, None, None)

@pytest.mark.parametrize('asynchronous', [False, True])
def test_error_response_is_bounded(asynchronous, tmp_path):
    chunks_read = []
    class ErrorStream(httpx.SyncByteStream, httpx.AsyncByteStream):
        def __iter__(self):
            for index in range(256):
                chunks_read.append(index)
                yield b'x' * 16384
        async def __aiter__(self):
            for chunk in self:
                yield chunk
    def handler(req):
        return httpx.Response(500, stream=ErrorStream())
    dest = tmp_path / 'model'
    dest.write_bytes(b'previous')
    with pytest.raises(nodus.NodusError) as caught:
        exercise(handler, asynchronous, lambda c: c.download_output('wl_test', 'model', dest))
    assert len(chunks_read) <= 5
    assert len(str(caught.value.body)) < 70000
    assert dest.read_bytes() == b'previous'

@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('failure', [httpx.ReadTimeout('timeout'), httpx.ReadError('broken pipe')])
def test_interrupted_stream_preserves_destination(asynchronous, failure, tmp_path):
    class Interrupted(httpx.SyncByteStream, httpx.AsyncByteStream):
        def __iter__(self):
            yield DATA
            raise failure
        async def __aiter__(self):
            for chunk in self:
                yield chunk
    dest = tmp_path / 'model'
    dest.write_bytes(b'previous')
    with pytest.raises((nodus.APITimeoutError, nodus.APIConnectionError)):
        exercise(lambda req: httpx.Response(200, headers={'X-Nodus-SHA256': DIGEST}, stream=Interrupted()),
                 asynchronous, lambda c: c.download_output('wl_test', 'model', dest))
    assert dest.read_bytes() == b'previous'
    assert list(tmp_path.glob('.nodus-download-*')) == []


def test_async_cancellation_removes_temporary_file(tmp_path):
    class Cancelled(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield DATA
            raise asyncio.CancelledError()
    dest = tmp_path / 'model'
    dest.write_bytes(b'previous')
    with pytest.raises(asyncio.CancelledError):
        exercise(lambda req: httpx.Response(200, headers={'X-Nodus-SHA256': DIGEST}, stream=Cancelled()),
                 True, lambda c: c.download_output('wl_test', 'model', dest))
    assert dest.read_bytes() == b'previous'
    assert list(tmp_path.glob('.nodus-download-*')) == []


@pytest.mark.parametrize('asynchronous', [False, True])
def test_stage_query_cannot_inject_headers_or_query(asynchronous, tmp_path):
    stage = 'train&stage=other#fragment\r\nAuthorization: stolen'
    def handler(req):
        assert list(req.url.params.items()) == [('stage', stage)]
        assert req.url.host == 'nodus.invalid'
        assert req.headers['Authorization'] == 'Bearer nk_test'
        assert req.headers['Accept-Encoding'] == 'identity'
        return httpx.Response(200, content=DATA, headers={'X-Nodus-SHA256': DIGEST,
                              'Content-Disposition': 'attachment; filename="../../attacker"'})
    existing = set(tmp_path.iterdir())
    dest = tmp_path / 'model'
    exercise(handler, asynchronous, lambda c: c.download_output('wl_test', 'model', dest, stage=stage))
    assert dest.read_bytes() == DATA
    assert set(tmp_path.iterdir()) - existing == {dest}


@pytest.mark.parametrize('asynchronous', [False, True])
def test_download_all_uses_stage_names_and_verified_endpoint(asynchronous, tmp_path):
    def handler(req):
        if req.url.path.endswith('/outputs'):
            return httpx.Response(200, json={'outputs': [dict(ROW, download='https://attacker.invalid/steal')]})
        assert req.url.host == 'nodus.invalid'
        assert req.url.params['stage'] == 'train'
        return httpx.Response(200, content=DATA, headers={'X-Nodus-SHA256': DIGEST})
    def action(c):
        w = nodus.AsyncWorkload(c) if asynchronous else nodus.Workload(c)
        w.id = 'wl_test'
        return w.download(tmp_path)
    result = exercise(handler, asynchronous, action)
    assert result == [tmp_path / 'train' / 'model']
    assert result[0].read_bytes() == DATA


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('row', [dict(ROW, name='../escape'), dict(ROW, stage_id='../escape'),
                                 dict(ROW, name='CON'), dict(ROW, name='model.'), dict(ROW, name='x:y')])
def test_download_all_rejects_unsafe_server_paths(asynchronous, row, tmp_path):
    def handler(req):
        assert req.url.path.endswith('/outputs')
        return httpx.Response(200, json={'outputs': [row]})
    def action(c):
        w = nodus.AsyncWorkload(c) if asynchronous else nodus.Workload(c)
        w.id = 'wl_test'
        return w.download(tmp_path)
    existing = set(tmp_path.iterdir())
    with pytest.raises(nodus.NodusError):
        exercise(handler, asynchronous, action)
    assert set(tmp_path.iterdir()) == existing

@pytest.mark.parametrize('asynchronous', [False, True])
def test_sink_state_and_reload_wire(asynchronous):
    def handler(req):
        if req.method == 'POST':
            assert req.url.path == '/v1/workloads/wl_test/outputs/model/reload'
            assert req.url.params['stage'] == 'train'
            return httpx.Response(202, json={'state': 'pending', 'rows': 0})
        return httpx.Response(200, json={'outputs': [{**ROW, 'sink_state': 'failed', 'sink_rows': 0, 'sink_error': 'Check target schema.'}]})
    rows = exercise(handler, asynchronous, lambda c: c.outputs('wl_test'))
    assert rows[0].sink_state == 'failed'
    assert rows[0].sink_rows == 0
    assert rows[0].sink_error == 'Check target schema.'
    assert exercise(handler, asynchronous, lambda c: c.reload_output('wl_test', 'model', stage='train'))['state'] == 'pending'


def test_structured_sink_declarations_validate_and_preserve_wire():
    from nodus._brief import _validate_outputs
    declaration = {'results': {'path': 'results.csv', 'sink': {'connection': 'warehouse', 'table': 'eval_results'}}}
    _validate_outputs(declaration)
    for table in ['public.results', 'nodus_runs', 'x; DROP TABLE y']:
        with pytest.raises(ValueError):
            _validate_outputs({'results': {'path': 'results.csv', 'sink': {'connection': 'warehouse', 'table': table}}})
    with pytest.raises(ValueError):
        _validate_outputs({**declaration, 'other': declaration['results']})

@pytest.mark.parametrize('asynchronous', [False, True])
def test_structured_sink_run_wire(asynchronous):
    import json
    declaration = {'results': {'path': 'results.jsonl', 'sink': {'connection': 'warehouse', 'table': 'eval_results'}}}
    def handler(req):
        body = json.loads(req.content)
        assert body['stages'][0]['outputs'] == declaration
        return httpx.Response(202, json={'workload_id': 'wl_sink', 'status': 'accepted'})
    workload = exercise(handler, asynchronous, lambda c: c.run(command=['python', 'train.py'], outputs=declaration, budget=1))
    assert workload.id == 'wl_sink'


def test_cli_sink_outputs_and_reload(monkeypatch, capsys):
    from nodus import cli
    from test_sandbox_cli import client_factory
    def handler(req):
        if req.method == 'POST':
            assert req.url.params['stage'] == 'train'
            return httpx.Response(202, json={'state': 'pending'})
        return httpx.Response(200, json={'outputs': [{**ROW, 'sink_state': 'loaded', 'sink_rows': 8}]})
    monkeypatch.setattr(cli, 'Client', client_factory(handler))
    assert cli.main(['workload', 'outputs', 'wl_test', '--plain']) == 0
    output = capsys.readouterr().out
    assert 'loaded' in output and '8' in output
    assert cli.main(['workload', 'outputs', 'wl_test', '--reload', 'model', '--stage', 'train']) == 0
    assert 'pending' in capsys.readouterr().out


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('finish', ['refresh', 'wait'])
def test_sink_error_clears_only_on_authoritative_recovery(asynchronous, finish):
    responses = iter([
        {'id': 'wl_test', 'status': 'completed', 'sink_error': 'sink load failed'},
        {'state': 'pending'},
        {'id': 'wl_test', 'status': 'completed'},
    ])
    def handler(req):
        return httpx.Response(202 if req.method == 'POST' else 200, json=next(responses))
    async def async_action(client):
        workload = await client.get('wl_test')
        await workload.reload_output('result')
        workload._absorb({'status': 'completed'})
        assert workload.sink_error == 'sink load failed'
        await getattr(workload, finish)()
        assert workload.sink_error == ''
    def sync_action(client):
        workload = client.get('wl_test')
        workload.reload_output('result')
        workload._absorb({'status': 'completed'})
        assert workload.sink_error == 'sink load failed'
        getattr(workload, finish)()
        assert workload.sink_error == ''
    exercise(handler, asynchronous, async_action if asynchronous else sync_action)

@pytest.mark.parametrize('asynchronous', [False, True])
def test_distributed_download_all_keeps_member_shards_distinct(asynchronous, tmp_path):
    members = ['dg-test:rank:0', 'dg-test:rank:1']
    payloads = {member: f'independent weights {rank}'.encode() for rank, member in enumerate(members)}
    def handler(req):
        if req.url.path.endswith('/outputs'):
            return httpx.Response(200, json={'outputs': [dict(ROW, name='shards/model.bin',
                group_id='dg-test', member_id=member, download='https://attacker.invalid/steal') for member in members]})
        assert req.url.host == 'nodus.invalid'
        assert req.url.params['stage'] == 'train'
        assert req.url.path == '/v1/workloads/wl_test/outputs/shards/model.bin'
        payload = payloads[req.url.params['member_id']]
        return httpx.Response(200, content=payload, headers={'X-Nodus-SHA256': hashlib.sha256(payload).hexdigest()})
    def action(c):
        w = nodus.AsyncWorkload(c) if asynchronous else nodus.Workload(c)
        w.id = 'wl_test'
        return w.download(tmp_path)
    saved = exercise(handler, asynchronous, action)
    assert [p.read_bytes() for p in saved] == list(payloads.values())
    assert saved == [tmp_path / 'train' / f'member-dg-test%3Arank%3A{rank}' / 'shards' / 'model.bin' for rank in range(2)]

@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('member', ['../other', 'member&stage=other', '', 'x\r\nsecret'])
def test_distributed_download_rejects_invalid_member_before_request(asynchronous, member, tmp_path):
    def handler(req):
        pytest.fail('invalid member reached transport')
    with pytest.raises((ValueError, nodus.NodusError)):
        exercise(handler, asynchronous, lambda c: c.download_output('wl_test', 'model', tmp_path / 'model', member_id=member))

def test_distributed_download_rejects_symlinked_nested_output(tmp_path):
    from nodus._outputs import output_destinations
    root = tmp_path / 'root'
    outside = tmp_path / 'outside'
    outside.mkdir()
    parent = root / 'train' / 'member-dg%3A0'
    parent.mkdir(parents=True)
    (parent / 'shards').symlink_to(outside, target_is_directory=True)
    output = nodus.Output.from_dict(dict(ROW, member_id='dg:0', name='shards/model.bin'))
    with pytest.raises(nodus.NodusError, match='symbolic'):
        output_destinations(root, [output])


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('rows', [
    [dict(ROW, name='a'), dict(ROW, name='a/b')],
    [dict(ROW, name='member-x'), dict(ROW, name='model', member_id='x')],
    [dict(ROW, name='MEMBER-X'), dict(ROW, name='model', member_id='x')],
])
def test_output_file_directory_overlap_fails_before_download(asynchronous, reverse, rows, tmp_path):
    destination = tmp_path / 'download'
    def handler(req):
        if req.url.path.endswith('/outputs'):
            return httpx.Response(200, json={'outputs': list(reversed(rows)) if reverse else rows})
        pytest.fail('overlapping inventory started downloading output bytes')
    def action(client):
        workload = nodus.AsyncWorkload(client) if asynchronous else nodus.Workload(client)
        workload.id = 'wl_test'
        return workload.download(destination)
    with pytest.raises(nodus.ValidationError, match='collide'):
        exercise(handler, asynchronous, action)
    assert not destination.exists()


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('member', ['_member', '.member', 'x' * 201])
def test_invalid_member_in_inventory_fails_before_any_download(asynchronous, member, tmp_path):
    destination = tmp_path / 'download'
    def handler(req):
        if req.url.path.endswith('/outputs'):
            return httpx.Response(200, json={'outputs': [ROW, dict(ROW, member_id=member)]})
        pytest.fail('invalid inventory started downloading output bytes')
    def action(client):
        workload = nodus.AsyncWorkload(client) if asynchronous else nodus.Workload(client)
        workload.id = 'wl_test'
        return workload.download(destination)
    with pytest.raises(nodus.ValidationError, match='member'):
        exercise(handler, asynchronous, action)
    assert not destination.exists()


@pytest.mark.parametrize('asynchronous', [False, True])
def test_existing_file_in_output_parent_fails_before_any_download(asynchronous, tmp_path):
    destination = tmp_path / 'download'
    blocked = destination / 'train' / 'blocked'
    blocked.parent.mkdir(parents=True)
    blocked.write_bytes(b'customer file')
    def handler(req):
        if req.url.path.endswith('/outputs'):
            return httpx.Response(200, json={'outputs': [dict(ROW, name='good'), dict(ROW, name='blocked/weights')]})
        pytest.fail('blocked destination started downloading output bytes')
    def action(client):
        workload = nodus.AsyncWorkload(client) if asynchronous else nodus.Workload(client)
        workload.id = 'wl_test'
        return workload.download(destination)
    with pytest.raises(nodus.ValidationError, match='directory'):
        exercise(handler, asynchronous, action)
    assert blocked.read_bytes() == b'customer file'
    assert list(blocked.parent.iterdir()) == [blocked]
