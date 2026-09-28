"""Managed defaults preserve customer authorization and exact project bytes."""

import base64
import asyncio
import hashlib
import io
import json
import os
import tarfile
import subprocess
import sys

import httpx
import pytest

import nodus


POSIX_FILES = hasattr(os, "fwalk") and hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd
requires_posix_files = pytest.mark.skipif(not POSIX_FILES, reason="Secure local file operations require POSIX descriptor APIs")
requires_local_files = pytest.mark.skipif(not POSIX_FILES and os.name != "nt", reason="Secure local files require POSIX or native Windows NTFS support")


def client_for(handler):
    client = nodus.Client(api_key="test", base_url="https://nodus.invalid", max_retries=0)
    client._http.close()
    client._http = httpx.Client(base_url="https://nodus.invalid", transport=httpx.MockTransport(handler))
    return client


def test_default_template_never_invents_budget_or_changes_name_only_reconnect():
    bodies = []
    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(202, json={"id": "sb_one", "state": "creating"})
    with client_for(handle) as client:
        client.sandboxes.create(budget=5)
        client.sandboxes.create(name="existing")
        client.sandboxes.create(image="customer:image")
    assert bodies[0]["template"] == "nodus:agent-tools-v1"
    assert bodies[0]["outcome"]["max_cost_usd"] == 5
    assert "template" not in bodies[1] and "outcome" not in bodies[1]
    assert bodies[2]["image"] == "customer:image" and "outcome" not in bodies[2]


@pytest.mark.parametrize("budget", [-1, float("nan"), float("inf"), True])
def test_managed_creation_refuses_malformed_legacy_budget_before_http(budget):
    with client_for(lambda request: pytest.fail("invalid authorization reached HTTP")) as client:
        with pytest.raises(nodus.ValidationError):
            client.sandboxes.create(budget=budget)


@requires_local_files
def test_project_upload_is_bounded_and_excludes_dependencies_and_credentials(tmp_path):
    (tmp_path / "main.py").write_bytes(b"print('ready')\n")
    (tmp_path / ".env").write_text("SECRET=hidden")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/private.js").write_text("cache")
    calls = []
    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/v1/sandboxes/capabilities":
            return httpx.Response(200, json={"available": True, "max_project_bytes": 100000})
        if request.url.path == "/v1/assets":
            return httpx.Response(200, json={"upload_idempotency": True, "max_import_bytes": 100000, "assets": []})
        if request.url.path == "/v1/assets/upload":
            with tarfile.open(fileobj=io.BytesIO(request.content), mode="r:gz") as archive:
                assert archive.getnames() == ["main.py"]
                assert archive.extractfile("main.py").read() == b"print('ready')\n"
            return httpx.Response(201, json={"id": "asset_project", "state": "ready"})
        body = json.loads(request.content)
        assert body["source"] == {"asset_id": "asset_project"}
        assert body["template"] == "nodus:agent-tools-v1"
        return httpx.Response(202, json={"id": "sb_one", "state": "creating"})
    with client_for(handle) as client:
        assert client.sandboxes.create(project=tmp_path, budget=5).id == "sb_one"
    assert calls[-1] == "/v1/sandboxes"


@requires_posix_files
def test_project_symlink_cannot_upload_file_outside_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "secret").write_text("private")
    (project / "leak").symlink_to(tmp_path / "secret")
    def handle(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"available": True, "max_project_bytes": 100000})
    with client_for(handle) as client, pytest.raises(nodus.ValidationError, match="symlink"):
        client.sandboxes.create(project=project, budget=5)


