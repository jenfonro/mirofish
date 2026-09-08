"""Reset-first scheduling: spend the credit that is about to expire unused.

Balanced ordering spreads conversations evenly, which leaves an account whose
weekly window resets tomorrow only half spent -- the rest is thrown away at the
reset. Reset-first puts that account in front while it still has room.
"""

import asyncio
import json
import time

import pytest

from mirofish.api.state import (DEFAULT_SCHEDULE_MAX_UTILIZATION,
                                MAX_QUOTA_COOLDOWN,
                                SCHEDULE_BALANCED, SCHEDULE_FABLE_FIRST,
                                SCHEDULE_RESET_FIRST,
                                SHARED_QUOTA_COOLDOWN, TRANSIENT_429_COOLDOWN)
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


def test_fable_first_prefers_the_fullest_fable_window(state):
    """Non-fable traffic goes to the urgent account with no fable credit left.

    Both windows reset at the same time and carry the same 7-day spend, so
    reset-first alone cannot separate them; only the fable window can.
    """
    with_windows(state, "fable-spent", resets_in_hours=20,
                 seven_day=0.50, fable=1.02)
    with_windows(state, "fable-free", resets_in_hours=20,
                 seven_day=0.50, fable=0.05)
    state.set_schedule_settings(SCHEDULE_FABLE_FIRST, 0.98)

    assert route(state, model="claude-opus-5") == "fable-spent"
    # Reset-first cannot tell them apart, so it falls back to alias order --
    # this is exactly what the new mode adds.
    state.set_schedule_settings(SCHEDULE_RESET_FIRST, 0.98)
    assert route(state, model="claude-opus-5", session="s2") == "fable-free"


def test_fable_first_leaves_fable_traffic_on_reset_first_ordering(state):
    """A fable request must not be steered at the spent fable window.

    For fable traffic the fable window is the constraint, not the criterion:
    sending it to the account whose fable credit is gone would just earn a
    refusal.
    """
    with_windows(state, "fable-spent", resets_in_hours=20,
                 seven_day=0.50, fable=1.02)
    with_windows(state, "fable-free", resets_in_hours=20,
                 seven_day=0.50, fable=0.05)
    state.set_schedule_settings(SCHEDULE_FABLE_FIRST, 0.98)

    # The spent fable window is exhausted for this model, so automatic
    # selection skips it and the account with headroom serves the request.
    assert route(state, model="claude-fable-5-1") == "fable-free"


def test_fable_first_still_honours_the_reset_horizon(state):
    """A full fable window outside the horizon earns no head start.

    Otherwise the mode would drain accounts whose weekly credit is not
    expiring, purely because their fable window happens to be spent.
    """
    with_windows(state, "far-fable-spent", resets_in_hours=140,
                 seven_day=0.50, fable=1.02)
    with_windows(state, "near-fable-free", resets_in_hours=6,
                 seven_day=0.50, fable=0.60)
    state.set_schedule_settings(SCHEDULE_FABLE_FIRST, 0.98)

    assert route(state, model="claude-opus-5") == "near-fable-free"


def test_fable_first_still_spreads_the_concurrency(state):
    """The fable tilt is capped like the reset tilt, so it cannot funnel."""
    for index in range(4):
        with_windows(state, f"acct-{index}", resets_in_hours=20,
                     seven_day=0.30, fable=1.0)
    state.set_schedule_settings(SCHEDULE_FABLE_FIRST, 0.98)

    picks = spread(state, 12, model="claude-opus-5")
    assert len(picks) > 1, f"every conversation landed on one account: {picks}"
    assert max(picks.values()) <= 6, picks


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


def test_balanced_also_skips_an_exhausted_fable_window(state):
    """The default mode must not keep funneling fable conversations onto an
    account whose own fable window is spent just because its 7d has room —
    that is exactly how a 7d_fable window gets driven to 130%."""
    with_windows(state, "fable-burnt", resets_in_hours=100,
                 seven_day=0.70, fable=1.30)
    with_windows(state, "fable-free", resets_in_hours=100,
                 seven_day=0.80, fable=0.20)
    state.set_schedule_settings(SCHEDULE_BALANCED, 0.98)
    for i in range(4):
        assert route(state, model="claude-fable-5", session=f"f{i}") == "fable-free"
    # Non-fable requests may still spend the account's remaining 7d room.
    assert route(state, model="claude-opus-5", session="o1") == "fable-burnt"


