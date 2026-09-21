"""Account verdicts persist; only completed model work recovers capacity."""

import datetime
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from mirofish.api.state import AppState, HEALTH_RETRY_AFTER
from mirofish.errors import RelayError
from tests.conftest import add_account

OVERLOADED = {"error": {"type": "overloaded_error", "message": "no upstream available"}}
SUSPENDED = {"error": {"type": "permission_error",
                       "message": "this account is suspended; contact support"}}


def refusal(status, body=None):
    return RelayError("upstream refused", status, body)


def temporary(seconds):
    stamp = datetime.datetime.fromtimestamp(time.time() + seconds, datetime.timezone.utc).isoformat()
    return {"error": {"type": "permission_error", "message":
        "this account is temporarily suspended after repeated upstream rate-limit "
        "refusals; access resumes at " + stamp + ". Contact support if unexpected"}}


@pytest.mark.parametrize("status,body,state_name", [
    (401, None, "error"), (503, OVERLOADED, "error"), (403, SUSPENDED, "suspended")])
def test_account_verdicts_park_and_detach_without_counting_quota(state, status, body, state_name):
    add_account(state, "a")
    add_account(state, "b")
    assert state.route_account("", "chat", {}) == "a"
    assert state.note_account_unserviceable("a", refusal(status, body))
    assert state.account_unhealthy("a")
    assert state.account_health("a")["state"] == state_name
    assert state.account_health("a")["status"] == status
    assert state.exhausted_cooldown("a") == 0
    assert state.route_account("", "chat", {}) == "b"


@pytest.mark.parametrize("status,body", [
    (400, None), (500, None), (502, {"proxy_network": True}), (503, None),
    (503, {"_raw": "<html>503</html>"}), (503, {"error": "service unavailable"}),
    (503, {"kind": "device_session_required"}), (503, {"kind": "no_proxy_node"}),
    (403, None), (403, {"error": {"type": "permission_error", "message": "forbidden"}}),
    (403, {"error": {"type": "invalid_request_error", "message": "suspended"}})])
def test_non_account_refusals_neither_park_nor_count_quota(state, status, body):
    add_account(state, "a")
    assert not state.note_account_unserviceable("a", refusal(status, body))
    assert state.account_health("a") == {}
    assert state.exhausted_cooldown("a") == 0
    assert state.route_account("", "chat", {}) == "a"


def test_temporary_suspension_uses_exact_deadline(state, monkeypatch):
    add_account(state, "a")
    now = time.time()
    state.note_account_error("a", refusal(403, temporary(3210)))
    assert state.health_retry_in("a") == pytest.approx(3210, abs=1)
    with pytest.raises(RelayError):
        state.route_account("a", "manual", {})
    monkeypatch.setattr("mirofish.api.state.time.time", lambda: now + 3211)
    assert not state.account_unhealthy("a")
    assert state.account_health("a")["status"] == 403
    assert state.route_account("", "business", {}) == "a"


def test_overloaded_waits_one_day_without_timer_recovery(state, monkeypatch):
    add_account(state, "a")
    now = time.time()
    state.note_account_error("a", refusal(503, OVERLOADED))
    assert state.health_retry_in("a") == pytest.approx(HEALTH_RETRY_AFTER[503], abs=1)
    assert HEALTH_RETRY_AFTER[503] == 86400
    monkeypatch.setattr("mirofish.api.state.time.time", lambda: now + 86401)
    assert not state.account_unhealthy("a")
    assert state.account_health("a")["status"] == 503
    assert state.route_account("", "business", {}) == "a"


@pytest.mark.parametrize("status,body", [(401, None), (403, SUSPENDED)])
async def test_indefinite_health_survives_restart_and_explicit_request(
        state, settings, status, body, monkeypatch):
    add_account(state, "a")
    state.note_account_error("a", refusal(status, body))
    future = time.time() + 365 * 86400
    monkeypatch.setattr("mirofish.api.state.time.time", lambda: future)
    revived = AppState(settings)
    try:
        assert revived.account_unhealthy("a")
        assert revived.health_retry_in("a") is None
        for requested in ("", "a"):
            with pytest.raises(RelayError):
                revived.route_account(requested, "", {})
    finally:
        await revived.aclose()


def test_401_requires_login_not_success(state):
    add_account(state, "a")
    state.note_account_error("a", refusal(401))
    state.note_account_healthy("a")
    assert state.account_unhealthy("a")
    state.reset_account_runtime("a")
    assert not state.account_unhealthy("a")


def test_legacy_parked_401_requires_login(state):
    add_account(state, "a")
    state.store.merge_metadata("a", {"parked": True, "parked_reason": "401"})
    assert state.account_unhealthy("a")
    state.note_account_healthy("a")
    assert state.account_unhealthy("a")
    state.reset_account_runtime("a")
    assert not state.account_unhealthy("a")


