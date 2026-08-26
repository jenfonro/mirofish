"""Reset-first scheduling: spend the credit that is about to expire unused.

Balanced ordering spreads conversations evenly, which leaves an account whose
weekly window resets tomorrow only half spent -- the rest is thrown away at the
reset. Reset-first puts that account in front while it still has room.
"""

import json
import time

import pytest

from mirofish.api.state import (DEFAULT_SCHEDULE_MAX_UTILIZATION,
                                SCHEDULE_BALANCED, SCHEDULE_RESET_FIRST)
from mirofish.errors import RelayError

from tests.conftest import add_account

HOUR = 3600.0


def with_windows(state, alias, *, resets_in_hours, seven_day=0.0, fable=0.0):
    """Give an account the usage windows a /v1/limits probe would have cached."""
    add_account(state, alias)
    reset_at = time.time() + resets_in_hours * HOUR
    metadata = json.loads(state.store.row(alias)["metadata_json"])
    metadata["limits"] = {"windows": [
        {"name": "5h", "used": 0.0, "budget": 39200.0,
         "reset_at": time.time() + HOUR},
        {"name": "7d", "used": seven_day * 140000.0, "budget": 140000.0,
         "reset_at": reset_at},
        {"name": "7d_fable", "used": fable * 74200.0, "budget": 74200.0,
         "reset_at": reset_at},
    ]}
    state.store.update_metadata(alias, metadata)


def route(state, model="claude-opus-5", session="s1"):
    return state.route_account("", session, {"model": model})


def test_balanced_is_the_default(state):
    assert state.schedule_settings() == {
        "mode": SCHEDULE_BALANCED,
        "max_utilization": DEFAULT_SCHEDULE_MAX_UTILIZATION,
    }


def test_reset_first_prefers_the_soonest_expiring_window(state):
    with_windows(state, "expires-late", resets_in_hours=140, seven_day=0.02)
    with_windows(state, "expires-soon", resets_in_hours=20, seven_day=0.48)
    state.set_schedule_settings(SCHEDULE_RESET_FIRST, 0.98)
    # Balanced would pick either; only the reset time distinguishes them.
    assert route(state) == "expires-soon"


def test_balanced_ignores_the_reset_time(state):
    with_windows(state, "expires-late", resets_in_hours=140, seven_day=0.02)
    with_windows(state, "expires-soon", resets_in_hours=20, seven_day=0.48)
    state.set_schedule_settings(SCHEDULE_BALANCED, 0.98)
    # Neither account holds a session yet, so the tie falls to the first alias.
    assert route(state) == "expires-late"


def test_a_nearly_spent_account_stops_attracting_conversations(state):
    with_windows(state, "expires-soon", resets_in_hours=20, seven_day=0.99)
    with_windows(state, "expires-late", resets_in_hours=140, seven_day=0.10)
    state.set_schedule_settings(SCHEDULE_RESET_FIRST, 0.98)
    assert route(state) == "expires-late"


def test_the_ceiling_is_configurable(state):
    with_windows(state, "expires-soon", resets_in_hours=20, seven_day=0.99)
    with_windows(state, "expires-late", resets_in_hours=140, seven_day=0.10)
    # Raise it above the account's load and it becomes preferred again.
    state.set_schedule_settings(SCHEDULE_RESET_FIRST, 1.5)
    assert route(state) == "expires-soon"


def test_fable_also_counts_its_own_window(state):
    # The 7d window has room, but fable's does not; another account is better.
    with_windows(state, "fable-spent", resets_in_hours=20,
                 seven_day=0.30, fable=0.99)
    with_windows(state, "fable-free", resets_in_hours=140,
                 seven_day=0.30, fable=0.10)
    state.set_schedule_settings(SCHEDULE_RESET_FIRST, 0.98)
    assert route(state, model="claude-fable-5") == "fable-free"
    # A non-fable model is unaffected by that window.
    assert route(state, model="claude-opus-5", session="s2") == "fable-spent"


def test_an_unprobed_account_gets_no_head_start(state):
    add_account(state, "never-probed")
    with_windows(state, "expiring", resets_in_hours=2, seven_day=0.10)
    state.set_schedule_settings(SCHEDULE_RESET_FIRST, 0.98)
    # Both start at zero sessions, so only the known expiry breaks the tie.
    assert route(state) == "expiring"


def test_a_far_off_reset_earns_no_tilt(state):
    # Beyond the horizon there is still a week to spend the credit normally.
    add_account(state, "never-probed")
    with_windows(state, "resets-next-week", resets_in_hours=140, seven_day=0.10)
    state.set_schedule_settings(SCHEDULE_RESET_FIRST, 0.98)
    assert state._urgency_bonus("resets-next-week") == 0.0
    assert state._urgency_bonus("never-probed") == 0.0


def test_affinity_still_pins_a_live_conversation(state):
    with_windows(state, "expires-late", resets_in_hours=140, seven_day=0.02)
    with_windows(state, "expires-soon", resets_in_hours=20, seven_day=0.48)
    state.set_schedule_settings(SCHEDULE_RESET_FIRST, 0.98)
    first = route(state, session="chat-1")
    # Re-routing mid-conversation would lose the cached prefix and context.
    assert route(state, session="chat-1") == first