def test_balanced_respects_the_utilization_ceiling(state):
    """A nearly-spent account stops attracting new conversations in balanced
    mode too; overshoot past the budget cannot be walked back afterwards."""
    with_windows(state, "nearly-spent", resets_in_hours=100, seven_day=0.985)
    with_windows(state, "has-room", resets_in_hours=100, seven_day=0.20)
    state.set_schedule_settings(SCHEDULE_BALANCED, 0.98)
    assert spread(state, 3) == {"has-room": 3}


def test_a_window_that_already_reset_no_longer_counts(state):
    """A cached probe from before the weekly reset is history, not load;
    treating it as current would bench a freshly refilled account."""
    with_windows(state, "reset-since-probe", resets_in_hours=-1,
                 seven_day=1.02, fable=1.16)
    assert state._load("reset-since-probe", "claude-fable-5") == 0.0
    assert state._quota_ok("reset-since-probe", "claude-fable-5")


def test_a_stale_header_scalar_past_its_reset_is_ignored(state):
    add_account(state, "work")
    state.store.merge_metadata("work", {"quota": {
        "7d_utilization": "1.02", "7d_reset_epoch": str(time.time() - 60)}})
    assert state._quota_ok("work")


def test_headerless_responses_keep_the_cached_quota(state):
    """A response without ratelimit headers (e.g. the Codex path) must not
    wipe the probed utilization — a None there reads as "has room" and puts
    an exhausted account straight back into rotation."""
    add_account(state, "work")
    state.store.merge_metadata("work", {"quota": {
        "7d_utilization": "1.02", "7d_reset_epoch": str(time.time() + 86400)}})
    assert not state._quota_ok("work")
    state.record_usage("work", "claude-opus-5",
                       {"input_tokens": 5, "output_tokens": 2}, {})
    quota = json.loads(state.store.row("work")["metadata_json"])["quota"]
    assert quota["7d_utilization"] == "1.02"
    assert not state._quota_ok("work")


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


def test_credit_exhaustion_cools_much_longer_than_a_transient_429(state):
    """Only the documented credit exhaustion earns the full cooldown.

    An unrecognized 429 is usually transient rate pressure; benching the
    account for the full 10 minutes would turn a seconds-long hiccup into an
    outage for a single-account deployment.
    """
    add_account(state, "work")
    state.note_account_unserviceable(
        "work", refusal(429, "credit_exhausted_shared"))
    assert state.exhausted_cooldown("work") == \
        pytest.approx(MAX_QUOTA_COOLDOWN, abs=5)
    state._exhausted_until.clear()
    state.note_account_unserviceable("work", refusal(429, "rate_limit_error"))
    assert state.exhausted_cooldown("work") == \
        pytest.approx(TRANSIENT_429_COOLDOWN, abs=5)


def exhausted_refusal(code, message=""):
    """The shape the upstream actually sends for a spent window: the window is
    named in `code`, while `type` is the same generic value a momentary rate
    refusal carries."""
    return RelayError("upstream refused", 429, {"error": {
        "code": code, "type": "rate_limit_error", "message": message}})


@pytest.mark.parametrize("code", ["credit_exhausted_5h", "credit_exhausted_7d",
                                  "credit_exhausted_shared"])
def test_a_spent_window_is_read_from_the_error_code(state, code):
    """`type` cannot tell a spent window from a hiccup — only `code` can.

    Reading just `type` classified every exhausted window as transient, so the
    account came back after 60s, was refused again, and enough of those earn an
    upstream 403 suspension for "repeated rate-limit refusals".
    """
    add_account(state, "work")

    assert state.note_account_unserviceable("work", exhausted_refusal(code))

    assert state.exhausted_cooldown("work") > TRANSIENT_429_COOLDOWN


