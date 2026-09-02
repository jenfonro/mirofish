import base64
import gzip
import hashlib
import json
import uuid
from typing import Any

import httpx
import respx
from cryptography.hazmat.primitives import serialization

from mirofish.device import metadata_digest
from mirofish.upstream import RESPONSES_PATH
from tests.conftest import AUTH_BASE, RELAY_BASE, add_account


def _device_session(result: httpx.Response | None = None):
    return respx.post(RELAY_BASE + "/v1/device/session").mock(
        return_value=result or httpx.Response(
            200, json={"ticket": "device-ticket", "expiresIn": 900}))


def _verify_signature(state, request: httpx.Request,
                      path: str = RESPONSES_PATH) -> None:
    """Recompute mrs-sig-v2 from the headers the request actually carries."""
    signer = state.upstream._signer("work")
    meta = {name.lower(): value for name, value in request.headers.items()
            if name.lower().startswith("x-mirasim-")
            and name.lower() not in (
                "x-mirasim-device", "x-mirasim-ts", "x-mirasim-nonce",
                "x-mirasim-sig", "x-mirasim-client")}
    credential = request.headers.get("authorization", "").removeprefix("Bearer ")
    canonical = "\n".join((
        "mrs-sig-v2",
        "POST",
        path,
        request.headers["x-mirasim-ts"],
        request.headers["x-mirasim-nonce"],
        signer.device_id,
        signer.client_version,
        hashlib.sha256(credential.encode("utf-8")).hexdigest(),
        metadata_digest(meta),
        hashlib.sha256(request.content).hexdigest(),
    )).encode("utf-8")
    signature_text = request.headers["x-mirasim-sig"]
    signature = base64.urlsafe_b64decode(
        signature_text + "=" * (-len(signature_text) % 4))
    public = serialization.load_der_public_key(base64.b64decode(signer.public_key))
    public.verify(signature, canonical)


@respx.mock
async def test_codex_compressed_body_is_decompressed_then_signed_verbatim(
        client, state, auth_headers):
    add_account(state, "work")
    _device_session()
    route = respx.post(
        RELAY_BASE + "/v1/responses?beta=true&trace=one").mock(
            return_value=httpx.Response(
                200,
                content=b'event: response.completed\ndata: {"ok":true}\n\n',
                headers=[
                    ("content-type", "text/event-stream"),
                    ("x-upstream-id", "response-1"),
                    ("set-cookie", "first=1; Path=/"),
                    ("set-cookie", "second=2; Path=/"),
                ],
            ))
    raw = (
        b'{\n  "model": "gpt-5.6-codex",\n  "stream": true, '
        b'"input": [{"role":"user","content":"hello"}]\n}'
    )

    response = await client.post(
        "/backend-api/codex/responses?beta=true&trace=one",
        content=gzip.compress(raw),
        headers={
            **auth_headers,
            "authorization": "Bearer codex-caller-secret",
            "content-type": "application/json; charset=utf-8",
            "content-encoding": "gzip",
            "content-digest": "sha-256=:caller-value:",
            "cookie": "codex-session=retained",
            "openai-beta": "responses=experimental",
            "originator": "caller-originator",
            "x-mirasim-device": "caller-spoof",
            "x-mirasim-sig": "caller-spoof",
            "x-mirasim-agent": "caller-spoof",
            "x-mirasim-probe": "usage",
            "x-mirasim-collect": "off",
            "x-mirasim-repo": "/Users/victim/private-repo",
            "x-forwarded-for": "203.0.113.1",
            "session-id": "codex-window-7",
        },
    )

    assert response.status_code == 200
    assert response.content == b'event: response.completed\ndata: {"ok":true}\n\n'
    assert response.headers["x-upstream-id"] == "response-1"
    assert response.headers.get_list("set-cookie") == [
        "first=1; Path=/", "second=2; Path=/"]
    assert response.headers["x-mirofish-account"] == "work"

    request = route.calls.last.request
    assert request.content == raw
    assert request.headers["authorization"] == "Bearer device-ticket"
    assert "codex-caller-secret" not in request.headers.values()
    assert request.headers["content-length"] == str(len(raw))
    assert request.headers["content-type"] == "application/json; charset=utf-8"
    assert request.headers["openai-beta"] == "responses=experimental"
    assert request.headers["x-mirasim-agent"] == "codex"
    assert request.headers["x-mirasim-account"] == "u-work"
    assert request.headers["originator"] == "mirasim"
    assert uuid.UUID(request.headers["x-mirasim-session"]).version == 4
    assert "content-encoding" not in request.headers
    assert "content-digest" not in request.headers
    assert "x-forwarded-for" not in request.headers
    # The relay multiplexes accounts, so a caller cookie would ride upstream
    # bound to another account's device ticket; and every x-mirasim-* field is
    # part of the relay's own signing envelope, never the caller's to set.
    assert "cookie" not in request.headers
    for name in ("x-mirasim-probe", "x-mirasim-collect", "x-mirasim-repo"):
        assert name not in request.headers
    assert "caller-spoof" not in request.headers.values()
    assert "caller-originator" not in request.headers.values()
    _verify_signature(state, request)


