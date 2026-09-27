"""Quota limits are read from the cache; the upstream is polled the way the
desktop polls it.

Page loads read what the last read stored, never the upstream. The refresh
button re-reads only the enabled accounts. In the background every enabled
account is asked /auth/me and then /v1/limits once a minute, idle or not, as
a signed-in 0.0.367 desktop does for as long as it is open; a 429 has the
refused account read at once.
"""

import datetime
import time
from unittest.mock import AsyncMock

import pytest
import respx
import httpx

from mirofish.errors import RelayError
from tests.conftest import RELAY_BASE, add_account
from tests.test_api import mock_device_session


SUSPENDED_BODY = {"type": "error", "error": {
    "type": "permission_error",
    "message": "this account is suspended; contact support"}}
RATE_LIMITED = {"error": {"type": "rate_limit_error", "message": "rate limit exceeded"}}
OPUS = "claude-opus-5"
FABLE = "claude-fable-5-1"


def limits_body():
    return {"windows": [{"name": "7d", "used": 1.0, "budget": 100.0,
                         "reset_at": time.time() + 86400}]}


@pytest.fixture
def clock(monkeypatch):
    """One clock for the sweep's arithmetic and the timestamps it reads."""
    now = [time.time()]
    monkeypatch.setattr("mirofish.api.state.time.time", lambda: now[0])
    stamp = lambda: datetime.datetime.fromtimestamp(  # noqa: E731
        now[0], datetime.timezone.utc).isoformat()
    monkeypatch.setattr("mirofish.accounts.utc_now", stamp)
    monkeypatch.setattr("mirofish.api.state.utc_now", stamp)
    return now


@pytest.fixture
def reads(state, monkeypatch):
    """Count upstream limits reads per account; a listed alias fails."""
    calls, failing = [], set()

    async def limits(alias, proxy_url=None):
        calls.append(alias)
        if alias in failing:
            return 503, {}, {"error": {"message": "overloaded"}}
        return 200, {}, limits_body()

    monkeypatch.setattr(state.upstream, "limits", limits)
    monkeypatch.setattr(state.accounts, "ping_identity", AsyncMock())
    return calls, failing


async def test_page_load_reads_the_cache_and_never_the_upstream(
        client, state, auth_headers, monkeypatch):
    for alias in ("cached", "never", "banned", "off"):
        add_account(state, alias)
    for alias in ("cached", "off"):
        state.store.merge_metadata(alias, {"limits": {"windows": limits_body()["windows"]}})
    state.store.merge_metadata("off", {"disabled": True})
    state.maybe_park_account("banned", RelayError("rejected", 403, SUSPENDED_BODY))
    send = AsyncMock(side_effect=AssertionError("unexpected upstream request"))
    monkeypatch.setattr(state.upstream, "send_explicit", send)

    response = await client.get("/api/limits", headers=auth_headers)
    assert response.status_code == 200
    entries = {entry["alias"]: entry for entry in response.json()["accounts"]}
    assert entries["cached"]["ok"] and entries["cached"]["limits"]["windows"]
    assert entries["off"]["ok"] and entries["off"]["limits"]["windows"]
    assert entries["never"] == {"alias": "never", "ok": False,
                                "error": "limits not read yet"}
    # A banned account reports the refusal that parked it, as a live read would.
    assert entries["banned"]["ok"] is False and entries["banned"]["status"] == 403
    assert "suspended" in entries["banned"]["error"]
    send.assert_not_awaited()


@respx.mock
async def test_the_refresh_button_reads_only_enabled_accounts_live(
        client, state, auth_headers):
    add_account(state, "on")
    add_account(state, "off")
    state.store.merge_metadata("off", {"disabled": True, "limits": {"windows": []}})
    mock_device_session()
    route = respx.get(RELAY_BASE + "/v1/limits").mock(
        return_value=httpx.Response(200, json=limits_body()))

    response = await client.get("/api/limits?refresh=1", headers=auth_headers)
    entries = {entry["alias"]: entry for entry in response.json()["accounts"]}
    assert route.call_count == 1
    assert entries["on"]["ok"] and entries["on"]["limits"]["windows"]
    assert entries["off"] == {"alias": "off", "ok": True, "limits": {"windows": []}}


