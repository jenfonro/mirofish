"""Proxy node parsing for manually entered endpoints.

Only transports the relay can dial directly are accepted: HTTP(S) and
SOCKS5. A node identity is its endpoint, not its name, so re-entering an
endpoint renames it instead of pooling two identical exits.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from typing import Any, Optional


def proxy_identity(config: dict[str, Any]) -> str:
    """A node's identity is the endpoint it dials, deliberately not its name.

    Renaming a node must not change its id: accounts are pinned by id, and a
    rename that re-identified the node would silently unpin every account on
    it. Two entries with the same endpoint are the same exit however they are
    labelled, so re-adding one renames it instead of pooling a duplicate.
    """
    endpoint = {key: config.get(key, "") for key in
                ("scheme", "host", "port", "username", "password")}
    canonical = json.dumps(endpoint, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def proxy_from_uri(value: str) -> Optional[dict[str, Any]]:
    value = value.strip().strip("'\"")
    if "://" not in value:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        scheme = {"http": "http", "https": "https", "socks": "socks5",
                  "socks5": "socks5", "socks5h": "socks5"}.get(parsed.scheme.lower())
        if not scheme or not parsed.hostname or not parsed.port:
            return None
        name = urllib.parse.unquote(parsed.fragment).strip()[:200] if parsed.fragment else ""
        return {"name": name or f"{parsed.hostname}:{parsed.port}", "scheme": scheme,
                "host": parsed.hostname, "port": parsed.port,
                "username": urllib.parse.unquote(parsed.username or ""),
                "password": urllib.parse.unquote(parsed.password or "")}
    except (ValueError, UnicodeError):
        return None


def proxy_url(config: dict[str, Any]) -> str:
    """Build an httpx-compatible proxy URL from a node config."""
    auth = ""
    if config.get("username") or config.get("password"):
        auth = (urllib.parse.quote(str(config.get("username", "")), safe="") + ":"
                + urllib.parse.quote(str(config.get("password", "")), safe="") + "@")
    host = config["host"]
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    return f"{config['scheme']}://{auth}{host}:{config['port']}"
