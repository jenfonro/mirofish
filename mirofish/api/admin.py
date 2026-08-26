"""Management API: health, accounts, login flow, proxy pool, usage stats."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request

from .. import __version__
from ..accounts import public_status
from ..errors import RelayError
from ..validate import alias_value, email_value
from .deps import get_state, read_json_body, require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    state = get_state(request)
    return {"ok": True, "accounts": len(state.store.aliases()),
            "version": __version__,
            "proxy_backend": "mihomo" if state.pool.uses_mihomo else "direct",
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
        accounts.append(status)
    return {"accounts": accounts}


@router.get("/accounts/{alias}/status")
async def account_status(alias: str, request: Request,
                         probe: Optional[str] = None) -> dict[str, Any]:
    state = get_state(request)
    alias = alias_value(alias)
    do_probe = probe in ("1", "true")
    status = await state.with_proxy(
        alias, lambda url: state.accounts.fetch_status(alias, do_probe, proxy_url=url))
    status["proxy"] = state.pool.account_public(alias)
    return status


@router.get("/accounts/{alias}/limits")
async def account_limits(alias: str, request: Request) -> dict[str, Any]:
    """Live per-window usage limits from upstream /v1/limits (zero model cost)."""
    state = get_state(request)
    alias = alias_value(alias)
    return await state.with_proxy(
        alias, lambda url: state.accounts.fetch_limits(alias, proxy_url=url))


@router.get("/api/limits")
async def all_limits(request: Request) -> dict[str, Any]:
    """Fetch usage limits for every account concurrently (zero model cost)."""
    state = get_state(request)
    aliases = state.store.aliases()

    async def one(alias: str) -> dict[str, Any]:
        try:
            limits = await state.with_proxy(
                alias, lambda url: state.accounts.fetch_limits(alias, proxy_url=url))
            return {"alias": alias, "ok": True, "limits": limits}
        except RelayError as exc:
            return {"alias": alias, "ok": False, "error": str(exc), "status": exc.status}
        except Exception as exc:  # noqa: BLE001 - never let one account break the batch
            return {"alias": alias, "ok": False, "error": str(exc) or type(exc).__name__}

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
    proxy, _ = await state.with_pending_proxy(
        alias, lambda url: state.accounts.start_login(alias, email, proxy_url=url))
    state.put_pending_login(alias, email,
                            proxy.get("id") if isinstance(proxy, dict) else None)
    return {"sent": True, "alias": alias}


@router.post("/api/login/finish")
async def login_finish(request: Request) -> dict[str, Any]:
    state = get_state(request)
    payload = await read_json_body(request)
    alias = alias_value(str(payload.get("alias", "")))
    pending = state.take_pending_login(alias)
    proxy = state.pool.by_id(pending.get("proxy_id"))
    result = await state.with_fixed_proxy(
        alias, proxy,
        lambda url: state.accounts.finish_login(
            alias, pending["email"], str(payload.get("code", "")), proxy_url=url,
            proxy_id=str(proxy["id"]) if proxy and proxy.get("id") else None))
    state.reset_account_runtime(alias)
    state.pending_logins.pop(alias, None)
    return result


@router.get("/proxies")
async def proxies(request: Request) -> dict[str, Any]:
    return get_state(request).pool.public_summary()


@router.post("/api/proxies/subscription")
async def set_subscription(request: Request) -> dict[str, Any]:
    state = get_state(request)
    payload = await read_json_body(request)
    return await state.pool.set_subscription(str(payload.get("url", "")))


@router.get("/api/schedule")
async def get_schedule(request: Request) -> dict[str, Any]:
    """Current account-scheduling mode and its utilization ceiling."""
    return get_state(request).schedule_settings()


@router.post("/api/schedule")
async def set_schedule(request: Request) -> dict[str, Any]:
    state = get_state(request)
    payload = await read_json_body(request)
    current = state.schedule_settings()
    try:
        ceiling = float(payload.get("max_utilization", current["max_utilization"]))
    except (TypeError, ValueError) as exc:
        raise RelayError("max_utilization must be a number", 400) from exc
    settings = state.set_schedule_settings(
        str(payload.get("mode", current["mode"])), ceiling)
    # Reset-first reads the cached windows: make sure the sweep is running and
    # probe now rather than ordering on days-old numbers until the next tick.
    state.start_limits_refresh()
    state.kick_limits_refresh()
    return settings


@router.post("/api/proxies/refresh")
async def refresh_proxies(request: Request) -> dict[str, Any]:
    return await get_state(request).pool.refresh(force=True)


@router.get("/api/usage")
async def usage(request: Request, hours: int = 24) -> dict[str, Any]:
    if hours < 1 or hours > 24 * 30:
        raise RelayError("hours must be between 1 and 720", 400)
    return get_state(request).store.usage_summary(hours)
