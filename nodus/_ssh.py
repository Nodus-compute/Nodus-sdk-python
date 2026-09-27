"""An ssh argv built from a connection's validated structured fields. No server text is interpolated."""
from __future__ import annotations

import ipaddress
import re
from typing import Any

from .errors import ValidationError

_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", re.ASCII)
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}", re.ASCII)
_PORT = re.compile(r"[1-9][0-9]{0,4}", re.ASCII)
# ssh runs ProxyCommand through a shell after substituting %h, which _host restricts to plain names.
_TUNNEL_PROXY = "ProxyCommand=cloudflared access ssh --hostname %h"


class UnsupportedTransport(Exception):
    """A connection this SDK cannot open directly."""


def _host(value: Any) -> str:
    # "%" would admit an IPv6 scope id, which ip_address accepts with arbitrary text after it.
    if isinstance(value, str) and 0 < len(value) <= 253 and "%" not in value:
        try:
            ipaddress.ip_address(value)
            return value
        except ValueError:
            if all(_LABEL.fullmatch(label) for label in value.split(".")):
                return value
    raise ValidationError("The server sent an SSH host that is not a plain host name or address")


def _port(value: Any) -> str:
    text = str(value) if type(value) is int else value
    if isinstance(text, str) and _PORT.fullmatch(text) and int(text) <= 65535:
        return text
    raise ValidationError("The server sent an SSH port that is not a number from 1 to 65535")


def _user(value: Any) -> str:
    if isinstance(value, str) and _USER.fullmatch(value):
        return value
    raise ValidationError("The server sent an SSH user name that is not a plain account name")


def ssh_argv(connection: Any) -> list[str]:
    """``ssh`` arguments for a tcp or tunnel connection. Anything else is refused."""
    transport = connection.get("transport") if isinstance(connection, dict) else None
    if transport not in ("tcp", "tunnel"):
        raise UnsupportedTransport(transport if isinstance(transport, str) else "")
    target = ["-p", _port(connection.get("port")), "-l", _user(connection.get("user")), "--",
              _host(connection.get("host"))]
    return ["ssh", *(["-o", _TUNNEL_PROXY] if transport == "tunnel" else []), *target]
