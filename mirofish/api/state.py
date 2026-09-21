"""Account health, fixed-exit execution and quota-safe conversation routing."""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import math
import threading
import time
import uuid
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable, Mapping, Optional

import httpx

from ..accounts import AccountService
from ..config import Settings
from ..errors import RelayError
from ..proxy import ProxyPool, proxy_url
from ..store import HEALTH_ERROR, HEALTH_PARKED_STATES, HEALTH_SUSPENDED, Store
from ..upstream import (RESPONSES_PATH, Upstream, account_overloaded_503,
                        account_suspension_403, credit_exhausted_429, quota_headers)
from ..validate import alias_value
from ..vault import make_credential_store

logger = logging.getLogger("mirofish.state")

LOGIN_TTL_SECONDS = 600.0
QUOTA_EXHAUSTED = 1.0
TRANSIENT_429_COOLDOWN = 60.0
HEALTH_RETRY_AFTER = {503: 86400.0}
BURST_WINDOW = "5h"
CLAUDE_WINDOW = "7d_claude"
FABLE_WINDOW = "7d_fable"
WINDOW_SECONDS = {"5h": 18000.0, "7d": 604800.0,
                  "7d_claude": 604800.0, "7d_fable": 604800.0}
URGENCY_HORIZON_HOURS = 48.0
RESET_BAND_HOURS = 1.0
ACCOUNT_GENERATION_EXTENSION = "mirofish_account_generation"


