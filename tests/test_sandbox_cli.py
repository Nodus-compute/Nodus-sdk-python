"""CLI journeys for the public sandbox resource."""

from __future__ import annotations

import base64
import json
import shlex

import httpx
import pytest

import nodus
from nodus import cli

SANDBOX = {
    "id": "sb_agent",
    "state": "ready",
    "envelope": {"name": "agent-session", "source": {"image": "python:3.12"}},
    "cost_usd": 1.25,
    "url": "https://console.nodus-compute.ai/sandboxes/sb_agent",
}
EXEC = {
    "id": "sx_python",
    "sandbox_id": "sb_agent",
    "spec": {"command": ["python", "-c", "print('hello')"]},
    "state": "running",
    "exit_code": None,
}


def client_factory(handler):
    def build(**_kwargs):
        client = nodus.Client(api_key="nk_live_test", base_url="https://nodus.invalid")
        client._http = httpx.Client(
            base_url="https://nodus.invalid",
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer nk_live_test"},
        )
        return client

    return build


@pytest.mark.parametrize("arguments", [
    ["devbox", "up", "scratch"], ["devbox", "ls"],
    ["devbox", "shell", "scratch"], ["devbox", "rm", "scratch"],
])
def test_cli_rejects_removed_devbox_commands(arguments):
    with pytest.raises(SystemExit) as error:
        cli.build_parser().parse_args(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize("reference", ["sb_legacy", "scratch"])
def test_existing_profile_resources_remain_inspectable_and_terminable(reference, monkeypatch, capsys):
    legacy = {**SANDBOX, "id": "sb_legacy", "envelope": {"name": "scratch", "profile": "devbox"}}
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/v1/sandboxes":
            return httpx.Response(200, json={"sandboxes": [legacy], "next_cursor": None})
        if request.method == "GET" and request.url.path == "/v1/sandboxes/sb_legacy":
            return httpx.Response(200, json=legacy)
        assert request.method == "POST" and request.url.path == "/v1/sandboxes/sb_legacy/terminate"
        assert request.headers["Idempotency-Key"] == "cleanup-existing"
        return httpx.Response(200, json={**legacy, "state": "terminated"})

    monkeypatch.setattr(cli, "Client", client_factory(handler))
    assert cli.main(["sandbox", "ls", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == "sb_legacy"
    assert cli.main(["sandbox", "rm", "--idempotency-key", "cleanup-existing", reference]) == 0
    assert capsys.readouterr().out.strip() == "sb_legacy"
    assert [call for call in calls if call[0] == "POST"] == [("POST", "/v1/sandboxes/sb_legacy/terminate")]


def test_sandbox_cli_mirrors_create_exec_logs_cost_list_and_remove(monkeypatch, capsys):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, dict(request.url.params), body))
        if request.method == "POST" and request.url.path == "/v1/sandboxes":
            return httpx.Response(202, json=SANDBOX)
        if request.method == "GET" and request.url.path == "/v1/sandboxes":
            return httpx.Response(200, json={"sandboxes": [SANDBOX], "next_cursor": None})
        if request.method == "GET" and request.url.path == "/v1/sandboxes/sb_agent":
            return httpx.Response(200, json=SANDBOX)
        if request.method == "POST" and request.url.path.endswith("/exec"):
            return httpx.Response(202, json=EXEC)
        if request.method == "GET" and request.url.path.endswith("/stream"):
            return httpx.Response(200, json={
                "frames": [{
                    "sequence": 1,
                    "stream": "stdout",
                    "offset": 0,
                    "data": base64.b64encode(b"hello\n").decode(),
                    "created_at": "2026-09-13T12:00:05Z",
                }],
                "next_sequence": 1,
                "last_sequence": 1,
                "final_sequence": 1,
                "state": "completed",
                "done": True,
                "complete": True,
            })
        if request.method == "GET" and request.url.path.endswith("/execs/sx_python"):
            return httpx.Response(200, json={**EXEC, "state": "completed", "exit_code": 0})
        if request.method == "POST" and request.url.path.endswith("/terminate"):
            return httpx.Response(200, json={**SANDBOX, "state": "terminated"})
        raise AssertionError(request.url)

    monkeypatch.setattr(cli, "Client", client_factory(handler))

    assert cli.main(["sandbox", "new", "python:3.12", "--name", "agent-session", "--budget", "3"]) == 0
    assert cli.main(["sandbox", "exec", "sb_agent", "printf 'hello\\n'"]) == 0
    assert cli.main(["sandbox", "logs", "sb_agent", "sx_python"]) == 0
    assert cli.main(["sandbox", "cost", "sb_agent"]) == 0
    assert cli.main(["--plain", "sandbox", "ls"]) == 0
    assert cli.main(["sandbox", "rm", "sb_agent"]) == 0

    output = capsys.readouterr().out
    assert "sb_agent" in output
    assert output.count("hello") == 2
    assert "$1.25" in output
    cost_read = ("GET", "/v1/sandboxes/sb_agent", {}, None)
    assert cost_read in calls
    exec_call = next(call for call in calls if call[0] == "POST" and call[1].endswith("/exec"))
    assert exec_call[3]["command"] == ["/bin/sh", "-lc", "printf 'hello\\n'"]


def test_sandbox_cli_accepts_name_only_reattach():
    args = cli.build_parser().parse_args(["sandbox", "new", "--name", "agent-session"])
    assert args.image is None
    assert args.name == "agent-session"
    assert "sandbox" in cli.build_parser().format_help()


def test_managed_sandbox_cli_creation_and_controls(monkeypatch, capsys):
    calls = []
    def handler(request):
        calls.append((request.url.path, json.loads(request.content) if request.content else None))
        return httpx.Response(202, json=SANDBOX)
    monkeypatch.setattr(cli, "Client", client_factory(handler))
    assert cli.main(["sandbox", "new", "--budget", "5"]) == 0
    assert cli.main(["sandbox", "sleep", "sb_agent", "--idempotency-key", "sleep-1"]) == 0
    assert cli.main(["sandbox", "wake", "sb_agent", "--idempotency-key", "wake-1"]) == 0
    assert cli.main(["sandbox", "detail", "sb_agent"]) == 0
    assert calls[0][1]["template"] == "nodus:agent-tools-v1"
    assert calls[0][1]["outcome"]["max_cost_usd"] == 5
    assert any(path.endswith("/sleep") for path, _ in calls)
    assert "sb_agent" in capsys.readouterr().out


def test_sandbox_file_cli_accepts_customer_transfer_commands():
    parser = cli.build_parser()
    assert parser.parse_args(["sandbox", "new", "--project", ".", "--budget", "5"]).project == "."
    assert parser.parse_args(["sandbox", "files", "download", "sb_agent", "results", "./results"]).destination == "./results"
    assert parser.parse_args(["sandbox", "files", "upload", "sb_agent", "./local", "project"]).source == "./local"
    assert parser.parse_args(["sandbox", "files", "read", "sb_agent", "result.txt"]).path == "result.txt"


@pytest.mark.parametrize("command, resource", [
    (["sandbox", "new", "--name", "repo-session"], "sandboxes"),
    (["agent", "deploy", "repo-agent"], "agents"),
])
@pytest.mark.parametrize("ref", [None, "feature/agent-tools"])
def test_github_cli_preserves_connected_source_budget_and_retry_receipt(command, resource, ref, monkeypatch, capsys):
    calls = []
    bootstrap = {"repo": "private-org/agent", "setup": "python -m pip install -r requirements.txt"}
    if ref:
        bootstrap["ref"] = ref

    def handler(request):
        assert request.method == "POST" and request.url.path == "/v1/" + resource
        assert request.headers["Idempotency-Key"] == "same-repo-admission"
        body = json.loads(request.content)
        calls.append(body)
        assert body["bootstrap"] == bootstrap
        assert body["network_permissions"] == ["python_packages", "github"]
        assert "source" not in body and "setup" not in body
        if resource == "sandboxes":
            assert body["name"] == "repo-session"
            assert body["template"] == "nodus:agent-tools-v1"
            assert body["outcome"]["max_cost_usd"] == 5
        else:
            assert body["name"] == "repo-agent" and body["budget_usd"] == 5
            assert body["entrypoint"] == "agent:main"
        if len(calls) == 1:
            return httpx.Response(202, json={"state": "creating"})
        return httpx.Response(202, json=SANDBOX if resource == "sandboxes" else {"id": "ag_repo", "status": "active"})

    monkeypatch.setattr(cli, "Client", client_factory(handler))
    arguments = [*command, "--github-repo", "private-org/agent", "--budget", "5",
                 "--setup", bootstrap["setup"], "--network-permission", "python_packages",
                 "--idempotency-key", "same-repo-admission"]
    if ref:
        arguments.extend(["--github-ref", ref])
    assert cli.main(arguments) == 2
    assert "same-repo-admission" in capsys.readouterr().err
    assert cli.main(arguments) == 0
    output = capsys.readouterr().out
    assert ("sb_agent" if resource == "sandboxes" else "ag_repo") in output
    assert len(calls) == 2 and calls[0] == calls[1]


@pytest.mark.parametrize("command", [["sandbox", "new"], ["agent", "deploy", "repo-agent"]])
@pytest.mark.parametrize("sources", [
    ["--github-repo", "org/repo", "--project", "."],
    ["--github-repo", "org/repo", "--source-asset-id", "asset_project"],
    ["--project", ".", "--source-asset-id", "asset_project"],
])
def test_github_cli_rejects_conflicting_sources(command, sources, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Client", lambda **kwargs: pytest.fail("conflicting sources reached a client"))
    with pytest.raises(SystemExit) as error:
        cli.main([*command, *sources, "--budget", "5"])
    assert error.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


@pytest.mark.parametrize("command", [["sandbox", "new"], ["agent", "deploy", "repo-agent"]])
def test_github_cli_requires_repository_for_ref_before_creating_a_client(command, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Client", lambda **kwargs: pytest.fail("ref without repo reached a client"))
    assert cli.main([*command, "--github-ref", "main", "--budget", "5"]) == 2
    assert "--github-ref requires --github-repo" in capsys.readouterr().err


@pytest.mark.parametrize("command", [["sandbox", "new"], ["agent", "deploy", "repo-agent"]])
@pytest.mark.parametrize("repository", ["https://token-private@github.com/org/repo", "org/../repo", "org/repo.git"])
def test_github_cli_rejects_urls_without_echoing_credentials(command, repository, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Client", lambda **kwargs: pytest.fail("invalid repository reached a client"))
    with pytest.raises(SystemExit) as error:
        cli.main([*command, "--github-repo", repository, "--budget", "5"])
    assert error.value.code == 2
    output = capsys.readouterr().err
    assert "owner/name" in output and repository not in output


def test_sandbox_cli_reuses_uploaded_source_without_reading_local_files(monkeypatch, capsys):
    calls = []
    def handler(request):
        assert request.method == "POST" and request.url.path == "/v1/sandboxes"
        body = json.loads(request.content)
        calls.append(body)
        assert body["source"] == {"asset_id": "asset_project"}
        assert body["template"] == "nodus:agent-tools-v1"
        assert body["outcome"]["max_cost_usd"] == 5
        return httpx.Response(202, json=SANDBOX)
    monkeypatch.setattr(cli, "Client", client_factory(handler))
    assert cli.main(["sandbox", "new", "--name", "repo-session", "--source-asset-id", "asset_project", "--budget", "5"]) == 0
    assert capsys.readouterr().out.strip() == "sb_agent" and len(calls) == 1


def test_sandbox_cli_rejects_nonpositive_and_nonfinite_budgets():
    parser = cli.build_parser()
    for value in ("0", "-1", "nan", "inf"):
        try:
            parser.parse_args(["sandbox", "new", "python:3.12", "--budget", value])
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError(f"accepted invalid budget {value}")


@pytest.mark.parametrize("name", ["agent-test", "agent.test", "sb_named"])
@pytest.mark.parametrize("verb", ["exec", "logs", "cost", "rm"])
def test_named_sandbox_commands_resolve_exact_active_identity(name, verb, monkeypatch, capsys):
    calls = []
    box = {**SANDBOX, "envelope": {"name": name}}

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == f"/v1/sandboxes/{name}":
            return httpx.Response(404, json={"code": "not_found", "message": "The sandbox resource was not found."})
        if request.url.path == "/v1/sandboxes":
            assert request.url.params["name"] == name
            if "cursor" not in request.url.params:
                return httpx.Response(200, json={"sandboxes": [
                    {**box, "id": "sb_old", "state": "terminated"},
                    {**box, "id": "sb_other", "envelope": {"name": name + "-other"}},
                ], "next_cursor": "next"})
            assert request.url.params["cursor"] == "next"
            return httpx.Response(200, json={"sandboxes": [box], "next_cursor": None})
        assert request.url.path.startswith("/v1/sandboxes/sb_agent")
        if request.url.path.endswith("/exec"):
            return httpx.Response(202, json={**EXEC, "state": "completed", "exit_code": 0})
        if request.url.path.endswith("/stream"):
            return httpx.Response(200, json={"frames": [], "next_sequence": 0, "last_sequence": 0,
                                            "final_sequence": 0, "state": "completed", "done": True, "complete": True})
        return httpx.Response(200, json=box)

    monkeypatch.setattr(cli, "Client", client_factory(handler))
    arguments = ["sandbox", verb, name] + ({"exec": ["true"], "logs": ["sx_python"]}.get(verb, []))
    assert cli.main(arguments) == 0
    assert any(path == "/v1/sandboxes" for _, path in calls)
    assert all(method == "GET" or path.startswith("/v1/sandboxes/sb_agent/") for method, path in calls)


@pytest.mark.parametrize("rows,cursor,expected", [
    ([], None, "No active sandbox"),
    ([SANDBOX, {**SANDBOX, "id": "sb_second"}], None, "More than one"),
    ([], "repeat", "pagination did not advance"),
])
def test_name_resolution_never_guesses_or_creates(rows, cursor, expected, monkeypatch, capsys):
    def handler(request):
        assert request.method == "GET"
        if request.url.path != "/v1/sandboxes":
            return httpx.Response(404, json={"code": "not_found"})
        return httpx.Response(200, json={"sandboxes": rows, "next_cursor": cursor})

    monkeypatch.setattr(cli, "Client", client_factory(handler))
    assert cli.main(["sandbox", "rm", "agent-session"]) == 2
    assert expected in capsys.readouterr().err


@pytest.mark.parametrize("reference", ["bad/name", "../agent", "agent\nforged", "", "a" * 129])
def test_invalid_sandbox_reference_never_reaches_network(reference, monkeypatch, capsys):
    def handler(request):
        pytest.fail("Invalid reference reached the API")

    monkeypatch.setattr(cli, "Client", client_factory(handler))
    assert cli.main(["sandbox", "rm", reference]) == 2


@pytest.mark.parametrize("body,expected", [
    ({"code": "not_found", "message": "The sandbox resource was not found."}, "not found"),
    (None, "endpoint is unavailable"),
])
def test_sandbox_missing_resource_is_distinct_from_missing_endpoint(body, expected, monkeypatch, capsys):
    def handler(request):
        if request.url.path == "/v1/sandboxes":
            return httpx.Response(200, json={"sandboxes": [], "next_cursor": None})
        return httpx.Response(404, json=body) if body else httpx.Response(404, text="404 page not found")

    monkeypatch.setattr(cli, "Client", client_factory(handler))
    assert cli.main(["sandbox", "cost", "sb_missing"]) == 2
    assert expected in capsys.readouterr().err


def test_failed_sandbox_exec_explains_failure_without_logs(monkeypatch, capsys):
    def handler(request):
        if request.url.path.endswith("/stream"):
            return httpx.Response(200, json={"frames": [], "next_sequence": 0, "last_sequence": 0,
                "final_sequence": None, "state": "lost", "done": True, "complete": False})
        if request.url.path.endswith(("/exec", "/execs/sx_python")):
            return httpx.Response(202, json={**EXEC, "state": "lost", "failure_code": "generation_lost"})
        return httpx.Response(200, json=SANDBOX)

    monkeypatch.setattr(cli, "Client", client_factory(handler))
    assert cli.main(["sandbox", "exec", "sb_agent", "true"]) != 0
    error = capsys.readouterr().err
    assert "sx_python" in error and "generation_lost" in error


def test_missing_canonical_id_never_selects_another_sandbox_name(monkeypatch, capsys):
    reference = "sb_c1f8f165-f6a9-4a65-ada2-c3e631e4344b"

    def handler(request):
        assert request.method == "GET"
        if request.url.path == "/v1/sandboxes":
            return httpx.Response(200, json={"sandboxes": [{**SANDBOX, "envelope": {"name": reference}}]})
        return httpx.Response(404, json={"code": "not_found"})

    monkeypatch.setattr(cli, "Client", client_factory(handler))
    assert cli.main(["sandbox", "rm", reference]) == 2
    assert "not found" in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["connection", "interrupt"])
def test_accepted_command_observation_failure_preserves_receipt(failure, monkeypatch, capsys):
    def handler(request):
        if request.url.path.endswith("/exec"):
            return httpx.Response(202, json=EXEC)
        if request.url.path.endswith("/stream"):
            if failure == "interrupt":
                raise KeyboardInterrupt
            raise httpx.ConnectError("connection lost", request=request)
        if request.url.path.endswith("/execs/sx_python"):
            return httpx.Response(200, json=EXEC)
        return httpx.Response(200, json=SANDBOX)

    build = client_factory(handler)

    def factory(**kwargs):
        client = build()
        client.max_retries = 0
        return client

    monkeypatch.setattr(cli, "Client", factory)
    assert cli.main(["sandbox", "exec", "sb_agent", "true"]) != 0
    error = capsys.readouterr().err
    assert "nodus sandbox logs sb_agent sx_python --follow" in error
    assert "Do not rerun" in error


def test_sandbox_list_includes_reusable_name(monkeypatch, capsys):
    monkeypatch.setattr(cli, "Client", client_factory(lambda request: httpx.Response(
        200, json={"sandboxes": [SANDBOX], "next_cursor": None})))
    assert cli.main(["sandbox", "ls", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "agent-session"


@pytest.mark.parametrize("arguments", [
    ["sandbox", "new", "python:3.12", "--budget", "5"],
    ["sandbox", "exec", "sb_agent", "true"],
    ["sandbox", "rm", "sb_agent"],
])
@pytest.mark.parametrize("failure", ["timeout", "server", "malformed"])
def test_uncertain_sandbox_mutation_exposes_reusable_request_key(arguments, failure, monkeypatch, capsys):
    requests = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=SANDBOX)
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("response lost", request=request)
        if failure == "server":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(202, text="invalid json")

    build = client_factory(handler)

    def factory(**kwargs):
        client = build()
        client.max_retries = 0
        return client

    monkeypatch.setattr(cli, "Client", factory)
    assert cli.main(arguments) == 2
    error = capsys.readouterr().err
    assert "outcome unknown" in error
    assert "--idempotency-key" in error
    assert requests[0].headers["Idempotency-Key"] in error
    assert len({request.headers["Idempotency-Key"] for request in requests}) == 1


@pytest.mark.parametrize("verb,tail", [("new", ["python:3.12", "--budget", "5"]), ("exec", ["sb_agent", "true"]), ("rm", ["sb_agent"])])
def test_sandbox_cli_reuses_explicit_request_key(verb, tail, monkeypatch, capsys):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=SANDBOX)
        assert request.headers["Idempotency-Key"] == "retry-key"
        return httpx.Response(409, json={"code": "idempotency_conflict", "message": "Request differs."})

    monkeypatch.setattr(cli, "Client", client_factory(handler))
    assert cli.main(["sandbox", verb, "--idempotency-key", "retry-key", *tail]) == 2
    assert "outcome unknown" not in capsys.readouterr().err


@pytest.mark.parametrize("key", ["-retry", "key'$(never-execute)"])
def test_uncertain_request_hint_is_a_literal_reusable_cli_option(key, monkeypatch, capsys):
    def handler(request):
        assert request.headers["Idempotency-Key"] == key
        return httpx.Response(503, json={"error": "unavailable"})

    build = client_factory(handler)

    def factory(**kwargs):
        client = build()
        client.max_retries = 0
        return client

    monkeypatch.setattr(cli, "Client", factory)
    assert cli.main(["sandbox", "new", "--idempotency-key=" + key, "python:3.12", "--budget", "5"]) == 2
    error = capsys.readouterr().err
    option = error.split("with ", 1)[1].split(" before the positional", 1)[0]
    args = cli.build_parser().parse_args(["sandbox", "new", *shlex.split(option), "python:3.12", "--budget", "5"])
    assert args.idempotency_key == key


@pytest.mark.parametrize("field", ["code", "error"])
def test_error_code_accessor_preserves_both_customer_api_envelopes(field):
    from nodus.errors import error_from_response

    error = error_from_response("GET", "/v1/sandboxes/sb_missing", 404,
                                {field: "not_found", "message": "The sandbox resource was not found."})
    assert error.code == "not_found"

@pytest.mark.parametrize("command", [["sandbox", "cost", "sb_agent"], ["--plain", "sandbox", "ls"]])
def test_sandbox_cli_never_presents_pending_zero_as_final_cost(command, monkeypatch, capsys):
    finalized = False

    def handler(request):
        box = {**SANDBOX, "state": "terminated", "cost_usd": 0.20 if finalized else 0,
               "charge_state": "final" if finalized else "estimated"}
        if finalized:
            box["final_charge_usd"] = 1.25
        return httpx.Response(200, json={"sandboxes": [box]} if request.url.path == "/v1/sandboxes" else box)

    monkeypatch.setattr(cli, "Client", client_factory(handler))
    assert cli.main(command) == 0
    pending = capsys.readouterr().out
    assert "Finalizing cost" in pending and "$0.00" not in pending
    finalized = True
    assert cli.main(command) == 0
    final = capsys.readouterr().out
    assert "$1.25" in final and "$0.20" not in final and "Finalizing" not in final