@respx.mock
async def test_codex_stream_records_usage_and_leaves_bytes_untouched(
        client, state, auth_headers):
    add_account(state, "work")
    _device_session()
    stream = (
        b'event: response.created\ndata: {"type":"response.created"}\n\n'
        b'event: response.output_text.delta\n'
        b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
        b'event: response.completed\n'
        b'data: {"type":"response.completed","response":{"id":"resp_1",'
        b'"usage":{"input_tokens":11,"output_tokens":4,"total_tokens":15}}}\n\n'
    )
    respx.post(RELAY_BASE + RESPONSES_PATH).mock(
        return_value=httpx.Response(
            200, content=stream,
            headers={"content-type": "text/event-stream"}))

    response = await client.post(
        "/v1/responses", headers=auth_headers,
        content=b'{"model":"gpt-5.6-codex","stream":true,"input":"hi"}')

    assert response.status_code == 200
    assert response.content == stream
    assert state.store.usage_summary(1)["totals"] == {
        "requests": 1, "input_tokens": 11, "output_tokens": 4}


@respx.mock
async def test_codex_account_scoped_429_fails_over_to_another_account(
        client, state, auth_headers):
    """A 429 on the Codex path moves the conversation to another account.

    Passing the refusal through verbatim would leave the session pinned to
    the refused account, so the caller's retry lands straight back on it.
    """
    add_account(state, "first")
    add_account(state, "second")
    _device_session()
    route = respx.post(RELAY_BASE + RESPONSES_PATH).mock(side_effect=[
        httpx.Response(429, json={"error": {"type": "usage_limit_reached"}}),
        httpx.Response(200, content=b'event: response.completed\ndata: {}\n\n',
                       headers={"content-type": "text/event-stream"}),
    ])

    response = await client.post(
        "/v1/responses", headers=auth_headers,
        content=b'{"model":"gpt-5.6-codex","stream":true,"input":"hi"}')

    assert response.status_code == 200
    assert response.headers["x-mirofish-account"] == "second"
    assert route.call_count == 2
    assert state.exhausted_cooldown("first") > 0


@respx.mock
async def test_alpha_search_relays_under_both_local_paths(
        client, state, auth_headers):
    add_account(state, "work")
    _device_session()
    route = respx.post(RELAY_BASE + "/v1/alpha/search").mock(
        return_value=httpx.Response(200, json={"results": []}))
    raw = b'{"query":"needle"}'

    first = await client.post(
        "/v1/alpha/search", headers=auth_headers, content=raw)
    second = await client.post(
        "/backend-api/codex/alpha/search", headers=auth_headers, content=raw)

    assert first.status_code == second.status_code == 200
    assert len(route.calls) == 2
    for call in route.calls:
        request = call.request
        assert request.content == raw
        assert request.headers["x-mirasim-agent"] == "codex"
        assert request.headers["authorization"] == "Bearer device-ticket"
    # Signed over its own pathname, not /v1/responses.
    _verify_signature(state, route.calls.last.request, path="/v1/alpha/search")


@respx.mock
async def test_responses_body_may_name_a_stored_prompt_instead_of_a_model(
        client, state, auth_headers):
    add_account(state, "work")
    _device_session()
    route = respx.post(RELAY_BASE + RESPONSES_PATH).mock(
        return_value=httpx.Response(200, json={"id": "response"}))
    raw = b'{"prompt":{"id":"pmpt_1","version":"3"},"input":"hi"}'

    response = await client.post(
        "/v1/responses", headers=auth_headers, content=raw)

    assert response.status_code == 200
    assert route.calls.last.request.content == raw


@respx.mock
async def test_responses_upstream_rejection_does_not_credit_the_exit(
        client, state, auth_headers):
    add_account(state, "work")
    _device_session()
    respx.post(RELAY_BASE + RESPONSES_PATH).mock(
        return_value=httpx.Response(
            503, json={"error": {"type": "upstream_unavailable"}}))
    credited: list[Any] = []
    state.pool.success = credited.append  # type: ignore[method-assign]

    response = await client.post(
        "/v1/responses", headers=auth_headers,
        content=b'{"model":"gpt-5.6-codex","input":"hi"}')

    # The rejection reaches the caller verbatim, but a node that never served
    # the request must not have its failure counter cleared.
    assert response.status_code == 503
    assert credited == []


