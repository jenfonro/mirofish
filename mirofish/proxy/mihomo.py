"""Async Mihomo controller client and the exit-slot manager.

The legacy relay switched one process-global selector under a global lock, so
every proxied request in the whole relay was serialized. The rewrite gives the
sidecar N independent "slot" listeners (mixed ports), each fronted by its own
selector group; every account is pinned to a slot, so accounts proxy requests
concurrently and a selector switch on one slot never affects another.

If the sidecar still runs an old config without slot groups, the manager
falls back to the legacy single-selector behavior (with its global lock) so an
existing deployment keeps working until the next `docker compose up`.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import httpx

from ..errors import RelayError


def slot_group_name(index: int) -> str:
    return f"MirofishSlot{index}"


class MihomoClient:
    def __init__(self, controller: str, timeout: float) -> None:
        self._client = httpx.AsyncClient(base_url=controller, trust_env=False,
                                         timeout=httpx.Timeout(timeout))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def json(self, method: str, path: str,
                   payload: Optional[dict[str, Any]] = None) -> Any:
        try:
            response = await self._client.request(method, path, json=payload,
                                                  headers={"User-Agent": "mirofish-relay/2.0"})
        except httpx.TimeoutException as exc:
            raise RelayError("Mihomo controller request timed out", 503) from exc
        except httpx.HTTPError as exc:
            raise RelayError("Mihomo controller is unavailable", 503,
                             {"reason": str(exc)[:200]}) from exc
        if response.status_code >= 400:
            raise RelayError("Mihomo controller request failed", 502,
                             {"status": response.status_code})
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    async def group_nodes(self, group: str) -> list[str]:
        data = await self.json("GET", "/proxies/" + urllib.parse.quote(group, safe=""))
        nodes = data.get("all") if isinstance(data, dict) else None
        if not isinstance(nodes, list):
            raise RelayError("Mihomo selector did not return proxy nodes", 502)
        return [str(item) for item in nodes]

    async def set_selector(self, group: str, node: str) -> None:
        try:
            await self.json("PUT", "/proxies/" + urllib.parse.quote(group, safe=""),
                            {"name": node})
        except RelayError as exc:
            if isinstance(exc.data, dict) and exc.data.get("status") == 400:
                # Mihomo answers 400 when the node name is not in the group:
                # the provider auto-updated and renamed its nodes, so every
                # stored assignment is stale until the pool resyncs.
                raise RelayError("Mihomo no longer knows proxy node", 502,
                                 {"unknown_node": True}) from exc
            raise

    async def refresh_provider(self, provider: str) -> None:
        try:
            await self.json("PUT", "/providers/proxies/" + urllib.parse.quote(provider, safe=""))
        except RelayError:
            # Older Mihomo releases may not expose provider refresh; the
            # configured provider still refreshes on its own interval.
            pass

    async def has_group(self, group: str) -> bool:
        try:
            await self.group_nodes(group)
            return True
        except RelayError:
            return False


class _Slot:
    def __init__(self, index: int, port: int) -> None:
        self.index = index
        self.group = slot_group_name(index)
        self.port = port
        self.lock = asyncio.Lock()
        self.current_node: Optional[str] = None
        self.accounts: set[str] = set()


class SlotManager:
    def __init__(self, client: MihomoClient, proxy_base_url: str,
                 slot_count: int, slot_base_port: int, legacy_selector: str) -> None:
        self.client = client
        self.legacy_selector = legacy_selector
        self.legacy_lock = asyncio.Lock()
        self.legacy_node: Optional[str] = None
        self._legacy_mode: Optional[bool] = None
        self._parsed_base = urllib.parse.urlsplit(proxy_base_url)
        self.slots = [_Slot(i, slot_base_port + i) for i in range(slot_count)]
        self._assign: dict[str, _Slot] = {}
        self._assign_lock = asyncio.Lock()

    def _proxy_url_for_port(self, port: int) -> str:
        parts = self._parsed_base
        host = parts.hostname or "mihomo"
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        return f"{parts.scheme}://{host}:{port}"

    @property
    def legacy_proxy_url(self) -> str:
        return self._parsed_base.geturl()

    async def detect_mode(self) -> bool:
        """Return True when slot groups exist in the running sidecar config."""
        if self._legacy_mode is None:
            self._legacy_mode = not await self.client.has_group(slot_group_name(0))
        return not self._legacy_mode

    async def _slot_for(self, alias: str) -> _Slot:
        async with self._assign_lock:
            slot = self._assign.get(alias)
            if slot is None:
                slot = min(self.slots, key=lambda item: (len(item.accounts), item.index))
                self._assign[alias] = slot
                slot.accounts.add(alias)
            return slot

    def release(self, alias: str) -> None:
        slot = self._assign.pop(alias, None)
        if slot is not None:
            slot.accounts.discard(alias)

    @asynccontextmanager
    async def route(self, alias: str, node: str) -> AsyncIterator[str]:
        """Yield the mihomo proxy URL whose exit is `node` for this account."""
        if not await self.detect_mode():
            # Legacy sidecar config: one global selector, serialized switching.
            async with self.legacy_lock:
                if self.legacy_node != node:
                    await self.client.set_selector(self.legacy_selector, node)
                    self.legacy_node = node
                yield self.legacy_proxy_url
            return
        slot = await self._slot_for(alias)
        shared = len(slot.accounts) > 1
        if shared:
            # More accounts than slots: serialize this slot's requests so one
            # account's selector switch cannot reroute another mid-request.
            async with slot.lock:
                if slot.current_node != node:
                    await self.client.set_selector(slot.group, node)
                    slot.current_node = node
                yield self._proxy_url_for_port(slot.port)
            return
        async with slot.lock:
            if slot.current_node != node:
                await self.client.set_selector(slot.group, node)
                slot.current_node = node
        yield self._proxy_url_for_port(slot.port)
