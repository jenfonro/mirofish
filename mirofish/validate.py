"""Input validation helpers shared across trust boundaries."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from .errors import RelayError

ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
CODE_RE = re.compile(r"\d{6}")

# Model ids exposed by the upstream can lose their dated Anthropic alias while
# existing clients keep sending it. Keep compatibility aliases narrow and only
# map ids whose canonical equivalent is unambiguous.
MODEL_ALIASES = {
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
}


def alias_value(value: str) -> str:
    value = value.strip()
    if not ALIAS_RE.fullmatch(value):
        raise RelayError("alias must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}", 400)
    return value


def email_value(value: str) -> str:
    value = value.strip()
    if not EMAIL_RE.fullmatch(value):
        raise RelayError("invalid email address", 400)
    return value


def code_value(value: str) -> str:
    value = value.strip()
    if not CODE_RE.fullmatch(value):
        raise RelayError("verification code must be 6 digits", 400)
    return value


def model_value(value: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > 200 or any(ch in value for ch in "\r\n\0"):
        raise RelayError("invalid model name", 400)
    return MODEL_ALIASES.get(value, value)


def proxy_node_value(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a manual HTTP(S)/SOCKS5 endpoint; its name may be empty."""
    scheme = str(payload.get("scheme", "socks5")).strip().lower()
    if scheme not in ("socks5", "http", "https"):
        raise RelayError("proxy scheme must be socks5, http or https", 400)
    host = str(payload.get("host") or "").strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or len(host) > 255 or any(
            ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or ch in "/?#@\\%[]"
            for ch in host):
        raise RelayError("invalid proxy host", 400)
    if ":" in host:
        try:
            host = str(ipaddress.IPv6Address(host))
        except ValueError as exc:
            raise RelayError("invalid proxy host", 400) from exc
    port_value = payload.get("port", 0)
    try:
        if isinstance(port_value, (bool, float)):
            raise ValueError
        port = int(port_value)
    except (TypeError, ValueError) as exc:
        raise RelayError("proxy port must be a number", 400) from exc
    if not 1 <= port <= 65535:
        raise RelayError("proxy port must be within 1-65535", 400)
    username = str(payload.get("username") or "")
    password = str(payload.get("password") or "")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in username + password):
        raise RelayError("invalid proxy credentials", 400)
    return {"name": str(payload.get("name") or "").strip()[:200],
            "scheme": scheme, "host": host, "port": port,
            "username": username, "password": password}
