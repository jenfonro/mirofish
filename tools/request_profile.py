#!/usr/bin/env python3
"""Create and compare secret-free HTTP request profiles.

The module is intentionally stdlib-only. It can be imported by tests, used as
``python tools/request_profile.py validate|compare ...``, or loaded directly by
mitmdump::

    mitmdump -q -nr /path/to/flows -s tools/request_profile.py

Mitmdump output contains ordered header names and structural JSON information,
but never credentials, signatures, account identifiers, or request-body text.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import unquote_plus, urlsplit


SCHEMA_VERSION = 1
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
_UNIX_MS = re.compile(r"[0-9]{13}")
_CACHE_BREAKPOINT = re.compile(
    r"(?:tools|system)\[[0-9]+\]|messages\[[0-9]+\]\.content\[[0-9]+\]")
_SAFE_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
_SAFE_HTTP_VERSIONS = frozenset({"HTTP/1.0", "HTTP/1.1", "HTTP/2", "HTTP/2.0", "HTTP/3"})
_SAFE_ROLES = {"assistant", "system", "tool", "user"}
_SAFE_BLOCK_TYPES = {
    "document", "image", "redacted_thinking", "server_tool_use", "text",
    "thinking", "tool_result", "tool_use", "web_search_tool_result",
}
_SAFE_HEADER_EXACT_VALUES = {
    "accept": frozenset({"application/json"}),
    "accept-encoding": frozenset({"gzip, deflate, br, zstd", "identity"}),
    "anthropic-dangerous-direct-browser-access": frozenset({"true"}),
    "anthropic-version": frozenset({"2023-06-01"}),
    "connection": frozenset({"keep-alive"}),
    "content-encoding": frozenset({"gzip"}),
    "content-type": frozenset({"application/json"}),
    "host": frozenset({
        "auth.mirasim.ai", "cdn-assets.mirasim.ai", "relay.mirasim.ai",
    }),
    "user-agent": frozenset({
        "claude-cli/2.1.241 (external, mirasim)",
        "mirasim-desktop/0.0.220",
    }),
    "x-app": frozenset({"cli"}),
    "x-mirasim-agent": frozenset({"claude"}),
    "x-mirasim-client": frozenset({"0.0.220"}),
    "x-mirasim-locale": frozenset({"zh-HK"}),
    "x-mirasim-probe": frozenset({"usage"}),
    "x-stainless-arch": frozenset({"arm64"}),
    "x-stainless-lang": frozenset({"js"}),
    "x-stainless-os": frozenset({"MacOS"}),
    "x-stainless-package-version": frozenset({"0.112.1"}),
    "x-stainless-retry-count": frozenset({"0"}),
    "x-stainless-runtime": frozenset({"node"}),
    "x-stainless-runtime-version": frozenset({"v26.3.0"}),
    "x-stainless-timeout": frozenset({"600"}),
}
_SAFE_ANTHROPIC_BETAS = frozenset({
    "advisor-tool-2026-03-01",
    "claude-code-20250219",
    "context-1m-2025-08-07",
    "context-management-2025-06-27",
    "effort-2025-11-24",
    "interleaved-thinking-2025-05-14",
    "mid-conversation-system-2026-04-07",
    "oauth-2025-04-20",
    "prompt-caching-scope-2026-01-05",
    "thinking-token-count-2026-05-13",
})
_SAFE_HEADER_VALUE_NAMES = frozenset(_SAFE_HEADER_EXACT_VALUES) | {"anthropic-beta"}
_SAFE_JSON_KEYS = {
    "code", "context_management", "deviceId", "email", "events", "max_tokens",
    "messages", "metadata", "model", "output_config", "publicKey", "stream",
    "system", "thinking", "tools",
}
_SAFE_PATHS = {
    "/",
    "/auth/code",
    "/auth/me",
    "/auth/oauth/providers",
    "/auth/referral",
    "/auth/refresh",
    "/auth/verify",
    "/events",
    "/mirasim/releases/latest.json",
    "/v1/device/session",
    "/v1/limits",
    "/v1/messages",
    "/v1/messages/count_tokens",
    "/v1/models",
}
_REDACTED_PATH = "/<redacted:path>"
_REDACTED_QUERY_FIELD = "<redacted:query-field>"
_REDACTED_HEADERS = {
    "x-access-token": "token",
    "authorization": "authorization",
    "cookie": "cookie",
    "x-auth-token": "token",
    "x-device-ticket": "ticket",
    "proxy-authorization": "proxy_authorization",
    "set-cookie": "cookie",
    "x-account-id": "account",
    "x-api-key": "api_key",
    "x-mirasim-account": "account",
    "x-mirofish-account": "account",
    "x-refresh-token": "token",
    "x-relay-ticket": "ticket",
}
_DYNAMIC_HEADER_NAMES = frozenset({
    "content-length", "x-claude-code-session-id", "x-mirasim-call",
    "x-mirasim-device", "x-mirasim-nonce", "x-mirasim-session",
    "x-mirasim-sig", "x-mirasim-ts",
})
_SAFE_HEADER_NAMES = (
    _SAFE_HEADER_VALUE_NAMES | frozenset(_REDACTED_HEADERS) | _DYNAMIC_HEADER_NAMES)
_REDACTED_HEADER_NAME = "<redacted:header-name>"


class UnsafeProfile(ValueError):
    """The input cannot be represented without risking secret disclosure."""


def _safe_literal_header_value(name: str, value: str) -> bool:
    if name == "anthropic-beta":
        features = value.split(",")
        return bool(features) and all(
            feature in _SAFE_ANTHROPIC_BETAS for feature in features)
    return value in _SAFE_HEADER_EXACT_VALUES.get(name, ())


def _base64url_bytes(value: str, expected: int, label: str) -> bytes:
    if not _BASE64URL.fullmatch(value):
        raise UnsafeProfile(f"invalid {label}: expected unpadded base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise UnsafeProfile(f"invalid {label}: malformed base64url") from exc
    if len(decoded) != expected:
        raise UnsafeProfile(f"invalid {label}: expected {expected} decoded bytes")
    return decoded


def _uuid4(value: str, label: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise UnsafeProfile(f"invalid {label}: expected UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != value.lower():
        raise UnsafeProfile(f"invalid {label}: expected canonical UUIDv4")


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _block_types(content: Any) -> list[str] | None:
    if not isinstance(content, list):
        return None
    result: list[str] = []
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else None
        result.append(kind if kind in _SAFE_BLOCK_TYPES else "<other>")
    return result


def _body_shape(body: bytes) -> dict[str, Any]:
    if not body:
        return {"kind": "empty"}
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"kind": "opaque"}
    if not isinstance(payload, dict):
        return {"kind": "json", "root_type": _json_type(payload)}

    result: dict[str, Any] = {
        "kind": "json",
        "keys": [key if key in _SAFE_JSON_KEYS else "<other>" for key in payload],
        "types": [_json_type(value) for value in payload.values()],
    }
    messages = payload.get("messages")
    if isinstance(messages, list):
        message_shapes = []
        for message in messages:
            if not isinstance(message, dict):
                message_shapes.append({"kind": "other"})
                continue
            role = message.get("role")
            content = message.get("content")
            shape: dict[str, Any] = {
                "role": role if role in _SAFE_ROLES else "<other>",
                "content_type": _json_type(content),
            }
            blocks = _block_types(content)
            if blocks is not None:
                shape["block_types"] = blocks
            message_shapes.append(shape)
        result["messages"] = message_shapes
    system = payload.get("system")
    system_blocks = _block_types(system)
    if system_blocks is not None:
        result["system_block_types"] = system_blocks
    tools = payload.get("tools")
    if isinstance(tools, list):
        result["tool_count"] = len(tools)
    breakpoints = _cache_breakpoints(payload)
    if breakpoints is not None:
        result["cache_breakpoints"] = breakpoints
    return result


def _cache_breakpoints(payload: dict[str, Any]) -> list[str] | None:
    """Positions of every ``cache_control`` marker, as index-only labels.

    Indices carry no prompt text, so this stays safe to commit while still
    pinning the breakpoint layout against the official capture.
    """
    found: list[str] = []
    for section in ("tools", "system"):
        blocks = payload.get(section)
        if not isinstance(blocks, list):
            continue
        found.extend(f"{section}[{index}]" for index, block in enumerate(blocks)
                     if isinstance(block, dict) and block.get("cache_control"))
    messages = payload.get("messages")
    if isinstance(messages, list):
        for index, message in enumerate(messages):
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            found.extend(
                f"messages[{index}].content[{position}]"
                for position, block in enumerate(content)
                if isinstance(block, dict) and block.get("cache_control"))
    if not found and not any(
            isinstance(payload.get(section), list)
            for section in ("tools", "system", "messages")):
        return None
    return found


def _normalize_header_value(
    name: str,
    value: str,
    session_labels: dict[str, str],
) -> str:
    lower = name.lower()
    if not value or _CONTROL.search(value):
        raise UnsafeProfile(f"unsafe or empty header value: {name}")
    if lower in _REDACTED_HEADERS:
        return f"<redacted:{_REDACTED_HEADERS[lower]}>"
    if lower in {"x-claude-code-session-id", "x-mirasim-session"}:
        if len(value) > 256:
            raise UnsafeProfile(f"invalid {name}: session id is too long")
        label = session_labels.setdefault(value, f"session:{len(session_labels) + 1}")
        return f"<dynamic:{label}>"
    if lower == "x-mirasim-call":
        _uuid4(value, lower)
        return "<dynamic:uuid4>"
    if lower == "x-mirasim-ts":
        if not _UNIX_MS.fullmatch(value):
            raise UnsafeProfile("invalid x-mirasim-ts: expected 13-digit milliseconds")
        return "<dynamic:unix_ms>"
    if lower == "x-mirasim-nonce":
        _base64url_bytes(value, 12, lower)
        return "<dynamic:base64url_12b>"
    if lower == "x-mirasim-sig":
        _base64url_bytes(value, 64, lower)
        return "<dynamic:ed25519_signature>"
    if lower == "x-mirasim-device":
        if len(value) != 22 or not _BASE64URL.fullmatch(value):
            raise UnsafeProfile("invalid x-mirasim-device: expected 22 base64url characters")
        return "<dynamic:device_id>"
    if lower == "content-length":
        if not value.isascii() or not value.isdigit():
            raise UnsafeProfile("invalid content-length")
        return "<dynamic:content_length>"
    if len(value) > 16384:
        raise UnsafeProfile(f"header value is too long to profile safely: {name}")
    if _safe_literal_header_value(lower, value):
        return value
    # Header vocabularies evolve. Unknown names still matter for ordered-shape
    # comparisons, but their values are not trusted to be non-sensitive.
    return "<redacted:opaque>"


def _safe_query(query: str) -> str:
    """Preserve query ordering while redacting credential-like parameters."""
    if not query:
        return ""
    result = []
    for field in query.split("&"):
        raw_name, _separator, _raw_value = field.partition("=")
        decoded_name = unquote_plus(raw_name)
        if not decoded_name or _CONTROL.search(decoded_name):
            raise UnsafeProfile("unsafe or empty query parameter name")
        # The only observed public query field is this exact literal. Do not
        # treat the name alone as safe: ``beta=<token>`` must be redacted too.
        if field == "beta=true":
            result.append("beta=true")
        else:
            # Unknown parameter names can themselves contain user/account data,
            # so retain only their position, not their name or value.
            result.append(_REDACTED_QUERY_FIELD)
    return "&".join(result)


def _safe_path(path: str) -> str:
    """Keep fixed public endpoints while hiding dynamic or unknown paths."""
    if path in _SAFE_PATHS or path == _REDACTED_PATH:
        return path
    return _REDACTED_PATH


def request_profile(
    method: str,
    url: str,
    http_version: str,
    headers: Iterable[tuple[str | bytes, str | bytes]],
    body: bytes = b"",
) -> dict[str, Any]:
    """Return an ordered, deterministic, secret-free request summary."""
    parsed = urlsplit(url)
    safe_method = method.upper()
    if safe_method not in _SAFE_METHODS:
        raise UnsafeProfile("unsupported or unsafe HTTP method")
    if http_version not in _SAFE_HTTP_VERSIONS:
        raise UnsafeProfile("unsupported or unsafe HTTP version")
    session_labels: dict[str, str] = {}
    ordered = []
    for raw_name, raw_value in headers:
        name = raw_name.decode("latin1") if isinstance(raw_name, bytes) else str(raw_name)
        value = raw_value.decode("latin1") if isinstance(raw_value, bytes) else str(raw_value)
        if not name or _CONTROL.search(name):
            raise UnsafeProfile("unsafe or empty header name")
        if name.lower() not in _SAFE_HEADER_NAMES:
            # Header names are usually harmless protocol vocabulary, but
            # arbitrary extension names can themselves contain account data.
            # Keep their ordered position without retaining that input.
            ordered.append({
                "name": _REDACTED_HEADER_NAME,
                "value": "<redacted:opaque>",
            })
            continue
        ordered.append({
            "name": name,
            "value": _normalize_header_value(name, value, session_labels),
        })
    profile = {
        "schema": SCHEMA_VERSION,
        "method": safe_method,
        "path": _safe_path(parsed.path or "/"),
        "query": _safe_query(parsed.query),
        "http_version": http_version,
        "headers": ordered,
        "body": _body_shape(body),
    }
    validate_profile(profile)
    return profile


def validate_profile(profile: dict[str, Any]) -> None:
    """Reject profiles that do not conform to the secret-free schema."""
    required = {"schema", "method", "path", "query", "http_version", "headers", "body"}
    if set(profile) != required or profile.get("schema") != SCHEMA_VERSION:
        raise UnsafeProfile("invalid request-profile schema")
    if not all(isinstance(profile.get(name), str) for name in (
            "method", "path", "query", "http_version")) \
            or not isinstance(profile["headers"], list) \
            or not isinstance(profile["body"], dict):
        raise UnsafeProfile("invalid request-profile headers/body")
    if profile["method"] not in _SAFE_METHODS:
        raise UnsafeProfile("unsupported or unsafe HTTP method")
    if profile["http_version"] not in _SAFE_HTTP_VERSIONS:
        raise UnsafeProfile("unsupported or unsafe HTTP version")
    if _safe_path(profile["path"]) != profile["path"]:
        raise UnsafeProfile("path profile contains an unredacted value")
    if _safe_query(profile["query"]) != profile["query"]:
        raise UnsafeProfile("query profile contains an unredacted value")
    for entry in profile["headers"]:
        if not isinstance(entry, dict) or set(entry) != {"name", "value"}:
            raise UnsafeProfile("invalid request-profile header entry")
        name, value = entry.get("name"), entry.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise UnsafeProfile("request-profile headers must be strings")
        if not name or not value or _CONTROL.search(name) or _CONTROL.search(value):
            raise UnsafeProfile("unsafe or empty request-profile header")
        if name == _REDACTED_HEADER_NAME:
            if value != "<redacted:opaque>":
                raise UnsafeProfile("redacted header name has an invalid value")
            continue
        lower = name.lower()
        if lower not in _SAFE_HEADER_NAMES:
            raise UnsafeProfile("unknown header name was not redacted")
        if lower in _REDACTED_HEADERS \
                and value != f"<redacted:{_REDACTED_HEADERS[lower]}>":
            raise UnsafeProfile(f"sensitive header was not redacted: {name}")
        exact_dynamic = {
            "content-length": "<dynamic:content_length>",
            "x-mirasim-call": "<dynamic:uuid4>",
            "x-mirasim-device": "<dynamic:device_id>",
            "x-mirasim-nonce": "<dynamic:base64url_12b>",
            "x-mirasim-sig": "<dynamic:ed25519_signature>",
            "x-mirasim-ts": "<dynamic:unix_ms>",
        }
        if lower in exact_dynamic and value != exact_dynamic[lower]:
            raise UnsafeProfile(f"dynamic header has an invalid marker: {name}")
        if lower in {"x-claude-code-session-id", "x-mirasim-session"} \
                and not re.fullmatch(r"<dynamic:session:[1-9][0-9]*>", value):
            raise UnsafeProfile(f"session header has an invalid marker: {name}")
        classified = lower in _REDACTED_HEADERS or lower in exact_dynamic \
            or lower in {"x-claude-code-session-id", "x-mirasim-session"}
        if lower in _SAFE_HEADER_VALUE_NAMES:
            if value != "<redacted:opaque>" \
                    and not _safe_literal_header_value(lower, value):
                raise UnsafeProfile(f"unsafe literal header value: {name}")
        elif not classified and value != "<redacted:opaque>":
            raise UnsafeProfile(f"unknown header value was not redacted: {name}")
    allowed_body = {
        "cache_breakpoints", "kind", "keys", "messages", "root_type",
        "system_block_types", "tool_count", "types",
    }
    body = profile["body"]
    if not set(body).issubset(allowed_body):
        raise UnsafeProfile("body profile contains non-structural fields")
    kind = body.get("kind")
    if kind in {"empty", "opaque"}:
        if set(body) != {"kind"}:
            raise UnsafeProfile("empty/opaque body profiles cannot contain details")
        return
    if kind != "json":
        raise UnsafeProfile("invalid body-profile kind")
    if "root_type" in body:
        if set(body) != {"kind", "root_type"} or body["root_type"] not in {
            "array", "boolean", "null", "number", "string",
        }:
            raise UnsafeProfile("invalid scalar/array JSON body profile")
        return
    keys, types = body.get("keys"), body.get("types")
    if not isinstance(keys, list) or not all(
            isinstance(key, str) and key in _SAFE_JSON_KEYS | {"<other>"} for key in keys) \
            or not isinstance(types, list) or len(keys) != len(types) \
            or not all(item in {
                "array", "boolean", "null", "number", "object", "string", "unknown",
            } for item in types):
        raise UnsafeProfile("invalid JSON key/type profile")
    messages = body.get("messages")
    if messages is not None:
        if not isinstance(messages, list):
            raise UnsafeProfile("invalid message-shape profile")
        for message in messages:
            if not isinstance(message, dict):
                raise UnsafeProfile("invalid message-shape entry")
            if message == {"kind": "other"}:
                continue
            if set(message) not in ({"role", "content_type"},
                                    {"role", "content_type", "block_types"}):
                raise UnsafeProfile("message profile contains body data")
            if message["role"] not in _SAFE_ROLES | {"<other>"} \
                    or message["content_type"] not in {
                        "array", "boolean", "null", "number", "object", "string", "unknown",
                    }:
                raise UnsafeProfile("invalid message role/content type")
            blocks = message.get("block_types")
            if blocks is not None and (not isinstance(blocks, list) or not all(
                    item in _SAFE_BLOCK_TYPES | {"<other>"} for item in blocks)):
                raise UnsafeProfile("invalid message block-type profile")
    system_blocks = body.get("system_block_types")
    if system_blocks is not None and (not isinstance(system_blocks, list) or not all(
            item in _SAFE_BLOCK_TYPES | {"<other>"} for item in system_blocks)):
        raise UnsafeProfile("invalid system block-type profile")
    tool_count = body.get("tool_count")
    if tool_count is not None and (not isinstance(tool_count, int) or tool_count < 0):
        raise UnsafeProfile("invalid tool-count profile")
    breakpoints = body.get("cache_breakpoints")
    if breakpoints is not None and (not isinstance(breakpoints, list) or not all(
            isinstance(item, str) and _CACHE_BREAKPOINT.fullmatch(item)
            for item in breakpoints)):
        raise UnsafeProfile("invalid cache-breakpoint profile")


def compare_profiles(expected: Any, actual: Any) -> list[str]:
    """Return compact, path-addressed differences between two profiles."""
    differences: list[str] = []

    def walk(left: Any, right: Any, path: str) -> None:
        if type(left) is not type(right):
            differences.append(f"{path}: type {type(left).__name__} != {type(right).__name__}")
            return
        if isinstance(left, dict):
            if list(left) != list(right):
                differences.append(f"{path}: keys {list(left)!r} != {list(right)!r}")
            for key in left:
                if key in right:
                    walk(left[key], right[key], f"{path}.{key}")
            return
        if isinstance(left, list):
            if len(left) != len(right):
                differences.append(f"{path}: length {len(left)} != {len(right)}")
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                walk(left_item, right_item, f"{path}[{index}]")
            return
        if left != right:
            differences.append(f"{path}: {left!r} != {right!r}")

    walk(expected, actual, "$")
    return differences


def _load(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise UnsafeProfile("profile root must be an object")
    validate_profile(value)
    return value


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate a sanitized profile")
    validate.add_argument("profile")
    compare = commands.add_parser("compare", help="compare two sanitized profiles")
    compare.add_argument("expected")
    compare.add_argument("actual")
    args = parser.parse_args(argv)
    if args.command == "validate":
        _load(args.profile)
        return 0
    differences = compare_profiles(_load(args.expected), _load(args.actual))
    if differences:
        print("\n".join(differences))
        return 1
    return 0


class _MitmProfileAddon:
    """Duck-typed mitmproxy addon; importing this module needs no mitmproxy."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def _emit(self, flow: Any) -> None:
        request = getattr(flow, "request", None)
        if request is None or flow.id in self._seen:
            return
        host = (urlsplit(request.pretty_url).hostname or "").lower()
        if host != "mirasim.ai" and not host.endswith(".mirasim.ai"):
            return
        self._seen.add(flow.id)
        profile = request_profile(
            request.method,
            request.pretty_url,
            request.http_version,
            request.headers.fields,
            request.raw_content or b"",
        )
        print("REQUEST_PROFILE " + json.dumps(
            profile, ensure_ascii=False, separators=(",", ":")))

    def response(self, flow: Any) -> None:
        self._emit(flow)

    def error(self, flow: Any) -> None:
        self._emit(flow)


# Mitmdump imports this script after loading its own modules. Ordinary imports
# and the pytest suite never import mitmproxy or add it to project dependencies.
if "mitmproxy" in sys.modules:  # pragma: no cover - exercised with mitmdump
    addons = [_MitmProfileAddon()]


if __name__ == "__main__":
    raise SystemExit(_main())
