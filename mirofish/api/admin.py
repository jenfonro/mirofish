"""Management API: health, accounts, login flow, proxy pool, usage stats."""

from __future__ import annotations

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
    return {"accounts": [public_status(state.store.row(alias),
                                       proxy=state.pool.account_public(alias))
                         for alias in state.store.aliases()]}


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


@router.delete("/api/accounts/{alias}")
async def delete_account(alias: str, request: Request) -> dict[str, Any]:
    state = get_state(request)
    alias = alias_value(alias)
    state.store.remove(alias)
    if state.pool.slots is not None:
        state.pool.slots.release(alias)
    return {"deleted": alias}


@router.post("/api/login/start")
async def login_start(request: Request) -> dict[str, Any]:
    state = get_state(request)
    payload = await read_json_body(request)
    alias = alias_value(str(payload.get("alias", "")))
    email = email_value(str(payload.get("email", "")))
    proxy = await state.pool.pending_proxy(alias)
    await state.with_fixed_proxy(
        alias, proxy, lambda url: state.accounts.start_login(alias, email, proxy_url=url))
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


@router.post("/api/proxies/refresh")
async def refresh_proxies(request: Request) -> dict[str, Any]:
    return await get_state(request).pool.refresh(force=True)


@router.get("/api/usage")
async def usage(request: Request, hours: int = 24) -> dict[str, Any]:
    if hours < 1 or hours > 24 * 30:
        raise RelayError("hours must be between 1 and 720", 400)
    return get_state(request).store.usage_summary(hours)
