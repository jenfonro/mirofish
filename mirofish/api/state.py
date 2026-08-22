"""Shared application state and the proxy-retry orchestration helpers."""

from __future__ import annotations

import json
import threading
import time
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable, Optional

import httpx

from ..accounts import AccountService
from ..config import Settings
from ..errors import RelayError
from ..proxy import ProxyPool
from ..store import Store
from ..upstream import Upstream, quota_headers
from ..validate import alias_value
from ..vault import make_credential_store

LOGIN_TTL_SECONDS = 600.0
QUOTA_EXHAUSTED = 0.999


class AppState:
    def __init__(self, settings: Settings, proxy_key: Optional[str] = None) -> None:
        self.settings = settings
        credentials = make_credential_store(settings.data_dir, settings.cred_backend,
                                            settings.in_docker, settings.keychain_service)
        self.store = Store(settings.data_dir, credentials,
                           settings.proxy_failure_threshold)
        self.pool = ProxyPool(self.store, settings)
        self.upstream = Upstream(settings, self.store)
        self.accounts = AccountService(settings, self.store, self.upstream)
        self.proxy_key = proxy_key or self.store.proxy_key()
        self.default_account = settings.default_account
        self.pending_logins: dict[str, dict[str, Any]] = {}
        self.model_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._rr_index = 0
        self._rr_lock = threading.Lock()

    async def aclose(self) -> None:
        await self.upstream.aclose()
        await self.pool.aclose()

    # --- account selection ----------------------------------------------------

    def _quota_ok(self, alias: str) -> bool:
        try:
            metadata = json.loads(self.store.row(alias)["metadata_json"])
            utilization = metadata.get("quota", {}).get("7d_utilization")
            return utilization is None or float(utilization) < QUOTA_EXHAUSTED
        except (RelayError, ValueError, TypeError, json.JSONDecodeError):
            return True

    def pick_account(self, requested: str) -> str:
        """Explicit header > default account > quota-aware round-robin."""
        requested = requested.strip()
        if requested:
            return alias_value(requested)
        if self.default_account:
            return self.default_account
        aliases = self.store.aliases()
        if not aliases:
            raise RelayError("no account configured; add one via WebUI or CLI first", 400)
        with self._rr_lock:
            start = self._rr_index
            chosen = None
            for offset in range(len(aliases)):
                candidate = aliases[(start + offset) % len(aliases)]
                if self._quota_ok(candidate):
                    chosen = candidate
                    self._rr_index = (start + offset + 1) % len(aliases)
                    break
            if chosen is None:
                # Every account looks exhausted; fall back to plain round-robin.
                chosen = aliases[start % len(aliases)]
                self._rr_index = (start + 1) % len(aliases)
            return chosen

    # --- pending logins -------------------------------------------------------

    def put_pending_login(self, alias: str, email: str, proxy_id: Optional[str]) -> None:
        self.pending_logins[alias] = {"email": email, "created": time.time(),
                                      "proxy_id": proxy_id}

    def take_pending_login(self, alias: str) -> dict[str, Any]:
        pending = self.pending_logins.get(alias)
        if not pending:
            raise RelayError("no pending login for this alias; send a code first", 400)
        if time.time() - pending["created"] > LOGIN_TTL_SECONDS:
            self.pending_logins.pop(alias, None)
            raise RelayError("login session expired; send a new code", 400)
        return pending

    # --- proxy-aware execution ----------------------------------------------

    def _attempts(self, proxy: Optional[dict[str, Any]]) -> int:
        if proxy is None:
            return 1
        return min(4, max(2, self.pool.active_count()))

    @staticmethod
    def _is_proxy_network_failure(exc: RelayError) -> bool:
        return (exc.status == 502 and isinstance(exc.data, dict)
                and exc.data.get("proxy_network") is True)

    async def with_proxy(self, alias: str,
                         op: Callable[[Optional[str]], Awaitable[Any]]) -> Any:
        """Run one account operation, rotating its sticky node on network failure."""
        proxy = await self.pool.for_account(alias)
        attempts = self._attempts(proxy)
        for attempt in range(attempts):
            try:
                async with self.pool.route(alias, proxy) as proxy_url:
                    result = await op(proxy_url)
                self.pool.success(proxy)
                return result
            except RelayError as exc:
                if not proxy or not self._is_proxy_network_failure(exc) \
                        or attempt + 1 >= attempts:
                    raise
                proxy = self.pool.rotate(alias, proxy, "proxy network failure")
        raise RelayError("proxy request failed", 502)

    async def with_fixed_proxy(self, alias: str, proxy: Optional[dict[str, Any]],
                               op: Callable[[Optional[str]], Awaitable[Any]]) -> Any:
        async with self.pool.route(alias, proxy) as proxy_url:
            return await op(proxy_url)

    async def open_messages_stream(
            self, alias: str,
            payload: dict[str, Any]) -> tuple[httpx.Response, AsyncExitStack]:
        """Open a streaming upstream call inside its proxy route context.

        The returned AsyncExitStack keeps the route (and, for shared mihomo
        slots, its lock) plus the HTTP response open; the caller closes it
        when the stream finishes.
        """
        proxy = await self.pool.for_account(alias)
        attempts = self._attempts(proxy)
        for attempt in range(attempts):
            stack = AsyncExitStack()
            try:
                proxy_url = await stack.enter_async_context(
                    self.pool.route(alias, proxy))
                response = await self.upstream.stream_messages(alias, payload, proxy_url)
                stack.push_async_callback(response.aclose)
                self.pool.success(proxy)
                return response, stack
            except RelayError as exc:
                await stack.aclose()
                if not proxy or not self._is_proxy_network_failure(exc) \
                        or attempt + 1 >= attempts:
                    raise
                proxy = self.pool.rotate(alias, proxy, "proxy network failure")
            except BaseException:
                await stack.aclose()
                raise
        raise RelayError("proxy request failed", 502)

    # --- usage accounting ---------------------------------------------------

    def record_usage(self, alias: str, model: Optional[str], usage: dict[str, Any],
                     response_headers: dict[str, str]) -> dict[str, str]:
        """Persist usage/quota metadata and return the outgoing relay headers."""
        quota = quota_headers(response_headers)
        try:
            self.store.merge_metadata(alias, {"last_usage": usage, "quota": quota,
                                              "last_model": model})
            if usage:
                self.store.log_usage(alias, model, usage)
        except RelayError:
            pass
        outgoing = {"X-Mirofish-Account": alias_value(alias)}
        if quota.get("7d_utilization"):
            outgoing["X-Mirofish-Quota-7d-Utilization"] = str(quota["7d_utilization"])
        if quota.get("7d_reset_epoch"):
            outgoing["X-Mirofish-Quota-7d-Reset"] = str(quota["7d_reset_epoch"])
        return outgoing
