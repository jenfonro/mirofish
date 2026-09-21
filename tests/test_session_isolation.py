"""Account-scoped caller identity, normalized models and zero-work probes."""

import copy
import json
import time
from collections import Counter

import httpx
import pytest
import respx

from mirofish.upstream import CLAUDE_AGENT_SYSTEM_MARKER, relay_session_identity
from tests.conftest import RELAY_BASE, add_account
from tests.mirasim_protocol import relay_metadata, verify_signature

SESSION = "0f20cf48-c292-42e9-a99e-994511307deb"
THREAD = "614b2b63-ea52-4c60-a4b5-373f33213485"


def _messages(stream=False):
    return {
        "model": "claude-fable-5", "max_tokens": 8, "stream": stream,
        "metadata": {"user_id": json.dumps({
            "device_id": "device-original", "account_uuid": "",
            "session_id": SESSION})},
        "system": [{"type": "text", "text": CLAUDE_AGENT_SYSTEM_MARKER,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": [{"type": "text",
                      "text": SESSION, "cache_control": {"type": "ephemeral"}}]}],
    }


def _mock_model(path, stream=False):
    respx.post(RELAY_BASE + "/v1/device/session").mock(
        return_value=httpx.Response(200, json={"ticket": "ticket", "expiresIn": 900}))
    if stream:
        reply = httpx.Response(200, content=(
            b'event: message_start\ndata: {"type":"message_start","message":'
            b'{"id":"msg_test","usage":{"input_tokens":2}}}\n\n'
            b'event: message_delta\ndata: {"type":"message_delta",'
            b'"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n'),
            headers={"content-type": "text/event-stream"})
    else:
        reply = httpx.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 2, "output_tokens": 1}})
    return respx.post(RELAY_BASE + path).mock(return_value=reply)


def test_distinct_ids_map_once_across_protocol_fields(state):
    calls = []

    def derive(claude, hint, payload, account):
        calls.append((claude, account))
        return state.relay_session_id(claude, hint, payload, account)

    payload = {
        "prompt_cache_key": SESSION, "thread_id": THREAD,
        "metadata": {"session_id": SESSION},
        "client_metadata": {"thread_id": THREAD},
        "input": [{"type": "function_call_output", "call_id": SESSION,
                   "output": THREAD}],
        "previous_response_id": "resp_original",
    }
    headers = httpx.Headers({
        "session-id": SESSION, "thread-id": THREAD,
        "x-codex-session-id": SESSION.upper(),
        "x-codex-turn-metadata": json.dumps({"session_id": SESSION, "thread_id": THREAD}),
        "x-mirofish-session": "local-secret",
    })
    original = copy.deepcopy(payload)
    first, rewritten, body = relay_session_identity(
        derive, "first", headers, payload, session_hint=SESSION)
    assert Counter(calls) == {(SESSION, "first"): 1, (THREAD, "first"): 1}
    assert first == rewritten["session-id"] == rewritten["x-codex-session-id"]
    assert first == body["prompt_cache_key"] == body["metadata"]["session_id"]
    assert rewritten["thread-id"] == body["thread_id"] != first
    assert "x-mirofish-session" not in rewritten
    assert payload == original
    assert body["input"] == original["input"]
    assert body["previous_response_id"] == "resp_original"
    second, second_headers, _ = relay_session_identity(
        derive, "second", headers, payload, session_hint=SESSION)
    assert second != first
    assert second_headers["thread-id"] != rewritten["thread-id"]


@pytest.mark.parametrize("stream", [False, True])
@respx.mock
async def test_real_cli_identity_isolated_and_signed_after_body_rewrite(
        client, state, auth_headers, stream):
    add_account(state, "first")
    add_account(state, "second")
    route = _mock_model("/v1/messages", stream)
    payload = _messages(stream)
    raw = json.dumps(payload, indent=2).encode()
    for alias in ("first", "second", "first"):
        response = await client.post("/v1/messages", content=raw, headers={
            **auth_headers, "X-Mirofish-Account": alias,
            "user-agent": "claude-cli/2.1.261 (external, mirasim)",
            "x-claude-code-session-id": SESSION, "x-mirofish-private": "never-forward"})
        assert response.status_code == 200
    seen = []
    for alias, call in zip(("first", "second", "first"), route.calls):
        request = call.request
        expected = state.relay_session_id(SESSION, "", payload, alias)
        sent = json.loads(request.content)
        metadata = json.loads(sent["metadata"]["user_id"])
        assert metadata["session_id"] == request.headers["x-claude-code-session-id"] == expected
        assert relay_metadata(request)["x-mirasim-session"] == expected
        assert metadata["device_id"] != "device-original"
        assert sent["messages"] == payload["messages"]
        assert sent["system"] == payload["system"]
        assert not any(name.startswith("x-mirofish-") for name in request.headers)
        verify_signature(state, request, "/v1/messages", alias=alias)
        seen.append(expected)
    assert seen[0] == seen[2] != seen[1]


