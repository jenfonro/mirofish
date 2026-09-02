"""Async upstream HTTP layer: auth endpoints, token refresh, model relay.

- One httpx.AsyncClient per proxy URL (connection pooling per exit).
- Token refresh is single-flight per alias so concurrent 401s do not stampede
  the refresh endpoint or clobber each other's rotated refresh token.
- /v1/messages and Codex /v1/responses support true streaming: successful
  upstream responses are handed to the caller unbuffered.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import ssl
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import httpx

from .config import Settings
from .device import DeviceSigner, uses_v2
from .errors import RelayError
from .seal import DEFAULT_SEAL_PUBLIC_KEY, seal_header_pairs
from .store import Store
from .wire import install_profile_header_order

DEVICE_SESSION_PATH = "/v1/device/session"
MESSAGES_PATH = "/v1/messages"
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
RESPONSES_PATH = "/v1/responses"
ALPHA_SEARCH_PATH = "/v1/alpha/search"
LIMITS_PATH = "/v1/limits"
#: Upstream endpoints the Codex agent reaches through the transparent MITM path.
CODEX_PATHS = (RESPONSES_PATH, ALPHA_SEARCH_PATH)


def _is_model_request_path(path: str) -> bool:
    """Whether ``path`` carries model work rather than control-plane data.

    The current desktop client requires a prepared device ticket before these
    routes are sent.  Keeping the predicate centralized prevents a new Codex
    alias from accidentally taking the account-token fallback path.
    """
    return path in (MESSAGES_PATH, COUNT_TOKENS_PATH, *CODEX_PATHS)

TICKET_REFRESH_LEAD_SECONDS = 120.0
TICKET_MINT_TIMEOUT_SECONDS = 10.0
TICKET_BACKOFF_BASE_SECONDS = 1.0
TICKET_BACKOFF_MAX_SECONDS = 30.0
TICKET_REFUSED_RETRY_SECONDS = 30.0
SIGNING_UNSUPPORTED_CACHE_SECONDS = 15.0 * 60.0
# Only used when the mint response omits an expiry.  The observed relay answers
# 900s, and guessing shorter than the real TTL just re-mints early.
DEFAULT_TICKET_LIFETIME_SECONDS = 15.0 * 60.0
#: Expiry fields seen across relay builds, in the order they are preferred.
#: ``*In`` values are durations; ``*At`` values are absolute unix seconds.
_TICKET_LIFETIME_FIELDS = ("expiresIn", "expires_in", "ttl", "ttlSeconds")
_TICKET_DEADLINE_FIELDS = ("expiresAt", "expires_at", "expiry", "exp")

# The current product relay only advertises Claude capacity when the request
# carries the short Agent SDK system marker emitted by official clients.  It
# returns a misleading model-unavailable 503 for an otherwise valid minimal
# Anthropic/OpenAI-compatible request.  Add only this routing marker for
# third-party Claude callers; official client payloads already contain it and
# remain structurally unchanged before canonical JSON serialization.
CLAUDE_AGENT_SYSTEM_MARKER = (
    "You are a Claude agent, built on Anthropic's Claude Agent SDK."
)

# The product relay preserves the Claude/Anthropic client fingerprint while
# replacing caller credentials and adding its own relay metadata/signature.
# Keep this list deliberately narrow so local proxy keys and cookies can never
# leak upstream.
_FORWARDED_MESSAGE_HEADERS = {
    "accept",
    "accept-encoding",
    "anthropic-beta",
    "anthropic-dangerous-direct-browser-access",
    "anthropic-version",
    "content-type",
    "user-agent",
    "x-app",
    "x-claude-code-session-id",
    # The Stainless SDK generator may add fields in a future release, and each
    # new name has to be added here deliberately.  Accepting the whole
    # ``x-stainless-*`` namespace by prefix would be less maintenance, but it
    # would also forward any future caller-supplied field in that namespace
    # upstream unread; the golden request profiles catch a dropped field, while
    # nothing would catch a forwarded secret.
    "x-stainless-arch",
    "x-stainless-lang",
    "x-stainless-os",
    "x-stainless-package-version",
    "x-stainless-retry-count",
    "x-stainless-runtime",
    "x-stainless-runtime-version",
    "x-stainless-timeout",
}
_DROPPED_ANTHROPIC_BETAS = {"oauth-2025-04-20"}
_MAX_FORWARDED_HEADER_VALUE = 8192

# Captured verbatim from an official client's /v1/messages request.  A
# third-party caller arrives with its own SDK identity or none at all, which
# leaves upstream looking at a fingerprint that matches no shipped client.
_CLI_USER_AGENT_PREFIX = "claude-cli/"
_CLI_ACCEPT_ENCODING = "gzip, deflate, br, zstd"
_CLI_CLAUDE_CODE_BETA = "claude-code-20250219"
_CLI_STAINLESS_FINGERPRINT: tuple[tuple[str, str], ...] = (
    ("x-stainless-arch", "arm64"),
    ("x-stainless-lang", "js"),
    ("x-stainless-os", "MacOS"),
    ("x-stainless-package-version", "0.112.1"),
    ("x-stainless-retry-count", "0"),
    ("x-stainless-runtime", "node"),
    ("x-stainless-runtime-version", "v26.3.0"),
    ("x-stainless-timeout", "600"),
)

logger = logging.getLogger("mirofish.upstream")

_HOP_BY_HOP_REQUEST_HEADERS = {
    "host", "connection", "content-length", "transfer-encoding",
    "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailer", "upgrade", "expect",
}
_LOCAL_REQUEST_HEADERS = {
    "authorization", "proxy-authorization", "x-api-key",
    "x-mirofish-account", "x-mirofish-proxy-key", "x-mirofish-session",
}
_BODY_INTEGRITY_REQUEST_HEADERS = {
    # Codex requests may arrive compressed.  The desktop signs and forwards
    # the decompressed bytes, so caller-provided digests describe the wrong
    # representation and must be removed together with Content-Encoding.
    "content-encoding", "content-md5", "content-digest", "digest",
}
# The relay owns its signing envelope and must never relay a caller's copy of
# it; see forwarded_codex_headers.
_MIRASIM_HEADER_PREFIX = "x-mirasim-"
_RELAY_OWNED_REQUEST_HEADERS = {"cookie", "cookie2"}
# Every observed official Codex request reaches the relay with this exact
# originator, so pin it rather than trusting whatever the local caller sends.
_CODEX_ORIGINATOR = "mirasim"
# Fields a stand-alone Codex CLI adds that the desktop's bundled Codex does
# not put on the wire (0.0.272 capture).  ``accept-encoding`` only affects
# what comes back, and the relay streams whatever the upstream sends either
# way; ``openai-beta`` was dropped by newer Codex builds.
_CODEX_DROPPED_REQUEST_HEADERS = frozenset({"accept-encoding", "openai-beta"})
_HOP_BY_HOP_RESPONSE_HEADERS = {
    "connection", "content-length", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
}


def _header_pairs(
        headers: Optional[Mapping[str, str] | Iterable[tuple[Any, Any]]],
) -> Iterable[tuple[str, str]]:
    if headers is None:
        return ()
    raw = getattr(headers, "raw", None)
    source = raw if raw is not None else (
        headers.items() if isinstance(headers, Mapping) else headers)

    def decoded() -> Iterable[tuple[str, str]]:
        for raw_name, raw_value in source:
            name = (raw_name.decode("latin1") if isinstance(raw_name, bytes)
                    else str(raw_name))
            value = (raw_value.decode("latin1") if isinstance(raw_value, bytes)
                     else str(raw_value))
            yield name, value
    return decoded()


def _forwarded_message_headers(
        headers: Optional[Mapping[str, str] | Iterable[tuple[Any, Any]]]
) -> list[tuple[str, str]]:
    """Copy only non-secret Claude SDK fingerprint headers, in wire order.

    The official client removes the obsolete OAuth beta token before relaying,
    but otherwise preserves the SDK's beta/version/user-agent metadata.  The
    allowlist is exact rather than accepting every ``x-stainless-*`` name so a
    future caller credential cannot accidentally become an upstream header.
    """
    forwarded: list[tuple[str, str]] = []
    positions: dict[str, int] = {}
    for raw_name, raw_value in _header_pairs(headers):
        name = str(raw_name).lower()
        if name not in _FORWARDED_MESSAGE_HEADERS:
            continue
        value = str(raw_value).strip()
        if not value or len(value) > _MAX_FORWARDED_HEADER_VALUE \
                or any(char in value for char in "\r\n\0"):
            continue
        if name == "anthropic-beta":
            betas = [item.strip() for item in value.split(",")
                     if item.strip() and item.strip() not in _DROPPED_ANTHROPIC_BETAS]
            if not betas:
                continue
            value = ",".join(betas)
        if name == "content-type" \
                and value.partition(";")[0].strip().lower() != "application/json":
            continue
        previous = positions.get(name)
        if previous is None:
            positions[name] = len(forwarded)
            forwarded.append((name, value))
        else:
            # Mapping implementations may expose a repeated header more than
            # once. Match the old last-value behavior without moving its first
            # position in the SDK fingerprint.
            forwarded[previous] = (name, value)
    return forwarded


def _place_before_authorization(
        headers: list[tuple[str, str]], name: str, value: str) -> None:
    """Assign ``name`` in place, or insert it just ahead of the credential.

    The bundled Codex emits ``originator``, ``user-agent`` and its cookie jar
    immediately before ``authorization``.  A caller that omits one of them
    would otherwise receive it *after* the credential slot, a header order the
    official client never produces.
    """
    lowered = name.lower()
    for index, (header_name, _) in enumerate(headers):
        if header_name.lower() == lowered:
            headers[index] = (header_name, value)
            return
    for index, (header_name, _) in enumerate(headers):
        if header_name.lower() == "authorization":
            headers.insert(index, (name, value))
            return
    headers.append((name, value))


def forwarded_codex_headers(
        headers: Optional[Mapping[str, str] | Iterable[tuple[Any, Any]]],
        user_agent: str = "",
) -> list[tuple[str, str]]:
    """Preserve Codex's evolving protocol headers while isolating local secrets.

    The desktop MITM uses a blocklist, not a fixed allowlist: Codex frequently
    adds routing and beta headers, and dropping a new one can change the request.
    Authentication and hop-by-hop fields are always rebuilt locally.  The
    caller's ``user-agent`` is replaced by ``user_agent`` when given: the
    desktop's bundled Codex identifies as the product, and a stand-alone
    ``codex_cli_rs/...`` string would name a client the upstream never sees.

    Two namespaces are refused outright even though the desktop forwards them.
    ``x-mirasim-*`` belongs to the relay's own signing envelope: the desktop
    assigns into Node's already-coalesced request-header object, where a caller
    cannot produce a duplicate, but a list-based rebuild would emit the caller's
    value *ahead* of the genuine one.  ``cookie`` is retained by the desktop
    only because it is a single-user process; this relay multiplexes accounts,
    so a caller's cookie would travel upstream attached to a different
    account's device ticket.
    """
    pairs = list(_header_pairs(headers))
    connection_fields = {
        item.strip().lower()
        for name, value in pairs if name.lower() == "connection"
        for item in value.split(",") if item.strip()
    }
    forwarded: list[tuple[str, str]] = []
    positions: dict[str, int] = {}
    for raw_name, raw_value in pairs:
        name = raw_name.lower()
        if name in {"authorization", "x-api-key"}:
            # Keep the first credential field's object-order slot without ever
            # retaining its value.  Assigning a replacement to an existing JS
            # object key does not move it; the desktop MITM has the same shape.
            if "authorization" not in positions:
                positions["authorization"] = len(forwarded)
                forwarded.append(("authorization", ""))
            continue
        if name in _HOP_BY_HOP_REQUEST_HEADERS or name in connection_fields \
                or name in _LOCAL_REQUEST_HEADERS \
                or name in _BODY_INTEGRITY_REQUEST_HEADERS \
                or name in _RELAY_OWNED_REQUEST_HEADERS \
                or name.startswith("x-forwarded-") \
                or name == "forwarded" or name.startswith("x-mirofish-") \
                or name.startswith(_MIRASIM_HEADER_PREFIX) \
                or name in _CODEX_DROPPED_REQUEST_HEADERS:
            continue
        value = raw_value.strip()
        if not value or len(value) > 16384 or any(char in value for char in "\r\n\0"):
            continue
        previous = positions.get(name)
        if previous is None:
            positions[name] = len(forwarded)
            forwarded.append((name, value))
        else:
            # Node's IncomingMessage.headers exposes the last/coalesced value at
            # the original field position.  Keep the same replacement behavior.
            forwarded[previous] = (name, value)
    content_type = next((value for name, value in forwarded
                         if name.lower() == "content-type"), "")
    if content_type.partition(";")[0].strip().lower() != "application/json":
        _set_ordered_header(forwarded, "content-type", "application/json")
    _place_before_authorization(forwarded, "originator", _CODEX_ORIGINATOR)
    if user_agent:
        _place_before_authorization(forwarded, "user-agent", user_agent)
    return forwarded


def _cookie_header(jar: httpx.Cookies, url: str) -> str:
    """Render the cookies ``jar`` would attach to a request for ``url``.

    ``http.cookiejar`` applies the usual RFC 6265 rules (host match, path,
    ``Secure``, expiry), so a Cloudflare cookie the upstream relays for another
    domain is not replayed, just as the bundled Codex's cookie store would not.
    """
    probe = httpx.Request("POST", url)
    jar.set_cookie_header(probe)
    return probe.headers.get("cookie", "")


def forwarded_response_headers(response: httpx.Response) -> list[tuple[str, str]]:
    """Return end-to-end upstream headers safe for an ASGI response.

    Repeated fields (notably Set-Cookie) remain repeated and in order.  Error
    bodies that were buffered for routing decisions have already been decoded
    by HTTPX, so their now-invalid Content-Encoding is omitted as well.
    """
    pairs = [(name.decode("latin1"), value.decode("latin1"))
             for name, value in response.headers.raw]
    connection_fields = {
        item.strip().lower()
        for name, value in pairs if name.lower() == "connection"
        for item in value.split(",") if item.strip()
    }
    blocked = {*_HOP_BY_HOP_RESPONSE_HEADERS, *connection_fields}
    if response.extensions.get("mirofish_body_decoded") is True:
        blocked.add("content-encoding")
    result: list[tuple[str, str]] = []
    for name, value in pairs:
        if name.lower() in blocked:
            continue
        if any(char in name or char in value for char in "\r\n\0"):
            continue
        result.append((name, value))
    return result


def _has_header(headers: Sequence[tuple[str, str]], name: str) -> bool:
    lowered = name.lower()
    return any(header_name.lower() == lowered for header_name, _ in headers)


def _is_cli_caller(headers: Sequence[tuple[str, str]]) -> bool:
    """True when the caller already presents an official Claude CLI identity."""
    return any(name.lower() == "user-agent"
               and value.lower().startswith(_CLI_USER_AGENT_PREFIX)
               for name, value in headers)


def _set_ordered_header(
        headers: list[tuple[str, str]], name: str, value: str) -> None:
    """Replace a header in place or append it without disturbing wire order."""
    lowered = name.lower()
    for index, (header_name, _) in enumerate(headers):
        if header_name.lower() == lowered:
            headers[index] = (header_name, value)
            return
    headers.append((name, value))


def _authority(url: str) -> str:
    """HTTP Host value, including a non-default port when present."""
    return httpx.URL(url).netloc.decode("ascii")


def _ticket_lifetime(data: Any) -> float:
    """Seconds a freshly minted device ticket remains valid.

    Relay builds have spelled the expiry several ways, and a numeric field may
    arrive as a JSON string.  Anything unparseable falls back to the default
    rather than pinning the ticket to a bogus deadline.
    """
    if not isinstance(data, dict):
        return DEFAULT_TICKET_LIFETIME_SECONDS
    for field in _TICKET_LIFETIME_FIELDS:
        value = _as_float(data.get(field))
        if value is not None and value > 0:
            return value
    for field in _TICKET_DEADLINE_FIELDS:
        value = _as_float(data.get(field))
        if value is not None:
            # Some builds report milliseconds; both scales are far outside each
            # other's plausible range, so the magnitude disambiguates them.
            if value > 1e11:
                value /= 1000.0
            remaining = value - time.time()
            if remaining > 0:
                return remaining
    return DEFAULT_TICKET_LIFETIME_SECONDS


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        candidate = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            candidate = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return candidate if math.isfinite(candidate) else None


def _relay_envelope(
        token: str, session_id: str, agent: str, account_id: str, call_id: str,
        device_id: str, client_version: str, locale: str,
        probe: bool) -> list[tuple[str, str]]:
    """The relay-owned request metadata, in the order the desktop emits it.

    Probe requests carry a deliberately reduced envelope: no session, agent,
    device, account, locale or call id, because a usage probe is not part of a
    conversation and the product does not attribute one.
    """
    if probe:
        envelope = [("x-mirasim-probe", "usage"),
                    ("authorization", "Bearer " + token)]
        if client_version:
            envelope.append(("x-mirasim-client", client_version))
        return envelope
    envelope = [("authorization", "Bearer " + token)]
    # The desktop assigns relay metadata only when its value is truthy.  In
    # normal model calls all three are present, but preserving that rule keeps
    # private/diagnostic callers from emitting empty signed fields.
    for name, value in (("x-mirasim-session", session_id),
                        ("x-mirasim-agent", agent),
                        ("x-mirasim-device", device_id)):
        if value:
            envelope.append((name, value))
    if account_id:
        envelope.append(("x-mirasim-account", account_id))
    if client_version:
        envelope.append(("x-mirasim-client", client_version))
    if locale:
        envelope.append(("x-mirasim-locale", locale))
    if call_id:
        envelope.append(("x-mirasim-call", call_id))
    return envelope


def _wire_tail(url: str, body: bytes = b"") -> list[tuple[str, str]]:
    tail: list[tuple[str, str]] = []
    if body:
        tail.append(("content-length", str(len(body))))
    tail.extend((("Host", _authority(url)), ("Connection", "keep-alive")))
    return tail


def _seal_model_headers(
        headers: Sequence[tuple[str, str]], method: str, path: str,
        settings: Settings) -> list[tuple[str, str]]:
    """Encrypt the generated relay envelope for a current Mirasim client.

    Sealing is deliberately fail-closed: a malformed/rotated public key must
    not silently turn the device, account, or signature metadata back into
    clear headers.  ``seal_header_pairs`` only receives relay-owned fields at
    this point; caller-provided ``x-mirasim-*`` values were removed by the
    forwarding filters before the envelope was assigned.
    """
    if not getattr(settings, "mirasim_seal_metadata", True):
        return list(headers)
    try:
        return seal_header_pairs(
            headers, method, path,
            getattr(settings, "mirasim_seal_public_key", DEFAULT_SEAL_PUBLIC_KEY),
        )
    except (TypeError, ValueError) as exc:
        # Keep the diagnostic useful for operators without echoing the public
        # key or any metadata values into a response/log payload.
        raise RelayError(
            "unable to seal Mirasim relay metadata", 502,
            {"reason": str(exc)[:200]},
        ) from exc


SIGNED_MODEL_REQUIRED_MESSAGE = "cloud relay requires a signed device session"


_SIGNATURE_IDENTITY_HEADERS = frozenset({
    "x-mirasim-device", "x-mirasim-ts", "x-mirasim-nonce",
    "x-mirasim-sig", "x-mirasim-client", "x-mirasim-enc",
})


def _signing_metadata(
        headers: Sequence[tuple[str, str]],
) -> dict[str, str]:
    """Extract the relay metadata covered by an mrs-sig-v2 signature.

    The official client passes ``XZe(headers)`` to its signer: only the
    ``x-mirasim-*`` namespace is considered, while the device/timestamp/nonce/
    signature fields and the clear client build marker are carried separately
    in the signing context.  Header assignment is last-value-wins, matching
    Node's coalesced request-header object.
    """
    result: dict[str, str] = {}
    for raw_name, raw_value in headers:
        name = str(raw_name).lower()
        value = str(raw_value)
        if (not name.startswith(_MIRASIM_HEADER_PREFIX)
                or name in _SIGNATURE_IDENTITY_HEADERS or not value):
            continue
        result[name] = value
    return result


def _rejection_detail(body: Any) -> str:
    """Upstream error type/message for logs; never includes our request content."""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return f"{error.get('type', 'error')}: {str(error.get('message', ''))[:500]}"
        if "_raw" in body:
            return "non-JSON body: " + str(body["_raw"])[:200]
    return str(body)[:300]


REGION_REFUSAL_TYPE = "shared_quota_unavailable"
CREDIT_EXHAUSTED_TYPE = "credit_exhausted_shared"


def _is_region_blocked(status: int, body: Any) -> bool:
    """The upstream refuses to serve requests from this exit's network region."""
    if status != 429 or not isinstance(body, dict):
        return False
    error = body.get("error")
    return (isinstance(error, dict)
            and str(error.get("type")) == REGION_REFUSAL_TYPE)


