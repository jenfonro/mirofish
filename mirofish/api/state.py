"""Shared application state and the proxy-retry orchestration helpers."""

from __future__ import annotations

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
from ..upstream import Upstream, quota_headers
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

    async def aclose(self) -> None:
        await self.upstream.aclose()
        await self.pool.aclose()

    # --- account selection ----------------------------------------------------

    def _quota_ok(self, alias: str) -> bool:
        try:
            metadata = json.loads(self.store.row(alias)["metadata_json"])
            utilization = metadata.get("quota", {}).get("7d_utilization")
            return utilization is None or float(utilization) < QUOTA_EXHAUSTED
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

    def pick_account(self, requested: str) -> str:
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
        with self._rr_lock:
            start = self._rr_index
            chosen = None
            for offset in range(len(aliases)):
                candidate = aliases[(start + offset) % len(aliases)]
                if candidate in selectable and self._quota_ok(candidate):
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
        meta = payload.get("metadata")
        if isinstance(meta, dict):
            uid = meta.get("user_id")
            if isinstance(uid, str) and uid.strip():
                return "uid:" + uid.strip()
        messages = payload.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict) and message.get("role") == "user":
                    text = cls._message_text(message)
                    if text:
                        return "msg:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        return ""

    def _prune_sessions(self, now: float) -> None:
        ttl = self.settings.session_ttl
        stale = [key for key, entry in self._sessions.items() if now - entry["last"] > ttl]
        for key in stale:
            del self._sessions[key]

    def _sticky_account(self, key: str, aliases: list[str]) -> str:
        with self._session_lock:
            now = time.time()
            self._prune_sessions(now)
            entry = self._sessions.get(key)
            if entry and entry["account"] in aliases and self._selectable(entry["account"]) \
                    and self._quota_ok(entry["account"]):
                entry["last"] = now
                return entry["account"]
            # New window: assign the eligible account carrying the fewest live
            # sessions (ties broken by least-recently assigned) so windows fan
            # out across accounts instead of piling onto one.
            serviceable = [alias for alias in aliases if self._selectable(alias)]
            if not serviceable:
                raise self._no_selectable_error()
            eligible = [alias for alias in serviceable if self._quota_ok(alias)] or serviceable
            counts = {alias: 0 for alias in eligible}
            for existing in self._sessions.values():
                if existing["account"] in counts:
                    counts[existing["account"]] += 1
            chosen = min(eligible,
                         key=lambda alias: (counts[alias], self._last_assigned.get(alias, 0.0)))
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
        if not key:
            return self.pick_account("")
        return self._sticky_account(key, aliases)

    # --- account-level failover -----------------------------------------------

    @staticmethod
    def _is_account_exhausted(exc: RelayError) -> bool:
        """The upstream refused to serve this ACCOUNT (its shared credit pool is
        used up). An account property, not an exit property: rotating proxies
        cannot fix it, but another account can still serve the request."""
        if not isinstance(exc.data, dict):
            return False
        error = exc.data.get("error")
        return (isinstance(error, dict)
                and str(error.get("type")) == "credit_exhausted_shared")

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
        avoids the account for a while. Returns True when the error was one."""
        if self._is_account_exhausted(exc):
            reason = "shared-quota refusal"
        elif self._is_region_refused_everywhere(exc):
            reason = "region refusal from every exit"
        else:
            return False
        self._exhausted_until[alias] = time.time() + SHARED_QUOTA_COOLDOWN
        self.drop_account_sessions(alias)
        logger.warning(
            "account cooling down for %ds after %s: account=%s",
            int(SHARED_QUOTA_COOLDOWN), reason, alias)
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
        """
        direct = (claude_session or "").strip()
        if direct and len(direct) <= 128 \
                and all(0x21 <= ord(char) <= 0x7e for char in direct):
            return direct
        key = (session_hint or "").strip() or cls._session_key_from_payload(payload)
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
                    session_id=session_id, beta=beta)
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

    # --- usage accounting ---------------------------------------------------

    def record_usage(self, alias: str, model: Optional[str], usage: dict[str, Any],
                     response_headers: dict[str, str]) -> dict[str, str]:
        """Persist usage/quota metadata and return the outgoing relay headers."""
        quota = quota_headers(response_headers)
        try:
            self.store.merge_metadata(alias, {"last_usage": usage, "quota": quota,
                                              "last_model": model})
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
