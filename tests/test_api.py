import asyncio
import base64
import json
import time
import uuid

import httpx
import pytest
import respx
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization

from mirofish.accounts import profile_fields
from mirofish.api.relay import (_MAX_USAGE_LINE_BYTES,
                                _ManagedStreamingResponse, _UsageWatcher,
                                _finalize_upstream_stream)
from mirofish.errors import RelayError
from mirofish.device import DEVICE_KEY_KIND
from mirofish.proxy import DIRECT
from mirofish.translate import MAX_SSE_EVENT_BYTES
from mirofish.upstream import CLAUDE_AGENT_SYSTEM_MARKER, _DeviceTicket
from tests.mirasim_protocol import (relay_metadata, signing_payload, unseal,
                                    verify_signature)

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
    """Open the sealed envelope and verify its ``mrs-sig-v2`` signature."""
    return verify_signature(state, request, path)


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
    # The default-account selection above already counted as an assignment.
    assert [first, second, third] == ["beta", "alpha", "beta"]


def test_removed_default_account_falls_back_to_existing_alias(state):
    add_account(state, "work")
    add_account(state, "other")
    state.default_account = "work"

    state.remove_account("work")

    assert state.default_account == "work"  # configuration is unchanged
    assert state.pick_account("") == "other"
    assert state.route_account("", "", _conv("new conversation")) == "other"


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


def test_relay_session_id_rewrites_claude_id_per_account_and_hashes_local_hints(state):
    official = "0f20cf48-c292-42e9-a99e-994511307deb"
    rewritten = state.relay_session_id(official, "local-secret", _conv("x"), "alpha")
    assert rewritten != official
    assert uuid.UUID(rewritten).version == 4
    assert rewritten == state.relay_session_id(official, "", _conv("changed"), "alpha")
    assert rewritten != state.relay_session_id(official, "", _conv("x"), "beta")
    first = state.relay_session_id("", "local-secret", _conv("private prompt"), "alpha")
    second = state.relay_session_id("", "local-secret", _conv("changed prompt"), "alpha")
    # Deterministic, and shaped like the bare v4 UUID every official client
    # sends, so the relay does not name itself in an upstream header.
    assert first == second
    assert uuid.UUID(first).version == 4
    assert "local-secret" not in first and "private prompt" not in first


def test_relay_session_id_never_forwards_a_non_uuid_caller_label(state):
    label = "claude-session-1"
    derived = state.relay_session_id(label, "", _conv("x"))

    # Labels and UUIDs are both rewritten, so callers cannot choose the wire ID.
    assert derived != label
    assert uuid.UUID(derived).version == 4
    # Still deterministic, so affinity for that conversation is unaffected.
    assert state.relay_session_id(label, "", _conv("different body")) == derived
    assert state.relay_session_id("other-label", "", _conv("x")) != derived
    # Case is normalized rather than treated as a different session.
    upper = "0F20CF48-C292-42E9-A99E-994511307DEB"
    assert state.relay_session_id(upper, "", _conv("x")) == \
        state.relay_session_id(upper.lower(), "", _conv("x"))
    assert state.relay_session_id(upper, "", _conv("x")) != upper.lower()


def test_session_key_follows_a_responses_conversation_across_turns(state):
    add_account(state, "alpha")
    add_account(state, "beta")
    turn_one = {
        "model": "gpt-5.6-codex",
        "prompt_cache_key": "codex-thread-9",
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": "first ask"}]}],
    }
    # A later turn shares only the cache key: the input has grown, tool output
    # has been appended, and previous_response_id changes every turn.
    turn_two = {
        "model": "gpt-5.6-codex",
        "prompt_cache_key": "codex-thread-9",
        "previous_response_id": "resp_abc123",
        "input": [
            {"type": "function_call_output", "output": "{}"},
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "follow-up ask"}]},
        ],
    }
    other = {**turn_one, "prompt_cache_key": "codex-thread-10"}

    account = state.route_account("", "", turn_one)
    assert state.route_account("", "", turn_two) == account
    assert state.relay_session_id("", "", turn_one) == \
        state.relay_session_id("", "", turn_two)
    assert state.relay_session_id("", "", other) != \
        state.relay_session_id("", "", turn_one)
    assert "codex-thread-9" not in state.relay_session_id("", "", turn_one)


def test_session_key_falls_back_to_the_first_responses_input_turn(state):
    add_account(state, "alpha")
    keyless = {"model": "gpt-5.6-codex",
               "input": [{"role": "user",
                          "content": [{"type": "input_text", "text": "hello"}]}]}
    grown = {"model": "gpt-5.6-codex",
             "input": [
                 {"role": "user",
                  "content": [{"type": "input_text", "text": "hello"}]},
                 {"role": "assistant",
                  "content": [{"type": "output_text", "text": "hi"}]},
             ]}
    plain = {"model": "gpt-5.6-codex", "input": "hello"}

    session = state.relay_session_id("", "", keyless)
    assert uuid.UUID(session).version == 4
    assert state.relay_session_id("", "", grown) == session
    assert state.relay_session_id("", "", plain) == session
    assert state.relay_session_id("", "", {"model": "gpt-5.6-codex"}) != session


