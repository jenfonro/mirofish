import base64
import hashlib
import json
import uuid

import httpx
import pytest
import respx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization

from mirofish.errors import RelayError

from tests.conftest import AUTH_BASE, RELAY_BASE, add_account

ANTHROPIC_RESPONSE = {
    "id": "msg_1", "type": "message", "role": "assistant",
    "model": "claude-haiku-4-5-20251001",
    "content": [{"type": "text", "text": "你好！"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 12, "output_tokens": 4},
}

SSE_BODY = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"usage":{"input_tokens":9}}}\n\n'
    'event: content_block_start\n'
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}\n\n'
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n'
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n\n'
)

QUOTA_HEADERS = {"anthropic-ratelimit-unified-7d-utilization": "0.42",
                 "anthropic-ratelimit-unified-7d-reset": "1700000000"}


def mock_device_session(ticket: str = "device-ticket"):
    return respx.post(RELAY_BASE + "/v1/device/session").mock(
        return_value=httpx.Response(200, json={"ticket": ticket, "expiresIn": 900}))


def verify_relay_signature(state, request: httpx.Request, path: str) -> bytes:
    headers = request.headers
    body = request.content
    signed = "\n".join((
        "mrs-sig-v1", request.method, path, headers["x-mirasim-ts"],
        headers["x-mirasim-nonce"], hashlib.sha256(body).hexdigest(),
    )).encode()
    signature = base64.urlsafe_b64decode(
        headers["x-mirasim-sig"] + "=" * (-len(headers["x-mirasim-sig"]) % 4))
    public = serialization.load_der_public_key(
        base64.b64decode(state.upstream._signer("work").public_key))
    public.verify(signature, signed)
    return signature


async def test_requires_auth(client):
    response = await client.get("/health")
    assert response.status_code == 401
    response = await client.get("/health", headers={"X-Mirofish-Proxy-Key": "wrong"})
    assert response.status_code == 401