@pytest.mark.parametrize("path", ["/v1/responses", "/backend-api/codex/responses",
                                 "/v1/responses/compact",
                                 "/backend-api/codex/responses/compact"])
@respx.mock
async def test_codex_multiple_ids_and_compact_isolated(
        client, state, auth_headers, path):
    add_account(state, "first")
    add_account(state, "second")
    upstream_path = "/v1/responses/compact" if path.endswith("compact") else "/v1/responses"
    route = _mock_model(upstream_path)
    payload = {
        "model": "gpt-5.6-sol", "prompt_cache_key": SESSION,
        "client_metadata": {"session_id": SESSION, "thread_id": THREAD},
        "input": [{"role": "user", "content": SESSION}],
        "previous_response_id": "resp_original",
    }
    headers = {"session-id": SESSION, "thread-id": THREAD,
               "x-codex-window-id": SESSION + ":0",
               "x-codex-turn-metadata": json.dumps({
                   "session_id": SESSION, "thread_id": THREAD,
                   "installation_id": "installation-original"}),
               "chatgpt-account-id": "account-original"}
    for alias in ("first", "second", "first"):
        response = await client.post(path, json=payload, headers={
            **auth_headers, **headers, "X-Mirofish-Account": alias})
        assert response.status_code == 200
    for alias, call in zip(("first", "second", "first"), route.calls):
        request = call.request
        sent = json.loads(request.content)
        turn = json.loads(request.headers["x-codex-turn-metadata"])
        expected = state.relay_session_id(SESSION, "", payload, alias)
        assert expected == request.headers["session-id"] == turn["session_id"]
        assert expected == sent["prompt_cache_key"] == relay_metadata(request)["x-mirasim-session"]
        assert turn["thread_id"] == request.headers["thread-id"]
        assert request.headers["thread-id"] == sent["client_metadata"]["thread_id"]
        assert sent["input"] == payload["input"]
        assert sent["previous_response_id"] == payload["previous_response_id"]
        verify_signature(state, request, upstream_path, alias=alias)
    for name in (*headers,):
        values = [call.request.headers[name] for call in route.calls]
        assert values[0] == values[2] != values[1]


@pytest.mark.parametrize("path,stream", [
    ("/v1/messages", False), ("/v1/messages", True),
    ("/v1/chat/completions", False), ("/v1/chat/completions", True),
    ("/v1/responses", False), ("/v1/responses/compact", False)])
