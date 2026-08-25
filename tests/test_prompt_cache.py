"""Prompt-cache breakpoints matching the captured official Messages body.

Ground truth (two independent official /v1/messages captures, 29 tools each):

    system[0]  billing header block   no cache_control
    system[1]  Agent SDK marker (62c) cache_control: ephemeral
    system[2]  main prompt            cache_control: ephemeral
    messages[last user].content[-1]   cache_control: ephemeral
    tools[*]                          no cache_control

Notably the 62-character marker block is marked, so there is no minimum-length
gate to reproduce, and 29 tools go unmarked, so tools are not a breakpoint.
"""

import json

import httpx
import pytest
import respx

from mirofish.upstream import (
    CLAUDE_AGENT_SYSTEM_MARKER, _carries_cache_control, _json_bytes,
    _with_cache_breakpoints,
)
from tests.conftest import RELAY_BASE, add_account
from tests.test_request_profile import _body as captured_body


EPHEMERAL = {"type": "ephemeral"}


def _breakpoints(payload: dict) -> list[str]:
    """Every marked location, as stable labels, in document order."""
    found = []
    for index, tool in enumerate(payload.get("tools") or []):
        if tool.get("cache_control"):
            found.append(f"tools[{index}]")
    for index, block in enumerate(payload.get("system") or []):
        if isinstance(block, dict) and block.get("cache_control"):
            found.append(f"system[{index}]")
    for index, message in enumerate(payload.get("messages") or []):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for position, block in enumerate(content):
            if isinstance(block, dict) and block.get("cache_control"):
                found.append(f"messages[{index}].content[{position}]")
    return found


def _official_shape() -> dict:
    """The captured layout: marker after a billing block, trailing reminder."""
    return {
        "model": "claude-fable-5",
        "max_tokens": 32,
        "tools": [{"name": f"tool_{n}", "description": "d", "input_schema": {}}
                  for n in range(29)],
        "system": [
            {"type": "text", "text": "x-anthropic-billing-header: cc=1;"},
            {"type": "text", "text": CLAUDE_AGENT_SYSTEM_MARKER},
            {"type": "text", "text": "You are an interactive agent." * 400},
        ],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "first"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            {"role": "user", "content": [{"type": "text", "text": "test"}]},
            {"role": "system", "content": "<total_tokens>15000000</total_tokens>"},
        ],
    }


def test_breakpoints_reproduce_the_captured_official_layout():
    prepared = _with_cache_breakpoints(_official_shape())

    assert _breakpoints(prepared) == [
        "system[1]", "system[2]", "messages[2].content[0]",
    ]
    # The billing block and every one of the 29 tools stay unmarked, exactly as
    # the official client sends them.
    assert "cache_control" not in prepared["system"][0]
    assert all("cache_control" not in tool for tool in prepared["tools"])


