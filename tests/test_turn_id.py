"""The 0.0.367 desktop's per-prompt ``x-mirasim-turn``.

The kernel mints one task id per user prompt and stamps it on every model
call made while answering that prompt.  The relay reads a caller's prompts
off its body: a tool loop keeps one id, the next prompt gets a new one, and
the same conversation on two accounts never shares an id.
"""

import json
import uuid

import httpx
import respx

from mirofish.upstream import relay_turn_id
from tests.conftest import RELAY_BASE, add_account
from tests.mirasim_protocol import relay_metadata
from tests.test_api import ANTHROPIC_RESPONSE, mock_device_session

SESSION = "0f20cf48-c292-42e9-a99e-994511307deb"


def _user(text):
    return {"role": "user", "content": text}


def _tool_result(text="ok"):
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": text},
        {"type": "text", "text": "<system-reminder>context</system-reminder>"},
    ]}


def _assistant(text="working"):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def test_turn_id_is_one_per_prompt_and_shared_by_its_tool_loop():
    prompt = {"messages": [_user("do it")]}
    loop = {"messages": [_user("do it"), _assistant(), _tool_result()]}
    loop_again = {"messages": [*loop["messages"], _assistant(), _tool_result()]}
    next_prompt = {"messages": [*loop_again["messages"], _assistant("done"),
                                _user("and this")]}

    first = relay_turn_id(SESSION, prompt)
    assert uuid.UUID(first).version == 4
    assert relay_turn_id(SESSION, loop) == first
    assert relay_turn_id(SESSION, loop_again) == first
    assert relay_turn_id(SESSION, next_prompt) != first
    # The id belongs to the account's session, not to the conversation text.
    assert relay_turn_id("another-session", prompt) != first


def test_turn_id_counts_responses_user_items():
    first = relay_turn_id(SESSION, {"input": [
        {"type": "message", "role": "developer", "content": "rules"},
        {"type": "message", "role": "user", "content": "hi"}]})
    tool_loop = relay_turn_id(SESSION, {"input": [
        {"type": "message", "role": "developer", "content": "rules"},
        {"type": "message", "role": "user", "content": "hi"},
        {"type": "function_call", "call_id": "c1", "name": "shell"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"}]})
    second = relay_turn_id(SESSION, {"input": [
        {"type": "message", "role": "developer", "content": "rules"},
        {"type": "message", "role": "user", "content": "hi"},
        {"type": "message", "role": "assistant", "content": "hello"},
        {"type": "message", "role": "user", "content": "more"}]})
    assert tool_loop == first != second
    assert relay_turn_id(SESSION, {"input": "hi"}) == first


def test_a_compacted_history_does_not_revive_the_first_turn_id():
    """Compaction leaves one summary user message, so the prompt count is
    back at one; the id must still not be the first prompt's, which a kernel
    task id never repeats.  The loop under the summary keeps the new id."""
    first = relay_turn_id(SESSION, {"messages": [_user("do it")]})
    summary = _user("This session is being continued from a previous conversation...")
    compacted = relay_turn_id(SESSION, {"messages": [summary]})
    assert compacted != first
    assert relay_turn_id(SESSION, {"messages": [summary, _assistant(), _tool_result()]}) \
        == compacted


@respx.mock
async def test_messages_carry_the_turn_between_locale_and_call(
        client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE))
    headers = {**auth_headers, "X-Claude-Code-Session-Id": SESSION}
    turns = []
    for messages in (
            [_user("do it")],
            [_user("do it"), _assistant(), _tool_result()],
            [_user("do it"), _assistant(), _tool_result(), _assistant("done"),
             _user("and this")]):
        response = await client.post("/v1/messages", headers=headers, json={
            "model": "claude-sonnet-5", "max_tokens": 16, "messages": messages})
        assert response.status_code == 200
        sealed = relay_metadata(route.calls.last.request)
        names = [name for name in sealed if name != "x-mirasim-client"]
        assert names.index("x-mirasim-locale") < names.index("x-mirasim-turn") \
            < names.index("x-mirasim-call")
        turns.append(sealed["x-mirasim-turn"])
    assert turns[0] == turns[1] != turns[2]
    assert uuid.UUID(turns[2]).version == 4


@respx.mock
async def test_the_same_turn_on_two_accounts_is_two_ids(client, state, auth_headers):
    add_account(state, "a")
    add_account(state, "b")
    mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE))
    payload = {"model": "claude-sonnet-5", "max_tokens": 16,
               "messages": [_user("do it")]}
    seen = {}
    for alias in ("a", "b"):
        response = await client.post("/v1/messages", headers={
            **auth_headers, "X-Mirofish-Account": alias,
            "X-Claude-Code-Session-Id": SESSION}, json=payload)
        assert response.status_code == 200
        seen[alias] = relay_metadata(route.calls.last.request)["x-mirasim-turn"]
    assert seen["a"] != seen["b"]


@respx.mock
async def test_codex_relay_carries_the_turn(client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/responses").mock(
        return_value=httpx.Response(200, content=b"event: done\n\n",
                                    headers={"content-type": "text/event-stream"}))
    body = json.dumps({"model": "gpt-5.6-codex", "stream": True,
                       "input": [{"role": "user", "content": "hello"}]}).encode()
    response = await client.post("/v1/responses", content=body, headers={
        **auth_headers, "content-type": "application/json", "session-id": SESSION})
    assert response.status_code == 200
    sealed = relay_metadata(route.calls.last.request)
    assert uuid.UUID(sealed["x-mirasim-turn"]).version == 4
    names = [name for name in sealed if name != "x-mirasim-client"]
    assert names.index("x-mirasim-turn") + 1 == names.index("x-mirasim-call")
