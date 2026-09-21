"""Anthropic-compatible relay endpoints: /v1/messages (with true streaming
passthrough) and the cached per-account /v1/models catalog."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..errors import RelayError
from ..upstream import (_claude_compatible_payload, quota_headers,
                        relay_session_identity)
from ..validate import model_value
from .deps import get_state, read_json_body, read_json_body_bytes, require_auth
from .state import ACCOUNT_GENERATION_EXTENSION

router = APIRouter(dependencies=[Depends(require_auth)])
logger = logging.getLogger("mirofish.relay")

# We only need the small message_start/message_delta usage objects. A malformed
# upstream event must not make the observation side-buffer grow with the full
# streamed response.
_MAX_USAGE_LINE_BYTES = 256 * 1024


def _stream_refusal(data: dict[str, Any]) -> RelayError | None:
    """Translate HTTP-200 error events into the normal account error envelope."""
    response = data.get("response")
    body = response if isinstance(response, dict) else data
    error = body.get("error", body)
    if not isinstance(error, dict):
        return None
    kind = error.get("type") or error.get("code")
    code = str(error.get("code") or "")
    status = (error.get("status_code") or error.get("status")
              or data.get("status_code") or data.get("status"))
    if not isinstance(status, int) or isinstance(status, bool):
        status = {
            "rate_limit_error": 429, "rate_limit_exceeded": 429,
            "usage_limit_reached": 429, "shared_quota_unavailable": 429,
            "permission_error": 403, "overloaded_error": 503,
            "authentication_error": 401,
        }.get(kind)
        if code.startswith("credit_exhausted") or str(kind).startswith("credit_exhausted"):
            status = 429
    if status not in (401, 403, 429, 503):
        return None
    if not error.get("type") and kind:
        error = {**error, "type": kind}
    return RelayError("upstream stream rejected", status, {"error": error})


def _beta_enabled(request: Request) -> bool:
    return request.query_params.get("beta", "").strip().lower() == "true"


class _UsageWatcher:
    """Extract usage numbers from an Anthropic SSE stream as it passes through."""

    def __init__(self) -> None:
        self.usage: dict[str, Any] = {}
        self._buffer = bytearray()
        self._discard_until_newline = False
        self.started = False
        self.successful = False
        self.failed = False
        self.exhausted = False
        self.refusal: RelayError | None = None

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
            self.started = True
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            self.usage.update(usage)
        elif kind == "message_delta":
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            self.usage.update(usage)
        elif kind == "message_stop":
            self.successful = self.started and not self.failed
        elif kind == "error":
            self.failed = True
            self.successful = False
            self.refusal = self.refusal or _stream_refusal(data)


class _ResponsesUsageWatcher(_UsageWatcher):
    """Extract usage numbers from a Codex Responses SSE stream.

    Only the terminal events carry a usage object, and they report cumulative
    totals, so there is nothing to accumulate across deltas. Reuses the parent's
    line framing so the relayed bytes stay untouched.
    """

    _TERMINAL_EVENTS = {"response.completed", "response.incomplete",
                        "response.failed", "error"}

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
        if data.get("type") == "response.completed":
            self.successful = isinstance(response, dict) and not response.get("error")
        else:
            self.failed = True
            self.successful = False
            self.refusal = self.refusal or _stream_refusal(data)


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
        observer: Any, upstream_headers: dict[str, str],
        account_generation: str | None = None) -> None:
    finish = getattr(observer, "finish", None)
    if finish is not None:
        finish()
    try:
        await stack.aclose()
    except Exception:  # noqa: BLE001 - cleanup continues without leaking details
        logger.warning("could not fully close upstream stream: account=%s", account)
    try:
        if observer.refusal is not None and (
                account_generation is None
                or state.store.account_generation(account) == account_generation):
            # The stream is already visible to the caller: update health/quota
            # exactly once, but never retry its model work on another account.
            if observer.refusal.status == 429:
                try:
                    await state.refresh_limits_if_stale(account, force=True)
                except RelayError:
                    pass
            # Refresh is an await point and may race alias replacement too.
            if (account_generation is None
                    or state.store.account_generation(account) == account_generation):
                state.note_account_unserviceable(account, observer.refusal)
        if account_generation is None:
            state.record_usage(account, model, observer.usage, upstream_headers)
        else:
            state.record_usage(account, model, observer.usage, upstream_headers,
                               account_generation=account_generation)
        if observer.exhausted and observer.successful and not observer.failed:
            state.note_account_healthy(
                account, account_generation=account_generation)
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
    claude_session = request.headers.get("X-Claude-Code-Session-Id", "")
    beta = _beta_enabled(request)
    model = payload.get("model") if isinstance(payload.get("model"), str) else None

    if state.settings.one_token_short_circuit and _is_one_token_probe(payload):
        envelope, outgoing = await _answer_one_token_probe(
            state, request, payload, model, stream=bool(payload.get("stream")))
        if not payload.get("stream"):
            return JSONResponse(envelope, headers=outgoing)

        async def probe_body() -> AsyncIterator[bytes]:
            for event, data in _probe_stream_events(envelope):
                yield _sse_frame(event, data)

        return StreamingResponse(
            probe_body(), media_type="text/event-stream",
            headers={**outgoing, "Cache-Control": "no-cache"})

    if not payload.get("stream"):
        async def run(account: str):
            generation = state.store.account_generation(account)
            relay_session, headers, prepared = relay_session_identity(
                state.relay_session_id, account, request.headers, payload,
                claude_session=claude_session, session_hint=session_hint)
            result = await state.with_proxy(
                account,
                lambda proxy_url: state.upstream.messages(
                    account, prepared, proxy_url, request_headers=headers,
                    session_id=relay_session, beta=beta,
                    raw_body=(raw_body if prepared is payload
                              and payload.get("model") == validated_model else None)))
            return result, generation
        account, (upstream_result, account_generation) = await state.with_account_failover(
            requested, session_hint, payload, run)
        result, headers = upstream_result
        model = payload.get("model")
        usage = result.get("usage", {}) if isinstance(result, dict) else {}
        outgoing = state.record_usage(account, model, usage, headers,
                                      account_generation=account_generation)
        if (isinstance(result, dict) and result.get("type") == "message"
                and result.get("stop_reason") and not result.get("error")):
            state.note_account_healthy(
                account, account_generation=account_generation)
        return JSONResponse(result, headers=outgoing)

    async def run_stream(account: str):
        relay_session, headers, prepared = relay_session_identity(
            state.relay_session_id, account, request.headers, payload,
            claude_session=claude_session, session_hint=session_hint)
        return await state.open_messages_stream(
            account, prepared, request_headers=headers,
            session_id=relay_session, beta=beta,
            raw_body=(raw_body if prepared is payload
                      and payload.get("model") == validated_model else None))
    account, (response, stack) = await state.with_account_failover(
        requested, session_hint, payload, run_stream)
    model = payload.get("model")
    account_generation = response.extensions.get(ACCOUNT_GENERATION_EXTENSION)
    if not isinstance(account_generation, str):
        account_generation = None
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
        watcher.exhausted = True

    async def finalize() -> None:
        await _finalize_upstream_stream(
            stack, state, account, model, watcher, upstream_headers,
            account_generation=account_generation)

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


async def _input_token_count(state: Any, request: Request,
                             payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Route an account and count a payload's input tokens.

    Counts obey the same quota gate as generation, but cannot prove an account
    healthy. Missing endpoints/network failures retain the local estimate.
    """
    session_hint = request.headers.get("X-Mirofish-Session", "")
    async def run(account: str):
        relay_session, headers, prepared = relay_session_identity(
            state.relay_session_id, account, request.headers, payload,
            claude_session=request.headers.get("X-Claude-Code-Session-Id", ""),
            session_hint=session_hint)

        async def op(proxy_url):
            return await state.upstream.signed_json(
                account, "POST", "/v1/messages/count_tokens", prepared, proxy_url,
                request_headers=headers, session_id=relay_session,
                beta=_beta_enabled(request))
        try:
            status, _, data = await state.with_proxy(account, op)
            if 200 <= status < 300 and isinstance(data, dict) and "input_tokens" in data:
                return data
            if status >= 400:
                raise RelayError("token count rejected", status, data)
        except RelayError as exc:
            # Account refusals belong to failover/health handling, not a
            # successful local count that would hide the quota gate.
            if exc.status not in (404, 501, 502):
                raise
        return {"input_tokens": _estimate_input_tokens(payload)}

    return await state.with_account_failover(
        request.headers.get("X-Mirofish-Account", ""), session_hint, payload,
        run, model_only=False)


