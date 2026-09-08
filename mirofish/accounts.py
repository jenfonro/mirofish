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

    # A tenant read must never be what fails a profile refresh. 404/405/501:
    # the current relay folded tenancy into /auth/me. 429: /me/tenant is a
    # relay-domain read, so an account out of weekly credit is refused there
    # while its mirasim profile — plan, expiry, holder — is perfectly
    # readable, and that is the moment the panel needs it.
    #
    # 403 is deliberately absent: a suspension is refused on both domains, and
    # letting the profile refresh succeed anyway would hide it.
    _OPTIONAL_TENANT_STATUSES = frozenset({404, 405, 429, 501})

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
                    access=access, proxy_url=proxy_url, alias=alias)
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
        # A login is the one moment a new device identity is expected: the
        # account is being set up as if on a fresh installation. Rotating here
        # is also how an account leaves a shared identity behind — accounts
        # that signed in before per-account keys existed all carry the same
        # one, which is precisely what lets the upstream relate them.
        self.upstream.rotate_device_identity(alias)
        if different_account:
            self.upstream.forget_account(alias)
        else:
            # Tickets issued for the previous credentials must never be reused.
            self.upstream.credentials_changed(alias)

        try:
            s1, _, me = await self.upstream.json(
                "GET", self.settings.auth_base, "/auth/me",
                access=access, proxy_url=proxy_url, alias=alias)
            # /auth/me answering is what says these credentials are usable at
            # all. A suspended account is refused here, and the two calls below
            # would only collect the same refusal twice more.
            if 200 <= s1 < 300:
                s2, _, referral = await self.upstream.json(
                    "GET", self.settings.auth_base, "/auth/referral",
                    access=access, proxy_url=proxy_url, alias=alias)
                s3, tenant = await self._optional_tenant(
                    alias, access, proxy_url, authenticated=False)
            else:
                s2, referral = s1, None
                s3, tenant = s1, None
        except RelayError as exc:
            logger.warning(
                "login credentials saved but profile lookup failed: account=%s status=%s",
                alias, exc.status)
            result = public_status(self.store.row(alias), metadata)
            result["profile_pending"] = True
            # Hand the verdict up: the caller records it against the account.
            # Swallowing the refusal keeps the spent code from being wasted,
            # but it also left a suspended account sitting in the pool looking
            # healthy until someone refreshed it by hand.
            result["profile_refusal"] = (exc.status, exc.data)
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
            refused = next(((status, body) for status, body in
                            ((s1, me), (s2, referral), (s3, tenant))
                            if not 200 <= status < 300), None)
            if refused is not None:
                result["profile_refusal"] = refused
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
        # Read the usage windows too, but only now that the profile came back:
        # a suspended account is refused at /auth/me, and asking for windows
        # after that would just add a second refusal for the same reason.
        #
        # Scheduling orders accounts by these windows, so without this a fresh
        # account has none until it happens to serve a request — it used to be
        # filled in by the 300s poll that no longer exists. Failure is silent:
        # the credentials are already saved and the verification code is spent,
        # so a missing window must not turn a completed login into an error.
        try:
            await self.fetch_limits(alias, proxy_url=proxy_url)
        except RelayError as exc:
            logger.info("limits unavailable right after login: account=%s %s",
                        alias, exc)
        except Exception as exc:  # noqa: BLE001 - a saved login must not fail here
            logger.info("limits lookup failed right after login: account=%s %s",
                        alias, exc)
        # Re-read rather than reporting the local `metadata`: that copy predates
        # the windows just written, so passing it would hide them from the panel.
        return public_status(self.store.row(alias))

    # --- status ------------------------------------------------------------

    async def fetch_status(self, alias: str, probe: bool = False,
                           proxy_url: Optional[str] = None) -> dict[str, Any]:
        """The account's mirasim profile: plan, expiry, holder, tenancy.

        Deliberately separate from ``fetch_limits``. They live on different
        domains — the profile is mirasim account data, the windows are relay
        metering — so one must not fail because of the other: an account with
        no weekly credit left still has a plan and an expiry to show, and an
        account whose profile reads fine but whose windows are refused with 403
        is a distinguishable state worth seeing.
        """
        row = self.store.row(alias)
        base = self.settings.auth_base
        status, _, me = await self.upstream.authed_json(alias, "GET", base, "/auth/me",
                                                        proxy_url=proxy_url)
        if status < 200 or status >= 300:
            raise RelayError("account identity check failed", status, me)
        # Only past /auth/me: a suspended account is refused there, and the
        # calls below would repeat the same refusal.
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
            # Legacy flag: callers that still ask for both get the windows too,
            # but a refused window read must not lose the profile that was
            # just fetched successfully — the panel's two buttons hit the two
            # endpoints separately now.
            try:
                await self.fetch_limits(alias, proxy_url=proxy_url)
            except RelayError as exc:
                logger.info("limits unavailable during profile refresh: "
                            "account=%s %s", alias, exc)
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
        self._attach_fable_split(alias, limits)
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
            per_model = self.store.usage_by_model_since(
                alias, reset_epoch - length_seconds, FABLE_MODELS,
                account_generation=generation)
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
        # `data` must stay a pure OpenAI/Anthropic model list, and no other
        # top-level list may hold bare strings: a strict client decodes every
        # top-level list into model-object structs, so one string list fails
        # its whole decode (this is what broke sub2api's "sync upstream
        # models"). Namespace the convenience fields instead.
        return {"object": "list", "data": rows, "ok": True, "status": status,
                "mirofish_model_ids": ids, "count": len(ids),
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