def test_a_spent_window_cools_until_that_window_resets(state):
    """The error names the window, so the cooldown can match the refusal
    instead of being a fixed guess."""
    add_account(state, "work")
    reset_in = 1800.0
    state.store.merge_metadata("work", {"limits": {"windows": [
        {"name": "5h", "used": 39200.0, "budget": 39200.0,
         "reset_at": time.time() + reset_in},
    ]}})

    state.note_account_unserviceable("work", exhausted_refusal("credit_exhausted_5h"))

    assert state.exhausted_cooldown("work") == pytest.approx(reset_in, abs=30)


def test_a_known_reset_is_waited_out_in_full(state):
    """A stated deadline is used as-is, however far off.

    Capping it at an hour re-probed a 7-day window hourly for days, spending
    one upstream refusal per account every time — the very pattern that earns
    a suspension. An early reset is noticed by the limits sweep instead, which
    re-reads the window every few minutes for free.
    """
    add_account(state, "work")
    reset_in = 5 * 86400
    state.store.merge_metadata("work", {"limits": {"windows": [
        {"name": "7d", "used": 140000.0, "budget": 140000.0,
         "reset_at": time.time() + reset_in},
    ]}})

    state.note_account_unserviceable("work", exhausted_refusal("credit_exhausted_7d"))

    assert state.exhausted_cooldown("work") == pytest.approx(reset_in, abs=30)


def test_an_unknown_reset_falls_back_to_the_ceiling(state):
    """With no cached deadline there is nothing to wait out, so re-probe on the
    fixed ceiling rather than guessing."""
    add_account(state, "work")

    state.note_account_unserviceable("work", exhausted_refusal("credit_exhausted_7d"))

    assert state.exhausted_cooldown("work") == pytest.approx(
        MAX_QUOTA_COOLDOWN, abs=5)


def test_an_unknown_window_falls_back_to_the_fixed_cooldown(state):
    """No cached window for the named code: still bench it properly."""
    add_account(state, "work")

    state.note_account_unserviceable("work", exhausted_refusal("credit_exhausted_9z"))

    assert state.exhausted_cooldown("work") == pytest.approx(
        MAX_QUOTA_COOLDOWN, abs=5)


def test_an_imminent_reset_is_not_retried_immediately(state):
    """SHARED_QUOTA_COOLDOWN survives as the floor: a window seconds from
    resetting must not put the account straight back to be refused again."""
    add_account(state, "work")
    state.store.merge_metadata("work", {"limits": {"windows": [
        {"name": "5h", "used": 100.0, "budget": 100.0,
         "reset_at": time.time() + 30}]}})

    state.note_account_unserviceable("work", exhausted_refusal("credit_exhausted_5h"))

    assert state.exhausted_cooldown("work") == pytest.approx(
        SHARED_QUOTA_COOLDOWN, abs=5)


def _window(name, utilization, hours=5):
    budget = 1000.0
    return {"name": name, "used": budget * utilization, "budget": budget,
            "reset_at": time.time() + hours * 3600}


@pytest.mark.parametrize("model", ["claude-opus-5", "claude-fable-5-1"])
def test_a_spent_burst_window_leaves_automatic_selection(state, model):
    """Every request draws on the 5h window, whatever the model.

    It is the window that fills first, so an account with days of weekly
    credit can still be unable to serve anything. Leaving it out of the load
    calculation meant scheduling kept electing it, the upstream refused with
    credit_exhausted_5h, and repeating that is what earns a suspension.
    """
    add_account(state, "spent")
    add_account(state, "fresh")
    state.store.merge_metadata("spent", {"limits": {"windows": [
        _window("5h", 1.0), _window("7d", 0.02, 24 * 7),
        _window("7d_fable", 0.02, 24 * 7)]}})
    state.store.merge_metadata("fresh", {"limits": {"windows": [
        _window("5h", 0.1), _window("7d", 0.5, 24 * 7),
        _window("7d_fable", 0.5, 24 * 7)]}})

    assert not state._quota_ok("spent", model)
    assert state.route_account("", "", {"model": model,
                                        "messages": [{"role": "user",
                                                      "content": "a window"}]}) == "fresh"


