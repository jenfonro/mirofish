"""Async upstream HTTP layer: auth endpoints, token refresh, model relay.

- One httpx.AsyncClient per proxy URL (connection pooling per exit).
- Token refresh is single-flight per alias so concurrent 401s do not stampede
  the refresh endpoint or clobber each other's rotated refresh token.
- /v1/messages supports true streaming: the upstream SSE response is handed to
  the caller unbuffered.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import httpx

from .config import Settings
from .device import DeviceSigner
from .errors import RelayError
from .store import Store

USER_AGENT = "mirofish-local-relay/2.0"
DEVICE_SESSION_PATH = "/v1/device/session"
MESSAGES_PATH = "/v1/messages"
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
LIMITS_PATH = "/v1/limits"

# The product relay preserves the Claude/Anthropic client fingerprint while
# replacing caller credentials and adding its own relay metadata/signature.
# Keep this list deliberately narrow so local proxy keys and cookies can never
# leak upstream.
_FORWARDED_MESSAGE_HEADERS = {
    "accept",
    "anthropic-beta",
    "anthropic-dangerous-direct-browser-access",
    "anthropic-version",
    "user-agent",
    "x-app",
    "x-claude-code-session-id",
}
_FORWARDED_MESSAGE_PREFIXES = ("x-stainless-",)
_DROPPED_ANTHROPIC_BETAS = {"oauth-2025-04-20"}
_MAX_FORWARDED_HEADER_VALUE = 8192

logger = logging.getLogger("mirofish.upstream")


def _forwarded_message_headers(headers: Optional[Mapping[str, str]]) -> dict[str, str]:
    """Copy only non-secret Claude SDK fingerprint headers.

    The official client removes the obsolete OAuth beta token before relaying,
    but otherwise preserves the SDK's beta/version/user-agent metadata.
    """
    forwarded: dict[str, str] = {}
    for raw_name, raw_value in (headers or {}).items():
        name = str(raw_name).lower()
        if name not in _FORWARDED_MESSAGE_HEADERS \
                and not name.startswith(_FORWARDED_MESSAGE_PREFIXES):
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
        forwarded[name] = value
    return forwarded


def _rejection_detail(body: Any) -> str:
    """Upstream error type/message for logs; never includes our request content."""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return f"{error.get('type', 'error')}: {str(error.get('message', ''))[:500]}"
        if "_raw" in body:
            return "non-JSON body: " + str(body["_raw"])[:200]
    return str(body)[:300]


def _is_region_blocked(status: int, body: Any) -> bool:
    """The upstream refuses to serve requests from this exit's network region."""
    if status != 429 or not isinstance(body, dict):
        return False
    error = body.get("error")
    return (isinstance(error, dict)
            and str(error.get("type")) == "shared_quota_unavailable")


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
        self._signers: dict[str, DeviceSigner] = {}

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
                    follow_redirects=False,
                    timeout=httpx.Timeout(self.settings.timeout, connect=10.0, pool=30.0),
                )
                self._clients[key] = client
            return client

    # --- generic JSON calls -------------------------------------------------

    async def json(self, method: str, base: str, path: str,
                   payload: Optional[dict[str, Any]] = None,
                   access: Optional[str] = None,
                   proxy_url: Optional[str] = None) -> tuple[int, dict[str, str], Any]:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if access:
            headers["Authorization"] = "Bearer " + access
        client = await self.client(proxy_url)
        try:
            response = await client.request(method, base.rstrip("/") + path,
                                            json=payload, headers=headers)
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

    def _message_request_headers(
            self, request_headers: Optional[Mapping[str, str]],
            session_id: str, call_id: str, probe: bool) -> dict[str, str]:
        headers = _forwarded_message_headers(request_headers)
        headers.setdefault("accept", "application/json")
        headers.setdefault("anthropic-version", self.settings.anthropic_version)
        if probe:
            # The product's explicit usage probe is intentionally a lean
            # request and does not carry per-conversation relay metadata.
            headers["accept-encoding"] = "identity"
            headers["x-mirasim-probe"] = "usage"
            return headers
        headers.update({
            "x-mirasim-session": session_id or "mirofish_" + str(uuid.uuid4()),
            "x-mirasim-agent": "claude",
            "x-mirasim-call": call_id,
        })
        if self.settings.mirasim_locale:
            headers["x-mirasim-locale"] = self.settings.mirasim_locale
        return headers

    def _invalidate_ticket(self, alias: str) -> None:
        # Access refresh and a relay 401 invalidate every route-scoped ticket
        # for this account.  Tickets are cached per route because the upstream
        # may bind a short-lived device session to its source exit.
        stale = [key for key in self._ticket_cache if key[0] == alias]
        for key in stale:
            self._ticket_cache.pop(key, None)

    async def refresh_access(self, alias: str, stale_access: str,
                             proxy_url: Optional[str] = None) -> str:
        async with self._refresh_lock(alias):
            current = self.store.vault.get(alias, "access")
            if current != stale_access:
                # Another request already refreshed while we waited on the lock.
                return current
            refresh_token = self.store.vault.get(alias, "refresh")
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
            self.store.vault.put(alias, "access", access)
            self.store.vault.put(alias, "refresh", renewal)
            self._invalidate_ticket(alias)
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

    # --- model relay -------------------------------------------------------

    async def _mint_device_ticket(self, alias: str, access: str,
                                  proxy_url: Optional[str] = None) -> _DeviceTicket:
        """Exchange an account access token for a short-lived relay ticket."""
        signer = self._signer(alias)
        body = _json_bytes({"publicKey": signer.public_key,
                            "deviceId": signer.device_id})
        headers = {
            "Authorization": "Bearer " + access,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
            **signer.headers("POST", DEVICE_SESSION_PATH, body),
        }
        client = await self.client(proxy_url)
        try:
            response = await client.post(
                self.settings.relay_base + DEVICE_SESSION_PATH,
                content=body, headers=headers)
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

    async def _device_ticket(self, alias: str, proxy_url: Optional[str] = None,
                             force: bool = False) -> str:
        key = self._ticket_key(alias, proxy_url)
        async with self._ticket_lock(alias, proxy_url):
            cached = self._ticket_cache.get(key)
            if not force and cached and time.monotonic() < cached.expires_at - 60.0:
                return cached.value
            access, _ = self.store.credentials(alias)
            try:
                ticket = await self._mint_device_ticket(alias, access, proxy_url)
            except RelayError as exc:
                # A stale access token can only be diagnosed by the session
                # endpoint. Refresh once, then mint a ticket with the new token.
                if exc.status != 401:
                    raise
                access = await self.refresh_access(alias, access, proxy_url)
                ticket = await self._mint_device_ticket(alias, access, proxy_url)
            self._ticket_cache[key] = ticket
            return ticket.value

    async def _signed_relay_response(self, alias: str, method: str, path: str,
                                     body: bytes, proxy_url: Optional[str],
                                     stream: bool = False,
                                     force_ticket: bool = False,
                                     url_path: Optional[str] = None,
                                     extra_headers: Optional[Mapping[str, str]] = None,
    ) -> httpx.Response:
        ticket = await self._device_ticket(alias, proxy_url, force=force_ticket)
        signer = self._signer(alias)
        headers = {
            "authorization": "Bearer " + ticket,
            "accept": "application/json",
            "user-agent": USER_AGENT,
        }
        headers.update(extra_headers or {})
        # Credentials and signature metadata always win over caller-derived
        # headers. The signature covers the canonical pathname only; the
        # product preserves ?beta=true on the URL but excludes it here.
        headers.update(signer.headers(method, path, body))
        if path == LIMITS_PATH:
            headers["accept-encoding"] = "identity"
            headers["x-mirasim-probe"] = "usage"
        if body:
            headers["content-type"] = "application/json"
        client = await self.client(proxy_url)
        url = self.settings.relay_base + (url_path or path)
        try:
            if stream:
                request = client.build_request(method, url, content=body,
                                               headers=headers,
                                               timeout=httpx.Timeout(
                                                   connect=10.0, read=300.0,
                                                   write=30.0, pool=30.0))
                return await client.send(request, stream=True)
            return await client.request(method, url, content=body, headers=headers)
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
        extra_headers: Optional[dict[str, str]] = None
        if path in (MESSAGES_PATH, COUNT_TOKENS_PATH):
            extra_headers = self._message_request_headers(
                request_headers, session_id, str(uuid.uuid4()), probe)
            if beta:
                url_path += "?beta=true"
        for attempt in range(2):
            response = await self._signed_relay_response(
                alias, method, path, request_body, proxy_url,
                force_ticket=attempt == 1, url_path=url_path,
                extra_headers=extra_headers)
            if response.status_code == 401 and attempt == 0:
                await response.aread()
                await response.aclose()
                self._invalidate_ticket(alias)
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
        request_body = _json_bytes(payload)
        url_path = MESSAGES_PATH + ("?beta=true" if beta else "")
        extra_headers = self._message_request_headers(
            request_headers, session_id, str(uuid.uuid4()), probe)
        for attempt in range(2):
            response = await self._signed_relay_response(
                alias, "POST", MESSAGES_PATH, request_body, proxy_url,
                force_ticket=attempt == 1, url_path=url_path,
                extra_headers=extra_headers)
            if response.status_code == 401 and attempt == 0:
                await response.aread()
                await response.aclose()
                self._invalidate_ticket(alias)
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
        request_body = _json_bytes(payload)
        url_path = MESSAGES_PATH + ("?beta=true" if beta else "")
        extra_headers = self._message_request_headers(
            request_headers, session_id, str(uuid.uuid4()), probe)
        for attempt in range(2):
            response = await self._signed_relay_response(
                alias, "POST", MESSAGES_PATH, request_body, proxy_url,
                stream=True, force_ticket=attempt == 1, url_path=url_path,
                extra_headers=extra_headers)
            if response.status_code == 401 and attempt == 0:
                await response.aread()
                await response.aclose()
                self._invalidate_ticket(alias)
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
