"""OpenAI-compatible /v1/chat/completions with true incremental streaming."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..errors import RelayError
from ..translate import (OpenAIStreamTranslator, anthropic_to_openai_response,
                         iter_sse_events, openai_to_anthropic)
from ..upstream import quota_headers
from .deps import get_state, read_json_body, require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


def _dump(chunk: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(chunk, ensure_ascii=False,
                                  separators=(",", ":")).encode("utf-8") + b"\n\n"


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    state = get_state(request)
    payload = await read_json_body(request)
    session_hint = request.headers.get("X-Mirofish-Session", "")
    requested = request.headers.get("X-Mirofish-Account", "")
    relay_session = state.relay_session_id("", session_hint, payload)
    if not payload.get("model"):
        payload["model"] = state.settings.default_model
    anthropic_payload = openai_to_anthropic(payload)
    model = str(payload.get("model"))

    if not payload.get("stream"):
        async def run(account: str):
            return await state.with_proxy(
                account,
                lambda proxy_url: state.upstream.messages(
                    account, anthropic_payload, proxy_url, session_id=relay_session))
        account, (result, headers) = await state.with_account_failover(
            requested, session_hint, payload, run)
        usage = result.get("usage", {}) if isinstance(result, dict) else {}
        outgoing = state.record_usage(account, model, usage, headers)
        return JSONResponse(anthropic_to_openai_response(result, model), headers=outgoing)

    anthropic_payload["stream"] = True

    async def run_stream(account: str):
        return await state.open_messages_stream(
            account, anthropic_payload, session_id=relay_session)
    account, (response, stack) = await state.with_account_failover(
        requested, session_hint, payload, run_stream)
    upstream_headers = {key.lower(): value for key, value in response.headers.items()}
    quota = quota_headers(upstream_headers)
    outgoing = {"X-Mirofish-Account": account, "Cache-Control": "no-cache"}
    if quota.get("7d_utilization"):
        outgoing["X-Mirofish-Quota-7d-Utilization"] = str(quota["7d_utilization"])
    if quota.get("7d_reset_epoch"):
        outgoing["X-Mirofish-Quota-7d-Reset"] = str(quota["7d_reset_epoch"])

    translator = OpenAIStreamTranslator(model)

    async def body() -> AsyncIterator[bytes]:
        try:
            async for event, data in iter_sse_events(response.aiter_lines()):
                for chunk in translator.feed(event, data):
                    yield _dump(chunk)
        except RelayError as exc:
            yield _dump({"error": {"message": str(exc), "type": "relay_error",
                                   "code": exc.status}})
        finally:
            yield b"data: [DONE]\n\n"
            await stack.aclose()
            state.record_usage(account, model, translator.usage, upstream_headers)

    return StreamingResponse(body(), media_type="text/event-stream", headers=outgoing)
