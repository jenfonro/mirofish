"""Proxy subscription parsing: URI list, base64 URI list, JSON, or Mihomo YAML.

Only transports the relay can dial directly (HTTP(S) and SOCKS5) are returned;
other Mihomo transports are counted as skipped (Mihomo mode handles those).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import urllib.parse
from typing import Any, Optional

import yaml

from ..errors import RelayError


def proxy_identity(config: dict[str, Any]) -> str:
    # Must stay byte-identical to the legacy relay so existing account-to-node
    # assignments in accounts.sqlite3 survive the upgrade.
    canonical = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def proxy_from_mapping(mapping: dict[str, Any], fallback_name: str = "") -> Optional[dict[str, Any]]:
    kind = str(mapping.get("type", "")).strip().lower()
    scheme = {"http": "http", "https": "https", "socks": "socks5", "socks5": "socks5"}.get(kind)
    if not scheme or (scheme == "socks5" and bool(mapping.get("tls"))):
        return None
    host = str(mapping.get("server", mapping.get("host", ""))).strip()
    try:
        port = int(mapping.get("port", 0))
    except (TypeError, ValueError):
        return None
    if not host or not 1 <= port <= 65535:
        return None
    name = str(mapping.get("name", fallback_name)).strip()[:200] or f"{host}:{port}"
    username = mapping.get("username", mapping.get("user"))
    password = mapping.get("password")
    return {"name": name, "scheme": scheme, "host": host, "port": port,
            "username": str(username) if username is not None else "",
            "password": str(password) if password is not None else ""}


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


def _yaml_mappings(candidate: str) -> list[dict[str, Any]]:
    try:
        parsed = yaml.safe_load(candidate)
    except yaml.YAMLError:
        return []
    if isinstance(parsed, dict) and isinstance(parsed.get("proxies"), list):
        return [item for item in parsed["proxies"] if isinstance(item, dict)]
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def parse_proxy_subscription(raw: bytes) -> tuple[list[dict[str, Any]], int]:
    text = raw.decode("utf-8", "replace").lstrip("﻿").strip()
    candidates = [text]
    compact = "".join(text.split())
    if len(compact) >= 16:
        try:
            decoded = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=False)
            decoded_text = decoded.decode("utf-8", "replace").strip()
            if decoded_text and decoded_text != text:
                candidates.insert(0, decoded_text)
        except (ValueError, binascii.Error):
            pass

    supported: list[dict[str, Any]] = []
    skipped = 0
    seen: set[str] = set()
    for candidate in candidates:
        mappings: list[dict[str, Any]] = []
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and isinstance(parsed.get("proxies"), list):
                mappings = [item for item in parsed["proxies"] if isinstance(item, dict)]
            elif isinstance(parsed, list):
                mappings = [item for item in parsed if isinstance(item, dict)]
        except json.JSONDecodeError:
            pass
        if not mappings and ("proxies:" in candidate or candidate.startswith("- ")):
            mappings = _yaml_mappings(candidate)
        entries: list[Optional[dict[str, Any]]] = []
        if mappings:
            entries.extend(proxy_from_mapping(item) for item in mappings)
        else:
            entries.extend(proxy_from_uri(line) for line in candidate.splitlines())
        candidate_supported = False
        for item in entries:
            if item is None:
                if any(token in candidate.lower() for token in ("://", "type:", "proxies:")):
                    skipped += 1
                continue
            candidate_supported = True
            identity = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if identity not in seen:
                seen.add(identity)
                supported.append(item)
        if candidate_supported:
            break
    if not supported and text:
        raise RelayError("proxy subscription contains no supported HTTP(S)/SOCKS5 nodes", 502,
                         {"skipped": skipped})
    return supported, skipped
