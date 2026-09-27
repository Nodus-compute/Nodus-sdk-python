import pytest
import nodus

def test_step_requires_explicit_stable_identity_before_invocation():
    calls=[]
    @nodus.step(name='send',version='1')
    def send(value):
        calls.append(value)
        return value
    with pytest.raises(nodus.ValidationError):
        send(42)
    assert calls==[]

import base64
from datetime import datetime, timedelta, timezone
import http.server
import json
import os
import socketserver
import sqlite3
import threading
import uuid

@pytest.fixture
def journal_socket(tmp_path,monkeypatch):
    import tempfile
    from pathlib import Path
    import socket
    if not hasattr(socket, 'AF_UNIX') or not hasattr(socketserver, 'UnixStreamServer'):
        pytest.skip('Guest driver fixture requires Unix-domain socket support')
    socket_dir=tempfile.TemporaryDirectory(prefix='nds-agent-',dir='/tmp' if os.name == 'posix' else None)
    path=Path(socket_dir.name)/'j.sock'
    db=sqlite3.connect(tmp_path/'journal.sqlite',check_same_thread=False)
    db.execute('CREATE TABLE journal (step TEXT PRIMARY KEY, definition TEXT, status TEXT, result TEXT, token TEXT, external_key TEXT)')
    db.execute('CREATE TABLE model_calls (run TEXT, step TEXT, call TEXT, input TEXT, id TEXT, state TEXT, response TEXT, PRIMARY KEY(run,step,call))')
    effects=sqlite3.connect(tmp_path/'remote-effects.sqlite',check_same_thread=False)
    effects.execute('CREATE TABLE effects (id INTEGER PRIMARY KEY, external_key TEXT UNIQUE)')
    state={'effects':effects,'status':'active','result':None,'lost_completion':False,'unknown_on_resume':False,'complete_requests':0}
    encode=lambda value:base64.b64encode(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).decode()
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            action=self.path[1:]
            result={}
            state.setdefault('requests', []).append((action, request))
            if action in ('session','renew'):
                result={'run':{'run_id':state.get('run_id','cycle-42'),'name':'main','version':'1','image_digest':'sha256:'+'a'*64,'status':state['status'],'input':encode(state.get('input',{'invoice':42})),'result':state['result']},'session_token':'scoped-capability','epoch':1,'expires_at':(datetime.now(timezone.utc)+timedelta(seconds=state.get('session_seconds', 60))).isoformat()}
                if state.get('recovery_policy'):
                    result['recovery_policy'] = state['recovery_policy']
                    if state.get('checkpoint_id'):
                        result['checkpoint_id'] = state['checkpoint_id']
            elif action in ('checkpoint_begin', 'checkpoint_status'):
                if action == 'checkpoint_begin':
                    checkpoint_id = 'cp_' + request['operation'] + '_' + request['request_id']
                    state.setdefault('checkpoint_requests', {})[checkpoint_id] = request
                    result = {'checkpoint_id': checkpoint_id, 'status': 'pending'}
                else:
                    checkpoint_id = request['checkpoint_id']
                    result = {'checkpoint_id': checkpoint_id, 'status': state.get('checkpoint_status', 'ready')}
                    if state.get('pending_polls', 0) > 0:
                        state['pending_polls'] -= 1
                        result['status'] = 'pending'
                    if result['status'] == 'ready' and state['checkpoint_requests'][checkpoint_id]['operation'] == 'baseline':
                        result['status'] = 'committed'
                    if result['status'] == 'failed':
                        result['failure_code'] = 'agent_checkpoint_state_empty'
                if 'status_wait_max_ms' in state:
                    result['status_wait_max_ms'] = state['status_wait_max_ms']
            elif action in ('model_begin', 'model_status'):
                owner = db.execute('SELECT token FROM journal WHERE step=?', (request['step_id'],)).fetchone()
                assert owner and owner[0] == request['claim_token']
                identity = (request['run_id'], request['step_id'], request['call_id'])
                row = db.execute('SELECT input,id,state,response FROM model_calls WHERE run=? AND step=? AND call=?', identity).fetchone()
                if action == 'model_begin':
                    definition = json.dumps(request['input'], sort_keys=True, separators=(',', ':'))
                    if row is None:
                        call_id = 'mdl_' + uuid.uuid4().hex
                        response = state.get('model_response', {'model': request['input']['model'], 'content': [{'type': 'text', 'text': 'Saved answer'}], 'stop_reason': 'end_turn', 'usage': {'input_tokens': 12, 'output_tokens': 3}})
                        db.execute('INSERT INTO model_calls VALUES (?,?,?,?,?,?,?)', (*identity, definition, call_id, 'running', json.dumps(response)))
                        db.commit()
                        state.setdefault('model_effects', []).append(request['input'])
                        row = (definition, call_id, 'running', json.dumps(response))
                    elif row[0] != definition:
                        self.send_response(409);self.end_headers();self.wfile.write(b'{"code":"idempotency_conflict"}');return
                    if state.get('lost_model_begin'):
                        state['lost_model_begin'] = False
                        self.close_connection = True
                        return
                assert row is not None
                if action == 'model_status' and row[2] == 'running':
                    if state.get('model_pending_polls', 0):
                        state['model_pending_polls'] -= 1
                    else:
                        status = state.get('model_final_state', 'succeeded')
                        db.execute('UPDATE model_calls SET state=? WHERE run=? AND step=? AND call=?', (status, *identity))
                        db.commit()
                        row = (row[0], row[1], status, row[3])
                result = {'receipt': {'id': row[1], 'model': json.loads(row[0])['model'], 'state': row[2], 'charge_micros': 17 if row[2] == 'succeeded' else 0}}
                if row[2] == 'succeeded':
                    result['response'] = json.loads(row[3])
                if state.get('change_model_identity') and action == 'model_status':
                    result['receipt']['id'] = 'mdl_unrelated'
                if state.get('lost_model_status', 0) and action == 'model_status':
                    state['lost_model_status'] -= 1
                    self.send_response(503);self.end_headers();self.wfile.write(b'{"error":"unavailable"}');return
            elif action=='claim':
                step_id=request['step_id']
                definition=json.dumps({k:request[k] for k in ['name','version','effect','encoding','input']},sort_keys=True)
                row=db.execute('SELECT definition,status,result,token,external_key FROM journal WHERE step=?',(step_id,)).fetchone()
                if row is None:
                    token,external=uuid.uuid4().hex,uuid.uuid4().hex
                    db.execute('INSERT INTO journal VALUES (?,?,?,?,?,?)',(step_id,definition,'started',None,token,external));db.commit()
                    result={'decision':'execute','step_id':step_id,'claim_token':token,'external_key':external,'revision':1}
                elif row[0]!=definition:
                    self.send_response(409);self.end_headers();self.wfile.write(b'{"code":"step_definition_conflict"}');return
                elif row[1]=='unknown' and request['effect'] in ('pure','idempotent'):
                    token=uuid.uuid4().hex
                    db.execute('UPDATE journal SET status=?,token=? WHERE step=?',('started',token,step_id));db.commit()
                    result={'decision':'execute','step_id':step_id,'claim_token':token,'external_key':row[4],'revision':3}
                else:
                    result={'decision':'replay' if row[1]=='completed' else 'unknown','step_id':step_id,'result':row[2],'external_key':row[4],'revision':2}
            elif action=='complete':
                state['complete_requests']+=1
                row=db.execute('SELECT token,status,result FROM journal WHERE step=?',(request['step_id'],)).fetchone()
                assert row[0]==request['claim_token']
                if row[1]=='completed':
                    assert row[2]==request['result']
                db.execute('UPDATE journal SET status=?,result=? WHERE step=?',('completed',request['result'],request['step_id']));db.commit()
                if state['lost_completion']:
                    state['lost_completion']=False
                    self.close_connection=True
                    return
                result={'decision':'replay','step_id':request['step_id'],'result':request['result'],'external_key':'stable','revision':2}
                if state.get('malformed_completion'):
                    result['result'] = encode(None)
            elif action=='unknown':
                db.execute('UPDATE journal SET status=? WHERE step=?',('unknown',request['step_id']));db.commit()
                if state.get('reject_unknown'):
                    self.send_response(500);self.end_headers();self.wfile.write(b'{"error":"unavailable"}');return
                result={'decision':'unknown','step_id':request['step_id'],'revision':2}
            elif action=='finish':
                state['status']='completed';state['result']=request['result']
                result={'status':'completed','result':request['result']}
            elif action in ('wait', 'continue'):
                state.setdefault('control_requests', []).append((action, request))
                result=state['control_response']
            elif action.startswith('blob_'):
                blobs = state.setdefault('blobs', {})
                blob_id = request['blob_id']
                state.setdefault('blob_requests', []).append((action, request))
                if action == 'blob_begin':
                    blobs.setdefault(blob_id, {'reference': {'id': blob_id, 'sha256': request['sha256'], 'bytes': request['bytes']}, 'data': bytearray(), 'status': 'uploading'})
                blob = blobs[blob_id]
                if action == 'blob_put':
                    assert request['offset'] == len(blob['data'])
                    blob['data'].extend(base64.b64decode(request['data']))
                if action == 'blob_commit':
                    blob['status'] = 'committed'
                result = {'reference': blob['reference'], 'status': blob['status']}
                if action == 'blob_get':
                    offset = request.get('offset', 0)
                    chunk = bytes(blob['data'][offset:offset + 262144])
                    if state.get('corrupt_blob'):
                        chunk = b'x' * len(chunk)
                    result.update(offset=offset, data=base64.b64encode(chunk).decode(), eof=offset + len(chunk) == len(blob['data']))
            else:
                raise AssertionError(action)
            body=json.dumps(result).encode();self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    class Server(socketserver.UnixStreamServer):
        allow_reuse_address=True
    server=Server(str(path),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv('NODUS_AGENT_SOCKET',str(path))
    yield db,state
    server.shutdown();server.server_close();thread.join(2);db.close();effects.close();socket_dir.cleanup()


def test_managed_wait_yields_without_recording_a_false_completion(journal_socket):
    from nodus.agent_runtime import run
    _, state = journal_socket
    state['control_response'] = {'decision': 'waiting'}
    def main(event):
        nodus.agent.wait_for_event('approval', wait_id='approval:42')
        pytest.fail('waiting execution continued')
    assert run(main, run_id='cycle-42', version='1') is None
    assert state['status'] == 'active'
    action, body = state['control_requests'][0]
    assert action == 'wait' and body['wait_id'] == 'approval:42' and body['signal'] == 'approval'
    assert body['run_id'] == 'cycle-42' and body['session_token'] == 'scoped-capability'


def test_managed_signal_replay_finishes_with_verified_event(journal_socket):
    from nodus.agent_runtime import run
    _, state = journal_socket
    state['control_response'] = {'decision': 'replay', 'result': base64.b64encode(b'{"approved":true}').decode()}
    def main(event):
        return nodus.agent.wait_for_event('approval', wait_id='approval:42')
    assert run(main, run_id='cycle-42', version='1') == {'approved': True}
    assert state['status'] == 'completed'


def test_continuation_yields_and_keeps_the_durable_segment_input(journal_socket):
    from nodus.agent_runtime import run
    _, state = journal_socket
    state['control_response'] = {'decision': 'continued'}
    def main(event):
        nodus.agent.continue_as_new({'page': 2}, continuation_id='page:2')
        pytest.fail('continued execution reached completion')
    run(main, run_id='cycle-42', version='1')
    action, body = state['control_requests'][0]
    assert action == 'continue' and json.loads(base64.b64decode(body['input'])) == {'page': 2}
    assert state['status'] == 'active'


def test_managed_blob_stores_large_step_result_by_verified_reference(journal_socket):
    from nodus.agent_runtime import run, put_blob, get_blob
    _, state = journal_socket
    payload = bytes(range(256)) * 2048
    @nodus.step(name='large-output', version='1', effect='pure')
    def store_output():
        return put_blob(payload)
    def main(event):
        reference = store_output(_step_id='large-output')
        assert get_blob(reference) == payload
        return reference
    reference = run(main, run_id='cycle-42', version='1')
    import hashlib
    assert reference == {'id': 'bl_' + hashlib.sha256(payload).hexdigest(), 'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)}
    puts = [body for action, body in state['blob_requests'] if action == 'blob_put']
    assert [body['offset'] for body in puts] == [0, 262144]
    assert all(body['session_token'] == 'scoped-capability' for _, body in state['blob_requests'])


def test_managed_blob_rejects_corrupted_download_before_returning_bytes(journal_socket):
    from nodus.agent_runtime import run, put_blob, get_blob
    _, state = journal_socket
    def main(event):
        reference = put_blob(b'original')
        state['corrupt_blob'] = True
        return get_blob(reference)
    with pytest.raises(nodus.StepOutcomeUnknown, match='hash'):
        run(main, run_id='cycle-42', version='1')
    assert state['status'] == 'active'


def test_managed_launcher_uses_the_assigned_export_name(journal_socket, monkeypatch):
    import sys
    import types
    from nodus.agent_runtime import main
    def implementation(event):
        return event
    module = types.ModuleType('customer_agent_fixture')
    module.main = implementation
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert main(['--entrypoint', 'customer_agent_fixture:main', '--run-id', 'cycle-42', '--version', '1']) == {'invoice': 42}


def test_private_rpc_serializes_renewal_with_blob_transfer(monkeypatch, tmp_path):
    import httpx
    from nodus._agent import _RPC
    monkeypatch.setenv('NODUS_AGENT_SOCKET', str(tmp_path / 'synthetic-agent.sock'))
    rpc = _RPC()
    rpc.client.close()
    first_entered, release_first, second_started = threading.Event(), threading.Event(), threading.Event()
    overlaps, failures = [], []
    def handler(request):
        if request.url.path == '/blob_get':
            first_entered.set()
            assert release_first.wait(2)
        elif not release_first.is_set():
            overlaps.append(request.url.path)
            return httpx.Response(409, json={'error': 'agent_bridge_unavailable'})
        return httpx.Response(200, json={'acknowledged': True})
    rpc.client = httpx.Client(base_url='http://agent.local', transport=httpx.MockTransport(handler))
    def call(action):
        if action == 'renew':
            second_started.set()
        try:
            rpc.call(action, {})
        except Exception as error:
            failures.append(error)
    first = threading.Thread(target=call, args=('blob_get',))
    second = threading.Thread(target=call, args=('renew',))
    try:
        first.start()
        assert first_entered.wait(1)
        second.start()
        assert second_started.wait(1)
        second.join(.05)
        release_first.set()
        first.join(2)
        second.join(2)
        assert not overlaps and not failures
    finally:
        release_first.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
        rpc.close()


def test_completed_replay_and_lost_completion_do_not_repeat_actual_effect(journal_socket):
    db,state=journal_socket
    calls=[]
    @nodus.step(name='invoice.send',version='1',effect='external')
    def send(invoice):
        key=nodus.step_context().idempotency_key
        state['effects'].execute('INSERT INTO effects(external_key) VALUES (?)',(key,));state['effects'].commit()
        calls.append(invoice)
        return {'receipt':'remote-7'}
    def main(event):
        return send(event['invoice'],_step_id='invoice:42:send')
    state['lost_completion']=True
    first=nodus.agent.resume(main,run_id='cycle-42',version='1')
    # Simulate an older application snapshot with a newer committed step journal.
    state['status']='active';state['result']=None
    second=nodus.agent.resume(main,run_id='cycle-42',version='1')
    assert first==second=={'receipt':'remote-7'}
    assert calls==[42]
    assert state['effects'].execute('SELECT count(*) FROM effects').fetchone()[0]==1
    assert state['complete_requests']==2


def test_unknown_remote_commit_blocks_without_reexecution(journal_socket):
    db,state=journal_socket
    @nodus.step(name='invoice.send',version='1')
    def send(invoice):
        state['effects'].execute('INSERT INTO effects(external_key) VALUES (?)',(nodus.step_context().idempotency_key,));state['effects'].commit()
        raise ConnectionError('response lost after remote commit')
    def main(event):
        return send(event['invoice'],_step_id='invoice:42:send')
    for _ in range(2):
        with pytest.raises(nodus.StepOutcomeUnknown):
            nodus.agent.resume(main,run_id='cycle-42',version='1')
    assert state['effects'].execute('SELECT count(*) FROM effects').fetchone()[0]==1


def test_oversized_result_never_justifies_retry(journal_socket):
    db,state=journal_socket
    calls=[]
    @nodus.step(name='big',version='1',effect='idempotent',dedupe_seconds=3600)
    def big():
        calls.append(1)
        return 'x'*(256<<10)
    def main(event):
        return big(_step_id='big-result')
    with pytest.raises(nodus.StepOutcomeUnknown):
        nodus.agent.resume(main,run_id='cycle-42',version='1')
    assert calls==[1]

@pytest.mark.parametrize('value',[float('nan'),float('inf'),{1:'invalid'},object(),'\ud800'])
def test_payload_rejects_non_json_before_transport(value):
    from nodus._agent import encode
    with pytest.raises(nodus.ValidationError):
        encode(value)


def test_changed_step_definition_refuses_function(journal_socket):
    calls=[]
    @nodus.step(name='stable',version='1')
    def first(value):
        calls.append(value)
        return value
    def main(event):
        return first(1,_step_id='business-id')
    nodus.agent.resume(main,run_id='cycle-42',version='1')
    journal_socket[1]['status']='active'
    @nodus.step(name='stable',version='2')
    def changed(value):
        calls.append(999)
        return value
    def changed_main(event):
        return changed(1,_step_id='business-id')
    with pytest.raises(nodus.StepDefinitionConflict):
        nodus.agent.resume(changed_main,name='main',run_id='cycle-42',version='1')
    assert calls==[1]


def test_run_registration_and_resolution_use_customer_identity():
    import httpx
    requests=[]
    encoded=base64.b64encode(b'{"invoice":42}').decode()
    run={'run_id':'cycle:42','name':'main','version':'1','image_digest':'sha256:'+'a'*64,'status':'active','epoch':0,'input':encoded}
    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/steps'):return httpx.Response(200,json={'steps':[]})
        if request.url.path.endswith('/resolve'):return httpx.Response(200,json={'decision':'unknown','step_id':'send','revision':2,'external_key':'stable'})
        return httpx.Response(201 if request.method=='POST' else 200,json=run)
    client=nodus.Client(api_key='nk_live_test',base_url='https://nodus.invalid')
    client._http=httpx.Client(base_url='https://nodus.invalid',transport=httpx.MockTransport(handler))
    try:
        runs=nodus.Sandbox(client,'sb_test').agent_runs
        created=runs.create(run_id='cycle:42',name='main',version='1',image_digest=run['image_digest'],input={'invoice':42})
        assert created.input=={'invoice':42}
        assert runs.get('cycle:42').run_id=='cycle:42'
        assert runs.steps('cycle:42')=={'steps':[]}
        runs.resolve('cycle:42',step_id='send',expected_revision=2,decision='no_effect',reason='Verified independent receipt absence',evidence_digest='sha256:'+'b'*64)
        assert json.loads(requests[0].content)['input']==encoded
        assert json.loads(requests[-1].content)['expected_revision']==2
        assert all('/agent-runs' in r.url.path for r in requests)
    finally: client.close()

@pytest.mark.asyncio
async def test_async_account_run_registration_and_read():
    import httpx
    run={'run_id':'run','name':'main','version':'1','image_digest':'sha256:'+'a'*64,'status':'active','epoch':0,'input':base64.b64encode(b'null').decode()}
    client=nodus.AsyncClient(api_key='nk_live_test',base_url='https://nodus.invalid')
    client._http=httpx.AsyncClient(base_url='https://nodus.invalid',transport=httpx.MockTransport(lambda r:httpx.Response(200,json=run)))
    try:
        runs=(await client.sandboxes.from_id('sb_test')).agent_runs
        assert (await runs.create(run_id='run',name='main',version='1',image_digest=run['image_digest'],input=None)).run_id=='run'
        assert (await runs.get('run')).input is None
    finally: await client.aclose()


def test_event_submission_preserves_command_and_source_identity():
    import httpx
    requests=[]
    event={'source':'queue','event_id':'42','run_id':'cycle-42','exec_id':'ex_once','state':'queued','reason':'waiting_for_sandbox'}
    def handler(request):
        requests.append(request)
        if request.method=='DELETE':
            return httpx.Response(200,json={'run_id':'cycle-42','name':'main','version':'1','image_digest':'sha256:'+'a'*64,'status':'expired','epoch':1})
        return httpx.Response(202 if request.method=='POST' else 200,json=event)
    client=nodus.Client(api_key='nk_live_test',base_url='https://nodus.invalid')
    client._http=httpx.Client(base_url='https://nodus.invalid',transport=httpx.MockTransport(handler))
    try:
        box=nodus.Sandbox(client,'sb_test')
        got=box.agent_events.submit(source='queue',event_id='42',run_id='cycle-42',name='main',version='1',image_digest='sha256:'+'a'*64,input={'invoice':42},command=['python','agent.py','cycle-42'])
        assert got==event
        assert box.agent_events.get('42',source='queue')==event
        assert box.agent_runs.delete('cycle-42').status=='expired'
        body=json.loads(requests[0].content)
        assert body['command']==['python','agent.py','cycle-42']
        assert body['run']['run_id']=='cycle-42'
        assert 'env' not in body and 'stdin' not in body
        assert requests[1].url.params['source']=='queue'
    finally:client.close()


def test_qualified_remote_deduplication_commits_one_effect_across_retry(journal_socket):
    db,state=journal_socket
    received=[]
    @nodus.step(name='charge',version='1',effect='idempotent',dedupe_seconds=3600)
    def charge():
        key=nodus.step_context().idempotency_key
        received.append(key)
        remote=state['effects']
        remote.execute('INSERT INTO effects(external_key) VALUES (?) ON CONFLICT(external_key) DO NOTHING',(key,))
        remote.commit()
        if len(received)==1:
            raise ConnectionError('remote response lost after commit')
        receipt=remote.execute('SELECT id FROM effects WHERE external_key=?',(key,)).fetchone()[0]
        return {'receipt':receipt}
    def main(event):
        return charge(_step_id='invoice:42:charge')
    result=nodus.agent.resume(main,run_id='cycle-42',version='1')
    assert result=={'receipt':1}
    assert len(received)==2 and received[0]==received[1]
    assert state['effects'].execute('SELECT count(*) FROM effects').fetchone()[0]==1


def test_checkpoint_enabled_driver_links_each_completed_boundary(journal_socket):
    from nodus.agent_runtime import run
    _, state = journal_socket
    state['recovery_policy'] = 'checkpoint-v1'
    calls = []
    @nodus.step(name='save', version='1', effect='pure')
    def save():
        calls.append('saved')
        return {'offset': 7}
    def main(event):
        return save(_step_id='page:7')
    assert run(main, run_id='cycle-42', version='1') == {'offset': 7}
    begins = [body for action, body in state['requests'] if action == 'checkpoint_begin']
    assert [body['operation'] for body in begins] == ['baseline', 'step', 'finish']
    assert begins[1]['step_id'] == 'page:7' and begins[1]['claim_token']
    for action in ('complete', 'finish'):
        body = next(body for recorded, body in state['requests'] if recorded == action)
        checkpoint = state['checkpoint_requests'][body['checkpoint_id']]
        assert checkpoint['operation'] == ('step' if action == 'complete' else action)
    assert calls == ['saved']


@pytest.mark.parametrize('action', ['wait', 'continue'])
def test_checkpoint_enabled_controls_capture_before_yield(journal_socket, action):
    from nodus.agent_runtime import run
    _, state = journal_socket
    state['recovery_policy'] = 'checkpoint-v1'
    state['checkpoint_id'] = 'cp_initial'
    state['control_response'] = {'decision': 'waiting' if action == 'wait' else 'continued'}
    def main(event):
        if action == 'wait':
            nodus.agent.wait_for_event('approval', wait_id='approval:42')
        else:
            nodus.agent.continue_as_new({'offset': 7}, continuation_id='page:7')
    assert run(main, run_id='cycle-42', version='1') is None
    body = state['control_requests'][0][1]
    checkpoint = state['checkpoint_requests'][body['checkpoint_id']]
    assert checkpoint['operation'] == action
    assert checkpoint['operation_id'] == ('approval:42' if action == 'wait' else 'page:7')


def test_checkpoint_enabled_failed_step_exits_before_dirty_retry(journal_socket):
    from nodus.agent_runtime import run
    _, state = journal_socket
    state['recovery_policy'] = 'checkpoint-v1'
    state['checkpoint_id'] = 'cp_initial'
    attempts = []
    @nodus.step(name='save', version='1', effect='pure')
    def save():
        attempts.append('dirty state')
        raise RuntimeError('capture must restore before retry')
    def main(event):
        return save(_step_id='page:7')
    with pytest.raises(nodus.StepOutcomeUnknown):
        run(main, run_id='cycle-42', version='1')
    assert attempts == ['dirty state']
    assert state['complete_requests'] == 0


def test_checkpoint_failure_never_commits_step_result(journal_socket):
    from nodus.agent_runtime import run
    _, state = journal_socket
    state['recovery_policy'] = 'checkpoint-v1'
    state['checkpoint_id'] = 'cp_initial'
    state['checkpoint_status'] = 'failed'
    @nodus.step(name='save', version='1', effect='pure')
    def save():
        return {'offset': 7}
    def main(event):
        return save(_step_id='page:7')
    with pytest.raises(nodus.StepOutcomeUnknown, match='agent_checkpoint_state_empty'):
        run(main, run_id='cycle-42', version='1')
    assert state['complete_requests'] == 0
    assert state['status'] == 'active'


def test_checkpoint_polling_allows_session_renewal(journal_socket):
    from nodus.agent_runtime import run
    _, state = journal_socket
    state.update(recovery_policy='checkpoint-v1', checkpoint_id='cp_initial', session_seconds=.9, pending_polls=5, status_wait_max_ms=4000)
    def main(event):
        return {'offset': 7}
    assert run(main, run_id='cycle-42', version='1') == {'offset': 7}
    assert any(action == 'renew' for action, _ in state['requests'])
    polls = [body for action, body in state['requests'] if action == 'checkpoint_status']
    assert len(polls) == 6
    assert len({body['checkpoint_id'] for body in polls}) == 1
    assert all('wait_ms' not in body and 'known_status' not in body for body in polls)


@pytest.mark.parametrize('capability', [None, 4000, 2000, True, '4000', 4001, -1])
def test_checkpoint_wait_negotiates_without_changing_legacy_requests(journal_socket, capability):
    from nodus.agent_runtime import run
    _, state = journal_socket
    state.update(recovery_policy='checkpoint-v1', checkpoint_id='cp_initial', pending_polls=1)
    if capability is not None:
        state['status_wait_max_ms'] = capability
    def main(event):
        return {'offset': 7}
    assert run(main, run_id='cycle-42', version='1') == {'offset': 7}
    polls = [body for action, body in state['requests'] if action == 'checkpoint_status']
    assert len(polls) == 2
    for body in polls:
        if type(capability) is int and 0 < capability <= 4000:
            assert body['wait_ms'] == capability
            assert body['known_status'] == 'pending'
        else:
            assert set(body) == {'run_id', 'session_token', 'epoch', 'checkpoint_id'}


def test_caught_checkpoint_failure_cannot_commit_dirty_later_state(journal_socket):
    from nodus.agent_runtime import run
    _, state = journal_socket
    state.update(recovery_policy='checkpoint-v1', checkpoint_id='cp_initial', checkpoint_status='failed')
    @nodus.step(name='save', version='1', effect='pure')
    def save():
        return {'offset': 7}
    def main(event):
        try:
            save(_step_id='page:7')
        except nodus.StepOutcomeUnknown:
            return {'ignored': 'failed save'}
    with pytest.raises(nodus.StepOutcomeUnknown):
        run(main, run_id='cycle-42', version='1')
    assert state['status'] == 'active'
    assert not any(action == 'finish' for action, _ in state['requests'])


@pytest.mark.parametrize('failure', ['invalid_result', 'unknown_response', 'complete_response'])
def test_checkpoint_step_failure_poisons_session_before_caught_retry(journal_socket, failure):
    from nodus.agent_runtime import run
    _, state = journal_socket
    state.update(recovery_policy='checkpoint-v1', checkpoint_id='cp_initial')
    state.update(reject_unknown=failure == 'unknown_response', malformed_completion=failure == 'complete_response')
    calls = []
    @nodus.step(name='save', version='1', effect='pure')
    def save():
        calls.append('dirty state')
        if failure == 'unknown_response':
            raise RuntimeError('step failed')
        return object() if failure == 'invalid_result' and len(calls) == 1 else {'offset': 7}
    def main(event):
        try:
            save(_step_id='page:7')
        except nodus.StepOutcomeUnknown:
            return save(_step_id='page:7')
    with pytest.raises(nodus.StepOutcomeUnknown):
        run(main, run_id='cycle-42', version='1')
    assert calls == ['dirty state']
    assert state['complete_requests'] == (1 if failure == 'complete_response' else 0)
