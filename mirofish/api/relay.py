"""Anthropic-compatible relay endpoints: /v1/messages (with true streaming
passthrough) and the cached per-account /v1/models catalog."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Awaitable, Callable

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..upstream import _claude_compatible_payload, quota_headers
from ..validate import model_value
from .deps import get_state, read_json_body, read_json_body_bytes, require_auth

router = APIRouter(dependencies=[Depends(require_auth)])
logger = logging.getLogger("mirofish.relay")

# We only need the small message_start/message_delta usage objects. A malformed
# upstream event must not make the observation side-buffer grow with the full
# streamed response.
_MAX_USAGE_LINE_BYTES = 256 * 1024


def _beta_enabled(request: Request) -> bool:
    return request.query_params.get("beta", "").strip().lower() == "true"


class _UsageWatcher:
    """Extract usage numbers from an Anthropic SSE stream as it passes through."""

    def __init__(self) -> None:
        self.usage: dict[str, Any] = {}
        self._buffer = bytearray()
        self._discard_until_newline = False

    def feed_bytes(self, chunk: bytes) -> None:
        """Observe complete SSE lines without changing the relayed bytes.

        HTTP chunks can split a UTF-8 code point or an SSE line anywhere, so
        parsing happens from a private buffer. The caller still yields the
        original ``chunk`` byte-for-byte.
        """
        start = 0
        while start < len(chunk):
            if self._discard_until_newline:
                newline = chunk.find(b"\n", start)
                if newline < 0:
                    return
                self._discard_until_newline = False
                start = newline + 1
                continue

            newline = chunk.find(b"\n", start)
            end = newline if newline >= 0 else len(chunk)
            segment_length = end - start
            if len(self._buffer) + segment_length > _MAX_USAGE_LINE_BYTES:
                self._buffer.clear()
                if newline < 0:
                    self._discard_until_newline = True
                    return
                # This oversized line ends inside the current chunk. Skip it
                # and resume observing the next line without copying it.
            else:
                self._buffer.extend(memoryview(chunk)[start:end])
                if newline >= 0:
                    raw = bytes(self._buffer)
                    self._buffer.clear()
                    if raw.endswith(b"\r"):
                        raw = raw[:-1]
                    self._feed_raw_line(raw)
            if newline < 0:
                return
            start = newline + 1

    def finish(self) -> None:
        """Parse a final SSE line even when the stream has no trailing newline."""
        if self._buffer and not self._discard_until_newline:
            raw = bytes(self._buffer)
            self._buffer.clear()
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            self._feed_raw_line(raw)
        self._buffer.clear()
        self._discard_until_newline = False

    def _feed_raw_line(self, raw: bytes) -> None:
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            return
        self.feed_line(line)

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


class _ResponsesUsageWatcher(_UsageWatcher):
    """Extract usage numbers from a Codex Responses SSE stream.

    Only the terminal events carry a usage object, and they report cumulative
    totals, so there is nothing to accumulate across deltas. Reuses the parent's
    line framing so the relayed bytes stay untouched.
    """

    _TERMINAL_EVENTS = {"response.completed", "response.incomplete",
                        "response.failed"}

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
        if not isinstance(data, dict) or data.get("type") not in self._TERMINAL_EVENTS:
            return
        response = data.get("response")
        usage = response.get("usage") if isinstance(response, dict) else None
        if isinstance(usage, dict):
            self.usage.update(usage)


class _ManagedStreamingResponse(StreamingResponse):
    """Always release an already-open upstream stream and proxy route.

    Starlette may cancel response sending before it ever enters the body
    iterator (for example, a disconnect while sending response headers). An
    async-generator ``finally`` cannot cover that case, so ownership lives at
    the outer ASGI response boundary.
    """

    def __init__(self, *args: Any, finalize: Callable[[], Awaitable[None]],
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._finalize = finalize
        self._finalized = False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            if not self._finalized:
                self._finalized = True
                # The disconnect path runs under cancellation. Closing the
                # response/route lock is cleanup, not optional response work.
                with anyio.CancelScope(shield=True):
                    close_iterator = getattr(self.body_iterator, "aclose", None)
                    if close_iterator is not None:
                        try:
                            await close_iterator()
                        except Exception:  # noqa: BLE001 - still release route
                            logger.warning("could not close downstream body iterator")
                    await self._finalize()


async def _finalize_upstream_stream(
        stack: Any, state: Any, account: str, model: str | None,
        observer: Any, upstream_headers: dict[str, str]) -> None:
    finish = getattr(observer, "finish", None)
    if finish is not None:
        finish()
    try:
        await stack.aclose()
    except Exception:  # noqa: BLE001 - cleanup continues without leaking details
        logger.warning("could not fully close upstream stream: account=%s", account)
    try:
        state.record_usage(account, model, observer.usage, upstream_headers)
    except Exception:  # noqa: BLE001 - response cleanup must never be undone
        logger.warning("could not persist streamed usage: account=%s", account)


@router.post("/v1/messages")
async def messages(request: Request) -> Any:
    state = get_state(request)
    raw_body, payload = await read_json_body_bytes(request)
    validated_model = model_value(str(payload.get("model", "")))
    if payload.get("model") != validated_model:
        # A configured alias/validation normalization changed the object; the
        # bytes must be regenerated so the signed body matches that object.
        raw_body = None
    payload["model"] = validated_model
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
                    session_id=relay_session, beta=beta, raw_body=raw_body))
        account, (result, headers) = await state.with_account_failover(
            requested, session_hint, payload, run)
        usage = result.get("usage", {}) if isinstance(result, dict) else {}
        outgoing = state.record_usage(account, model, usage, headers)
        return JSONResponse(result, headers=outgoing)

    async def run_stream(account: str):
        return await state.open_messages_stream(
            account, payload, request_headers=request.headers,
            session_id=relay_session, beta=beta, raw_body=raw_body)
    account, (response, stack) = await state.with_account_failover(
        requested, session_hint, payload, run_stream)
    upstream_headers = {key.lower(): value for key, value in response.headers.items()}
    quota = quota_headers(upstream_headers)
    outgoing = {"X-Mirofish-Account": account}
    if quota.get("7d_utilization"):
        outgoing["X-Mirofish-Quota-7d-Utilization"] = str(quota["7d_utilization"])
    if quota.get("7d_reset_epoch"):
        outgoing["X-Mirofish-Quota-7d-Reset"] = str(quota["7d_reset_epoch"])

    watcher = _UsageWatcher()

    async def body() -> AsyncIterator[bytes]:
        async for chunk in response.aiter_bytes():
            watcher.feed_bytes(chunk)
            yield chunk

    async def finalize() -> None:
        await _finalize_upstream_stream(
            stack, state, account, model, watcher, upstream_headers)

    return _ManagedStreamingResponse(
        body(), finalize=finalize, media_type="text/event-stream",
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
    payload = _claude_compatible_payload(payload)
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
    account = state.pick_account(requested)
    cached = state.model_cache.get(account)
    if cached and time.time() - cached[0] < state.settings.model_catalog_ttl:
        return {**cached[1], "default_model": state.settings.default_model}
    payload = await state.with_proxy(
        account, lambda url: state.accounts.model_list(account, proxy_url=url))
    payload["default_model"] = state.settings.default_model
    state.model_cache[account] = (time.time(), payload)
    return payload
