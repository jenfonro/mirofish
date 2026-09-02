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
from ..store import Store
from ..upstream import (CREDIT_EXHAUSTED_TYPE, RESPONSES_PATH, Upstream,
                        account_scoped_429, quota_headers)
from ..validate import alias_value
from ..vault import make_credential_store

logger = logging.getLogger("mirofish.state")

LOGIN_TTL_SECONDS = 600.0
QUOTA_EXHAUSTED = 0.999
MAX_NETWORK_PROXY_ATTEMPTS = 4
# How long automatic selection avoids an account after the upstream refuses it
# with credit_exhausted_shared. The reset time is unknown to us, so re-probe
# occasionally instead of blacklisting until restart.
SHARED_QUOTA_COOLDOWN = 600.0
# Cooldown for a 429 the relay does not recognize. Those are usually transient
# rate pressure that clears in seconds, so the account only needs to sit out
# long enough for its dropped sessions to land elsewhere; the full cooldown
# would bench a single-account deployment for 10 minutes over one hiccup.
TRANSIENT_429_COOLDOWN = 60.0

# Account scheduling. "balanced" spreads new conversations over the accounts
# carrying the fewest live sessions. "reset_first" instead prefers the account
# whose 7-day window resets soonest, so credit that is about to expire unused
# is spent before it is thrown away; the balanced key stays as the tie-break.
# "fable_first" is for non-fable traffic: among the accounts whose window is
# about to reset it prefers the one whose fable window is fullest, so the
# general 7-day credit is spent on the accounts whose fable credit is already
# gone (a fable request there would be refused anyway) and the accounts with
# fable headroom stay free for fable traffic.
SCHEDULE_BALANCED = "balanced"
SCHEDULE_RESET_FIRST = "reset_first"
SCHEDULE_FABLE_FIRST = "fable_first"
SCHEDULE_MODES = (SCHEDULE_BALANCED, SCHEDULE_RESET_FIRST, SCHEDULE_FABLE_FIRST)
SETTING_SCHEDULE_MODE = "schedule_mode"
SETTING_SCHEDULE_MAX_UTILIZATION = "schedule_max_utilization"
# Above this utilization an account sorts behind every account with room, in
# both schedule modes, so one nearly-spent account does not absorb every new
# conversation. The ceiling is a soft preference; QUOTA_EXHAUSTED is the hard
# skip (a ceiling deliberately set above it raises the skip mark too). Both
# matter because the upstream meters lazily enough that a window kept in
# rotation can be driven far past 100% before a 429 ever lands.
DEFAULT_SCHEDULE_MAX_UTILIZATION = 0.98
# The model whose spend is metered against its own weekly window as well.
FABLE_WINDOW = "7d_fable"
# Reset-first is a tilt on the balanced ordering, not a replacement for it.
# An account is treated as carrying up to this many fewer sessions than it
# really does as its weekly window approaches expiry, so it takes the next few
# conversations and then rejoins the rotation once its real count catches up.
# Keeping the bonus small is deliberate: a large one would hand it every
# conversation and concentrate the concurrency on one account.
URGENCY_MAX_BONUS = 2.0
# Only a window closing within this many hours is worth diverting toward; the
# rest of the week there is time to spend the credit at the normal rate.
URGENCY_HORIZON_HOURS = 48.0
# Account ordering in both modes reads the cached /v1/limits windows (the
# fable window has no response header to keep it fresh), so they are refreshed
# in the background rather than on the request path: probing there would put
# an upstream round-trip in front of every new conversation. The probe costs
# no model tokens, and stale numbers only ever cost one extra attempt, since
# the upstream 429 plus failover is what actually stops a request.
LIMITS_REFRESH_SECONDS = 300.0
# Subscription profiles (plan tier, expiry, holder name) change on the scale
# of billing periods, so the sweep only re-reads /auth/me + /auth/referral for
# an account whose stored profile is missing (pre-upgrade rows) or a day old.
PROFILE_REFRESH_SECONDS = 86400.0


