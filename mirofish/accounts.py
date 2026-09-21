"""Account lifecycle: email-code login, status refresh, model catalog.

Profiles and quotas are separate on-demand reads. No background or model probes run.
"""

from __future__ import annotations

import asyncio
import copy
import datetime
import json
import logging
import math
import sqlite3
import time
from typing import Any, Optional

from .config import Settings
from .errors import RelayError
from .store import Store, utc_now
from .upstream import Upstream
from .validate import alias_value, code_value, email_value

logger = logging.getLogger("mirofish.accounts")

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
                  proxy: Optional[dict[str, Any]] = None,
                  device_id: Optional[str] = None) -> dict[str, Any]:
    metadata = metadata or json.loads(row["metadata_json"])
    return {"alias": row["alias"], "display_name": metadata.get("display_name", ""),
            "device_id": device_id, "proxy_id": row["proxy_id"],
            "email": row["email"], "user_id": row["user_id"],
            "plan": row["plan"], "tenant": row["tenant"],
            "profile": metadata.get("profile", {}),
            "referral": metadata.get("referral", {}),
            "quota": metadata.get("quota", {}),
            "last_usage": metadata.get("last_usage", {}),
            "last_model": metadata.get("last_model"),
            "limits": metadata.get("limits"),
            "profile_pending": bool(metadata.get("profile_pending")),
            "disabled": bool(metadata.get("disabled")),
            "health": metadata.get("health") or {},
            "checked_at": metadata.get("checked_at"),
            "proxy": proxy}


# Window ordering and human labels mirror the upstream /v1/limits response
# (the same windows the official usage widget reads). 7d_fable is the fable
# model's own weekly window; reset-first scheduling weighs it for fable
# requests, and accounts without one simply never report it.
LIMIT_WINDOW_ORDER = ["5h", "7d", "7d_claude", "7d_fable", "30d"]
LIMIT_WINDOW_LABEL = {"5h": "5 小时窗口", "7d": "7 天窗口",
                      "7d_claude": "7 天 Claude 窗口",
                      "7d_fable": "7 天 Fable 窗口", "30d": "30 天窗口"}
LIMIT_WINDOW_LEN = {"5h": 18000, "7d": 604800, "7d_claude": 604800,
                    "7d_fable": 604800, "30d": 2592000}
# The upstream meters every fable model against one shared 7d_fable window and
# reports a single number. These are the ids whose spend lands in it, split out
# locally from the usage log so the panel can show which one consumed it.
FABLE_WINDOW = "7d_fable"
FABLE_MODELS = ["claude-fable-5", "claude-fable-5-1"]


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
        if not isinstance(name, str) or not name or isinstance(used, bool) \
                or isinstance(budget, bool) or not isinstance(used, (int, float)) \
                or not isinstance(budget, (int, float)) \
                or not math.isfinite(used) or not math.isfinite(budget) \
                or used < 0 or budget < 0:
            continue
        windows.append({
            "name": name,
            "label": LIMIT_WINDOW_LABEL.get(name, name),
            "length": LIMIT_WINDOW_LEN.get(name),
            "used": float(used),
            "budget": float(budget),
            "reset_at": _epoch_value(entry.get("reset_at")),
        })
    windows.sort(key=lambda w: LIMIT_WINDOW_ORDER.index(w["name"])
                 if w["name"] in LIMIT_WINDOW_ORDER else len(LIMIT_WINDOW_ORDER))
    return {
        "subject": body.get("subject"),
        "suspended": body.get("suspended") is True,
        "degraded": body.get("degraded") is True,
        "unmetered": body.get("unmetered") is True,
        "windows": windows,
        "fetched_epoch": fetched_epoch,
    }


