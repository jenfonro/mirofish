"""Management API: health, accounts, login flow, proxy pool, usage stats."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request

from .. import __version__
from ..accounts import public_status
from ..device import DEVICE_KEY_KIND
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
            "proxy_backend": "direct",
            "default_account": state.default_account or None}


@router.get("/accounts")
async def list_accounts(request: Request) -> dict[str, Any]:
    state = get_state(request)
    sessions = state.session_counts()
    accounts = []
    for alias in state.store.aliases():
        status = public_status(state.store.row(alias),
                               proxy=state.pool.account_public(alias),
                               device_id=state.upstream._signer(alias).device_id)
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
    status["device_id"] = state.upstream._signer(alias).device_id
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


@router.post("/api/accounts/{alias}/reset-device")
async def reset_device(alias: str, request: Request) -> dict[str, Any]:
    """Rotate the account's device identity: delete its Ed25519 key and drop
    every cached device ticket, so the next request mints a fresh identity."""
    state = get_state(request)
    alias = alias_value(alias)
    state.store.row(alias)  # 404 for unknown alias, like delete_account
    state.store.vault.delete(alias, DEVICE_KEY_KIND)
    state.upstream.reset_device_identity(alias)
    return {"alias": alias, "device_id": state.upstream._signer(alias).device_id}


@router.post("/api/accounts/{alias}/proxy")
async def set_account_proxy(alias: str, request: Request) -> dict[str, Any]:
    """Pin this account to one exit, or release it with an empty id.

    Live sessions are dropped: they are pinned to the account, and the next
    turn must go out through the exit the operator just chose.
    """
    state = get_state(request)
    alias = alias_value(alias)
    state.store.row(alias)
    payload = await read_json_body(request)
    proxy_id = str(payload.get("proxy_id") or "").strip()
    if proxy_id and state.pool.by_id(proxy_id) is None:
        raise RelayError("unknown proxy node: " + proxy_id, 404)
    # An empty id is "no proxy", stored as DIRECT rather than NULL: NULL reads
    # as "never assigned" and the next request would hand the account an exit.
    state.store.set_account_proxy(alias, proxy_id or DIRECT)
    state.drop_account_sessions(alias)
    return {"alias": alias, "proxy": state.pool.account_public(alias)}


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
    the good lines over one bad one is not useful.
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
    return state.set_schedule_settings(ceiling)


@router.get("/api/usage")
async def usage(request: Request, hours: int = 24) -> dict[str, Any]:
    if hours < 1 or hours > 24 * 30:
        raise RelayError("hours must be between 1 and 720", 400)
    return get_state(request).store.usage_summary(hours)
