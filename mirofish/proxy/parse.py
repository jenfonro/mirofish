"""Manual proxy endpoints: one HTTP(S)/SOCKS5 URI per line."""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from typing import Any, Optional

from ..errors import RelayError
from ..validate import proxy_node_value

# Stored binding sentinel. NULL/empty stored ids are unbound, never direct.
DIRECT = "direct"


def proxy_identity(config: dict[str, Any]) -> str:
    """Endpoint fingerprint for deduplication, not an editable node's id."""
    endpoint = {key: config.get(key, "") for key in
                ("scheme", "host", "port", "username", "password")}
    canonical = json.dumps(endpoint, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def proxy_from_uri(value: str) -> Optional[dict[str, Any]]:
    value = value.strip().strip("'\"")
    if "://" not in value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        scheme = {"http": "http", "https": "https", "socks": "socks5",
                  "socks5": "socks5", "socks5h": "socks5"}.get(parsed.scheme.lower())
        if not scheme or not parsed.hostname or not parsed.port \
                or parsed.path not in ("", "/") or parsed.query:
            return None
        return proxy_node_value({
            "name": urllib.parse.unquote(parsed.fragment),
            "scheme": scheme, "host": parsed.hostname, "port": parsed.port,
            "username": urllib.parse.unquote(parsed.username or ""),
            "password": urllib.parse.unquote(parsed.password or ""),
        })
    except (RelayError, ValueError, UnicodeError):
        return None


def parse_proxy_uris(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Return entries and redacted line errors; blanks/comments are ignored."""
    nodes, failed = [], []
    for number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        config = proxy_from_uri(line)
        if config is None:
            failed.append("line %d: invalid proxy URI" % number)
        else:
            nodes.append(config)
    return nodes, failed


def proxy_url(config: dict[str, Any] | str | None) -> Optional[str]:
    """None is explicit direct at this boundary; resolve stored ids before calling."""
    if config is None or config == DIRECT:
        return None
    if not isinstance(config, dict) or "mihomo_node" in config \
            or not {"scheme", "host", "port"} <= config.keys():
        raise RelayError("proxy binding is missing or invalid", 503)
    try:
        config = proxy_node_value(config)
    except RelayError as exc:
        raise RelayError("proxy configuration is invalid", 503) from exc
    auth = ""
    if config["username"] or config["password"]:
        auth = (urllib.parse.quote(config["username"], safe="") + ":"
                + urllib.parse.quote(config["password"], safe="") + "@")
    host = config["host"]
    if ":" in host:
        host = "[" + host + "]"
    return f"{config['scheme']}://{auth}{host}:{config['port']}"
