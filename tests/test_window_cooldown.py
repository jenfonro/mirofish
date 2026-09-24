"""An upstream quota refusal locks its window, not the whole account."""

import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from mirofish.api.state import AppState
from mirofish.errors import RelayError
from tests.conftest import RELAY_BASE, add_account
from tests.test_api import ANTHROPIC_RESPONSE, SSE_BODY, mock_device_session


FABLE = "claude-fable-5-1"
OPUS = "claude-opus-5"
GPT = "gpt-6"
WINDOW_SECONDS = {"5h": 5 * 3600, "7d": 7 * 86400,
                  "7d_claude": 7 * 86400, "7d_fable": 7 * 86400}


@pytest.fixture
def clock(monkeypatch):
    now = [time.time()]
    monkeypatch.setattr("mirofish.api.state.time.time", lambda: now[0])
    return now


def with_windows(state, alias, reset_at):
    add_account(state, alias)
    state.store.merge_metadata(alias, {"limits": {"windows": [
        {"name": name, "used": 0.1, "budget": 1.0, "length": length,
         "reset_at": reset_at}
        for name, length in WINDOW_SECONDS.items()
    ]}})


def refusal(window=None, message="", status=429):
    error = {"type": "rate_limit_error", "message": message}
    if window:
        error["code"] = "credit_exhausted_" + window
    return RelayError("model request rejected", status, {"error": error})


def assert_blocked(state, model, window, requested=""):
    with pytest.raises(RelayError) as caught:
        state.route_account(requested, "", {"model": model})
    assert caught.value.status == 429
    assert caught.value.data["error"]["code"] == "credit_exhausted_" + window


@pytest.mark.parametrize(("window", "blocked", "allowed"), [
    ("7d_fable", [FABLE, "claude-fable-5"], [OPUS, GPT]),
    ("7d_claude", [FABLE, OPUS], [GPT]),
    ("5h", [FABLE, OPUS, GPT], []),
    ("7d", [FABLE, OPUS, GPT], []),
])
def test_quota_refusal_locks_only_its_window_until_reset(
        state, clock, window, blocked, allowed):
    reset = clock[0] + 7200
    with_windows(state, "work", reset)
    assert state.note_account_unserviceable("work", refusal(window))
    assert state.exhausted_cooldown("work") == 0

    # Neither a one-minute timeout nor raising the scheduling ceiling can
    # override an explicit upstream exhaustion verdict.
    clock[0] += 61
    state.set_schedule_settings(1.5)
    for model in blocked:
        for requested in ("", "work"):
            assert_blocked(state, model, window, requested)
    for model in allowed:
        assert state.route_account("", "", {"model": model}) == "work"

    clock[0] = reset
    for model in blocked:
        assert state.route_account("", "", {"model": model}) == "work"


@pytest.mark.parametrize(("message", "window"), [
    ("已用满 7d_fable 用量上限，可切换其它模型继续 "
     "(this model family's allowance is spent; other models still work)",
     "7d_fable"),
    ("已用满 7d_claude 用量上限，可切换其它模型继续", "7d_claude"),
    ("已用满 5 小时用量上限，约 40 分钟后自动恢复。这是短时突发上限，"
     "本周额度并没有用完； / Your 5-hour usage limit is used up; "
     "it resets in about 40 min. Your weekly quota is not spent.", "5h"),
    ("Your 5-hour usage limit is used up; your weekly quota is not spent.", "5h"),
    ("已用满 7 天用量上限，约 2 天 11 小时后自动恢复。", "7d"),
    ("Your 7-day usage limit is used up; it resets in about 2d 11h.", "7d"),
])
def test_documented_quota_messages_are_not_transient_rate_limits(
        state, clock, message, window):
    with_windows(state, "work", clock[0] + 7200)
    assert state.note_account_unserviceable("work", refusal(message=message))
    clock[0] += 61
    assert_blocked(state, FABLE, window)


@pytest.mark.parametrize("error_type", [
    "credit_exhausted_shared", "credit_exhausted_7d_fable",
])
def test_legacy_error_type_also_locks_a_window(state, clock, error_type):
    with_windows(state, "work", clock[0] + 7200)
    error = RelayError("refused", 429, {"error": {"type": error_type}})
    assert state.note_account_unserviceable("work", error)
    clock[0] += 601
    assert_blocked(state, FABLE,
                   "7d" if error_type.endswith("_shared") else "7d_fable")