class AppState:
    _CONVERSATION_META_KEYS = ("user_id", "conversation_id", "thread_id", "session_id")

    def __init__(self, settings: Settings, proxy_key: Optional[str] = None) -> None:
        self.settings = settings
        credentials = make_credential_store(settings.data_dir, settings.cred_backend,
                                            settings.in_docker, settings.keychain_service)
        self.store = Store(settings.data_dir, credentials)
        try:
            ceiling = float(self.store.setting("quota_ceiling", ""))
        except (TypeError, ValueError):
            ceiling = None
        if ceiling is not None and 0.1 <= ceiling <= 1.0:
            settings.quota_ceiling = ceiling
        self.pool = ProxyPool(self.store, settings)
        self.upstream = Upstream(settings, self.store)
        self.accounts = AccountService(settings, self.store, self.upstream)
        self.proxy_key = proxy_key or self.store.proxy_key()
        self.default_account = settings.default_account
        self.pending_logins: dict[str, dict[str, Any]] = {}
        self._sessions: dict[str, dict[str, Any]] = {}
        self._last_assigned: dict[str, float] = {}
        self._session_lock = threading.Lock()
        self._exhausted_until: dict[str, dict[str, float]] = {}

    async def aclose(self) -> None:
        await self.upstream.aclose()
        await self.pool.aclose()

    def _quota_ceiling(self) -> float:
        return min(QUOTA_EXHAUSTED, self.settings.quota_ceiling)

    async def refresh_limits_if_stale(self, alias: str, *,
                                      force: bool = False) -> Any:
        """Fetch one account through its fixed exit; AccountService owns TTL,
        failed-attempt caching and singleflight, including forced refreshes.

        A failed read is never permission to send a model request with stale
        data. Control-plane success, conversely, never clears model health.
        """
        if self.account_disabled(alias):
            raise RelayError("account is disabled in the panel: " + alias, 403)
        try:
            result = await self.with_proxy(
                alias, lambda url: self.accounts.fetch_limits(
                    alias, proxy_url=url, force=force))
            if isinstance(result, dict) and result.get("suspended") is True:
                raise RelayError("account is suspended", 403, {"error": {
                    "type": "permission_error",
                    "message": "this account is suspended; contact support"}})
            return result
        except RelayError as exc:
            self.note_account_error(alias, exc)
            raise

    # --- quota and health --------------------------------------------------

    def _metadata(self, alias: str) -> dict[str, Any]:
        try:
            return json.loads(self.store.row(alias)["metadata_json"])
        except (RelayError, json.JSONDecodeError):
            return {}

    def _windows_envelope(self, alias: str) -> dict[str, Any]:
        limits = self._metadata(alias).get("limits")
        return limits if isinstance(limits, dict) else {}

    def _windows(self, alias: str) -> dict[str, dict[str, Any]]:
        windows = self._windows_envelope(alias).get("windows")
        if not isinstance(windows, list):
            return {}
        return {str(window.get("name")): window for window in windows
                if isinstance(window, dict)}

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    @classmethod
    def _window_utilization(cls, window: Optional[dict[str, Any]]) -> Optional[float]:
        if not isinstance(window, dict):
            return None
        used, budget = cls._number(window.get("used")), cls._number(window.get("budget"))
        reset = cls._number(window.get("reset_at"))
        if reset is not None and reset <= time.time():
            return None  # Candidate only: a successful limits read still precedes work.
        if used is None or budget is None or used < 0 or budget < 0:
            return None
        if budget == 0:
            return math.inf
        return used / budget

    @staticmethod
    def _is_fable_model(model: Optional[str]) -> bool:
        return bool(model) and model.lower().startswith("claude-fable-")

    @staticmethod
    def _is_claude_model(model: Optional[str]) -> bool:
        return bool(model) and model.lower().startswith("claude-")

    def _relevant_windows(self, model: Optional[str]) -> list[str]:
        names = [BURST_WINDOW, "7d"]
        if self._is_claude_model(model):
            names.append(CLAUDE_WINDOW)
            if self._is_fable_model(model):
                names.append(FABLE_WINDOW)
        return names

    def _window_applies(self, window: str, model: Optional[str]) -> bool:
        return not window or window == "shared" or window in self._relevant_windows(model)

    def _load(self, alias: str, model: Optional[str]) -> float:
        windows = self._windows(alias)
        loads = [self._window_utilization(windows.get(name))
                 for name in self._relevant_windows(model)]
        return max((value for value in loads if value is not None), default=0.0)

    def _header_utilization(self, alias: str) -> Optional[float]:
        quota = self._metadata(alias).get("quota") or {}
        reset = self._number(quota.get("7d_reset_epoch"))
        if reset is not None and reset <= time.time():
            return None
        return self._number(quota.get("7d_utilization"))

    def _quota_ok(self, alias: str, model: Optional[str] = None) -> bool:
        ceiling = self._quota_ceiling()
        header = self._header_utilization(alias)
        return self._load(alias, model) < ceiling and (header is None or header < ceiling)

    def _limits_ready(self, alias: str, model: Optional[str]) -> bool:
        """Missing/invalid windows are not evidence of available quota."""
        limits = self._windows_envelope(alias)
        if limits.get("suspended") is True:
            return False
        fetched = self._number(limits.get("fetched_epoch"))
        if fetched is None or time.time() - fetched >= self.settings.limits_ttl:
            return False
        if limits.get("unmetered") is True:
            return True
        windows = self._windows(alias)
        for name in self._relevant_windows(model):
            window = windows.get(name) or {}
            used, budget = self._number(window.get("used")), self._number(window.get("budget"))
            if used is None or used < 0 or budget is None or budget <= 0:
                return False
            reset = self._number(window.get("reset_at"))
            if reset is not None and fetched < reset <= time.time():
                return False  # A reset crossed since the last successful read.
        return True

    def account_disabled(self, alias: str) -> bool:
        return bool(self._metadata(alias).get("disabled"))

    def account_health(self, alias: str) -> dict[str, Any]:
        metadata = self._metadata(alias)
        health = metadata.get("health")
        if isinstance(health, dict) and health:
            return health
        # Upstream's pre-health schema parked 401s here. Never revive those
        # merely because the new health record has not been written yet.
        if metadata.get("parked"):
            return {"state": HEALTH_ERROR, "status": 401,
                    "message": metadata.get("parked_reason"), "retry_at": None}
        return {}

    def account_suspended(self, alias: str) -> bool:
        return (self.account_health(alias).get("state") == HEALTH_SUSPENDED
                or self._windows_envelope(alias).get("suspended") is True)

    @classmethod
    def _health_deadline(cls, health: dict[str, Any]) -> Optional[float]:
        if health.get("status") == 401 or health.get("state") == HEALTH_SUSPENDED:
            return None
        deadline = cls._number(health.get("retry_at"))
        if deadline is not None:
            return deadline
        window = HEALTH_RETRY_AFTER.get(health.get("status"))
        if window is None:
            return None
        try:
            marked = datetime.datetime.fromisoformat(str(health.get("at", "")).replace("Z", "+00:00"))
            if marked.tzinfo is None:
                marked = marked.replace(tzinfo=datetime.timezone.utc)
            return marked.timestamp() + window
        except (ValueError, TypeError, OverflowError):
            return 0.0  # Legacy undated 503: try on business traffic, never a probe.

    def account_unhealthy(self, alias: str) -> bool:
        if self.account_suspended(alias):
            return True
        health = self.account_health(alias)
        if health.get("state") not in HEALTH_PARKED_STATES:
            return False
        deadline = self._health_deadline(health)
        return deadline is None or deadline > time.time()

    def account_parked(self, alias: str) -> bool:
        return self.account_unhealthy(alias)

    def health_retry_in(self, alias: str) -> Optional[float]:
        health = self.account_health(alias)
        if health.get("state") not in HEALTH_PARKED_STATES:
            return None
        deadline = self._health_deadline(health)
        return max(0.0, deadline - time.time()) if deadline is not None else None

    @staticmethod
    def _refusal_message(exc: RelayError) -> str:
        error = exc.data.get("error") if isinstance(exc.data, dict) else None
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            kind = error.get("type") or error.get("code")
            return f"{kind}: {error['message']}" if kind else error["message"]
        return str(exc)

    @staticmethod
    def _is_health_refusal(exc: RelayError) -> bool:
        return (exc.status == 401
                or account_suspension_403(exc.status, exc.data) is not None
                or account_overloaded_503(exc.status, exc.data))

    def note_account_error(self, alias: str, exc: RelayError) -> bool:
        if not self._is_health_refusal(exc):
            return False
        previous = self.account_health(alias)
        # A later overloaded/profile refusal must not overwrite a login-only
        # 401 or weaken a permanent suspension into a timed capacity failure.
        if previous.get("status") == 401:
            return True
        suspension = account_suspension_403(exc.status, exc.data)
        if previous.get("state") == HEALTH_SUSPENDED and exc.status != 401:
            return True
        state, retry_at = HEALTH_ERROR, None
        if suspension is not None:
            permanent, retry_at = suspension
            if permanent:
                state = HEALTH_SUSPENDED
        elif exc.status in HEALTH_RETRY_AFTER:
            retry_at = time.time() + HEALTH_RETRY_AFTER[exc.status]
        self.drop_account_sessions(alias)
        try:
            self.store.mark_account_error(
                alias, exc.status, self._refusal_message(exc),
                "upstream_%d" % exc.status, retry_after=retry_at, state=state)
        except RelayError:
            pass  # The account may have been removed while the request ran.
        return True

    def note_account_healthy(self, alias: str,
                             account_generation: Optional[str] = None) -> None:
        """Call only after a completed, successful model conversation.

        Opening an HTTP/SSE response, count_tokens, limits and profile reads
        are not success signals. A 401 can only be cleared by re-login.
        """
        health = self.account_health(alias)
        if not health or health.get("status") == 401:
            return
        try:
            if account_generation is not None \
                    and self.store.account_generation(alias) != account_generation:
                return
            self.store.clear_account_error(alias)
        except RelayError:
            pass

    # --- selection: synchronous candidates, async preflight before work ------

    def exhausted_cooldown(self, alias: str, model: Optional[str] = None) -> float:
        now = time.time()
        return max([0.0, *(until - now for window, until in
                           self._exhausted_until.get(alias, {}).items()
                           if model is None or self._window_applies(window, model))])

    def _serviceable(self, alias: str, model: Optional[str] = None) -> bool:
        return (not self.account_disabled(alias) and not self.account_suspended(alias)
                and self.exhausted_cooldown(alias, model or "") <= 0
                and self._quota_ok(alias, model))

    def _selectable(self, alias: str, model: Optional[str] = None) -> bool:
        return self._serviceable(alias, model) and not self.account_unhealthy(alias)

    def _blocked_windows(self, alias: str, model: Optional[str]) -> set[str]:
        windows = self._windows(alias)
        blocked = {name for name in self._relevant_windows(model)
                   if (self._window_utilization(windows.get(name)) or 0) >= self._quota_ceiling()}
        if (self._header_utilization(alias) or 0) >= self._quota_ceiling():
            blocked.add("7d")
        blocked.update(window for window, until in self._exhausted_until.get(alias, {}).items()
                       if window and until > time.time() and self._window_applies(window, model))
        return blocked

    def _tightest_window(self, model: Optional[str], aliases: list[str]) -> str:
        names = [*self._relevant_windows(model), "shared"]
        counts = {name: sum(name in self._blocked_windows(alias, model) for alias in aliases)
                  for name in names}
        return max(names, key=lambda name: (counts[name], names.index(name)))

    def _spent_allowance_error(self, model: Optional[str], aliases: list[str]) -> RelayError:
        window = self._tightest_window(model, aliases)
        message = ("account quota ceiling reached for %s; switch models or "
                   "wait for the window to reset" % window)
        return RelayError(message, 429, {"error": {
            "type": "rate_limit_error", "code": "credit_exhausted_" + window,
            "message": message}})

    def _no_selectable_error(self, model: Optional[str] = None,
                              aliases: Optional[list[str]] = None) -> RelayError:
        candidates = [alias for alias in (self.store.aliases() if aliases is None else aliases)
                      if not self.account_disabled(alias) and not self.account_unhealthy(alias)]
        if candidates and all(self._blocked_windows(alias, model) for alias in candidates):
            return self._spent_allowance_error(model, candidates)
        return RelayError("no serviceable account; check account health, disabled "
                          "state and quota cooldowns", 503)

    def _explicit_account(self, requested: str, model: Optional[str] = None) -> str:
        alias = alias_value(requested)
        self.store.row(alias)
        if self.account_disabled(alias):
            raise RelayError("account is disabled in the panel: " + alias, 403)
        if self.account_suspended(alias):
            raise RelayError("account is suspended; contact support: " + alias, 403)
        if not self._quota_ok(alias, model) or self._blocked_windows(alias, model):
            raise self._spent_allowance_error(model, [alias])
        if self.account_health(alias).get("status") == 401:
            raise RelayError("account must be logged in again: " + alias, 401)
        if self.account_unhealthy(alias) and self.account_health(alias).get("status") != 503:
            raise RelayError("account is temporarily suspended: " + alias, 403)
        if self.exhausted_cooldown(alias, model or "") > 0:
            raise RelayError("account is cooling down after an upstream 429", 429)
        return alias

    def _reset_at(self, alias: str) -> Optional[float]:
        return self._number((self._windows(alias).get("7d") or {}).get("reset_at"))

    def _reset_rank(self, alias: str) -> float:
        reset = self._reset_at(alias)
        if reset is None or reset <= time.time():
            return URGENCY_HORIZON_HOURS
        hours = (reset - time.time()) / 3600.0
        return min(URGENCY_HORIZON_HOURS, (hours // RESET_BAND_HOURS) * RESET_BAND_HOURS)

    def _fable_spent(self, alias: str) -> float:
        return self._window_utilization(self._windows(alias).get(FABLE_WINDOW)) or 0.0

    def _assignment_key(self, alias: str, model: Optional[str]):
        return (self._reset_rank(alias), -self._fable_spent(alias),
                self._last_assigned.get(alias, 0.0))

    def pick_account(self, requested: str, model: Optional[str] = None, *,
                     allow_unhealthy: bool = False) -> str:
        if (requested or "").strip():
            alias = self._explicit_account(requested.strip(), model)
            if self.account_unhealthy(alias):
                raise RelayError("account is unhealthy: " + alias, 503)
            return alias
        aliases = self.store.aliases()
        if not aliases:
            raise RelayError("no account configured; add one via WebUI or CLI first", 400)
        eligible = self._serviceable if allow_unhealthy else self._selectable
        candidates = [alias for alias in aliases if eligible(alias, model)]
        if not candidates:
            raise self._no_selectable_error(model)
        with self._session_lock:
            chosen = self.default_account if self.default_account in candidates else min(
                candidates, key=lambda alias: self._assignment_key(alias, model))
            self._last_assigned[chosen] = time.time()
        return chosen

    def pick_catalog_account(self, requested: str) -> str:
        """Roster reads cost no quota but cannot bypass disabled/health gates."""
        requested = (requested or "").strip()
        if requested:
            alias = alias_value(requested)
            self.store.row(alias)
            # An explicitly pinned account is refused as clearly as a model
            # request would refuse it, rather than silently 503.
            if self.account_disabled(alias):
                raise RelayError("account is disabled in the panel: " + alias, 403)
            if self.account_health(alias).get("status") == 401:
                raise RelayError("account must be logged in again: " + alias, 401)
            return alias
        aliases = self.store.aliases()
        candidates = [alias for alias in aliases
                      if not self.account_disabled(alias) and not self.account_unhealthy(alias)]
        if not candidates:
            raise RelayError("no healthy enabled account for model catalog", 503)
        return self.default_account if self.default_account in candidates else candidates[0]

    def _sticky_account(self, key: str, aliases: list[str],
                        model: Optional[str] = None) -> str:
        candidates = [alias for alias in aliases if self._selectable(alias, model)]
        if not candidates:
            raise self._no_selectable_error(model, aliases)
        with self._session_lock:
            now = time.time()
            self._prune_sessions(now)
            entry = self._sessions.get(key)
            if key and entry and entry["account"] in candidates:
                chosen = entry["account"]
            elif self.default_account in candidates:
                chosen = self.default_account
            else:
                chosen = min(candidates, key=lambda alias: self._assignment_key(alias, model))
            if not key or not entry or entry["account"] != chosen:
                self._last_assigned[chosen] = now
            if key:
                self._sessions[key] = {"account": chosen, "last": now, "model": model}
            return chosen

    def route_account(self, requested: str, session_hint: str, payload: Any, *,
                       exclude: Optional[set[str]] = None) -> str:
        """Choose a candidate, not permission to send with missing/stale limits.

        Model endpoints must execute via ``with_account_failover`` to perform
        the successful limits preflight and recheck before sending anything.
        """
        model = payload.get("model") if isinstance(payload, dict) else None
        model = model if isinstance(model, str) else None
        if (requested or "").strip():
            return self._explicit_account(requested.strip(), model)
        aliases = self.store.aliases()
        if not aliases:
            raise RelayError("no account configured; add one via WebUI or CLI first", 400)
        aliases = [alias for alias in aliases if alias not in (exclude or ())]
        key = (session_hint or "").strip() or self._session_key_from_payload(payload)
        return self._sticky_account(key, aliases, model)

    # --- account-level failover ---------------------------------------------

    @staticmethod
    def _is_account_exhausted(exc: RelayError) -> bool:
        return exc.status == 429

    @staticmethod
    def _is_credit_exhausted(exc: RelayError) -> bool:
        return credit_exhausted_429(exc.status, exc.data)

    @staticmethod
    def _exhausted_window(exc: RelayError) -> str:
        error = exc.data.get("error") if isinstance(exc.data, dict) else None
        if not isinstance(error, dict):
            return ""
        code = str(error.get("code") or error.get("type") or "")
        window = code.removeprefix("credit_exhausted_")
        return window if window in WINDOW_SECONDS else "shared"

    def _quota_cooldown(self, alias: str, exc: RelayError) -> float:
        window = self._exhausted_window(exc)
        windows = self._windows(alias)
        names = [window] if window in WINDOW_SECONDS else list(WINDOW_SECONDS)
        resets = [self._number((windows.get(name) or {}).get("reset_at")) for name in names]
        waits = [reset - time.time() for reset in resets if reset is not None and reset > time.time()]
        # No hourly recovery probes. A known reset is honored exactly, even if
        # imminent; without one, wait the named window's full duration.
        return max(waits) if waits else WINDOW_SECONDS.get(window, WINDOW_SECONDS["7d"])

    def note_account_unserviceable(self, alias: str, exc: RelayError) -> bool:
        if exc.status != 429:
            return self.note_account_error(alias, exc)
        credit = self._is_credit_exhausted(exc)
        window = self._exhausted_window(exc) if credit else ""
        cooldown = self._quota_cooldown(alias, exc) if credit else TRANSIENT_429_COOLDOWN
        scoped = self._exhausted_until.setdefault(alias, {})
        scoped[window] = max(scoped.get(window, 0.0), time.time() + cooldown)
        self.drop_account_sessions(alias, window)
        return True

    async def with_account_failover(
            self, requested: str, session_hint: str, payload: Any,
            run: Callable[[str], Awaitable[Any]], *,
            model_only: bool = True) -> tuple[str, Any]:
        requested = (requested or "").strip()
        model = payload.get("model") if isinstance(payload, dict) else None
        model = model if isinstance(model, str) else None
        original_model = model
        tried: set[str] = set()
        last: Optional[RelayError] = None
        while True:
            if original_model is not None:
                payload["model"] = original_model
            try:
                account = self.route_account(requested, session_hint, payload, exclude=tried)
            except RelayError as exc:
                if exc.status == 429 or last is None:
                    raise
                raise last from exc
            tried.add(account)
            try:
                if not model_only and self.account_unhealthy(account):
                    raise RelayError("account is unhealthy: " + account, 503)
                await self.refresh_limits_if_stale(account)
                # The await may race a panel disable, login, quota update or
                # another request's refusal. Recheck everything before work.
                if requested:
                    self._explicit_account(account, model)
                elif not self._selectable(account, model):
                    raise self._no_selectable_error(model, [account])
                if not self._limits_ready(account, model):
                    raise RelayError("limits unavailable for requested model: " + account, 503)
                if original_model:
                    canonical = await self.with_proxy(
                        account, lambda url: self.accounts.resolve_model(
                            account, original_model, proxy_url=url))
                    payload["model"] = canonical
                    model = canonical
                    # Canonicalization and roster fetching are await points too.
                    if requested:
                        self._explicit_account(account, model)
                    elif not self._selectable(account, model):
                        raise self._no_selectable_error(model, [account])
                    if not self._limits_ready(account, model):
                        raise RelayError("limits unavailable for requested model: " + account, 503)
            except RelayError as exc:
                # A refused read already is the refresh attempt: record its
                # scope but do not force the same failing endpoint again.
                self.note_account_unserviceable(account, exc)
                key = (session_hint or "").strip() or self._session_key_from_payload(payload)
                with self._session_lock:
                    entry = self._sessions.get(key)
                    if entry and entry["account"] == account:
                        del self._sessions[key]
                if requested:
                    raise
                last = exc
                model = original_model
                continue
            try:
                result = await run(account)
            except RelayError as exc:
                if exc.status == 429:
                    try:
                        await self.refresh_limits_if_stale(account, force=True)
                    except RelayError as refresh_exc:
                        # Health was recorded by the refresh wrapper. The
                        # original refusal still supplies the quota scope.
                        logger.info("limits refresh after 429 failed: account=%s status=%s",
                                    account, refresh_exc.status)
                failover = self.note_account_unserviceable(account, exc)
                if requested or not failover:
                    raise
                last = exc
                model = original_model
                continue
            # Neither a returned response nor opening a stream proves that a
            # conversation succeeded. Routes call note_account_healthy only
            # after validating a completed model response. ``model_only`` is
            # retained for count_tokens callers; neither kind auto-recovers.
            return account, result

    def drop_account_sessions(self, alias: str, window: str = "") -> None:
        with self._session_lock:
            stale = [key for key, entry in self._sessions.items()
                     if entry["account"] == alias
                     and self._window_applies(window, entry.get("model"))]
            for key in stale:
                del self._sessions[key]

    def reset_account_runtime(self, alias: str) -> None:
        """Successful re-login clears only login-bound health, not 403/503."""
        alias = alias_value(alias)
        self.drop_account_sessions(alias)
        with self._session_lock:
            self._last_assigned.pop(alias, None)
        self._exhausted_until.pop(alias, None)
        self.accounts.forget_limits(alias)
        if self.account_health(alias).get("status") == 401:
            self.store.clear_account_error(alias)
            try:
                self.store.merge_metadata(alias, {"parked": False, "parked_reason": None,
                                                  "parked_at": None, "park_checked_at": None})
            except RelayError:
                pass

    def remove_account(self, alias: str) -> None:
        alias = alias_value(alias)
        self.store.row(alias)
        self.upstream.reset_device_identity(alias)
        self.upstream.forget_account(alias)
        self.reset_account_runtime(alias)
        self.pending_logins.pop(alias, None)
        self.store.remove(alias)

    # --- fixed-exit execution -----------------------------------------------

    @staticmethod
    def _is_proxy_network_failure(exc: RelayError) -> bool:
        return (exc.status == 502 and isinstance(exc.data, dict)
                and exc.data.get("proxy_network") is True)

    async def with_proxy(self, alias: str,
                         op: Callable[[Optional[str]], Awaitable[Any]]) -> Any:
        proxy = self.pool.for_account(alias)
        try:
            result = await op(proxy_url(proxy))
        except RelayError as exc:
            if self._is_proxy_network_failure(exc):
                self.pool.fail(proxy, "proxy network failure")
            raise
        self.pool.success(proxy)
        return result

    async def with_fixed_proxy(self, alias: str, proxy: dict[str, Any] | str | None,
                               op: Callable[[Optional[str]], Awaitable[Any]]) -> Any:
        if proxy is not None:
            proxy = self.pool.by_id(proxy.get("id") if isinstance(proxy, dict) else proxy)
        return await op(proxy_url(proxy))

    async def with_pending_proxy(
            self, alias: str, op: Callable[[Optional[str]], Awaitable[Any]],
            pinned: dict[str, Any] | str | None = None, direct: bool = False,
            attempts: int = 1) -> tuple[Optional[dict[str, Any]], Any]:
        if direct:
            proxy = None
        elif pinned is not None:
            proxy = self.pool.by_id(pinned.get("id") if isinstance(pinned, dict) else pinned)
        else:
            proxy = self.pool.pending_proxy(alias_value(alias))
        # ``attempts`` is accepted for legacy callers, never used to rotate.
        return proxy, await op(proxy_url(proxy))

    async def open_messages_stream(
            self, alias: str, payload: dict[str, Any], *,
            request_headers: Optional[Mapping[str, str]] = None,
            session_id: str = "", beta: bool = False,
            raw_body: Optional[bytes] = None,
    ) -> tuple[httpx.Response, AsyncExitStack]:
        stack = AsyncExitStack()
        try:
            generation = self.store.account_generation(alias)
            response = await self.with_proxy(
                alias, lambda url: self.upstream.stream_messages(
                    alias, payload, url, request_headers=request_headers,
                    session_id=session_id, beta=beta, raw_body=raw_body))
            response.extensions[ACCOUNT_GENERATION_EXTENSION] = generation
            stack.push_async_callback(response.aclose)
            return response, stack
        except BaseException:
            await stack.aclose()
            raise

    async def open_responses_stream(
            self, alias: str, body: bytes, *,
            request_headers: Optional[Mapping[str, str]] = None,
            session_id: str = "", account_id: str = "",
            query_string: str = "", path: str = RESPONSES_PATH,
    ) -> tuple[httpx.Response, AsyncExitStack]:
        stack = AsyncExitStack()
        try:
            generation = self.store.account_generation(alias)
            response = await self.with_proxy(
                alias, lambda url: self.upstream.stream_responses(
                    alias, body, url, request_headers=request_headers,
                    session_id=session_id, account_id=account_id,
                    query_string=query_string, path=path))
            stack.push_async_callback(response.aclose)
            response.extensions[ACCOUNT_GENERATION_EXTENSION] = generation
            if response.status_code >= 400:
                await response.aread()
                response.extensions["mirofish_body_decoded"] = True
                try:
                    data = response.json()
                except ValueError:
                    data = {"_raw": response.text}
                exc = RelayError("upstream refused", response.status_code, data)
                if exc.status == 429 or self._is_health_refusal(exc):
                    raise exc
            return response, stack
        except BaseException:
            await stack.aclose()
            raise

    @staticmethod
    def _message_text(message: Any) -> str:
        """Concatenate the text of one message; handles a plain string or a
        list of Anthropic/OpenAI content blocks."""
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        parts: list[str] = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
        return "\n".join(parts)


    @classmethod
    def _explicit_conversation_id(cls, payload: dict[str, Any]) -> str:
        """An id the client itself treats as identifying the conversation.

        ``previous_response_id`` is deliberately excluded: it chains turns but
        changes on every one of them, so keying on it would hand each turn of a
        dialogue to a different account.
        """
        for container in ("metadata", "client_metadata"):
            meta = payload.get(container)
            if not isinstance(meta, dict):
                continue
            for key in cls._CONVERSATION_META_KEYS:
                value = meta.get(key)
                if isinstance(value, str) and value.strip():
                    return "uid:" + value.strip()
        # Codex sets prompt_cache_key once per conversation and repeats it on
        # every turn, which is exactly the affinity anchor we want.
        for key in ("prompt_cache_key", "conversation", "conversation_id"):
            value = payload.get(key)
            if isinstance(value, dict):
                value = value.get("id")
            if isinstance(value, str) and value.strip():
                return "uid:" + value.strip()
        return ""


    @classmethod
    def _first_user_text(cls, payload: dict[str, Any]) -> str:
        """Text of the earliest user turn, from an Anthropic or Responses body.

        ``messages`` carries Anthropic/OpenAI chat turns; ``input`` carries the
        Responses item list, which may also be a bare string.  Items without a
        user role (tool output, function calls) are skipped so a conversation
        keeps its key once tools start running.
        """
        for field in ("messages", "input"):
            value = payload.get(field)
            if isinstance(value, str) and value.strip():
                return value
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, dict) and item.get("role") == "user":
                    text = cls._message_text(item)
                    if text:
                        return text
        return ""


    @classmethod
    def _session_key_from_payload(cls, payload: Any) -> str:
        """Derive a key that is stable across the turns of one conversation but
        differs between conversations. Prefer an explicit client id; otherwise
        anchor on the first user message (constant from turn 1, and distinct per
        window). The system prompt is deliberately ignored — it is identical
        across every window of the same client and would collapse them onto one
        account."""
        if not isinstance(payload, dict):
            return ""
        explicit = cls._explicit_conversation_id(payload)
        if explicit:
            return explicit
        text = cls._first_user_text(payload)
        if text:
            return "msg:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        return ""


    def _prune_sessions(self, now: float) -> None:
        ttl = self.settings.session_ttl
        stale = [key for key, entry in self._sessions.items() if now - entry["last"] > ttl]
        for key in stale:
            del self._sessions[key]


    def session_counts(self) -> dict[str, int]:
        """Live (non-expired) session count per account, for dashboard display."""
        with self._session_lock:
            now = time.time()
            self._prune_sessions(now)
            counts: dict[str, int] = {}
            for entry in self._sessions.values():
                counts[entry["account"]] = counts.get(entry["account"], 0) + 1
            return counts


    @classmethod
    def relay_session_id(cls, claude_session: str, session_hint: str,
                         payload: Any, account: str = "") -> str:
        """Stable, non-secret session id for the upstream relay metadata.

        Every caller identity is rewritten per account. The hash is the
        mapping: deterministic, so one conversation on one account keeps one
        upstream session id for as long as it lives, and derived, so there is
        no table to persist or expire.

        The hash is shaped as a v4 UUID rather than a readable prefix: this
        value is sent as both ``x-mirasim-session`` and, for synthesized client
        identities, ``x-claude-code-session-id``, where every official client
        sends a bare UUID.  Determinism is what session affinity needs, and it
        is unchanged; only the encoding differs.

        A caller's own UUID is rewritten too, rather than passed through. It
        identifies the caller's session, not the account's: one client asking
        two accounts — which is what failover does on a quota refusal — would
        otherwise announce the same session id from both, and a real
        installation cannot know another's. Rewriting also keeps an arbitrary
        caller-supplied label out of an upstream header.

        ``account`` is therefore always in the hash. Without it the derivation
        is purely content-based, and two accounts answering the same prompt
        collided exactly.

        The caller's id is lowercased first so the same session written in
        either case does not fork into two upstream sessions.
        """
        key = (claude_session or "").strip().lower() \
            or (session_hint or "").strip() \
            or cls._session_key_from_payload(payload)
        if key:
            digest = hashlib.sha256(
                ("%s\x00%s" % (account, key)).encode("utf-8")).digest()[:16]
            return str(uuid.UUID(bytes=digest, version=4))
        return str(uuid.uuid4())


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


    def record_usage(self, alias: str, model: Optional[str], usage: dict[str, Any],
                     response_headers: dict[str, str],
                     account_generation: Optional[str] = None) -> dict[str, str]:
        """Persist usage/quota metadata and return the outgoing relay headers."""
        current_generation: Optional[str] = None
        try:
            current_generation = self.store.account_generation(alias)
        except RelayError:
            # The stream may finish after the account has been removed. Its
            # usage row is still retained, but it cannot update live metadata.
            pass
        if account_generation is None:
            account_generation = current_generation
        # Only merge the quota values this response actually carried. A
        # headerless response (e.g. the Codex path) must not wipe the
        # utilization cached by /v1/limits probes — a None there reads as
        # "has room" and would put an exhausted account back into rotation.
        quota = {key: value for key, value in quota_headers(response_headers).items()
                 if value is not None}
        if account_generation is None or account_generation == current_generation:
            try:
                self.store.merge_metadata(alias, {"last_usage": usage, "quota": quota,
                                                  "last_model": model}, deep=("quota",))
            except RelayError:
                pass
        if usage:
            try:
                self.store.log_usage(alias, model, usage,
                                     account_generation=account_generation)
            except RelayError:
                pass
        outgoing = {"X-Mirofish-Account": alias_value(alias)}
        if quota.get("7d_utilization"):
            outgoing["X-Mirofish-Quota-7d-Utilization"] = str(quota["7d_utilization"])
        if quota.get("7d_reset_epoch"):
            outgoing["X-Mirofish-Quota-7d-Reset"] = str(quota["7d_reset_epoch"])
        return outgoing
