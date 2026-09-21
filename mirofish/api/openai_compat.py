"""OpenAI-compatible /v1/chat/completions with true incremental streaming."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..errors import RelayError
from ..translate import (OpenAIStreamTranslator, anthropic_to_openai_response,
                         iter_sse_events, iter_sse_lines, openai_to_anthropic)
from ..upstream import quota_headers, relay_session_identity
from ..validate import model_value
from .deps import get_state, read_json_body, require_auth
from .relay import (_ManagedStreamingResponse, _answer_one_token_probe,
                    _finalize_upstream_stream, _is_one_token_probe,
                    _probe_stream_events, _UsageWatcher)
from .state import ACCOUNT_GENERATION_EXTENSION

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
    if not payload.get("model"):
        payload["model"] = state.settings.default_model
    payload["model"] = model_value(str(payload["model"]))
    model = str(payload.get("model"))

    # Check before translation: its defaulting of falsy max_tokens would turn
    # zero into 4096, accidentally making a synthetic probe billable.
    probe_limit = payload.get("max_tokens", payload.get("max_completion_tokens"))
    if state.settings.one_token_short_circuit and _is_one_token_probe(
            {"max_tokens": probe_limit}):
        anthropic_payload = openai_to_anthropic(payload)
        envelope, outgoing = await _answer_one_token_probe(
            state, request, anthropic_payload, model,
            stream=bool(payload.get("stream")))
        if not payload.get("stream"):
            return JSONResponse(anthropic_to_openai_response(envelope, model),
                                headers=outgoing)
        probe_translator = OpenAIStreamTranslator(model)

        async def probe_body() -> AsyncIterator[bytes]:
            for event, data in _probe_stream_events(envelope):
                for chunk in probe_translator.feed(event, data):
                    yield _dump(chunk)
            yield b"data: [DONE]\n\n"

        return StreamingResponse(
            probe_body(), media_type="text/event-stream",
            headers={**outgoing, "Cache-Control": "no-cache"})

    if not payload.get("stream"):
        async def run(account: str):
            generation = state.store.account_generation(account)
            relay_session, _, _ = relay_session_identity(
                state.relay_session_id, account, request.headers, payload,
                claude_session=request.headers.get("X-Claude-Code-Session-Id", ""),
                session_hint=session_hint)
            anthropic_payload = openai_to_anthropic(payload)
            result = await state.with_proxy(
                account,
                lambda proxy_url: state.upstream.messages(
                    account, anthropic_payload, proxy_url, session_id=relay_session))
            return result, generation
        account, (upstream_result, account_generation) = await state.with_account_failover(
            requested, session_hint, payload, run)
        result, headers = upstream_result
        model = str(payload["model"])
        usage = result.get("usage", {}) if isinstance(result, dict) else {}
        outgoing = state.record_usage(account, model, usage, headers,
                                      account_generation=account_generation)
        if (isinstance(result, dict) and result.get("type") == "message"
                and result.get("stop_reason") and not result.get("error")):
            state.note_account_healthy(
                account, account_generation=account_generation)
        return JSONResponse(anthropic_to_openai_response(result, model), headers=outgoing)

    async def run_stream(account: str):
        relay_session, _, _ = relay_session_identity(
            state.relay_session_id, account, request.headers, payload,
            claude_session=request.headers.get("X-Claude-Code-Session-Id", ""),
            session_hint=session_hint)
        anthropic_payload = openai_to_anthropic(payload)
        anthropic_payload["stream"] = True
        return await state.open_messages_stream(
            account, anthropic_payload, session_id=relay_session)
    account, (response, stack) = await state.with_account_failover(
        requested, session_hint, payload, run_stream)
    model = str(payload["model"])
    account_generation = response.extensions.get(ACCOUNT_GENERATION_EXTENSION)
    if not isinstance(account_generation, str):
        account_generation = None
    upstream_headers = {key.lower(): value for key, value in response.headers.items()}
    quota = quota_headers(upstream_headers)
    outgoing = {"X-Mirofish-Account": account, "Cache-Control": "no-cache"}
    if quota.get("7d_utilization"):
        outgoing["X-Mirofish-Quota-7d-Utilization"] = str(quota["7d_utilization"])
    if quota.get("7d_reset_epoch"):
        outgoing["X-Mirofish-Quota-7d-Reset"] = str(quota["7d_reset_epoch"])

    translator = OpenAIStreamTranslator(model)
    watcher = _UsageWatcher()

    async def chunks() -> AsyncIterator[bytes]:
        async for chunk in response.aiter_bytes():
            watcher.feed_bytes(chunk)
            yield chunk
        watcher.exhausted = True

    async def body() -> AsyncIterator[bytes]:
        try:
            lines = iter_sse_lines(chunks())
            async for event, data in iter_sse_events(lines):
                for chunk in translator.feed(event, data):
                    yield _dump(chunk)
        except RelayError as exc:
            yield _dump({"error": {"message": str(exc), "type": "relay_error",
                                   "code": exc.status}})
        yield b"data: [DONE]\n\n"

    async def finalize() -> None:
        await _finalize_upstream_stream(
            stack, state, account, model, watcher, upstream_headers,
            account_generation=account_generation)

    return _ManagedStreamingResponse(
        body(), finalize=finalize, media_type="text/event-stream", headers=outgoing)
