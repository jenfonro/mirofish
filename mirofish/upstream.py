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
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .config import Settings
from .device import DeviceSigner
from .errors import RelayError
from .store import Store

USER_AGENT = "mirofish-local-relay/2.0"
DEVICE_SESSION_PATH = "/v1/device/session"


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
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._clients_lock = asyncio.Lock()
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._ticket_locks: dict[str, asyncio.Lock] = {}
        self._ticket_cache: dict[str, _DeviceTicket] = {}
        self._signers: dict[str, DeviceSigner] = {}

    async def aclose(self) -> None:
        async with self._clients_lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            await client.aclose()

    async def client(self, proxy_url: Optional[str]) -> httpx.AsyncClient:
        key = proxy_url or ""
        async with self._clients_lock:
            client = self._clients.get(key)
            if client is None:
                client = httpx.AsyncClient(
                    proxy=proxy_url, trust_env=False, follow_redirects=False,
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
        return response.status_code, _lower_headers(response), _parse_body(response)

    # --- token refresh (single-flight per alias) ------------------------------

    def _refresh_lock(self, alias: str) -> asyncio.Lock:
        lock = self._refresh_locks.get(alias)
        if lock is None:
            lock = self._refresh_locks.setdefault(alias, asyncio.Lock())
        return lock

    def _ticket_lock(self, alias: str) -> asyncio.Lock:
        lock = self._ticket_locks.get(alias)
        if lock is None:
            lock = self._ticket_locks.setdefault(alias, asyncio.Lock())
        return lock

    def _signer(self, alias: str) -> DeviceSigner:
        signer = self._signers.get(alias)
        if signer is None:
            signer = DeviceSigner(self.store, alias, self.settings.mirasim_client_version)
            self._signers[alias] = signer
        return signer

    def _invalidate_ticket(self, alias: str) -> None:
        self._ticket_cache.pop(alias, None)

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
        async with self._ticket_lock(alias):
            cached = self._ticket_cache.get(alias)
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
            self._ticket_cache[alias] = ticket
            return ticket.value

    async def _signed_relay_response(self, alias: str, method: str, path: str,
                                     body: bytes, proxy_url: Optional[str],
                                     stream: bool = False,
                                     force_ticket: bool = False) -> httpx.Response:
        ticket = await self._device_ticket(alias, proxy_url, force=force_ticket)
        signer = self._signer(alias)
        headers = {
            "Authorization": "Bearer " + ticket,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            **signer.headers(method, path, body),
        }
        if path == "/v1/messages":
            headers.update({
                "anthropic-version": self.settings.anthropic_version,
                "Accept-Encoding": "identity",
                "x-mirasim-probe": "usage",
            })
        if body:
            headers["Content-Type"] = "application/json"
        client = await self.client(proxy_url)
        url = self.settings.relay_base + path
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
                          proxy_url: Optional[str] = None) -> tuple[int, dict[str, str], Any]:
        """Call a relay control/model endpoint using device auth."""
        body = _json_bytes(payload)
        for attempt in range(2):
            response = await self._signed_relay_response(
                alias, method, path, body, proxy_url, force_ticket=attempt == 1)
            if response.status_code == 401 and attempt == 0:
                await response.aread()
                await response.aclose()
                self._invalidate_ticket(alias)
                continue
            data = _parse_body(response)
            headers = _lower_headers(response)
            await response.aclose()
            return response.status_code, headers, data
        raise RelayError("signed relay request failed after ticket refresh", 401)

    async def messages(self, alias: str, payload: dict[str, Any],
                       proxy_url: Optional[str] = None) -> tuple[dict[str, Any], dict[str, str]]:
        """Buffered (non-stream) Messages call with ticket/signature retry."""
        body = _json_bytes(payload)
        for attempt in range(2):
            response = await self._signed_relay_response(
                alias, "POST", "/v1/messages", body, proxy_url,
                force_ticket=attempt == 1)
            if response.status_code == 401 and attempt == 0:
                await response.aread()
                await response.aclose()
                self._invalidate_ticket(alias)
                continue
            body = _parse_body(response)
            headers = _lower_headers(response)
            await response.aclose()
            if response.status_code >= 400:
                raise RelayError("model request rejected", response.status_code, body)
            return body, headers
        raise RelayError("model request failed after ticket refresh", 401)

    async def stream_messages(self, alias: str, payload: dict[str, Any],
                              proxy_url: Optional[str] = None) -> httpx.Response:
        """Open a streaming Anthropic Messages call; caller must aclose() it.

        Returns after upstream status/headers are known, so proxy rotation can
        still happen on connect failure; the body streams afterwards.
        """
        body = _json_bytes(payload)
        for attempt in range(2):
            response = await self._signed_relay_response(
                alias, "POST", "/v1/messages", body, proxy_url,
                stream=True, force_ticket=attempt == 1)
            if response.status_code == 401 and attempt == 0:
                await response.aread()
                await response.aclose()
                self._invalidate_ticket(alias)
                continue
            if response.status_code >= 400:
                await response.aread()
                body = _parse_body(response)
                await response.aclose()
                raise RelayError("model request rejected", response.status_code, body)
            return response
        raise RelayError("model request failed after ticket refresh", 401)
