"""Transparent Codex Responses relay endpoints."""

from __future__ import annotations

from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request

from ..errors import RelayError
from ..upstream import (ALPHA_SEARCH_PATH, RESPONSES_PATH,
                        forwarded_response_headers)
from ..validate import model_value
from .deps import get_state, read_json_body_bytes, require_auth
from .relay import (_finalize_upstream_stream, _ManagedStreamingResponse,
                    _ResponsesUsageWatcher)

router = APIRouter(dependencies=[Depends(require_auth)])


def _safe_metadata(value: str | None, maximum: int = 256) -> str:
    value = (value or "").strip()
    if not value or len(value) > maximum \
            or any(not 0x21 <= ord(char) <= 0x7e for char in value):
        return ""
    return value


def _session_hint(request: Request) -> str:
    # X-Mirofish-Session is the explicit local override.  The remaining names
    # cover current Codex CLI builds without baking any of them into the signed
    # protocol itself.
    for name in (
        "x-mirofish-session", "session-id", "x-codex-session-id",
        "x-codex-window-id", "x-openai-session-id",
    ):
        value = _safe_metadata(request.headers.get(name), 128)
        if value:
            return value
    return ""


async def _codex_relay(request: Request, path: str) -> Any:
    state = get_state(request)
    body, payload = await read_json_body_bytes(request)
    model = payload.get("model")
    if model is not None and not isinstance(model, str):
        raise RelayError("invalid model name", 400)
    if model:
        # Validate only.  Unlike the Anthropic compatibility endpoint, this is a
        # transparent MITM path and therefore never reserializes Codex's JSON.
        model_value(model)
    # A Responses body may name a stored prompt instead of a model, so an absent
    # model is not a local error: upstream decides, and its protocol rejection
    # reaches the caller verbatim.

    requested = request.headers.get("X-Mirofish-Account", "")
    hint = _session_hint(request)
    relay_session = state.relay_session_id("", hint, payload)
    try:
        query_string = request.scope.get("query_string", b"").decode("ascii")
    except UnicodeDecodeError as exc:
        raise RelayError("invalid URL query encoding", 400) from exc

    async def run(account: str):
        row = state.store.row(account)
        account_id = _safe_metadata(
            str(row["user_id"]) if row["user_id"] is not None else "")
        return await state.open_responses_stream(
            account, body, request_headers=request.headers,
            session_id=relay_session, account_id=account_id,
            query_string=query_string, path=path)

    account, (response, stack) = await state.with_account_failover(
        requested, hint, payload, run)

    observer = _ResponsesUsageWatcher()

    async def body_stream() -> AsyncIterator[bytes]:
        if response.extensions.get("mirofish_body_decoded") is True:
            if response.content:
                observer.feed_bytes(response.content)
                yield response.content
            return
        async for chunk in response.aiter_raw():
            # Observed from a private buffer; the relayed bytes are unchanged.
            observer.feed_bytes(chunk)
            yield chunk

    upstream_headers = dict(response.headers)

    async def finalize() -> None:
        await _finalize_upstream_stream(
            stack, state, account, model, observer, upstream_headers)

    outgoing = [
        (name, value) for name, value in forwarded_response_headers(response)
        if name.lower() != "x-mirofish-account"
    ]
    outgoing.append(("X-Mirofish-Account", account))
    result = _ManagedStreamingResponse(
        body_stream(), finalize=finalize, status_code=response.status_code)
    # StreamingResponse's Mapping-based constructor would coalesce repeated
    # headers.  Assign the validated raw list so fields such as Set-Cookie are
    # relayed independently, as the desktop proxy does.
    result.raw_headers = [
        (name.encode("latin1"), value.encode("latin1"))
        for name, value in outgoing
    ]
    return result


# Each upstream endpoint is reachable under both the bare /v1 path and the
# /backend-api/codex prefix, matching the desktop MITM's own path mapping.
@router.post(RESPONSES_PATH)
async def responses(request: Request) -> Any:
    return await _codex_relay(request, RESPONSES_PATH)


@router.post("/backend-api/codex/responses")
async def backend_responses(request: Request) -> Any:
    return await _codex_relay(request, RESPONSES_PATH)


@router.post(ALPHA_SEARCH_PATH)
async def alpha_search(request: Request) -> Any:
    return await _codex_relay(request, ALPHA_SEARCH_PATH)


@router.post("/backend-api/codex/alpha/search")
async def backend_alpha_search(request: Request) -> Any:
    return await _codex_relay(request, ALPHA_SEARCH_PATH)

