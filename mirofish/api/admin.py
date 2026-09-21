"""Management API: health, accounts, login flow, proxy pool, usage stats."""

from __future__ import annotations

import asyncio
import math
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request

from .. import __version__
from ..accounts import public_status
from ..errors import RelayError
from ..proxy import DIRECT
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
        status["limits"] = state.accounts.cached_limits(alias)
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
    """Refresh one account's subscription profile, never its quota.

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
    try:
        status = await state.with_proxy(
            alias, lambda url: state.accounts.fetch_status(alias, proxy_url=url))
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
            alias, lambda url: state.accounts.fetch_limits(alias, proxy_url=url, force=True))
    except RelayError as exc:
        state.note_account_error(alias, exc)
        raise
    return limits


@router.get("/api/limits")
async def all_limits(request: Request) -> dict[str, Any]:
    """Panel reads never refresh upstream data."""
    state = get_state(request)
    return {"accounts": [{"alias": alias, "ok": True,
                           "limits": state.accounts.cached_limits(alias)}
                          for alias in state.store.aliases()]}


@router.post("/api/limits/refresh")
async def refresh_all_limits(request: Request) -> dict[str, Any]:
    """Explicit refresh only; disabled/suspended accounts are not batch-probed."""
    state = get_state(request)
    aliases = [alias for alias in state.store.aliases()
               if not state.account_disabled(alias) and not state.account_suspended(alias)]
    gate = asyncio.Semaphore(4)

    async def one(alias: str) -> dict[str, Any]:
        async with gate:
            try:
                limits = await state.with_proxy(alias, lambda url:
                    state.accounts.fetch_limits(alias, proxy_url=url, force=True))
                return {"alias": alias, "ok": True, "limits": limits}
            except RelayError as exc:
                state.note_account_error(alias, exc)
                return {"alias": alias, "ok": False, "status": exc.status, "error": str(exc)}
    return {"accounts": list(await asyncio.gather(*(one(alias) for alias in aliases)))}


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


@router.patch("/api/accounts/{alias}")
@router.post("/api/accounts/{alias}/proxy")
async def edit_account(alias: str, request: Request) -> dict[str, Any]:
    state = get_state(request)
    alias = alias_value(alias)
    state.store.row(alias)
    payload = await read_json_body(request)
    name = payload.get("display_name")
    if name is not None and (not isinstance(name, str) or len(name) > 120):
        raise RelayError("display_name must be a string of at most 120 characters", 400)
    if "proxy_id" in payload:
        proxy_id = str(payload.get("proxy_id") or "").strip() or DIRECT
        if proxy_id != DIRECT and state.pool.by_id(proxy_id) is None:
            raise RelayError("unknown proxy node", 404)
        state.store.set_account_proxy(alias, proxy_id)
        state.drop_account_sessions(alias)
        state.upstream.credentials_changed(alias)
    if name is not None:
        state.store.merge_metadata(alias, {"display_name": name.strip()})
    return public_status(state.store.row(alias), proxy=state.pool.account_public(alias))


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
    if "proxy_id" in payload:
        requested = str(payload.get("proxy_id") or "").strip() or DIRECT
    else:
        try:
            requested = state.store.row(alias)["proxy_id"]
            if not requested:
                raise RelayError("choose a proxy or explicitly choose direct before login", 409)
        except RelayError as exc:
            if exc.status != 404:
                raise
            requested = DIRECT
    pinned = state.pool.by_id(requested) if requested != DIRECT else None
    if requested != DIRECT and pinned is None:
        raise RelayError("selected proxy configuration is missing", 503)
    await state.with_fixed_proxy(alias, pinned,
        lambda url: state.accounts.start_login(alias, email, proxy_url=url))
    state.put_pending_login(alias, email, requested)
    return {"sent": True, "alias": alias}


@router.post("/api/login/finish")
async def login_finish(request: Request) -> dict[str, Any]:
    state = get_state(request)
    payload = await read_json_body(request)
    alias = alias_value(str(payload.get("alias", "")))
    pending = state.take_pending_login(alias)
    requested = str(pending.get("proxy_id") or "")
    proxy = state.pool.by_id(requested) if requested != DIRECT else None
    if requested != DIRECT and proxy is None:
        raise RelayError("selected login proxy configuration is missing", 503)
    result = await state.with_fixed_proxy(
        alias, proxy,
        lambda url: state.accounts.finish_login(
            alias, pending["email"], str(payload.get("code", "")), proxy_url=url,
            proxy_id=requested))
    state.reset_account_runtime(alias)
    state.pending_logins.pop(alias, None)
    for kind in ("profile_refusal", "limits_refusal"):
        refusal = result.pop(kind, None)
        if refusal is not None:
            status, body = refusal
            state.note_account_error(alias, RelayError(kind, status, body))
            result[kind.replace("refusal", "error")] = {"status": status}
    result.update(public_status(state.store.row(alias), proxy=state.pool.account_public(alias)))
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
    return state.pool.import_uris(str(payload.get("text", "")))


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


@router.get("/api/schedule")
async def get_schedule(request: Request) -> dict[str, Any]:
    settings = get_state(request).settings
    return {"policy": "reset_first_fable", "max_utilization": settings.quota_ceiling,
            "limits_ttl": settings.limits_ttl}


@router.post("/api/schedule")
async def set_schedule(request: Request) -> dict[str, Any]:
    state = get_state(request)
    payload = await read_json_body(request)
    raw = payload.get("max_utilization")
    if isinstance(raw, bool):
        raise RelayError("max_utilization must be between 0.10 and 1.0", 400)
    try:
        ceiling = float(raw)
    except (TypeError, ValueError) as exc:
        raise RelayError("max_utilization must be between 0.10 and 1.0", 400) from exc
    if not math.isfinite(ceiling) or not 0.1 <= ceiling <= 1.0:
        raise RelayError("max_utilization must be between 0.10 and 1.0", 400)
    state.store.set_setting("quota_ceiling", str(ceiling))
    state.settings.quota_ceiling = ceiling
    return await get_schedule(request)


@router.get("/api/usage")
async def usage(request: Request, hours: int = 24) -> dict[str, Any]:
    if hours < 1 or hours > 24 * 30:
        raise RelayError("hours must be between 1 and 720", 400)
    return get_state(request).store.usage_summary(hours)
