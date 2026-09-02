"""Account lifecycle: email-code login, status refresh, model catalog.

Status probes use the zero-cost /v1/limits endpoint. Explicit model scans send
small, billable work requests and are only run when the caller asks for them.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import sqlite3
import time
from typing import Any, Optional

from .config import Settings
from .device import DEVICE_KEY_KIND
from .errors import RelayError
from .store import Store, utc_now
from .upstream import Upstream
from .validate import alias_value, code_value, email_value

logger = logging.getLogger("mirofish.accounts")

SCAN_CANDIDATES = [
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
    "claude-sonnet-5",
    "claude-haiku-4-5",
    "claude-fable-5",
    "claude-fable-5-1",
    # The 0.0.272 roster replaces the short-lived 4-7 entry with 4-6. Keep
    # both IDs so installations talking to an older relay remain usable.
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "kimi-k3",
]


def _epoch_value(value: Any) -> Optional[float]:
    """Normalize a positive finite epoch value from JSON number/string data."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        candidate = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            candidate = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return candidate if math.isfinite(candidate) and candidate > 0 else None


def _iso_epoch(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        parsed = datetime.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        candidate = parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None
    return candidate if math.isfinite(candidate) and candidate > 0 else None


def profile_fields(me: dict[str, Any], referral: dict[str, Any]) -> dict[str, Any]:
    """Normalize the subscription profile the upstream returns about an account.

    /auth/me carries the holder's name, roles, and the plan expiry as an epoch
    (plan_exp); /auth/referral repeats the expiry as an ISO timestamp
    (plan_expires_at) and knows the tier a completed referral ladder upgrades
    to. Free accounts simply have no expiry.
    """
    if not isinstance(me, dict):
        me = {}
    if not isinstance(referral, dict):
        referral = {}
    roles = me.get("roles")
    next_plan = referral.get("next_plan", referral.get("nextPlan"))
    expiry = (_epoch_value(me.get("plan_exp"))
              or _epoch_value(me.get("plan_expires_epoch"))
              or _iso_epoch(me.get("plan_expires_at"))
              or _iso_epoch(referral.get("plan_expires_at")))
    return {
        "name": (me.get("name") if isinstance(me.get("name"), str)
                 else me.get("display_name")
                 if isinstance(me.get("display_name"), str) else None),
        "roles": [role for role in roles if isinstance(role, str)]
        if isinstance(roles, list) else [],
        "plan_expires_epoch": expiry,
        "next_plan": next_plan if isinstance(next_plan, str) else None,
    }


def public_status(row: sqlite3.Row, metadata: Optional[dict[str, Any]] = None,
                  proxy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    metadata = metadata or json.loads(row["metadata_json"])
    return {"alias": row["alias"], "email": row["email"], "user_id": row["user_id"],
            "plan": row["plan"], "tenant": row["tenant"],
            "profile": metadata.get("profile", {}),
            "referral": metadata.get("referral", {}),
            "quota": metadata.get("quota", {}),
            "last_usage": metadata.get("last_usage", {}),
            "last_model": metadata.get("last_model"),
            "limits": metadata.get("limits"),
            "profile_pending": bool(metadata.get("profile_pending")),
            "disabled": bool(metadata.get("disabled")),
            "checked_at": metadata.get("checked_at"),
            "proxy": proxy}


# Window ordering and human labels mirror the upstream /v1/limits response
# (the same windows the official usage widget reads). 7d_fable is the fable
# model's own weekly window; reset-first scheduling weighs it for fable
# requests, and accounts without one simply never report it.
LIMIT_WINDOW_ORDER = ["5h", "7d", "7d_fable", "30d"]
LIMIT_WINDOW_LABEL = {"5h": "5 小时窗口", "7d": "7 天窗口",
                      "7d_fable": "7 天 Fable 窗口", "30d": "30 天窗口"}
LIMIT_WINDOW_LEN = {"5h": 18000, "7d": 604800, "7d_fable": 604800,
                    "30d": 2592000}


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

    _OPTIONAL_TENANT_STATUSES = frozenset({404, 405, 501})

    @staticmethod
    def _tenant_from_payload(
            me: Any, tenant_response: Any,
            fallback: Optional[str] = None) -> Optional[str]:
        """Read a tenant from either profile endpoint shape.

        The 0.0.272 client no longer requests ``/me/tenant`` during startup;
        newer auth responses may carry the value inline.  Older relay builds
        still expose the endpoint, so retain it as an optional enrichment and
        keep the previous value when neither response contains a tenant.
        """
        candidates: list[Any] = []
        if isinstance(tenant_response, dict):
            candidates.extend((tenant_response.get("tenant"),
                               tenant_response.get("tenant_id"),
                               tenant_response.get("tenantId")))
            nested = tenant_response.get("data")
            if isinstance(nested, dict):
                candidates.extend((nested.get("tenant"), nested.get("tenant_id"),
                                   nested.get("tenantId")))
        if isinstance(me, dict):
            candidates.extend((me.get("tenant"), me.get("tenant_id"),
                               me.get("tenantId")))
            nested = me.get("organization")
            if isinstance(nested, dict):
                candidates.extend((nested.get("tenant"), nested.get("id")))
        for value in candidates:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return fallback

    async def _optional_tenant(
            self, alias: str, access: str, proxy_url: Optional[str], *,
            authenticated: bool) -> tuple[int, Any]:
        """Fetch the legacy tenant endpoint without making it a login blocker.

        A 404/405/501 means the current relay simply folded tenant data into
        ``/auth/me`` (or does not expose tenancy at all).  Network, region and
        authentication failures remain errors so proxy rotation and credential
        refresh retain their existing behavior.
        """
        try:
            if authenticated:
                status, _, data = await self.upstream.authed_json(
                    alias, "GET", self.settings.relay_base, "/me/tenant",
                    proxy_url=proxy_url)
            else:
                status, _, data = await self.upstream.json(
                    "GET", self.settings.relay_base, "/me/tenant",
                    access=access, proxy_url=proxy_url)
        except RelayError as exc:
            if exc.status in self._OPTIONAL_TENANT_STATUSES:
                return exc.status, {}
            raise
        if status in self._OPTIONAL_TENANT_STATUSES:
            return status, {}
        return status, data

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
                    "profile": {}, "referral": {}, "tenant_response": {},
                    "quota": {}, "last_usage": {}, "profile_pending": True,
                    "checked_at": None}
        try:
            previous_email = str(self.store.row(alias)["email"])
        except RelayError as exc:
            if exc.status != 404:
                raise
            previous_email = ""
        different_account = bool(
            previous_email and previous_email.casefold() != email.casefold())
        self.store.save(alias, email, access, renewal, metadata, proxy_id=proxy_id)
        if different_account:
            # Upgrade old per-account keys into the installation slot before
            # cleaning up the legacy secret. Re-login rotates authorization and
            # tickets, never the official installation-wide Ed25519 identity.
            self.upstream.ensure_device_identity(alias)
            self.store.vault.delete(alias, DEVICE_KEY_KIND)
            self.upstream.forget_account(alias)
        else:
            # A normal re-login keeps the stable device identity, but tickets
            # issued for the previous credentials must never be reused.
            self.upstream.credentials_changed(alias)

        try:
            s1, _, me = await self.upstream.json(
                "GET", self.settings.auth_base, "/auth/me",
                access=access, proxy_url=proxy_url)
            s2, _, referral = await self.upstream.json(
                "GET", self.settings.auth_base, "/auth/referral",
                access=access, proxy_url=proxy_url)
            s3, tenant = await self._optional_tenant(
                alias, access, proxy_url, authenticated=False)
        except RelayError as exc:
            logger.warning(
                "login credentials saved but profile lookup failed: account=%s status=%s",
                alias, exc.status)
            result = public_status(self.store.row(alias), metadata)
            result["profile_pending"] = True
            return result

        profile_ok = (
            200 <= s1 < 300 and 200 <= s2 < 300
            and (200 <= s3 < 300 or s3 in self._OPTIONAL_TENANT_STATUSES)
            and isinstance(me, dict)
            and isinstance(referral, dict)
            and (isinstance(tenant, dict) or s3 in self._OPTIONAL_TENANT_STATUSES)
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
            "plan": referral.get("current_plan") or me.get("plan"),
            "tenant": self._tenant_from_payload(me, tenant),
            "profile": profile_fields(me, referral),
            "referral": referral,
            "tenant_response": tenant if isinstance(tenant, dict) else {},
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
        access, _ = self.store.credentials(alias)
        ten_status, tenant = await self._optional_tenant(
            alias, access, proxy_url, authenticated=True)
        if ref_status < 200 or ref_status >= 300:
            raise RelayError("account referral check failed", ref_status, referral)
        if (ten_status < 200 or ten_status >= 300) \
                and ten_status not in self._OPTIONAL_TENANT_STATUSES:
            raise RelayError("account tenant check failed", ten_status, tenant)
        if not isinstance(me, dict) or not isinstance(referral, dict) \
                or (not isinstance(tenant, dict)
                    and ten_status not in self._OPTIONAL_TENANT_STATUSES):
            raise RelayError("account status response is malformed", 502)
        # Merge instead of rebuilding: a status refresh must not wipe fields it
        # does not produce (cached limits, the panel's disabled switch, usage).
        old_metadata = json.loads(row["metadata_json"])
        old_tenant = row["tenant"]
        tenant_value = self._tenant_from_payload(me, tenant, old_tenant)
        tenant_snapshot = (tenant if isinstance(tenant, dict)
                           else old_metadata.get("tenant_response", {}))
        metadata = self.store.merge_metadata(alias, {
            "user_id": me.get("id"), "email": me.get("email", row["email"]),
            "plan": referral.get("current_plan") or me.get("plan"),
            "tenant": tenant_value,
            "profile": profile_fields(me, referral),
            "referral": referral, "tenant_response": tenant_snapshot,
            "profile_pending": False, "checked_at": utc_now()})
        if probe:
            # Keep the legacy query flag/CLI option, but use the upstream's
            # supported zero-cost availability source instead of a synthetic
            # one-token Messages request (which is now explicitly rejected).
            await self.fetch_limits(alias, proxy_url=proxy_url)
            return public_status(self.store.row(alias))
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
        status, _, data = await self.upstream.limits(alias, proxy_url=proxy_url)
        if status < 200 or status >= 300:
            raise RelayError("could not read usage limits", status, data)
        limits = normalize_limits(data, time.time())
        row = self.store.row(alias)
        metadata = json.loads(row["metadata_json"])
        metadata["limits"] = limits
        metadata["limits_checked_at"] = utc_now()
        seven_day = next((window for window in limits["windows"]
                          if window["name"] == "7d"), None)
        if seven_day and seven_day["budget"] > 0:
            quota = dict(metadata.get("quota", {}))
            quota["7d_utilization"] = str(seven_day["used"] / seven_day["budget"])
            if seven_day.get("reset_at") is not None:
                quota["7d_reset_epoch"] = str(seven_day["reset_at"])
            metadata["quota"] = quota
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
        """Minimal work requests over candidate models; accepted calls are billable."""
        candidates = SCAN_CANDIDATES[:max_models] if max_models else SCAN_CANDIDATES
        results: list[dict[str, Any]] = []
        for model in candidates:
            payload = {"model": model, "max_tokens": 2,
                       "messages": [{"role": "user", "content": "Reply OK"}]}
            try:
                # This is real work, not an availability probe: carry normal
                # session metadata so the upstream does not reject it as a
                # deprecated one-token probe.
                await self.upstream.messages(alias, payload, proxy_url=proxy_url)
                results.append({"model": model, "accepted": True})
            except RelayError as exc:
                results.append({"model": model, "accepted": False, "status": exc.status})
        return results
