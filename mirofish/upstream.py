"""Async upstream HTTP layer: auth endpoints, token refresh, model relay.

- One httpx.AsyncClient per proxy URL (connection pooling per exit).
- Token refresh is single-flight per alias so concurrent 401s do not stampede
  the refresh endpoint or clobber each other's rotated refresh token.
- /v1/messages supports true streaming: the upstream SSE response is handed to
  the caller unbuffered.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from .config import Settings
from .errors import RelayError
from .store import Store

USER_AGENT = "mirofish-local-relay/2.0"


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


class Upstream:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._clients_lock = asyncio.Lock()
        self._refresh_locks: dict[str, asyncio.Lock] = {}

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
                              "reason": str(exc)[:200]}) from exc
        return response.status_code, _lower_headers(response), _parse_body(response)

    # --- token refresh (single-flight per alias) ------------------------------

    def _refresh_lock(self, alias: str) -> asyncio.Lock:
        lock = self._refresh_locks.get(alias)
        if lock is None:
            lock = self._refresh_locks.setdefault(alias, asyncio.Lock())
        return lock

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

    def _messages_headers(self, access: str) -> dict[str, str]:
        return {"Authorization": "Bearer " + access,
                "anthropic-version": self.settings.anthropic_version,
                "Accept-Encoding": "identity",
                "User-Agent": USER_AGENT,
                "x-mirasim-probe": "usage"}

    async def messages(self, alias: str, payload: dict[str, Any],
                       proxy_url: Optional[str] = None) -> tuple[dict[str, Any], dict[str, str]]:
        """Buffered (non-stream) Anthropic Messages call with 401 refresh retry."""
        access, _ = self.store.credentials(alias)
        client = await self.client(proxy_url)
        url = self.settings.relay_base + "/v1/messages"
        for attempt in range(2):
            try:
                response = await client.post(url, json=payload,
                                             headers=self._messages_headers(access))
            except httpx.HTTPError as exc:
                raise RelayError("relay network error", 502,
                                 {"proxy_network": bool(proxy_url),
                                  "reason": str(exc)[:200]}) from exc
            if response.status_code == 401 and attempt == 0:
                access = await self.refresh_access(alias, access, proxy_url)
                continue
            body = _parse_body(response)
            if response.status_code >= 400:
                raise RelayError("model request rejected", response.status_code, body)
            return body, _lower_headers(response)
        raise RelayError("model request failed after token refresh", 401)

    async def stream_messages(self, alias: str, payload: dict[str, Any],
                              proxy_url: Optional[str] = None) -> httpx.Response:
        """Open a streaming Anthropic Messages call; caller must aclose() it.

        Returns after upstream status/headers are known, so proxy rotation can
        still happen on connect failure; the body streams afterwards.
        """
        access, _ = self.store.credentials(alias)
        client = await self.client(proxy_url)
        url = self.settings.relay_base + "/v1/messages"
        timeout = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=30.0)
        for attempt in range(2):
            request = client.build_request("POST", url, json=payload,
                                           headers=self._messages_headers(access),
                                           timeout=timeout)
            try:
                response = await client.send(request, stream=True)
            except httpx.HTTPError as exc:
                raise RelayError("relay network error", 502,
                                 {"proxy_network": bool(proxy_url),
                                  "reason": str(exc)[:200]}) from exc
            if response.status_code == 401 and attempt == 0:
                await response.aread()
                await response.aclose()
                access = await self.refresh_access(alias, access, proxy_url)
                continue
            if response.status_code >= 400:
                await response.aread()
                body = _parse_body(response)
                await response.aclose()
                raise RelayError("model request rejected", response.status_code, body)
            return response
        raise RelayError("model request failed after token refresh", 401)
