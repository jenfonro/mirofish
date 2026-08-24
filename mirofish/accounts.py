"""Account lifecycle: email-code login, status refresh, model catalog.

Probe and model-scan operations send billable 1-token model requests against
the account itself; they only run when explicitly requested.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Optional

from .config import Settings
from .errors import RelayError
from .store import Store, utc_now
from .upstream import Upstream, quota_headers
from .validate import alias_value, code_value, email_value

logger = logging.getLogger("mirofish.accounts")

SCAN_CANDIDATES = [
    "claude-sonnet-5",
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-5",
    "claude-opus-4-1-20250805",
    "claude-opus-4-20250514",
    "claude-sonnet-4-20250514",
    "claude-3-7-sonnet-20250219",
    "claude-3-5-haiku-20241022",
]


def public_status(row: sqlite3.Row, metadata: Optional[dict[str, Any]] = None,
                  proxy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    metadata = metadata or json.loads(row["metadata_json"])
    return {"alias": row["alias"], "email": row["email"], "user_id": row["user_id"],
            "plan": row["plan"], "tenant": row["tenant"],
            "referral": metadata.get("referral", {}),
            "quota": metadata.get("quota", {}),
            "last_usage": metadata.get("last_usage", {}),
            "last_model": metadata.get("last_model"),
            "limits": metadata.get("limits"),
            "profile_pending": bool(metadata.get("profile_pending")),
            "checked_at": metadata.get("checked_at"),
            "proxy": proxy}


# Window ordering and human labels mirror the upstream /v1/limits response
# (the same windows the official usage widget reads).
LIMIT_WINDOW_ORDER = ["5h", "7d", "30d"]
LIMIT_WINDOW_LABEL = {"5h": "5 小时窗口", "7d": "7 天窗口", "30d": "30 天窗口"}
LIMIT_WINDOW_LEN = {"5h": 18000, "7d": 604800, "30d": 2592000}


def normalize_limits(data: Any, fetched_epoch: float) -> dict[str, Any]:
    """Shape an upstream /v1/limits body into the payload the WebUI consumes.

    Pass-through of used/budget/reset_at per window plus a server clock so the
    client can compute the pace line (匀速线 = even-rate reference) exactly.
    """
    body = data if isinstance(data, dict) else {}
    windows = []
    for entry in body.get("windows", []) if isinstance(body.get("windows"), list) else []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        used = entry.get("used")
        budget = entry.get("budget")
        if not isinstance(name, str) or not isinstance(used, (int, float)) \
                or not isinstance(budget, (int, float)):
            continue
        windows.append({
            "name": name,
            "label": LIMIT_WINDOW_LABEL.get(name, name),
            "length": LIMIT_WINDOW_LEN.get(name),
            "used": float(used),
            "budget": float(budget),
            "reset_at": entry.get("reset_at"),
        })
    windows.sort(key=lambda w: LIMIT_WINDOW_ORDER.index(w["name"])
                 if w["name"] in LIMIT_WINDOW_ORDER else len(LIMIT_WINDOW_ORDER))
    return {
        "subject": body.get("subject"),
        "suspended": bool(body.get("suspended")),
        "degraded": bool(body.get("degraded")),
        "unmetered": bool(body.get("unmetered")),
        "windows": windows,
        "fetched_epoch": fetched_epoch,
    }


class AccountService:
    def __init__(self, settings: Settings, store: Store, upstream: Upstream) -> None:
        self.settings = settings
        self.store = store
        self.upstream = upstream

    # --- login ------------------------------------------------------------

    async def start_login(self, alias: str, email: str,
                          proxy_url: Optional[str] = None) -> None:
        alias_value(alias)
        email = email_value(email)
        status, _, sent = await self.upstream.json(
            "POST", self.settings.auth_base, "/auth/code", {"email": email},
            proxy_url=proxy_url)
        if status < 200 or status >= 300 or not isinstance(sent, dict) \
                or sent.get("sent") is not True:
            raise RelayError("verification code was not accepted", status, sent)

    async def finish_login(self, alias: str, email: str, code: str,
                           proxy_url: Optional[str] = None,
                           proxy_id: Optional[str] = None) -> dict[str, Any]:
        alias = alias_value(alias)
        email = email_value(email)
        code = code_value(code)
        status, _, auth = await self.upstream.json(
            "POST", self.settings.auth_base, "/auth/verify",
            {"email": email, "code": code}, proxy_url=proxy_url)
        if status < 200 or status >= 300 or not isinstance(auth, dict):
            raise RelayError("login failed", status, auth)
        access = auth.get("access_token")
        renewal = auth.get("refresh_token")
        if not isinstance(access, str) or not access \
                or not isinstance(renewal, str) or not renewal:
            raise RelayError("upstream login response is missing tokens", 502)
        # Verification codes are single-use. Persist the issued credentials
        # before making the optional profile calls below: if one of those calls
        # has a transient proxy/upstream failure, reporting 502 would prompt the
        # caller to submit an already-consumed code and receive a misleading
        # 401. A saved account can refresh its profile later without another
        # login code.
        metadata = {"user_id": None, "email": email, "plan": None, "tenant": None,
                    "referral": {}, "tenant_response": {}, "quota": {},
                    "last_usage": {}, "profile_pending": True,
                    "checked_at": None}
        self.store.save(alias, email, access, renewal, metadata, proxy_id=proxy_id)

        try:
            s1, _, me = await self.upstream.json(
                "GET", self.settings.auth_base, "/auth/me",
                access=access, proxy_url=proxy_url)
            s2, _, referral = await self.upstream.json(
                "GET", self.settings.auth_base, "/auth/referral",
                access=access, proxy_url=proxy_url)
            s3, _, tenant = await self.upstream.json(
                "GET", self.settings.relay_base, "/me/tenant",
                access=access, proxy_url=proxy_url)
        except RelayError as exc:
            logger.warning(
                "login credentials saved but profile lookup failed: account=%s status=%s",
                alias, exc.status)
            result = public_status(self.store.row(alias), metadata)
            result["profile_pending"] = True
            return result

        profile_ok = (
            all(200 <= status < 300 for status in (s1, s2, s3))
            and isinstance(me, dict)
            and isinstance(referral, dict)
            and isinstance(tenant, dict)
        )
        if not profile_ok:
            logger.warning(
                "login credentials saved but profile lookup was rejected: "
                "account=%s statuses=%s/%s/%s", alias, s1, s2, s3)
            result = public_status(self.store.row(alias), metadata)
            result["profile_pending"] = True
            return result

        metadata.update({
            "user_id": me.get("id"),
            "email": me.get("email", email),
            "plan": referral.get("current_plan"),
            "tenant": tenant.get("tenant"),
            "referral": referral,
            "tenant_response": tenant,
            "profile_pending": False,
            "checked_at": utc_now(),
        })
        self.store.update_metadata(alias, metadata)
        return public_status(self.store.row(alias), metadata)

    # --- status ------------------------------------------------------------

    async def fetch_status(self, alias: str, probe: bool = False,
                           proxy_url: Optional[str] = None) -> dict[str, Any]:
        row = self.store.row(alias)
        base = self.settings.auth_base
        status, _, me = await self.upstream.authed_json(alias, "GET", base, "/auth/me",
                                                        proxy_url=proxy_url)
        if status < 200 or status >= 300:
            raise RelayError("account identity check failed", status, me)
        ref_status, _, referral = await self.upstream.authed_json(
            alias, "GET", base, "/auth/referral", proxy_url=proxy_url)
        ten_status, _, tenant = await self.upstream.authed_json(
            alias, "GET", self.settings.relay_base, "/me/tenant", proxy_url=proxy_url)
        if ref_status < 200 or ref_status >= 300 or ten_status < 200 or ten_status >= 300:
            raise RelayError("account status check failed", 502)
        old = json.loads(row["metadata_json"])
        metadata = {"user_id": me.get("id"), "email": me.get("email", row["email"]),
                    "plan": referral.get("current_plan"), "tenant": tenant.get("tenant"),
                    "referral": referral, "tenant_response": tenant,
                    "quota": old.get("quota", {}), "last_usage": old.get("last_usage", {}),
                    "last_model": old.get("last_model"), "checked_at": utc_now()}
        if probe:
            result, headers = await self.upstream.messages(alias, {
                "model": self.settings.default_model, "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            }, proxy_url=proxy_url, probe=True)
            metadata["last_usage"] = result.get("usage", {}) if isinstance(result, dict) else {}
            metadata["quota"] = quota_headers(headers)
        self.store.update_metadata(alias, metadata)
        return public_status(self.store.row(alias), metadata)

    # --- usage limits --------------------------------------------------------

    async def fetch_limits(self, alias: str,
                           proxy_url: Optional[str] = None) -> dict[str, Any]:
        """Upstream /v1/limits with device auth (zero model cost).

        Returns the per-window budgets the official usage widget reads, and
        caches the tightest window's utilization into account metadata so the
        accounts list can show it without another live call.
        """
        alias = alias_value(alias)
        status, _, data = await self.upstream.signed_json(
            alias, "GET", "/v1/limits", proxy_url=proxy_url)
        if status < 200 or status >= 300:
            raise RelayError("could not read usage limits", status, data)
        limits = normalize_limits(data, time.time())
        row = self.store.row(alias)
        metadata = json.loads(row["metadata_json"])
        metadata["limits"] = limits
        metadata["limits_checked_at"] = utc_now()
        self.store.update_metadata(alias, metadata)
        return limits

    # --- model catalog -------------------------------------------------------

    async def model_list(self, alias: str,
                         proxy_url: Optional[str] = None) -> dict[str, Any]:
        """Upstream /v1/models with device auth (zero model cost)."""
        status, _, data = await self.upstream.signed_json(
            alias, "GET", "/v1/models", proxy_url=proxy_url)
        body = data if isinstance(data, dict) else {"raw": data}
        return self._public_model_list(status, body)

    @staticmethod
    def _public_model_list(status: int, data: dict[str, Any]) -> dict[str, Any]:
        if status < 200 or status >= 300:
            return {"ok": False, "status": status, "data": [], "error": data}
        ids = sorted(entry["id"] for entry
                     in (data.get("data") if isinstance(data.get("data"), list) else [])
                     if isinstance(entry, dict) and isinstance(entry.get("id"), str))
        rows = [{"id": mid, "object": "model", "type": "model",
                 "display_name": mid, "created_at": "2024-01-01T00:00:00Z",
                 "created": 0, "owned_by": "mirofish"} for mid in ids]
        return {"object": "list", "data": rows, "ok": True, "status": status,
                "models": ids, "count": len(ids),
                "note": "来自上游 /v1/models；若为空说明该接口未输出模型或账号被隐藏。"}

    async def scan_models(self, alias: str, max_models: int = 0,
                          proxy_url: Optional[str] = None) -> list[dict[str, Any]]:
        """1-token probes over a short candidate list; each accepted probe is billable."""
        candidates = SCAN_CANDIDATES[:max_models] if max_models else SCAN_CANDIDATES
        results: list[dict[str, Any]] = []
        for model in candidates:
            payload = {"model": model, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "hi"}]}
            try:
                await self.upstream.messages(
                    alias, payload, proxy_url=proxy_url, probe=True)
                results.append({"model": model, "accepted": True})
            except RelayError as exc:
                results.append({"model": model, "accepted": False, "status": exc.status})
        return results
