"""A 403 suspension takes the account out of scheduling at once.

The upstream answers a banned account with ``403 permission_error: this
account is suspended``. Every retry on that account only repeats it, so the
relay parks it, fails the request over to another account, and brings it back
only when the limits read the ban refused succeeds again.
"""

import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from mirofish.errors import RelayError
from tests.conftest import RELAY_BASE, add_account
from tests.mirasim_protocol import client_user_id, relay_metadata
from tests.test_api import ANTHROPIC_RESPONSE, SSE_BODY


OPUS = "claude-opus-5"
SUSPENDED_BODY = {"type": "error", "error": {
    "type": "permission_error",
    "message": "this account is suspended; contact support"}}
LIMITS_BODY = {"windows": [{"name": "7d", "used": 1.0, "budget": 100.0,
                            "reset_at": time.time() + 86400}]}


def suspended():
    return RelayError("model request rejected", 403, SUSPENDED_BODY)


def park_state(state, alias):
    metadata = json.loads(state.store.row(alias)["metadata_json"])
    return bool(metadata.get("parked")), metadata.get("parked_status")


async def test_suspension_parks_the_account_and_fails_over_in_the_same_request(state):
    for alias in ("a", "b"):
        add_account(state, alias)
    calls = []

    async def upstream(alias):
        calls.append(alias)
        if len(calls) == 1:
            raise suspended()
        return "served"

    account, result = await state.with_account_failover(
        "", "conversation", {"model": OPUS}, upstream)
    banned = calls[0]
    assert result == "served" and account != banned
    assert park_state(state, banned) == (True, 403)
    # The conversation moved for good, and no other conversation is handed
    # the banned account either.
    for hint in ("conversation", "another-conversation", ""):
        assert state.route_account("", hint, {"model": OPUS}) == account


async def test_every_account_suspended_each_is_asked_once_then_answered_locally(state):
    for alias in ("a", "b", "c"):
        add_account(state, alias)
    calls = []

    async def upstream(alias):
        calls.append(alias)
        raise suspended()

    with pytest.raises(RelayError) as caught:
        await state.with_account_failover("", "conversation", {"model": OPUS}, upstream)
    assert caught.value.status == 403
    assert sorted(calls) == ["a", "b", "c"]
    for _ in range(5):
        with pytest.raises(RelayError) as local:
            await state.with_account_failover(
                "", "conversation", {"model": OPUS}, upstream)
        assert local.value.status == 503
    assert len(calls) == 3


async def test_explicitly_requested_suspended_account_is_parked_not_substituted(state):
    for alias in ("a", "b"):
        add_account(state, alias)
    calls = []

    async def upstream(alias):
        calls.append(alias)
        raise suspended()

    with pytest.raises(RelayError) as caught:
        await state.with_account_failover("a", "", {"model": OPUS}, upstream)
    assert caught.value.status == 403
    assert calls == ["a"]
    assert park_state(state, "a") == (True, 403)
    assert not state.account_parked("b")
    with pytest.raises(RelayError) as again:
        state.route_account("a", "", {"model": OPUS})
    assert again.value.status == 403


@pytest.mark.parametrize("message", ["model not available on your plan", ""])
async def test_other_403_neither_parks_nor_fails_over(state, message):
    for alias in ("a", "b"):
        add_account(state, alias)
    calls = []

    async def upstream(alias):
        calls.append(alias)
        raise RelayError("model request rejected", 403, {"error": {
            "type": "permission_error", "message": message}})

    with pytest.raises(RelayError) as caught:
        await state.with_account_failover("", "conversation", {"model": OPUS}, upstream)
    assert caught.value.status == 403
    assert len(calls) == 1
    assert not state.account_parked(calls[0])


async def test_limits_refresh_parks_a_ban_and_its_success_lifts_it(state, monkeypatch):
    add_account(state, "work")
    monkeypatch.setattr(state.upstream, "limits", AsyncMock(side_effect=[
        (403, {}, SUSPENDED_BODY), (200, {}, LIMITS_BODY)]))
    with pytest.raises(RelayError) as caught:
        await state.accounts.fetch_limits("work")
    assert caught.value.status == 403
    assert park_state(state, "work") == (True, 403)
    with pytest.raises(RelayError) as local:
        state.route_account("", "", {"model": OPUS})
    assert local.value.status == 503

    await state.accounts.fetch_limits("work")
    assert park_state(state, "work") == (False, None)
    assert state.route_account("", "", {"model": OPUS}) == "work"


async def test_limits_401_does_not_park(state, monkeypatch):
    add_account(state, "work")
    monkeypatch.setattr(state.upstream, "limits", AsyncMock(
        return_value=(401, {}, {"error": {"message": "token expired"}})))
    with pytest.raises(RelayError):
        await state.accounts.fetch_limits("work")
    assert not state.account_parked("work")


