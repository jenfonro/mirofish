"""Anthropic-compatible relay endpoints: /v1/messages (with true streaming
passthrough) and the cached per-account /v1/models catalog."""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..upstream import quota_headers
from .deps import get_state, read_json_body, require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


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
    account = state.pick_account(request.headers.get("X-Mirofish-Account", ""))
    payload = await read_json_body(request)
    model = payload.get("model") if isinstance(payload.get("model"), str) else None

    if not payload.get("stream"):
        async def op(proxy_url):
            return await state.upstream.messages(account, payload, proxy_url)
        result, headers = await state.with_proxy(account, op)
        usage = result.get("usage", {}) if isinstance(result, dict) else {}
        outgoing = state.record_usage(account, model, usage, headers)
        return JSONResponse(result, headers=outgoing)

    response, stack = await state.open_messages_stream(account, payload)
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
        return cached[1]
    payload = await state.with_proxy(
        account, lambda url: state.accounts.model_list(account, proxy_url=url))
    state.model_cache[account] = (time.time(), payload)
    return payload
