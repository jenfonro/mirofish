"""Per-model breakdown of the shared 7d_fable window.

The upstream meters claude-fable-5 and claude-fable-5-1 against ONE window and
reports a single number, so the panel cannot tell which model consumed it. The
split is computed locally from the usage log, scoped to the window's own period
so it resets exactly when the window does.
"""

import datetime
import json
import time

import pytest

from mirofish.accounts import FABLE_MODELS, FABLE_WINDOW

from tests.conftest import add_account

WEEK = 604800.0


def _iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


def _log(store, alias, model, *, created_at, inp=0, out=0,
         cache_read=0, cache_write=0):
    """Insert a usage row with an explicit timestamp."""
    store.db.execute(
        """INSERT INTO usage_log(alias,model,input_tokens,output_tokens,
                                 cache_read_tokens,cache_write_tokens,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (alias, model, inp, out, cache_read, cache_write, created_at))
    store.db.commit()


def _limits(reset_at, used=6101.0):
    return {"windows": [
        {"name": "7d", "label": "7 天窗口", "length": WEEK,
         "used": 40000.0, "budget": 140000.0, "reset_at": reset_at},
        {"name": FABLE_WINDOW, "label": "7 天 Fable 窗口", "length": WEEK,
         "used": used, "budget": 74200.0, "reset_at": reset_at},
    ]}


def _window(limits):
    return next(w for w in limits["windows"] if w["name"] == FABLE_WINDOW)


def _split(limits):
    return {entry["model"]: entry for entry in _window(limits)["models"]}


def test_split_reports_each_fable_model_separately(state):
    add_account(state, "work")
    reset_at = time.time() + WEEK / 2          # halfway through the window
    started = reset_at - WEEK
    _log(state.store, "work", "claude-fable-5", created_at=_iso(started + 60),
         inp=100, out=200, cache_read=1000, cache_write=50)
    _log(state.store, "work", "claude-fable-5", created_at=_iso(started + 120),
         inp=10, out=20)
    _log(state.store, "work", "claude-fable-5-1", created_at=_iso(started + 180),
         inp=26, out=7750, cache_read=6640519, cache_write=657960)

    limits = _limits(reset_at)
    state.accounts._attach_fable_split("work", limits)
    split = _split(limits)

    assert split["claude-fable-5"]["requests"] == 2
    assert split["claude-fable-5"]["input_tokens"] == 110
    assert split["claude-fable-5"]["output_tokens"] == 220
    assert split["claude-fable-5"]["total_tokens"] == 110 + 220 + 1000 + 50
    assert split["claude-fable-5-1"]["requests"] == 1
    assert split["claude-fable-5-1"]["total_tokens"] == 26 + 7750 + 6640519 + 657960


def test_split_resets_with_the_window(state):
    """Usage from the previous window must not carry into the new one.

    This is the point of scoping to `reset_at - length`: a reset needs no
    deletion or zeroing, the old rows simply fall outside the range.
    """
    add_account(state, "work")
    reset_at = time.time() + WEEK / 2
    started = reset_at - WEEK
    # Heavy spend, but one minute BEFORE this window opened.
    _log(state.store, "work", "claude-fable-5", created_at=_iso(started - 60),
         inp=500_000, out=500_000)
    # A single small request inside the window.
    _log(state.store, "work", "claude-fable-5-1", created_at=_iso(started + 60),
         inp=1, out=2)

    limits = _limits(reset_at)
    state.accounts._attach_fable_split("work", limits)
    split = _split(limits)

    assert split["claude-fable-5"] == {
        "model": "claude-fable-5", "requests": 0, "input_tokens": 0,
        "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "total_tokens": 0,
    }
    assert split["claude-fable-5-1"]["requests"] == 1
    assert split["claude-fable-5-1"]["total_tokens"] == 3


def test_split_lists_every_fable_model_even_with_no_usage(state):
    """A model with no spend still needs a row: otherwise the panel shows
    nothing for it, which reads as missing data rather than zero."""
    add_account(state, "work")
    limits = _limits(time.time() + WEEK / 2)
    state.accounts._attach_fable_split("work", limits)

    assert [entry["model"] for entry in _window(limits)["models"]] == FABLE_MODELS
    assert all(entry["total_tokens"] == 0 for entry in _window(limits)["models"])


def test_split_counts_only_the_requested_account(state):
    add_account(state, "work")
    add_account(state, "other")
    reset_at = time.time() + WEEK / 2
    _log(state.store, "other", "claude-fable-5",
         created_at=_iso(reset_at - WEEK + 60), inp=999, out=999)

    limits = _limits(reset_at)
    state.accounts._attach_fable_split("work", limits)

    assert all(entry["total_tokens"] == 0 for entry in _window(limits)["models"])


def test_split_ignores_non_fable_models(state):
    """Only the models the upstream meters against this window may count."""
    add_account(state, "work")
    reset_at = time.time() + WEEK / 2
    _log(state.store, "work", "claude-opus-5",
         created_at=_iso(reset_at - WEEK + 60), inp=1000, out=2000)

    limits = _limits(reset_at)
    state.accounts._attach_fable_split("work", limits)

    assert all(entry["total_tokens"] == 0 for entry in _window(limits)["models"])


@pytest.mark.parametrize("window", [
    {"name": FABLE_WINDOW, "label": "x", "length": WEEK, "used": 1.0,
     "budget": 74200.0, "reset_at": None},
    {"name": FABLE_WINDOW, "label": "x", "length": None, "used": 1.0,
     "budget": 74200.0, "reset_at": 1788000000.0},
])
def test_split_is_skipped_when_the_window_period_is_unknown(state, window):
    """Without reset_at and length there is no period to scope to.

    Falling back to an all-time count would report a figure spanning several
    windows, which is worse than reporting none.
    """
    add_account(state, "work")
    limits = {"windows": [dict(window)]}
    state.accounts._attach_fable_split("work", limits)

    assert "models" not in limits["windows"][0]


def test_split_is_absent_when_the_account_has_no_fable_window(state):
    add_account(state, "work")
    limits = {"windows": [{"name": "7d", "label": "7 天窗口", "length": WEEK,
                           "used": 1.0, "budget": 2.0, "reset_at": time.time()}]}
    state.accounts._attach_fable_split("work", limits)

    assert "models" not in limits["windows"][0]


async def test_fetch_limits_attaches_and_caches_the_split(state, monkeypatch):
    """The split must survive into cached metadata: the accounts list and the
    limits card both read the windows from there."""
    add_account(state, "work")
    reset_at = time.time() + WEEK / 2
    _log(state.store, "work", "claude-fable-5-1",
         created_at=_iso(reset_at - WEEK + 60), inp=5, out=7)

    async def fake_limits(alias, proxy_url=None):
        return 200, {}, {"windows": [
            {"name": FABLE_WINDOW, "used": 6101.0, "budget": 74200.0,
             "reset_at": reset_at},
        ]}

    monkeypatch.setattr(state.upstream, "limits", fake_limits)
    limits = await state.accounts.fetch_limits("work")

    assert _split(limits)["claude-fable-5-1"]["total_tokens"] == 12
    cached = json.loads(state.store.row("work")["metadata_json"])["limits"]
    assert _split(cached)["claude-fable-5-1"]["total_tokens"] == 12