@pytest.mark.parametrize("status,body", [(503, OVERLOADED), (403, SUSPENDED)])
def test_relogin_preserves_non_auth_health_and_disabled(state, status, body):
    add_account(state, "a")
    state.note_account_error("a", refusal(status, body))
    state.store.merge_metadata("a", {"disabled": True})
    before = state.account_health("a")
    state.reset_account_runtime("a")
    assert state.account_health("a") == before
    assert state.account_disabled("a")


def test_manual_completed_model_conversation_recovers_capacity(state):
    add_account(state, "a")
    state.note_account_error("a", refusal(503, OVERLOADED))
    assert state.route_account("a", "manual", {"model": "claude-opus-5"}) == "a"
    state.note_account_healthy("a", account_generation=state.store.account_generation("a"))
    assert state.account_health("a") == {}


def test_manual_capacity_recovery_cannot_bypass_quota_or_disabled(state):
    add_account(state, "a")
    state.note_account_error("a", refusal(503, OVERLOADED))
    cache = state._windows_envelope("a")
    cache["windows"][0]["used"] = 90
    state.store.merge_metadata("a", {"limits": cache})
    with pytest.raises(RelayError) as caught:
        state.route_account("a", "", {"model": "claude-opus-5"})
    assert caught.value.status == 429
    state.store.merge_metadata("a", {"disabled": True})
    with pytest.raises(RelayError) as caught:
        state.route_account("a", "", {})
    assert caught.value.status == 403


def test_old_generation_success_cannot_heal_replaced_alias(state):
    add_account(state, "a")
    generation = state.store.account_generation("a")
    add_account(state, "a", "replacement@example.test")
    state.note_account_error("a", refusal(503, OVERLOADED))
    state.note_account_healthy("a", account_generation=generation)
    assert state.account_unhealthy("a")


def test_late_results_after_removal_are_harmless(state):
    add_account(state, "a")
    state.remove_account("a")
    state.note_account_error("a", refusal(503, OVERLOADED))
    state.note_account_healthy("a")
    state.reset_account_runtime("a")


@pytest.mark.parametrize("status,body", [(401, None), (403, SUSPENDED), (503, OVERLOADED)])
async def test_failed_refresh_records_health_and_raises(state, monkeypatch, status, body):
    add_account(state, "a")
    error = refusal(status, body)
    monkeypatch.setattr(state.accounts, "fetch_limits", AsyncMock(side_effect=error))
    with pytest.raises(RelayError) as caught:
        await state.refresh_limits_if_stale("a")
    assert caught.value is error
    assert state.account_unhealthy("a")


async def test_successful_limits_do_not_heal(state):
    add_account(state, "a")
    state.note_account_error("a", refusal(503, OVERLOADED))
    await state.refresh_limits_if_stale("a")
    assert state.account_unhealthy("a")


async def test_returned_200_or_open_stream_does_not_heal(state):
    add_account(state, "a")
    state.note_account_error("a", refusal(503, OVERLOADED))
    run = AsyncMock(return_value=httpx.Response(200, json={"input_tokens": 5}))
    alias, response = await state.with_account_failover("a", "manual", {}, run)
    assert alias == "a" and response.status_code == 200
    assert state.account_unhealthy("a")


async def test_count_and_catalog_cannot_manually_bypass_capacity_health(state):
    add_account(state, "a")
    state.note_account_error("a", refusal(503, OVERLOADED))
    run = AsyncMock()
    with pytest.raises(RelayError):
        await state.with_account_failover("a", "manual", {}, run, model_only=False)
    run.assert_not_awaited()
    with pytest.raises(RelayError):
        state.pick_account("a", allow_unhealthy=True)


def test_legacy_capacity_deadline_is_measured_from_record(state):
    add_account(state, "a")
    stamp = datetime.datetime.fromtimestamp(time.time() - 3600, datetime.timezone.utc).isoformat()
    state.store.merge_metadata("a", {"health": {
        "state": "error", "status": 503, "at": stamp, "message": "overloaded_error"}})
    assert state.health_retry_in("a") == pytest.approx(23 * 3600, abs=1)


def test_later_capacity_error_does_not_weaken_401(state):
    add_account(state, "a")
    state.note_account_error("a", refusal(401))
    state.note_account_error("a", refusal(503, OVERLOADED))
    assert state.account_health("a")["status"] == 401
    assert state.health_retry_in("a") is None


async def test_limits_suspended_flag_blocks_work_and_records_health(state, monkeypatch):
    add_account(state, "a")
    cache = state._windows_envelope("a")
    cache["suspended"] = True
    monkeypatch.setattr(state.accounts, "fetch_limits", AsyncMock(return_value=cache))
    run = AsyncMock()
    with pytest.raises(RelayError) as caught:
        await state.with_account_failover("a", "manual", {}, run)
    assert caught.value.status == 403
    assert state.account_health("a")["state"] == "suspended"
    run.assert_not_awaited()
