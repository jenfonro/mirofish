"""Input validation helpers shared across trust boundaries."""

from __future__ import annotations

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
    """Validate one manually entered proxy node.

    The name is optional and falls back to host:port: a pasted endpoint often
    has no meaningful name, and requiring one would just get a placeholder.
    """
    scheme = str(payload.get("scheme", "socks5")).strip().lower()
    if scheme not in ("socks5", "http", "https"):
        raise RelayError("proxy scheme must be socks5, http or https", 400)
    host = str(payload.get("host", "")).strip()
    if not host or len(host) > 255 or any(c.isspace() for c in host):
        raise RelayError("proxy host is required", 400)
    try:
        port = int(payload.get("port", 0))
    except (TypeError, ValueError) as exc:
        raise RelayError("proxy port must be a number", 400) from exc
    if not 1 <= port <= 65535:
        raise RelayError("proxy port must be within 1-65535", 400)
    name = str(payload.get("name", "")).strip()[:200]
    return {"name": name or "%s:%d" % (host, port), "scheme": scheme,
            "host": host, "port": port,
            "username": str(payload.get("username", "")).strip()[:200],
            "password": str(payload.get("password", ""))[:400]}
