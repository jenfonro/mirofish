"""Request-path quota gate: an account at/over the ceiling on any window the
requested model draws on is never handed a live request. When the whole
eligible pool is spent the relay answers a local 429 instead of forwarding
(which only earns upstream refusals and, repeated, a suspension).

Covers the regression where scheduling weighed only 7d/7d_fable and ignored
5h and 7d_claude, so an account at 104% on 7d_claude but 25% on 7d kept being
forwarded requests.
"""
import time

import pytest

from mirofish.errors import RelayError
from tests.conftest import add_account


def _set_windows(state, alias, **pct):
    """Attach cached /v1/limits windows at the given utilisation percentages."""
    reset = time.time() + 7200
    windows = [{"name": name, "used": frac, "budget": 1.0, "reset_at": reset}
               for name, frac in pct.items()]
    state.store.merge_metadata(alias, {"limits": {"windows": windows,
                                                  "fetched_epoch": time.time()}})


def _conv(text):
    return {"messages": [{"role": "user", "content": text}]}


def test_claude_blocked_when_only_7d_claude_is_spent(state):
    add_account(state, "a")
    # 7d looks open (25%) but the Claude weekly window is over the 90% ceiling.
    _set_windows(state, "a", **{"5h": 0.0, "7d": 0.25, "7d_claude": 1.04, "7d_fable": 0.4})
    with pytest.raises(RelayError) as exc:
        state.route_account("", "", {"model": "claude-opus-5", **_conv("hi")})
    assert exc.value.status == 429
    assert exc.value.data["error"]["code"] == "credit_exhausted_7d_claude"


def test_burst_window_blocks_even_with_weekly_headroom(state):
    add_account(state, "a")
    _set_windows(state, "a", **{"5h": 0.95, "7d": 0.1, "7d_claude": 0.1})
    with pytest.raises(RelayError) as exc:
        state.route_account("", "", {"model": "claude-opus-5", **_conv("hi")})
    assert exc.value.status == 429
    assert exc.value.data["error"]["code"] == "credit_exhausted_5h"


def test_non_claude_model_ignores_claude_window(state):
    add_account(state, "a")
    # 7d_claude is maxed, but a gpt-* request does not draw on it.
    _set_windows(state, "a", **{"5h": 0.1, "7d": 0.2, "7d_claude": 1.2})
    assert state.route_account("", "", {"model": "gpt-5.6-luna", **_conv("hi")}) == "a"


def test_pool_exhausted_answers_local_429_not_forwarded(state):
    add_account(state, "a")
    add_account(state, "b")
    for alias in ("a", "b"):
        _set_windows(state, alias, **{"5h": 0.0, "7d": 0.99, "7d_claude": 0.99})
    with pytest.raises(RelayError) as exc:
        state.route_account("", "", {"model": "claude-opus-5", **_conv("hi")})
    assert exc.value.status == 429
    assert exc.value.data["error"]["type"] == "rate_limit_error"


def test_explicit_over_ceiling_account_is_refused_not_forwarded(state):
    add_account(state, "a")
    _set_windows(state, "a", **{"5h": 0.0, "7d": 0.99, "7d_claude": 0.99})
    with pytest.raises(RelayError) as exc:
        state.route_account("a", "", {"model": "claude-opus-5", **_conv("hi")})
    assert exc.value.status == 429


def test_account_under_ceiling_is_still_served(state):
    add_account(state, "a")
    _set_windows(state, "a", **{"5h": 0.5, "7d": 0.5, "7d_claude": 0.5, "7d_fable": 0.5})
    assert state.route_account("", "", {"model": "claude-fable-5", **_conv("hi")}) == "a"


def test_missing_windows_assume_room(state):
    add_account(state, "a")
    # No cached limits at all: assume room, let the upstream 429 be the authority.
    assert state.route_account("", "", {"model": "claude-opus-5", **_conv("hi")}) == "a"
