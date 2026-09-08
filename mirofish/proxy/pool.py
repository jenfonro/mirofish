"""Fixed account-to-node proxy pool over a manually curated node list.

Nodes are added by an operator (one at a time or pasted in bulk) rather than
pulled from a subscription, and the relay dials them directly. There is no
sidecar: an earlier version drove a Mihomo instance through its controller API
and routed each account through a per-account listener, which meant a node
list that changed under us, a second process to keep alive, and a whole
protocol to speak. Dialing HTTP(S)/SOCKS5 ourselves gives the same per-account
isolation — each account is pinned to its own node — with none of that.

An account keeps the node it was bound to, for good: a node is picked once,
when the account has none, and nothing moves it afterwards. Failures used to
rotate the account to another exit, which meant one flaky proxy silently
changed an account's upstream IP — exactly the kind of correlation between
accounts that gets them flagged. A broken node now fails its own accounts'
requests until an operator fixes the node or rebinds the account by hand.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from typing import Any, Optional

from ..errors import RelayError
from ..config import Settings
from ..store import Store
from ..validate import alias_value
from .parse import proxy_identity, proxy_url

# Where a node test dials. Plain HTTP with an empty 204 body: no TLS handshake
# to confuse a proxy failure with a certificate one, and nothing to download.
TEST_URL = "http://www.gstatic.com/generate_204"
TEST_TIMEOUT = 10.0
# Stored in `accounts.proxy_id` when an operator picked "no proxy". A NULL there
# means "never assigned", which is what makes the account eligible for a node;
# without a distinct marker the two are indistinguishable and a deliberate
# direct account gets silently handed an exit on its next request.
DIRECT = "direct"


class ProxyPool:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self.configs = store.proxy_configs()

    @property
    def configured(self) -> bool:
        return bool(self.configs)

    # --- node management ------------------------------------------------------

    def add(self, config: dict[str, Any]) -> dict[str, Any]:
        """Add one node, or update it when the same endpoint already exists.

        Identity is the endpoint (scheme/host/port/credentials), not the name,
        so re-adding a node an operator already has renames it instead of
        silently pooling two identical exits.
        """
        proxy_id = proxy_identity(config)
        merged = dict(self.configs)
        merged[proxy_id] = {**config, "id": proxy_id}
        self.store.save_proxy_configs(merged)
        self.store.upsert_proxy(proxy_id, merged[proxy_id], active=True)
        self.configs = merged
        return merged[proxy_id]

    def update(self, proxy_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Edit a node in place, keeping its id when the endpoint is unchanged."""
        current = self.configs.get(str(proxy_id))
        if not isinstance(current, dict):
            raise RelayError("unknown proxy node: " + str(proxy_id), 404)
        updated = {**current, **changes}
        new_id = proxy_identity(updated)
        merged = dict(self.configs)
        if new_id != str(proxy_id):
            # The endpoint moved, so the identity moved with it. Accounts
            # pinned to the old id are released rather than silently following
            # the edit to a different server.
            merged.pop(str(proxy_id), None)
            self.store.prune_proxies(keep=set(merged) | {new_id})
            for alias in self.store.aliases():
                if str(self.store.row(alias)["proxy_id"] or "") == str(proxy_id):
                    self.store.set_account_proxy(alias, None)
        merged[new_id] = {**updated, "id": new_id}
        self.store.save_proxy_configs(merged)
        self.store.upsert_proxy(new_id, merged[new_id], active=True)
        self.configs = merged
        return merged[new_id]

    def remove(self, proxy_id: str) -> None:
        """Delete a node and release every account pinned to it."""
        proxy_id = str(proxy_id)
        if proxy_id not in self.configs:
            raise RelayError("unknown proxy node: " + proxy_id, 404)
        merged = {key: value for key, value in self.configs.items() if key != proxy_id}
        for alias in self.store.aliases():
            if str(self.store.row(alias)["proxy_id"] or "") == proxy_id:
                self.store.set_account_proxy(alias, None)
        self.store.save_proxy_configs(merged)
        self.store.prune_proxies(keep=set(merged))
        self.configs = merged

    async def test(self, proxy_id: str) -> dict[str, Any]:
        """Dial TEST_URL through one node and record the verdict.

        A success clears the node's failure count, which is how an operator
        puts a repaired node back into new assignments; a failure records the
        reason so the panel can show it.
        """
        import httpx

        config = self.by_id(proxy_id)
        if config is None:
            raise RelayError("unknown proxy node: " + str(proxy_id), 404)
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(proxy=proxy_url(config), trust_env=False,
                                         timeout=TEST_TIMEOUT,
                                         follow_redirects=False) as client:
                response = await client.get(TEST_URL)
        except httpx.HTTPError as exc:
            reason = (str(exc) or type(exc).__name__)[:200]
            self.store.mark_proxy_failure(str(proxy_id), reason)
            return {"id": str(proxy_id), "ok": False, "error": reason}
        latency = round((time.monotonic() - started) * 1000)
        if response.status_code != 204:
            reason = "unexpected status %d" % response.status_code
            self.store.mark_proxy_failure(str(proxy_id), reason)
            return {"id": str(proxy_id), "ok": False, "error": reason,
                    "latency_ms": latency}
        self.store.mark_proxy_success(str(proxy_id))
        return {"id": str(proxy_id), "ok": True, "latency_ms": latency}

    # --- sticky selection -----------------------------------------------------

    def _config_for_row(self, row: sqlite3.Row) -> Optional[dict[str, Any]]:
        config = self.configs.get(str(row["proxy_id"]))
        return dict(config) if isinstance(config, dict) else None

    def _select(self, alias: str) -> dict[str, Any]:
        """Pick the node for an account that has none yet.

        Only ever called for an unbound account: once an account has a node it
        keeps it, so this is a one-time assignment rather than a scheduling
        decision. Least-loaded first, then a stable hash, so a fresh pool
        spreads accounts over exits instead of stacking them on one.
        """
        rows = [row for row in self.store.proxy_rows(active_only=True)
                if int(row["failure_count"]) == 0
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

    def pending_proxy(self, alias: str) -> Optional[dict[str, Any]]:
        """Pick (without persisting) the node a not-yet-saved account would use."""
        if not self.configured:
            return None
        return self._select(alias_value(alias))

    def by_id(self, proxy_id: Any) -> Optional[dict[str, Any]]:
        if not proxy_id:
            return None
        config = self.configs.get(str(proxy_id))
        return dict(config) if isinstance(config, dict) else None

    def for_account(self, alias: str) -> Optional[dict[str, Any]]:
        alias = alias_value(alias)
        if not self.configured:
            return None
        row = self.store.row(alias)
        current_id = str(row["proxy_id"] or "")
        if current_id == DIRECT:
            # An operator chose no proxy. Assigning one here would override
            # that choice behind their back.
            return None
        if current_id:
            # The binding is honoured even when the node is failing or
            # deactivated. Handing back a different exit here is the silent
            # IP change this pool exists to prevent; a broken node is the
            # operator's to fix (or to rebind the account away from).
            config = self.configs.get(current_id)
            if isinstance(config, dict):
                return dict(config)
        config = self._select(alias)
        self.store.set_account_proxy(alias, str(config["id"]))
        return config

    def fail(self, failed: dict[str, Any], reason: str) -> None:
        """Record that a node could not carry a request.

        The count marks the node in the panel and keeps it out of *new*
        assignments; accounts already bound to it stay bound.
        """
        self.store.mark_proxy_failure(str(failed["id"]), reason)

    def success(self, proxy: Optional[dict[str, Any]]) -> None:
        if proxy:
            self.store.mark_proxy_success(str(proxy["id"]))

    def active_count(self) -> int:
        return sum(1 for row in self.store.proxy_rows(active_only=True)
                   if int(row["failure_count"]) == 0)

    # --- reporting -----------------------------------------------------------

    def account_public(self, alias: str) -> Optional[dict[str, Any]]:
        row = self.store.row(alias)
        proxy_id = str(row["proxy_id"] or "")
        # `DIRECT` is a deliberate "no exit", not a node to look up.
        if not proxy_id or proxy_id == DIRECT:
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
        """The pool as the panel renders it: one row per node.

        Credentials are included because the panel edits them; they never
        leave the authenticated admin API, and they live in the encrypted
        config vault rather than SQLite.
        """
        rows = self.store.proxy_rows()
        counts = self.store.proxy_assignment_counts()
        nodes = []
        for row in rows:
            proxy_id = str(row["proxy_id"])
            config = self.configs.get(proxy_id, {})
            nodes.append({
                "id": proxy_id,
                "name": str(config.get("name") or row["name"] or ""),
                "scheme": str(config.get("scheme", row["scheme"])),
                "host": str(config.get("host", row["host"])),
                "port": int(config.get("port", row["port"])),
                "username": str(config.get("username", "")),
                "password": str(config.get("password", "")),
                "active": bool(row["active"]) and int(row["failure_count"]) == 0,
                "assigned": counts.get(proxy_id, 0),
                "failure_count": int(row["failure_count"]),
                "last_error": row["last_error"],
                "last_checked": row["last_checked"],
            })
        return {"configured": self.configured,
                "active": sum(node["active"] for node in nodes),
                "total": len(nodes), "assigned": sum(counts.values()),
                "nodes": nodes}
