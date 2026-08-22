import json

import httpx
import pytest
import respx

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
    assert session.calls.last.request.headers["authorization"] == "Bearer access-work"
    totals = state.store.usage_summary(1)["totals"]
    assert totals == {"requests": 1, "input_tokens": 12, "output_tokens": 4}


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
async def test_messages_stream_passthrough(client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, content=SSE_BODY.encode(),
                                    headers={"content-type": "text/event-stream",
                                             **QUOTA_HEADERS}))
    async with client.stream("POST", "/v1/messages", headers=auth_headers, json={
        "model": "m", "max_tokens": 16, "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = (await response.aread()).decode()
    assert '"type":"text_delta","text":"Hi"' in body.replace(" ", "").replace('", "', '","') \
        or 'text_delta' in body
    assert "message_stop" in body
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
    assert state.store.vault.get("work", "access") == "a1"
    # bad code format is rejected before hitting upstream
    response = await client.post("/api/login/finish", headers=auth_headers,
                                 json={"alias": "work", "code": "12"})
    assert response.status_code == 400


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
    route = respx.post(RELAY_BASE + "/v1/messages/count_tokens").mock(
        return_value=httpx.Response(200, json={"input_tokens": 42}))
    response = await client.post("/v1/messages/count_tokens", headers=auth_headers, json={
        "model": "claude-haiku-4-5", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    assert response.json()["input_tokens"] == 42
    assert route.calls.last.request.headers["authorization"] == "Bearer device-ticket"


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
    cached = json.loads(state.store.row("work")["metadata_json"])["limits"]
    assert cached["windows"][0]["name"] == "5h"


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
