"""The ``x-anthropic-billing-header`` system block and the ``HEAD /api/hello``
preconnect, as a live 0.0.367 desktop running Claude Code 2.1.278 sends them.

Both vectors below are from that capture: the kernel's session-naming call
and the main call of the same turn, whose fingerprints differ only by the
prompt text.  The fingerprint is Claude Code's own (``aHe`` in the installed
binary): the first non-meta user text's UTF-16 units 4, 7 and 20, salted,
with the CLI version, SHA-256, three hex digits.
"""

import json

import httpx
import respx

from mirofish.upstream import _billing_fingerprint, _with_billing_header
from tests.conftest import RELAY_BASE, add_account
from tests.mirasim_protocol import billing_block, relay_metadata
from tests.test_api import ANTHROPIC_RESPONSE, mock_device_session

UA = "claude-cli/2.1.278 (external, mirasim)"
NAMING_PROMPT = ("<session>\nReply with the single word: pong\n</session>\n\nWrite the "
                 "title in the predominant language of the session")
MAIN_PROMPT = "Reply with the single word: pong"
REMINDER = {"type": "text", "text": "<system-reminder>\n# Environment\n</system-reminder>"}


def test_fingerprint_matches_the_live_capture():
    assert _billing_fingerprint(NAMING_PROMPT, "2.1.278") == "d43"
    assert _billing_fingerprint(MAIN_PROMPT, "2.1.278") == "02b"


def test_fingerprint_indexes_utf16_units_and_pads_short_prompts():
    # A CJK prompt is one unit per character; a short prompt pads with "0".
    assert _billing_fingerprint("你好，帮我改一下这个函数的返回值类型", "2.1.278") \
        == _billing_fingerprint("你好，帮我改一下这个函数的返回值类型!", "2.1.278")
    assert _billing_fingerprint("hi", "2.1.278") == _billing_fingerprint("hi!", "2.1.278")
    assert _billing_fingerprint("hi", "2.1.278") != _billing_fingerprint("hi there", "2.1.278")
    # An astral character occupies two units; the lone half becomes U+FFFD.
    assert len(_billing_fingerprint("ab🙂cdefghijklmnopqrstuvwxyz", "2.1.278")) == 3


def test_the_prompt_is_the_first_user_text_that_is_not_a_reminder():
    payload = {"model": "claude-haiku-4-5", "system": [{"type": "text", "text": "marker"}],
               "messages": [{"role": "user", "content": [
                   REMINDER, {"type": "text", "text": MAIN_PROMPT}]}]}
    out = _with_billing_header(payload, UA)
    assert out["system"][0] == {
        "type": "text",
        "text": "x-anthropic-billing-header: cc_version=2.1.278.02b; cc_entrypoint=mirasim;"}
    assert out["system"][1:] == payload["system"]


def test_a_callers_block_is_rebuilt_in_place_and_named_after_this_installation():
    payload = {"model": "claude-opus-4-8", "system": [
        {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.241.abc; cc_entrypoint=cli;",
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "rules"}],
        "messages": [{"role": "user", "content": MAIN_PROMPT}]}
    out = _with_billing_header(payload, UA)
    assert out["system"] == [billing_block(MAIN_PROMPT), {"type": "text", "text": "rules"}]
    # Only Claude bodies carry one, and only a recognised CLI identity can name it.
    assert _with_billing_header({**payload, "model": "gpt-5.6-luna"}, UA) is not out
    assert _with_billing_header({**payload, "model": "gpt-5.6-luna"}, UA)["system"] == payload["system"]
    assert _with_billing_header(payload, "custom-relay/1.0")["system"] == payload["system"]
    # A count carries no block unless the caller sent one.
    assert "system" not in _with_billing_header(
        {"model": "claude-opus-4-8", "messages": []}, UA, add=False)


@respx.mock
async def test_hello_precedes_a_sessions_first_call_on_an_account(client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    hello = respx.head(RELAY_BASE + "/api/hello").mock(return_value=httpx.Response(200))
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE))
    headers = {**auth_headers, "X-Claude-Code-Session-Id": "0f20cf48-c292-42e9-a99e-994511307deb"}
    payload = {"model": "claude-haiku-4-5", "max_tokens": 8,
               "messages": [{"role": "user", "content": "hi"}]}
    for _ in range(2):
        assert (await client.post("/v1/messages", headers=headers, json=payload)).status_code == 200
    assert hello.call_count == 1 and route.call_count == 2
    preconnect, first = hello.calls[0].request, route.calls[0].request
    assert [name for name, _ in preconnect.headers.raw][:3] == \
        [b"user-agent", b"accept", b"accept-encoding"]
    assert preconnect.headers["user-agent"] == "Bun/1.4.3"
    assert preconnect.headers["accept"] == "*/*"
    assert preconnect.headers["authorization"] == "Bearer device-ticket"
    assert "content-length" not in preconnect.headers
    sealed, model_sealed = relay_metadata(preconnect, "/api/hello"), relay_metadata(first)
    assert [name for name in sealed if name != "x-mirasim-client"] == [
        "x-mirasim-session", "x-mirasim-agent", "x-mirasim-device", "x-mirasim-locale",
        "x-mirasim-turn", "x-mirasim-call", "x-mirasim-ts", "x-mirasim-nonce", "x-mirasim-sig"]
    for name in ("x-mirasim-session", "x-mirasim-agent", "x-mirasim-device", "x-mirasim-turn"):
        assert sealed[name] == model_sealed[name]
    assert sealed["x-mirasim-call"] != model_sealed["x-mirasim-call"]
    assert "x-mirasim-account" not in model_sealed


@respx.mock
async def test_hello_fires_again_once_the_desktop_would_have_evicted_the_process(
        client, state, auth_headers, monkeypatch):
    add_account(state, "work")
    mock_device_session()
    hello = respx.head(RELAY_BASE + "/api/hello").mock(return_value=httpx.Response(200))
    respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE))
    headers = {**auth_headers, "X-Claude-Code-Session-Id": "0f20cf48-c292-42e9-a99e-994511307deb"}
    payload = {"model": "claude-haiku-4-5", "max_tokens": 8,
               "messages": [{"role": "user", "content": "hi"}]}
    await client.post("/v1/messages", headers=headers, json=payload)
    for key in list(state.upstream._hello_at):
        state.upstream._hello_at[key] -= 31 * 60
    await client.post("/v1/messages", headers=headers, json=payload)
    assert hello.call_count == 2


@respx.mock
async def test_a_failed_hello_never_blocks_the_model_call(client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    respx.head(RELAY_BASE + "/api/hello").mock(side_effect=httpx.ConnectError("down"))
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=ANTHROPIC_RESPONSE))
    response = await client.post("/v1/messages", headers=auth_headers, json={
        "model": "claude-haiku-4-5", "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200 and route.call_count == 1
    assert json.loads(route.calls.last.request.content)["system"][0]["text"].startswith(
        "x-anthropic-billing-header: cc_version=2.1.278.")