def _is_uuid(value: str) -> bool:
    """True for a canonically formatted UUID, hyphens and all.

    ``uuid.UUID`` also accepts braces, urn: prefixes and bare hex, none of
    which an official client would send, so require the round trip.
    """
    if len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


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
        self._exhausted_until: dict[str, float] = {}
        self._limits_task: Optional[asyncio.Task[None]] = None
        self._limits_wake: Optional[asyncio.Event] = None

    async def aclose(self) -> None:
        await self.stop_limits_refresh()
        await self.upstream.aclose()
        await self.pool.aclose()

    # --- background limits refresh -------------------------------------------

    async def refresh_all_limits(self) -> None:
        """Re-probe every selectable account's usage windows, one failure at a
        time.

        Scheduling only reads these numbers, so an account that cannot be
        probed keeps its previous values instead of dropping out of the
        ordering. Accounts switched off in the panel are skipped: they never
        take part in automatic selection, so keeping their windows warm would
        contact the upstream for nothing.
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
                   if not self.account_disabled(alias)]
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
        """Keep the cached windows warm: both schedule modes read them to keep
        exhausted windows out of automatic selection, and the fable window has
        no response header that could refresh it between probes."""
        if self._limits_task is not None:
            return
        wake = self._limits_wake = asyncio.Event()

        async def loop() -> None:
            while True:
                # Clear before sweeping so a kick that lands mid-sweep still
                # triggers a fresh pass instead of being swallowed.
                wake.clear()
                try:
                    await self.refresh_all_limits()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the loop must outlive a bad sweep
                    logger.warning("limits refresh sweep failed: %s", exc)
                try:
                    await asyncio.wait_for(wake.wait(), LIMITS_REFRESH_SECONDS)
                except TimeoutError:
                    pass

        self._limits_task = asyncio.create_task(loop())

    def kick_limits_refresh(self) -> None:
        """Sweep now instead of waiting out the interval (e.g. right after
        reset-first is switched on, when the cached windows may be days old)."""
        if self._limits_wake is not None:
            self._limits_wake.set()

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

    def schedule_settings(self) -> dict[str, Any]:
        mode = self.store.setting(SETTING_SCHEDULE_MODE, SCHEDULE_BALANCED)
        if mode not in SCHEDULE_MODES:
            mode = SCHEDULE_BALANCED
        try:
            ceiling = float(self.store.setting(
                SETTING_SCHEDULE_MAX_UTILIZATION,
                str(DEFAULT_SCHEDULE_MAX_UTILIZATION)))
        except ValueError:
            ceiling = DEFAULT_SCHEDULE_MAX_UTILIZATION
        return {"mode": mode, "max_utilization": ceiling}

    def set_schedule_settings(self, mode: str, max_utilization: float) -> dict[str, Any]:
        if mode not in SCHEDULE_MODES:
            raise RelayError("unknown schedule mode: " + str(mode), 400)
        if not 0.0 < max_utilization <= 2.0:
            raise RelayError("max_utilization must be within (0, 2]", 400)
        self.store.set_setting(SETTING_SCHEDULE_MODE, mode)
        self.store.set_setting(SETTING_SCHEDULE_MAX_UTILIZATION, repr(max_utilization))
        return self.schedule_settings()

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

    def _urgency_bonus(self, alias: str) -> float:
        """How many live sessions of head start an expiring window is worth.

        Expressed in the same unit the balanced ordering counts in, so the two
        combine instead of one overriding the other: an account resetting
        within the hour is handed the next few conversations, but once it has
        taken them its real session count catches up and the others get their
        turn. Accounts with no probe yet, or resets beyond the horizon, get
        nothing and simply sort by session count.
        """
        reset_at = self._reset_at(alias)
        if reset_at is None:
            return 0.0
        hours = (reset_at - time.time()) / 3600.0
        if hours >= URGENCY_HORIZON_HOURS:
            return 0.0
        if hours <= 0:
            return URGENCY_MAX_BONUS
        return URGENCY_MAX_BONUS * (1.0 - hours / URGENCY_HORIZON_HOURS)

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

    def _load(self, alias: str, model: Optional[str]) -> float:
        """How full this account is for the requested model.

        A fable request also draws on the model's own weekly window, so take
        whichever of the two is tighter; the spend lands on both.
        """
        windows = self._windows(alias)
        names = ["7d"]
        if self._is_fable_model(model):
            names.append(FABLE_WINDOW)
        loads = [value for value in
                 (self._window_utilization(windows.get(name)) for name in names)
                 if value is not None]
        return max(loads) if loads else 0.0

    def _quota_ok(self, alias: str, model: Optional[str] = None) -> bool:
        """Below the exhaustion mark on every window this model draws on.

        The cached /v1/limits windows are the model-aware source — a fable
        request also spends the model's own weekly window, and skipping that
        check is how a 7d_fable window ends up at 130%. The header-fed scalar
        still covers the 7d window between sweeps, since every response
        refreshes it. A ceiling deliberately configured above 100% raises the
        skip mark with it (the operator chose to overspend). No usable data
        means the account is assumed to have room; the upstream 429 stays the
        final authority either way.
        """
        mark = max(QUOTA_EXHAUSTED, self.schedule_settings()["max_utilization"])
        if self._load(alias, model) >= mark:
            return False
        try:
            quota = json.loads(self.store.row(alias)["metadata_json"]).get("quota", {})
            utilization = quota.get("7d_utilization")
            if utilization is None:
                return True
            reset = quota.get("7d_reset_epoch")
            if reset is not None and float(reset) <= time.time():
                return True  # that window has since reset; the number is history
            return float(utilization) < mark
        except (RelayError, ValueError, TypeError, json.JSONDecodeError):
            return True

    def account_disabled(self, alias: str) -> bool:
        try:
            metadata = json.loads(self.store.row(alias)["metadata_json"])
            return bool(metadata.get("disabled"))
        except (RelayError, json.JSONDecodeError):
            return False

    def exhausted_cooldown(self, alias: str) -> float:
        """Seconds left in this account's shared-quota cooldown (0 = serviceable)."""
        return max(0.0, self._exhausted_until.get(alias, 0.0) - time.time())

    def _selectable(self, alias: str) -> bool:
        """Eligible for automatic selection: not switched off in the panel and
        not cooling down after an upstream shared-quota refusal. Quota load is
        a soft preference handled separately; these two are hard exclusions."""
        return not self.account_disabled(alias) and self.exhausted_cooldown(alias) <= 0.0

    def _explicit_account(self, requested: str) -> str:
        """An explicitly requested account is honored even during a cooldown
        (the caller may know the quota reset), but never when switched off."""
        alias = alias_value(requested)
        if self.account_disabled(alias):
            raise RelayError("account is disabled in the panel: " + alias, 403)
        return alias

    def _no_selectable_error(self) -> RelayError:
        return RelayError("all accounts are disabled or cooling down after a "
                          "shared-quota refusal; enable one in the panel or retry later", 503)

    def pick_account(self, requested: str, model: Optional[str] = None) -> str:
        """Explicit header > default account > quota-aware round-robin."""
        requested = requested.strip()
        if requested:
            return self._explicit_account(requested)
        aliases = self.store.aliases()
        if not aliases:
            raise RelayError("no account configured; add one via WebUI or CLI first", 400)
        if self.default_account in aliases and self._selectable(self.default_account):
            return self.default_account
        selectable = [alias for alias in aliases if self._selectable(alias)]
        if not selectable:
            raise self._no_selectable_error()
        schedule = self.schedule_settings()
        if schedule["mode"] == SCHEDULE_RESET_FIRST:
            # There is no session key to stay stable for, but the live session
            # counts still say where the load already is, so reuse the same
            # tilted ordering instead of sending every keyless request to the
            # one account with the nearest reset.
            counts = self.session_counts()
            with self._rr_lock:
                chosen = min(selectable,
                             key=lambda alias: self._assignment_key(
                                 alias, counts, schedule, model))
                self._last_assigned[chosen] = time.time()
                return chosen
        with self._rr_lock:
            start = self._rr_index
            chosen = None
            for offset in range(len(aliases)):
                candidate = aliases[(start + offset) % len(aliases)]
                if candidate in selectable and self._quota_ok(candidate, model):
                    chosen = candidate
                    self._rr_index = (start + offset + 1) % len(aliases)
                    break
            if chosen is None:
                # Every serviceable account looks exhausted; round-robin among
                # the serviceable ones anyway.
                chosen = selectable[start % len(selectable)]
                self._rr_index = (start + 1) % len(aliases)
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

    def _assignment_key(self, alias: str, counts: dict[str, int],
                        schedule: dict[str, Any], model: Optional[str]):
        """Ordering for a new conversation; lowest wins.

        Both modes spread conversations by live session count, so no single
        account absorbs the concurrency, and both demote an account above the
        utilization ceiling — spreading by session count alone is what let a
        nearly-dead window keep attracting conversations until it overshot its
        budget. Reset-first additionally tilts the count: an account whose
        weekly window expires sooner is treated as carrying fewer sessions
        than it really does, so it picks up the next conversation earlier and
        its about-to-expire credit gets spent. Ordering by the reset time
        itself would not spread at all, because the timestamps never tie.

        Fable-first refines reset-first for non-fable traffic: among the
        accounts already inside the reset horizon it prefers the one whose own
        fable window is fullest. That account's fable credit is spent, so a
        fable request would be refused there anyway, while its general 7-day
        credit is about to expire — spend that one, and leave the accounts
        with fable headroom free for fable traffic. A fable request itself
        gets plain reset-first ordering: there the fable window is the
        constraint (``_load`` already weighs it), not the selection criterion.
        """
        live = counts.get(alias, 0)
        recency = self._last_assigned.get(alias, 0.0)
        if self._load(alias, model) >= schedule["max_utilization"]:
            # Nearly spent: keep it in service, but let every account with room
            # take a turn first.
            return (float(live) + URGENCY_MAX_BONUS + 1.0, recency)
        if schedule["mode"] == SCHEDULE_BALANCED:
            return (float(live), recency)
        bonus = self._urgency_bonus(alias)
        if schedule["mode"] == SCHEDULE_FABLE_FIRST and not self._is_fable_model(model):
            # Scale the head start by how spent the fable window is, instead of
            # adding a tie-break after it: the tilted count is a float that
            # rarely ties, so a separate key would be dead code. An urgent
            # account with no fable credit left keeps the full bonus and goes
            # first; one with fable headroom keeps almost none and is left for
            # fable traffic. The bonus stays capped at URGENCY_MAX_BONUS, so
            # the ordering still spreads by session count.
            bonus *= min(1.0, self._fable_spent(alias))
        return (float(live) - bonus, recency)

    def _sticky_account(self, key: str, aliases: list[str],
                        model: Optional[str] = None) -> str:
        with self._session_lock:
            now = time.time()
            self._prune_sessions(now)
            entry = self._sessions.get(key)
            if entry and entry["account"] in aliases and self._selectable(entry["account"]) \
                    and self._quota_ok(entry["account"], model):
                entry["last"] = now
                return entry["account"]
            # New window: order the eligible accounts by the configured mode.
            serviceable = [alias for alias in aliases if self._selectable(alias)]
            if not serviceable:
                raise self._no_selectable_error()
            eligible = [alias for alias in serviceable
                        if self._quota_ok(alias, model)] or serviceable
            counts = {alias: 0 for alias in eligible}
            for existing in self._sessions.values():
                if existing["account"] in counts:
                    counts[existing["account"]] += 1
            schedule = self.schedule_settings()
            chosen = min(eligible,
                         key=lambda alias: self._assignment_key(
                             alias, counts, schedule, model))
            self._sessions[key] = {"account": chosen, "last": now}
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
        if self.default_account in aliases and self._selectable(self.default_account):
            return self.default_account
        key = (session_hint or "").strip() or self._session_key_from_payload(payload)
        model = payload.get("model") if isinstance(payload, dict) else None
        if not key:
            return self.pick_account("", model if isinstance(model, str) else None)
        return self._sticky_account(key, aliases,
                                    model if isinstance(model, str) else None)

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
        """The documented shared-credit exhaustion, which holds until the
        weekly window resets — unlike other 429 shapes, which are usually
        transient rate pressure."""
        if not isinstance(exc.data, dict):
            return False
        error = exc.data.get("error")
        return (isinstance(error, dict)
                and str(error.get("type")) == CREDIT_EXHAUSTED_TYPE)

    def drop_account_sessions(self, alias: str) -> None:
        """Detach live sessions pinned to an account so each conversation's next
        turn is reassigned instead of repeating a failing or disabled account."""
        with self._session_lock:
            stale = [key for key, entry in self._sessions.items()
                     if entry["account"] == alias]
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
        self.upstream.ensure_device_identity(alias)
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
                cooldown, reason = SHARED_QUOTA_COOLDOWN, "shared-quota refusal"
            else:
                cooldown, reason = TRANSIENT_429_COOLDOWN, "account-scoped 429"
        elif self._is_region_refused_everywhere(exc):
            cooldown, reason = SHARED_QUOTA_COOLDOWN, "region refusal from every exit"
        else:
            return False
        self._exhausted_until[alias] = time.time() + cooldown
        self.drop_account_sessions(alias)
        logger.warning(
            "account cooling down for %ds after %s: account=%s",
            int(cooldown), reason, alias)
        return True

    async def with_account_failover(
            self, requested: str, session_hint: str, payload: Any,
            run: Callable[[str], Awaitable[Any]]) -> tuple[str, Any]:
        """Route an account and run the request, failing over to another account
        when the upstream refuses the chosen one with credit_exhausted_shared.
        An explicitly requested account is never substituted."""
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
                return account, await run(account)
            except RelayError as exc:
                if not self.note_account_unserviceable(account, exc) or requested:
                    raise
                tried.add(account)
                last = exc

    @classmethod
    def relay_session_id(cls, claude_session: str, session_hint: str,
                         payload: Any) -> str:
        """Stable, non-secret session id for the upstream relay metadata.

        Preserve Claude Code's own printable session id when available. Other
        local affinity hints are hashed so arbitrary caller values and message
        text never appear in an upstream header.

        The hash is shaped as a v4 UUID rather than a readable prefix: this
        value is sent as both ``x-mirasim-session`` and, for synthesized client
        identities, ``x-claude-code-session-id``, where every official client
        sends a bare UUID.  Determinism is what session affinity needs, and it
        is unchanged; only the encoding differs.

        A caller's session id is therefore passed through only when it already
        *is* a UUID.  Anything else is hashed like any other hint: it still
        keys the same conversation to the same account, without letting a local
        caller put an arbitrary label in an upstream header.
        """
        direct = (claude_session or "").strip()
        if _is_uuid(direct):
            return direct.lower()
        key = direct or (session_hint or "").strip() \
            or cls._session_key_from_payload(payload)
        if key:
            digest = hashlib.sha256(key.encode("utf-8")).digest()[:16]
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
                response = await self.upstream.stream_messages(
                    alias, payload, proxy_url, request_headers=request_headers,
                    session_id=session_id, beta=beta, raw_body=raw_body)
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
                response = await self.upstream.stream_responses(
                    alias, body, proxy_url, request_headers=request_headers,
                    session_id=session_id, account_id=account_id,
                    query_string=query_string, path=path)
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
                     response_headers: dict[str, str]) -> dict[str, str]:
        """Persist usage/quota metadata and return the outgoing relay headers."""
        # Only merge the quota values this response actually carried. A
        # headerless response (e.g. the Codex path) must not wipe the
        # utilization cached by /v1/limits probes — a None there reads as
        # "has room" and would put an exhausted account back into rotation.
        quota = {key: value for key, value in quota_headers(response_headers).items()
                 if value is not None}
        try:
            self.store.merge_metadata(alias, {"last_usage": usage, "quota": quota,
                                              "last_model": model}, deep=("quota",))
        except RelayError:
            pass
        if usage:
            try:
                self.store.log_usage(alias, model, usage)
            except RelayError:
                pass
        outgoing = {"X-Mirofish-Account": alias_value(alias)}
        if quota.get("7d_utilization"):
            outgoing["X-Mirofish-Quota-7d-Utilization"] = str(quota["7d_utilization"])
        if quota.get("7d_reset_epoch"):
            outgoing["X-Mirofish-Quota-7d-Reset"] = str(quota["7d_reset_epoch"])
        return outgoing
