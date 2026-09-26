"""Quota limits are read from the cache; the upstream is asked once an hour.

Page loads read what the last read stored, never the upstream. The refresh
button re-reads only the enabled accounts. In the background each enabled
account is read once its last read — or last attempt — is an hour old, and a
manual read resets that clock.
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


async def test_the_sweep_reads_each_account_at_most_once_an_hour(state, clock, reads):
    calls, _ = reads
    for alias in ("a", "b"):
        add_account(state, alias)
    start = clock[0]

    await state.refresh_all_limits()
    assert sorted(calls) == ["a", "b"]
    for offset in (60, 1800, 3599):
        clock[0] = start + offset
        await state.refresh_all_limits()
    assert sorted(calls) == ["a", "b"]

    # A manual read resets that account's clock; the other keeps its own.
    clock[0] = start + 1800
    await state.accounts.fetch_limits("a")
    calls.clear()
    clock[0] = start + 3600
    await state.refresh_all_limits()
    assert calls == ["b"]
    clock[0] = start + 5400
    await state.refresh_all_limits()
    assert calls == ["b", "a"]


async def test_a_failed_background_read_waits_the_hour_too(state, clock, reads):
    calls, failing = reads
    add_account(state, "flaky")
    failing.add("flaky")
    start = clock[0]

    await state.refresh_all_limits()
    clock[0] = start + 300
    await state.refresh_all_limits()
    assert calls == ["flaky"]
    clock[0] = start + 3600
    await state.refresh_all_limits()
    assert calls == ["flaky", "flaky"]


async def test_a_new_login_is_read_at_the_next_sweep(state, clock, reads):
    calls, failing = reads
    add_account(state, "work")
    failing.add("work")
    await state.refresh_all_limits()
    failing.clear()
    # Fresh credentials: the previous login's attempt clock does not carry over.
    add_account(state, "work")
    state.reset_account_runtime("work")
    clock[0] += 60
    await state.refresh_all_limits()
    assert calls == ["work", "work"]


def test_a_ban_recovery_probe_waits_an_hour(state, clock):
    add_account(state, "work")
    state.maybe_park_account("work", RelayError("rejected", 403, SUSPENDED_BODY))
    start = clock[0]
    clock[0] = start + 1800
    assert not state._park_probe_due("work")
    clock[0] = start + 3600
    assert state._park_probe_due("work")