def test_a_pool_wide_spent_burst_window_is_answered_locally(state):
    """A spent window is not a preference to fall back from — it is a fact.

    Serving anyway was the old behaviour, on the grounds that the upstream is
    the final authority. But the cache says this request cannot succeed, so
    sending it only buys one refusal per attempt, and repeating that is what
    suspends an account for a day.
    """
    add_account(state, "only")
    state.store.merge_metadata("only", {"limits": {"windows": [
        _window("5h", 1.0), _window("7d", 0.02, 24 * 7)]}})

    with pytest.raises(RelayError) as excinfo:
        state.route_account("", "", {
            "messages": [{"role": "user", "content": "a window"}]})

    assert excinfo.value.status == 429
    assert excinfo.value.payload()["error"]["code"] == "credit_exhausted_5h"


def test_missing_or_stale_numbers_still_serve(state):
    """The local refusal must rest on positive evidence only.

    An account with no cached windows, or whose window already reset, has no
    spend to speak of — refusing there would take the relay down over missing
    data rather than a real limit.
    """
    add_account(state, "unknown")
    add_account(state, "reset")
    state.store.merge_metadata("reset", {"limits": {"windows": [
        {"name": "5h", "used": 1000.0, "budget": 1000.0,
         "reset_at": time.time() - 60}]}})

    assert state.route_account("", "", {
        "messages": [{"role": "user", "content": "a window"}]}) in {"unknown", "reset"}


def fable_exhausted():
    """The refusal the upstream sends once a fable allowance is spent. Its own
    message says the account still serves everything else."""
    return RelayError("upstream refused", 429, {"error": {
        "code": "credit_exhausted_7d_fable", "type": "rate_limit_error",
        "message": "已用满 7d_fable 用量上限，可切换其它模型继续 (this model "
                   "family's allowance is spent; other models still work)"}})


def test_a_spent_fable_allowance_does_not_bench_other_models(state):
    """A cooldown only covers the window the upstream named.

    Every account exhausts its fable allowance long before its 7d window, so
    benching the whole account on that refusal emptied the pool for opus too:
    81 of 82 accounts sat out over a refusal that never applied to them, while
    the upstream itself said "other models still work".
    """
    add_account(state, "work")

    assert state.note_account_unserviceable("work", fable_exhausted())

    assert state.exhausted_cooldown("work", "claude-fable-5") > 0
    assert state.exhausted_cooldown("work", "claude-fable-5-1") > 0
    assert state.exhausted_cooldown("work", "claude-opus-5") == 0
    # The panel asks the account-wide question and still sees the cooldown.
    assert state.exhausted_cooldown("work") > 0