@respx.mock
async def test_responses_unsupported_model_422_is_not_a_signature_error(
        client, state, auth_headers):
    add_account(state, "work")
    _device_session()
    upstream_body = (
        b'{ "error": {"type":"unsupported_model",'
        b' "message":"model is not supported"} }'
    )
    respx.post(RELAY_BASE + RESPONSES_PATH).mock(
        return_value=httpx.Response(
            422, content=upstream_body,
            headers={"content-type": "application/json", "x-request-id": "req-422"}))

    response = await client.post(
        "/v1/responses", headers=auth_headers,
        content=b'{"model":"missing-codex-model","input":"hi"}')

    assert response.status_code == 422
    assert response.content == upstream_body
    assert response.headers["x-request-id"] == "req-422"
    assert response.json()["error"]["type"] == "unsupported_model"


@respx.mock
async def test_device_session_unsupported_falls_back_to_unsigned_account_token(
        client, state, auth_headers):
    add_account(state, "work")
    device = _device_session(httpx.Response(
        404, json={"error": {"type": "not_found"}}))
    route = respx.post(RELAY_BASE + RESPONSES_PATH).mock(
        return_value=httpx.Response(200, json={"id": "response"}))
    raw = b'{"model":"gpt-5.6-codex","input":"hi"}'

    first = await client.post("/v1/responses", headers=auth_headers, content=raw)
    second = await client.post("/v1/responses", headers=auth_headers, content=raw)

    assert first.status_code == second.status_code == 200
    assert len(device.calls) == 1
    for call in route.calls:
        request = call.request
        assert request.headers["authorization"] == "Bearer access-work"
        assert request.headers["x-mirasim-client"] == "0.0.228"
        # Unsigned fallback still carries the real installation device id, not a
        # differently shaped stand-in.
        assert request.headers["x-mirasim-device"] == \
            state.upstream._signer("work").device_id
        for name in ("x-mirasim-ts", "x-mirasim-nonce", "x-mirasim-sig"):
            assert name not in request.headers


@respx.mock
async def test_unsigned_fallback_refreshes_account_token_on_401(
        client, state, auth_headers):
    add_account(state, "work")
    _device_session(httpx.Response(
        404, json={"error": {"type": "not_found"}}))
    refresh = respx.post(AUTH_BASE + "/auth/refresh").mock(
        return_value=httpx.Response(200, json={
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
        }))
    route = respx.post(RELAY_BASE + RESPONSES_PATH).mock(side_effect=[
        httpx.Response(401, json={"error": {"type": "token_invalid"}}),
        httpx.Response(200, json={"id": "response"}),
    ])

    response = await client.post(
        "/v1/responses", headers=auth_headers,
        content=b'{"model":"gpt-5.6-codex","input":"hi"}')

    assert response.status_code == 200
    assert refresh.call_count == 1
    assert [call.request.headers["authorization"] for call in route.calls] == [
        "Bearer access-work", "Bearer fresh-access"]
    assert state.store.credentials("work") == ("fresh-access", "fresh-refresh")


@respx.mock
async def test_messages_preserve_an_already_complete_body_bytes(
        client, state, auth_headers):
    add_account(state, "work")
    _device_session()
    route = respx.post(RELAY_BASE + "/v1/messages?beta=true").mock(
        return_value=httpx.Response(200, json={"content": [], "usage": {}}))
    raw = json.dumps({
        "model": "claude-fable-5",
        "max_tokens": 8,
        "system": [{
            "type": "text",
            "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
            "cache_control": {"type": "ephemeral"},
        }],
        "messages": [{
            "role": "user",
            "content": [{
                "type": "text", "text": "hello",
                "cache_control": {"type": "ephemeral"},
            }],
        }],
    }, ensure_ascii=False, indent=2).encode("utf-8")

    response = await client.post(
        "/v1/messages?beta=true", headers=auth_headers, content=raw)

    assert response.status_code == 200
    assert route.calls.last.request.content == raw


async def test_invalid_or_oversized_compression_is_rejected_before_forwarding(
        client, state, auth_headers):
    add_account(state, "work")

    malformed = await client.post(
        "/v1/responses", content=b"not-gzip",
        headers={**auth_headers, "content-encoding": "gzip"})
    unsupported = await client.post(
        "/v1/responses", content=b"{}",
        headers={**auth_headers, "content-encoding": "snappy"})

    state.settings.max_body_bytes = 128
    oversized_raw = json.dumps({
        "model": "gpt-5.6-codex", "input": "a" * 400,
    }).encode("utf-8")
    oversized = await client.post(
        "/v1/responses", content=gzip.compress(oversized_raw),
        headers={**auth_headers, "content-encoding": "gzip"})

    assert malformed.status_code == 400
    assert unsupported.status_code == 415
    assert oversized.status_code == 413