def account_scoped_429(status: int, body: Any) -> bool:
    """A 429 another account, rather than another proxy exit, can recover from.

    ``credit_exhausted_shared`` is the refusal the product documents, but a
    window that fills up can surface under other 429 types too, so every 429
    except the region refusal counts. The single definition is shared by all
    relay paths; account-level failover keys off it.
    """
    if status != 429:
        return False
    if not isinstance(body, dict):
        return True
    error = body.get("error")
    if not isinstance(error, dict):
        return True
    return str(error.get("type")) != REGION_REFUSAL_TYPE


def _region_block_error(status: int, body: Any,
                        proxy_url: Optional[str]) -> Optional[RelayError]:
    """Region availability is a property of the proxy node, not the account, so
    rotating to a node in a served region recovers; without a proxy there is
    nothing to rotate and the caller sees the upstream refusal as-is."""
    if not proxy_url or not _is_region_blocked(status, body):
        return None
    return RelayError("upstream does not serve this proxy exit region", 502,
                      {"region_blocked": True, "upstream": _rejection_detail(body)})


def _raise_if_region_blocked(alias: str, status: int, body: Any,
                             proxy_url: Optional[str]) -> None:
    """Turn an upstream region refusal into the pool's rotatable error shape."""
    blocked = _region_block_error(status, body, proxy_url)
    if blocked is None:
        return
    logger.warning("upstream refused exit region: account=%s %s",
                   alias, _rejection_detail(body))
    raise blocked


