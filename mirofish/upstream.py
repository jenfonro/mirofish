"""Async upstream HTTP layer: auth endpoints, token refresh, model relay.

- One httpx.AsyncClient per proxy URL (connection pooling per exit).
- Token refresh is single-flight per alias so concurrent 401s do not stampede
  the refresh endpoint or clobber each other's rotated refresh token.
- /v1/messages supports true streaming: the upstream SSE response is handed to
  the caller unbuffered.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import httpx

from .config import Settings
from .device import DeviceSigner
from .errors import RelayError
from .store import Store

DEVICE_SESSION_PATH = "/v1/device/session"
MESSAGES_PATH = "/v1/messages"
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
LIMITS_PATH = "/v1/limits"

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

# arch/os is the one fingerprint slot that is a property of the machine rather
# than of the client build.  Emitting the captured pair for every account would
# present a whole set of subscriptions as one workstation — the correlation the
# per-account device key and per-account proxy exit exist to avoid — so each
# alias picks a pair and keeps it.
#
# The general rule for whether a fingerprint field may vary per account: only
# when upstream has no independent way to cross-check it.  A CPU/OS claim cannot
# be verified over a TLS connection, so diversifying it is free.  The two
# neighbouring fields that stay shared on purpose fail that test and are held
# constant deliberately (see _signed_relay_response for the locale note):
#   - x-mirasim-locale is checkable against the exit IP's geography, so a
#     per-alias value would manufacture IP<->locale mismatches — a sharper tell
#     than the shared value it replaced.  With a zh-HK default the pool is an
#     Asia one, where a shared Asian locale is what independent users send.
#   - x-mirasim-client / x-stainless-runtime-version are real software versions
#     upstream knows; a fabricated one is a never-shipped build, and real user
#     populations genuinely cluster on a current version.
#
# arch and os move together as a unit because they are not independent: sampling
# each on its own yields machines that were never shipped (arm64 Windows
# desktops, say), and an impossible fingerprint is more distinctive than a
# repeated one.  Every pair here is an ordinary Node desktop target; the
# captured pair leads.
_CLI_MACHINE_PROFILES: tuple[tuple[str, str], ...] = (
    ("arm64", "MacOS"),
    ("x64", "MacOS"),
    ("arm64", "Linux"),
    ("x64", "Linux"),
    ("x64", "Windows"),
)
_MACHINE_PROFILE_DOMAIN = b"mirofish/cli-machine-profile\0"

logger = logging.getLogger("mirofish.upstream")


def _forwarded_message_headers(
        headers: Optional[Mapping[str, str]]) -> list[tuple[str, str]]:
    """Copy only non-secret Claude SDK fingerprint headers, in wire order.

    The official client removes the obsolete OAuth beta token before relaying,
    but otherwise preserves the SDK's beta/version/user-agent metadata.  The
    allowlist is exact rather than accepting every ``x-stainless-*`` name so a
    future caller credential cannot accidentally become an upstream header.
    """
    forwarded: list[tuple[str, str]] = []
    positions: dict[str, int] = {}
    for raw_name, raw_value in (headers or {}).items():
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


def _has_header(headers: Sequence[tuple[str, str]], name: str) -> bool:
    lowered = name.lower()
    return any(header_name.lower() == lowered for header_name, _ in headers)


def _is_cli_caller(headers: Sequence[tuple[str, str]]) -> bool:
    """True when the caller already presents an official Claude CLI identity."""
    return any(name.lower() == "user-agent"
               and value.lower().startswith(_CLI_USER_AGENT_PREFIX)
               for name, value in headers)


def _machine_profile(alias: str) -> tuple[str, str]:
    """The (arch, os) pair this account presents, stable for its lifetime.

    Derived from the alias so it survives restarts with no stored state and
    never depends on which other accounts exist: a fingerprint that moved
    between requests would describe a user who changes computer mid-conversation,
    which is worse than sharing one with another account.  The Ed25519 device id
    would be the more natural seed, but it is loaded asynchronously from the
    vault, after these headers are built.

    Only arch and os vary.  ``x-stainless-runtime-version`` stays at the single
    value a capture attests, because a real population running one current Node
    across several platforms is unremarkable while an invented patch number is
    a fact upstream can check and this relay cannot.
    """
    digest = hashlib.sha256(
        _MACHINE_PROFILE_DOMAIN + alias.encode("utf-8")).digest()
    index = int.from_bytes(digest[:8], "big") % len(_CLI_MACHINE_PROFILES)
    return _CLI_MACHINE_PROFILES[index]


def _set_ordered_header(
        headers: list[tuple[str, str]], name: str, value: str) -> None:
    """Replace a header in place or append it without disturbing wire order."""
    lowered = name.lower()
    for index, (header_name, _) in enumerate(headers):
        if header_name.lower() == lowered:
            headers[index] = (header_name, value)
            return
    headers.append((name, value))


def _apply_machine_profile(
        headers: list[tuple[str, str]], alias: str) -> None:
    """Rewrite an existing arch/os pair as this account's machine identity.

    One Claude CLI relaying several accounts sends its own real fingerprint on
    every one of them, so without this the accounts differ in device key and
    exit IP while still reporting a single shared workstation.

    Absent headers stay absent.  A probe that deliberately sends no fingerprint
    must not acquire two thirds of one, and only the machine slot is touched:
    the caller's own runtime and version remain its to report.
    """
    arch, os_name = _machine_profile(alias)
    for name, value in (("x-stainless-arch", arch), ("x-stainless-os", os_name)):
        if _has_header(headers, name):
            _set_ordered_header(headers, name, value)


def _authority(url: str) -> str:
    """HTTP Host value, including a non-default port when present."""
    return httpx.URL(url).netloc.decode("ascii")


def _wire_tail(url: str, body: bytes = b"") -> list[tuple[str, str]]:
    tail: list[tuple[str, str]] = []
    if body:
        tail.append(("content-length", str(len(body))))
    tail.extend((("Host", _authority(url)), ("Connection", "keep-alive")))
    return tail


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


def _is_region_blocked(status: int, body: Any) -> bool:
    """The upstream refuses to serve requests from this exit's network region."""
    if status != 429 or not isinstance(body, dict):
        return False
    error = body.get("error")
    return (isinstance(error, dict)
            and str(error.get("type")) == REGION_REFUSAL_TYPE)


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


