"""Fixed ordering and hard model-scoped quota ceilings."""

import time

import pytest

from mirofish.api.state import AppState, TRANSIENT_429_COOLDOWN, URGENCY_HORIZON_HOURS
from mirofish.errors import RelayError
from tests.conftest import add_account

WINDOWS = ("5h", "7d", "7d_claude", "7d_fable")
MODELS = ("claude-fable-5", "claude-fable-5-1", "claude-opus-5",
          "claude-haiku-4-5", "gpt-5.6-terra", "kimi-k3", "other-model")


def with_windows(state, alias, *, hours=100, burst=.1, seven_day=.1, claude=.1, fable=.1):
    add_account(state, alias)
    now = time.time()
    state.store.merge_metadata(alias, {"limits": {"fetched_epoch": now, "windows": [
        {"name": name, "used": used * 100, "budget": 100,
         "reset_at": now + (5 if name == "5h" else hours) * 3600}
        for name, used in zip(WINDOWS, (burst, seven_day, claude, fable))]}})


def route(state, session="chat", model="claude-opus-5", requested=""):
    return state.route_account(requested, session, {"model": model})


def refusal(window):
    return RelayError("spent", 429, {"error": {
        "type": "rate_limit_error", "code": "credit_exhausted_" + window}})


def test_reset_horizon_and_hour_bands_precede_fable(state, monkeypatch):
    now = time.time()
    monkeypatch.setattr("mirofish.api.state.time.time", lambda: now)
    with_windows(state, "later-band", hours=3.1, fable=.89)
    with_windows(state, "same-band-low", hours=2.1, fable=.1)
    with_windows(state, "same-band-high", hours=2.9, fable=.8)
    with_windows(state, "far", hours=140, fable=1)
    assert route(state) == "same-band-high"
    assert state._reset_rank("same-band-low") == 2
    assert state._reset_rank("same-band-high") == 2
    assert state._reset_rank("far") == URGENCY_HORIZON_HOURS


def test_beyond_48h_fable_breaks_ties(state):
    with_windows(state, "sooner", hours=60, fable=.1)
    with_windows(state, "later", hours=140, fable=.8)
    assert route(state) == "later"


def test_no_concurrency_count_ordering(state):
    with_windows(state, "soon", hours=2)
    with_windows(state, "later", hours=10, fable=.8)
    assert {route(state, f"chat-{i}") for i in range(20)} == {"soon"}
    assert state.session_counts() == {"soon": 20}


@pytest.mark.parametrize("session", ["", "unique"])
def test_last_assigned_breaks_otherwise_identical_ties(state, session):
    for alias in ("a", "b", "c"):
        with_windows(state, alias)
    assert [route(state, f"{session}-{i}" if session else "") for i in range(3)] == ["a", "b", "c"]


def test_affinity_survives_unrelated_window_cooldown(state):
    with_windows(state, "a")
    with_windows(state, "b")
    assert route(state, "opus") == "a"
    state.note_account_unserviceable("a", refusal("7d_fable"))
    assert route(state, "opus") == "a"
    assert route(state, "fable", "claude-fable-5-1") == "b"


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("spent", WINDOWS)
def test_four_window_matrix(state, model, spent):
    with_windows(state, "work")
    cache = state._windows_envelope("work")
    for window in cache["windows"]:
        if window["name"] == spent:
            window["used"] = 90
    state.store.merge_metadata("work", {"limits": cache})
    blocked = (spent in ("5h", "7d")
               or spent == "7d_claude" and model.startswith("claude-")
               or spent == "7d_fable" and model.startswith("claude-fable-"))
    assert state._quota_ok("work", model) is not blocked
    if blocked:
        with pytest.raises(RelayError) as caught:
            route(state, model=model)
        assert caught.value.status == 429
        assert caught.value.payload()["error"]["code"] == "credit_exhausted_" + spent
    else:
        assert route(state, model=model) == "work"


@pytest.mark.parametrize("path", ["default", "affinity", "no-session", "pick", "manual"])
@pytest.mark.parametrize("used", [.90, .99, 1, 1.3])
def test_every_path_obeys_hard_ceiling(state, path, used):
    with_windows(state, "a", hours=2)
    with_windows(state, "b", hours=140)
    assert route(state) == "a"
    cache = state._windows_envelope("a")
    cache["windows"][0]["used"] = used * 100
    state.store.merge_metadata("a", {"limits": cache})
    if path == "default":
        state.default_account = "a"
    if path == "manual":
        with pytest.raises(RelayError) as caught:
            route(state, requested="a")
        assert caught.value.status == 429
    elif path == "pick":
        assert state.pick_account("", "claude-opus-5") == "b"
    else:
        assert route(state, "" if path == "no-session" else "chat") == "b"