def _payload_summary(payload: dict[str, Any]) -> str:
    """Content-free request shape for rejection diagnostics.

    Only roles, block types, counts, and parameter values appear — never
    message text, tool arguments, or credentials."""
    def describe(content: Any) -> str:
        if isinstance(content, str):
            return f"text({len(content)}ch)" if content else "EMPTY"
        if not isinstance(content, list):
            return type(content).__name__
        if not content:
            return "EMPTY[]"
        kinds = []
        for block in content:
            if isinstance(block, dict):
                kind = str(block.get("type", "?"))
                if kind == "text":
                    kind += f"({len(str(block.get('text', '')))}ch)"
                kinds.append(kind)
            else:
                kinds.append(type(block).__name__)
        return "[" + ",".join(kinds) + "]"

    system = payload.get("system")
    if isinstance(system, list):
        system_chars = sum(len(str(block.get("text", "")))
                           for block in system if isinstance(block, dict))
    else:
        system_chars = len(system) if isinstance(system, str) else None
    fields: dict[str, Any] = {
        "model": payload.get("model"),
        "stream": payload.get("stream"),
        "max_tokens": payload.get("max_tokens"),
        "temperature": payload.get("temperature"),
        "top_p": payload.get("top_p"),
        "top_k": payload.get("top_k"),
        "system_chars": system_chars,
        "tools": len(payload["tools"]) if isinstance(payload.get("tools"), list) else None,
        "tool_choice": payload.get("tool_choice"),
        "stop_sequences": (len(payload["stop_sequences"])
                           if isinstance(payload.get("stop_sequences"), list) else None),
        "thinking": (payload.get("thinking") or {}).get("type")
                    if isinstance(payload.get("thinking"), dict) else None,
    }
    header = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    shape = [f"{message.get('role', '?')}:{describe(message.get('content'))}"
             for message in messages if isinstance(message, dict)]
    if len(shape) > 40:
        shape = shape[:40] + [f"...+{len(shape) - 40}"]
    return header + " messages=" + (" ".join(shape) or "NONE")


def quota_headers(headers: dict[str, str]) -> dict[str, Any]:
    return {"7d_utilization": headers.get("anthropic-ratelimit-unified-7d-utilization"),
            "7d_reset_epoch": headers.get("anthropic-ratelimit-unified-7d-reset")}


def _lower_headers(response: httpx.Response) -> dict[str, str]:
    return {key.lower(): value for key, value in response.headers.items()}


def _parse_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"_raw": response.text[:1000]}