@dataclass
class _DeviceTicket:
    value: str
    expires_at: float


class Upstream:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self._clients: dict[tuple[str, str], httpx.AsyncClient] = {}
        self._clients_lock = asyncio.Lock()
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._ticket_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._ticket_cache: dict[tuple[str, str], _DeviceTicket] = {}
        # A route enters signed-limits mode after its first successful device
        # session. New process/route pairs begin with the captured account-token
        # limits profile and switch once model traffic mints a ticket.
        self._device_sessions: set[tuple[str, str]] = set()
        self._signers: dict[str, DeviceSigner] = {}
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
                client = httpx.AsyncClient(
                    proxy=transport_url or None, trust_env=False,
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

        The sequence is the HTTPX request-model order. Its HTTP/1.1 serializer
        (h11) emits Host first as recommended by RFC 7230, while preserving the
        relative order of the remaining fields. Golden comparisons therefore
        validate profile construction, not a claim of byte-identical wire/TLS
        fingerprinting.
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

    def _signer(self, alias: str) -> DeviceSigner:
        signer = self._signers.get(alias)
        if signer is None:
            signer = DeviceSigner(self.store, alias, self.settings.mirasim_client_version)
            self._signers[alias] = signer
        return signer

    def _advance_credentials(self, alias: str, *, clear_device: bool) -> None:
        self._credential_generations[alias] = (
            self._credential_generations.get(alias, 0) + 1)
        self._invalidate_ticket(alias)
        if clear_device:
            self._device_sessions = {
                key for key in self._device_sessions if key[0] != alias}

    def credentials_changed(self, alias: str) -> None:
        """Invalidate account-bound authorization after login credentials change."""
        self._advance_credentials(alias, clear_device=True)

    def forget_account(self, alias: str) -> None:
        """Forget authorization and in-memory device identity after deletion."""
        self._advance_credentials(alias, clear_device=True)
        self._signers.pop(alias, None)

    def has_device_session(
            self, alias: str, proxy_url: Optional[str] = None) -> bool:
        return self._ticket_key(alias, proxy_url) in self._device_sessions

    def _cli_identity_headers(
            self, forwarded: Sequence[tuple[str, str]],
            session_id: str, alias: str) -> list[tuple[str, str]]:
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
        _apply_machine_profile(headers, alias)
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
            self, request_headers: Optional[Mapping[str, str]],
            probe: bool, session_id: str = "",
            alias: str = "") -> list[tuple[str, str]]:
        headers = _forwarded_message_headers(request_headers)
        if not probe and not _is_cli_caller(headers):
            # Probes stay lean on purpose; every other caller gets the full
            # official fingerprint instead of a partial one.
            return self._cli_identity_headers(headers, session_id, alias)
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
        else:
            # A real CLI reached us with its own machine identity. It is one
            # client in front of several accounts, so the machine it reports
            # becomes the account's rather than its own.
            _apply_machine_profile(headers, alias)
        return headers

    def _invalidate_ticket(self, alias: str) -> None:
        # Access refresh and a relay 401 invalidate every route-scoped ticket
        # for this account.  Tickets are cached per route because the upstream
        # may bind a short-lived device session to its source exit.
        stale = [key for key in self._ticket_cache if key[0] == alias]
        for key in stale:
            self._ticket_cache.pop(key, None)

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
        signature = signer.headers("POST", DEVICE_SESSION_PATH, body)
        url = self.settings.relay_base + DEVICE_SESSION_PATH
        headers = [
            ("content-type", "application/json"),
            ("authorization", "Bearer " + access),
            ("x-mirasim-device", signature["x-mirasim-device"]),
            ("x-mirasim-ts", signature["x-mirasim-ts"]),
            ("x-mirasim-nonce", signature["x-mirasim-nonce"]),
            ("x-mirasim-sig", signature["x-mirasim-sig"]),
            ("x-mirasim-client", signature["x-mirasim-client"]),
            ("accept-encoding", "identity"),
            *_wire_tail(url, body),
        ]
        try:
            response = await self.send_explicit(
                "POST", url, headers, body, proxy_url)
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
        try:
            lifetime = float(data.get("expiresIn", 900)) if isinstance(data, dict) else 900.0
        except (TypeError, ValueError):
            lifetime = 900.0
        return _DeviceTicket(ticket, time.monotonic() + max(60.0, lifetime))

    async def _device_ticket(self, alias: str,
                             proxy_url: Optional[str] = None) -> str:
        key = self._ticket_key(alias, proxy_url)
        async with self._ticket_lock(alias, proxy_url):
            while True:
                cached = self._ticket_cache.get(key)
                if cached and time.monotonic() < cached.expires_at - 60.0:
                    return cached.value
                generation = self._credential_generations.get(alias, 0)
                access, _ = self.store.credentials(alias)
                try:
                    ticket = await self._mint_device_ticket(alias, access, proxy_url)
                except RelayError as exc:
                    # A stale access token can only be diagnosed by the session
                    # endpoint. Refresh once, then mint with the new generation.
                    if exc.status != 401:
                        raise
                    access = await self.refresh_access(alias, access, proxy_url)
                    generation = self._credential_generations.get(alias, 0)
                    ticket = await self._mint_device_ticket(alias, access, proxy_url)
                if generation != self._credential_generations.get(alias, 0):
                    # Re-login/delete raced this request; discard its old ticket.
                    continue
                self._ticket_cache[key] = ticket
                self._device_sessions.add(key)
                return ticket.value

    async def _signed_relay_response(self, alias: str, method: str, path: str,
                                     body: bytes, proxy_url: Optional[str],
                                     stream: bool = False,
                                     url_path: Optional[str] = None,
                                     extra_headers: Optional[
                                         Sequence[tuple[str, str]]] = None,
                                     session_id: str = "",
                                     call_id: str = "",
                                     probe: bool = False,
    ) -> httpx.Response:
        ticket = await self._device_ticket(alias, proxy_url)
        signer = self._signer(alias)
        # Credentials and signature metadata always win over caller-derived
        # headers. The signature covers the canonical pathname only; the
        # product preserves ?beta=true on the URL but excludes it here.
        signature = signer.headers(method, path, body)
        url = self.settings.relay_base + (url_path or path)
        if path in (MESSAGES_PATH, COUNT_TOKENS_PATH):
            headers = list(extra_headers or ())
            if probe:
                headers.append(("x-mirasim-probe", "usage"))
            headers.append(("authorization", "Bearer " + ticket))
            if not probe:
                headers.extend((
                    ("x-mirasim-session", session_id),
                    ("x-mirasim-agent", "claude"),
                ))
            headers.extend((
                ("x-mirasim-device", signature["x-mirasim-device"]),
                ("x-mirasim-client", signature["x-mirasim-client"]),
            ))
            if not probe and self.settings.mirasim_locale:
                # Deliberately one shared value, not per-account: locale is
                # cross-checkable against the exit IP's geography, so a
                # per-alias locale uncorrelated with this request's proxy exit
                # would introduce an IP<->locale mismatch — a stronger tell than
                # the shared value.  The only correct per-account locale tracks
                # the exit region, which the node metadata does not carry
                # reliably; an operator who knows an account's geography sets it
                # globally via MIROFISH_MIRASIM_LOCALE.  Contrast the arch/os
                # slot, which has no such external correlate and so is
                # per-account (see _CLI_MACHINE_PROFILES).
                headers.append(("x-mirasim-locale", self.settings.mirasim_locale))
            if not probe:
                headers.append(("x-mirasim-call", call_id))
            headers.extend((
                ("x-mirasim-ts", signature["x-mirasim-ts"]),
                ("x-mirasim-nonce", signature["x-mirasim-nonce"]),
                ("x-mirasim-sig", signature["x-mirasim-sig"]),
                *_wire_tail(url, body),
            ))
        else:
            headers: list[tuple[str, str]] = []
            if body:
                headers.append(("content-type", "application/json"))
            if path == LIMITS_PATH:
                headers.append(("x-mirasim-probe", "usage"))
            headers.extend((
                ("Authorization", "Bearer " + ticket),
                ("x-mirasim-device", signature["x-mirasim-device"]),
                ("x-mirasim-ts", signature["x-mirasim-ts"]),
                ("x-mirasim-nonce", signature["x-mirasim-nonce"]),
                ("x-mirasim-sig", signature["x-mirasim-sig"]),
                ("x-mirasim-client", signature["x-mirasim-client"]),
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
            response.extensions["mirofish_device_ticket"] = ticket
            return response
        except httpx.HTTPError as exc:
            raise RelayError("relay network error", 502,
                             {"proxy_network": bool(proxy_url),
                              "reason": (str(exc) or type(exc).__name__)[:200]}) from exc

    async def signed_json(self, alias: str, method: str, path: str,
                          payload: Optional[dict[str, Any]] = None,
                          proxy_url: Optional[str] = None, *,
                          request_headers: Optional[Mapping[str, str]] = None,
                          session_id: str = "", beta: bool = False,
                          probe: bool = False) -> tuple[int, dict[str, str], Any]:
        """Call a relay control/model endpoint using device auth."""
        request_body = _json_bytes(payload)
        url_path = path
        extra_headers: Optional[list[tuple[str, str]]] = None
        relay_session = session_id
        call_id = ""
        if path in (MESSAGES_PATH, COUNT_TOKENS_PATH):
            relay_session = session_id or str(uuid.uuid4())
            extra_headers = self._message_request_headers(
                request_headers, probe, relay_session, alias)
            call_id = str(uuid.uuid4())
            if beta:
                url_path += "?beta=true"
        for attempt in range(2):
            response = await self._signed_relay_response(
                alias, method, path, request_body, proxy_url,
                url_path=url_path,
                extra_headers=extra_headers, session_id=relay_session,
                call_id=call_id, probe=probe)
            if response.status_code == 401 and attempt == 0:
                await response.aread()
                await response.aclose()
                self._invalidate_route_ticket(
                    alias, proxy_url,
                    str(response.extensions.get("mirofish_device_ticket", "")))
                continue
            data = _parse_body(response)
            headers = _lower_headers(response)
            await response.aclose()
            _raise_if_region_blocked(alias, response.status_code, data, proxy_url)
            return response.status_code, headers, data
        raise RelayError("signed relay request failed after ticket refresh", 401)

    async def messages(self, alias: str, payload: dict[str, Any],
                       proxy_url: Optional[str] = None, *,
                       request_headers: Optional[Mapping[str, str]] = None,
                       session_id: str = "", beta: bool = False,
                       probe: bool = False) -> tuple[dict[str, Any], dict[str, str]]:
        """Buffered (non-stream) Messages call with ticket/signature retry."""
        payload = _with_cache_breakpoints(_claude_compatible_payload(payload))
        request_body = _json_bytes(payload)
        url_path = MESSAGES_PATH + ("?beta=true" if beta else "")
        relay_session = session_id or str(uuid.uuid4())
        extra_headers = self._message_request_headers(
            request_headers, probe, relay_session, alias)
        call_id = str(uuid.uuid4())
        for attempt in range(2):
            response = await self._signed_relay_response(
                alias, "POST", MESSAGES_PATH, request_body, proxy_url,
                url_path=url_path,
                extra_headers=extra_headers, session_id=relay_session,
                call_id=call_id, probe=probe)
            if response.status_code == 401 and attempt == 0:
                await response.aread()
                await response.aclose()
                self._invalidate_route_ticket(
                    alias, proxy_url,
                    str(response.extensions.get("mirofish_device_ticket", "")))
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
                              request_headers: Optional[Mapping[str, str]] = None,
                              session_id: str = "", beta: bool = False,
                              probe: bool = False) -> httpx.Response:
        """Open a streaming Anthropic Messages call; caller must aclose() it.

        Returns after upstream status/headers are known, so proxy rotation can
        still happen on connect failure; the body streams afterwards.
        """
        payload = _with_cache_breakpoints(_claude_compatible_payload(payload))
        request_body = _json_bytes(payload)
        url_path = MESSAGES_PATH + ("?beta=true" if beta else "")
        relay_session = session_id or str(uuid.uuid4())
        extra_headers = self._message_request_headers(
            request_headers, probe, relay_session, alias)
        call_id = str(uuid.uuid4())
        for attempt in range(2):
            response = await self._signed_relay_response(
                alias, "POST", MESSAGES_PATH, request_body, proxy_url,
                stream=True, url_path=url_path,
                extra_headers=extra_headers, session_id=relay_session,
                call_id=call_id, probe=probe)
            if response.status_code == 401 and attempt == 0:
                await response.aread()
                await response.aclose()
                self._invalidate_route_ticket(
                    alias, proxy_url,
                    str(response.extensions.get("mirofish_device_ticket", "")))
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
