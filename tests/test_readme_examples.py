"""Check published examples against the real SDK without submitting paid work.

Source documentation is intentionally absent from installed-wheel test runs.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
import shlex
from urllib.parse import unquote, urlsplit

import httpx
import pytest

from nodus import cli

ROOT = pathlib.Path(__file__).parents[1]
README = ROOT / "README.md"


def _documents():
    if not README.exists():
        pytest.skip("documentation checks require a source checkout")
    return [README, *(path for folder in ("docs", "examples", "openapi")
                       for path in sorted((ROOT / folder).rglob("*.md")))]


def _fences(text):
    """Yield language, body, and preceding test marker from Markdown fences."""
    pattern = r"(?P<marker><!-- test: [\w-]+ -->\s*)?^```(?P<lang>[^\n]*)\n(?P<body>.*?)^```\s*$"
    for match in re.finditer(pattern, text, re.MULTILINE | re.DOTALL):
        yield match["lang"].strip(), match["body"], match["marker"] or ""


def _nodus_lines(text: str) -> list[str]:
    lines = []
    for language, body, _ in _fences(text):
        if language not in {"bash", "sh", "shell", "console", ""}:
            continue
        for raw in re.sub(r"\\\n\s*", " ", body).splitlines():
            command = raw.strip().removeprefix("$ ").strip()
            if command == "nodus" or command.startswith("nodus "):
                lines.append(command)
    return lines


def test_shell_continuations_are_one_command():
    text = '```bash\nnodus run \\\n  --budget 2 \\\n  -- python -c "print(1)"\n```'
    assert _nodus_lines(text) == ['nodus run  --budget 2  -- python -c "print(1)"']


def test_every_documented_nodus_command_parses():
    failures = []
    count = 0
    for path in _documents():
        for command in _nodus_lines(path.read_text(encoding="utf-8")):
            count += 1
            argv = shlex.split(command, comments=True)[1:]
            if "--" in argv:
                argv = argv[:argv.index("--")]
            try:
                cli.build_parser().parse_args(argv)
            except SystemExit as exc:
                if exc.code:
                    failures.append(f"{path.relative_to(ROOT)}: {command}")
    assert count, "no documented nodus commands found"
    assert not failures, "CLI rejects documented commands:\n" + "\n".join(failures)


def test_documented_python_is_valid_syntax():
    for path in _documents():
        for language, body, _ in _fences(path.read_text(encoding="utf-8")):
            if language in {"python", "py"}:
                # Compile, rather than parse alone, while permitting async snippets
                # intended for notebooks or an enclosing coroutine.
                compile(body, str(path), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)


def test_local_markdown_links_resolve():
    failures = []
    for path in _documents():
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"^```.*?^```[^\n]*", "", text, flags=re.MULTILINE | re.DOTALL)
        destinations = re.findall(r"\]\(([^\s)]+)(?:\s+\"[^\"]*\")?\)", text)
        destinations += re.findall(r"^\[[^\]]+\]:\s*(\S+)", text, re.MULTILINE)
        for destination in destinations:
            parsed = urlsplit(destination.strip("<>"))
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = (path.parent / unquote(parsed.path)).resolve()
            if not target.exists():
                failures.append(f"{path.relative_to(ROOT)}: {destination}")
    assert not failures, "broken relative links:\n" + "\n".join(failures)


def test_readme_links_work_outside_github():
    if not README.exists():
        pytest.skip('README is not included in this environment')
    destinations = re.findall(r'\]\(([^\s)]+)\)', README.read_text(encoding='utf-8'))
    relative = [target for target in destinations if not target.startswith(('https://', '#'))]
    assert not relative, f'PyPI cannot resolve repository-relative links: {relative}'


def test_documented_assistant_turns_accept_caller_stable_request_keys():
    from types import SimpleNamespace
    source = (ROOT / 'docs/guides/managed-agents.md').read_text()
    body, = [body for language, body, _ in _fences(source)
             if language == 'python' and 'def submit_report(' in body]
    namespace = {}
    exec(compile(body, 'managed-agents.md', 'exec'), namespace)
    calls = []
    assistant = SimpleNamespace(submit=lambda task, **options: calls.append((task, options)))
    submit = namespace['submit_report']
    submit(assistant, 'First report', idempotency_key='report-001')
    submit(assistant, 'Second report', idempotency_key='report-002')
    submit(assistant, 'Second report', idempotency_key='report-002')
    assert [options['idempotency_key'] for _, options in calls] == ['report-001', 'report-002', 'report-002']
    assert calls[1] == calls[2]


def test_marked_first_workload_examples(monkeypatch):
    examples = []
    for path in _documents():
        for language, body, marker in _fences(path.read_text(encoding="utf-8")):
            if language == "python" and "test: first-workload" in marker:
                examples.append((path, body))
    assert examples, "mark the self-contained first-workload Python example for execution"
    monkeypatch.setenv("NODUS_API_KEY", "nk_live_documentation_test")
    monkeypatch.setenv("NODUS_BASE_URL", "https://nodus.invalid")
    original_client = httpx.Client
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer nk_live_documentation_test"
        if request.method == "POST" and request.url.path == "/v1/workloads":
            payload = json.loads(request.content)
            assert payload["source"]["command"], "first example must run a command"
            assert "max_cost_usd" not in payload.get("outcome", {}), "first example must not invent a cap"
            return httpx.Response(202, json={"id": "wl_docs", "workload_id": "wl_docs", "status": "accepted", "revision": 1})
        if request.method == "GET" and request.url.path == "/v1/workloads/wl_docs":
            return httpx.Response(200, json={"id": "wl_docs", "status": "completed", "revision": 2, "spend_usd": 0.01, "meter": {"total_now_usd": 0.01}})
        if request.method == "GET" and request.url.path == "/v1/workloads/wl_docs/logs":
            return httpx.Response(200, text="Hello from Nodus!\n")
        raise AssertionError(f"Unexpected documented request: {request.method} {request.url}")

    def mocked_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", mocked_client)
    for path, body in examples:
        calls.clear()
        exec(compile(body, str(path), "exec"), {"__name__": "__main__"})
        assert ("POST", "/v1/workloads") in calls
        assert ("GET", "/v1/workloads/wl_docs") in calls