async def test_the_sweep_polls_every_enabled_account_each_pass(state, clock, reads):
    calls, _ = reads
    for alias in ("a", "b", "off"):
        add_account(state, alias)
    state.store.merge_metadata("off", {"disabled": True})
    start = clock[0]

    await state.refresh_all_limits()
    assert sorted(calls) == ["a", "b"]
    for offset in (60, 120):
        clock[0] = start + offset
        await state.refresh_all_limits()
    assert sorted(calls) == ["a", "a", "a", "b", "b", "b"]
    # A manual read does not exempt an account from the next poll.
    await state.accounts.fetch_limits("a")
    calls.clear()
    clock[0] = start + 180
    await state.refresh_all_limits()
    assert sorted(calls) == ["a", "b"]


async def test_each_pass_asks_auth_me_before_the_windows(state, clock, reads):
    calls, _ = reads
    add_account(state, "work")
    order = []
    state.accounts.ping_identity = AsyncMock(side_effect=lambda *a, **k: order.append("me"))
    await state.refresh_all_limits()
    assert order == ["me"] and calls == ["work"]


async def test_a_failed_read_is_retried_at_the_next_pass(state, clock, reads):
    calls, failing = reads
    add_account(state, "flaky")
    failing.add("flaky")
    await state.refresh_all_limits()
    clock[0] += 60
    await state.refresh_all_limits()
    assert calls == ["flaky", "flaky"]


async def test_accounts_poll_at_their_own_second_of_the_minute(state):
    phases = {alias: state._poll_phase(alias) for alias in ("a", "b", "c", "d")}
    assert all(0 <= phase < 60 for phase in phases.values())
    assert len(set(phases.values())) > 1
    assert state._poll_phase("a") == phases["a"]


def test_a_ban_recovery_probe_waits_an_hour(state, clock):
    add_account(state, "work")
    state.maybe_park_account("work", RelayError("rejected", 403, SUSPENDED_BODY))
    start = clock[0]
    clock[0] = start + 1800
    assert not state._park_probe_due("work")
    clock[0] = start + 3601
    assert state._park_probe_due("work")


async def test_a_429_has_the_refused_account_read_at_once(state, clock, monkeypatch):
    for alias in ("a", "b"):
        add_account(state, alias)
    read = []

    async def limits(alias, proxy_url=None):
        read.append(alias)
        return 200, {}, {"windows": [{"name": "7d_fable", "used": 100.0,
                                      "budget": 100.0, "reset_at": clock[0] + 7200}]}

    monkeypatch.setattr(state.upstream, "limits", limits)
    kicks = []
    monkeypatch.setattr(state, "kick_limits_refresh", lambda: kicks.append(1))
    calls = []

    async def upstream(alias):
        calls.append(alias)
        if len(calls) == 1:
            raise RelayError("model request rejected", 429, RATE_LIMITED)
        return "served"

    account, result = await state.with_account_failover(
        "", "conversation", {"model": FABLE}, upstream)
    refused = calls[0]
    assert result == "served" and account != refused
    assert kicks, "the sweep was not woken"
    # A second refusal before the read collapses into the same read, and the
    # kicked pass reads only the account the refusal named.
    state.refresh_limits_after_refusal(refused)
    await state.refresh_all_limits(forced_only=True)
    assert read == [refused]
    # A read this recent already answers the next refusal.
    kicks.clear()
    state.refresh_limits_after_refusal(refused)
    assert not kicks and not state._limits_forced
    # The read tells scheduling which window is spent; other models still work.
    clock[0] += 61
    assert state.route_account("", "", {"model": FABLE}) == account
    assert state.route_account(refused, "", {"model": OPUS}) == refused


async def test_a_429_read_that_finds_a_ban_takes_the_account_out(
        state, clock, monkeypatch):
    add_account(state, "work")
    monkeypatch.setattr(state.upstream, "limits",
                        AsyncMock(return_value=(403, {}, SUSPENDED_BODY)))

    async def upstream(alias):
        raise RelayError("model request rejected", 429, RATE_LIMITED)

    with pytest.raises(RelayError):
        await state.with_account_failover("", "", {"model": OPUS}, upstream)
    await state.refresh_all_limits()
    assert state.account_parked("work")
    with pytest.raises(RelayError) as local:
        state.route_account("", "", {"model": OPUS})
    assert local.value.status == 503