def test_the_last_account_still_routes_non_fable_traffic_after_a_fable_refusal(state):
    """The pool must not look empty to opus because fable is spent."""
    add_account(state, "work")
    state.note_account_unserviceable("work", fable_exhausted())

    assert state.route_account("", "", {
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": "a window"}]}) == "work"
    assert state.pick_account("", "claude-opus-5") == "work"


def test_an_unscoped_429_still_cools_the_whole_account(state):
    """A refusal that names no window says nothing about which models are
    affected, so it has to cover all of them."""
    add_account(state, "work")

    state.note_account_unserviceable("work", refusal(429, "rate_limit_error"))

    assert state.exhausted_cooldown("work", "claude-opus-5") > 0
    assert state.exhausted_cooldown("work", "claude-fable-5") > 0


def test_the_claude_window_holds_back_every_claude_model(state):
    """7d_claude sits between the shared 7d window and the per-family ones.

    A fable request spends 7d, 7d_claude and 7d_fable at once, and opus spends
    7d and 7d_claude, so exhausting it refuses both — while the gpt-*/kimi-*
    ids served over Codex never touch it and must keep working.
    """
    add_account(state, "work")

    state.note_account_unserviceable("work", exhausted_refusal(
        "credit_exhausted_7d_claude"))

    assert state.exhausted_cooldown("work", "claude-opus-5") > 0
    assert state.exhausted_cooldown("work", "claude-fable-5-1") > 0
    assert state.exhausted_cooldown("work", "claude-haiku-4-5") > 0
    assert state.exhausted_cooldown("work", "gpt-5.6-terra") == 0
    assert state.exhausted_cooldown("work", "kimi-k3") == 0


def test_a_spent_fable_window_leaves_the_claude_window_alone(state):
    """The narrower family must not hold back the wider one."""
    add_account(state, "work")

    state.note_account_unserviceable("work", fable_exhausted())

    assert state.exhausted_cooldown("work", "claude-fable-5") > 0
    assert state.exhausted_cooldown("work", "claude-opus-5") == 0


@pytest.mark.parametrize("spent,blocked,free", [
    ("7d_claude", "claude-opus-5", "gpt-5.6-terra"),
    ("7d_fable", "claude-fable-5", "claude-opus-5"),
    ("5h", "claude-opus-5", None),
    ("7d", "gpt-5.6-terra", None),
])
def test_scheduling_skips_an_account_whose_relevant_window_is_spent(
        state, spent, blocked, free):
    """Any one of the windows a request spends being full is enough to skip the
    account — and only for the models that spend it."""
    add_account(state, "spent")
    windows = [_window(name, 0.02, 24 * 7)
               for name in ("5h", "7d", "7d_claude", "7d_fable")]
    for entry in windows:
        if entry["name"] == spent:
            entry["used"] = entry["budget"]
    state.store.merge_metadata("spent", {"limits": {"windows": windows}})

    assert not state._quota_ok("spent", blocked)
    if free:
        assert state._quota_ok("spent", free)


def test_a_pool_wide_fable_refusal_answers_429_without_calling_upstream(state):
    """Once every account's fable allowance is spent, the relay answers itself.

    Sending the request anyway earns one more upstream refusal per attempt,
    and enough of those suspend the account for a day — the exact loop that
    benched ten accounts. The answer carries the upstream's own vocabulary so
    a client can tell "wait for the window" from "the relay is down".
    """
    add_account(state, "one")
    add_account(state, "two")
    for alias in ("one", "two"):
        state.note_account_unserviceable(alias, fable_exhausted())

    with pytest.raises(RelayError) as excinfo:
        state.route_account("", "", {
            "model": "claude-fable-5-1",
            "messages": [{"role": "user", "content": "a window"}]})

    error = excinfo.value
    assert error.status == 429
    body = error.payload()["error"]
    assert body["type"] == "rate_limit_error"
    assert body["code"] == "credit_exhausted_7d_fable"
    # Non-fable traffic is untouched by the same state.
    assert state.route_account("", "", {
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": "a window"}]}) in {"one", "two"}


def test_a_mixed_outage_does_not_claim_the_allowance_is_spent(state):
    """Only claim a spent allowance when that is really why the pool is empty;
    a disabled account plus a cooling one is a different situation."""
    add_account(state, "cooling")
    add_account(state, "off")
    state.store.merge_metadata("off", {"disabled": True})
    state.note_account_unserviceable("cooling", refusal(429, "rate_limit_error"))

    with pytest.raises(RelayError) as excinfo:
        state.route_account("", "", {
            "model": "claude-fable-5-1",
            "messages": [{"role": "user", "content": "a window"}]})

    assert excinfo.value.status == 503


def test_accounts_spent_on_different_windows_still_report_one(state):
    """Real pools are not uniform: a few accounts have spent 7d while most have
    spent 7d_fable. Both hold back a fable request, so the pool is genuinely
    out of allowance and must not fall back to the generic 503."""
    add_account(state, "weekly")
    for alias in ("fable_a", "fable_b"):
        add_account(state, alias)
        state.note_account_unserviceable(alias, fable_exhausted())
    state.note_account_unserviceable("weekly", RelayError(
        "upstream refused", 429, {"error": {
            "code": "credit_exhausted_7d", "type": "rate_limit_error"}}))

    with pytest.raises(RelayError) as excinfo:
        state.route_account("", "", {
            "model": "claude-fable-5-1",
            "messages": [{"role": "user", "content": "a window"}]})

    assert excinfo.value.status == 429
    # The window holding back the most accounts is the useful one to name.
    assert excinfo.value.payload()["error"]["code"] == "credit_exhausted_7d_fable"
    # The 7d account is spent for everything, but the fable ones still serve opus.
    assert state.route_account("", "", {
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": "a window"}]}) in {"fable_a", "fable_b"}


def test_region_refusal_stays_with_the_proxy_pool(state):
    # Rotating the exit fixes this one; taking the account out would not.
    add_account(state, "work")
    assert not state.note_account_unserviceable(
        "work", refusal(429, "shared_quota_unavailable"))
    assert state.exhausted_cooldown("work") == 0


def test_transport_and_client_errors_are_left_alone(state):
    """A bad request or a transport failure is not a verdict on the account.

    401/503 are (see test_account_health.py): those say the upstream will not
    serve this account at all.
    """
    add_account(state, "work")
    for status in (400, 500, 502):
        assert not state.note_account_unserviceable("work", refusal(status))
    assert state.exhausted_cooldown("work") == 0
    assert not state.account_unhealthy("work")


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


async def test_the_sweep_skips_disabled_accounts(state, monkeypatch):
    """A switched-off account never takes part in selection, so probing it
    would contact the upstream for nothing."""
    add_account(state, "on")
    add_account(state, "off")
    state.store.merge_metadata("off", {"disabled": True})
    probed = []

    async def fake_with_proxy(alias, op):
        probed.append(alias)

    monkeypatch.setattr(state, "with_proxy", fake_with_proxy)
    await state.refresh_all_limits()
    assert set(probed) == {"on"}


async def test_the_sweep_refreshes_missing_or_dated_profiles(state, monkeypatch):
    """The panel reads plan tier and expiry from the stored profile. It only
    changes on billing-period timescales, so the sweep re-reads it when it is
    missing (rows saved before profiles existed) or a day old — not every
    pass."""
    add_account(state, "fresh")
    state.store.merge_metadata("fresh", {
        "profile": {"name": "n", "plan_expires_epoch": time.time() + 7 * 86400}})
    add_account(state, "stale")  # saved without any profile
    calls = []

    async def fake_with_proxy(alias, op):
        calls.append(alias)

    monkeypatch.setattr(state, "with_proxy", fake_with_proxy)
    await state.refresh_all_limits()
    assert calls.count("fresh") == 1  # limits probe only
    assert calls.count("stale") == 2  # limits probe + profile refresh

    calls.clear()
    state.store.merge_metadata("fresh", {"checked_at": "2020-01-01T00:00:00+00:00"})
    await state.refresh_all_limits()
    assert calls.count("fresh") == 2  # a dated profile is re-read too


async def test_the_sweep_runs_in_every_mode(state, monkeypatch):
    """Both modes read the cached windows to keep exhausted accounts out of
    selection, and the fable window has no response header to refresh it, so
    the sweep can no longer wait for reset-first to be switched on."""
    sweeps = []

    async def fake_refresh():
        sweeps.append(time.time())

    monkeypatch.setattr(state, "refresh_all_limits", fake_refresh)
    state.start_limits_refresh()  # app startup, still in balanced mode
    for _ in range(200):
        if sweeps:
            break
        await asyncio.sleep(0.01)
    assert sweeps, "the startup sweep did not run in balanced mode"

    state.set_schedule_settings(SCHEDULE_RESET_FIRST, 0.98)
    state.kick_limits_refresh()
    before = len(sweeps)
    for _ in range(200):
        if len(sweeps) > before:
            break
        await asyncio.sleep(0.01)
    assert len(sweeps) > before, "the kick did not trigger a fresh sweep"
    await state.stop_limits_refresh()