class AccountService:
    def __init__(self, settings: Settings, store: Store, upstream: Upstream) -> None:
        self.settings = settings
        self.store = store
        self.upstream = upstream
        self._limits_locks: dict[str, asyncio.Lock] = {}
        self._limits_errors: dict[str, RelayError] = {}
        self._limits_versions: dict[str, int] = {}
        self._limits_force_versions: dict[str, int] = {}
        self._limits_generations: dict[str, int] = {}
        self._roster_locks: dict[str, asyncio.Lock] = {}

    def cached_limits(self, alias: str) -> Optional[dict[str, Any]]:
        value = json.loads(self.store.row(alias)["metadata_json"]).get("limits")
        if not isinstance(value, dict):
            return None
        value = copy.deepcopy(value)
        self._attach_fable_split(alias, value)
        return value

    def forget_limits(self, alias: str) -> None:
        self._limits_errors.pop(alias, None)
        self._limits_generations[alias] = self._limits_generations.get(alias, 0) + 1

    # --- login ------------------------------------------------------------

    async def start_login(self, alias: str, email: str,
                          proxy_url: Optional[str] = None) -> None:
        alias_value(alias)
        email = email_value(email)
        status, _, sent = await self.upstream.json(
            "POST", self.settings.auth_base, "/auth/code", {"email": email},
            proxy_url=proxy_url, alias=alias)
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
            {"email": email, "code": code}, proxy_url=proxy_url, alias=alias)
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
        try:
            previous = self.store.row(alias)
            previous_email = str(previous["email"])
            old = json.loads(previous["metadata_json"])
        except RelayError as exc:
            if exc.status != 404:
                raise
            previous_email, old = "", {}
        different_account = bool(previous_email and previous_email.casefold() != email.casefold())
        metadata = {} if different_account else dict(old)
        metadata.update({"user_id": None, "email": email, "plan": None, "tenant": None,
                         "profile": {}, "referral": {}, "profile_pending": True,
                         "checked_at": None, "limits": None, "quota": {},
                         "limits_attempt_epoch": None, "limits_forced_epoch": None, "limits_error": None,
                         "model_roster": None, "roster_attempt_epoch": None, "roster_error": None})
        # Operator controls survive a re-login; credentials do not implicitly enable an account.
        for key in ("disabled", "display_name"):
            if key in old:
                metadata[key] = old[key]
        self.store.save(alias, email, access, renewal, metadata, proxy_id=proxy_id)
        self.forget_limits(alias)
        if different_account:
            self.upstream.drop_device_identity(alias)
            self.upstream.forget_account(alias)
        else:
            self.upstream.credentials_changed(alias)
        try:
            await self.fetch_status(alias, proxy_url=proxy_url)
        except RelayError as exc:
            result = public_status(self.store.row(alias))
            result["profile_refusal"] = (exc.status, exc.data)
            return result
        try:
            await self.fetch_limits(alias, proxy_url=proxy_url)
        except RelayError as exc:
            result = public_status(self.store.row(alias))
            result["limits_refusal"] = (exc.status, exc.data)
            return result
        return public_status(self.store.row(alias))

    # --- subscription profile: auth domain only -------------------------------

    async def fetch_status(self, alias: str, probe: bool = False,
                           proxy_url: Optional[str] = None) -> dict[str, Any]:
        """Read subscription data only. The legacy probe flag never adds a quota request."""
        row = self.store.row(alias)
        generation = (self.store.account_generation(alias), self._limits_generations.get(alias, 0))
        status, _, me = await self.upstream.authed_json(
            alias, "GET", self.settings.auth_base, "/auth/me", proxy_url=proxy_url)
        if not 200 <= status < 300:
            raise RelayError("account profile /auth/me refused", status, me)
        if not isinstance(me, dict):
            raise RelayError("account profile response is malformed", 502)
        ref_status, _, referral = await self.upstream.authed_json(
            alias, "GET", self.settings.auth_base, "/auth/referral", proxy_url=proxy_url)
        # Keep a readable plan even if a later profile endpoint refuses the account.
        fields = {"user_id": me.get("id"), "email": me.get("email", row["email"]),
                  "plan": me.get("plan"), "profile": profile_fields(me, {}),
                  "profile_pending": False, "checked_at": utc_now()}
        if 200 <= ref_status < 300 and isinstance(referral, dict):
            fields.update({"plan": referral.get("current_plan") or me.get("plan"),
                           "profile": profile_fields(me, referral), "referral": referral})
        for key in ("tenant", "tenant_id", "tenantId"):
            if isinstance(me.get(key), str) and me[key]:
                fields["tenant"] = me[key]
                break
        if (self.store.account_generation(alias), self._limits_generations.get(alias, 0)) != generation:
            raise RelayError("account changed during profile read", 409)
        self.store.merge_metadata(alias, fields)
        if not 200 <= ref_status < 300:
            raise RelayError("account profile /auth/referral refused", ref_status, referral)
        if not isinstance(referral, dict):
            raise RelayError("account profile response is malformed", 502)
        return public_status(self.store.row(alias))

    # --- on-demand, single-flight quota cache ---------------------------------

    async def fetch_limits(self, alias: str, proxy_url: Optional[str] = None, *,
                           force: bool = False) -> dict[str, Any]:
        """One read per account/TTL, including failed reads; manual/429 refresh may bypass TTL.

        Concurrent forced reads share the same attempt. A one-second floor also
        coalesces a burst of 429s finishing just after that attempt. No timer runs.
        """
        alias = alias_value(alias)
        versions = self._limits_force_versions if force else self._limits_versions
        observed = versions.get(alias, 0)
        lock = self._limits_locks.setdefault(alias, asyncio.Lock())
        async with lock:
            metadata = json.loads(self.store.row(alias)["metadata_json"])
            cached = self.cached_limits(alias)
            now = time.time()
            attempt = float(metadata.get("limits_attempt_epoch") or 0)
            fetched = float((cached or {}).get("fetched_epoch") or 0)
            error = metadata.get("limits_error")
            shared = versions.get(alias, 0) != observed
            fresh = (now - float(metadata.get("limits_forced_epoch") or 0) < 1.0
                     if force else now - max(attempt, fetched) < self.settings.limits_ttl)
            if shared or fresh:
                if error:
                    raise self._limits_errors.get(alias) or RelayError(
                        str(error.get("message", "usage read failed")),
                        int(error.get("status", 503)), error.get("data"))
                if cached is not None:
                    return cached
            fields = {"limits_attempt_epoch": now}
            if force:
                fields["limits_forced_epoch"] = now
            self.store.merge_metadata(alias, fields)
            generation = (self.store.account_generation(alias),
                          self._limits_generations.get(alias, 0))
            try:
                status, _, data = await self.upstream.limits(alias, proxy_url=proxy_url)
                if not 200 <= status < 300:
                    raise RelayError("could not read usage limits", status, data)
                if not isinstance(data, dict) or not isinstance(data.get("windows"), list):
                    raise RelayError("usage limits response is malformed", 502)
                limits = normalize_limits(data, time.time())
                if not limits["windows"] and not limits["unmetered"]:
                    raise RelayError("usage limits contain no usable windows", 503)
                if (self.store.account_generation(alias), self._limits_generations.get(alias, 0)) != generation:
                    raise RelayError("account changed during usage read", 409)
                self._attach_fable_split(alias, limits)
                quota = {}
                weekly = next((w for w in limits["windows"] if w["name"] == "7d"), None)
                if weekly and weekly["budget"] > 0:
                    quota["7d_utilization"] = str(weekly["used"] / weekly["budget"])
                    if weekly.get("reset_at") is not None:
                        quota["7d_reset_epoch"] = str(weekly["reset_at"])
                self.store.merge_metadata(alias, {"limits": limits,
                    "limits_checked_at": utc_now(), "limits_error": None, "quota": quota})
                self._limits_errors.pop(alias, None)
                return limits
            except asyncio.CancelledError:
                if (self.store.account_generation(alias), self._limits_generations.get(alias, 0)) == generation:
                    self.store.merge_metadata(alias, {"limits_error": {
                        "status": 503, "message": "usage read was interrupted", "data": None}})
                raise
            except RelayError as exc:
                if (self.store.account_generation(alias), self._limits_generations.get(alias, 0)) == generation:
                    self._limits_errors[alias] = exc
                    self.store.merge_metadata(alias, {"limits_error": {
                        "status": exc.status, "message": str(exc), "data": exc.data}})
                raise
            finally:
                self._limits_versions[alias] = self._limits_versions.get(alias, 0) + 1
                if force:
                    self._limits_force_versions[alias] = self._limits_force_versions.get(alias, 0) + 1

    def _attach_fable_split(self, alias: str, limits: dict[str, Any]) -> None:
        """Break the shared fable window down per model, in place.

        The upstream reports one number for every fable model together. The
        breakdown counts only the usage logged since this window started, so
        it resets exactly when the window does: ``reset_at - length`` is the
        current window's start, and older rows fall out of the range on their
        own. Missing or invalid upstream window timing leaves the window
        untouched rather than showing a total that spans two windows.
        """
        window = next((entry for entry in limits.get("windows", [])
                       if entry.get("name") == FABLE_WINDOW), None)
        if window is None:
            return
        reset_at, length = window.get("reset_at"), window.get("length")
        if (isinstance(reset_at, bool) or not isinstance(reset_at, (int, float))
                or isinstance(length, bool)):
            return
        try:
            reset_epoch = float(reset_at)
            length_seconds = float(length)
        except (TypeError, ValueError, OverflowError):
            return
        if (not math.isfinite(reset_epoch) or reset_epoch <= 0
                or not math.isfinite(length_seconds) or length_seconds <= 0):
            return
        try:
            generation = self.store.account_generation(alias)
            per_model = {} if reset_epoch <= time.time() else self.store.usage_by_model_since(
                alias, reset_epoch - length_seconds, FABLE_MODELS,
                account_generation=generation, until_epoch=reset_epoch)
        except RelayError:
            return
        window["models"] = [{
            "model": model,
            "requests": stats["requests"],
            "input_tokens": stats["input_tokens"],
            "output_tokens": stats["output_tokens"],
            "cache_read_tokens": stats["cache_read_tokens"],
            "cache_write_tokens": stats["cache_write_tokens"],
            "total_tokens": (stats["input_tokens"] + stats["output_tokens"]
                             + stats["cache_read_tokens"] + stats["cache_write_tokens"]),
        } for model, stats in (
            (model, per_model.get(model) or {
                "requests": 0, "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0})
            for model in FABLE_MODELS)]

    # --- model catalog -------------------------------------------------------

    async def fetch_roster(self, alias: str,
                           proxy_url: Optional[str] = None) -> dict[str, Any]:
        """Signed account roster, read only on a model/catalog request (10-minute cache)."""
        from .model_roster import normalize_roster
        alias = alias_value(alias)
        async with self._roster_locks.setdefault(alias, asyncio.Lock()):
            metadata = json.loads(self.store.row(alias)["metadata_json"])
            roster = metadata.get("model_roster")
            now = time.time()
            attempt = float(metadata.get("roster_attempt_epoch") or
                            (roster or {}).get("fetched_epoch") or 0)
            if now - attempt < self.settings.model_catalog_ttl:
                error = metadata.get("roster_error")
                if error:
                    raise RelayError(str(error.get("message", "model roster unavailable")),
                                     int(error.get("status", 503)), error.get("data"))
                if roster:
                    return roster
            generation = (self.store.account_generation(alias), self._limits_generations.get(alias, 0))
            self.store.merge_metadata(alias, {"roster_attempt_epoch": now})
            try:
                status, _, body = await self.upstream.signed_json(
                    alias, "GET", "/v1/model-roster", proxy_url=proxy_url)
                if not 200 <= status < 300:
                    raise RelayError("account model roster refused", status, body)
                roster = normalize_roster(body)
                if roster is None:
                    raise RelayError("account model roster is empty or malformed", 503)
                if (self.store.account_generation(alias), self._limits_generations.get(alias, 0)) != generation:
                    raise RelayError("account changed during model roster read", 409)
                roster["fetched_epoch"] = time.time()
                self.store.merge_metadata(alias, {"model_roster": roster, "roster_error": None})
                return roster
            except asyncio.CancelledError:
                if (self.store.account_generation(alias), self._limits_generations.get(alias, 0)) == generation:
                    self.store.merge_metadata(alias, {"roster_error": {
                        "message": "model roster read was interrupted", "status": 503, "data": None}})
                raise
            except RelayError as exc:
                if (self.store.account_generation(alias), self._limits_generations.get(alias, 0)) == generation:
                    self.store.merge_metadata(alias, {"roster_error": {
                        "message": str(exc), "status": exc.status, "data": exc.data}})
                raise

    async def resolve_model(self, alias: str, model: str,
                            proxy_url: Optional[str] = None) -> str:
        from .model_roster import supported_model
        roster = await self.fetch_roster(alias, proxy_url=proxy_url)
        resolved = supported_model(roster, model)
        if resolved is None:
            raise RelayError("this account does not advertise the requested model", 400,
                             {"kind": "model_not_supported", "model": model})
        return resolved

    async def model_list(self, alias: str,
                         proxy_url: Optional[str] = None) -> dict[str, Any]:
        from .model_roster import entries
        roster = await self.fetch_roster(alias, proxy_url=proxy_url)
        rows = entries(roster)
        return {"object": "list", "data": rows,
                "mirofish_model_ids": [row["id"] for row in rows],
                "count": len(rows), "ok": True, "status": 200,
                "roster_version": roster["version"]}

    @staticmethod
    def _public_model_list(status: int, data: dict[str, Any]) -> dict[str, Any]:
        # Compatibility formatter for callers consuming the legacy catalog shape.
        if not 200 <= status < 300:
            return {"object": "list", "ok": False, "status": status, "data": [], "error": data}
        ids = sorted({entry["id"] for entry in data.get("data", [])
                      if isinstance(entry, dict) and isinstance(entry.get("id"), str)})
        return {"object": "list", "data": [{"id": mid, "object": "model",
                 "created": 0, "owned_by": "mirofish"} for mid in ids],
                "ok": True, "status": status, "mirofish_model_ids": ids, "count": len(ids)}