def test_record_usage_survives_account_deleted_during_stream(state):
    add_account(state, "work")
    state.remove_account("work")

    outgoing = state.record_usage(
        "work", "claude-fable-5", {"input_tokens": 7, "output_tokens": 3}, {})

    assert outgoing["X-Mirofish-Account"] == "work"
    assert state.store.usage_summary(1)["totals"] == {
        "requests": 1, "input_tokens": 7, "output_tokens": 3}


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
    assert sent["model"] == "claude-haiku-4-5"
    assert sent["system"] == [
        {"type": "text", "text": CLAUDE_AGENT_SYSTEM_MARKER,
         "cache_control": {"type": "ephemeral"}},
    ]
    upstream_request = route.calls.last.request
    assert upstream_request.headers["authorization"] == "Bearer device-ticket"
    # 0.0.303 profile: only the build marker and the sealed envelope travel in
    # clear; every other relay field lives inside x-mirasim-enc.
    assert upstream_request.headers["x-mirasim-client"] == "0.0.303"
    assert upstream_request.headers["x-mirasim-enc"]
    clear = {name.lower() for name in upstream_request.headers
             if name.lower().startswith("x-mirasim-")}
    assert clear == {"x-mirasim-client", "x-mirasim-enc"}
    metadata = relay_metadata(upstream_request)
    assert all(metadata.get(name) for name in (
        "x-mirasim-device", "x-mirasim-ts", "x-mirasim-nonce", "x-mirasim-sig",
        "x-mirasim-client"))
    assert metadata["x-mirasim-agent"] == "claude"
    assert uuid.UUID(metadata["x-mirasim-session"]).version == 4
    assert "x-mirasim-probe" not in metadata
    verify_relay_signature(state, upstream_request, "/v1/messages")
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
    metadata = relay_metadata(sent)
    expected_session = state.relay_session_id(session_id, "", {}, "work")
    assert metadata["x-mirasim-session"] == expected_session != session_id
    assert sent.headers["x-claude-code-session-id"] == expected_session
    assert metadata["x-mirasim-agent"] == "claude"
    assert metadata["x-mirasim-locale"] == "zh-HK"
    assert sent.headers["authorization"] == "Bearer device-ticket"
    assert "x-api-key" not in sent.headers
    uuid.UUID(metadata["x-mirasim-call"])
    assert "x-mirasim-probe" not in metadata
    # The caller's spoofed session never reaches upstream, clear or sealed.
    assert "attacker-controlled" not in sent.headers.values()
    assert "attacker-controlled" not in metadata.values()

    # Signed over the canonical pathname, not the query string.
    signature = verify_relay_signature(state, sent, "/v1/messages")
    public = serialization.load_der_public_key(
        base64.b64decode(state.upstream._signer("work").public_key))
    with pytest.raises(InvalidSignature):
        public.verify(signature, signing_payload(
            sent, "/v1/messages?beta=true", "device-ticket", fields=metadata))
    # The seal is bound to the same canonical pathname: opening it under the
    # query-bearing path fails authentication.
    with pytest.raises(InvalidTag):
        unseal(sent.headers["x-mirasim-enc"], "POST", "/v1/messages?beta=true")