def test_the_refusal_that_parks_counts_as_the_latest_check(state):
    add_account(state, "work")
    assert state.maybe_park_account("work", suspended())
    assert not state._park_probe_due("work")


async def test_ban_recovery_probe_reads_limits_never_the_profile(state, monkeypatch):
    add_account(state, "work")
    state.maybe_park_account("work", suspended())
    profile = AsyncMock(side_effect=AssertionError(
        "a banned account can still read its profile"))
    monkeypatch.setattr(state.accounts, "fetch_status", profile)
    monkeypatch.setattr(state.upstream, "limits", AsyncMock(side_effect=[
        (403, {}, SUSPENDED_BODY), (200, {}, LIMITS_BODY)]))
    await state._probe_parked("work")
    assert park_state(state, "work") == (True, 403)
    await state._probe_parked("work")
    assert park_state(state, "work") == (False, None)
    profile.assert_not_awaited()


async def test_credential_park_probe_still_reads_the_profile(state, monkeypatch):
    add_account(state, "work")
    state.maybe_park_account("work", RelayError(
        "model request rejected", 401, {"error": {"message": "token revoked"}}))
    assert park_state(state, "work") == (True, 401)
    monkeypatch.setattr(state.accounts, "fetch_status", AsyncMock(return_value={}))
    monkeypatch.setattr(state.upstream, "limits", AsyncMock(
        side_effect=AssertionError("a 401 park recovers through the profile")))
    await state._probe_parked("work")
    assert not state.account_parked("work")


@pytest.mark.parametrize("stream", [False, True])
@respx.mock
async def test_upstream_403_suspension_is_served_by_another_account_in_isolation(
        client, state, auth_headers, stream):
    """The failover retry is the other account's own request.

    Both attempts carry one caller's conversation, which is exactly when two
    accounts could be tied together, so nothing of the refused account's
    identity — bearer, device, ticket, session — may reach the second one.
    """
    for alias in ("a", "b"):
        add_account(state, alias)
    mints = []

    def mint(request):
        # The upstream issues one ticket per device. A single shared test
        # ticket would hide an identity leaking between accounts.
        device = json.loads(request.content)["deviceId"]
        mints.append((request.headers["authorization"], device))
        return httpx.Response(200, json={"ticket": "ticket-" + device,
                                         "expiresIn": 900})

    respx.post(RELAY_BASE + "/v1/device/session").mock(side_effect=mint)

    def success():
        if stream:
            return httpx.Response(200, text=SSE_BODY,
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=ANTHROPIC_RESPONSE)

    route = respx.post(RELAY_BASE + "/v1/messages").mock(side_effect=[
        httpx.Response(403, json=SUSPENDED_BODY), success(), success()])
    caller_session = "0f20cf48-c292-42e9-a99e-994511307deb"
    headers = {**auth_headers,
               "user-agent": "claude-cli/2.1.261 (external, mirasim)",
               "X-Claude-Code-Session-Id": caller_session}
    payload = {"model": OPUS, "max_tokens": 16, "stream": stream,
               "messages": [{"role": "user", "content": "hi"}]}
    first = await client.post("/v1/messages", headers=headers, json=payload)
    assert first.status_code == 200
    assert route.call_count == 2
    [banned] = [alias for alias in ("a", "b") if state.account_parked(alias)]
    healthy = "b" if banned == "a" else "a"

    # Each account minted its own device session with its own bearer.
    devices = {alias: state.upstream._signer(alias).device_id for alias in ("a", "b")}
    assert devices["a"] != devices["b"]
    assert sorted(mints) == sorted(
        ("Bearer access-" + alias, devices[alias]) for alias in ("a", "b"))
    sessions = set()
    for request, alias in ((route.calls[0].request, banned),
                           (route.calls[1].request, healthy)):
        metadata = relay_metadata(request)
        assert request.headers["authorization"] == "Bearer ticket-" + devices[alias]
        assert metadata["x-mirasim-device"] == devices[alias]
        # The caller's one session id becomes a distinct id per account, and
        # both session headers stay paired as the official client pairs them.
        expected = state.relay_session_id(caller_session, "", {}, alias)
        assert metadata["x-mirasim-session"] == expected
        assert request.headers["x-claude-code-session-id"] == expected
        # The body agrees with the headers: each account's own install and
        # its own session id, so the two requests share no identifier.
        assert json.loads(request.content)["metadata"] == {
            "user_id": client_user_id(state, alias, expected)}
        sessions.add(expected)
    assert caller_session not in sessions and len(sessions) == 2

    again = await client.post("/v1/messages", headers=headers, json=payload)
    assert again.status_code == 200
    assert route.call_count == 3
    assert route.calls[2].request.headers["authorization"] == \
        "Bearer ticket-" + devices[healthy]

    accounts = (await client.get("/accounts", headers=auth_headers)).json()["accounts"]
    assert sorted(a["parked_status"] for a in accounts if a["parked"]) == [403]