def _json_bytes(payload: Optional[dict[str, Any]]) -> bytes:
    if payload is None:
        return b""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _claude_compatible_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a Claude-routable payload without mutating the caller's body.

    The marker is an independent system text block so an existing system
    instruction keeps its text and ordering relative to other caller blocks.
    Non-Claude models and official Claude payloads are returned as-is.
    """
    model = payload.get("model")
    if not isinstance(model, str) or not model.lower().startswith("claude-"):
        return payload

    system = payload.get("system")
    if isinstance(system, str):
        texts = [system]
    elif isinstance(system, list):
        texts = [block.get("text") for block in system
                 if isinstance(block, dict) and block.get("type") == "text"]
    elif system is None:
        texts = []
    else:
        # Preserve normal upstream validation for malformed system values.
        return payload
    if CLAUDE_AGENT_SYSTEM_MARKER in texts:
        return payload

    marker = {"type": "text", "text": CLAUDE_AGENT_SYSTEM_MARKER}
    if isinstance(system, str) and system:
        prepared_system: list[dict[str, Any]] = [
            marker, {"type": "text", "text": system},
        ]
    elif isinstance(system, list):
        prepared_system = [marker, *system]
    else:
        prepared_system = [marker]
    return {**payload, "system": prepared_system}


def _has_cache_control(block: Any) -> bool:
    return isinstance(block, dict) and bool(block.get("cache_control"))


def _carries_cache_control(payload: Mapping[str, Any]) -> bool:
    """True when the caller already placed prompt-cache breakpoints itself.

    Anthropic allows at most four breakpoints per request, so a caller that
    expressed any is left entirely alone rather than topped up past the limit.
    """
    tools = payload.get("tools")
    if isinstance(tools, list) and any(_has_cache_control(tool) for tool in tools):
        return True
    system = payload.get("system")
    if isinstance(system, list) and any(_has_cache_control(block) for block in system):
        return True
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        if _has_cache_control(message):
            return True
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) \
                and any(_has_cache_control(block) for block in content):
            return True
    return False


def _marked(block: Mapping[str, Any]) -> dict[str, Any]:
    return {**block, "cache_control": {"type": "ephemeral"}}


def _cached_user_turn(message: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Copy a user turn with its final content block marked, or None."""
    content = message.get("content")
    if isinstance(content, str):
        if not content:
            return None
        # Official clients always send structured user content.  Promoting the
        # shorthand string is semantically identical upstream and gives the
        # breakpoint a block to attach to.
        return {**message, "content": [_marked({"type": "text", "text": content})]}
    if not isinstance(content, list) or not content \
            or not isinstance(content[-1], dict) or "type" not in content[-1]:
        return None
    return {**message, "content": [*content[:-1], _marked(content[-1])]}


def _with_cache_breakpoints(payload: dict[str, Any]) -> dict[str, Any]:
    """Add the official client's prompt-cache breakpoints, copy-on-write.

    Captured official /v1/messages bodies carry exactly three ephemeral
    breakpoints: the Agent SDK marker system block, the final system block, and
    the final content block of the last user turn.  Tools are deliberately not
    marked even at 29 tools, so this does not mark them either.  Third-party
    Claude callers otherwise pay full input price on every turn of a
    conversation the relay is happy to serve from cache.
    """
    model = payload.get("model")
    if not isinstance(model, str) or not model.lower().startswith("claude-"):
        # cache_control is Anthropic-shaped; only Claude routes are captured
        # evidence that upstream honors it.
        return payload
    if _carries_cache_control(payload):
        return payload

    prepared = payload
    system = prepared.get("system")
    if isinstance(system, list) and system:
        # Only the first marker occurrence: a caller that repeated it must not
        # push the request past the four-breakpoint ceiling.
        marker = next((index for index, block in enumerate(system)
                       if isinstance(block, dict)
                       and block.get("text") == CLAUDE_AGENT_SYSTEM_MARKER), None)
        marks = {marker} if marker is not None else set()
        if isinstance(system[-1], dict) and system[-1].get("type") == "text":
            marks.add(len(system) - 1)
        if marks:
            prepared = {**prepared, "system": [
                _marked(block) if index in marks else block
                for index, block in enumerate(system)]}

    messages = prepared.get("messages")
    if isinstance(messages, list):
        last_user = next(
            (index for index in reversed(range(len(messages)))
             if isinstance(messages[index], dict)
             and messages[index].get("role") == "user"), None)
        if last_user is not None:
            turn = _cached_user_turn(messages[last_user])
            if turn is not None:
                prepared = {**prepared, "messages": [
                    turn if index == last_user else message
                    for index, message in enumerate(messages)]}
    return prepared


#: The elliptic-curve groups a BoringSSL-based client offers, in its order.
#: OpenSSL 3.5 instead leads with X25519MLKEM768 and appends the finite-field
#: ffdhe2048/ffdhe3072 groups, which no browser-derived client sends -- the
#: clearest stack tell in the ClientHello that is reachable from Python at all.
_BORINGSSL_GROUPS = "x25519:secp256r1:secp384r1"
_tls_context_cache: ssl.SSLContext | None = None


def tls_context() -> ssl.SSLContext:
    """Return the shared client TLS context, narrowed where Python allows it.

    Only the group list is adjusted, and only on interpreters exposing
    ``SSLContext.set_groups`` (3.13+); elsewhere this is httpx's own context
    unchanged.  Two things deliberately are *not* attempted:

    ALPN is left alone.  httpcore assigns ``http/1.1`` into whatever context it
    is handed, and the official client sends the extension too (with an empty
    protocol list), so both hellos carry extension 16 and a JA3 hash sees no
    difference.  Suppressing it would remove an extension the official client
    has and make the fingerprint *less* similar, not more.

    Nothing tries to reproduce the official JA3 exactly.  That hash covers the
    cipher list, extension order and GREASE values, all of which belong to the
    TLS library rather than to this code; matching it means replacing the TLS
    stack, not configuring OpenSSL.  Fidelity here is header- and body-level.
    """
    global _tls_context_cache
    if _tls_context_cache is None:
        context = httpx.create_ssl_context()
        set_groups = getattr(context, "set_groups", None)
        if set_groups is not None:
            try:
                set_groups(_BORINGSSL_GROUPS)
            except (ssl.SSLError, ValueError, OSError):
                # A build without one of these curves keeps its own defaults;
                # a narrower list is cosmetic, a failed handshake is not.
                logger.debug("keeping default TLS groups")
        _tls_context_cache = context
    return _tls_context_cache


@dataclass
class _DeviceTicket:
    value: str
    expires_at: float


@dataclass(frozen=True)
class _RelayCredential:
    value: str
    kind: str
    signed: bool