async def test_health_and_accounts(client, state, auth_headers):
    add_account(state, "work")
    response = await client.get("/health", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["accounts"] == 1
    response = await client.get("/accounts", headers=auth_headers)
    accounts = response.json()["accounts"]
    assert accounts[0]["alias"] == "work" and accounts[0]["plan"] == "pro"


async def test_bearer_auth_accepted(client, state):
    response = await client.get("/health",
                                headers={"Authorization": "Bearer " + state.proxy_key})
    assert response.status_code == 200


def test_pick_account_order(state):
    add_account(state, "alpha")
    add_account(state, "beta")
    assert state.pick_account("beta") == "beta"
    state.default_account = "alpha"
    assert state.pick_account("") == "alpha"
    state.default_account = ""
    first, second, third = (state.pick_account(""), state.pick_account(""),
                            state.pick_account(""))
    assert [first, second, third] == ["alpha", "beta", "alpha"]


def test_pick_account_skips_exhausted_quota(state):
    add_account(state, "alpha")
    add_account(state, "beta")
    state.store.merge_metadata("alpha", {"quota": {"7d_utilization": "1.0"}})
    assert state.pick_account("") == "beta"
    assert state.pick_account("") == "beta"


def _conv(text):
    return {"messages": [{"role": "user", "content": text}]}


def test_route_account_sticky_within_conversation(state):
    add_account(state, "alpha")
    add_account(state, "beta")
    conv = _conv("help me refactor this function")
    first = state.route_account("", "", conv)
    # Same conversation (first user message unchanged as it grows) -> same account.
    grown = {"messages": conv["messages"] + [
        {"role": "assistant", "content": "sure"},
        {"role": "user", "content": "now add tests"}]}
    for _ in range(5):
        assert state.route_account("", "", grown) == first


def test_route_account_new_windows_spread(state):
    add_account(state, "alpha")
    add_account(state, "beta")
    a = state.route_account("", "", _conv("window one topic"))
    b = state.route_account("", "", _conv("a totally different second topic"))
    # Two distinct windows land on the two different accounts, not the same one.
    assert {a, b} == {"alpha", "beta"}


def test_route_account_ignores_shared_system_prompt(state):
    add_account(state, "alpha")
    add_account(state, "beta")
    sys_block = {"role": "system", "content": "You are a coding assistant."}
    one = state.route_account("", "", {"messages": [sys_block, {"role": "user", "content": "task A"}]})
    two = state.route_account("", "", {"messages": [sys_block, {"role": "user", "content": "task B"}]})
    # Identical system prompt must not collapse different windows onto one account.
    assert {one, two} == {"alpha", "beta"}


def test_route_account_explicit_header_overrides(state):
    add_account(state, "alpha")
    add_account(state, "beta")
    assert state.route_account("beta", "", _conv("anything")) == "beta"


def test_route_account_session_header_sticky(state):
    add_account(state, "alpha")
    add_account(state, "beta")
    first = state.route_account("", "sess-123", _conv("x"))
    # Same session id -> same account even if the body differs.
    assert state.route_account("", "sess-123", _conv("completely different body")) == first


def test_relay_session_id_preserves_claude_id_and_hashes_local_hints(state):
    assert state.relay_session_id("claude-session-1", "local-secret", _conv("x")) == \
        "claude-session-1"
    first = state.relay_session_id("", "local-secret", _conv("private prompt"))
    second = state.relay_session_id("", "local-secret", _conv("changed prompt"))
    assert first == second and first.startswith("mirofish_")
    assert "local-secret" not in first and "private prompt" not in first


@respx.mock
async def test_messages_non_stream(client, state, auth_headers):
    add_account(state, "work")
    session = mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE, headers=QUOTA_HEADERS))
    response = await client.post("/v1/messages", headers=auth_headers, json={
        "model": "claude-haiku-4-5-20251001", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == "你好！"
    assert response.headers["X-Mirofish-Account"] == "work"
    assert response.headers["X-Mirofish-Quota-7d-Utilization"] == "0.42"
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "claude-haiku-4-5-20251001"
    assert route.calls.last.request.headers["authorization"] == "Bearer device-ticket"
    assert all(route.calls.last.request.headers.get(name) for name in (
        "x-mirasim-device", "x-mirasim-ts", "x-mirasim-nonce", "x-mirasim-sig",
        "x-mirasim-client"))
    assert route.calls.last.request.headers["x-mirasim-client"] == "0.0.220"
    assert route.calls.last.request.headers["x-mirasim-agent"] == "claude"
    assert route.calls.last.request.headers["x-mirasim-session"].startswith("mirofish_")
    assert "x-mirasim-probe" not in route.calls.last.request.headers
    assert session.calls.last.request.headers["authorization"] == "Bearer access-work"
    totals = state.store.usage_summary(1)["totals"]
    assert totals == {"requests": 1, "input_tokens": 12, "output_tokens": 4}


@respx.mock
async def test_messages_preserves_beta_query_and_claude_fingerprint(
        client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages?beta=true").mock(
        return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE))
    session_id = "8fe5ff39-0f50-4121-a1de-9e896e963ee2"
    headers = {
        **auth_headers,
        "user-agent": "claude-cli/2.1.241 (external, mirasim)",
        "x-claude-code-session-id": session_id,
        "x-stainless-arch": "arm64",
        "x-stainless-lang": "js",
        "x-stainless-os": "MacOS",
        "x-stainless-package-version": "0.112.1",
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": "v26.3.0",
        "anthropic-beta": (
            "claude-code-20250219,oauth-2025-04-20,"
            "mid-conversation-system-2026-04-07"),
        "anthropic-dangerous-direct-browser-access": "true",
        "x-app": "cli",
        "authorization": "Bearer caller-secret",
        "x-api-key": "caller-api-key",
        # Caller-supplied relay metadata must never override our own.
        "x-mirasim-session": "attacker-controlled",
    }
    response = await client.post("/v1/messages?beta=true", headers=headers, json={
        "model": "claude-sonnet-5", "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert response.status_code == 200
    sent = route.calls.last.request
    assert sent.url.query == b"beta=true"
    assert sent.headers["user-agent"] == headers["user-agent"]
    assert sent.headers["x-stainless-package-version"] == "0.112.1"
    assert sent.headers["anthropic-beta"] == (
        "claude-code-20250219,mid-conversation-system-2026-04-07")
    assert sent.headers["x-mirasim-session"] == session_id
    assert sent.headers["x-mirasim-agent"] == "claude"
    assert sent.headers["x-mirasim-locale"] == "zh-HK"
    assert sent.headers["authorization"] == "Bearer device-ticket"
    assert "x-api-key" not in sent.headers
    uuid.UUID(sent.headers["x-mirasim-call"])
    assert "x-mirasim-probe" not in sent.headers

    signature = verify_relay_signature(state, sent, "/v1/messages")
    query_payload = "\n".join((
        "mrs-sig-v1", "POST", "/v1/messages?beta=true",
        sent.headers["x-mirasim-ts"], sent.headers["x-mirasim-nonce"],
        hashlib.sha256(sent.content).hexdigest(),
    )).encode()
    public = serialization.load_der_public_key(
        base64.b64decode(state.upstream._signer("work").public_key))
    with pytest.raises(InvalidSignature):
        public.verify(signature, query_payload)


@respx.mock
async def test_messages_refreshes_ticket_on_401(client, state, auth_headers):
    add_account(state, "work")
    session = mock_device_session()
    respx.post(RELAY_BASE + "/v1/messages").mock(side_effect=[
        httpx.Response(401, json={"error": {"type": "authentication_error"}}),
        httpx.Response(200, json=ANTHROPIC_RESPONSE),
    ])
    response = await client.post("/v1/messages", headers=auth_headers, json={
        "model": "m", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}],
    })
    assert response.status_code == 200
    assert session.call_count == 2
    assert state.store.vault.get("work", "access") == "access-work"


@respx.mock
async def test_device_session_refreshes_access_on_401(client, state, auth_headers):
    add_account(state, "work")
    session = respx.post(RELAY_BASE + "/v1/device/session").mock(side_effect=[
        httpx.Response(401, json={"detail": "expired access"}),
        httpx.Response(200, json={"ticket": "device-ticket", "expiresIn": 900}),
    ])
    refresh = respx.post(AUTH_BASE + "/auth/refresh").mock(
        return_value=httpx.Response(200, json={"access_token": "new-access",
                                               "refresh_token": "new-refresh"}))
    respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE))
    response = await client.post("/v1/messages", headers=auth_headers, json={
        "model": "m", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert response.status_code == 200
    assert session.call_count == 2 and refresh.called
    assert state.store.vault.get("work", "access") == "new-access"


@respx.mock
async def test_message_region_refusal_is_exposed_as_rotatable_proxy_error(state):
    add_account(state, "work")
    mock_device_session()
    respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(429, json={
            "error": {
                "type": "shared_quota_unavailable",
                "message": "The cloud route is not served to this network region.",
            },
        }))

    with pytest.raises(RelayError) as raised:
        await state.upstream.messages(
            "work", {"model": "m", "max_tokens": 16,
                     "messages": [{"role": "user", "content": "hi"}]},
            proxy_url="http://proxy.test:8080")

    assert raised.value.status == 502
    assert raised.value.data["region_blocked"] is True


@respx.mock
async def test_messages_stream_passthrough(client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages?beta=true").mock(
        return_value=httpx.Response(200, content=SSE_BODY.encode(),
                                    headers={"content-type": "text/event-stream",
                                             **QUOTA_HEADERS}))
    headers = {**auth_headers,
               "X-Claude-Code-Session-Id": "0f20cf48-c292-42e9-a99e-994511307deb"}
    async with client.stream("POST", "/v1/messages?beta=true", headers=headers, json={
        "model": "m", "max_tokens": 16, "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = (await response.aread()).decode()
    assert '"type":"text_delta","text":"Hi"' in body.replace(" ", "").replace('", "', '","') \
        or 'text_delta' in body
    assert "message_stop" in body
    assert route.calls.last.request.headers["x-mirasim-session"] == \
        "0f20cf48-c292-42e9-a99e-994511307deb"
    assert route.calls.last.request.url.query == b"beta=true"
    totals = state.store.usage_summary(1)["totals"]
    assert totals["input_tokens"] == 9 and totals["output_tokens"] == 2


@respx.mock
async def test_chat_completions_non_stream(client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE, headers=QUOTA_HEADERS))
    response = await client.post("/v1/chat/completions", headers=auth_headers, json={
        "model": "claude-haiku-4-5-20251001",
        "messages": [{"role": "system", "content": "terse"},
                     {"role": "user", "content": "hi"}],
    })
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "你好！"
    assert data["usage"]["total_tokens"] == 16
    sent = json.loads(route.calls.last.request.content)
    assert sent["system"] == "terse"
    assert sent["messages"] == [{"role": "user", "content": "hi"}]


@respx.mock
async def test_chat_completions_stream(client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, content=SSE_BODY.encode(),
                                    headers={"content-type": "text/event-stream"}))
    async with client.stream("POST", "/v1/chat/completions", headers=auth_headers, json={
        "model": "m", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }) as response:
        assert response.status_code == 200
        body = (await response.aread()).decode()
    lines = [line for line in body.split("\n") if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(line[6:]) for line in lines[:-1]]
    assert chunks[0]["object"] == "chat.completion.chunk"
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert text == "Hi"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["total_tokens"] == 11


@respx.mock
async def test_login_flow(client, state, auth_headers):
    respx.post(AUTH_BASE + "/auth/code").mock(
        return_value=httpx.Response(200, json={"sent": True}))
    respx.post(AUTH_BASE + "/auth/verify").mock(
        return_value=httpx.Response(200, json={"access_token": "a1",
                                               "refresh_token": "r1"}))
    respx.get(AUTH_BASE + "/auth/me").mock(
        return_value=httpx.Response(200, json={"id": "u1", "email": "x@example.com"}))
    respx.get(AUTH_BASE + "/auth/referral").mock(
        return_value=httpx.Response(200, json={"current_plan": "pro"}))
    respx.get(RELAY_BASE + "/me/tenant").mock(
        return_value=httpx.Response(200, json={"tenant": "t1"}))

    response = await client.post("/api/login/start", headers=auth_headers,
                                 json={"alias": "work", "email": "x@example.com"})
    assert response.status_code == 200 and response.json()["sent"] is True
    response = await client.post("/api/login/finish", headers=auth_headers,
                                 json={"alias": "work", "code": "123456"})
    assert response.status_code == 200
    assert response.json()["plan"] == "pro"
    assert response.json()["profile_pending"] is False
    assert state.store.vault.get("work", "access") == "a1"
    # bad code format is rejected before hitting upstream
    response = await client.post("/api/login/finish", headers=auth_headers,
                                 json={"alias": "work", "code": "12"})
    assert response.status_code == 400


@respx.mock
async def test_login_keeps_consumed_code_credentials_when_profile_is_rejected(
        client, state, auth_headers):
    respx.post(AUTH_BASE + "/auth/code").mock(
        return_value=httpx.Response(200, json={"sent": True}))
    verify = respx.post(AUTH_BASE + "/auth/verify").mock(
        return_value=httpx.Response(200, json={"access_token": "saved-access",
                                               "refresh_token": "saved-refresh"}))
    respx.get(AUTH_BASE + "/auth/me").mock(
        return_value=httpx.Response(200, json={"id": "u1", "email": "x@example.com"}))
    respx.get(AUTH_BASE + "/auth/referral").mock(
        return_value=httpx.Response(503, json={"error": {"message": "temporary"}}))
    respx.get(RELAY_BASE + "/me/tenant").mock(
        return_value=httpx.Response(200, json={"tenant": "t1"}))

    await client.post("/api/login/start", headers=auth_headers,
                      json={"alias": "work", "email": "x@example.com"})
    response = await client.post("/api/login/finish", headers=auth_headers,
                                 json={"alias": "work", "code": "123456"})

    # The verification code has already been consumed, so optional profile
    # failures must not turn this into a 502 followed by a retrying 401.
    assert response.status_code == 200
    assert response.json()["profile_pending"] is True
    assert verify.call_count == 1
    assert state.store.aliases() == ["work"]
    assert state.store.credentials("work") == ("saved-access", "saved-refresh")
    assert "work" not in state.pending_logins


@respx.mock
async def test_login_keeps_credentials_when_profile_has_network_failure(
        client, state, auth_headers):
    respx.post(AUTH_BASE + "/auth/code").mock(
        return_value=httpx.Response(200, json={"sent": True}))
    respx.post(AUTH_BASE + "/auth/verify").mock(
        return_value=httpx.Response(200, json={"access_token": "saved-access",
                                               "refresh_token": "saved-refresh"}))
    respx.get(AUTH_BASE + "/auth/me").mock(
        side_effect=httpx.ConnectError("profile exit disconnected"))

    await client.post("/api/login/start", headers=auth_headers,
                      json={"alias": "work", "email": "x@example.com"})
    response = await client.post("/api/login/finish", headers=auth_headers,
                                 json={"alias": "work", "code": "123456"})

    assert response.status_code == 200
    assert response.json()["profile_pending"] is True
    assert state.store.credentials("work") == ("saved-access", "saved-refresh")


async def test_delete_account(client, state, auth_headers):
    add_account(state, "work")
    response = await client.request("DELETE", "/api/accounts/work", headers=auth_headers)
    assert response.status_code == 200
    assert state.store.aliases() == []


async def test_usage_endpoint_validation(client, auth_headers):
    response = await client.get("/api/usage?hours=0", headers=auth_headers)
    assert response.status_code == 400


LIMITS_RESPONSE = {
    "subject": "usr_x", "suspended": False, "degraded": False, "unmetered": False,
    "windows": [
        {"name": "30d", "used": 1000.0, "budget": 320000, "reset_at": 1789565221},
        {"name": "5h", "used": 5450.59305, "budget": 26096, "reset_at": 1787075656},
        {"name": "7d", "used": 13621.348735, "budget": 74560, "reset_at": 1787577758},
    ],
}


@respx.mock
async def test_count_tokens_proxied(client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages/count_tokens?beta=true").mock(
        return_value=httpx.Response(200, json={"input_tokens": 42}))
    headers = {**auth_headers, "X-Claude-Code-Session-Id": "count-session"}
    response = await client.post("/v1/messages/count_tokens?beta=true", headers=headers, json={
        "model": "claude-haiku-4-5", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    assert response.json()["input_tokens"] == 42
    assert route.calls.last.request.headers["authorization"] == "Bearer device-ticket"
    assert route.calls.last.request.headers["x-mirasim-session"] == "count-session"
    assert route.calls.last.request.url.query == b"beta=true"
    verify_relay_signature(state, route.calls.last.request, "/v1/messages/count_tokens")


@respx.mock
async def test_count_tokens_falls_back_on_upstream_404(client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    respx.post(RELAY_BASE + "/v1/messages/count_tokens").mock(
        return_value=httpx.Response(404, json={"error": {"message": "no such endpoint"}}))
    body = {"model": "claude-haiku-4-5",
            "messages": [{"role": "user", "content": "hi there"}]}
    response = await client.post("/v1/messages/count_tokens", headers=auth_headers, json=body)
    assert response.status_code == 200
    # Local estimate is a positive integer, never a 404.
    assert isinstance(response.json()["input_tokens"], int)
    assert response.json()["input_tokens"] >= 1


@respx.mock
async def test_account_limits(client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    route = respx.get(RELAY_BASE + "/v1/limits").mock(
        return_value=httpx.Response(200, json=LIMITS_RESPONSE))
    response = await client.get("/accounts/work/limits", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    # Windows are normalized and ordered 5h -> 7d -> 30d.
    assert [w["name"] for w in body["windows"]] == ["5h", "7d", "30d"]
    assert body["windows"][0]["used"] == 5450.59305
    assert body["windows"][0]["label"] == "5 小时窗口"
    assert body["windows"][0]["length"] == 18000
    assert body["suspended"] is False
    # Signed with the device ticket, and the summary is cached into metadata.
    assert route.calls.last.request.headers["authorization"] == "Bearer device-ticket"
    assert route.calls.last.request.headers["x-mirasim-probe"] == "usage"
    assert route.calls.last.request.headers["accept-encoding"] == "identity"
    cached = json.loads(state.store.row("work")["metadata_json"])["limits"]
    assert cached["windows"][0]["name"] == "5h"


@respx.mock
async def test_status_probe_uses_zero_cost_limits_instead_of_messages(
        client, state, auth_headers):
    add_account(state, "work")
    respx.get(AUTH_BASE + "/auth/me").mock(
        return_value=httpx.Response(200, json={"id": "u-work", "email": "work@example.com"}))
    respx.get(AUTH_BASE + "/auth/referral").mock(
        return_value=httpx.Response(200, json={"current_plan": "pro"}))
    respx.get(RELAY_BASE + "/me/tenant").mock(
        return_value=httpx.Response(200, json={"tenant": "t1"}))
    mock_device_session()
    limits = respx.get(RELAY_BASE + "/v1/limits").mock(
        return_value=httpx.Response(200, json=LIMITS_RESPONSE))
    messages = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(500, json={"error": {"message": "must not run"}}))

    response = await client.get("/accounts/work/status?probe=1", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["limits"]["windows"]
    assert limits.call_count == 1
    assert messages.call_count == 0
    metadata = json.loads(state.store.row("work")["metadata_json"])
    assert float(metadata["quota"]["7d_utilization"]) == pytest.approx(
        13621.348735 / 74560)


@respx.mock
async def test_model_scan_sends_minimal_work_with_session_not_probe(state):
    add_account(state, "work")
    mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE))

    result = await state.accounts.scan_models("work", max_models=1)

    assert result == [{"model": "claude-sonnet-5", "accepted": True}]
    sent = route.calls.last.request
    assert json.loads(sent.content)["max_tokens"] == 2
    assert sent.headers["x-mirasim-session"].startswith("mirofish_")
    assert "x-mirasim-probe" not in sent.headers


@respx.mock
async def test_all_limits_survives_one_failure(client, state, auth_headers):
    add_account(state, "alpha")
    add_account(state, "beta")
    mock_device_session()
    respx.get(RELAY_BASE + "/v1/limits").mock(side_effect=[
        httpx.Response(200, json=LIMITS_RESPONSE),
        httpx.Response(403, json={"error": {"message": "nope"}}),
    ])
    response = await client.get("/api/limits", headers=auth_headers)
    assert response.status_code == 200
    results = {r["alias"]: r for r in response.json()["accounts"]}
    assert results["alpha"]["ok"] != results["beta"]["ok"]
    ok = next(r for r in results.values() if r["ok"])
    bad = next(r for r in results.values() if not r["ok"])
    assert ok["limits"]["windows"]
    assert bad["error"]


@respx.mock
async def test_login_start_fails_over_dead_node(client, state, auth_headers):
    import time as time_module

    from mirofish.proxy.parse import proxy_identity

    # Two direct-mode nodes; pretend the subscription was already refreshed.
    for name, host in [("node-a", "a.example"), ("node-b", "b.example")]:
        config = {"name": name, "scheme": "http", "host": host, "port": 8080,
                  "username": "", "password": ""}
        node_id = proxy_identity(config)
        state.pool.configs[node_id] = {**config, "id": node_id}
        state.store.upsert_proxy(node_id, config)
    state.pool.subscription_url = "https://sub.test/nodes"
    state.pool.last_refresh = time_module.time()

    respx.post(AUTH_BASE + "/auth/code").mock(side_effect=[
        httpx.ConnectError("dead exit"),
        httpx.Response(200, json={"sent": True}),
    ])
    response = await client.post("/api/login/start", headers=auth_headers,
                                 json={"alias": "work", "email": "x@example.com"})
    assert response.status_code == 200 and response.json()["sent"] is True
    failures = [int(row["failure_count"]) for row in state.store.proxy_rows()]
    assert sorted(failures) == [0, 1]  # the dead node was marked and skipped


EXHAUSTED_BODY = {"type": "error", "error": {
    "type": "credit_exhausted_shared", "message": "shared quota used up"}}


async def test_account_toggle_excludes_from_selection(client, state, auth_headers):
    add_account(state, "alpha")
    add_account(state, "beta")
    response = await client.post("/api/accounts/alpha/enabled", headers=auth_headers,
                                 json={"enabled": False})
    assert response.json() == {"alias": "alpha", "enabled": False}
    accounts = (await client.get("/accounts", headers=auth_headers)).json()["accounts"]
    assert {a["alias"]: a["disabled"] for a in accounts} == {"alpha": True, "beta": False}
    # Automatic selection never lands on the disabled account.
    assert state.pick_account("") == "beta"
    assert state.route_account("", "", _conv("hello there")) == "beta"
    # Explicitly pinning a disabled account is refused clearly.
    with pytest.raises(RelayError) as excinfo:
        state.route_account("alpha", "", {})
    assert excinfo.value.status == 403
    await client.post("/api/accounts/alpha/enabled", headers=auth_headers,
                      json={"enabled": True})
    assert state.route_account("alpha", "", {}) == "alpha"


async def test_disable_detaches_live_sessions(client, state, auth_headers):
    add_account(state, "alpha")
    add_account(state, "beta")
    conv = _conv("pinned conversation")
    pinned = state.route_account("", "", conv)
    other = "beta" if pinned == "alpha" else "alpha"
    await client.post(f"/api/accounts/{pinned}/enabled", headers=auth_headers,
                      json={"enabled": False})
    # The next turn of the same conversation moves off the disabled account.
    assert state.route_account("", "", conv) == other


@respx.mock
async def test_messages_fail_over_on_shared_credit_exhaustion(client, state, auth_headers):
    add_account(state, "alpha")
    add_account(state, "beta")
    mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages").mock(side_effect=[
        httpx.Response(429, json=EXHAUSTED_BODY),
        httpx.Response(200, json=ANTHROPIC_RESPONSE, headers=QUOTA_HEADERS),
    ])
    response = await client.post("/v1/messages", headers=auth_headers, json={
        "model": "claude-haiku-4-5-20251001", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert response.status_code == 200
    assert response.headers["X-Mirofish-Account"] == "beta"
    assert route.call_count == 2
    # The refused account cools down; later windows avoid it and the panel sees it.
    assert state.exhausted_cooldown("alpha") > 0
    assert state.route_account("", "", _conv("a brand new window")) == "beta"
    accounts = (await client.get("/accounts", headers=auth_headers)).json()["accounts"]
    cooldowns = {a["alias"]: a["shared_quota_cooldown"] for a in accounts}
    assert cooldowns["alpha"] > 0 and cooldowns["beta"] == 0


@respx.mock
async def test_messages_stream_fails_over_on_shared_credit_exhaustion(
        client, state, auth_headers):
    add_account(state, "alpha")
    add_account(state, "beta")
    mock_device_session()
    respx.post(RELAY_BASE + "/v1/messages").mock(side_effect=[
        httpx.Response(429, json=EXHAUSTED_BODY),
        httpx.Response(200, text=SSE_BODY,
                       headers={"content-type": "text/event-stream", **QUOTA_HEADERS}),
    ])
    response = await client.post("/v1/messages", headers=auth_headers, json={
        "model": "claude-haiku-4-5-20251001", "max_tokens": 16, "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert response.status_code == 200
    assert response.headers["X-Mirofish-Account"] == "beta"
    assert "message_stop" in response.text


@respx.mock
async def test_messages_explicit_account_is_never_substituted(client, state, auth_headers):
    add_account(state, "alpha")
    add_account(state, "beta")
    mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(429, json=EXHAUSTED_BODY))
    response = await client.post(
        "/v1/messages", headers={**auth_headers, "X-Mirofish-Account": "alpha"},
        json={"model": "claude-haiku-4-5-20251001", "max_tokens": 16,
              "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "credit_exhausted_shared"
    assert route.call_count == 1


@respx.mock
async def test_messages_surface_exhaustion_when_every_account_is_refused(
        client, state, auth_headers):
    add_account(state, "alpha")
    add_account(state, "beta")
    mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(429, json=EXHAUSTED_BODY))
    response = await client.post("/v1/messages", headers=auth_headers, json={
        "model": "claude-haiku-4-5-20251001", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    })
    # Both accounts were tried once, then the actionable upstream error surfaced.
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "credit_exhausted_shared"
    assert route.call_count == 2


async def test_failover_covers_region_refused_everywhere(state):
    add_account(state, "alpha")
    add_account(state, "beta")
    refused = RelayError(
        "upstream does not serve this proxy exit region", 502,
        {"region_blocked": True, "region_refused_everywhere": True})
    served = []

    async def run(account: str):
        served.append(account)
        if account == "alpha":
            raise refused
        return "ok"

    account, result = await state.with_account_failover("", "", _conv("hello"), run)
    assert (account, result) == ("beta", "ok")
    assert served == ["alpha", "beta"]
    assert state.exhausted_cooldown("alpha") > 0
