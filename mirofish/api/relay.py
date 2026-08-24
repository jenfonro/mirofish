"""Anthropic-compatible relay endpoints: /v1/messages (with true streaming
passthrough) and the cached per-account /v1/models catalog."""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..upstream import quota_headers
from ..validate import model_value
from .deps import get_state, read_json_body, require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


def _beta_enabled(request: Request) -> bool:
    return request.query_params.get("beta", "").strip().lower() == "true"


class _UsageWatcher:
    """Extract usage numbers from an Anthropic SSE stream as it passes through."""

    def __init__(self) -> None:
        self.usage: dict[str, Any] = {}

    def feed_line(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        if kind == "message_start":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            self.usage.update(usage)
        elif kind == "message_delta":
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            self.usage.update(usage)


@router.post("/v1/messages")
async def messages(request: Request) -> Any:
    state = get_state(request)
    payload = await read_json_body(request)
    payload["model"] = model_value(str(payload.get("model", "")))
    session_hint = request.headers.get("X-Mirofish-Session", "")
    requested = request.headers.get("X-Mirofish-Account", "")
    relay_session = state.relay_session_id(
        request.headers.get("X-Claude-Code-Session-Id", ""), session_hint, payload)
    beta = _beta_enabled(request)
    model = payload.get("model") if isinstance(payload.get("model"), str) else None

    if not payload.get("stream"):
        async def run(account: str):
            return await state.with_proxy(
                account,
                lambda proxy_url: state.upstream.messages(
                    account, payload, proxy_url, request_headers=request.headers,
                    session_id=relay_session, beta=beta))
        account, (result, headers) = await state.with_account_failover(
            requested, session_hint, payload, run)
        usage = result.get("usage", {}) if isinstance(result, dict) else {}
        outgoing = state.record_usage(account, model, usage, headers)
        return JSONResponse(result, headers=outgoing)

    async def run_stream(account: str):
        return await state.open_messages_stream(
            account, payload, request_headers=request.headers,
            session_id=relay_session, beta=beta)
    account, (response, stack) = await state.with_account_failover(
        requested, session_hint, payload, run_stream)
    upstream_headers = {key.lower(): value for key, value in response.headers.items()}
    quota = quota_headers(upstream_headers)
    outgoing = {"X-Mirofish-Account": account}
    if quota.get("7d_utilization"):
        outgoing["X-Mirofish-Quota-7d-Utilization"] = str(quota["7d_utilization"])
    if quota.get("7d_reset_epoch"):
        outgoing["X-Mirofish-Quota-7d-Reset"] = str(quota["7d_reset_epoch"])

    async def body() -> AsyncIterator[bytes]:
        watcher = _UsageWatcher()
        try:
            async for line in response.aiter_lines():
                watcher.feed_line(line)
                yield (line + "\n").encode("utf-8")
        finally:
            await stack.aclose()
            state.record_usage(account, model, watcher.usage, upstream_headers)

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={**outgoing, "Cache-Control": "no-cache"})


def _estimate_input_tokens(payload: dict[str, Any]) -> int:
    """Rough local token estimate (~4 chars/token) for when upstream count is
    unavailable. Walks system + message text so token-counting clients keep
    working instead of getting a 404."""
    chars = 0

    def add(value: Any) -> None:
        nonlocal chars
        if isinstance(value, str):
            chars += len(value)
        elif isinstance(value, list):
            for item in value:
                add(item)
        elif isinstance(value, dict):
            add(value.get("text"))
            add(value.get("content"))

    add(payload.get("system"))
    for message in payload.get("messages", []) if isinstance(payload.get("messages"), list) else []:
        if isinstance(message, dict):
            add(message.get("content"))
    return max(1, chars // 4)


@router.post("/v1/messages/count_tokens")
async def count_tokens(request: Request) -> Any:
    """Anthropic token-counting endpoint. Proxied to upstream (device-signed,
    not billable); falls back to a local estimate if upstream lacks it or the
    proxy hop fails, so clients never see a 404 and stop retry-storming."""
    state = get_state(request)
    payload = await read_json_body(request)
    payload["model"] = model_value(str(payload.get("model", "")))
    session_hint = request.headers.get("X-Mirofish-Session", "")
    account = state.route_account(request.headers.get("X-Mirofish-Account", ""),
                                  session_hint, payload)
    relay_session = state.relay_session_id(
        request.headers.get("X-Claude-Code-Session-Id", ""), session_hint, payload)
    outgoing = {"X-Mirofish-Account": account}
    try:
        async def op(proxy_url):
            return await state.upstream.signed_json(
                account, "POST", "/v1/messages/count_tokens", payload, proxy_url,
                request_headers=request.headers, session_id=relay_session,
                beta=_beta_enabled(request))
        status, _, data = await state.with_proxy(account, op)
        if 200 <= status < 300 and isinstance(data, dict) and "input_tokens" in data:
            return JSONResponse(data, headers=outgoing)
    except Exception:  # noqa: BLE001 - any failure falls back to the estimate
        pass
    return JSONResponse({"input_tokens": _estimate_input_tokens(payload)}, headers=outgoing)


@router.get("/v1/models")
async def models(request: Request) -> Any:
    state = get_state(request)
    requested = request.headers.get("X-Mirofish-Account", "").strip()
    account = requested or state.default_account
    if not account:
        aliases = state.store.aliases()
        account = aliases[0] if aliases else ""
    if not account:
        return JSONResponse({"error": {"type": "relay_error",
                                       "message": "no account; add one or pass X-Mirofish-Account"}},
                            status_code=400)
    cached = state.model_cache.get(account)
    if cached and time.time() - cached[0] < state.settings.model_catalog_ttl:
        return {**cached[1], "default_model": state.settings.default_model}
    payload = await state.with_proxy(
        account, lambda url: state.accounts.model_list(account, proxy_url=url))
    payload["default_model"] = state.settings.default_model
    state.model_cache[account] = (time.time(), payload)
    return payload