@pytest.mark.parametrize("field", ["code", "type"])
def test_explicit_window_message_takes_priority_over_generic_shared_error(
        state, clock, field):
    with_windows(state, "work", clock[0] + 7200)
    error = refusal(message="Your 5-hour usage limit is used up; "
                    "your weekly quota is not spent.")
    error.data["error"][field] = "credit_exhausted_shared"
    state.note_account_unserviceable("work", error)
    clock[0] += 61
    assert_blocked(state, FABLE, "5h")


def test_fable_lock_preserves_opus_session_affinity(state, clock):
    for alias in ("a", "b"):
        with_windows(state, alias, clock[0] + 7200)
    assert state.route_account("", "opus-conversation", {"model": OPUS}) == "a"
    state.note_account_unserviceable("a", refusal("7d_fable"))
    assert state.route_account("", "opus-conversation", {"model": OPUS}) == "a"
    assert state.route_account("", "fable-conversation", {"model": FABLE}) == "b"


async def test_failover_locks_each_spent_account_and_later_retries_stay_local(
        state, clock):
    for alias in ("a", "b"):
        with_windows(state, alias, clock[0] + 7200)
    calls = []

    async def upstream(alias):
        calls.append(alias)
        raise refusal("7d_fable")

    for i in range(10):
        with pytest.raises(RelayError) as caught:
            await state.with_account_failover(
                "", "conversation", {"model": FABLE}, upstream)
        assert caught.value.status == 429
        if i == 0:
            clock[0] += 61
    assert sorted(calls) == ["a", "b"]
    for alias in ("a", "b"):
        assert_blocked(state, FABLE, "7d_fable", alias)
        assert state.route_account(alias, "", {"model": OPUS}) == alias


def test_overlapping_windows_expire_independently(state, clock):
    with_windows(state, "work", clock[0] + 7200)
    metadata = json.loads(state.store.row("work")["metadata_json"])
    metadata["limits"]["windows"][0]["reset_at"] = clock[0] + 120
    state.store.update_metadata("work", metadata)
    state.note_account_unserviceable("work", refusal("5h"))
    state.note_account_unserviceable("work", refusal("7d_fable"))
    assert_blocked(state, OPUS, "5h")
    clock[0] += 120
    assert state.route_account("", "", {"model": OPUS}) == "work"
    assert_blocked(state, FABLE, "7d_fable")


async def test_limits_refresh_cannot_erase_a_known_refusal(
        state, clock, monkeypatch):
    reset = clock[0] + 7200
    with_windows(state, "work", reset)
    state.note_account_unserviceable("work", refusal("7d_fable"))
    windows = json.loads(state.store.row("work")["metadata_json"])["limits"]["windows"]
    calls = []

    async def limits(alias, proxy_url=None):
        calls.append(alias)
        return 200, {}, {"windows": windows}

    monkeypatch.setattr(state.upstream, "limits", limits)
    # A manual/background limits read remains allowed; lagging usage cannot
    # put this known-spent window back into the model scheduler.
    await state.accounts.fetch_limits("work")
    assert calls == ["work"]
    assert_blocked(state, FABLE, "7d_fable")
    assert state._windows("work")["7d_fable"]["used"] == 0.1
    clock[0] = reset
    assert state.route_account("", "", {"model": FABLE}) == "work"


async def test_window_lock_survives_a_relay_restart(state, clock):
    with_windows(state, "work", clock[0] + 7200)
    state.note_account_unserviceable("work", refusal("7d_fable"))
    restarted = AppState(state.settings)
    try:
        clock[0] += 61
        assert_blocked(restarted, FABLE, "7d_fable")
        assert restarted.route_account("", "", {"model": OPUS}) == "work"
    finally:
        await restarted.aclose()


@pytest.mark.parametrize("reset_offset", [None, -60])
def test_missing_reset_holds_the_window_until_limits_supply_it(
        state, clock, reset_offset):
    reset = None if reset_offset is None else clock[0] + reset_offset
    with_windows(state, "work", reset)
    state.note_account_unserviceable("work", refusal("7d_fable"))
    clock[0] += 7200
    assert_blocked(state, FABLE, "7d_fable")
    assert state.route_account("", "", {"model": OPUS}) == "work"

    metadata = json.loads(state.store.row("work")["metadata_json"])
    for entry in metadata["limits"]["windows"]:
        entry["reset_at"] = clock[0] + 120
    state.store.update_metadata("work", metadata)
    assert_blocked(state, FABLE, "7d_fable")
    clock[0] += 120
    assert state.route_account("", "", {"model": FABLE}) == "work"


