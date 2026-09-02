import gzip
import json
import uuid
from typing import Any

import httpx
import respx

from mirofish.config import DEFAULT_CODEX_USER_AGENT
from mirofish.upstream import RESPONSES_PATH, SIGNED_MODEL_REQUIRED_MESSAGE
from tests.conftest import AUTH_BASE, RELAY_BASE, add_account
from tests.mirasim_protocol import relay_metadata, verify_signature


def _device_session(result: httpx.Response | None = None):
    return respx.post(RELAY_BASE + "/v1/device/session").mock(
        return_value=result or httpx.Response(
            200, json={"ticket": "device-ticket", "expiresIn": 900}))


def _verify_signature(state, request: httpx.Request,
                      path: str = RESPONSES_PATH) -> None:
    verify_signature(state, request, path)


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
    # Fields the desktop's bundled Codex never sends are not relayed either.
    assert "openai-beta" not in request.headers
    assert "accept-encoding" not in request.headers
    assert request.headers["user-agent"] == DEFAULT_CODEX_USER_AGENT
    metadata = relay_metadata(request)
    assert metadata["x-mirasim-agent"] == "codex"
    assert metadata["x-mirasim-account"] == "u-work"
    assert request.headers["originator"] == "mirasim"
    assert uuid.UUID(metadata["x-mirasim-session"]).version == 4
    assert "content-encoding" not in request.headers
    assert "content-digest" not in request.headers
    assert "x-forwarded-for" not in request.headers
    # The relay multiplexes accounts, so a caller cookie would ride upstream
    # bound to another account's device ticket; and every x-mirasim-* field is
    # part of the relay's own signing envelope, never the caller's to set.
    assert "cookie" not in request.headers
    for name in ("x-mirasim-probe", "x-mirasim-collect", "x-mirasim-repo"):
        assert name not in request.headers
        assert name not in metadata
    assert "caller-spoof" not in request.headers.values()
    assert "caller-spoof" not in metadata.values()
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
        assert relay_metadata(request)["x-mirasim-agent"] == "codex"
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
async def test_device_session_unsupported_fails_closed_for_current_client(
        client, state, auth_headers):
    """A 0.0.272 model call never falls back to the plain account token.

    The upstream binds model traffic to a device ticket; sending the account
    bearer instead would silently bill the user's own account during a relay
    outage.  The request fails with 503 and nothing reaches the model route.
    """
    add_account(state, "work")
    device = _device_session(httpx.Response(
        404, json={"error": {"type": "not_found"}}))
    route = respx.post(RELAY_BASE + RESPONSES_PATH).mock(
        return_value=httpx.Response(200, json={"id": "response"}))
    raw = b'{"model":"gpt-5.6-codex","input":"hi"}'

    first = await client.post("/v1/responses", headers=auth_headers, content=raw)
    second = await client.post("/v1/responses", headers=auth_headers, content=raw)

    assert first.status_code == second.status_code == 503
    assert SIGNED_MODEL_REQUIRED_MESSAGE in first.text
    assert "device_session_required" in first.text
    # The unsupported route is remembered; the mint is not retried per request.
    assert len(device.calls) == 1
    assert len(route.calls) == 0


@respx.mock
async def test_device_session_unsupported_falls_back_to_unsigned_account_token(
        client, state, auth_headers):
    # The historical plain-token fallback exists only for pre-v2 profiles.
    state.settings.mirasim_client_version = "0.0.228"
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
    assert len(route.calls) == 2
    for call in route.calls:
        request = call.request
        assert request.headers["authorization"] == "Bearer access-work"
        assert request.headers["x-mirasim-client"] == "0.0.228"
        # Unsigned fallback still carries the real installation device id, not a
        # differently shaped stand-in.  Legacy profiles are never sealed.
        assert request.headers["x-mirasim-device"] == \
            state.upstream._signer("work").device_id
        for name in ("x-mirasim-ts", "x-mirasim-nonce", "x-mirasim-sig",
                     "x-mirasim-enc"):
            assert name not in request.headers


@respx.mock
async def test_unsigned_fallback_refreshes_account_token_on_401(
        client, state, auth_headers):
    state.settings.mirasim_client_version = "0.0.228"
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


@respx.mock
async def test_cloudflare_cookies_are_replayed_per_account_and_exit_only(
        client, state, auth_headers):
    """The bundled Codex keeps one cookie store per install.  The relay keeps
    one per (account, exit): another account never inherits the jar, and the
    Claude path (Node fetch, no store) never sends cookies at all."""
    add_account(state, "work")
    add_account(state, "other")
    _device_session()
    responses = respx.post(RELAY_BASE + RESPONSES_PATH).mock(side_effect=[
        httpx.Response(200, json={"id": "r1"}, headers=[
            ("set-cookie", "__cflb=lb; Path=/; Secure; HttpOnly")]),
        httpx.Response(200, json={"id": "r2"}),
        httpx.Response(200, json={"id": "r3"}),
    ])
    messages = respx.post(RELAY_BASE + "/v1/messages").mock(side_effect=[
        httpx.Response(200, json={"content": [], "usage": {}}, headers=[
            ("set-cookie", "__cflb=lb; Path=/; Secure; HttpOnly")]),
        httpx.Response(200, json={"content": [], "usage": {}}),
    ])
    raw = b'{"model":"gpt-5.6-codex","input":"hi"}'
    pinned = {**auth_headers, "X-Mirofish-Account": "work"}

    await client.post("/v1/responses", headers=pinned, content=raw)
    await client.post("/v1/responses", headers=pinned, content=raw)
    await client.post("/v1/responses", content=raw,
                      headers={**auth_headers, "X-Mirofish-Account": "other"})
    for _ in range(2):
        await client.post("/v1/messages", headers=pinned, json={
            "model": "claude-haiku-4-5", "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}]})

    first, second, other = (call.request for call in responses.calls)
    assert "cookie" not in first.headers
    assert second.headers["cookie"] == "__cflb=lb"
    assert "cookie" not in other.headers
    assert all("cookie" not in call.request.headers for call in messages.calls)

    # Re-login drops the jar with the rest of the account's route state.
    state.upstream.credentials_changed("work")
    assert not any(key[0] == "work" for key in state.upstream._cookie_jars)
