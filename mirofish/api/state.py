"""Shared application state and the proxy-retry orchestration helpers."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import threading
import time
import uuid
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable, Mapping, Optional

import httpx

from ..accounts import AccountService
from ..config import Settings
from ..errors import RelayError
from ..proxy import ProxyPool
from ..store import (HEALTH_ERROR, HEALTH_PARKED_STATES, HEALTH_SUSPENDED,
                     Store)
from ..upstream import (CREDIT_EXHAUSTED_CODE_PREFIX, RESPONSES_PATH,
                        Upstream, account_overloaded_503, account_scoped_429,
                        account_suspension_403, credit_exhausted_429,
                        quota_headers)
from ..validate import alias_value
from ..vault import make_credential_store

logger = logging.getLogger("mirofish.state")

LOGIN_TTL_SECONDS = 600.0
# A window at or above this is spent, and automatic selection skips the
# account for the models that draw on it. There is no configurable soft
# ceiling below it: an account is used until its window is actually gone, at
# which point the upstream refuses it and the cooldown takes over. Reserving
# headroom would only leave credit unspent at the reset.
QUOTA_EXHAUSTED = 0.999
MAX_NETWORK_PROXY_ATTEMPTS = 4
# Fallback cooldown for a spent window whose reset time we cannot read. The
# refusal holds until the window resets, so re-probing every 10 minutes is
# already generous; the cached `reset_at` is preferred when available.
SHARED_QUOTA_COOLDOWN = 600.0
# Ceiling on a cooldown derived from a window whose reset time we do NOT know.
# A known `reset_at` is used in full instead: the refusal provably lasts until
# then, and an elected account re-reads its window once per LIMITS_TTL_SECONDS,
# so a window that is raised or reset early is picked up from the cache rather
# than by spending an upstream refusal to discover it. Capping a known deadline
# at an hour meant a 7-day window was re-probed hourly forever: 76 accounts
# refused every hour, which is exactly the "repeated rate-limit refusals" the
# upstream suspends accounts for.
MAX_QUOTA_COOLDOWN = 3600.0
# Cooldown for a 429 the relay does not recognize. Those are usually transient
# rate pressure that clears in seconds, so the account only needs to sit out
# long enough for its dropped sessions to land elsewhere; the full cooldown
# would bench a single-account deployment for 10 minutes over one hiccup.
#
# It is deliberately short, which makes correct classification essential: a
# spent window sent back after a minute is refused again, and enough of those
# earn an upstream 403 suspension for "repeated rate-limit refusals".
TRANSIENT_429_COOLDOWN = 60.0

# Upstream refusals that mean "this account cannot serve model traffic at all",
# mapped to how long automatic selection avoids it afterwards.
#
# 401: its credentials or signed session are rejected. That does not heal on
# its own — the account has to be logged in again — so it is parked with no
# retry deadline at all.
#
# 503 `overloaded_error`: the upstream has no capacity for THIS account (the
# message points at a Discord status channel). It does recover eventually, but
# on the order of hours, not minutes: re-probing sooner just burns a request
# and re-parks the account. A day is long enough to be nearly free while still
# guaranteeing the account comes back without anyone watching it.
#
# Every other 503 is deliberately absent: see ``_is_health_refusal``.
#
# 403 is deliberately absent: a rate-limit bench states when access returns and
# that deadline is used verbatim, while an outright suspension ("contact
# support") is recorded as HEALTH_SUSPENDED with no deadline at all. Giving it
# a fallback window is what had 46 banned accounts probing hourly.
HEALTH_RETRY_AFTER = {503: 86400.0}

# Account scheduling: spend the credit that is about to expire, on the account
# that can least use it for anything else.
#
# There is one policy, not a choice of three. The earlier "balanced" mode
# ordered by live session count, which was never the thing that mattered:
# what keeps load even is the window utilization every candidate is already
# filtered and ordered by, so an account that takes more conversations simply
# fills up and sorts behind the rest. Session count only decided which of two
# equally-loaded accounts went next, and reset time answers that better.
#
# Order: the account whose 7-day window resets soonest first, so credit that
# would otherwise expire unused is spent; among accounts resetting at the same
# time, the one whose fable window is fullest, because its general credit is
# all it has left to give while accounts with fable headroom stay free for
# fable traffic.
# The model whose spend is metered against its own weekly window as well.
FABLE_WINDOW = "7d_fable"
# The weekly window every Claude model draws on, fable included. It sits
# between the shared 7d window and the per-family ones: a fable request spends
# 7d, 7d_claude and 7d_fable at once, while opus/sonnet/haiku spend 7d and
# 7d_claude. Any one of them being full refuses the request, so all of them
# have to be weighed.
CLAUDE_WINDOW = "7d_claude"
# The burst window every request draws on, whatever the model. It is the one
# that fills first — the weekly windows have days of room while this one is
# already spent — so leaving it out of the load calculation means scheduling
# keeps electing an account the upstream will refuse with
# credit_exhausted_5h.
BURST_WINDOW = "5h"
# Only a window closing within this many hours is worth ordering by; beyond it
# there is still time to spend the credit at the normal rate, so those accounts
# share one rank and the fable tie-break decides between them.
URGENCY_HORIZON_HOURS = 48.0
# Reset times are grouped into bands this wide. Ordering by the raw timestamp
# would never tie — accounts provisioned together still differ by microseconds
# — so the first key would decide every comparison and the fable tie-break
# would be dead code. An hour is well inside the noise of "about to expire".
RESET_BAND_HOURS = 1.0
# Account ordering in both modes reads the cached /v1/limits windows (the
# fable window has no response header to keep it fresh), so they are refreshed
# in the background rather than on the request path: probing there would put
# an upstream round-trip in front of every new conversation. The probe costs
# no model tokens, and stale numbers only ever cost one extra attempt, since
# the upstream 429 plus failover is what actually stops a request.
#
# How long a cached window is trusted. Nothing polls: the numbers are refreshed
# for the one account a request elects, and only when they are older than this.
# An idle relay therefore makes no upstream calls at all — a 5-minute sweep
# across every account was ~16k calls a day with nobody using it, which is
# both pointless and the kind of traffic that draws rate-limit attention.
LIMITS_TTL_SECONDS = 3600.0
# Subscription profiles (plan tier, expiry, holder name) change on the scale
# of billing periods, so the sweep only re-reads /auth/me + /auth/referral for
# an account whose stored profile is missing (pre-upgrade rows) or a day old.
PROFILE_REFRESH_SECONDS = 86400.0
# httpx response extension used to carry the account generation from request
# start to stream finalization. This keeps an in-flight old-account response
# from being logged under a newly re-used alias.
ACCOUNT_GENERATION_EXTENSION = "mirofish_account_generation"


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
        # Session affinity: a conversation (window) sticks to one account so a
        # single dialogue is never served by alternating accounts. key ->
        # {"account", "last"}; new keys are assigned to the least-loaded account
        # so separate windows spread across accounts instead of round-robining
        # per request.
        self._sessions: dict[str, dict[str, Any]] = {}
        self._last_assigned: dict[str, float] = {}
        self._session_lock = threading.Lock()
        # alias -> epoch until which automatic selection avoids the account
        # (upstream refused it with credit_exhausted_shared).
        # alias -> {window name: epoch until}. Scoped per window so a spent
        # fable allowance does not bench the account for every other model.
        self._exhausted_until: dict[str, dict[str, float]] = {}
        self._limits_task: Optional[asyncio.Task[None]] = None
        self._limits_wake: Optional[asyncio.Event] = None

    async def aclose(self) -> None:
        await self.stop_limits_refresh()
        await self.upstream.aclose()
        await self.pool.aclose()

    # --- background limits refresh -------------------------------------------

    async def refresh_all_limits(self) -> None:
        """Re-probe every selectable account's usage windows, one failure at a
        time. Only a deliberate request runs this — the panel's refresh or a
        settings change — never a timer.

        Scheduling only reads these numbers, so an account that cannot be
        probed keeps its previous values instead of dropping out of the
        ordering. Accounts switched off in the panel are skipped: they never
        take part in automatic selection, so keeping their windows warm would
        contact the upstream for nothing.

        An account the upstream suspended outright is skipped for the same
        reason, and a stronger one: every probe is a guaranteed refusal that
        teaches us nothing (the numbers stay frozen at the moment of the ban),
        and a steady stream of refused requests is exactly what draws
        rate-limit attention. 26 banned accounts would otherwise contribute
        312 doomed calls an hour.
        """
        async def one(alias: str) -> None:
            try:
                await self.with_proxy(
                    alias, lambda url: self.accounts.fetch_limits(alias, proxy_url=url))
            except Exception as exc:  # noqa: BLE001 - one account must not stop the sweep
                logger.debug("limits refresh failed: account=%s %s", alias, exc)
            if not self._profile_stale(alias):
                return
            try:
                await self.with_proxy(
                    alias, lambda url: self.accounts.fetch_status(alias, proxy_url=url))
            except Exception as exc:  # noqa: BLE001 - profile is best-effort here
                logger.debug("profile refresh failed: account=%s %s", alias, exc)

        aliases = [alias for alias in self.store.aliases()
                   if not self.account_disabled(alias)
                   and not self.account_suspended(alias)]
        if aliases:
            await asyncio.gather(*(one(alias) for alias in aliases))

    def _profile_stale(self, alias: str) -> bool:
        """True when the stored subscription profile should be re-read: never
        fetched (accounts saved before profiles existed), or older than
        PROFILE_REFRESH_SECONDS — plan renewals and expiries move the panel's
        tier/expiry display, but only on billing-period timescales."""
        try:
            metadata = json.loads(self.store.row(alias)["metadata_json"])
        except Exception:  # noqa: BLE001 - racing a concurrent account removal
            return False
        if not metadata.get("profile"):
            return True
        checked = metadata.get("checked_at")
        if not isinstance(checked, str) or not checked:
            return True
        try:
            text = checked.strip()
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            parsed = datetime.datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            checked_epoch = parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return True
        return time.time() - checked_epoch >= PROFILE_REFRESH_SECONDS

    def start_limits_refresh(self) -> None:
        """Run the manual/on-demand refresh worker.

        It sleeps until something asks for a sweep — the panel's refresh, a
        settings change — and then makes one pass. There is deliberately no
        interval: an idle relay must not talk to the upstream at all.
        """
        if self._limits_task is not None:
            return
        wake = self._limits_wake = asyncio.Event()

        async def loop() -> None:
            while True:
                await wake.wait()
                # Clear before sweeping so a kick that lands mid-sweep still
                # triggers a fresh pass instead of being swallowed.
                wake.clear()
                try:
                    await self.refresh_all_limits()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the loop must outlive a bad sweep
                    logger.warning("limits refresh sweep failed: %s", exc)

        self._limits_task = asyncio.create_task(loop())

    def kick_limits_refresh(self) -> None:
        """Ask for one sweep now. This is the only thing that starts one."""
        if self._limits_wake is not None:
            self._limits_wake.set()

    def _limits_stale(self, alias: str) -> bool:
        """Whether this account's cached windows are older than the TTL."""
        fetched = (self._windows_envelope(alias) or {}).get("fetched_epoch")
        try:
            return time.time() - float(fetched) >= LIMITS_TTL_SECONDS
        except (TypeError, ValueError):
            return True  # never probed, or unreadable: read it once

    async def refresh_limits_if_stale(self, alias: str, *,
                                      force: bool = False) -> None:
        """Read one account's windows, at most once per TTL.

        Called for the account a request just elected, so the numbers backing
        the next selection are current without anything polling. ``force`` is
        for a 429: the refusal proves the cache was wrong, and the fresh
        `reset_at` is what makes the cooldown match the real window.

        Failure is deliberately silent. The cached numbers stay, and the
        upstream refusal plus failover remains the authority on whether a
        request can be served — a probe that cannot run must not fail the
        request that triggered it.
        """
        if self.account_suspended(alias) or self.account_disabled(alias):
            return
        if not force and not self._limits_stale(alias):
            return
        try:
            await self.with_proxy(
                alias, lambda url: self.accounts.fetch_limits(alias, proxy_url=url))
        except Exception as exc:  # noqa: BLE001 - never fail the caller's request
            logger.info("limits refresh failed: account=%s %s", alias, exc)

    async def stop_limits_refresh(self) -> None:
        task, self._limits_task = self._limits_task, None
        self._limits_wake = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # --- account selection ----------------------------------------------------

    def _windows(self, alias: str) -> dict[str, dict[str, Any]]:
        """Cached per-window usage from the last /v1/limits probe."""
        try:
            metadata = json.loads(self.store.row(alias)["metadata_json"])
        except (RelayError, json.JSONDecodeError):
            return {}
        windows = (metadata.get("limits") or {}).get("windows")
        if not isinstance(windows, list):
            return {}
        return {str(window.get("name")): window for window in windows
                if isinstance(window, dict)}

    def _windows_envelope(self, alias: str) -> dict[str, Any]:
        """The whole cached /v1/limits payload, for its `fetched_epoch`."""
        try:
            metadata = json.loads(self.store.row(alias)["metadata_json"])
        except (RelayError, json.JSONDecodeError):
            return {}
        limits = metadata.get("limits")
        return limits if isinstance(limits, dict) else {}

    @staticmethod
    def _window_utilization(window: Optional[dict[str, Any]]) -> Optional[float]:
        if not isinstance(window, dict):
            return None
        used, budget = window.get("used"), window.get("budget")
        if not isinstance(used, (int, float)) or not isinstance(budget, (int, float)):
            return None
        reset_at = window.get("reset_at")
        if isinstance(reset_at, (int, float)) and reset_at <= time.time():
            # The window has reset since the probe: the cached spend is
            # history, not load, and would bench a freshly refilled account.
            return None
        return (used / budget) if budget > 0 else None

    def _reset_at(self, alias: str) -> Optional[float]:
        """Epoch the 7-day window resets at, or None when it is unknown."""
        reset = (self._windows(alias).get("7d") or {}).get("reset_at")
        try:
            return float(reset) if reset is not None else None
        except (TypeError, ValueError):
            return None

    def _fable_spent(self, alias: str) -> float:
        """How full this account's own fable window is (0.0 when unknown).

        Used by fable-first to rank the accounts that are already inside the
        reset horizon: the fullest fable window first.
        """
        value = self._window_utilization(self._windows(alias).get(FABLE_WINDOW))
        return value if value is not None else 0.0

    @staticmethod
    def _is_fable_model(model: Optional[str]) -> bool:
        return bool(model) and "fable" in model.lower()

    @staticmethod
    def _is_claude_model(model: Optional[str]) -> bool:
        """Whether this model's spend lands in the 7d_claude window.

        The pool also serves gpt-*/kimi-* through the Codex path, which the
        Claude weekly window says nothing about. An unknown model is treated as
        Claude: the Anthropic endpoints are what this window governs, and
        over-weighing an unrecognized id only makes selection more cautious,
        whereas under-weighing it routes a request that cannot succeed.
        """
        if not model:
            return True
        lowered = model.lower()
        return not (lowered.startswith("gpt-") or lowered.startswith("kimi-"))

    def _load(self, alias: str, model: Optional[str]) -> float:
        """How full this account is for the requested model.

        Every request draws on the 5h burst window and the shared 7d window.
        A Claude model additionally draws on 7d_claude, and a fable model on
        7d_fable on top of that. The spend lands on all of them at once, so the
        tightest one decides: an account whose burst window is spent cannot
        serve the request no matter how much weekly credit it still has, and a
        fable request needs headroom in all four.
        """
        windows = self._windows(alias)
        names = self._relevant_windows(model)
        loads = [value for value in
                 (self._window_utilization(windows.get(name)) for name in names)
                 if value is not None]
        return max(loads) if loads else 0.0

    def _relevant_windows(self, model: Optional[str]) -> list[str]:
        """Every usage window a request for ``model`` spends, tightest family
        first so a refusal names the most specific one."""
        names = [BURST_WINDOW, "7d"]
        if self._is_claude_model(model):
            names.append(CLAUDE_WINDOW)
            if self._is_fable_model(model):
                names.append(FABLE_WINDOW)
        return names

    def _quota_ok(self, alias: str, model: Optional[str] = None) -> bool:
        """Whether every window this model draws on still has credit.

        The cached /v1/limits windows are the model-aware source — a fable
        request also spends the model's own weekly window, and skipping that
        check is how a 7d_fable window ends up at 130%. The header-fed scalar
        still covers the 7d window between reads, since every response
        refreshes it. No usable data means the account is assumed to have
        room; the upstream 429 stays the final authority either way.
        """
        if self._load(alias, model) >= QUOTA_EXHAUSTED:
            return False
        try:
            quota = json.loads(self.store.row(alias)["metadata_json"]).get("quota", {})
            utilization = quota.get("7d_utilization")
            if utilization is None:
                return True
            reset = quota.get("7d_reset_epoch")
            if reset is not None and float(reset) <= time.time():
                return True  # that window has since reset; the number is history
            return float(utilization) < QUOTA_EXHAUSTED
        except (RelayError, ValueError, TypeError, json.JSONDecodeError):
            return True

    def account_disabled(self, alias: str) -> bool:
        try:
            metadata = json.loads(self.store.row(alias)["metadata_json"])
            return bool(metadata.get("disabled"))
        except (RelayError, json.JSONDecodeError):
            return False

    def account_health(self, alias: str) -> dict[str, Any]:
        """Persisted health record, or an empty dict when the account is fine."""
        try:
            metadata = json.loads(self.store.row(alias)["metadata_json"])
        except (RelayError, json.JSONDecodeError):
            return {}
        health = metadata.get("health")
        return health if isinstance(health, dict) else {}

    def account_suspended(self, alias: str) -> bool:
        """The upstream suspended this account outright ("contact support").

        Distinct from ``account_unhealthy``: that covers refusals which clear
        on their own or on the next success, while this one only support lifts.
        Nothing may contact the upstream on its behalf until then — not the
        limits sweep, not the model catalog — because every such call is a
        certain refusal.
        """
        return self.account_health(alias).get("state") == HEALTH_SUSPENDED

    def account_unhealthy(self, alias: str) -> bool:
        """True while a recorded refusal still keeps the account out of
        automatic selection.

        A record with no ``retry_at`` never expires on its own (401: the
        credentials have to be replaced). One with a deadline stops parking the
        account once that passes, so a capacity outage heals without anyone
        watching. Either way an explicit request that succeeds clears it
        immediately — that is the panel's playground path.
        """
        health = self.account_health(alias)
        state = health.get("state")
        if state not in HEALTH_PARKED_STATES:
            return False
        if state == HEALTH_SUSPENDED:
            # Only support lifts this; no timer may put it back in rotation.
            return True
        retry_at = health.get("retry_at")
        if retry_at is None:
            # A record written before retry deadlines existed. Treat it by the
            # rule for its status rather than as "never": a 503 written then is
            # the same capacity outage that now expires on its own, and leaving
            # those parked forever is exactly the bug the deadline fixes.
            return not self._retry_deadline_elapsed(health)
        try:
            return time.time() < float(retry_at)
        except (TypeError, ValueError):
            # An unreadable deadline must not strand the account forever.
            return False

    @staticmethod
    def _retry_deadline_elapsed(health: dict[str, Any]) -> bool:
        """Whether a legacy record (no ``retry_at``) is already due a retry.

        Derived from when it was recorded plus the status's window, so an old
        503 is retried a day after it was parked, not a day after the upgrade.
        """
        window = HEALTH_RETRY_AFTER.get(health.get("status"))
        if not window:
            return False
        try:
            marked = datetime.datetime.fromisoformat(str(health.get("at", "")))
        except (ValueError, TypeError):
            return True  # undatable: retry now rather than park forever
        if marked.tzinfo is None:
            marked = marked.replace(tzinfo=datetime.timezone.utc)
        return time.time() >= marked.timestamp() + window

    def health_retry_in(self, alias: str) -> Optional[float]:
        """Seconds until a parked account is retried, or None when never."""
        health = self.account_health(alias)
        state = health.get("state")
        if state not in HEALTH_PARKED_STATES or state == HEALTH_SUSPENDED:
            return None
        retry_at = health.get("retry_at")
        if retry_at is None:
            window = HEALTH_RETRY_AFTER.get(health.get("status"))
            if not window:
                return None
            # Legacy record: report the remaining part of its own window.
            return 0.0 if self._retry_deadline_elapsed(health) else window
        try:
            return max(0.0, float(retry_at) - time.time())
        except (TypeError, ValueError):
            return None

    def exhausted_cooldown(self, alias: str,
                           model: Optional[str] = None) -> float:
        """Seconds left in this account's quota cooldown (0 = serviceable).

        A cooldown is scoped to the window the upstream named: exhausting
        ``7d_fable`` only blocks fable models, and the upstream says so
        outright ("other models still work"). Benching the whole account there
        is what emptied the pool — every account has a spent fable window long
        before its 7d window is gone, so opus traffic lost 81 of 82 accounts
        over a refusal that never applied to it.

        ``model=None`` asks the account-wide question (panel display, health
        checks) and reports the longest cooldown in force.
        """
        now = time.time()
        scoped = self._exhausted_until.get(alias)
        if not scoped:
            return 0.0
        if model is None:
            return max([0.0, *(until - now for until in scoped.values())])
        return max([0.0, *(until - now for window, until in scoped.items()
                           if self._window_applies(window, model))])

    def _window_applies(self, window: str, model: Optional[str]) -> bool:
        """Whether a request for ``model`` spends this usage window.

        The empty window is a refusal that named none, so it counts against
        everything. A family window only counts against the models that draw
        on it: 7d_fable against fable models, 7d_claude against every Claude
        model, neither against the gpt-*/kimi-* ids served over Codex.
        """
        if not window:
            return True
        return window in self._relevant_windows(model)

    def _selectable(self, alias: str, model: Optional[str] = None) -> bool:
        """Eligible for automatic selection: not switched off in the panel and
        neither cooling down for this model's windows nor parked by a
        401/403/503. Quota load is a soft preference handled separately; these
        are hard exclusions."""
        return self._serviceable(alias, model) and not self.account_unhealthy(alias)

    def _serviceable(self, alias: str, model: Optional[str] = None) -> bool:
        """Usable for a zero-cost control-plane read.

        Same as ``_selectable`` minus the health verdict: a 401/503 refusal
        stops model traffic, but the account can still answer the model
        catalog, and a parked account must not take the panel down with it.

        A suspended account is the exception: the upstream refuses everything
        from it, catalog reads included, so electing it here just spends a
        certain refusal.
        """
        return (not self.account_disabled(alias)
                and not self.account_suspended(alias)
                and self.exhausted_cooldown(alias, model) <= 0.0)

    def _explicit_account(self, requested: str) -> str:
        """An explicitly requested account is honored even during a cooldown
        (the caller may know the quota reset), but never when switched off."""
        alias = alias_value(requested)
        if self.account_disabled(alias):
            raise RelayError("account is disabled in the panel: " + alias, 403)
        return alias

    def _no_selectable_error(self, model: Optional[str] = None) -> RelayError:
        """Why no account can take this request.

        When every account is merely cooling down on the window this model
        spends, the honest answer is the upstream's own verdict: the allowance
        is gone until it resets. Answering it here matters — sending the
        request anyway would earn one more refusal per attempt, and enough of
        those suspend the account for a day. A shorter model keeps working, so
        the refusal names the window rather than claiming the relay is down.
        """
        window = self._pool_exhausted_window(model)
        if window:
            resets_in = self._pool_cooldown(model)
            return RelayError(
                "every account has spent its %s allowance; it comes back when "
                "the window resets%s. Other models are unaffected." % (
                    window,
                    " (about %d min)" % round(resets_in / 60) if resets_in else ""),
                429, {"error": {
                    "type": "rate_limit_error",
                    "code": "credit_exhausted_" + window,
                    "message": "every account has spent its %s allowance; "
                               "switch models or wait for the window to reset"
                               % window}})
        return RelayError("all accounts are disabled or cooling down after a "
                          "shared-quota refusal; enable one in the panel or retry later", 503)

    def _pool_exhausted_window(self, model: Optional[str]) -> str:
        """The spent allowance to report when no account can take this request.

        Every otherwise-usable account must be held back by a spent window this
        model draws on — if even one is free for another reason, the pool is
        not out of allowance and saying so would be a lie. Accounts commonly
        sit on different windows (some spent `7d`, most spent `7d_fable`), so
        report the one that holds back the most of them rather than demanding
        they all match; the client only needs to know which allowance to wait
        on. An unnamed window ("") cannot be reported and disqualifies the
        whole answer.
        """
        counts: dict[str, int] = {}
        now = time.time()
        for alias in self.store.aliases():
            if self.account_disabled(alias) or self.account_unhealthy(alias):
                continue
            scoped = self._exhausted_until.get(alias) or {}
            live = [window for window, until in scoped.items()
                    if until > now and self._window_applies(window, model)]
            if not live:
                return ""  # this account could have served it
            if not all(live):
                return ""  # an unscoped refusal: no allowance to name
            for window in live:
                counts[window] = counts.get(window, 0) + 1
        if not counts:
            return ""
        return max(counts, key=lambda window: (counts[window], window))

    def _pool_cooldown(self, model: Optional[str]) -> float:
        """Shortest wait until some account can serve this model again."""
        waits = [self.exhausted_cooldown(alias, model)
                 for alias in self.store.aliases()
                 if not self.account_disabled(alias)
                 and not self.account_unhealthy(alias)]
        return min(waits) if waits else 0.0

    def _last_resort(self, serviceable: list[str],
                     model: Optional[str]) -> list[str]:
        """What to do when no account is under the exhaustion mark.

        Serving anyway is right when the numbers are merely stale or missing —
        the upstream stays the final authority. It is wrong when the cache
        positively says every window this model spends is used up: the request
        cannot succeed, and sending it costs one upstream refusal per attempt.
        Repeating that is what suspends an account for a day, and with 76
        accounts over their fable budget it meant 76 refusals an hour forever.
        """
        if all(self._load(alias, model) >= QUOTA_EXHAUSTED
               for alias in serviceable):
            raise self._spent_allowance_error(model, serviceable)
        return serviceable

    def _spent_allowance_error(self, model: Optional[str],
                               aliases: list[str]) -> RelayError:
        """The upstream's own verdict, answered locally: this model's allowance
        is gone on every account until its window resets."""
        window = self._tightest_window(model, aliases)
        resets_in = min((self._window_reset_in(alias, window)
                         for alias in aliases), default=0.0)
        return RelayError(
            "every account has spent its %s allowance; it comes back when the "
            "window resets%s. Other models are unaffected." % (
                window,
                " (about %d h)" % round(resets_in / 3600) if resets_in else ""),
            429, {"error": {
                "type": "rate_limit_error",
                "code": "credit_exhausted_" + window,
                "message": "every account has spent its %s allowance; switch "
                           "models or wait for the window to reset" % window}})

    def _tightest_window(self, model: Optional[str],
                         aliases: list[str]) -> str:
        """The window that is spent on the most accounts, which is the one a
        caller has to wait on."""
        counts: dict[str, int] = {}
        names = self._relevant_windows(model)
        for alias in aliases:
            windows = self._windows(alias)
            for name in names:
                value = self._window_utilization(windows.get(name))
                if value is not None and value >= QUOTA_EXHAUSTED:
                    counts[name] = counts.get(name, 0) + 1
        if not counts:
            return names[-1]
        # Break a tie towards the most specific family: with 7d and 7d_fable
        # equally spent, the fable allowance is the one the caller has to wait
        # on, and it is the one that frees up other models by resetting.
        return max(counts, key=lambda name: (counts[name], names.index(name)))

    def _window_reset_in(self, alias: str, window: str) -> float:
        reset_at = (self._windows(alias).get(window) or {}).get("reset_at")
        try:
            return max(0.0, float(reset_at) - time.time()) if reset_at else 0.0
        except (TypeError, ValueError):
            return 0.0

    def pick_account(self, requested: str, model: Optional[str] = None, *,
                     allow_unhealthy: bool = False) -> str:
        """Explicit header > default account > quota-aware round-robin.

        ``allow_unhealthy`` keeps accounts parked by a 401/503 in the running
        for zero-cost catalog reads: those refusals are about serving model
        traffic, and refusing `/v1/models` because every account is parked
        would take the panel down exactly when it is needed to recover one.
        """
        requested = requested.strip()
        if requested:
            return self._explicit_account(requested)
        aliases = self.store.aliases()
        if not aliases:
            raise RelayError("no account configured; add one via WebUI or CLI first", 400)
        base = self._serviceable if allow_unhealthy else self._selectable
        def eligible(alias: str) -> bool:
            return base(alias, model)
        if self.default_account in aliases and eligible(self.default_account):
            return self.default_account
        selectable = [alias for alias in aliases if eligible(alias)]
        if not selectable:
            raise self._no_selectable_error(model)
        # No session key to stay stable for, but the ordering is the same one a
        # keyed request gets: expiring credit first. Round-robin used to live
        # here instead, which spent whichever account happened to be next
        # rather than the one about to lose its credit.
        eligible_now = [alias for alias in selectable
                        if self._quota_ok(alias, model)] \
            or self._last_resort(selectable, model)
        with self._rr_lock:
            chosen = min(eligible_now,
                         key=lambda alias: self._assignment_key(alias, model))
            self._last_assigned[chosen] = time.time()
            return chosen

    # --- session-affinity routing --------------------------------------------

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

    #: Free-form metadata keys that clients use for a per-conversation id.  The
    #: Responses API declares ``metadata`` as an open string map, so there is no
    #: single official name to key on.
    _CONVERSATION_META_KEYS = ("user_id", "conversation_id", "thread_id",
                               "session_id")

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

    def _assignment_key(self, alias: str, model: Optional[str]):
        """Ordering for a new conversation; lowest wins.

        1. 7-day reset time, soonest first: that credit is about to expire
           unused, while every other account still has a week to spend its own
           at the normal rate.
        2. Then how spent the account's fable window is, fullest first. Its
           general credit is all it has left to give, whereas an account with
           fable headroom is worth keeping free for fable traffic. A fable
           request needs no special case: `_load` weighs the fable window, so a
           full one is already excluded by ``_quota_ok``.
        3. Least-recently-assigned breaks a remaining tie, which keeps a pool
           of identical fresh accounts fanning out instead of piling onto
           whichever alias sorts first.

        No session count and no configurable ceiling. An account is used until
        its window is actually spent, and ``_quota_ok`` then removes it — a
        soft ceiling below that would only leave credit unspent at the reset,
        which is the thing this ordering exists to avoid.
        """
        return (self._reset_rank(alias), -self._fable_spent(alias),
                self._last_assigned.get(alias, 0.0))

    def _reset_rank(self, alias: str) -> float:
        """Which urgency band this account's 7-day window falls in; lower first.

        Deliberately a band, not the timestamp. Raw reset times never tie —
        they differ by microseconds even for accounts provisioned together — so
        ordering by them directly would decide every comparison on the first
        key and make the fable tie-break dead code. Rounding to
        ``RESET_BAND_HOURS`` groups accounts that expire at practically the
        same time and lets the next key choose between them.

        Beyond ``URGENCY_HORIZON_HOURS`` the exact time stops mattering at all:
        there is still time to spend the credit at the normal rate, so those
        accounts share the last band. An account with no cached window sorts
        with them rather than jumping the queue on missing data.
        """
        reset_at = self._reset_at(alias)
        if reset_at is None:
            return URGENCY_HORIZON_HOURS
        hours = max(0.0, (reset_at - time.time()) / 3600.0)
        if hours >= URGENCY_HORIZON_HOURS:
            return URGENCY_HORIZON_HOURS
        return (hours // RESET_BAND_HOURS) * RESET_BAND_HOURS

    def _sticky_account(self, key: str, aliases: list[str],
                        model: Optional[str] = None) -> str:
        with self._session_lock:
            now = time.time()
            self._prune_sessions(now)
            entry = self._sessions.get(key)
            if entry and entry["account"] in aliases and self._selectable(entry["account"]) \
                    and self._quota_ok(entry["account"], model) \
                    and self.exhausted_cooldown(entry["account"], model) <= 0.0:
                entry["last"] = now
                entry["model"] = model
                return entry["account"]
            # New conversation: order the eligible accounts by expiring credit.
            serviceable = [alias for alias in aliases if self._selectable(alias, model)]
            if not serviceable:
                raise self._no_selectable_error(model)
            eligible = [alias for alias in serviceable
                        if self._quota_ok(alias, model)]
            if not eligible:
                eligible = self._last_resort(serviceable, model)
            chosen = min(eligible,
                         key=lambda alias: self._assignment_key(alias, model))
            self._sessions[key] = {"account": chosen, "last": now, "model": model}
            self._last_assigned[chosen] = now
            return chosen

    def session_counts(self) -> dict[str, int]:
        """Live (non-expired) session count per account, for dashboard display."""
        with self._session_lock:
            now = time.time()
            self._prune_sessions(now)
            counts: dict[str, int] = {}
            for entry in self._sessions.values():
                counts[entry["account"]] = counts.get(entry["account"], 0) + 1
            return counts

    def route_account(self, requested: str, session_hint: str, payload: Any) -> str:
        """Account selection for a model request.

        Order: explicit `X-Mirofish-Account` > configured default account >
        session affinity (same conversation -> same account, new conversation ->
        least-loaded account) > quota-aware round-robin when no session key can
        be derived.
        """
        requested = (requested or "").strip()
        if requested:
            return self._explicit_account(requested)
        aliases = self.store.aliases()
        if not aliases:
            raise RelayError("no account configured; add one via WebUI or CLI first", 400)
        key = (session_hint or "").strip() or self._session_key_from_payload(payload)
        model = payload.get("model") if isinstance(payload, dict) else None
        model = model if isinstance(model, str) else None
        if self.default_account in aliases \
                and self._selectable(self.default_account, model):
            return self.default_account
        if not key:
            return self.pick_account("", model)
        return self._sticky_account(key, aliases, model)

    # --- account-level failover -----------------------------------------------

    @staticmethod
    def _is_account_exhausted(exc: RelayError) -> bool:
        """The upstream refused to serve this ACCOUNT rather than this exit.

        Treating every account-scoped 429 this way costs one extra attempt
        when the guess is wrong; not treating it leaves the conversation
        pinned to an account that answers 429 until its window resets,
        because affinity keeps routing the client's retry straight back to it.
        """
        return account_scoped_429(exc.status, exc.data)

    @staticmethod
    def _is_credit_exhausted(exc: RelayError) -> bool:
        """A spent usage window, which holds until that window resets — unlike
        other 429 shapes, which are usually transient rate pressure."""
        return credit_exhausted_429(exc.status, exc.data)

    @staticmethod
    def _exhausted_window(exc: RelayError) -> str:
        """Which usage window the upstream says is spent, from the error code.

        ``credit_exhausted_7d_fable`` -> ``7d_fable``. An empty string means the
        refusal did not name one, in which case it has to be treated as
        covering the whole account.
        """
        error = exc.data.get("error") if isinstance(exc.data, dict) else None
        code = str((error or {}).get("code") or "")
        prefix = CREDIT_EXHAUSTED_CODE_PREFIX + "_"
        window = code[len(prefix):] if code.startswith(prefix) else ""
        # "shared" is the generic spelling, not a window in /v1/limits.
        return "" if window == "shared" else window

    def _quota_cooldown(self, alias: str, exc: RelayError) -> float:
        """How long to bench an account whose window the upstream says is spent.

        The refusal lasts until that window resets, and the error names which
        window it was (``credit_exhausted_5h`` -> the 5h window), so the cached
        `reset_at` gives a cooldown that actually matches the refusal instead of
        a fixed guess. Capped by ``MAX_QUOTA_COOLDOWN`` so a multi-day 7-day
        window still gets re-probed, and floored by ``SHARED_QUOTA_COOLDOWN``
        so a reset that is seconds away does not put the account straight back
        into rotation to be refused again.
        """
        reset_at = (self._windows(alias).get(
            self._exhausted_window(exc)) or {}).get("reset_at")
        try:
            remaining = float(reset_at) - time.time() if reset_at else 0.0
        except (TypeError, ValueError):
            remaining = 0.0
        if remaining > 0:
            # A known deadline is used in full. Capping it meant a 7-day window
            # was re-probed every hour for days, spending one refusal per
            # account each time; an elected account re-reads its window once
            # per LIMITS_TTL_SECONDS, so an early reset is noticed there
            # instead.
            return max(SHARED_QUOTA_COOLDOWN, remaining)
        return MAX_QUOTA_COOLDOWN

    def drop_account_sessions(self, alias: str, window: str = "") -> None:
        """Detach live sessions pinned to an account so each conversation's next
        turn is reassigned instead of repeating a failing or disabled account.

        ``window`` narrows this to the conversations a scoped cooldown actually
        covers: a spent fable allowance must not tear down the account's opus
        conversations, which keep working. The default drops everything, which
        is what a whole-account refusal (401/403/503, an unscoped 429) needs.
        """
        with self._session_lock:
            stale = [key for key, entry in self._sessions.items()
                     if entry["account"] == alias
                     and self._window_applies(window, entry.get("model"))]
            for key in stale:
                del self._sessions[key]

    def reset_account_runtime(self, alias: str) -> None:
        """Clear account-derived routing and catalog state after a login.

        Credentials and device authorization are owned by ``AccountService`` /
        ``Upstream``. This resets the application-level decisions that must not
        leak from the previous login occupying the same alias.
        """
        alias = alias_value(alias)
        self.drop_account_sessions(alias)
        with self._session_lock:
            self._last_assigned.pop(alias, None)
        self.model_cache.pop(alias, None)
        self._exhausted_until.pop(alias, None)
        # New credentials invalidate a recorded 401; a 503 was never about them,
        # but re-probing once after a login is cheaper than staying parked.
        self.note_account_healthy(alias)
        # A successful login may represent a different upstream identity under
        # the same local alias. Region refusals belong to the old identity, but
        # the alias's slot remains valid (and may still carry an in-flight
        # request), so do not release it here.
        self.pool.clear_region_refusals(alias)

    def remove_account(self, alias: str) -> None:
        """Remove an account and every in-memory identity derived from it."""
        alias = alias_value(alias)
        # Validate before mutating runtime state, preserving the existing 404
        # behavior for an unknown alias.
        self.store.row(alias)
        # The device key belongs to this account alone, so it goes with it: a
        # later account reusing the alias must not inherit its identity.
        self.upstream.drop_device_identity(alias)
        self.upstream.forget_account(alias)
        self.reset_account_runtime(alias)
        self.pending_logins.pop(alias, None)
        self.store.remove(alias)
        self.pool.forget_account(alias)

    @staticmethod
    def _is_region_refused_everywhere(exc: RelayError) -> bool:
        """Every available exit region refused this account (flag set by
        `_rotate_after_failure` once the per-account rotation ran out of
        exits). Like a shared-quota refusal, this cannot be fixed by another
        node — only by another account or by waiting."""
        return (isinstance(exc.data, dict)
                and exc.data.get("region_refused_everywhere") is True)

    def note_account_unserviceable(self, alias: str, exc: RelayError) -> bool:
        """Record an account-scoped upstream refusal so automatic selection
        avoids the account for a while. Returns True when the error was one.

        The cooldown length depends on what the refusal was: shared-credit
        exhaustion holds until the window resets, so re-probing every 10
        minutes is enough, while an unrecognized 429 is usually transient
        rate pressure — benching the account (and, with a single account,
        the whole relay) for 10 minutes over one of those would turn a
        seconds-long hiccup into a self-inflicted outage.
        """
        if self._is_account_exhausted(exc):
            if self._is_credit_exhausted(exc):
                cooldown = self._quota_cooldown(alias, exc)
                window = self._exhausted_window(exc)
                reason = "spent %s window" % (window or "usage")
            else:
                cooldown, reason = TRANSIENT_429_COOLDOWN, "account-scoped 429"
                window = ""
        elif self._is_region_refused_everywhere(exc):
            cooldown, reason = SHARED_QUOTA_COOLDOWN, "region refusal from every exit"
            window = ""
        else:
            return self.note_account_error(alias, exc)
        self._exhausted_until.setdefault(alias, {})[window] = time.time() + cooldown
        # Only the traffic this cooldown covers has to be reassigned; a spent
        # fable window must not tear down the account's other conversations.
        self.drop_account_sessions(alias, window)
        logger.warning(
            "account cooling down for %ds after %s: account=%s",
            int(cooldown), reason, alias)
        return True

    @staticmethod
    def _refusal_message(exc: RelayError) -> str:
        """Human-readable upstream reason for the panel's status column."""
        data = exc.data if isinstance(exc.data, dict) else {}
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            kind = error.get("type") or error.get("code")
            if isinstance(message, str) and message.strip():
                return (f"{kind}: {message}" if isinstance(kind, str) and kind
                        else message).strip()
        return str(exc)

    @staticmethod
    def _is_health_refusal(exc: RelayError) -> bool:
        """Whether this refusal is a verdict on the ACCOUNT rather than a fault
        on the way to the upstream.

        401 always is: the credentials or the signed session were rejected.
        403 is when the upstream suspended the account, either temporarily for
        repeated rate-limit refusals or outright pending support.
        503 only is when the upstream says ``overloaded_error``. The relay also
        raises 503 for its own reasons (no device ticket, no usable proxy exit)
        and the edge in front of the upstream serves 503 HTML error pages or
        body-less rejections during a hiccup — none of which say anything about
        the account that happened to carry the request. Parking on those is
        actively harmful: one bad minute at the edge walks the whole pool out
        of rotation for a day, which is exactly what a shared fault should not
        be allowed to do.
        """
        if exc.status == 401:
            return True
        if account_suspension_403(exc.status, exc.data) is not None:
            return True
        return account_overloaded_503(exc.status, exc.data)

    def note_account_error(self, alias: str, exc: RelayError) -> bool:
        """Park an account the upstream refused with 401, a suspension 403, or
        a capacity 503.

        Returns True when the error was one of those, letting the caller fail
        over to another account. How long it stays parked depends on whether
        the refusal heals by itself (see ``HEALTH_RETRY_AFTER``); a successful
        explicitly-pinned request clears it at any point.
        """
        if not self._is_health_refusal(exc):
            return False
        message = self._refusal_message(exc)
        suspension = account_suspension_403(exc.status, exc.data)
        state = HEALTH_ERROR
        if suspension is not None:
            permanent, resumes_at = suspension
            if permanent:
                # "contact support": nothing we do lifts it, so record it as a
                # suspension with no deadline. Retrying on a guessed window is
                # what had 46 banned accounts probing the upstream every hour.
                state, retry_at = HEALTH_SUSPENDED, None
            else:
                # A stated deadline beats any window we could pick: retrying
                # before it is guaranteed to fail, and retrying long after it
                # wastes an account that is already back.
                retry_at = resumes_at
        else:
            window = HEALTH_RETRY_AFTER.get(exc.status)
            retry_at = time.time() + window if window else None
        self.drop_account_sessions(alias)
        try:
            self.store.mark_account_error(alias, exc.status, message,
                                          "upstream_%d" % exc.status,
                                          retry_after=retry_at, state=state)
        except RelayError:
            # The account was deleted mid-request; nothing left to park.
            return True
        if state == HEALTH_SUSPENDED:
            logger.warning(
                "account suspended by the upstream and taken out of rotation; "
                "only support can lift it: account=%s %s", alias, message)
            return True
        held = ("until it is logged in again" if retry_at is None
                else "for %dh" % round(max(0.0, retry_at - time.time()) / 3600.0))
        logger.warning(
            "account parked %s after upstream %s; a successful request from "
            "the playground clears it: account=%s %s",
            held, exc.status, alias, message)
        return True

    def note_account_healthy(self, alias: str) -> None:
        """Clear a recorded refusal once the account serves a request again.

        This covers a suspension too: if the upstream is answering, support has
        evidently lifted it, and a success is stronger evidence than the record.
        Nothing probes on its own, so this only ever follows a real request.
        """
        if not self.account_health(alias):
            return
        try:
            self.store.clear_account_error(alias)
        except RelayError:
            return
        logger.info("account back to normal after a successful request: "
                    "account=%s", alias)

    async def with_account_failover(
            self, requested: str, session_hint: str, payload: Any,
            run: Callable[[str], Awaitable[Any]]) -> tuple[str, Any]:
        """Route an account and run the request, failing over to another account
        when the upstream refuses the chosen one with credit_exhausted_shared or
        parks it with a 401/503. An explicitly requested account is never
        substituted.

        Windows are refreshed *after* the call, for the one account that served
        it (or after a quota refusal, where the refusal proves the cache was
        wrong and the fresh `reset_at` is what makes the cooldown match the
        real window). Never before: a probe in front of the caller's request
        would both delay it and pre-empt the proxy rotation `run` relies on.
        This is the only thing that reads /v1/limits on the request path, and
        it reads one account, so nothing polls.
        """
        requested = (requested or "").strip()
        tried: set[str] = set()
        last: Optional[RelayError] = None
        while True:
            try:
                account = self.route_account(requested, session_hint, payload)
            except RelayError as route_exc:
                # Prefer the actionable upstream refusal over the pool-empty error.
                raise (last or route_exc) from route_exc
            if account in tried:
                raise last if last is not None else RelayError(
                    "account selection returned an already-failed account", 500)
            try:
                result = account, await run(account)
            except RelayError as exc:
                if self._is_credit_exhausted(exc):
                    await self.refresh_limits_if_stale(account, force=True)
                if not self.note_account_unserviceable(account, exc) or requested:
                    raise
                tried.add(account)
                last = exc
                continue
            self.note_account_healthy(account)
            # Refresh after the fact, never before: the windows this spend
            # lands on are now stale, and reading them here keeps the next
            # election accurate without putting a probe in front of the
            # caller's request. A failure here cannot affect the result.
            await self.refresh_limits_if_stale(account)
            return result

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

    @staticmethod
    def _is_proxy_network_failure(exc: RelayError) -> bool:
        return (exc.status == 502 and isinstance(exc.data, dict)
                and exc.data.get("proxy_network") is True)

    @staticmethod
    def _is_region_blocked(exc: RelayError) -> bool:
        return (exc.status == 502 and isinstance(exc.data, dict)
                and exc.data.get("region_blocked") is True)

    def _rotate_after_failure(self, alias: str, proxy: dict[str, Any],
                              exc: RelayError,
                              network_failures: int) -> Optional[dict[str, Any]]:
        """Pick the next exit after a routed failure, or raise `exc`.

        A region refusal moves the account through exits it has not been
        refused from yet — a per-account memory, never a global node failure,
        because whether a region is served depends on the account's upstream
        tier. Once every exit has refused the account, `exc` is annotated so
        account-level failover treats the account as unserviceable. A network
        failure keeps quarantining the node globally (a dead node is dead for
        everyone), capped at MAX_NETWORK_PROXY_ATTEMPTS."""
        if self._is_region_blocked(exc):
            try:
                return self.pool.rotate_region(alias, proxy)
            except RelayError as rotate_exc:
                if rotate_exc.status == 503:
                    if isinstance(exc.data, dict):
                        exc.data["region_refused_everywhere"] = True
                    raise exc from rotate_exc
                raise
        if self._is_proxy_network_failure(exc):
            if network_failures >= MAX_NETWORK_PROXY_ATTEMPTS:
                # The final failed node must also be quarantined; otherwise
                # the next request immediately starts on the same known-bad
                # exit and repeats the rejection loop.
                self.pool.fail(alias, proxy, "proxy network failure")
                raise exc
            try:
                return self.pool.rotate(alias, proxy, "proxy network failure")
            except RelayError as rotate_exc:
                if rotate_exc.status == 503:
                    # rotate() already recorded the final failed exit. Keep
                    # the actionable upstream error instead of replacing it
                    # with the pool's generic "no node" error.
                    raise exc from rotate_exc
                raise
        raise exc

    async def with_proxy(self, alias: str,
                         op: Callable[[Optional[str]], Awaitable[Any]]) -> Any:
        """Run an account operation, rotating away from unusable sticky exits."""
        proxy = await self.pool.for_account(alias)
        network_failures = 0
        while True:
            try:
                async with self.pool.route(alias, proxy) as proxy_url:
                    result = await op(proxy_url)
                self.pool.success(proxy)
                return result
            except RelayError as exc:
                if not proxy:
                    raise
                if self._is_proxy_network_failure(exc):
                    network_failures += 1
                proxy = self._rotate_after_failure(alias, proxy, exc, network_failures)
                if proxy is None:
                    raise

    async def with_fixed_proxy(self, alias: str, proxy: Optional[dict[str, Any]],
                               op: Callable[[Optional[str]], Awaitable[Any]]) -> Any:
        async with self.pool.route(alias, proxy) as proxy_url:
            return await op(proxy_url)

    @staticmethod
    def _is_non_api_response(exc: RelayError) -> bool:
        """The upstream body was not JSON (e.g. an HTML block page from a bad exit)."""
        return exc.status >= 400 and isinstance(exc.data, dict) and "_raw" in exc.data

    async def with_pending_proxy(
            self, alias: str, op: Callable[[Optional[str]], Awaitable[Any]],
            attempts: int = 3) -> tuple[Optional[dict[str, Any]], Any]:
        """Run a pre-login operation, failing over to another node when the
        picked one cannot reach the upstream (network error or a non-API
        response such as an HTML block page). Returns (proxy, result)."""
        # Region serviceability is account-tier dependent. A deliberate new
        # login may replace the identity behind this alias, so it must not be
        # blocked by the previous identity's refusal history. Clear once before
        # the retry loop; refusals learned by this login attempt still guide
        # subsequent rotations below.
        alias = alias_value(alias)
        self.pool.clear_region_refusals(alias)
        for attempt in range(attempts):
            proxy = await self.pool.pending_proxy(alias)
            if proxy is None:
                return None, await op(None)
            try:
                async with self.pool.route(alias, proxy) as proxy_url:
                    result = await op(proxy_url)
                self.pool.success(proxy)
                return proxy, result
            except RelayError as exc:
                region = self._is_region_blocked(exc)
                retriable = (region or self._is_proxy_network_failure(exc)
                             or self._is_non_api_response(exc))
                if not retriable or attempt + 1 >= attempts:
                    raise
                if region:
                    self.pool.mark_region_refused(alias, proxy)
                else:
                    self.store.mark_proxy_failure(str(proxy["id"]), str(exc)[:200])
        raise RelayError("proxy request failed", 502)

    async def open_messages_stream(
            self, alias: str,
            payload: dict[str, Any], *,
            request_headers: Optional[Mapping[str, str]] = None,
            session_id: str = "", beta: bool = False,
            raw_body: Optional[bytes] = None,
    ) -> tuple[httpx.Response, AsyncExitStack]:
        """Open a streaming upstream call inside its proxy route context.

        The returned AsyncExitStack keeps the route (and, for shared mihomo
        slots, its lock) plus the HTTP response open; the caller closes it
        when the stream finishes.
        """
        proxy = await self.pool.for_account(alias)
        network_failures = 0
        while True:
            stack = AsyncExitStack()
            try:
                proxy_url = await stack.enter_async_context(
                    self.pool.route(alias, proxy))
                account_generation = self.store.account_generation(alias)
                response = await self.upstream.stream_messages(
                    alias, payload, proxy_url, request_headers=request_headers,
                    session_id=session_id, beta=beta, raw_body=raw_body)
                response.extensions[ACCOUNT_GENERATION_EXTENSION] = account_generation
                stack.push_async_callback(response.aclose)
                self.pool.success(proxy)
                return response, stack
            except RelayError as exc:
                await stack.aclose()
                if not proxy:
                    raise
                if self._is_proxy_network_failure(exc):
                    network_failures += 1
                proxy = self._rotate_after_failure(alias, proxy, exc, network_failures)
                if proxy is None:
                    raise
            except BaseException:
                await stack.aclose()
                raise

    async def open_responses_stream(
            self, alias: str, body: bytes, *,
            request_headers: Optional[Mapping[str, str]] = None,
            session_id: str = "", account_id: str = "",
            query_string: str = "", path: str = RESPONSES_PATH,
    ) -> tuple[httpx.Response, AsyncExitStack]:
        """Open a Codex relay stream while retaining its proxy route."""
        proxy = await self.pool.for_account(alias)
        network_failures = 0
        while True:
            stack = AsyncExitStack()
            try:
                proxy_url = await stack.enter_async_context(
                    self.pool.route(alias, proxy))
                account_generation = self.store.account_generation(alias)
                response = await self.upstream.stream_responses(
                    alias, body, proxy_url, request_headers=request_headers,
                    session_id=session_id, account_id=account_id,
                    query_string=query_string, path=path)
                response.extensions[ACCOUNT_GENERATION_EXTENSION] = account_generation
                stack.push_async_callback(response.aclose)
                # Unlike the Anthropic path, stream_responses *returns* upstream
                # rejections so the Codex caller sees them verbatim.  Clearing
                # the node's failure counter on one of those would credit an
                # exit that never served a request.
                if response.status_code < 400:
                    self.pool.success(proxy)
                return response, stack
            except RelayError as exc:
                await stack.aclose()
                if not proxy:
                    raise
                if self._is_proxy_network_failure(exc):
                    network_failures += 1
                proxy = self._rotate_after_failure(
                    alias, proxy, exc, network_failures)
                if proxy is None:
                    raise
            except BaseException:
                await stack.aclose()
                raise

    # --- usage accounting ---------------------------------------------------

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