@pytest.mark.parametrize("operation", ["project", "upload", "download"])
@pytest.mark.skipif(os.name == "nt", reason="Windows uses native handles instead of POSIX descriptors")
def test_missing_descriptor_support_refuses_local_files_without_changing_them(tmp_path, monkeypatch, operation):
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    original = tmp_path / "data.txt"
    original.write_bytes(b"unchanged local bytes")
    initial_entries = set(tmp_path.iterdir())
    file_request, _ = file_handler(b"remote bytes")
    def handle(request):
        if operation == "project":
            assert request.url.path == "/v1/sandboxes/capabilities"
            return httpx.Response(200, json={"available": True, "max_project_bytes": 100000})
        assert operation == "download", "upload must refuse before sending local bytes"
        if request.method == "POST":
            assert json.loads(request.content)["operation"] == "stat"
        return file_request(request)
    with client_for(handle) as client, pytest.raises(nodus.ValidationError, match="POSIX filesystem"):
        if operation == "project":
            client.sandboxes.create(project=tmp_path, budget=5)
        elif operation == "upload":
            nodus.Sandbox(client, "sb_one").files.upload(original, "data.txt")
        else:
            nodus.Sandbox(client, "sb_one").files.download("data.txt", original)
    assert original.read_bytes() == b"unchanged local bytes"
    assert set(tmp_path.iterdir()) == initial_entries


@pytest.mark.parametrize("action", ["sleep", "wake"])
def test_lifecycle_preserves_sent_identity_on_invalid_receipt(action):
    def handle(request):
        assert request.url.path == "/v1/sandboxes/sb_one/" + action
        assert request.headers["Idempotency-Key"] == "operation-key"
        return httpx.Response(202, json={"id": "sb_other"})
    with client_for(handle) as client, pytest.raises(nodus.APIError) as caught:
        getattr(nodus.Sandbox(client, "sb_one"), action)(idempotency_key="operation-key")
    assert caught.value.body == {"idempotency_key": "operation-key"}


def test_managed_agent_create_submit_and_controls_use_durable_keys():
    calls = []
    def handle(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.url.path, body, request.headers.get("Idempotency-Key")))
        if request.url.path.endswith("/runs"):
            return httpx.Response(202, json={"id": "run_one", "agent_id": "ag_one", "status": "queued", "input": body["input"]})
        return httpx.Response(201, json={"id": "ag_one", "name": "worker", "status": "active", "current_revision": 1, "budget_usd": 20})
    with client_for(handle) as client:
        agent = client.agents.create(name="worker", budget=20, entrypoint="agent:main", max_workers=4, idempotency_key="deploy")
        run = agent.submit({"question": "hello"}, session="customer-1", idempotency_key="event-1")
        assert run.id == "run_one"
        agent.pause(idempotency_key="pause")
    assert calls == [
        ("/v1/agents", {"name": "worker", "budget_usd": 20, "entrypoint": "agent:main", "max_workers": 4}, "deploy"),
        ("/v1/agents/ag_one/runs", {"input": {"question": "hello"}, "session": "customer-1"}, "event-1"),
        ("/v1/agents/ag_one/pause", {}, "pause"),
    ]


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "a/../../secret", "a\\..\\secret"])
def test_file_paths_reject_escape_before_http(path):
    with client_for(lambda request: pytest.fail("unsafe path reached HTTP")) as client:
        with pytest.raises(nodus.ValidationError):
            nodus.Sandbox(client, "sb_one").files.read(path)


@requires_local_files
def test_project_retry_reuses_immutable_upload_with_same_client(tmp_path):
    (tmp_path / "main.py").write_text("print(1)")
    uploads, attempts = [], []
    def handle(request):
        if request.url.path == "/v1/sandboxes/capabilities":
            return httpx.Response(200, json={"available": True, "max_project_bytes": 100000})
        if request.url.path == "/v1/assets":
            return httpx.Response(200, json={"upload_idempotency": True, "max_import_bytes": 100000})
        if request.url.path == "/v1/assets/upload":
            uploads.append(request.content)
            return httpx.Response(201, json={"id": "asset_" + str(len(uploads)), "state": "ready"})
        attempts.append(json.loads(request.content))
        if len(attempts) == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(202, json={"id": "sb_one"})
    with client_for(handle) as client:
        with pytest.raises(nodus.APITimeoutError):
            client.sandboxes.create(project=tmp_path, budget=5, idempotency_key="same-create")
        assert client.sandboxes.create(project=tmp_path, budget=5, idempotency_key="same-create").id == "sb_one"
    assert len(uploads) == 1 and attempts[0] == attempts[1]


