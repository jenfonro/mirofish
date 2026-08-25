"""SQLite metadata store. Credentials live in the vault, never here."""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import secrets
import sqlite3
import stat
import threading
from typing import Any, Optional

from .errors import RelayError
from .validate import alias_value, proxy_subscription_value
from .vault import CredentialStore

PROXY_POOL_ALIAS = "proxy_pool"


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Store:
    def __init__(self, data_dir: pathlib.Path, credentials: CredentialStore,
                 proxy_failure_threshold: int = 2) -> None:
        self.data_dir = data_dir.expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_dir, stat.S_IRWXU)
        self.db_path = self.data_dir / "accounts.sqlite3"
        self.proxy_key_path = self.data_dir / "proxy.key"
        self.vault = credentials
        self.proxy_failure_threshold = proxy_failure_threshold
        self.db_lock = threading.RLock()
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._migrate()
        os.chmod(self.db_path, stat.S_IRUSR | stat.S_IWUSR)

    def _migrate(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
              alias TEXT PRIMARY KEY, email TEXT NOT NULL, user_id TEXT,
              plan TEXT, tenant TEXT, proxy_id TEXT,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        columns = {str(row[1]) for row in self.db.execute("PRAGMA table_info(accounts)")}
        if "proxy_id" not in columns:
            self.db.execute("ALTER TABLE accounts ADD COLUMN proxy_id TEXT")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS proxies (
              proxy_id TEXT PRIMARY KEY, name TEXT NOT NULL, scheme TEXT NOT NULL,
              host TEXT NOT NULL, port INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
              failure_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,
              last_checked TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS usage_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              alias TEXT NOT NULL, model TEXT,
              input_tokens INTEGER NOT NULL DEFAULT 0,
              output_tokens INTEGER NOT NULL DEFAULT 0,
              cache_read_tokens INTEGER NOT NULL DEFAULT 0,
              cache_write_tokens INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            )
        """)
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_log(created_at)")
        self.db.commit()

    # --- local proxy key ----------------------------------------------------

    def proxy_key(self) -> str:
        if self.proxy_key_path.exists():
            return self.proxy_key_path.read_text(encoding="utf-8").strip()
        value = secrets.token_urlsafe(32)
        fd = os.open(str(self.proxy_key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(value + "\n")
        finally:
            if fd != -1:
                os.close(fd)
        return value

    # --- accounts -------------------------------------------------------------

    def aliases(self) -> list[str]:
        with self.db_lock:
            return [str(row[0]) for row in self.db.execute(
                "SELECT alias FROM accounts ORDER BY alias")]

    def row(self, alias: str) -> sqlite3.Row:
        alias = alias_value(alias)
        with self.db_lock:
            row = self.db.execute("SELECT * FROM accounts WHERE alias=?", (alias,)).fetchone()
        if row is None:
            raise RelayError("unknown account: " + alias, 404)
        return row

    def credentials(self, alias: str) -> tuple[str, str]:
        self.row(alias)
        return self.vault.get(alias, "access"), self.vault.get(alias, "refresh")

    def save(self, alias: str, email: str, access: str, refresh: str,
             metadata: dict[str, Any], proxy_id: Optional[str] = None) -> None:
        alias = alias_value(alias)
        self.vault.put(alias, "refresh", refresh)
        self.vault.put(alias, "access", access)
        stamp = utc_now()
        with self.db_lock:
            self.db.execute("""
            INSERT INTO accounts(alias,email,user_id,plan,tenant,proxy_id,metadata_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(alias) DO UPDATE SET email=excluded.email,user_id=excluded.user_id,
              plan=excluded.plan,tenant=excluded.tenant,
              proxy_id=COALESCE(excluded.proxy_id, accounts.proxy_id),
              metadata_json=excluded.metadata_json,
              updated_at=excluded.updated_at
            """, (alias, email, metadata.get("user_id"), metadata.get("plan"),
                  metadata.get("tenant"), proxy_id, json.dumps(metadata, ensure_ascii=False),
                  stamp, stamp))
            self.db.commit()

    def update_metadata(self, alias: str, metadata: dict[str, Any]) -> None:
        with self.db_lock:
            self.db.execute(
                "UPDATE accounts SET user_id=?,plan=?,tenant=?,metadata_json=?,updated_at=? WHERE alias=?",
                (metadata.get("user_id"), metadata.get("plan"), metadata.get("tenant"),
                 json.dumps(metadata, ensure_ascii=False), utc_now(), alias_value(alias)))
            self.db.commit()

    def merge_metadata(self, alias: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Atomic read-modify-write so concurrent requests do not clobber each other."""
        alias = alias_value(alias)
        with self.db_lock:
            row = self.db.execute("SELECT metadata_json FROM accounts WHERE alias=?",
                                  (alias,)).fetchone()
            if row is None:
                raise RelayError("unknown account: " + alias, 404)
            metadata = json.loads(row["metadata_json"])
            metadata.update(patch)
            self.db.execute("UPDATE accounts SET metadata_json=?,updated_at=? WHERE alias=?",
                            (json.dumps(metadata, ensure_ascii=False), utc_now(), alias))
            self.db.commit()
        return metadata

    def remove(self, alias: str) -> None:
        alias = alias_value(alias)
        self.row(alias)
        self.vault.delete(alias, "access")
        self.vault.delete(alias, "refresh")
        self.vault.delete(alias, "device_private_key")
        with self.db_lock:
            self.db.execute("DELETE FROM accounts WHERE alias=?", (alias,))
            self.db.commit()

    # --- proxy pool persistence -------------------------------------------

    def _optional_secret(self, alias: str, kind: str) -> str:
        try:
            return self.vault.get(alias, kind)
        except RelayError as exc:
            if "missing" in str(exc).lower():
                return ""
            raise

    def proxy_subscription_url(self) -> str:
        file_path = os.environ.get("MIROFISH_PROXY_SUBSCRIPTION_URL_FILE", "").strip()
        if file_path:
            try:
                return proxy_subscription_value(
                    pathlib.Path(file_path).read_text(encoding="utf-8"))
            except OSError as exc:
                raise RelayError("could not read proxy subscription URL file", 500) from exc
        env_value = os.environ.get("MIROFISH_PROXY_SUBSCRIPTION_URL", "").strip()
        if env_value:
            return proxy_subscription_value(env_value)
        return self._optional_secret(PROXY_POOL_ALIAS, "subscription_url").strip()

    def set_proxy_subscription_url(self, value: str) -> None:
        if value.strip():
            self.vault.put(PROXY_POOL_ALIAS, "subscription_url", proxy_subscription_value(value))
        else:
            self.vault.delete(PROXY_POOL_ALIAS, "subscription_url")

    def proxy_configs(self) -> dict[str, dict[str, Any]]:
        raw = self._optional_secret(PROXY_POOL_ALIAS, "configs")
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RelayError("could not decode encrypted proxy pool", 500) from exc
        return value if isinstance(value, dict) else {}

    def save_proxy_configs(self, configs: dict[str, dict[str, Any]]) -> None:
        self.vault.put(PROXY_POOL_ALIAS, "configs", json.dumps(configs, ensure_ascii=False))

    def account_proxy_ids(self) -> set[str]:
        """Proxy ids some account is currently pinned to."""
        with self.db_lock:
            rows = self.db.execute(
                "SELECT DISTINCT proxy_id FROM accounts WHERE proxy_id IS NOT NULL").fetchall()
        return {str(row[0]) for row in rows}

    def prune_proxies(self, keep: set[str]) -> int:
        """Delete proxy rows outside `keep`. Provider auto-updates rename every
        node and subscription switches replace the set wholesale, so rows that
        are neither current nor pinned by an account are dead weight."""
        with self.db_lock:
            existing = [str(row[0]) for row in
                        self.db.execute("SELECT proxy_id FROM proxies").fetchall()]
            stale = [proxy_id for proxy_id in existing if proxy_id not in keep]
            for proxy_id in stale:
                self.db.execute("DELETE FROM proxies WHERE proxy_id=?", (proxy_id,))
            if stale:
                self.db.commit()
        return len(stale)

    def set_account_proxy(self, alias: str, proxy_id: Optional[str]) -> None:
        with self.db_lock:
            self.db.execute("UPDATE accounts SET proxy_id=?,updated_at=? WHERE alias=?",
                            (proxy_id, utc_now(), alias_value(alias)))
            self.db.commit()

    def deactivate_proxies(self) -> None:
        with self.db_lock:
            self.db.execute("UPDATE proxies SET active=0,updated_at=?", (utc_now(),))
            self.db.commit()

    def upsert_proxy(self, proxy_id: str, config: dict[str, Any], active: bool = True) -> None:
        stamp = utc_now()
        with self.db_lock:
            self.db.execute("""
                INSERT INTO proxies(proxy_id,name,scheme,host,port,active,failure_count,last_error,
                                    last_checked,created_at,updated_at)
                VALUES(?,?,?,?,?,?,0,NULL,NULL,?,?)
                ON CONFLICT(proxy_id) DO UPDATE SET name=excluded.name,scheme=excluded.scheme,
                  host=excluded.host,port=excluded.port,active=excluded.active,
                  failure_count=CASE WHEN excluded.active=1 THEN 0 ELSE proxies.failure_count END,
                  last_error=CASE WHEN excluded.active=1 THEN NULL ELSE proxies.last_error END,
                  updated_at=excluded.updated_at
            """, (proxy_id, config["name"], config["scheme"], config["host"], config["port"],
                  1 if active else 0, stamp, stamp))
            self.db.commit()

    def proxy_rows(self, active_only: bool = False) -> list[sqlite3.Row]:
        query = ("SELECT * FROM proxies WHERE active=1 ORDER BY proxy_id" if active_only
                 else "SELECT * FROM proxies ORDER BY proxy_id")
        with self.db_lock:
            return list(self.db.execute(query).fetchall())

    def mark_proxy_success(self, proxy_id: str) -> None:
        with self.db_lock:
            self.db.execute(
                "UPDATE proxies SET failure_count=0,last_error=NULL,last_checked=?,updated_at=? WHERE proxy_id=?",
                (utc_now(), utc_now(), proxy_id))
            self.db.commit()

    def mark_proxy_failure(self, proxy_id: str, message: str) -> None:
        with self.db_lock:
            self.db.execute("""
                UPDATE proxies SET failure_count=failure_count+1,last_error=?,last_checked=?,
                  active=CASE WHEN failure_count+1>=? THEN 0 ELSE active END,updated_at=?
                WHERE proxy_id=?
            """, (message[:500], utc_now(), self.proxy_failure_threshold, utc_now(), proxy_id))
            self.db.commit()

    def proxy_assignment_counts(self) -> dict[str, int]:
        with self.db_lock:
            rows = self.db.execute(
                "SELECT proxy_id,COUNT(*) AS count FROM accounts WHERE proxy_id IS NOT NULL GROUP BY proxy_id")
            return {str(row[0]): int(row[1]) for row in rows}

    # --- usage log ----------------------------------------------------------

    def log_usage(self, alias: str, model: Optional[str], usage: dict[str, Any]) -> None:
        def _int(key: str) -> int:
            try:
                return int(usage.get(key) or 0)
            except (TypeError, ValueError):
                return 0
        with self.db_lock:
            self.db.execute("""
                INSERT INTO usage_log(alias,model,input_tokens,output_tokens,
                                      cache_read_tokens,cache_write_tokens,created_at)
                VALUES(?,?,?,?,?,?,?)
            """, (alias_value(alias), model, _int("input_tokens"), _int("output_tokens"),
                  _int("cache_read_input_tokens"), _int("cache_creation_input_tokens"), utc_now()))
            self.db.commit()

    def usage_summary(self, hours: int = 24) -> dict[str, Any]:
        hours = max(1, min(24 * 30, hours))
        since = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(hours=hours)).isoformat()
        with self.db_lock:
            buckets = list(self.db.execute("""
                SELECT substr(created_at, 1, 13) AS hour, alias,
                       COUNT(*) AS requests,
                       SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens
                FROM usage_log WHERE created_at >= ?
                GROUP BY hour, alias ORDER BY hour
            """, (since,)).fetchall())
            totals = self.db.execute("""
                SELECT COUNT(*) AS requests, COALESCE(SUM(input_tokens),0) AS input_tokens,
                       COALESCE(SUM(output_tokens),0) AS output_tokens
                FROM usage_log WHERE created_at >= ?
            """, (since,)).fetchone()
        return {
            "hours": hours,
            "totals": {"requests": int(totals["requests"]),
                       "input_tokens": int(totals["input_tokens"]),
                       "output_tokens": int(totals["output_tokens"])},
            "buckets": [{"hour": str(row["hour"]) + ":00Z", "alias": str(row["alias"]),
                         "requests": int(row["requests"]),
                         "input_tokens": int(row["input_tokens"] or 0),
                         "output_tokens": int(row["output_tokens"] or 0)} for row in buckets],
        }
