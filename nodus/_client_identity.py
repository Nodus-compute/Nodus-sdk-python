"""The Nodus-Client header: which surface sent a request, from a fixed allowlist."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

HEADER = "Nodus-Client"
ALLOWED = frozenset({"python-sdk", "cli", "mcp", "claude-code", "codex", "cursor"})
# Checked in order, against the MCP client's self-reported name.
_MCP_CLIENTS = (("claude", "claude-code"), ("codex", "codex"), ("cursor", "cursor"))

_current: ContextVar[str] = ContextVar("nodus_client", default="python-sdk")


def identify(value: str) -> str:
    """The value to send, refusing anything outside the allowlist."""
    if value not in ALLOWED:
        raise ValueError(f"{value!r} is not a Nodus client identity")
    return value


def current() -> str:
    return _current.get()


@contextmanager
def acting_as(value: str) -> Iterator[None]:
    """Clients constructed inside this block identify as ``value``."""
    token = _current.set(identify(value))
    try:
        yield
    finally:
        _current.reset(token)


def mcp_client(name: object) -> str:
    """Map an MCP client's self-reported name to an allowlisted value. Its text is never sent."""
    lowered = name.lower() if isinstance(name, str) else ""
    for needle, value in _MCP_CLIENTS:
        if needle in lowered:
            return value
    return "mcp"


def mcp_caller() -> str:
    """The identity of the MCP client whose request is being handled, from its initialize clientInfo."""
    from mcp.server.lowlevel.server import request_ctx
    context = request_ctx.get(None)
    params = getattr(getattr(context, "session", None), "client_params", None)
    return mcp_client(getattr(getattr(params, "clientInfo", None), "name", None))