@requires_local_files
def test_project_retry_reaches_saved_receipt_after_admission_gate_closes(tmp_path):
    (tmp_path / "main.py").write_text("print(1)")
    attempts = []
    def handle(request):
        if request.url.path == "/v1/sandboxes/capabilities":
            return httpx.Response(200, json={"available": not attempts, "max_project_bytes": 100000})
        if request.url.path == "/v1/assets":
            return httpx.Response(200, json={"upload_idempotency": True, "max_import_bytes": 100000})
        if request.url.path == "/v1/assets/upload":
            assert not attempts
            return httpx.Response(201, json={"id": "asset_project", "state": "ready"})
        attempts.append(json.loads(request.content))
        assert request.headers["Idempotency-Key"] == "same-create"
        if len(attempts) == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(202, json={"id": "sb_one"})
    with client_for(handle) as client:
        with pytest.raises(nodus.APITimeoutError):
            client.sandboxes.create(project=tmp_path, budget=5, idempotency_key="same-create")
        assert client.sandboxes.create(project=tmp_path, budget=5, idempotency_key="same-create").id == "sb_one"
    assert attempts[0] == attempts[1]


@requires_posix_files
def test_project_fifo_replacement_is_rejected_without_blocking(tmp_path):
    (tmp_path / "main.py").write_text("print(1)")
    script = '''
import os, sys
from nodus._projects import archive_project
from nodus.errors import ValidationError
original = os.open
def raced(name, flags, *args, **kwargs):
    if name == 'main.py':
        os.unlink(name, dir_fd=kwargs['dir_fd'])
        os.mkfifo(os.path.join(sys.argv[1], name))
    return original(name, flags, *args, **kwargs)
os.open = raced
os.supports_dir_fd.add(raced)
try:
    with archive_project(sys.argv[1], 100000):
        raise AssertionError('FIFO accepted')
except ValidationError:
    pass
'''
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=2)
    assert result.returncode == 0, result.stderr