def _is_one_token_probe(payload: dict[str, Any]) -> bool:
    """True for a Messages request that can emit at most one output token.

    The upstream reads that shape as an availability probe rather than work and
    answers 400, pointing the caller at /v1/limits. A missing max_tokens is not
    a probe; it stays the upstream's own validation problem.
    """
    max_tokens = payload.get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
        return False
    return max_tokens <= 1


def _probe_envelope(model: str | None, input_tokens: int) -> dict[str, Any]:
    """A structurally valid Messages response for a one-token probe.

    Empty content with stop_reason "max_tokens" is what a request capped at one
    token can honestly return: nothing was generated, and nothing is invented
    here either.
    """
    return {
        "id": "msg_" + uuid.uuid4().hex,
        "type": "message",
        "role": "assistant",
        "model": model or "",
        "content": [],
        "stop_reason": "max_tokens",
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": 0},
    }


def _probe_stream_events(envelope: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The probe envelope as an Anthropic SSE event sequence.

    Single source of truth for both streaming paths: /v1/messages serializes
    these frames as-is and the OpenAI path feeds them to OpenAIStreamTranslator.
    """
    return [
        ("message_start", {"type": "message_start", "message": envelope}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "max_tokens",
                                     "stop_sequence": None},
                           "usage": {"output_tokens": 0}}),
        ("message_stop", {"type": "message_stop"}),
    ]


def _sse_frame(event: str, data: dict[str, Any]) -> bytes:
    return (f"event: {event}\ndata: "
            + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            + "\n\n").encode("utf-8")


async def _answer_one_token_probe(
        state: Any, request: Request, payload: dict[str, Any],
        model: str | None, *, stream: bool) -> tuple[dict[str, Any], dict[str, str]]:
    """Answer synthetically with zero upstream work, including account routing.

    Returns the Messages envelope and the relay's response headers. The caller's
    user agent is logged because the access log only carries the source address,
    which is the Docker bridge for anything reaching a published port.
    """
    logger.info(
        "answered synthetic one-token probe locally: model=%s max_tokens=%s "
        "tools=%s stream=%s user_agent=%s",
        model, payload.get("max_tokens"),
        len(payload["tools"]) if isinstance(payload.get("tools"), list) else 0,
        stream, request.headers.get("user-agent", "-"))
    envelope = _probe_envelope(model, _estimate_input_tokens(payload))
    return envelope, {"X-Mirofish-Probe": "short-circuit",
                      "X-Mirofish-Synthetic": "true"}


@router.post("/v1/messages/count_tokens")
async def count_tokens(request: Request) -> Any:
    """Anthropic token-counting endpoint. Proxied to upstream (device-signed,
    not billable); falls back to a local estimate if upstream lacks it or the
    proxy hop fails, so clients never see a 404 and stop retry-storming."""
    state = get_state(request)
    payload = await read_json_body(request)
    payload["model"] = model_value(str(payload.get("model", "")))
    payload = _claude_compatible_payload(payload)
    account, data = await _input_token_count(state, request, payload)
    return JSONResponse(data, headers={"X-Mirofish-Account": account})


@router.get("/v1/models")
async def models(request: Request) -> Any:
    state = get_state(request)
    requested = request.headers.get("X-Mirofish-Account", "").strip()
    account = state.pick_catalog_account(requested)
    generation = state.store.account_generation(account)
    try:
        return await state.with_proxy(
            account, lambda url: state.accounts.model_list(account, proxy_url=url))
    except RelayError as exc:
        try:
            if generation == state.store.account_generation(account):
                state.note_account_error(account, exc)
        except RelayError:
            pass  # Deletion raced the catalog response.
        raise
