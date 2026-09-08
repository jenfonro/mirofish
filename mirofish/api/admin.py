"""Management API: health, accounts, login flow, proxy pool, usage stats."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request

from .. import __version__
from ..accounts import public_status
from ..errors import RelayError
from ..proxy import DIRECT, proxy_from_uri
from ..validate import alias_value, email_value, proxy_node_value
from .deps import get_state, read_json_body, require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    state = get_state(request)
    return {"ok": True, "accounts": len(state.store.aliases()),
            "version": __version__,
            "default_account": state.default_account or None}


@router.get("/accounts")
async def list_accounts(request: Request) -> dict[str, Any]:
    state = get_state(request)
    sessions = state.session_counts()
    accounts = []
    for alias in state.store.aliases():
        status = public_status(state.store.row(alias),
                               proxy=state.pool.account_public(alias))
        status["active_sessions"] = sessions.get(alias, 0)
        status["shared_quota_cooldown"] = round(state.exhausted_cooldown(alias))
        # One flag the panel renders directly: abnormal accounts are the ones an
        # upstream 401/503 parked, which scheduling now skips until an operator
        # retries them from the playground.
        status["healthy"] = not state.account_unhealthy(alias)
        # Seconds until a self-healing refusal (503 capacity) is retried, or
        # None when the record needs an operator (401 credentials).
        retry_in = state.health_retry_in(alias)
        status["health_retry_in"] = None if retry_in is None else round(retry_in)
        accounts.append(status)
    return {"accounts": accounts}


@router.get("/accounts/{alias}/status")
async def account_status(alias: str, request: Request,
                         probe: Optional[str] = None) -> dict[str, Any]:
    """Refresh one account's profile and usage.

    A refusal aimed at the account (401, 403 suspension, 503 capacity) is
    recorded: a suspended account otherwise kept reading "正常" in the panel
    while every refresh failed, and scheduling went on electing it.

    A quota refusal is deliberately *not*: reading a profile is not model
    traffic, so "out of weekly credit" says nothing about whether this call
    should have worked, and benching the account for it would be wrong. Nor
    does success here clear a recorded refusal — only real model traffic
    proves an account can serve model traffic.
    """
    state = get_state(request)
    alias = alias_value(alias)
    do_probe = probe in ("1", "true")
    try:
        status = await state.with_proxy(
            alias, lambda url: state.accounts.fetch_status(alias, do_probe, proxy_url=url))
    except RelayError as exc:
        state.note_account_error(alias, exc)
        raise
    status["proxy"] = state.pool.account_public(alias)
    return status


@router.get("/accounts/{alias}/limits")
async def account_limits(alias: str, request: Request) -> dict[str, Any]:
    """Live per-window usage limits from upstream /v1/limits (zero model cost).

    Records an account-aimed refusal like ``account_status``, and for the same
    reason ignores a quota one: this read costs no credit, so being out of
    credit is not why it failed.
    """
    state = get_state(request)
    alias = alias_value(alias)
    try:
        limits = await state.with_proxy(
            alias, lambda url: state.accounts.fetch_limits(alias, proxy_url=url))
    except RelayError as exc:
        state.note_account_error(alias, exc)
        raise
    return limits


@router.get("/api/limits")
async def all_limits(request: Request) -> dict[str, Any]:
    """Fetch usage limits for every account concurrently (zero model cost)."""
    state = get_state(request)
    aliases = state.store.aliases()

    async def one(alias: str) -> dict[str, Any]:
        try:
            limits = await state.with_proxy(
                alias, lambda url: state.accounts.fetch_limits(alias, proxy_url=url))
        except RelayError as exc:
            state.note_account_error(alias, exc)
            return {"alias": alias, "ok": False, "error": str(exc), "status": exc.status}
        except Exception as exc:  # noqa: BLE001 - never let one account break the batch
            return {"alias": alias, "ok": False, "error": str(exc) or type(exc).__name__}
        return {"alias": alias, "ok": True, "limits": limits}

    results = await asyncio.gather(*(one(alias) for alias in aliases))
    return {"accounts": list(results)}


@router.post("/api/accounts/{alias}/enabled")
async def set_account_enabled(alias: str, request: Request) -> dict[str, Any]:
    """Panel switch. A disabled account keeps its credentials but is excluded
    from automatic selection; requesting it explicitly returns 403."""
    state = get_state(request)
    alias = alias_value(alias)
    payload = await read_json_body(request)
    enabled = bool(payload.get("enabled"))
    state.store.merge_metadata(alias, {"disabled": not enabled})
    if not enabled:
        state.drop_account_sessions(alias)
    return {"alias": alias, "enabled": enabled}


@router.post("/api/accounts/{alias}/proxy")
async def set_account_proxy(alias: str, request: Request) -> dict[str, Any]:
    """Pin this account to one exit, or release it with an empty id.

    Live sessions are dropped: they are pinned to the account, and the next
    turn must go out through the exit the operator just chose rather than
    finishing on the old one.
    """
    state = get_state(request)
    alias = alias_value(alias)
    state.store.row(alias)
    payload = await read_json_body(request)
    proxy_id = str(payload.get("proxy_id") or "").strip()
    if proxy_id and state.pool.by_id(proxy_id) is None:
        raise RelayError("unknown proxy node: " + proxy_id, 404)
    # An empty id is "no proxy", stored as `DIRECT` rather than NULL: NULL reads
    # as "never assigned" and the next request would hand the account an exit.
    state.store.set_account_proxy(alias, proxy_id or DIRECT)
    state.drop_account_sessions(alias)
    return {"alias": alias, "proxy": state.pool.account_public(alias)}


@router.delete("/api/accounts/{alias}")
async def delete_account(alias: str, request: Request) -> dict[str, Any]:
    state = get_state(request)
    alias = alias_value(alias)
    state.remove_account(alias)
    return {"deleted": alias}


@router.post("/api/login/start")
async def login_start(request: Request) -> dict[str, Any]:
    state = get_state(request)
    payload = await read_json_body(request)
    alias = alias_value(str(payload.get("alias", "")))
    email = email_value(str(payload.get("email", "")))
    # An operator can pick the exit up front. An explicitly empty proxy_id is
    # them choosing no proxy, which must not fall back to auto-selection.
    pinned = None
    direct = False
    if "proxy_id" in payload:
        requested = str(payload.get("proxy_id") or "").strip()
        if requested:
            pinned = state.pool.by_id(requested)
            if pinned is None:
                raise RelayError("unknown proxy node: " + requested, 404)
        else:
            direct = True
    proxy, _ = await state.with_pending_proxy(
        alias, lambda url: state.accounts.start_login(alias, email, proxy_url=url),
        pinned=pinned, direct=direct)
    # `DIRECT` when the operator explicitly chose no exit: `finish_login` has to
    # tell that apart from "no choice made", or the save falls back to whatever
    # binding the alias had before.
    state.put_pending_login(
        alias, email,
        proxy.get("id") if isinstance(proxy, dict) else (DIRECT if direct else None))
    return {"sent": True, "alias": alias}


@router.post("/api/login/finish")
async def login_finish(request: Request) -> dict[str, Any]:
    state = get_state(request)
    payload = await read_json_body(request)
    alias = alias_value(str(payload.get("alias", "")))
    pending = state.take_pending_login(alias)
    requested = str(pending.get("proxy_id") or "")
    proxy = state.pool.by_id(requested) if requested != DIRECT else None
    result = await state.with_fixed_proxy(
        alias, proxy,
        lambda url: state.accounts.finish_login(
            alias, pending["email"], str(payload.get("code", "")), proxy_url=url,
            proxy_id=(str(proxy["id"]) if proxy and proxy.get("id")
                      else (DIRECT if requested == DIRECT else None))))
    state.reset_account_runtime(alias)
    state.pending_logins.pop(alias, None)
    # A login that saved credentials but was refused a profile has already been
    # judged by the upstream; record it so a suspended account is parked right
    # away instead of waiting for someone to refresh it by hand.
    refusal = result.pop("profile_refusal", None)
    if refusal is not None:
        status, body = refusal
        state.note_account_error(alias, RelayError("profile lookup refused", status, body))
        # Report the state the account is actually in now, not the pre-verdict one.
        result["health"] = public_status(state.store.row(alias)).get("health") or {}
    return result


@router.get("/proxies")
async def proxies(request: Request) -> dict[str, Any]:
    return get_state(request).pool.public_summary()


@router.post("/api/proxies")
async def add_proxy(request: Request) -> dict[str, Any]:
    """Add one node. The name is optional and falls back to host:port, since a
    pasted endpoint often has no meaningful name to give it."""
    state = get_state(request)
    payload = await read_json_body(request)
    return state.pool.add(proxy_node_value(payload))


@router.post("/api/proxies/import")
async def import_proxies(request: Request) -> dict[str, Any]:
    """Bulk-add from pasted lines.

    Reports per-line outcomes rather than failing the batch: a list pasted
    from elsewhere routinely has a stray blank or comment in it, and losing
    the twenty good lines over one bad one is not useful.
    """
    state = get_state(request)
    payload = await read_json_body(request)
    text = str(payload.get("text", ""))
    added, failed = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        config = proxy_from_uri(line) or proxy_from_uri("socks5://" + line)
        if config is None:
            failed.append(line[:120])
            continue
        added.append(state.pool.add(proxy_node_value(config)))
    return {"added": len(added), "failed": failed,
            "pool": state.pool.public_summary()}


@router.patch("/api/proxies/{proxy_id}")
async def edit_proxy(proxy_id: str, request: Request) -> dict[str, Any]:
    state = get_state(request)
    payload = await read_json_body(request)
    return state.pool.update(proxy_id, proxy_node_value(payload))


@router.delete("/api/proxies/{proxy_id}")
async def delete_proxy(proxy_id: str, request: Request) -> dict[str, Any]:
    state = get_state(request)
    state.pool.remove(proxy_id)
    return {"ok": True, "pool": state.pool.public_summary()}


@router.post("/api/proxies/{proxy_id}/test")
async def test_proxy(proxy_id: str, request: Request) -> dict[str, Any]:
    return await get_state(request).pool.test(proxy_id)


@router.get("/api/usage")
async def usage(request: Request, hours: int = 24) -> dict[str, Any]:
    if hours < 1 or hours > 24 * 30:
        raise RelayError("hours must be between 1 and 720", 400)
    return get_state(request).store.usage_summary(hours)