@pytest.mark.parametrize("session", ["", "chat"])
def test_pool_at_ceiling_never_uses_last_resort(state, session):
    with_windows(state, "a", seven_day=.90)
    with_windows(state, "b", burst=.95)
    state.default_account = "a"
    with pytest.raises(RelayError) as caught:
        route(state, session)
    assert caught.value.status == 429


def test_ceiling_defaults_to_90_percent_and_cannot_exceed_one(state):
    assert state.settings.quota_ceiling == .90
    with_windows(state, "a", seven_day=.999)
    state.settings.quota_ceiling = 1.0
    assert route(state) == "a"
    cache = state._windows_envelope("a")
    cache["windows"][1]["used"] = 100
    state.store.merge_metadata("a", {"limits": cache})
    state.settings.quota_ceiling = 1.5
    assert not state._quota_ok("a", "claude-opus-5")


@pytest.mark.parametrize("saved,expected", [
    ("0.1", .1), ("0.75", .75), ("1", 1), ("1.01", .8), ("0", .8),
    ("garbage", .8), ("nan", .8), ("inf", .8),
])
async def test_persisted_ceiling_loads_only_when_valid(state, settings, saved, expected):
    state.store.set_setting("quota_ceiling", saved)
    settings.quota_ceiling = .8
    revived = AppState(settings)
    try:
        assert revived.settings.quota_ceiling == expected
    finally:
        await revived.aclose()


@pytest.mark.parametrize("window", WINDOWS)
def test_cooldown_and_detachment_are_scoped(state, window):
    with_windows(state, "a")
    for model in MODELS:
        route(state, model, model)
    before = dict(state._sessions)
    state.note_account_unserviceable("a", refusal(window))
    for model in MODELS:
        affected = window in state._relevant_windows(model)
        assert (state.exhausted_cooldown("a", model) > 0) is affected
        assert (model in state._sessions) is not affected
        if not affected:
            assert state._sessions[model] == before[model]


@pytest.mark.parametrize("seconds", [30, 1800, 5 * 86400])
def test_cooldown_uses_exact_reset_not_hourly_probes(state, seconds):
    with_windows(state, "a")
    cache = state._windows_envelope("a")
    cache["windows"][1]["reset_at"] = time.time() + seconds
    state.store.merge_metadata("a", {"limits": cache})
    state.note_account_unserviceable("a", refusal("7d"))
    assert state.exhausted_cooldown("a") == pytest.approx(seconds, abs=1)


def test_missing_reset_uses_full_window_not_hourly_probe(state):
    with_windows(state, "a")
    state.store.merge_metadata("a", {"limits": None})
    state.note_account_unserviceable("a", refusal("7d"))
    assert state.exhausted_cooldown("a") == pytest.approx(7 * 86400, abs=1)


@pytest.mark.parametrize("body", [None, "plain 429", {"_raw": "HTML 429"},
    {"error": "slow down"}, {"error": {"type": "shared_quota_unavailable"}}])
def test_all_unscoped_429_shapes_cool_down(state, body):
    with_windows(state, "a")
    assert state.note_account_unserviceable("a", RelayError("429", 429, body))
    assert state.exhausted_cooldown("a", "kimi-k3") == pytest.approx(TRANSIENT_429_COOLDOWN, abs=1)


def test_header_quota_uses_same_ceiling_and_survives_headerless_usage(state):
    with_windows(state, "a")
    state.store.merge_metadata("a", {"quota": {
        "7d_utilization": ".9", "7d_reset_epoch": str(time.time() + 500)}})
    assert not state._quota_ok("a", "kimi-k3")
    state.record_usage("a", "kimi-k3", {"input_tokens": 1}, {})
    assert not state._quota_ok("a", "kimi-k3")


def test_reset_cache_is_candidate_not_permission_to_send(state):
    with_windows(state, "a", seven_day=1)
    cache = state._windows_envelope("a")
    cache["fetched_epoch"] = time.time() - 100
    cache["windows"][1]["reset_at"] = time.time() - 50
    state.store.merge_metadata("a", {"limits": cache})
    assert state._quota_ok("a", "gpt-5.6-terra")
    assert not state._limits_ready("a", "gpt-5.6-terra")


@pytest.mark.parametrize("window", WINDOWS)
def test_zero_budget_is_known_exhaustion(state, window):
    with_windows(state, "a")
    cache = state._windows_envelope("a")
    for item in cache["windows"]:
        if item["name"] == window:
            item.update(used=0, budget=0)
    state.store.merge_metadata("a", {"limits": cache})
    with pytest.raises(RelayError) as caught:
        route(state, model="claude-fable-5-1")
    assert caught.value.status == 429


def test_background_and_behavior_interfaces_are_removed(state):
    for name in ("behavior", "start_limits_refresh", "kick_limits_refresh",
                 "stop_limits_refresh", "refresh_all_limits", "_probe_parked"):
        assert not hasattr(state, name)