def test_async_managed_agent_and_sandbox_defaults():
    requests = []
    def handle(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(202, json={"id": "ag_one" if request.url.path == "/v1/agents" else "sb_one"})
    async def run():
        async with nodus.AsyncClient(api_key="test", base_url="https://nodus.invalid") as client:
            await client._http.aclose()
            client._http = httpx.AsyncClient(base_url="https://nodus.invalid", transport=httpx.MockTransport(handle))
            assert (await client.sandboxes.create(budget=5)).id == "sb_one"
            assert (await client.agents.create(name="worker", budget=10)).id == "ag_one"
    asyncio.run(run())
    assert requests[0][1]["template"] == "nodus:agent-tools-v1"
    assert requests[1][1]["budget_usd"] == 10


def file_handler(content, *, corrupt=False):
    output = None
    seen = []
    def handle(request):
        nonlocal output
        if request.method == "POST":
            body = json.loads(request.content)
            seen.append(body)
            path, operation = body["path"], body["operation"]
            if operation == "stat":
                output = {"operation": operation, "path": path, "type": "file", "size_bytes": len(content)}
            elif operation == "read":
                offset = body["offset"]
                expected = hashlib.sha256(content).hexdigest()
                if offset:
                    assert body["expected_sha256"] == expected
                chunk = content[offset:offset + body["limit"]]
                output = {"operation": operation, "path": path, "data": base64.b64encode(chunk).decode(),
                          "offset": offset, "next_offset": offset + len(chunk), "size_bytes": len(content),
                          "sha256": "0" * 64 if corrupt else expected, "eof": offset + len(chunk) == len(content)}
            return httpx.Response(202, json={"id": "exec_one", "sandbox_id": "sb_one", "state": "completed", "exit_code": 0})
        assert request.url.path.endswith("/stream")
        data = base64.b64encode(json.dumps(output).encode()).decode()
        return httpx.Response(200, json={"frames": [{"sequence": 1, "stream": "stdout", "data": data}],
                                        "next_sequence": 1, "last_sequence": 1, "done": True, "complete": True})
    return handle, seen


@requires_local_files
def test_download_discovers_type_checks_chunks_and_replaces_only_verified_file(tmp_path):
    data = bytes(range(256)) * 2048
    handler, seen = file_handler(data)
    target = tmp_path / "output.bin"
    with client_for(handler) as client:
        nodus.Sandbox(client, "sb_one").files.download("output.bin", target)
    assert target.read_bytes() == data
    assert seen[0]["operation"] == "stat"
    assert [body["offset"] for body in seen[1:]] == [0, 262144]


@requires_local_files
def test_corrupt_download_keeps_existing_local_output(tmp_path):
    handler, _ = file_handler(b"corrupt", corrupt=True)
    target = tmp_path / "output.bin"
    target.write_bytes(b"original")
    with client_for(handler) as client, pytest.raises(nodus.NodusError, match="hash"):
        nodus.Sandbox(client, "sb_one").files.download("output.bin", target)
    assert target.read_bytes() == b"original"
    assert not list(tmp_path.glob(".nodus-download-*"))


@requires_local_files
def test_cli_download_preserves_binary_bytes_through_public_sdk(tmp_path, monkeypatch, capsys):
    from nodus import cli
    data = bytes(range(256)) * 2048
    files, _ = file_handler(data)
    def handle(request):
        if request.method == "GET" and request.url.path == "/v1/sandboxes/sb_one":
            return httpx.Response(200, json={"id": "sb_one", "state": "ready"})
        return files(request)
    monkeypatch.setattr(cli, "Client", lambda **kwargs: client_for(handle))
    destination = tmp_path / "résultats 日本語.bin"
    assert cli.main(["sandbox", "files", "download", "sb_one", "output.bin", str(destination)]) == 0
    assert destination.read_bytes() == data
    assert str(destination) in capsys.readouterr().out


@requires_local_files
def test_folder_upload_preserves_nested_binary_files_and_chunk_hashes(tmp_path):
    expected = {"project/nested/data.bin": bytes(range(256)) * 300, "project/empty.txt": b""}
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/data.bin").write_bytes(expected["project/nested/data.bin"])
    (tmp_path / "empty.txt").write_bytes(b"")
    uploaded, response = {}, None
    def handle(request):
        nonlocal response
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["operation"] == "write"
            accumulated = uploaded.setdefault(body["path"], bytearray())
            assert body["offset"] == len(accumulated)
            if accumulated:
                assert body["expected_sha256"] == hashlib.sha256(accumulated).hexdigest()
            accumulated.extend(base64.b64decode(body["data"]))
            response = {"operation": "write", "path": body["path"], "size_bytes": len(accumulated), "sha256": hashlib.sha256(accumulated).hexdigest()}
            return httpx.Response(202, json={"id": "exec_one", "sandbox_id": "sb_one", "state": "completed", "exit_code": 0})
        return httpx.Response(200, json={"frames": [{"sequence": 1, "stream": "stdout", "data": base64.b64encode(json.dumps(response).encode()).decode()}],
                                        "next_sequence": 1, "last_sequence": 1, "done": True, "complete": True})
    with client_for(handle) as client:
        nodus.Sandbox(client, "sb_one").files.upload(tmp_path, "project", idempotency_key="folder-upload")
    assert uploaded == expected


@requires_posix_files
def test_download_parent_swap_cannot_redirect_writes_or_leave_partial_files(tmp_path):
    directory, elsewhere, renamed = tmp_path / "download", tmp_path / "elsewhere", tmp_path / "renamed"
    directory.mkdir()
    elsewhere.mkdir()
    handler, _ = file_handler(b"verified bytes")
    def swapping(request):
        if request.method == "POST" and json.loads(request.content)["operation"] == "read":
            directory.rename(renamed)
            directory.symlink_to(elsewhere, target_is_directory=True)
        return handler(request)
    with client_for(swapping) as client, pytest.raises(nodus.ValidationError):
        nodus.Sandbox(client, "sb_one").files.download("result.txt", directory / "result.txt")
    assert list(elsewhere.iterdir()) == []
    assert list(renamed.iterdir()) == []


def test_writes_use_bounded_chunks_and_replayable_original_request_key():
    calls, outputs = [], {}
    assembled = bytearray()
    def handle(request):
        if request.method == "POST":
            body = json.loads(request.content)
            offset = body["offset"]
            chunk = base64.b64decode(body["data"])
            assert len(chunk) <= 32768
            assert offset == len(assembled)
            if offset:
                assert body["expected_sha256"] == hashlib.sha256(assembled).hexdigest()
            assembled.extend(chunk)
            calls.append(request.headers["Idempotency-Key"])
            if len(calls) == 2:
                raise httpx.ReadTimeout("chunk acknowledgement lost", request=request)
            outputs["last"] = {"operation": "write", "path": "data.bin", "size_bytes": len(assembled), "sha256": hashlib.sha256(assembled).hexdigest()}
            return httpx.Response(202, json={"id": "exec_one", "sandbox_id": "sb_one", "state": "completed", "exit_code": 0})
        return httpx.Response(200, json={"frames": [{"sequence": 1, "stream": "stdout", "data": base64.b64encode(json.dumps(outputs["last"]).encode()).decode()}],
                                        "next_sequence": 1, "last_sequence": 1, "done": True, "complete": True})
    with client_for(handle) as client, pytest.raises(nodus.APITimeoutError) as caught:
        nodus.Sandbox(client, "sb_one").files.write("data.bin", b"a" * 70000, idempotency_key="original-write")
    assert caught.value.body["idempotency_key"] == "original-write"
    assert calls[0] != calls[1]


@pytest.mark.parametrize("operation", ["submit", "signal"])
def test_durable_event_requires_explicit_nonempty_retry_identity(operation):
    from nodus._managed_agents import ManagedAgent, ManagedRun
    with client_for(lambda request: pytest.fail("missing event key reached HTTP")) as client:
        handle = ManagedAgent(client, {"id": "ag_one"}) if operation == "submit" else ManagedRun(client, {"id": "run_one", "agent_id": "ag_one"})
        with pytest.raises(nodus.ValidationError):
            getattr(handle, operation)("event", idempotency_key=None)


def test_connect_resolves_dotted_name_without_creating_compute():
    calls = []
    def handle(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/v1/sandboxes"
        assert request.url.params["name"] == "customer.session"
        return httpx.Response(200, json={"sandboxes": [{"id": "sb_one", "state": "ready", "envelope": {"name": "customer.session"}}], "next_cursor": None})
    with client_for(handle) as client:
        assert client.sandboxes.connect("customer.session").id == "sb_one"
    assert len(calls) == 1


def test_managed_resolution_keeps_verified_evidence_and_expected_revision():
    from nodus._managed_agents import ManagedRun
    calls = []
    def handle(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"steps": [{"step_id": "charge:1", "status": "unknown", "revision": 3}], "next_after": ""})
        body = json.loads(request.content)
        assert body == {"step_id": "charge:1", "expected_revision": 3, "decision": "completed", "reason": "Verified external receipt",
                        "evidence_digest": "sha256:" + "a" * 64, "result": base64.b64encode(b'{"receipt":"remote-1"}').decode()}
        return httpx.Response(200, json={"decision": "replay", "step_id": "charge:1", "revision": 4, "external_key": "stable"})
    with client_for(handle) as client:
        run = ManagedRun(client, {"id": "run_one", "agent_id": "ag_one"})
        assert run.steps()["steps"][0]["status"] == "unknown"
        assert run.resolve(step_id="charge:1", expected_revision=3, decision="completed", reason="Verified external receipt",
                           evidence_digest="sha256:" + "a" * 64, result={"receipt": "remote-1"})["decision"] == "replay"
    assert calls[1].url.path.endswith("/runs/run_one/resolve")


def test_managed_cli_deploy_submit_and_pause_use_public_api(monkeypatch, capsys):
    from nodus import cli
    calls = []
    def handle(request):
        calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path.endswith("/runs"):
            return httpx.Response(202, json={"id": "run_one", "agent_id": "ag_one", "status": "queued"})
        return httpx.Response(200, json={"id": "ag_one", "name": "worker", "status": "active", "budget_usd": 20})
    monkeypatch.setattr(cli, "Client", lambda **kwargs: client_for(handle))
    assert cli.main(["agent", "deploy", "worker", "--source-asset-id", "asset_source", "--budget", "20", "--idempotency-key", "deploy-1"]) == 0
    assert cli.main(["agent", "submit", "ag_one", "--input", '{"task":"hello"}', "--idempotency-key", "event-1"]) == 0
    assert cli.main(["agent", "pause", "ag_one", "--idempotency-key", "pause-1"]) == 0
    assert calls[0][2]["source"] == {"asset_id": "asset_source"}
    assert calls[0][2]["budget_usd"] == 20
    assert any(body and body.get("input") == {"task": "hello"} for _, _, body in calls)
    assert "run_one" in capsys.readouterr().out


def test_missing_download_reports_absence_without_creating_local_output(tmp_path):
    def handler(request):
        if request.method == "POST":
            assert json.loads(request.content)["operation"] == "stat"
            return httpx.Response(202, json={"id": "exec_one", "sandbox_id": "sb_one", "state": "completed", "exit_code": 0})
        result = {"operation": "stat", "path": "missing/file.txt", "type": "missing", "size_bytes": 0}
        return httpx.Response(200, json={"frames": [{"sequence": 1, "stream": "stdout", "data": base64.b64encode(json.dumps(result).encode()).decode()}],
                                        "next_sequence": 1, "last_sequence": 1, "done": True, "complete": True})
    target = tmp_path / "result.txt"
    with client_for(handler) as client, pytest.raises(FileNotFoundError):
        nodus.Sandbox(client, "sb_one").files.download("missing/file.txt", target)
    assert not target.exists()


@requires_posix_files
def test_upload_ancestor_swap_cannot_read_outside_the_selected_directory(tmp_path, monkeypatch):
    from nodus._sandbox_files import SandboxFiles
    selected, private, moved = tmp_path / "selected", tmp_path / "private", tmp_path / "moved"
    selected.mkdir()
    private.mkdir()
    (selected / "data.txt").write_bytes(b"public")
    (private / "data.txt").write_bytes(b"SYNTHETIC PRIVATE BYTES")
    original = SandboxFiles._drive
    def swap(self, workflow, **options):
        selected.rename(moved)
        selected.symlink_to(private, target_is_directory=True)
        return original(self, workflow, **options)
    monkeypatch.setattr(SandboxFiles, "_drive", swap)
    with client_for(lambda request: pytest.fail("outside bytes reached the API")) as client:
        with pytest.raises((nodus.ValidationError, OSError)):
            nodus.Sandbox(client, "sb_one").files.upload(selected / "data.txt", "data.txt")


@pytest.mark.parametrize("resource", ["sandboxes", "agents"])
def test_managed_setup_is_persisted_in_the_admitted_definition(resource):
    def handle(request):
        body = json.loads(request.content)
        assert body["setup"] == "python -m pip install -r requirements.txt"
        return httpx.Response(202, json={"id": "ag_one" if resource == "agents" else "sb_one"})
    with client_for(handle) as client:
        options = {"budget": 5, "setup": "python -m pip install -r requirements.txt"}
        if resource == "agents":
            options["name"] = "worker"
        assert getattr(client, resource).create(**options).id


@requires_posix_files
def test_project_archive_remains_bound_when_an_ancestor_is_swapped(tmp_path, monkeypatch):
    import os
    selected, private, moved = tmp_path / "selected", tmp_path / "private", tmp_path / "moved"
    for root, content in ((selected, b"public"), (private, b"SYNTHETIC PRIVATE BYTES")):
        (root / "project").mkdir(parents=True)
        (root / "project/main.py").write_bytes(content)
    original = os.fwalk
    def swap(*args, **kwargs):
        selected.rename(moved)
        selected.symlink_to(private, target_is_directory=True)
        return original(*args, **kwargs)
    monkeypatch.setattr(os, "fwalk", swap)
    def handle(request):
        if request.url.path == "/v1/sandboxes/capabilities":
            return httpx.Response(200, json={"available": True, "max_project_bytes": 100000})
        if request.url.path == "/v1/assets":
            return httpx.Response(200, json={"upload_idempotency": True, "max_import_bytes": 100000})
        if request.url.path == "/v1/assets/upload":
            with tarfile.open(fileobj=io.BytesIO(request.content), mode="r:gz") as archive:
                assert archive.extractfile("main.py").read() == b"public"
            return httpx.Response(201, json={"id": "asset_project", "state": "ready"})
        return httpx.Response(202, json={"id": "sb_one"})
    with client_for(handle) as client:
        assert client.sandboxes.create(project=selected / "project", budget=5).id == "sb_one"


def test_managed_revision_history_policy_and_explicit_retry_match_server_wire():
    from nodus._managed_agents import ManagedRun
    policy = {"network": "allowlist", "egress_allow": ["tools.example.com"]}
    revision = {"agent_id": "ag_one", "revision": 1, "definition": {"name": "worker", "budget_usd": 10, "entrypoint": "agent:main", "policy": policy}}
    def handle(request):
        if request.url.path.endswith("/revisions"):
            return httpx.Response(200, json={"revisions": [revision], "next_after": ""})
        if request.url.path.endswith("/revisions/1"):
            return httpx.Response(200, json=revision)
        if request.url.path.endswith("/retry"):
            assert request.headers["Idempotency-Key"] == "retry-1"
            return httpx.Response(202, json={"id": "run_one", "agent_id": "ag_one", "status": "queued"})
        assert json.loads(request.content)["policy"] == policy
        return httpx.Response(202, json={"id": "ag_one"})
    with client_for(handle) as client:
        agent = client.agents.create(name="worker", budget=10, policy=policy)
        assert agent.revisions()["revisions"][0] == revision
        assert agent.revision(1) == revision
        assert ManagedRun(client, {"id": "run_one", "agent_id": "ag_one"}).retry(idempotency_key="retry-1").status == "queued"


@requires_local_files
@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('resource', ['sandboxes', 'agents'])
@pytest.mark.parametrize('lost_response', ['upload', 'create'])
def test_project_retry_from_fresh_client_replays_one_asset(tmp_path, asynchronous, resource, lost_response):
    """Losing either receipt must preserve the upload and compute request identity."""
    (tmp_path / 'main.py').write_bytes(b'print(1)\n')
    uploads, attempts, lost = {}, [], False
    identifier = 'sb_one' if resource == 'sandboxes' else 'ag_one'

    def handle(request):
        nonlocal lost
        if request.url.path == '/v1/sandboxes/capabilities':
            return httpx.Response(200, json={'available': True, 'max_project_bytes': 100000})
        if request.url.path == '/v1/assets':
            return httpx.Response(200, json={'max_import_bytes': 100000, 'upload_idempotency': True})
        if request.url.path == '/v1/assets/upload':
            key = request.headers.get('Idempotency-Key') or 'anonymous-' + str(len(uploads))
            previous = uploads.get(key)
            if previous and previous[1] != request.content:
                return httpx.Response(409, json={'error': 'idempotency_conflict'})
            if previous is None:
                previous = uploads[key] = ('asset_' + str(len(uploads)), request.content)
            if not lost and lost_response == 'upload':
                lost = True
                raise httpx.ReadTimeout('upload receipt lost', request=request)
            return httpx.Response(201, json={'id': previous[0], 'state': 'ready'})
        assert request.url.path == '/v1/' + resource
        assert request.headers['Idempotency-Key'] == 'same-create'
        body = json.loads(request.content)
        if attempts and attempts[0] != body:
            return httpx.Response(409, json={'error': 'idempotency_conflict'})
        attempts.append(body)
        if not lost and lost_response == 'create':
            lost = True
            raise httpx.ReadTimeout('create receipt lost', request=request)
        return httpx.Response(202, json={'id': identifier})

    options = {'project': tmp_path, 'budget': 5, 'idempotency_key': 'same-create'}
    if resource == 'agents':
        options['name'] = 'worker'

    def create():
        if asynchronous:
            async def run():
                async with nodus.AsyncClient(api_key='test', base_url='https://nodus.invalid', max_retries=0) as client:
                    await client._http.aclose()
                    client._http = httpx.AsyncClient(base_url='https://nodus.invalid', transport=httpx.MockTransport(handle))
                    return await getattr(client, resource).create(**options)
            return asyncio.run(run())
        with client_for(handle) as client:
            return getattr(client, resource).create(**options)

    with pytest.raises(nodus.APITimeoutError):
        create()
    assert create().id == identifier
    assert len(uploads) == 1
    assert attempts[-1]['source']['asset_id'] == next(iter(uploads.values()))[0]
    (tmp_path / 'main.py').write_bytes(b'changed project\n')
    with pytest.raises(nodus.IdempotencyConflictError):
        create()
    assert len(uploads) == 1
