"""Sticky account-to-node proxy pool.

Modes:
- "mihomo": nodes come from the sidecar's selector group; traffic exits via
  per-account slot listeners (see mihomo.SlotManager).
- "direct": the relay fetches and parses the subscription itself and dials
  HTTP(S)/SOCKS5 nodes directly.
- disabled: no proxy configured; requests go out directly.

Each account stays pinned to one node and only rotates after a proxy network
failure, mirroring the legacy behavior.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import httpx

from ..config import Settings
from ..errors import RelayError
from ..store import Store
from ..validate import alias_value, proxy_subscription_value
from .mihomo import MihomoClient, SlotManager
from .parse import parse_proxy_subscription, proxy_from_uri, proxy_identity, proxy_url


class ProxyPool:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self.lock = asyncio.Lock()
        self.mihomo_proxy = proxy_from_uri(settings.mihomo_proxy) if settings.mihomo_proxy else None
        if bool(settings.mihomo_controller) != bool(settings.mihomo_proxy):
            raise RelayError("configure both MIROFISH_MIHOMO_CONTROLLER and MIROFISH_MIHOMO_PROXY", 500)
        if settings.mihomo_proxy and not self.mihomo_proxy:
            raise RelayError("MIROFISH_MIHOMO_PROXY must be an http(s) or socks5 URL", 500)
        self.mihomo: Optional[MihomoClient] = None
        self.slots: Optional[SlotManager] = None
        if self.uses_mihomo:
            self.mihomo = MihomoClient(settings.mihomo_controller,
                                       settings.mihomo_controller_timeout)
            self.slots = SlotManager(self.mihomo, proxy_url(self.mihomo_proxy),
                                     settings.mihomo_slots, settings.mihomo_slot_base_port,
                                     settings.mihomo_selector)
        self.subscription_url = store.proxy_subscription_url()
        self.configs = store.proxy_configs()
        self.last_refresh = 0.0
        self.last_attempt = 0.0
        self.last_error = ""
        self.skipped_nodes = 0

    @property
    def uses_mihomo(self) -> bool:
        return bool(self.settings.mihomo_controller and self.mihomo_proxy)

    @property
    def configured(self) -> bool:
        return bool(self.subscription_url) or self.uses_mihomo

    async def aclose(self) -> None:
        if self.mihomo:
            await self.mihomo.aclose()

    # --- refresh ------------------------------------------------------------

    def _sync_url(self) -> str:
        url = self.store.proxy_subscription_url()
        if url != self.subscription_url:
            self.subscription_url = url
            self.last_refresh = 0.0
            self.last_attempt = 0.0
            self.last_error = ""
            self.configs = self.store.proxy_configs()
        return url

    def _store_nodes(self, configs: dict[str, dict[str, Any]], skipped: int) -> None:
        merged = dict(self.configs)
        merged.update(configs)
        self.store.save_proxy_configs(merged)
        self.store.deactivate_proxies()
        for proxy_id, config in configs.items():
            self.store.upsert_proxy(proxy_id, config, active=True)
        self.configs = merged
        self.skipped_nodes = skipped
        self.last_refresh = time.time()
        self.last_error = ""

    async def _fetch_subscription(self, url: str) -> bytes:
        limits = self.settings
        try:
            async with httpx.AsyncClient(trust_env=False,
                                         timeout=min(limits.timeout, limits.proxy_fetch_timeout),
                                         follow_redirects=True) as client:
                async with client.stream("GET", url, headers={
                    "Accept": "application/yaml, text/yaml, text/plain, application/json, */*",
                    "User-Agent": limits.proxy_subscription_user_agent,
                }) as response:
                    if response.status_code >= 400:
                        raise RelayError("proxy subscription request failed", 502,
                                         {"status": response.status_code})
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > limits.proxy_fetch_max_bytes:
                            raise RelayError("proxy subscription is too large", 413)
                    return bytes(body)
        except httpx.HTTPError as exc:
            raise RelayError("proxy subscription network error", 502,
                             {"reason": str(exc)[:200]}) from exc

    async def _refresh_mihomo(self, force: bool) -> dict[str, Any]:
        assert self.mihomo is not None and self.mihomo_proxy is not None
        now = time.time()
        if not force and now - self.last_refresh < self.settings.proxy_refresh_seconds:
            return self.public_summary()
        self.last_attempt = now
        try:
            if force:
                await self.mihomo.refresh_provider(self.settings.mihomo_provider)
            names = await self.mihomo.group_nodes(self.settings.mihomo_selector)
            system = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL"}
            configs: dict[str, dict[str, Any]] = {}
            for raw_name in names:
                name = raw_name.strip()
                if not name or name in system or name == self.settings.mihomo_selector \
                        or name.startswith("MirofishSlot"):
                    continue
                config = {**self.mihomo_proxy, "name": name, "mihomo_node": name}
                proxy_id = proxy_identity(config)
                configs[proxy_id] = {**config, "id": proxy_id}
            if not configs:
                raise RelayError("Mihomo has not loaded any subscription nodes", 502)
            self._store_nodes(configs, skipped=0)
            return self.public_summary()
        except RelayError as exc:
            self.last_error = str(exc)
            if force or not self.store.proxy_rows(active_only=True):
                raise
            return self.public_summary()

    async def refresh(self, force: bool = False) -> dict[str, Any]:
        async with self.lock:
            if self.uses_mihomo:
                return await self._refresh_mihomo(force)
            url = self._sync_url()
            if not url:
                self.last_refresh = time.time()
                self.last_error = ""
                return self.public_summary()
            now = time.time()
            if not force and now - self.last_refresh < self.settings.proxy_refresh_seconds:
                return self.public_summary()
            self.last_attempt = now
            try:
                raw = await self._fetch_subscription(url)
                nodes, skipped = parse_proxy_subscription(raw)
                configs = {proxy_identity(item): {**item, "id": proxy_identity(item)}
                           for item in nodes}
                if not configs:
                    raise RelayError("proxy subscription contains no supported nodes", 502,
                                     {"skipped": skipped})
                self._store_nodes(configs, skipped)
                return self.public_summary()
            except RelayError as exc:
                self.last_error = str(exc)
                if force or not self.store.proxy_rows(active_only=True):
                    raise
                return self.public_summary()

    async def refresh_if_needed(self) -> None:
        if not self.configured and not self._sync_url():
            return
        now = time.time()
        if now - self.last_refresh < self.settings.proxy_refresh_seconds:
            return
        if self.last_error and now - self.last_attempt < self.settings.proxy_refresh_seconds \
                and self.store.proxy_rows(active_only=True):
            return
        try:
            await self.refresh(force=False)
        except RelayError:
            # Keep serving with the previously stored nodes; the next request
            # retries after the refresh interval.
            pass

    async def set_subscription(self, value: str) -> dict[str, Any]:
        if self.uses_mihomo:
            raise RelayError("Mihomo mode reads the subscription from .env; "
                             "change it and recreate the containers", 400)
        value = value.strip()
        if value:
            value = proxy_subscription_value(value)
        self.store.set_proxy_subscription_url(value)
        async with self.lock:
            self.subscription_url = value
            self.last_refresh = 0.0
            self.last_attempt = 0.0
            self.last_error = ""
            if not value:
                self.store.deactivate_proxies()
                for alias in self.store.aliases():
                    self.store.set_account_proxy(alias, None)
        return await self.refresh(force=True)

    # --- sticky selection -----------------------------------------------------

    def _config_for_row(self, row: sqlite3.Row) -> Optional[dict[str, Any]]:
        config = self.configs.get(str(row["proxy_id"]))
        return dict(config) if isinstance(config, dict) else None

    def _select(self, alias: str, exclude: Optional[str] = None) -> dict[str, Any]:
        rows = [row for row in self.store.proxy_rows(active_only=True)
                if str(row["proxy_id"]) != (exclude or "") and int(row["failure_count"]) == 0
                and self._config_for_row(row)]
        if not rows:
            raise RelayError("proxy pool has no available node for this account", 503)
        counts = self.store.proxy_assignment_counts()
        rows.sort(key=lambda row: (counts.get(str(row["proxy_id"]), 0),
                                   hashlib.sha256((alias + str(row["proxy_id"])).encode()).hexdigest()))
        config = self._config_for_row(rows[0])
        if not config:
            raise RelayError("proxy pool node configuration is missing", 500)
        return config

    async def pending_proxy(self, alias: str) -> Optional[dict[str, Any]]:
        """Pick (without persisting) the node a not-yet-saved account would use."""
        alias = alias_value(alias)
        await self.refresh_if_needed()
        if not self.configured:
            return None
        return self._select(alias)

    def by_id(self, proxy_id: Any) -> Optional[dict[str, Any]]:
        if not proxy_id:
            return None
        config = self.configs.get(str(proxy_id))
        return dict(config) if isinstance(config, dict) else None

    async def for_account(self, alias: str) -> Optional[dict[str, Any]]:
        alias = alias_value(alias)
        await self.refresh_if_needed()
        if not self.configured:
            return None
        row = self.store.row(alias)
        current_id = str(row["proxy_id"] or "")
        if current_id:
            proxy_row = next((item for item in self.store.proxy_rows(active_only=True)
                              if str(item["proxy_id"]) == current_id
                              and int(item["failure_count"]) == 0), None)
            if proxy_row is not None:
                config = self._config_for_row(proxy_row)
                if config:
                    return config
        config = self._select(alias)
        self.store.set_account_proxy(alias, str(config["id"]))
        return config

    def rotate(self, alias: str, failed: dict[str, Any], reason: str) -> Optional[dict[str, Any]]:
        alias = alias_value(alias)
        self.store.mark_proxy_failure(str(failed["id"]), reason)
        if not self.configured:
            return None
        self.store.set_account_proxy(alias, None)
        config = self._select(alias, exclude=str(failed["id"]))
        self.store.set_account_proxy(alias, str(config["id"]))
        return config

    def success(self, proxy: Optional[dict[str, Any]]) -> None:
        if proxy:
            self.store.mark_proxy_success(str(proxy["id"]))

    def active_count(self) -> int:
        return sum(1 for row in self.store.proxy_rows(active_only=True)
                   if int(row["failure_count"]) == 0)

    # --- routing ----------------------------------------------------------------

    @asynccontextmanager
    async def route(self, alias: str,
                    proxy: Optional[dict[str, Any]]) -> AsyncIterator[Optional[str]]:
        """Yield the proxy URL requests for this account must use right now."""
        if proxy is None:
            yield None
            return
        if self.uses_mihomo and self.slots is not None:
            node = str(proxy.get("mihomo_node", "")).strip()
            if not node:
                raise RelayError("Mihomo proxy node name is missing", 500)
            async with self.slots.route(alias_value(alias), node) as url:
                yield url
            return
        yield proxy_url(proxy)

    # --- reporting -----------------------------------------------------------

    def account_public(self, alias: str) -> Optional[dict[str, Any]]:
        row = self.store.row(alias)
        proxy_id = str(row["proxy_id"] or "")
        if not proxy_id:
            return None
        proxy_row = next((item for item in self.store.proxy_rows()
                          if str(item["proxy_id"]) == proxy_id), None)
        config = self.configs.get(proxy_id, {})
        if not proxy_row and not config:
            return {"id": proxy_id, "active": False}
        return {"id": proxy_id,
                "name": str(config.get("name", proxy_row["name"] if proxy_row else proxy_id)),
                "scheme": str(config.get("scheme", proxy_row["scheme"] if proxy_row else "")),
                "host": str(config.get("host", proxy_row["host"] if proxy_row else "")),
                "port": int(config.get("port", proxy_row["port"] if proxy_row else 0)),
                "active": (bool(proxy_row["active"]) and int(proxy_row["failure_count"]) == 0)
                          if proxy_row else False,
                "failure_count": int(proxy_row["failure_count"]) if proxy_row else 0,
                "last_error": proxy_row["last_error"] if proxy_row else None}

    def public_summary(self) -> dict[str, Any]:
        rows = self.store.proxy_rows()
        counts = self.store.proxy_assignment_counts()
        return {"configured": self.configured,
                "backend": "mihomo" if self.uses_mihomo else "direct",
                "active": sum(bool(row["active"]) and int(row["failure_count"]) == 0
                              for row in rows),
                "total": len(rows), "assigned": sum(counts.values()),
                "last_refresh": datetime.datetime.fromtimestamp(
                    self.last_refresh, datetime.timezone.utc).isoformat()
                    if self.last_refresh else None,
                "last_error": self.last_error or None,
                "skipped_nodes": self.skipped_nodes,
                "nodes": [{"id": str(row["proxy_id"]), "name": str(row["name"]),
                           "scheme": str(row["scheme"]), "host": str(row["host"]),
                           "port": int(row["port"]),
                           "active": bool(row["active"]) and int(row["failure_count"]) == 0,
                           "assigned": counts.get(str(row["proxy_id"]), 0),
                           "failure_count": int(row["failure_count"]),
                           "last_error": row["last_error"]} for row in rows]}