def test_the_short_marker_block_is_marked_despite_its_length():
    """Refutes a minimum-cacheable-length gate: the captured block is 62 chars."""
    prepared = _with_cache_breakpoints({
        "model": "claude-fable-5", "max_tokens": 8,
        "system": [{"type": "text", "text": CLAUDE_AGENT_SYSTEM_MARKER}],
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert len(CLAUDE_AGENT_SYSTEM_MARKER) == 62
    assert prepared["system"][0]["cache_control"] == EPHEMERAL


def test_never_exceeds_the_four_breakpoint_ceiling():
    for extra_system in range(1, 6):
        payload = _official_shape()
        payload["system"].extend(
            {"type": "text", "text": f"block {n}"} for n in range(extra_system))
        assert len(_breakpoints(_with_cache_breakpoints(payload))) <= 4


def test_a_repeated_marker_block_is_only_marked_once():
    payload = _official_shape()
    payload["system"] = [
        {"type": "text", "text": CLAUDE_AGENT_SYSTEM_MARKER} for _ in range(5)
    ]

    prepared = _with_cache_breakpoints(payload)

    assert _breakpoints(prepared) == [
        "system[0]", "system[4]", "messages[2].content[0]",
    ]


def test_caller_breakpoints_leave_the_body_byte_identical():
    """Anthropic caps breakpoints at four, so a caller's choices are final."""
    payload = _official_shape()
    payload["system"][2]["cache_control"] = dict(EPHEMERAL)
    before = json.dumps(payload, sort_keys=True)

    prepared = _with_cache_breakpoints(payload)

    assert _breakpoints(prepared) == ["system[2]"]
    assert json.dumps(prepared, sort_keys=True) == before


@pytest.mark.parametrize("where", [
    lambda p: p["tools"][0].update(cache_control=dict(EPHEMERAL)),
    lambda p: p["system"][0].update(cache_control=dict(EPHEMERAL)),
    lambda p: p["messages"][0]["content"][0].update(cache_control=dict(EPHEMERAL)),
])
def test_a_breakpoint_anywhere_counts_as_caller_owned(where):
    payload = _official_shape()
    assert not _carries_cache_control(payload)

    where(payload)

    assert _carries_cache_control(payload)
    assert _with_cache_breakpoints(payload) is payload


def test_the_captured_official_body_survives_byte_identically():
    """The sanitized capture already carries its own breakpoints, so an
    official client's request must reach upstream unchanged."""
    payload = json.loads(captured_body())
    assert _carries_cache_control(payload)

    assert _json_bytes(_with_cache_breakpoints(payload)) == _json_bytes(payload)


def test_added_breakpoints_land_where_the_capture_put_them():
    """Stripping the capture's breakpoints and re-adding reproduces them."""
    payload = json.loads(captured_body())
    expected = _breakpoints(payload)
    for block in payload["system"]:
        block.pop("cache_control", None)
    for message in payload["messages"]:
        for block in message["content"] if isinstance(message["content"], list) else []:
            block.pop("cache_control", None)
    payload["system"][1]["text"] = CLAUDE_AGENT_SYSTEM_MARKER

    assert _breakpoints(_with_cache_breakpoints(payload)) == expected


def test_non_claude_models_are_left_alone():
    payload = {"model": "gpt-5.6-luna", "max_tokens": 8,
               "system": [{"type": "text", "text": "hello"}],
               "messages": [{"role": "user", "content": "hi"}]}

    assert _with_cache_breakpoints(payload) is payload


def test_the_caller_payload_is_never_mutated_in_place():
    payload = _official_shape()

    prepared = _with_cache_breakpoints(payload)

    assert prepared is not payload
    assert _breakpoints(payload) == []
    assert payload["messages"][2]["content"][0] == {"type": "text", "text": "test"}


def test_string_user_content_is_promoted_so_it_can_carry_a_breakpoint():
    prepared = _with_cache_breakpoints({
        "model": "claude-fable-5", "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert prepared["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "hi", "cache_control": EPHEMERAL},
    ]}]


def test_the_breakpoint_lands_on_the_final_block_of_the_turn():
    """Agentic turns end in tool_result; caching must cover the whole turn."""
    prepared = _with_cache_breakpoints({
        "model": "claude-fable-5", "max_tokens": 8,
        "messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "1"},
            {"type": "tool_result", "tool_use_id": "b", "content": "2"},
        ]}],
    })

    assert _breakpoints(prepared) == ["messages[0].content[1]"]


@pytest.mark.parametrize("messages", [
    [],
    [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}],
    [{"role": "user", "content": ""}],
    [{"role": "user", "content": []}],
    [{"role": "user", "content": ["raw string block"]}],
])
def test_unmarkable_turns_are_skipped_rather_than_reshaped(messages):
    payload = {"model": "claude-fable-5", "max_tokens": 8, "messages": messages}

    prepared = _with_cache_breakpoints(payload)

    assert prepared["messages"] == messages


@respx.mock
async def test_messages_sends_the_breakpoints_upstream(state):
    add_account(state, "work")
    respx.post(RELAY_BASE + "/v1/device/session").mock(
        return_value=httpx.Response(200, json={"ticket": "t", "expiresIn": 900}))
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json={"content": [], "usage": {}}))

    await state.upstream.messages("work", _official_shape())

    assert _breakpoints(json.loads(route.calls.last.request.content)) == [
        "system[1]", "system[2]", "messages[2].content[0]",
    ]


@respx.mock
async def test_stream_messages_sends_the_breakpoints_upstream(state):
    add_account(state, "work")
    respx.post(RELAY_BASE + "/v1/device/session").mock(
        return_value=httpx.Response(200, json={"ticket": "t", "expiresIn": 900}))
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, text="data: {}\n\n"))

    response = await state.upstream.stream_messages("work", _official_shape())
    await response.aclose()

    assert _breakpoints(json.loads(route.calls.last.request.content)) == [
        "system[1]", "system[2]", "messages[2].content[0]",
    ]