@respx.mock
async def test_messages_refreshes_ticket_on_401(client, state, auth_headers):
    add_account(state, "work")
    session = mock_device_session()
    respx.post(RELAY_BASE + "/v1/messages").mock(side_effect=[
        httpx.Response(401, json={"error": {"type": "authentication_error"}}),
        httpx.Response(200, json=ANTHROPIC_RESPONSE),
    ])
    response = await client.post("/v1/messages", headers=auth_headers, json={
        "model": "claude-haiku-4-5", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
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
        "model": "claude-haiku-4-5", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert response.status_code == 200
    assert session.call_count == 2 and refresh.called
    assert state.store.vault.get("work", "access") == "new-access"


@respx.mock
async def test_message_region_refusal_preserves_account_scoped_429(state):
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

    assert raised.value.status == 429
    assert raised.value.data["error"]["type"] == "shared_quota_unavailable"
    assert "region_blocked" not in raised.value.data


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
        "model": "claude-fable-5", "max_tokens": 16, "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = (await response.aread()).decode()
    assert '"type":"text_delta","text":"Hi"' in body.replace(" ", "").replace('", "', '","') \
        or 'text_delta' in body
    assert "message_stop" in body
    expected_session = state.relay_session_id(
        headers["X-Claude-Code-Session-Id"], "", {}, "work")
    assert relay_metadata(route.calls.last.request)["x-mirasim-session"] == expected_session
    assert route.calls.last.request.headers["x-claude-code-session-id"] == expected_session
    assert expected_session != headers["X-Claude-Code-Session-Id"]
    assert route.calls.last.request.url.query == b"beta=true"
    assert json.loads(route.calls.last.request.content)["system"] == [
        {"type": "text", "text": CLAUDE_AGENT_SYSTEM_MARKER,
         "cache_control": {"type": "ephemeral"}},
    ]
    totals = state.store.usage_summary(1)["totals"]
    assert totals["input_tokens"] == 9 and totals["output_tokens"] == 2


@respx.mock
async def test_messages_stream_preserves_fragmented_crlf_bytes(
        client, state, auth_headers):
    """Usage inspection must not normalize line endings or append a newline."""
    add_account(state, "work")
    mock_device_session()
    original = (
        b'event: message_start\r\n'
        b'data: {"type":"message_start","message":{"usage":'
        b'{"input_tokens":7}}}\r\n\r\n'
        b'event: content_block_delta\r\n'
        b'data: {"type":"content_block_delta","delta":{"type":'
        b'"text_delta","text":"\xe4\xbd\xa0\xe5\xa5\xbd"}}\r\n\r\n'
        b'event: message_delta\r\n'
        b'data: {"type":"message_delta","usage":{"output_tokens":3}}'
    )

    class FragmentedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            # Deliberately split inside CRLF, JSON, and the UTF-8 text.
            cuts = (1, 24, 25, 67, 104, 149, 174, 175, 221, len(original))
            start = 0
            for end in cuts:
                yield original[start:end]
                start = end

    respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(
            200, stream=FragmentedStream(),
            headers={"content-type": "text/event-stream"}))
    response = await client.post("/v1/messages", headers=auth_headers, json={
        "model": "claude-fable-5", "max_tokens": 16, "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert response.status_code == 200
    assert response.content == original
    totals = state.store.usage_summary(1)["totals"]
    assert totals["input_tokens"] == 7 and totals["output_tokens"] == 3


def test_usage_watcher_bounds_oversized_unterminated_line():
    watcher = _UsageWatcher()

    watcher.feed_bytes(b"x" * (_MAX_USAGE_LINE_BYTES + 1))
    assert len(watcher._buffer) == 0
    assert watcher._discard_until_newline is True

    watcher.feed_bytes(
        b"ignored remainder\n"
        b'data: {"type":"message_delta","usage":{"output_tokens":5}}')
    watcher.finish()

    assert watcher.usage == {"output_tokens": 5}
    assert len(watcher._buffer) == 0


async def test_managed_stream_finalizes_when_headers_cannot_be_sent():
    entered = False
    finalized = 0

    async def content():
        nonlocal entered
        entered = True
        yield b"never sent"

    async def finalize():
        nonlocal finalized
        finalized += 1

    async def receive():
        await asyncio.Event().wait()

    async def send(_message):
        raise OSError("client disconnected before body iteration")

    response = _ManagedStreamingResponse(content(), finalize=finalize)
    with pytest.raises(OSError):
        await response(
            {"type": "http", "asgi": {"spec_version": "2.3"}},
            receive, send)

    assert entered is False
    assert finalized == 1


async def test_managed_stream_shields_cleanup_from_cancellation():
    body_send_started = asyncio.Event()
    finalized = False

    async def content():
        yield b"part"

    async def finalize():
        nonlocal finalized
        await asyncio.sleep(0.01)
        finalized = True

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("more_body"):
            body_send_started.set()
            await asyncio.Event().wait()

    response = _ManagedStreamingResponse(content(), finalize=finalize)
    task = asyncio.create_task(response(
        {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    await body_send_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert finalized is True


async def test_stream_finalize_records_usage_even_when_stack_close_fails():
    class FailingStack:
        async def aclose(self):
            raise RuntimeError("synthetic close failure")

    class RecordingState:
        recorded = None

        def record_usage(self, account, model, usage, headers):
            self.recorded = (account, model, usage, headers)

    watcher = _UsageWatcher()
    watcher.feed_bytes(
        b'data: {"type":"message_delta","usage":{"output_tokens":4}}')
    recording = RecordingState()

    await _finalize_upstream_stream(
        FailingStack(), recording, "work", "model", watcher, {"quota": "x"})

    assert recording.recorded == (
        "work", "model", {"output_tokens": 4}, {"quota": "x"})


@pytest.mark.parametrize("model,betas", [
    (
        "claude-fable-5",
        "claude-code-20250219,context-1m-2025-08-07,"
        "interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,"
        "context-management-2025-06-27,prompt-caching-scope-2026-01-05,"
        "mid-conversation-system-2026-04-07,advisor-tool-2026-03-01,"
        "effort-2025-11-24,fallback-credit-2026-06-01",
    ),
    (
        "claude-opus-4-8",
        "claude-code-20250219,context-1m-2025-08-07,"
        "interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,"
        "context-management-2025-06-27,prompt-caching-scope-2026-01-05,"
        "mid-conversation-system-2026-04-07,advisor-tool-2026-03-01,"
        "effort-2025-11-24",
    ),
])
@respx.mock
async def test_complete_claude_code_payload_preserves_work_and_rewrites_identity(
        client, state, auth_headers, model, betas):
    add_account(state, "work")
    state.store.merge_metadata("work", {"model_roster": {
        "version": "cli-payload-fixture", "fetched_epoch": time.time(),
        "agents": {"claude": [{"id": model, "contextWindow": 200_000}]},
    }})
    mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages?beta=true").mock(
        return_value=httpx.Response(
            200, content=SSE_BODY.encode(),
            headers={"content-type": "text/event-stream"}))
    session_id = "0f20cf48-c292-42e9-a99e-994511307deb"
    metadata_id = json.dumps({
        "device_id": "a" * 64,
        "account_uuid": "",
        "session_id": session_id,
    }, separators=(",", ":"))
    payload = {
        "model": model,
        "max_tokens": 64000,
        "stream": True,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
        "context_management": {
            "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
        "metadata": {"user_id": metadata_id},
        "system": [
            {"type": "text", "text": "first"},
            {"type": "text", "text": CLAUDE_AGENT_SYSTEM_MARKER,
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "third",
             "cache_control": {"type": "ephemeral"}},
        ],
        "tools": [{
            "name": "noop",
            "description": "test tool",
            "input_schema": {"type": "object", "properties": {}},
        }],
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "hello",
                 "cache_control": {"type": "ephemeral"}}]},
            {"role": "system", "content": "continue"},
        ],
    }
    headers = {
        **auth_headers,
        "user-agent": "claude-cli/2.1.241 (external, mirasim)",
        "x-claude-code-session-id": session_id,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": betas,
        "anthropic-dangerous-direct-browser-access": "true",
        "x-app": "cli",
        "x-stainless-package-version": "0.112.1",
        "x-stainless-runtime": "node",
    }

    async with client.stream(
            "POST", "/v1/messages?beta=true", headers=headers, json=payload) as response:
        assert response.status_code == 200
        await response.aread()

    sent = route.calls.last.request
    expected_session = state.relay_session_id(session_id, "", payload, "work")
    expected_identity = {
        "device_id": state.relay_session_id("a" * 64, "", payload, "work"),
        "account_uuid": "",
        "session_id": expected_session,
    }
    expected = {**payload, "metadata": {
        "user_id": json.dumps(expected_identity, separators=(",", ":"))}}
    assert sent.content == json.dumps(
        expected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert sent.headers["anthropic-beta"] == betas
    assert sent.headers["user-agent"] == headers["user-agent"]
    assert sent.headers["x-stainless-package-version"] == "0.112.1"
    assert relay_metadata(sent)["x-mirasim-session"] == expected_session != session_id
    assert sent.headers["x-claude-code-session-id"] == expected_session
    assert sent.url.query == b"beta=true"
    verify_relay_signature(state, sent, "/v1/messages")


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
    assert sent["model"] == "claude-haiku-4-5"
    # The marker and the final system block are cache breakpoints, and the last
    # user turn is promoted to structured content so it can carry the third.
    assert sent["system"] == [
        {"type": "text", "text": CLAUDE_AGENT_SYSTEM_MARKER,
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "terse",
         "cache_control": {"type": "ephemeral"}},
    ]
    assert sent["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "hi",
         "cache_control": {"type": "ephemeral"}},
    ]}]


