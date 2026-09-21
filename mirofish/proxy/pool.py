"""Manually maintained endpoints with explicit, fixed account bindings.

Resolving a binding never writes it or makes a network request. A missing or
invalid binding fails closed; network failures only update diagnostic status.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Optional

import httpx

from ..config import Settings
from ..errors import RelayError
from ..store import Store
from ..validate import alias_value, proxy_node_value
from .parse import DIRECT, parse_proxy_uris, proxy_identity, proxy_url

# Only the explicit admin test action calls this URL.
TEST_URL = "http://www.gstatic.com/generate_204"
TEST_TIMEOUT = 10.0


class ProxyPool:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self.configs = store.proxy_configs()

    @property
    def configured(self) -> bool:
        return bool(self.configs)

    async def aclose(self) -> None:
        """The manual pool owns no persistent clients or background tasks."""

    # --- node management ------------------------------------------------------

    def _save(self, proxy_id: str, config: dict[str, Any]) -> dict[str, Any]:
        merged = {**self.configs, proxy_id: {**config, "id": proxy_id}}
        self.store.save_proxy_configs(merged)
        self.store.upsert_proxy(proxy_id, merged[proxy_id])
        self.configs = merged
        return dict(merged[proxy_id])

    def add(self, config: dict[str, Any]) -> dict[str, Any]:
        """Add an endpoint, or rename the existing copy without changing its id."""
        config = proxy_node_value(config)
        with self.store.db_lock:
            identity = proxy_identity(config)
            proxy_id = next((key for key, item in self.configs.items()
                             if isinstance(item, dict) and "mihomo_node" not in item
                             and proxy_identity(item) == identity), secrets.token_hex(16))
            return self._save(proxy_id, config)

    def update(self, proxy_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Edit in place, including endpoint changes. Never rewrite bindings."""
        with self.store.db_lock:
            current = self.configs.get(proxy_id)
            if not isinstance(current, dict):
                raise RelayError("unknown proxy node: " + proxy_id, 404)
            if "mihomo_node" in current and not {"scheme", "host", "port"} <= changes.keys():
                raise RelayError("replace the legacy proxy with a manual endpoint", 400)
            config = proxy_node_value({**current, **changes})
            identity = proxy_identity(config)
            if any(key != proxy_id and isinstance(item, dict)
                   and "mihomo_node" not in item and proxy_identity(item) == identity
                   for key, item in self.configs.items()):
                raise RelayError("proxy endpoint already exists", 409)
            return self._save(proxy_id, config)

    def remove(self, proxy_id: str) -> None:
        """Delete only an unreferenced node; the operator must unbind first."""
        with self.store.db_lock:
            if self.store.proxy_assignment_counts().get(proxy_id):
                raise RelayError("proxy is bound to accounts; unbind it first", 409)
            if proxy_id not in self.configs and not any(
                    row["proxy_id"] == proxy_id for row in self.store.proxy_rows()):
                raise RelayError("unknown proxy node: " + proxy_id, 404)
            merged = {key: value for key, value in self.configs.items() if key != proxy_id}
            self.store.save_proxy_configs(merged)
            self.store.delete_proxy(proxy_id)
            self.configs = merged

    def import_uris(self, text: str) -> dict[str, Any]:
        """Import URI lines without probing them; report bad lines independently."""
        nodes, failed = parse_proxy_uris(text)
        for config in nodes:
            self.add(config)
        return {"added": len(nodes), "failed": failed, "pool": self.public_summary()}

    async def test(self, proxy_id: str) -> dict[str, Any]:
        """Manually test one endpoint against Google 204, without any fallback."""
        if proxy_id == DIRECT:
            raise RelayError("DIRECT is not a proxy node", 400)
        if proxy_id not in self.configs:
            raise RelayError("unknown proxy node: " + proxy_id, 404)
        config = self.by_id(proxy_id)
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(proxy=proxy_url(config), trust_env=False,
                                         timeout=TEST_TIMEOUT,
                                         follow_redirects=False) as client:
                response = await client.get(TEST_URL)
        except httpx.HTTPError as exc:
            reason = (str(exc) or type(exc).__name__)[:200]
            self.fail(config, reason)
            return {"id": proxy_id, "ok": False, "error": reason}
        latency = round((time.monotonic() - started) * 1000)
        if response.status_code != 204:
            reason = "unexpected status %d" % response.status_code
            self.fail(config, reason)
            return {"id": proxy_id, "ok": False, "error": reason, "latency_ms": latency}
        self.success(config)
        return {"id": proxy_id, "ok": True, "latency_ms": latency}

    # --- fixed bindings -------------------------------------------------------

    def by_id(self, proxy_id: Any) -> Optional[dict[str, Any]]:
        """Resolve an explicit id or DIRECT. Missing/invalid bindings are 503.

        Legacy active/failure counters are diagnostics, not routing gates:
        a later business request may retry the same endpoint after a failure.
        """
        if proxy_id == DIRECT:
            return None
        if not isinstance(proxy_id, str) or not proxy_id:
            raise RelayError("proxy is unbound; explicitly choose a node or DIRECT", 503)
        config = self.configs.get(proxy_id)
        if not isinstance(config, dict) or not any(
                row["proxy_id"] == proxy_id for row in self.store.proxy_rows()):
            raise RelayError("bound proxy node is missing: " + proxy_id, 503)
        if "mihomo_node" in config:
            raise RelayError("bound proxy requires a manual endpoint: " + proxy_id, 503)
        if not {"scheme", "host", "port"} <= config.keys():
            raise RelayError("bound proxy configuration is incomplete: " + proxy_id, 503)
        try:
            return {**proxy_node_value(config), "id": proxy_id}
        except RelayError as exc:
            raise RelayError("bound proxy configuration is invalid: " + proxy_id, 503) from exc

    def for_account(self, alias: str) -> Optional[dict[str, Any]]:
        return self.by_id(self.store.row(alias_value(alias))["proxy_id"])

    def pending_proxy(self, alias: str, proxy_id: Any = None) -> Optional[dict[str, Any]]:
        """Use an explicit login choice or an existing binding; never select one."""
        alias = alias_value(alias)
        if proxy_id is not None:
            return self.by_id(proxy_id)
        try:
            return self.for_account(alias)
        except RelayError as exc:
            if exc.status == 404:
                raise RelayError("login requires an explicit proxy node or DIRECT", 503) from exc
            raise

    def fail(self, failed: Optional[dict[str, Any]], reason: str) -> None:
        if isinstance(failed, dict):
            self.store.mark_proxy_failure(str(failed["id"]), reason)

    def success(self, proxy: Optional[dict[str, Any]]) -> None:
        if isinstance(proxy, dict):
            self.store.mark_proxy_success(str(proxy["id"]))

    def active_count(self) -> int:
        return self.public_summary()["active"]

    # --- reporting -----------------------------------------------------------

    def account_public(self, alias: str) -> dict[str, Any] | None:
        proxy_id = self.store.row(alias)["proxy_id"]
        if proxy_id == DIRECT:
            return None
        if not proxy_id:
            return {"id": None, "active": False, "status": "unbound"}
        for node in self.public_summary()["nodes"]:
            if node["id"] == proxy_id:
                return {key: value for key, value in node.items()
                        if key not in ("username", "password", "assigned")}
        return {"id": proxy_id, "active": False, "status": "missing"}

    def public_summary(self) -> dict[str, Any]:
        """Admin-only editable nodes; credentials remain in the encrypted vault."""
        counts = self.store.proxy_assignment_counts()
        nodes = []
        for row in self.store.proxy_rows():
            proxy_id = str(row["proxy_id"])
            raw = self.configs.get(proxy_id)
            config = raw if isinstance(raw, dict) else {}
            try:
                self.by_id(proxy_id)
                active = True
            except RelayError:
                active = False
            status = ("error" if row["failure_count"] or row["last_error"]
                      else "ok" if row["last_checked"] else "untested")
            nodes.append({
                "id": proxy_id, "name": str(row["name"]),
                "scheme": str(row["scheme"]), "host": str(row["host"]),
                "port": int(row["port"]),
                "username": str(config.get("username", "")),
                "password": str(config.get("password", "")),
                "active": active, "status": status if active else "invalid",
                "assigned": counts.get(proxy_id, 0),
                "failure_count": int(row["failure_count"]),
                "last_error": row["last_error"], "last_checked": row["last_checked"],
            })
        return {"configured": self.configured, "active": sum(node["active"] for node in nodes),
                "total": len(nodes), "assigned": sum(counts.values()), "nodes": nodes}