def test_settings_round_trip_and_reject_nonsense(state):
    assert state.set_schedule_settings(SCHEDULE_RESET_FIRST, 0.9) == {
        "mode": SCHEDULE_RESET_FIRST, "max_utilization": 0.9}
    assert state.schedule_settings()["mode"] == SCHEDULE_RESET_FIRST
    with pytest.raises(RelayError):
        state.set_schedule_settings("sideways", 0.9)
    with pytest.raises(RelayError):
        state.set_schedule_settings(SCHEDULE_RESET_FIRST, 0.0)


async def test_schedule_api_round_trip(client, state, auth_headers):
    response = await client.get("/api/schedule", headers=auth_headers)
    assert response.json()["mode"] == SCHEDULE_BALANCED
    response = await client.post("/api/schedule", headers=auth_headers,
                                 json={"mode": SCHEDULE_RESET_FIRST,
                                       "max_utilization": 0.95})
    assert response.json() == {"mode": SCHEDULE_RESET_FIRST, "max_utilization": 0.95}
    assert (await client.get("/api/schedule", headers=auth_headers)).json() == {
        "mode": SCHEDULE_RESET_FIRST, "max_utilization": 0.95}
    bad = await client.post("/api/schedule", headers=auth_headers,
                            json={"mode": "sideways"})
    assert bad.status_code == 400


def refusal(status, error_type=None):
    data = {"error": {"type": error_type}} if error_type else None
    return RelayError("upstream refused", status, data)


def test_any_account_scoped_429_frees_the_conversation(state):
    """A 429 the relay does not recognize must still move the conversation on.

    Affinity would otherwise route the client's retry straight back to the
    account that just answered 429, and keep doing so until its window resets.
    """
    add_account(state, "work")
    for error_type in ("credit_exhausted_shared", "rate_limit_error",
                       "usage_limit_reached", None):
        state._exhausted_until.clear()
        assert state.note_account_unserviceable("work", refusal(429, error_type)), \
            f"429 {error_type} left the account selectable"
        assert state.exhausted_cooldown("work") > 0


def test_region_refusal_stays_with_the_proxy_pool(state):
    # Rotating the exit fixes this one; taking the account out would not.
    add_account(state, "work")
    assert not state.note_account_unserviceable(
        "work", refusal(429, "shared_quota_unavailable"))
    assert state.exhausted_cooldown("work") == 0


def test_non_429_errors_are_left_alone(state):
    add_account(state, "work")
    for status in (400, 401, 500, 502):
        assert not state.note_account_unserviceable("work", refusal(status))
    assert state.exhausted_cooldown("work") == 0


def spread(state, count, model="claude-opus-5"):
    """Route N distinct conversations and report where they landed."""
    picks = {}
    for i in range(count):
        alias = route(state, model=model, session=f"chat-{i}")
        picks[alias] = picks.get(alias, 0) + 1
    return picks


def test_reset_first_still_spreads_the_concurrency(state):
    """The tilt must not funnel every conversation into one account.

    Sorting by the reset timestamp alone never ties, so the balanced part of
    the key would be dead code and one account would absorb all the load.
    """
    with_windows(state, "expires-soon", resets_in_hours=2, seven_day=0.40)
    with_windows(state, "expires-mid", resets_in_hours=60, seven_day=0.40)
    with_windows(state, "expires-late", resets_in_hours=140, seven_day=0.40)
    state.set_schedule_settings(SCHEDULE_RESET_FIRST, 0.98)

    picks = spread(state, 9)
    assert len(picks) == 3, f"conversations landed on {len(picks)} account(s): {picks}"
    # The expiring account leads, but the others keep taking their turn.
    assert picks["expires-soon"] > picks["expires-late"]
    assert max(picks.values()) <= 5


def test_balanced_splits_evenly(state):
    with_windows(state, "a", resets_in_hours=2, seven_day=0.40)
    with_windows(state, "b", resets_in_hours=60, seven_day=0.40)
    with_windows(state, "c", resets_in_hours=140, seven_day=0.40)
    state.set_schedule_settings(SCHEDULE_BALANCED, 0.98)
    # Expiry is ignored entirely, so the split is exact.
    assert spread(state, 9) == {"a": 3, "b": 3, "c": 3}


def test_the_head_start_is_bounded(state):
    """An expiring account leads by a fixed number of sessions, not forever."""
    with_windows(state, "expires-soon", resets_in_hours=1, seven_day=0.40)
    with_windows(state, "expires-late", resets_in_hours=140, seven_day=0.40)
    state.set_schedule_settings(SCHEDULE_RESET_FIRST, 0.98)
    picks = spread(state, 8)
    # Once its real count catches up with the bonus, the other one is used too.
    assert picks["expires-late"] >= 2, picks


def test_urgency_grows_as_the_window_closes(state):
    with_windows(state, "far", resets_in_hours=47, seven_day=0.10)
    with_windows(state, "near", resets_in_hours=1, seven_day=0.10)
    assert state._urgency_bonus("near") > state._urgency_bonus("far") > 0.0


def test_a_spent_account_yields_to_everyone_with_room(state):
    with_windows(state, "spent-but-expiring", resets_in_hours=1, seven_day=0.99)
    with_windows(state, "has-room", resets_in_hours=140, seven_day=0.10)
    state.set_schedule_settings(SCHEDULE_RESET_FIRST, 0.98)
    picks = spread(state, 6)
    assert picks["has-room"] > picks.get("spent-but-expiring", 0)