def test_genuine_transient_rate_limit_still_uses_short_cooldown(state, clock):
    with_windows(state, "work", clock[0] + 7200)
    assert state.note_account_unserviceable(
        "work", refusal(message="rate limit exceeded"))
    assert state.exhausted_cooldown("work") == pytest.approx(60)
    clock[0] += 61
    assert state.route_account("", "", {"model": FABLE}) == "work"


@pytest.mark.parametrize("status", [401, 403, 503])
def test_non_429_never_creates_quota_locks(state, clock, status):
    with_windows(state, "work", clock[0] + 7200)
    assert not state.note_account_unserviceable(
        "work", refusal("7d_fable", status=status))
    assert state.route_account("", "", {"model": FABLE}) == "work"


@pytest.mark.parametrize("requested", ["", "work"])
@pytest.mark.parametrize(("path", "payload"), [
    ("/v1/messages", {"model": FABLE, "max_tokens": 16,
                      "messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/messages", {"model": FABLE, "max_tokens": 16, "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/messages", {"model": FABLE, "max_tokens": 1,
                      "messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/messages/count_tokens", {"model": FABLE,
                                   "messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/chat/completions", {"model": OPUS,
                              "messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/chat/completions", {"model": OPUS, "stream": True,
                              "messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/responses", {"model": GPT, "input": "hi", "stream": True}),
])
async def test_all_model_routes_stop_before_any_upstream_request(
        client, state, auth_headers, clock, monkeypatch, requested, path, payload):
    with_windows(state, "work", clock[0] + 7200)
    state.note_account_unserviceable("work", refusal("7d"))
    clock[0] += 61
    send = AsyncMock(side_effect=AssertionError("unexpected upstream request"))
    monkeypatch.setattr(state.upstream, "send_explicit", send)
    response = await client.post(
        path, json=payload,
        headers={**auth_headers, "X-Mirofish-Account": requested})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "credit_exhausted_7d"
    send.assert_not_awaited()


@pytest.mark.parametrize("stream", [False, True])
@respx.mock
async def test_real_429_response_stops_fable_retries_but_not_opus(
        client, state, auth_headers, clock, stream):
    with_windows(state, "work", clock[0] + 7200)
    mock_device_session()
    success = (httpx.Response(200, text=SSE_BODY,
                             headers={"content-type": "text/event-stream"})
               if stream else httpx.Response(200, json=ANTHROPIC_RESPONSE))
    route = respx.post(RELAY_BASE + "/v1/messages").mock(side_effect=[
        httpx.Response(429, json=refusal(
            message="已用满 7d_fable 用量上限，可切换其它模型继续").data),
        success,
    ])
    payload = {"model": FABLE, "max_tokens": 16, "stream": stream,
               "messages": [{"role": "user", "content": "hi"}]}
    first = await client.post("/v1/messages", headers=auth_headers, json=payload)
    assert first.status_code == 429
    assert route.call_count == 1
    clock[0] += 61
    for model in (FABLE, "claude-fable-5"):
        retry = await client.post("/v1/messages", headers=auth_headers,
                                  json={**payload, "model": model})
        assert retry.status_code == 429
    assert route.call_count == 1
    opus = await client.post("/v1/messages", headers=auth_headers,
                            json={**payload, "model": OPUS})
    assert opus.status_code == 200
    assert route.call_count == 2
    again = await client.post("/v1/messages", headers=auth_headers, json=payload)
    assert again.status_code == 429
    assert route.call_count == 2


@pytest.mark.parametrize("replace", [False, True])
async def test_inflight_refusal_does_not_lock_a_removed_or_replaced_account(
        state, clock, replace):
    with_windows(state, "work", clock[0] + 7200)

    async def upstream(alias):
        if replace:
            add_account(state, alias, "replacement@example.com")
        else:
            state.remove_account(alias)
        raise refusal("7d_fable")

    with pytest.raises(RelayError) as caught:
        await state.with_account_failover("work", "", {"model": FABLE}, upstream)
    assert caught.value.status == 429
    if replace:
        assert state.route_account("work", "", {"model": FABLE}) == "work"