class Upstream:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self._clients: dict[tuple[str, str], httpx.AsyncClient] = {}
        self._clients_lock = asyncio.Lock()
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._ticket_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._ticket_cache: dict[tuple[str, str], _DeviceTicket] = {}
        self._ticket_retry_after: dict[tuple[str, str], float] = {}
        self._ticket_failures: dict[tuple[str, str], int] = {}
        self._signing_unsupported_until: dict[tuple[str, str], float] = {}
        # A route enters signed-limits mode after its first successful device
        # session. New process/route pairs begin with the captured account-token
        # limits profile and switch once model traffic mints a ticket.
        self._device_sessions: set[tuple[str, str]] = set()
        # The bundled Codex keeps a per-process cookie store and replays the
        # Cloudflare cookies the relay host sets (``__cflb``, ``_cfuvid``,
        # ``__cf_bm``).  One jar per (account, exit) mirrors one desktop
        # install per account; Node's fetch on the Claude path keeps none.
        self._cookie_jars: dict[tuple[str, str], httpx.Cookies] = {}
        self._device_signer: DeviceSigner | None = None
        # Monotonic per-alias epoch. In-flight ticket/refresh work may finish
        # after a re-login or deletion; only results from the current epoch may
        # write account-bound caches or credentials.
        self._credential_generations: dict[str, int] = {}

    async def aclose(self) -> None:
        async with self._clients_lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            await client.aclose()

    @staticmethod
    def _proxy_route(proxy_url: Optional[str]) -> tuple[str, str]:
        """Return the transport URL and its logical route identity.

        Mihomo switches several nodes behind one stable listener URL.  A
        keep-alive CONNECT tunnel was therefore previously reused after a node
        rotation, leaving retries on the refused old exit.  ``RoutedProxyURL``
        supplies the selector/node identity without changing the URL httpx
        receives; ordinary direct proxy strings retain the legacy URL key.
        """
        if not proxy_url:
            return "", ""
        return str(proxy_url), str(getattr(proxy_url, "route_identity", ""))

    async def client(self, proxy_url: Optional[str]) -> httpx.AsyncClient:
        transport_url, route_identity = self._proxy_route(proxy_url)
        key = (transport_url, route_identity)
        async with self._clients_lock:
            client = self._clients.get(key)
            if client is None:
                install_profile_header_order()
                client = httpx.AsyncClient(
                    proxy=transport_url or None, trust_env=False,
                    verify=tls_context(),
                    http2=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(self.settings.timeout, connect=10.0, pool=30.0),
                    limits=httpx.Limits(
                        max_connections=getattr(self.settings, "max_connections", 100),
                        max_keepalive_connections=getattr(
                            self.settings, "max_keepalive_connections", 20),
                        keepalive_expiry=getattr(
                            self.settings, "keepalive_expiry", 75.0),
                    ),
                )
                self._clients[key] = client
            return client

    async def send_explicit(
            self, method: str, url: str,
            headers: Sequence[tuple[str, str]], body: bytes = b"",
            proxy_url: Optional[str] = None, *, stream: bool = False,
            timeout: Optional[httpx.Timeout] = None) -> httpx.Response:
        """Send one explicitly profiled request without AsyncClient defaults.

        Callers own the entire header list, including Host, Connection, and
        Content-Length.  This is the common escape hatch for endpoint profiles:
        using ``build_request`` or ``request`` would merge the client's Accept,
        User-Agent, and Accept-Encoding values before the transport sees them.

        The sequence is also the wire order: ``mirofish.wire`` replaces h11's
        Host-first writer so ``Host`` and ``Connection`` go out last, where the
        official clients (and every capture-derived golden profile) put them.
        TLS remains OpenSSL's; see ``tls_context``.
        """
        request = httpx.Request(
            method,
            url,
            content=body,
            headers=headers,
            extensions={"timeout": timeout.as_dict()} if timeout else {},
        )
        client = await self.client(proxy_url)
        return await client.send(request, stream=stream)

    # --- generic JSON calls -------------------------------------------------

    async def json(self, method: str, base: str, path: str,
                   payload: Optional[dict[str, Any]] = None,
                   access: Optional[str] = None,
                   proxy_url: Optional[str] = None) -> tuple[int, dict[str, str], Any]:
        body = _json_bytes(payload)
        url = base.rstrip("/") + path
        headers: list[tuple[str, str]] = []
        if body:
            headers.append(("content-type", "application/json"))
        if path == LIMITS_PATH and access:
            # Before a device ticket exists the desktop performs the same lean
            # usage probe with the account token. Keep this profile available
            # to lifecycle callers without conflating it with signed limits.
            headers.extend((
                ("x-mirasim-probe", "usage"),
                ("Authorization", "Bearer " + access),
                ("x-mirasim-client", self.settings.mirasim_client_version),
            ))
        elif access:
            authorization_name = (
                "authorization" if path == "/auth/referral" else "Authorization")
            headers.append((authorization_name, "Bearer " + access))
        headers.append(("accept-encoding", "identity"))
        headers.extend(_wire_tail(url, body))
        try:
            response = await self.send_explicit(
                method, url, headers, body, proxy_url)
        except httpx.HTTPError as exc:
            raise RelayError("upstream network error", 502,
                             {"proxy_network": bool(proxy_url),
                              "reason": (str(exc) or type(exc).__name__)[:200]}) from exc
        data = _parse_body(response)
        blocked = _region_block_error(response.status_code, data, proxy_url)
        if blocked is not None:
            # Generic authenticated calls such as /me/tenant use this path too.
            # Preserve the rotatable marker so AppState can abandon the exit
            # instead of collapsing the upstream 429 into an opaque 502.
            logger.warning("upstream refused exit region: path=%s %s",
                           path, _rejection_detail(data))
            raise blocked
        return response.status_code, _lower_headers(response), data

    # --- token refresh (single-flight per alias) ------------------------------

    def _refresh_lock(self, alias: str) -> asyncio.Lock:
        lock = self._refresh_locks.get(alias)
        if lock is None:
            lock = self._refresh_locks.setdefault(alias, asyncio.Lock())
        return lock

    def _ticket_key(self, alias: str, proxy_url: Optional[str]) -> tuple[str, str]:
        transport_url, route_identity = self._proxy_route(proxy_url)
        return alias, route_identity or transport_url

    def _ticket_lock(self, alias: str, proxy_url: Optional[str]) -> asyncio.Lock:
        key = self._ticket_key(alias, proxy_url)
        lock = self._ticket_locks.get(key)
        if lock is None:
            lock = self._ticket_locks.setdefault(key, asyncio.Lock())
        return lock

    def _signer(self, alias: str = "") -> DeviceSigner:
        """Return the installation signer; ``alias`` is only a migration hint."""
        if self._device_signer is None:
            legacy = (alias,) if alias else ()
            self._device_signer = DeviceSigner(
                self.store, self.settings.mirasim_client_version, legacy)
        else:
            # Settings are mutable in the test harness and in long-lived
            # deployments that rotate the upstream client profile without
            # restarting the process.  The signer is installation-wide, but
            # its version marker is per active protocol profile.
            self._device_signer.set_client_version(
                self.settings.mirasim_client_version)
        return self._device_signer

    def ensure_device_identity(self, legacy_alias: str = "") -> str:
        """Persist/migrate the installation key before account data is removed."""
        return self._signer(legacy_alias).device_id

    def _advance_credentials(self, alias: str, *, clear_device: bool) -> None:
        self._credential_generations[alias] = (
            self._credential_generations.get(alias, 0) + 1)
        self._invalidate_ticket(alias)
        if clear_device:
            self._device_sessions = {
                key for key in self._device_sessions if key[0] != alias}
            for key in [key for key in self._cookie_jars if key[0] == alias]:
                del self._cookie_jars[key]

    def _cookie_jar(self, alias: str, proxy_url: Optional[str]) -> httpx.Cookies:
        key = self._ticket_key(alias, proxy_url)
        jar = self._cookie_jars.get(key)
        if jar is None:
            jar = self._cookie_jars[key] = httpx.Cookies()
        return jar

    def credentials_changed(self, alias: str) -> None:
        """Invalidate account-bound authorization after login credentials change."""
        self._advance_credentials(alias, clear_device=True)

    def forget_account(self, alias: str) -> None:
        """Forget account authorization without rotating the installation key."""
        self._advance_credentials(alias, clear_device=True)

    def has_device_session(
            self, alias: str, proxy_url: Optional[str] = None) -> bool:
        return self._ticket_key(alias, proxy_url) in self._device_sessions

    def _cli_identity_headers(
            self, forwarded: Sequence[tuple[str, str]],
            session_id: str) -> list[tuple[str, str]]:
        """Rebuild a non-CLI caller's headers as the captured official profile.

        Only ``anthropic-version`` and ``anthropic-beta`` survive from the
        caller: those change request semantics and are the caller's to choose.
        Every fingerprint slot is overwritten rather than merely defaulted, so
        a Python SDK's own ``x-stainless-lang: python`` cannot survive beside a
        ``js`` claim, and a caller's ``accept: text/event-stream`` cannot
        contradict a client that the capture shows always sends
        ``application/json``, streaming or not.  The session id matches
        ``x-mirasim-session`` exactly as the official client pairs them.
        """
        supplied = {name.lower(): value for name, value in forwarded}
        betas = [item for item in supplied.get("anthropic-beta", "").split(",")
                 if item]
        if _CLI_CLAUDE_CODE_BETA not in betas:
            # Pair the routing beta with the CLI identity.  The semantic betas
            # the official client also negotiates (context-1m,
            # interleaved-thinking, context-management) stay opt-in.
            betas.insert(0, _CLI_CLAUDE_CODE_BETA)
        headers: list[tuple[str, str]] = [
            ("accept", "application/json"),
            ("content-type", "application/json"),
            ("user-agent", self.settings.claude_cli_user_agent),
        ]
        if session_id:
            headers.append(("x-claude-code-session-id", session_id))
        headers.extend(_CLI_STAINLESS_FINGERPRINT)
        headers.extend((
            ("anthropic-beta", ",".join(betas)),
            ("anthropic-dangerous-direct-browser-access", "true"),
            ("anthropic-version",
             supplied.get("anthropic-version") or self.settings.anthropic_version),
            ("x-app", "cli"),
            ("accept-encoding", _CLI_ACCEPT_ENCODING),
        ))
        return headers

    def _message_request_headers(
            self, request_headers: Optional[
                Mapping[str, str] | Iterable[tuple[Any, Any]]],
            probe: bool, session_id: str = "",
            alias: str = "") -> list[tuple[str, str]]:
        headers = _forwarded_message_headers(request_headers)
        if not probe and not _is_cli_caller(headers):
            # Probes stay lean on purpose; every other caller gets the full
            # official fingerprint instead of a partial one.
            return self._cli_identity_headers(headers, session_id)
        if not _has_header(headers, "accept"):
            # Accept and anthropic-version are Messages protocol semantics,
            # not a fabricated Claude CLI/SDK fingerprint. Internal OpenAI
            # translation and model probes have no inbound Anthropic headers,
            # so retain the compatibility defaults used by the relay before
            # endpoint profiles became explicit.
            headers.insert(0, ("accept", "application/json"))
        if not _has_header(headers, "content-type"):
            # This is a JSON protocol header, not a fabricated Claude SDK
            # fingerprint. Put it after Accept when that header was supplied,
            # matching the captured official Messages profile.
            insert_at = next((index + 1 for index, (name, _) in enumerate(headers)
                              if name.lower() == "accept"), 0)
            headers.insert(insert_at, ("content-type", "application/json"))
        if not _has_header(headers, "anthropic-version"):
            headers.append(("anthropic-version", self.settings.anthropic_version))
        if probe:
            # The product's explicit usage probe is intentionally a lean
            # request and does not carry per-conversation relay metadata.
            _set_ordered_header(headers, "accept-encoding", "identity")
        return headers

    def _invalidate_ticket(self, alias: str) -> None:
        # Access refresh and a relay 401 invalidate every route-scoped ticket
        # for this account.  Tickets are cached per route because the upstream
        # may bind a short-lived device session to its source exit.
        stale = {key for mapping in (
            self._ticket_cache, self._ticket_retry_after, self._ticket_failures,
            self._signing_unsupported_until,
        ) for key in mapping if key[0] == alias}
        for key in stale:
            self._ticket_cache.pop(key, None)
            self._ticket_retry_after.pop(key, None)
            self._ticket_failures.pop(key, None)

    def _invalidate_route_ticket(
            self, alias: str, proxy_url: Optional[str], expected: str) -> None:
        """Drop only the ticket that actually produced a relay 401.

        Another waiter may already have refreshed the route while this response
        was in flight. Comparing values prevents a late 401 from deleting that
        newer ticket and causing a second device-session stampede.
        """
        key = self._ticket_key(alias, proxy_url)
        cached = self._ticket_cache.get(key)
        if cached is not None and cached.value == expected:
            self._ticket_cache.pop(key, None)
            self._ticket_retry_after.pop(key, None)
            self._ticket_failures.pop(key, None)
            self._signing_unsupported_until.pop(key, None)

    async def refresh_access(self, alias: str, stale_access: str,
                             proxy_url: Optional[str] = None) -> str:
        async with self._refresh_lock(alias):
            current = self.store.vault.get(alias, "access")
            if current != stale_access:
                # Another request already refreshed while we waited on the lock.
                return current
            refresh_token = self.store.vault.get(alias, "refresh")
            generation = self._credential_generations.get(alias, 0)
            status, _, data = await self.json(
                "POST", self.settings.auth_base, "/auth/refresh",
                {"refresh_token": refresh_token}, proxy_url=proxy_url)
            if status < 200 or status >= 300 or not isinstance(data, dict):
                raise RelayError("account refresh failed", 401, data)
            access = data.get("access_token")
            renewal = data.get("refresh_token")
            if not isinstance(access, str) or not access \
                    or not isinstance(renewal, str) or not renewal:
                raise RelayError("upstream refresh response is missing tokens", 502)
            # A re-login may have replaced both credentials while the refresh
            # request was in flight. Never let the stale response overwrite it.
            if generation != self._credential_generations.get(alias, 0) \
                    or self.store.vault.get(alias, "access") != stale_access \
                    or self.store.vault.get(alias, "refresh") != refresh_token:
                return self.store.vault.get(alias, "access")
            self.store.vault.put(alias, "refresh", renewal)
            # If the process exits between non-transactional credential writes,
            # retaining the old access token with the new refresh token is more
            # recoverable than retaining a rotated/invalid refresh token.
            self.store.vault.put(alias, "access", access)
            self._advance_credentials(alias, clear_device=False)
            return access

    async def authed_json(self, alias: str, method: str, base: str, path: str,
                          payload: Optional[dict[str, Any]] = None,
                          proxy_url: Optional[str] = None) -> tuple[int, dict[str, str], Any]:
        """JSON call with the account's token, refreshing once on 401."""
        access, _ = self.store.credentials(alias)
        status, headers, data = await self.json(method, base, path, payload,
                                                access=access, proxy_url=proxy_url)
        if status == 401:
            access = await self.refresh_access(alias, access, proxy_url)
            status, headers, data = await self.json(method, base, path, payload,
                                                    access=access, proxy_url=proxy_url)
        return status, headers, data

    async def limits(
            self, alias: str,
            proxy_url: Optional[str] = None) -> tuple[int, dict[str, str], Any]:
        """Use the captured startup or signed limits profile for this route.

        A fresh process/exit sends the account-token probe while establishing a
        device session in parallel. Once that succeeds, later polls use the
        route-scoped ticket and Ed25519 signature, including after ticket expiry.
        Device-session failure does not hide a successful zero-cost limits read.
        """
        if self.has_device_session(alias, proxy_url):
            return await self.signed_json(
                alias, "GET", LIMITS_PATH, proxy_url=proxy_url)

        initial = asyncio.create_task(self.authed_json(
            alias, "GET", self.settings.relay_base, LIMITS_PATH,
            proxy_url=proxy_url))
        device = asyncio.create_task(self._device_ticket(alias, proxy_url))
        initial_result, device_result = await asyncio.gather(
            initial, device, return_exceptions=True)
        if isinstance(initial_result, BaseException):
            raise initial_result
        if isinstance(device_result, BaseException):
            status = device_result.status if isinstance(device_result, RelayError) else 502
            logger.warning(
                "initial limits succeeded without device-session prewarm: "
                "account=%s status=%s", alias, status)
        return initial_result

    # --- model relay -------------------------------------------------------

    async def _mint_device_ticket(self, alias: str, access: str,
                                  proxy_url: Optional[str] = None) -> _DeviceTicket:
        """Exchange an account access token for a short-lived relay ticket."""
        signer = self._signer(alias)
        body = _json_bytes({"publicKey": signer.public_key,
                            "deviceId": signer.device_id})
        # The account access token authenticates the mint request and is part
        # of the v2 signature context (only its SHA-256 digest is signed).
        signature = signer.headers(
            "POST", DEVICE_SESSION_PATH, body, credential=access, metadata={})
        url = self.settings.relay_base + DEVICE_SESSION_PATH
        headers = [
            ("content-type", "application/json"),
            ("authorization", "Bearer " + access),
            ("x-mirasim-device", signature["x-mirasim-device"]),
            ("x-mirasim-ts", signature["x-mirasim-ts"]),
            ("x-mirasim-nonce", signature["x-mirasim-nonce"]),
            ("x-mirasim-sig", signature["x-mirasim-sig"]),
        ]
        if signature.get("x-mirasim-client"):
            headers.append(("x-mirasim-client", signature["x-mirasim-client"]))
        headers.append(("accept-encoding", "identity"))
        headers.extend(_wire_tail(url, body))
        try:
            response = await self.send_explicit(
                "POST", url, headers, body, proxy_url,
                timeout=httpx.Timeout(TICKET_MINT_TIMEOUT_SECONDS))
        except httpx.HTTPError as exc:
            raise RelayError("upstream network error", 502,
                             {"proxy_network": bool(proxy_url),
                              "reason": (str(exc) or type(exc).__name__)[:200]}) from exc
        data = _parse_body(response)
        if response.status_code < 200 or response.status_code >= 300:
            _raise_if_region_blocked(alias, response.status_code, data, proxy_url)
            raise RelayError("device session request rejected", response.status_code, data)
        ticket = data.get("ticket") if isinstance(data, dict) else None
        if not isinstance(ticket, str) or not ticket:
            raise RelayError("device session response is missing a ticket", 502, data)
        return _DeviceTicket(
            ticket, time.monotonic() + max(1.0, _ticket_lifetime(data)))

    def _note_ticket_failure(self, key: tuple[str, str], transient: bool) -> None:
        now = time.monotonic()
        if transient:
            failures = self._ticket_failures.get(key, 0)
            delay = min(
                TICKET_BACKOFF_BASE_SECONDS * (2 ** failures),
                TICKET_BACKOFF_MAX_SECONDS,
            )
            self._ticket_failures[key] = failures + 1
        else:
            delay = TICKET_REFUSED_RETRY_SECONDS
        self._ticket_retry_after[key] = now + delay

    @staticmethod
    def _mint_failure_is_rotatable(exc: RelayError) -> bool:
        return isinstance(exc.data, dict) and (
            exc.data.get("region_blocked") is True
            or exc.data.get("proxy_network") is True
        )

    def _ticket_fallback(
            self, key: tuple[str, str], alias: str,
            cached: _DeviceTicket | None, exc: RelayError) -> Optional[str]:
        """Record a mint failure and return a still-valid old ticket if possible."""
        if self._mint_failure_is_rotatable(exc):
            raise exc
        if exc.status in (404, 501):
            self._signing_unsupported_until[key] = (
                time.monotonic() + SIGNING_UNSUPPORTED_CACHE_SECONDS)
            logger.info(
                "device signing unsupported; using account token: account=%s status=%s",
                alias, exc.status)
        else:
            self._note_ticket_failure(key, transient=exc.status >= 500)
            logger.warning(
                "device session unavailable; temporarily using account token: "
                "account=%s status=%s", alias, exc.status)
        if cached is not None and time.monotonic() < cached.expires_at:
            return cached.value
        return None

    async def _device_ticket(self, alias: str,
                             proxy_url: Optional[str] = None) -> Optional[str]:
        key = self._ticket_key(alias, proxy_url)
        async with self._ticket_lock(alias, proxy_url):
            while True:
                now = time.monotonic()
                cached = self._ticket_cache.get(key)
                if cached and now < cached.expires_at - TICKET_REFRESH_LEAD_SECONDS:
                    return cached.value
                if now < self._signing_unsupported_until.get(key, 0.0) \
                        or now < self._ticket_retry_after.get(key, 0.0):
                    return (cached.value if cached and now < cached.expires_at else None)
                generation = self._credential_generations.get(alias, 0)
                access, _ = self.store.credentials(alias)
                ticket: _DeviceTicket | None = None
                for auth_attempt in range(2):
                    try:
                        ticket = await self._mint_device_ticket(alias, access, proxy_url)
                        break
                    except RelayError as exc:
                        # A stale account token can only be diagnosed by the
                        # session endpoint. Refresh it once; all other failures
                        # follow the desktop's plain-token fallback behavior.
                        if exc.status == 401 and auth_attempt == 0:
                            access = await self.refresh_access(alias, access, proxy_url)
                            generation = self._credential_generations.get(alias, 0)
                            continue
                        fallback = self._ticket_fallback(key, alias, cached, exc)
                        if generation != self._credential_generations.get(alias, 0):
                            break
                        return fallback
                if generation != self._credential_generations.get(alias, 0):
                    # Re-login/delete raced this request; discard its old ticket.
                    continue
                if ticket is None:
                    continue
                self._ticket_cache[key] = ticket
                self._device_sessions.add(key)
                self._ticket_retry_after.pop(key, None)
                self._ticket_failures.pop(key, None)
                self._signing_unsupported_until.pop(key, None)
                return ticket.value

    async def _relay_credential(
            self, alias: str, proxy_url: Optional[str], *,
            require_signed: bool = False) -> _RelayCredential:
        """Resolve the credential for one relay request.

        Control-plane calls may use the account bearer while a device session
        is being established.  A current-client model call is different: the
        upstream binds model traffic to a short-lived device ticket and
        rejects a plain account token.  More importantly, silently sending the
        account token here would turn a cloud-relay outage into an unexpected
        charge against the user's own account.  Legacy profiles retain the
        historical plain-token fallback explicitly by selecting a pre-v2
        ``mirasim_client_version``.
        """
        ticket = await self._device_ticket(alias, proxy_url)
        if ticket:
            return _RelayCredential(ticket, "ticket", True)
        if require_signed and uses_v2(self.settings.mirasim_client_version):
            key = self._ticket_key(alias, proxy_url)
            retry_after = max(
                self._signing_unsupported_until.get(key, 0.0),
                self._ticket_retry_after.get(key, 0.0),
            )
            detail: dict[str, Any] = {"kind": "device_session_required"}
            if retry_after > time.monotonic():
                # A relative duration is useful to callers but does not reveal
                # any credential or upstream response detail.
                detail["retry_after"] = max(1, int(retry_after - time.monotonic()))
            raise RelayError(SIGNED_MODEL_REQUIRED_MESSAGE, 503, detail)
        access, _ = self.store.credentials(alias)
        return _RelayCredential(access, "account", False)

    async def _signed_relay_response(self, alias: str, method: str, path: str,
                                     body: bytes, proxy_url: Optional[str],
                                     stream: bool = False,
                                     url_path: Optional[str] = None,
                                     extra_headers: Optional[
                                         Sequence[tuple[str, str]]] = None,
                                     session_id: str = "",
                                     call_id: str = "",
                                     probe: bool = False,
                                     agent: str = "claude",
                                     account_id: str = "",
    ) -> httpx.Response:
        model_request = _is_model_request_path(path)
        # Explicit usage probes are intentionally allowed to use the account
        # token: they are the zero-cost compatibility profile used while a
        # route is pre-warming its device session.  Real model traffic on the
        # current protocol is fail-closed when minting is unavailable.
        require_signed = model_request and not probe
        credential = await self._relay_credential(
            alias, proxy_url, require_signed=require_signed)
        url = self.settings.relay_base + (url_path or path)
        if credential.signed and httpx.URL(url).path != path:
            # A relay base carrying its own path prefix would make the signed
            # pathname disagree with the one upstream actually receives, and
            # every request would fail verification for a non-obvious reason.
            raise RelayError(
                "relay base must not add a URL path prefix", 500,
                {"signed_path": path})
        if model_request:
            # ``extra_headers`` is caller-derived, so every relay-owned field is
            # assigned rather than appended: the desktop mutates an already
            # coalesced header object, where assignment replaces in place and a
            # caller cannot end up with a second copy. Appending to a list would
            # emit a smuggled duplicate *ahead* of the genuine value, and which
            # copy upstream reads is not ours to decide.
            headers = list(extra_headers or ())
            for name, value in _relay_envelope(
                    credential.value, session_id, agent, account_id, call_id,
                    # The device id is derived from the Ed25519 public key and
                    # is the same value whether or not this request ends up
                    # signed.  Substituting an unrelated install UUID on the
                    # unsigned fallback path would change the field's shape
                    # (36-char UUID vs 22-char base64url) and identify the
                    # relay outright.
                    self._signer(alias).device_id,
                    self.settings.mirasim_client_version,
                    self.settings.mirasim_locale, probe):
                _set_ordered_header(headers, name, value)
            # The v2 signature covers the final relay metadata (before its
            # device/timestamp/nonce/signature fields are assigned).  Build the
            # envelope first so x-mirasim-session/agent/account/locale/call are
            # bound to the signature exactly as on the desktop client.
            signature = (self._signer(alias).headers(
                method, path, body, credential=credential.value,
                metadata=_signing_metadata(headers))
                         if credential.signed else None)
            if signature is not None:
                # Signing overwrites device/client in place, preserving each
                # metadata field's official header position.
                for name in ("x-mirasim-device", "x-mirasim-client",
                             "x-mirasim-ts", "x-mirasim-nonce", "x-mirasim-sig"):
                    value = signature.get(name)
                    if value:
                        _set_ordered_header(headers, name, value)
            # Starting with the 0.0.272 client, only the build marker remains
            # visible.  The complete relay envelope (including the signing
            # fields, when present) is sealed with the route's public key and
            # bound to this canonical pathname.  Explicit usage probes retain
            # their deliberately lean clear profile.
            if (not probe and credential.signed
                    and uses_v2(self.settings.mirasim_client_version)):
                headers = _seal_model_headers(headers, method, path, self.settings)
            headers.extend(_wire_tail(url, body))
        else:
            headers: list[tuple[str, str]] = []
            if body:
                headers.append(("content-type", "application/json"))
            if path == LIMITS_PATH:
                headers.append(("x-mirasim-probe", "usage"))
            # The desktop's usage probe reuses its Node request object, whose
            # ``Authorization`` key is capitalized; every other signed control
            # call in the 0.0.272 capture (``/v1/models``, ``/v1/model-roster``)
            # spells the field in lower case.
            headers.append((
                "Authorization" if path == LIMITS_PATH else "authorization",
                "Bearer " + credential.value))
            signature = (self._signer(alias).headers(
                method, path, body, credential=credential.value,
                metadata=_signing_metadata(headers))
                         if credential.signed else None)
            if signature is not None:
                headers.extend((
                    ("x-mirasim-device", signature["x-mirasim-device"]),
                    ("x-mirasim-ts", signature["x-mirasim-ts"]),
                    ("x-mirasim-nonce", signature["x-mirasim-nonce"]),
                    ("x-mirasim-sig", signature["x-mirasim-sig"]),
                ))
            headers.extend((
                ("x-mirasim-client", self.settings.mirasim_client_version),
                ("accept-encoding", "identity"),
                *_wire_tail(url, body),
            ))
        try:
            timeout = None
            if stream:
                timeout = httpx.Timeout(
                    connect=10.0,
                    read=getattr(self.settings, "stream_read_timeout", 600.0),
                    write=30.0,
                    pool=30.0,
                )
            response = await self.send_explicit(
                method, url, headers, body, proxy_url,
                stream=stream, timeout=timeout)
            response.extensions["mirofish_device_ticket"] = (
                credential.value if credential.kind == "ticket" else "")
            response.extensions["mirofish_relay_credential"] = credential.value
            response.extensions["mirofish_relay_credential_kind"] = credential.kind
            return response
        except httpx.HTTPError as exc:
            raise RelayError("relay network error", 502,
                             {"proxy_network": bool(proxy_url),
                              "reason": (str(exc) or type(exc).__name__)[:200]}) from exc

    async def _retry_relay_401(
            self, alias: str, proxy_url: Optional[str],
            response: httpx.Response) -> None:
        """Invalidate exactly the credential that produced a relay 401.

        Signed requests remint their route ticket.  In plain-token fallback
        mode, the account access token itself is refreshed once instead.
        """
        await response.aread()
        await response.aclose()
        kind = str(response.extensions.get(
            "mirofish_relay_credential_kind", ""))
        credential = str(response.extensions.get(
            "mirofish_relay_credential", ""))
        if kind == "ticket":
            self._invalidate_route_ticket(alias, proxy_url, credential)
            return
        if kind == "account" and credential:
            await self.refresh_access(alias, credential, proxy_url)
            return
        raise RelayError("relay rejected an unknown credential", 401)

    async def signed_json(self, alias: str, method: str, path: str,
                          payload: Optional[dict[str, Any]] = None,
                          proxy_url: Optional[str] = None, *,
                          request_headers: Optional[
                              Mapping[str, str] | Iterable[tuple[Any, Any]]] = None,
                          session_id: str = "", beta: bool = False,
                          probe: bool = False) -> tuple[int, dict[str, str], Any]:
        """Call a relay control/model endpoint using device auth."""
        request_body = _json_bytes(payload)
        url_path = path
        extra_headers: Optional[list[tuple[str, str]]] = None
        relay_session = session_id
        model_call = path in (MESSAGES_PATH, COUNT_TOKENS_PATH)
        if model_call:
            relay_session = session_id or str(uuid.uuid4())
            extra_headers = self._message_request_headers(
                request_headers, probe, relay_session, alias)
            if beta:
                url_path += "?beta=true"
        for attempt in range(2):
            response = await self._signed_relay_response(
                alias, method, path, request_body, proxy_url,
                url_path=url_path,
                extra_headers=extra_headers, session_id=relay_session,
                # x-mirasim-call identifies one HTTP request, not one logical
                # call: a credential-refresh retry is a second request and gets
                # its own id, as the session id stays put across both.
                call_id=str(uuid.uuid4()) if model_call else "", probe=probe)
            if response.status_code == 401 and attempt == 0:
                await self._retry_relay_401(alias, proxy_url, response)
                continue
            data = _parse_body(response)
            headers = _lower_headers(response)
            await response.aclose()
            _raise_if_region_blocked(alias, response.status_code, data, proxy_url)
            return response.status_code, headers, data
        raise RelayError("signed relay request failed after ticket refresh", 401)

    async def messages(self, alias: str, payload: dict[str, Any],
                       proxy_url: Optional[str] = None, *,
                       request_headers: Optional[
                           Mapping[str, str] | Iterable[tuple[Any, Any]]] = None,
                       session_id: str = "", beta: bool = False,
                       probe: bool = False,
                       raw_body: Optional[bytes] = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Buffered (non-stream) Messages call with ticket/signature retry."""
        original = payload
        payload = _with_cache_breakpoints(_claude_compatible_payload(payload))
        request_body = (raw_body if raw_body is not None and payload is original
                        else _json_bytes(payload))
        url_path = MESSAGES_PATH + ("?beta=true" if beta else "")
        relay_session = session_id or str(uuid.uuid4())
        extra_headers = self._message_request_headers(
            request_headers, probe, relay_session, alias)
        for attempt in range(2):
            response = await self._signed_relay_response(
                alias, "POST", MESSAGES_PATH, request_body, proxy_url,
                url_path=url_path,
                extra_headers=extra_headers, session_id=relay_session,
                call_id=str(uuid.uuid4()), probe=probe)
            if response.status_code == 401 and attempt == 0:
                await self._retry_relay_401(alias, proxy_url, response)
                continue
            response_body = _parse_body(response)
            headers = _lower_headers(response)
            await response.aclose()
            if response.status_code >= 400:
                _raise_if_region_blocked(
                    alias, response.status_code, response_body, proxy_url)
                logger.warning(
                    "upstream rejected /v1/messages: account=%s status=%s %s | %s",
                    alias, response.status_code, _rejection_detail(response_body),
                    _payload_summary(payload))
                raise RelayError("model request rejected", response.status_code, response_body)
            return response_body, headers
        raise RelayError("model request failed after ticket refresh", 401)

    async def stream_messages(self, alias: str, payload: dict[str, Any],
                              proxy_url: Optional[str] = None, *,
                              request_headers: Optional[
                                  Mapping[str, str] | Iterable[tuple[Any, Any]]] = None,
                              session_id: str = "", beta: bool = False,
                              probe: bool = False,
                              raw_body: Optional[bytes] = None,
    ) -> httpx.Response:
        """Open a streaming Anthropic Messages call; caller must aclose() it.

        Returns after upstream status/headers are known, so proxy rotation can
        still happen on connect failure; the body streams afterwards.
        """
        original = payload
        payload = _with_cache_breakpoints(_claude_compatible_payload(payload))
        request_body = (raw_body if raw_body is not None and payload is original
                        else _json_bytes(payload))
        url_path = MESSAGES_PATH + ("?beta=true" if beta else "")
        relay_session = session_id or str(uuid.uuid4())
        extra_headers = self._message_request_headers(
            request_headers, probe, relay_session, alias)
        for attempt in range(2):
            response = await self._signed_relay_response(
                alias, "POST", MESSAGES_PATH, request_body, proxy_url,
                stream=True, url_path=url_path,
                extra_headers=extra_headers, session_id=relay_session,
                call_id=str(uuid.uuid4()), probe=probe)
            if response.status_code == 401 and attempt == 0:
                await self._retry_relay_401(alias, proxy_url, response)
                continue
            if response.status_code >= 400:
                await response.aread()
                response_body = _parse_body(response)
                await response.aclose()
                _raise_if_region_blocked(
                    alias, response.status_code, response_body, proxy_url)
                logger.warning(
                    "upstream rejected /v1/messages (stream): account=%s status=%s %s | %s",
                    alias, response.status_code, _rejection_detail(response_body),
                    _payload_summary(payload))
                raise RelayError("model request rejected", response.status_code, response_body)
            return response
        raise RelayError("model request failed after ticket refresh", 401)

    async def stream_responses(
            self, alias: str, body: bytes,
            proxy_url: Optional[str] = None, *,
            request_headers: Optional[
                Mapping[str, str] | Iterable[tuple[Any, Any]]] = None,
            session_id: str = "", account_id: str = "",
            query_string: str = "", path: str = RESPONSES_PATH,
    ) -> httpx.Response:
        """Open a Codex relay call without rebuilding its JSON body.

        Several local paths collapse onto each upstream endpoint.  The query is
        retained on the request URL but deliberately excluded from the signing
        pathname, matching the desktop MITM's canonicalization.
        """
        if path not in CODEX_PATHS:
            raise RelayError("unsupported codex endpoint", 404)
        url_path = path + ("?" + query_string if query_string else "")
        relay_session = session_id or str(uuid.uuid4())
        jar = self._cookie_jar(alias, proxy_url)
        for attempt in range(2):
            extra_headers = forwarded_codex_headers(
                request_headers, self.settings.codex_user_agent)
            # Replay this account's Cloudflare cookies exactly where the bundled
            # Codex's cookie store puts them: after user-agent, before the
            # credential.  A caller's own cookie never survives (see
            # forwarded_codex_headers); only what this route was served counts.
            cookie = _cookie_header(jar, self.settings.relay_base + url_path)
            if cookie:
                _place_before_authorization(extra_headers, "cookie", cookie)
            response = await self._signed_relay_response(
                alias, "POST", path, body, proxy_url,
                stream=True, url_path=url_path, extra_headers=extra_headers,
                session_id=relay_session, call_id=str(uuid.uuid4()),
                agent="codex", account_id=account_id)
            if getattr(response, "_request", None) is not None:
                jar.extract_cookies(response)
            if response.status_code == 401 and attempt == 0:
                await self._retry_relay_401(alias, proxy_url, response)
                continue
            if response.status_code >= 400:
                # Buffer only rejected responses so routing/account decisions
                # can inspect their small JSON envelope.  Successful Responses
                # streams remain byte-for-byte passthroughs.
                await response.aread()
                response.extensions["mirofish_body_decoded"] = True
                response_body = _parse_body(response)
                try:
                    _raise_if_region_blocked(
                        alias, response.status_code, response_body, proxy_url)
                    if account_scoped_429(response.status_code, response_body):
                        raise RelayError(
                            "model request rejected", response.status_code,
                            response_body)
                except BaseException:
                    await response.aclose()
                    raise
                logger.warning(
                    "upstream rejected %s: account=%s status=%s %s",
                    path, alias, response.status_code,
                    _rejection_detail(response_body))
                # Non-429 protocol errors such as unsupported_model belong to
                # the Codex caller. Preserve their status, body, and end-to-end
                # headers instead of translating them into our error schema.
            return response
        raise RelayError("model request failed after credential refresh", 401)