@respx.mock
async def test_failover_derives_only_after_account_selection(
        client, state, auth_headers, monkeypatch, path, stream):
    for alias in ("first", "second"):
        add_account(state, alias)
    upstream_path = path if "responses" in path else "/v1/messages"
    route = _mock_model(upstream_path, stream)
    success = route.return_value
    route.mock(side_effect=[httpx.Response(
        429, json={"error": {"type": "rate_limit_error"}}), success])

    async def limits(*args, **kwargs):
        pass

    monkeypatch.setattr(state, "refresh_limits_if_stale", limits)
    response = await client.post(path, headers={
        **auth_headers, "X-Mirofish-Session": SESSION,
        "x-claude-code-session-id": SESSION, "session-id": SESSION}, json={
            "model": "gpt-5.6-sol" if "responses" in path else "claude-fable-5",
            "messages": [{"role": "user", "content": "hello"}],
            "input": "hello", "stream": stream, "max_tokens": 8})
    assert response.status_code == 200
    assert response.headers["x-mirofish-account"] == "second"
    assert route.call_count == 2
    for alias, call in zip(("first", "second"), route.calls):
        assert relay_metadata(call.request)["x-mirasim-session"] == \
            state.relay_session_id(SESSION, "", {}, alias)


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/chat/completions"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("limit", [0, 1])
@respx.mock
async def test_one_token_is_zero_upstream_even_without_accounts(
        client, state, auth_headers, monkeypatch, path, stream, limit):
    def forbidden(*args, **kwargs):
        pytest.fail("synthetic probe must not select, refresh, derive, bill or heal")

    for name in ("route_account", "pick_account", "with_account_failover",
                 "refresh_limits_if_stale", "record_usage", "note_account_healthy",
                 "relay_session_id"):
        monkeypatch.setattr(state, name, forbidden)
    monkeypatch.setattr(state.upstream, "send_explicit", forbidden)
    response = await client.post(path, headers=auth_headers, json={
        "model": "claude-fable-5", "max_tokens": limit, "stream": stream,
        "messages": [{"role": "user", "content": "local estimate only"}]})
    assert response.status_code == 200
    assert response.headers["x-mirofish-probe"] == "short-circuit"
    assert response.headers["x-mirofish-synthetic"] == "true"
    assert "x-mirofish-account" not in response.headers
    assert len(respx.calls) == 0
    assert state.store.usage_summary(1)["totals"]["requests"] == 0
    if not stream:
        data = response.json()
        assert (data["usage"].get("output_tokens",
                                data["usage"].get("completion_tokens"))) == 0


@pytest.mark.parametrize("path,stream", [
    ("/v1/messages", False), ("/v1/messages", True),
    ("/v1/chat/completions", False), ("/v1/chat/completions", True),
    ("/v1/responses", False), ("/v1/responses/compact", False)])
@respx.mock
async def test_normalized_model_is_serialized_inside_selected_account_closure(
        client, state, auth_headers, monkeypatch, path, stream):
    add_account(state, "work")
    upstream_path = path if "responses" in path else "/v1/messages"
    route = _mock_model(upstream_path, stream)

    async def normalize(account, requested, proxy_url=None):
        assert account == "work"
        assert requested == "claude-fable-5"
        return "claude-fable-5[1m]"

    monkeypatch.setattr(state.accounts, "resolve_model", normalize)
    payload = {**_messages(stream), "input": "hello"}
    del payload["metadata"]  # Reserialization must follow model changes alone.
    response = await client.post(path, headers=auth_headers, json=payload)
    assert response.status_code == 200
    request = route.calls.last.request
    assert json.loads(request.content)["model"] == "claude-fable-5[1m]"
    verify_signature(state, request, upstream_path)


@respx.mock
async def test_count_tokens_preflights_and_never_heals(
        client, state, auth_headers, monkeypatch):
    add_account(state, "work")
    route = _mock_model("/v1/messages/count_tokens")
    route.mock(return_value=httpx.Response(200, json={"input_tokens": 12}))
    calls = []

    async def normalize(account, requested, proxy_url=None):
        calls.append(account)
        return "claude-fable-5[1m]"

    def forbidden(*args, **kwargs):
        pytest.fail("count_tokens cannot heal or record model success")

    monkeypatch.setattr(state.accounts, "resolve_model", normalize)
    monkeypatch.setattr(state, "note_account_healthy", forbidden)
    monkeypatch.setattr(state, "record_usage", forbidden)
    response = await client.post("/v1/messages/count_tokens", headers={
        **auth_headers, "x-claude-code-session-id": SESSION}, json=_messages())
    assert response.status_code == 200
    assert response.json() == {"input_tokens": 12}
    assert calls == ["work"]
    request = route.calls.last.request
    assert json.loads(request.content)["model"] == "claude-fable-5[1m]"
    verify_signature(state, request, "/v1/messages/count_tokens")


@respx.mock
async def test_count_tokens_quota_gate_makes_zero_model_requests(
        client, state, auth_headers, monkeypatch):
    add_account(state, "work")
    state.store.merge_metadata("work", {"limits": {
        "unmetered": False, "fetched_epoch": time.time(),
        "windows": [{"name": "5h", "used": 100, "budget": 100,
                     "reset_at": time.time() + 3600}]}})

    async def refreshed(*args, **kwargs):
        pass

    monkeypatch.setattr(state, "refresh_limits_if_stale", refreshed)
    response = await client.post("/v1/messages/count_tokens",
                                 headers=auth_headers, json=_messages())
    assert response.status_code == 429
    assert len(respx.calls) == 0