@respx.mock
async def test_chat_completions_uses_configured_default_model(
        client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE, headers=QUOTA_HEADERS))

    response = await client.post("/v1/chat/completions", headers=auth_headers, json={
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert response.status_code == 200
    assert response.json()["model"] == "gpt-5.6-luna"
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "gpt-5.6-luna"
    assert "system" not in sent


@respx.mock
async def test_chat_completions_stream(client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, content=SSE_BODY.encode(),
                                    headers={"content-type": "text/event-stream"}))
    async with client.stream("POST", "/v1/chat/completions", headers=auth_headers, json={
        "model": "claude-haiku-4-5", "stream": True,
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
async def test_chat_completions_rejects_oversized_unterminated_sse_event(
        client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()

    class OversizedStream(httpx.AsyncByteStream):
        def __init__(self):
            self.closed = False

        async def __aiter__(self):
            yield b"x" * (MAX_SSE_EVENT_BYTES + 1)

        async def aclose(self):
            self.closed = True

    oversized_stream = OversizedStream()
    upstream_response = httpx.Response(
        200, stream=oversized_stream,
        headers={"content-type": "text/event-stream"})
    respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=upstream_response)

    response = await client.post(
        "/v1/chat/completions", headers=auth_headers, json={
            "model": "claude-haiku-4-5", "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        })

    assert response.status_code == 200
    assert "upstream SSE event is too large" in response.text
    assert response.text.rstrip().endswith("data: [DONE]")
    assert oversized_stream.closed is True


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
    # After the profile lands, finish_login reads usage limits once.
    mock_device_session()
    respx.get(RELAY_BASE + "/v1/limits").mock(
        return_value=httpx.Response(200, json=LIMITS_RESPONSE))

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
    # The profile read is refused with a transient upstream error (not an
    # account-scoped 401/403/overloaded), so no health is recorded and the
    # already-consumed verification code must not be thrown away.
    respx.get(AUTH_BASE + "/auth/me").mock(
        return_value=httpx.Response(500, json={"error": {"message": "temporary"}}))

    await client.post("/api/login/start", headers=auth_headers,
                      json={"alias": "work", "email": "x@example.com"})
    response = await client.post("/api/login/finish", headers=auth_headers,
                                 json={"alias": "work", "code": "123456"})

    # The verification code has already been consumed, so optional profile
    # failures must not turn this into a 502 followed by a retrying 401.
    assert response.status_code == 200
    assert response.json()["profile_pending"] is True
    assert response.json()["profile_error"]["status"] == 500
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


def _mock_login(email: str, access: str = "new-access",
                refresh: str = "new-refresh") -> None:
    respx.post(AUTH_BASE + "/auth/code").mock(
        return_value=httpx.Response(200, json={"sent": True}))
    respx.post(AUTH_BASE + "/auth/verify").mock(
        return_value=httpx.Response(200, json={"access_token": access,
                                               "refresh_token": refresh}))
    respx.get(AUTH_BASE + "/auth/me").mock(
        return_value=httpx.Response(200, json={"id": "new-user", "email": email}))
    respx.get(AUTH_BASE + "/auth/referral").mock(
        return_value=httpx.Response(200, json={"current_plan": "pro"}))
    # A successful login now reads usage limits once the profile lands; the
    # legacy /me/tenant probe is gone.
    mock_device_session()
    respx.get(RELAY_BASE + "/v1/limits").mock(
        return_value=httpx.Response(200, json=LIMITS_RESPONSE))


def _seed_account_runtime(state, alias: str) -> None:
    state._exhausted_until[alias] = {"shared": time.time() + 600}
    state._sessions["old-conversation"] = {"account": alias, "last": time.time()}
    state._last_assigned[alias] = time.time()


@respx.mock
async def test_relogin_same_email_keeps_device_and_clears_old_runtime(
        client, state, auth_headers):
    add_account(state, "work", "x@example.com")
    signer = state.upstream._signer("work")
    device_id = signer.device_id
    key = state.upstream._ticket_key("work", None)
    state.upstream._ticket_cache[key] = _DeviceTicket(
        "stale-ticket", time.monotonic() + 900)
    state.upstream._device_sessions.add(key)
    _seed_account_runtime(state, "work")
    _mock_login("x@example.com")

    await client.post("/api/login/start", headers=auth_headers,
                      json={"alias": "work", "email": "x@example.com"})
    response = await client.post("/api/login/finish", headers=auth_headers,
                                 json={"alias": "work", "code": "123456"})

    assert response.status_code == 200
    assert state.store.credentials("work") == ("new-access", "new-refresh")
    assert state.upstream._signer("work") is signer
    assert state.upstream._signer("work").device_id == device_id
    # Old authorization is invalidated: the stale ticket is gone. A relogin now
    # reads usage limits, which may re-establish a fresh device session, so the
    # invariant is "the stale ticket no longer authorizes", not "cache empty".
    stale = state.upstream._ticket_cache.get(key)
    assert stale is None or stale.value != "stale-ticket"
    assert "work" not in state._exhausted_until
    assert "work" not in state._last_assigned
    assert all(entry["account"] != "work" for entry in state._sessions.values())


@respx.mock
async def test_relogin_different_email_resets_device_identity(
        client, state, auth_headers):
    add_account(state, "work", "old@example.com")
    signer = state.upstream._signer("work")
    old_device_id = signer.device_id
    key = state.upstream._ticket_key("work", None)
    state.upstream._ticket_cache[key] = _DeviceTicket(
        "stale-ticket", time.monotonic() + 900)
    state.upstream._device_sessions.add(key)
    _mock_login("new@example.com")

    await client.post("/api/login/start", headers=auth_headers,
                      json={"alias": "work", "email": "new@example.com"})
    response = await client.post("/api/login/finish", headers=auth_headers,
                                 json={"alias": "work", "code": "123456"})

    assert response.status_code == 200
    assert state.store.row("work")["email"] == "new@example.com"
    # A different upstream account taking over the alias is a different client:
    # its predecessor's signer and device key are dropped, so it gets an
    # independent device identity rather than inheriting the old one.
    assert state.upstream._signer("work") is not signer
    assert state.upstream._signer("work").device_id != old_device_id
    stale = state.upstream._ticket_cache.get(key)
    assert stale is None or stale.value != "stale-ticket"


async def test_delete_account(client, state, auth_headers):
    add_account(state, "work")
    old_device_id = state.upstream._signer("work").device_id
    key = state.upstream._ticket_key("work", None)
    state.upstream._ticket_cache[key] = _DeviceTicket(
        "stale-ticket", time.monotonic() + 900)
    state.upstream._device_sessions.add(key)
    _seed_account_runtime(state, "work")
    state.pending_logins["work"] = {
        "email": "work@example.com", "created": time.time(), "proxy_id": None}

    response = await client.request("DELETE", "/api/accounts/work", headers=auth_headers)

    assert response.status_code == 200
    assert state.store.aliases() == []
    assert key not in state.upstream._ticket_cache
    assert key not in state.upstream._device_sessions
    assert "work" not in state._exhausted_until
    assert "work" not in state._last_assigned
    assert "work" not in state.pending_logins
    assert all(entry["account"] != "work" for entry in state._sessions.values())

    # A deleted account takes its device key with it; a reused alias is a new
    # client and is issued an independent device identity.
    add_account(state, "work")
    assert state.upstream._signer("work").device_id != old_device_id


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
    # A caller's own session id is rewritten per account (session isolation),
    # not forwarded verbatim: two accounts must never announce the same id.
    count_session = "6f1de6e1-1f3c-4a51-b8cd-0c1cb1c8f4d2"
    headers = {**auth_headers, "X-Claude-Code-Session-Id": count_session}
    response = await client.post("/v1/messages/count_tokens?beta=true", headers=headers, json={
        "model": "claude-haiku-4-5-20251001",
        "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    assert response.json()["input_tokens"] == 42
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "claude-haiku-4-5"
    assert sent["system"] == [
        {"type": "text", "text": CLAUDE_AGENT_SYSTEM_MARKER},
    ]
    assert route.calls.last.request.headers["authorization"] == "Bearer device-ticket"
    rewritten = relay_metadata(route.calls.last.request)["x-mirasim-session"]
    assert uuid.UUID(rewritten).version == 4
    assert rewritten != count_session
    # Deterministic per account+session: the same caller id maps stably.
    assert rewritten == state.relay_session_id(count_session, "", {}, account="work")
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
    # The fallback counts the same compatibility block used for generation.
    assert response.json()["input_tokens"] == \
        (len(CLAUDE_AGENT_SYSTEM_MARKER) + len("hi there")) // 4


@respx.mock
async def test_one_token_probe_answered_without_generation(client, state, auth_headers):
    """max_tokens<=1 is an availability probe the upstream refuses outright."""
    add_account(state, "work")
    mock_device_session()
    generate = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE))

    response = await client.post("/v1/messages?beta=true", headers=auth_headers, json={
        "model": "claude-opus-5", "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message" and body["role"] == "assistant"
    assert body["model"] == "claude-opus-5"
    assert body["content"] == []
    assert body["stop_reason"] == "max_tokens"
    # The count is a local estimate; the probe makes no upstream call at all.
    assert body["usage"] == {"input_tokens": 1, "output_tokens": 0}
    assert response.headers["X-Mirofish-Probe"] == "short-circuit"
    # Nothing was generated upstream, so nothing is billed or logged as usage.
    assert not generate.called
    assert state.store.usage_summary(1)["totals"]["requests"] == 0


@respx.mock
async def test_one_token_probe_streams_synthetic_events(client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    generate = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, content=SSE_BODY.encode(),
                                    headers={"content-type": "text/event-stream"}))

    async with client.stream("POST", "/v1/messages", headers=auth_headers, json={
        "model": "claude-opus-5", "max_tokens": 1, "stream": True,
        "messages": [{"role": "user", "content": "hi"}]}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["X-Mirofish-Probe"] == "short-circuit"
        body = (await response.aread()).decode()

    events = [line[7:] for line in body.split("\n") if line.startswith("event: ")]
    assert events == ["message_start", "message_delta", "message_stop"]
    start = json.loads(body.split("data: ", 1)[1].split("\n", 1)[0])
    assert start["message"]["usage"]["input_tokens"] == 1
    assert start["message"]["stop_reason"] == "max_tokens"
    assert not generate.called


@respx.mock
async def test_one_token_probe_falls_back_to_local_estimate(client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    generate = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE))

    response = await client.post("/v1/messages", headers=auth_headers, json={
        "model": "claude-opus-5", "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi there"}]})

    assert response.status_code == 200
    # The probe estimates the raw request locally (~4 chars/token), never the
    # prepared body, and never contacts the upstream count endpoint.
    assert response.json()["usage"]["input_tokens"] == len("hi there") // 4
    assert not generate.called


@respx.mock
async def test_two_token_request_still_reaches_upstream(client, state, auth_headers):
    """Only the shape the upstream refuses is short-circuited."""
    add_account(state, "work")
    mock_device_session()
    generate = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE))

    response = await client.post("/v1/messages", headers=auth_headers, json={
        "model": "claude-opus-5", "max_tokens": 2,
        "messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 200
    assert generate.called
    assert json.loads(generate.calls.last.request.content)["max_tokens"] == 2


@respx.mock
async def test_one_token_short_circuit_switch_restores_passthrough(
        client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    state.settings.one_token_short_circuit = False
    generate = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE))

    response = await client.post("/v1/messages", headers=auth_headers, json={
        "model": "claude-opus-5", "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 200
    assert generate.called
    assert "X-Mirofish-Probe" not in response.headers


@respx.mock
async def test_chat_completions_one_token_probe_answered_locally(
        client, state, auth_headers):
    """openai_to_anthropic clamps max_tokens to 1, so the OpenAI path can build
    the refused shape from a max_tokens of 0 or 1 as well."""
    add_account(state, "work")
    mock_device_session()
    generate = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE))

    response = await client.post("/v1/chat/completions", headers=auth_headers, json={
        "model": "claude-opus-5", "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["finish_reason"] == "length"
    assert data["choices"][0]["message"]["content"] is None
    assert data["usage"] == {"prompt_tokens": 1, "completion_tokens": 0,
                             "total_tokens": 1}
    assert response.headers["X-Mirofish-Probe"] == "short-circuit"
    assert not generate.called


@respx.mock
async def test_chat_completions_one_token_probe_streams(client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    generate = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, content=SSE_BODY.encode(),
                                    headers={"content-type": "text/event-stream"}))

    async with client.stream("POST", "/v1/chat/completions", headers=auth_headers, json={
        "model": "claude-opus-5", "max_tokens": 1, "stream": True,
        "messages": [{"role": "user", "content": "hi"}]}) as response:
        assert response.status_code == 200
        body = (await response.aread()).decode()

    lines = [line for line in body.split("\n") if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(line[6:]) for line in lines[:-1]]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "length"
    assert chunks[-1]["usage"]["prompt_tokens"] == 1
    assert not generate.called


@respx.mock
async def test_account_limits(client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    route = respx.get(RELAY_BASE + "/v1/limits").mock(
        return_value=httpx.Response(200, json=LIMITS_RESPONSE))
    first = await client.get("/accounts/work/limits", headers=auth_headers)
    assert first.status_code == 200
    body = first.json()
    # Windows are normalized and ordered 5h -> 7d -> 30d.
    assert [w["name"] for w in body["windows"]] == ["5h", "7d", "30d"]
    assert body["windows"][0]["used"] == 5450.59305
    assert body["windows"][0]["label"] == "5 小时窗口"
    assert body["windows"][0]["length"] == 18000
    assert body["suspended"] is False
    # The startup probe uses the account token while prewarming a device
    # session. Later polls use the signed relay-ticket profile.
    initial = route.calls[0].request
    assert initial.headers["authorization"] == "Bearer access-work"
    assert "x-mirasim-device" not in initial.headers
    # The forced single-account refresh coalesces bursts within one second; age
    # the marker so the second poll actually issues a request and we can see it
    # use the warmed device session instead of the account token.
    state.store.merge_metadata("work", {"limits_forced_epoch": time.time() - 5,
                                        "limits_attempt_epoch": time.time() - 5})
    second = await client.get("/accounts/work/limits", headers=auth_headers)
    assert second.status_code == 200
    signed = route.calls.last.request
    assert signed.headers["authorization"] == "Bearer device-ticket"
    assert signed.headers["x-mirasim-device"]
    assert signed.headers["x-mirasim-probe"] == "usage"
    assert signed.headers["accept-encoding"] == "identity"
    cached = json.loads(state.store.row("work")["metadata_json"])["limits"]
    assert cached["windows"][0]["name"] == "5h"


@respx.mock
async def test_model_catalog_exposes_configured_default(client, state, auth_headers):
    add_account(state, "work")
    # The catalog is the account's own signed model roster (fixture-seeded),
    # shaped as an OpenAI list. No /v1/models passthrough remains.
    response = await client.get("/v1/models", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    ids = body["mirofish_model_ids"]
    assert "claude-fable-5" in ids and "gpt-5.6-luna" in ids
    assert all(entry["object"] == "model" for entry in body["data"])


@respx.mock
async def test_model_catalog_reads_signed_roster_once_per_ttl(client, state, auth_headers):
    add_account(state, "work")
    # Drop the fixture roster so the route must fetch it from the signed
    # endpoint, and prove it is cached (one upstream read per TTL).
    state.store.merge_metadata("work", {"model_roster": None, "roster_attempt_epoch": None})
    mock_device_session()
    roster = respx.get(RELAY_BASE + "/v1/model-roster").mock(return_value=httpx.Response(
        200, json={"version": "v-test",
                   "agents": {"claude": [{"id": "claude-opus-5", "contextWindow": 200000}]}}))

    first = await client.get("/v1/models", headers=auth_headers)
    second = await client.get("/v1/models", headers=auth_headers)

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["mirofish_model_ids"] == ["claude-opus-5"]
    assert roster.call_count == 1


@respx.mock
async def test_model_catalog_ignores_deleted_default_account(
        client, state, auth_headers):
    add_account(state, "work")
    add_account(state, "other")
    state.default_account = "work"
    state.remove_account("work")
    # The removed default is no longer a candidate; the catalog serves the
    # surviving account's roster (fixture-seeded) without error.
    response = await client.get("/v1/models", headers=auth_headers)

    assert response.status_code == 200
    assert "gpt-5.6-luna" in response.json()["mirofish_model_ids"]


async def test_model_catalog_rejects_explicit_disabled_account(
        client, state, auth_headers):
    add_account(state, "work")
    state.store.merge_metadata("work", {"disabled": True})

    response = await client.get(
        "/v1/models", headers={**auth_headers, "X-Mirofish-Account": "work"})

    assert response.status_code == 403


@respx.mock
async def test_status_probe_uses_zero_cost_limits_instead_of_messages(
        client, state, auth_headers):
    add_account(state, "work")
    respx.get(AUTH_BASE + "/auth/me").mock(
        return_value=httpx.Response(200, json={"id": "u-work", "email": "work@example.com"}))
    respx.get(AUTH_BASE + "/auth/referral").mock(
        return_value=httpx.Response(200, json={"current_plan": "pro"}))
    limits = respx.get(RELAY_BASE + "/v1/limits").mock(
        return_value=httpx.Response(200, json=LIMITS_RESPONSE))
    messages = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(500, json={"error": {"message": "must not run"}}))

    response = await client.get("/accounts/work/status?probe=1", headers=auth_headers)

    assert response.status_code == 200
    # A status refresh reads the subscription profile only. It is not model
    # traffic and no longer touches usage limits: the legacy probe flag is
    # ignored, and neither /v1/limits nor /v1/messages is contacted.
    assert response.json()["plan"] == "pro"
    assert limits.call_count == 0
    assert messages.call_count == 0
    calls = [str(call.request.url) for call in respx.calls]
    assert all(url.startswith(AUTH_BASE) for url in calls)


@respx.mock
async def test_status_refresh_stores_profile_and_keeps_local_fields(
        client, state, auth_headers):
    """The upstream knows the subscription tier, its expiry, and the holder;
    a status refresh stores that as the normalized profile. The refresh merges
    into metadata, so fields it does not produce — the panel's disabled
    switch, cached limits — survive it."""
    add_account(state, "work")
    state.store.merge_metadata("work", {"disabled": True,
                                        "limits": {"windows": []}})
    respx.get(AUTH_BASE + "/auth/me").mock(
        return_value=httpx.Response(200, json={
            "id": "u-work", "email": "work@example.com", "name": "Michael Chan",
            "roles": ["user"], "plan": "plus", "plan_exp": 1789029052}))
    respx.get(AUTH_BASE + "/auth/referral").mock(
        return_value=httpx.Response(200, json={
            "current_plan": "plus", "next_plan": "max", "redeemed": 3,
            "threshold": 10, "plan_expires_at": "2026-09-10T08:30:52.119910Z"}))

    response = await client.get("/accounts/work/status", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["plan"] == "plus"
    assert body["profile"] == {"name": "Michael Chan", "roles": ["user"],
                               "plan_expires_epoch": 1789029052.0,
                               "next_plan": "max"}
    assert body["referral"]["redeemed"] == 3
    metadata = json.loads(state.store.row("work")["metadata_json"])
    assert metadata["disabled"] is True
    assert metadata["limits"] == {"windows": []}
    assert state.store.row("work")["plan"] == "plus"


def test_profile_fields_falls_back_to_the_referral_iso_expiry():
    """Free-tier /auth/me bodies may omit plan_exp; the referral endpoint's
    ISO timestamp is the fallback, and a free account simply has neither."""
    profile = profile_fields(
        {"name": "n", "roles": ["user"]},
        {"plan_expires_at": "2026-09-10T08:30:52.119910Z", "next_plan": "max"})
    assert profile["plan_expires_epoch"] == pytest.approx(1789029052.119910, abs=1)

    assert profile_fields({}, {})["plan_expires_epoch"] is None


@respx.mock
async def test_all_limits_survives_one_failure(client, state, auth_headers):
    add_account(state, "alpha")
    add_account(state, "beta")
    mock_device_session()
    respx.get(RELAY_BASE + "/v1/limits").mock(side_effect=[
        httpx.Response(200, json=LIMITS_RESPONSE),
        httpx.Response(403, json={"error": {"message": "nope"}}),
    ])
    # GET /api/limits is cache-only; the explicit batch refresh is what reads
    # upstream, so one node's failure must not sink the others.
    response = await client.post("/api/limits/refresh", headers=auth_headers)
    assert response.status_code == 200
    results = {r["alias"]: r for r in response.json()["accounts"]}
    assert results["alpha"]["ok"] != results["beta"]["ok"]
    ok = next(r for r in results.values() if r["ok"])
    bad = next(r for r in results.values() if not r["ok"])
    assert ok["limits"]["windows"]
    assert bad["error"]


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
    respx.get(RELAY_BASE + "/v1/limits").mock(
        return_value=httpx.Response(200, json=LIMITS_RESPONSE))
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
    respx.get(RELAY_BASE + "/v1/limits").mock(
        return_value=httpx.Response(200, json=LIMITS_RESPONSE))
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
    respx.get(RELAY_BASE + "/v1/limits").mock(
        return_value=httpx.Response(200, json=LIMITS_RESPONSE))
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
    respx.get(RELAY_BASE + "/v1/limits").mock(
        return_value=httpx.Response(200, json=LIMITS_RESPONSE))
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(429, json=EXHAUSTED_BODY))
    response = await client.post("/v1/messages", headers=auth_headers, json={
        "model": "claude-haiku-4-5-20251001", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    })
    # Both accounts were tried once, then the actionable upstream refusal is
    # surfaced rather than a synthesized local one.
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "credit_exhausted_shared"
    assert route.call_count == 2


async def test_generic_upstream_error_does_not_fail_over_across_accounts(state):
    """Region-based proxy failover is gone. Only quota 429s fan out; a generic
    upstream/proxy error surfaces from the chosen account without silently
    substituting another one and without setting a quota cooldown."""
    add_account(state, "alpha")
    add_account(state, "beta")
    served = []

    async def run(account: str):
        served.append(account)
        raise RelayError("upstream unavailable", 502, {"error": {"message": "boom"}})

    with pytest.raises(RelayError) as caught:
        await state.with_account_failover("", "", _conv("hello"), run)
    assert caught.value.status == 502
    assert len(served) == 1
    assert state.exhausted_cooldown(served[0]) == 0
