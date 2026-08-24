from copy import deepcopy

import pytest

from mirofish.upstream import (CLAUDE_AGENT_SYSTEM_MARKER,
                               _claude_compatible_payload)


MARKER_BLOCK = {"type": "text", "text": CLAUDE_AGENT_SYSTEM_MARKER}


@pytest.mark.parametrize(("system", "tail"), [
    (None, []),
    ("", []),
    ("Keep the answer terse.", [{"type": "text", "text": "Keep the answer terse."}]),
    ([{"type": "text", "text": "Keep the answer terse."}],
     [{"type": "text", "text": "Keep the answer terse."}]),
])
def test_claude_compatibility_marker_is_copy_on_write_and_idempotent(system, tail):
    payload = {
        "model": "claude-fable-5",
        "max_tokens": 2,
        "messages": [{"role": "user", "content": "hi"}],
    }
    if system is not None:
        payload["system"] = system
    original = deepcopy(payload)

    prepared = _claude_compatible_payload(payload)

    assert prepared is not payload
    assert payload == original
    assert prepared["system"] == [MARKER_BLOCK, *tail]
    assert _claude_compatible_payload(prepared) is prepared


@pytest.mark.parametrize("system", [
    CLAUDE_AGENT_SYSTEM_MARKER,
    [{"type": "text", "text": CLAUDE_AGENT_SYSTEM_MARKER,
      "cache_control": {"type": "ephemeral"}}],
])
def test_official_claude_marker_is_left_unchanged(system):
    payload = {"model": "claude-opus-4-8", "system": system, "messages": []}

    assert _claude_compatible_payload(payload) is payload


@pytest.mark.parametrize("payload", [
    {"model": "gpt-5.6-luna", "messages": []},
    {"model": "kimi-k3", "messages": []},
    {"model": "claude-fable-5", "system": {"invalid": True}, "messages": []},
])
def test_compatibility_marker_does_not_change_other_or_malformed_payloads(payload):
    assert _claude_compatible_payload(payload) is payload
