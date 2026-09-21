"""Demand-only quota/capability preflight and fixed-exit execution."""

import asyncio
import time
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest

from mirofish.api.state import ACCOUNT_GENERATION_EXTENSION
from mirofish.errors import RelayError
from mirofish.proxy import proxy_url
from mirofish.upstream import RESPONSES_COMPACT_PATH
from tests.conftest import add_account

MODEL = "claude-fable-5-1"


@pytest.fixture(autouse=True)
def model_capability(state, monkeypatch):
    async def resolve(alias, requested, proxy_url=None):
        return requested
    monkeypatch.setattr(state.accounts, "resolve_model", resolve)


def payload():
    return {"model": MODEL, "messages": [{"role": "user", "content": "test"}]}


def limits(used=10):
    return {"fetched_epoch": time.time(), "windows": [
        {"name": name, "used": used, "budget": 100, "reset_at": time.time() + 7200}
        for name in ("5h", "7d", "7d_claude", "7d_fable")]}


def missing(state, alias):
    state.store.merge_metadata(alias, {"limits": None, "limits_error": None,
                                       "limits_attempt_epoch": None})


async def test_missing_data_fetches_only_elected_account_before_work(state, monkeypatch):
    for alias in ("a", "b", "c"):
        add_account(state, alias)
        missing(state, alias)
    events = []
    async def read(alias, proxy_url=None):
        events.append(("limits", alias, proxy_url))
        return 200, {}, limits()
    async def run(alias):
        events.append(("work", alias))
        return "ok"
    monkeypatch.setattr(state.upstream, "limits", read)
    assert await state.with_account_failover("", "chat", payload(), run) == ("a", "ok")
    assert events == [("limits", "a", None), ("work", "a")]


async def test_every_dispatch_calls_service_but_fresh_cache_avoids_network(state, monkeypatch):
    add_account(state, "a")
    fetch = AsyncMock(wraps=state.accounts.fetch_limits)
    monkeypatch.setattr(state.accounts, "fetch_limits", fetch)
    network = AsyncMock(side_effect=AssertionError("fresh cache hit network"))
    monkeypatch.setattr(state.upstream, "limits", network)
    for _ in range(3):
        await state.with_account_failover("", "chat", payload(), AsyncMock(return_value="ok"))
    assert fetch.await_count == 3
    assert all(call.kwargs == {"proxy_url": None, "force": False} for call in fetch.await_args_list)
    network.assert_not_awaited()


@pytest.mark.parametrize("which", ["missing", "expired"])
async def test_failed_read_never_blind_sends_and_failure_cache_is_honored(state, monkeypatch, which):
    add_account(state, "a")
    if which == "missing":
        missing(state, "a")
    else:
        cache = limits()
        cache["fetched_epoch"] -= state.settings.limits_ttl + 1
        state.store.merge_metadata("a", {"limits": cache})
    read = AsyncMock(side_effect=RelayError("network down", 502, {"proxy_network": True}))
    monkeypatch.setattr(state.upstream, "limits", read)
    run = AsyncMock()
    for _ in range(2):
        with pytest.raises(RelayError) as caught:
            await state.with_account_failover("", "chat", payload(), run)
        assert caught.value.status == 502
    assert read.await_count == 1
    run.assert_not_awaited()


async def test_failed_preflight_moves_to_separately_checked_account(state, monkeypatch):
    for alias in ("a", "b"):
        add_account(state, alias)
        missing(state, alias)
    reads = []
    async def read(alias, proxy_url=None):
        reads.append(alias)
        if alias == "a":
            raise RelayError("auth", 401)
        return 200, {}, limits()
    monkeypatch.setattr(state.upstream, "limits", read)
    run = AsyncMock(return_value="ok")
    assert await state.with_account_failover("", "chat", payload(), run) == ("b", "ok")
    assert reads == ["a", "b"]
    assert state.account_health("a")["status"] == 401
    run.assert_awaited_once_with("b")


@pytest.mark.parametrize("requested", ["", "a"])
async def test_new_data_at_ceiling_prevents_generation(state, monkeypatch, requested):
    add_account(state, "a")
    missing(state, "a")
    read = AsyncMock(return_value=(200, {}, limits(90)))
    monkeypatch.setattr(state.upstream, "limits", read)
    run = AsyncMock()
    with pytest.raises(RelayError) as caught:
        await state.with_account_failover(requested, "chat", payload(), run)
    assert caught.value.status == 429
    assert read.await_count == 1
    run.assert_not_awaited()


@pytest.mark.parametrize("requested", ["", "a"])
async def test_known_full_pool_causes_no_upstream_at_all(state, monkeypatch, requested):
    add_account(state, "a")
    state.store.merge_metadata("a", {"limits": limits(90)})
    state.default_account = "a"
    fetch, run = AsyncMock(), AsyncMock()
    monkeypatch.setattr(state.accounts, "fetch_limits", fetch)
    with pytest.raises(RelayError) as caught:
        await state.with_account_failover(requested, "", payload(), run)
    assert caught.value.status == 429
    fetch.assert_not_awaited()
    run.assert_not_awaited()


async def test_failover_never_uses_over_ceiling_last_resort(state, monkeypatch):
    add_account(state, "a")
    add_account(state, "b")
    state.store.merge_metadata("b", {"limits": limits(95)})
    fetch = AsyncMock(wraps=state.accounts.fetch_limits)
    monkeypatch.setattr(state.accounts, "fetch_limits", fetch)
    run = AsyncMock(side_effect=RelayError("429", 429))
    with pytest.raises(RelayError) as caught:
        await state.with_account_failover("", "chat", payload(), run)
    assert caught.value.status == 429
    run.assert_awaited_once_with("a")
    assert [call.kwargs["force"] for call in fetch.await_args_list] == [False, True]


@pytest.mark.parametrize("body", [None, "plain", {"_raw": "HTML"}, {"error": "slow"},
    {"error": {"type": "shared_quota_unavailable"}},
    {"error": {"type": "credit_exhausted_shared"}},
    {"error": {"type": "rate_limit_error", "code": "credit_exhausted_7d_fable"}}])
async def test_all_model_429_force_once_then_failover_same_session(state, monkeypatch, body):
    for alias in ("a", "b"):
        add_account(state, alias)
    events = []
    async def fetch(alias, proxy_url=None, *, force=False):
        events.append(("limits", alias, force))
        return state._windows_envelope(alias)
    async def run(alias):
        events.append(("work", alias))
        if alias == "a":
            raise RelayError("429", 429, body)
        return "ok"
    monkeypatch.setattr(state.accounts, "fetch_limits", fetch)
    assert await state.with_account_failover("", "chat", payload(), run) == ("b", "ok")
    assert events == [("limits", "a", False), ("work", "a"), ("limits", "a", True),
                      ("limits", "b", False), ("work", "b")]
    assert state.route_account("", "chat", payload()) == "b"


async def test_preflight_429_records_window_without_force_same_endpoint(state, monkeypatch):
    add_account(state, "a")
    fetch = AsyncMock(side_effect=RelayError("quota", 429, {"error": {
        "type": "rate_limit_error", "code": "credit_exhausted_7d_fable"}}))
    monkeypatch.setattr(state.accounts, "fetch_limits", fetch)
    run = AsyncMock()
    with pytest.raises(RelayError):
        await state.with_account_failover("", "chat", payload(), run)
    fetch.assert_awaited_once_with("a", proxy_url=None, force=False)
    run.assert_not_awaited()
    assert state.exhausted_cooldown("a", MODEL) > 0
    assert state.exhausted_cooldown("a", "claude-opus-5") == 0


async def test_failed_force_records_health_never_retries_same_account(state, monkeypatch):
    for alias in ("a", "b"):
        add_account(state, alias)
    async def fetch(alias, proxy_url=None, *, force=False):
        if alias == "a" and force:
            raise RelayError("auth", 401)
        return state._windows_envelope(alias)
    attempts = []
    async def run(alias):
        attempts.append(alias)
        if alias == "a":
            raise RelayError("429", 429)
        return "ok"
    monkeypatch.setattr(state.accounts, "fetch_limits", fetch)
    assert await state.with_account_failover("", "chat", payload(), run) == ("b", "ok")
    assert attempts == ["a", "b"]
    assert state.account_health("a")["status"] == 401


async def test_explicit_429_forces_without_substitution(state, monkeypatch):
    for alias in ("a", "b"):
        add_account(state, alias)
    fetch = AsyncMock(wraps=state.accounts.fetch_limits)
    monkeypatch.setattr(state.accounts, "fetch_limits", fetch)
    run = AsyncMock(side_effect=RelayError("429", 429))
    with pytest.raises(RelayError):
        await state.with_account_failover("a", "manual", payload(), run)
    run.assert_awaited_once_with("a")
    assert [call.kwargs["force"] for call in fetch.await_args_list] == [False, True]


async def test_concurrent_429_uses_account_service_singleflight(state, monkeypatch):
    add_account(state, "a")
    cache = limits()
    cache["fetched_epoch"] -= 10
    state.store.merge_metadata("a", {"limits": cache})
    entered = 0
    ready = asyncio.Event()
    reads = []
    async def read(alias, proxy_url=None):
        reads.append(alias)
        await asyncio.sleep(.02)
        return 200, {}, limits(100)
    async def run(alias):
        nonlocal entered
        entered += 1
        if entered == 8:
            ready.set()
        await ready.wait()
        raise RelayError("429", 429)
    monkeypatch.setattr(state.upstream, "limits", read)
    results = await asyncio.gather(*(state.with_account_failover(
        "a", f"chat-{i}", payload(), run) for i in range(8)), return_exceptions=True)
    assert all(isinstance(exc, RelayError) and exc.status == 429 for exc in results)
    assert reads == ["a"]
    assert entered == 8


async def test_disabling_during_refresh_prevents_dispatch(state, monkeypatch):
    add_account(state, "a")
    async def fetch(alias, proxy_url=None, *, force=False):
        state.store.merge_metadata(alias, {"disabled": True})
        return state._windows_envelope(alias)
    monkeypatch.setattr(state.accounts, "fetch_limits", fetch)
    run = AsyncMock()
    with pytest.raises(RelayError):
        await state.with_account_failover("", "chat", payload(), run)
    run.assert_not_awaited()


@pytest.mark.parametrize("windows", [None, [], [{"name": "7d", "used": 1, "budget": 100}]])
async def test_incomplete_successful_limits_still_cannot_authorize_work(state, monkeypatch, windows):
    add_account(state, "a")
    cache = {"fetched_epoch": time.time(), "windows": windows}
    state.store.merge_metadata("a", {"limits": cache})
    monkeypatch.setattr(state.accounts, "fetch_limits", AsyncMock(return_value=cache))
    run = AsyncMock()
    with pytest.raises(RelayError) as caught:
        await state.with_account_failover("", "chat", payload(), run)
    assert caught.value.status == 503
    run.assert_not_awaited()


async def test_unmetered_successful_limits_authorize_work(state):
    add_account(state, "a")
    state.store.merge_metadata("a", {"limits": {
        "fetched_epoch": time.time(), "unmetered": True, "windows": []}})
    assert await state.with_account_failover("", "chat", payload(), AsyncMock(return_value="ok")) == ("a", "ok")


async def test_capability_follows_limits_and_canonical_reaches_dispatch(state, monkeypatch):
    add_account(state, "a")
    events = []
    async def fetch(alias, proxy_url=None, *, force=False):
        events.append("limits")
        return state._windows_envelope(alias)
    async def resolve(alias, requested, proxy_url=None):
        events.append(("roster", alias, requested, proxy_url))
        return requested + "[1m]"
    body = payload()
    async def run(alias):
        events.append(("work", body["model"]))
    monkeypatch.setattr(state.accounts, "fetch_limits", fetch)
    monkeypatch.setattr(state.accounts, "resolve_model", resolve)
    await state.with_account_failover("", "chat", body, run)
    assert events == ["limits", ("roster", "a", MODEL, None), ("work", MODEL + "[1m]")]


async def test_unsupported_capability_fails_over_without_health_error(state, monkeypatch):
    for alias in ("a", "b"):
        add_account(state, alias)
    async def resolve(alias, requested, proxy_url=None):
        if alias == "a":
            raise RelayError("not advertised", 400, {"kind": "model_not_supported"})
        return requested
    monkeypatch.setattr(state.accounts, "resolve_model", resolve)
    body = payload()
    body["model"] += "[1m]"
    run = AsyncMock(return_value="ok")
    assert await state.with_account_failover("", "chat", body, run) == ("b", "ok")
    run.assert_awaited_once_with("b")
    assert state.account_health("a") == {} and state.exhausted_cooldown("a") == 0
    assert state.route_account("", "chat", body) == "b"


async def test_all_unsupported_returns_local_400_no_generation(state, monkeypatch):
    for alias in ("a", "b"):
        add_account(state, alias)
    monkeypatch.setattr(state.accounts, "resolve_model", AsyncMock(side_effect=RelayError(
        "unsupported", 400, {"kind": "model_not_supported"})))
    run = AsyncMock()
    with pytest.raises(RelayError) as caught:
        await state.with_account_failover("", "chat", payload(), run)
    assert caught.value.status == 400 and caught.value.data["kind"] == "model_not_supported"
    run.assert_not_awaited()


async def test_failover_resolves_original_not_previous_canonical(state, monkeypatch):
    for alias in ("a", "b"):
        add_account(state, alias)
    models = []
    async def resolve(alias, requested, proxy_url=None):
        models.append(requested)
        return requested + "[1m]" if alias == "a" else requested
    body = payload()
    async def run(alias):
        if alias == "a":
            assert body["model"] == MODEL + "[1m]"
            raise RelayError("429", 429)
        assert body["model"] == MODEL
        return "ok"
    monkeypatch.setattr(state.accounts, "resolve_model", resolve)
    assert await state.with_account_failover("", "chat", body, run) == ("b", "ok")
    assert models == [MODEL, MODEL]


async def test_fixed_exit_used_for_limits_roster_and_generation(state, monkeypatch):
    add_account(state, "a")
    node = state.pool.add({"name": "fixed", "scheme": "http", "host": "proxy.test", "port": 8080})
    state.store.set_account_proxy("a", node["id"])
    urls = []
    async def fetch(alias, proxy_url=None, *, force=False):
        urls.append(proxy_url)
        return state._windows_envelope(alias)
    async def resolve(alias, requested, proxy_url=None):
        urls.append(proxy_url)
        return requested
    async def send(url):
        urls.append(url)
        return "ok"
    async def run(alias):
        return await state.with_proxy(alias, send)
    monkeypatch.setattr(state.accounts, "fetch_limits", fetch)
    monkeypatch.setattr(state.accounts, "resolve_model", resolve)
    assert await state.with_account_failover("", "chat", payload(), run) == ("a", "ok")
    assert urls == [proxy_url(node)] * 3
    assert state.store.row("a")["proxy_id"] == node["id"]


async def test_proxy_failure_does_not_rotate_or_change_binding(state):
    add_account(state, "a")
    node = state.pool.add({"name": "fixed", "scheme": "http", "host": "proxy.test", "port": 8080})
    state.pool.add({"name": "other", "scheme": "http", "host": "other.test", "port": 8080})
    state.store.set_account_proxy("a", node["id"])
    send = AsyncMock(side_effect=RelayError("network", 502, {"proxy_network": True}))
    with pytest.raises(RelayError):
        await state.with_proxy("a", send)
    send.assert_awaited_once_with(proxy_url(node))
    assert state.store.row("a")["proxy_id"] == node["id"]


async def test_unbound_is_not_direct_but_explicit_none_is_direct(state):
    add_account(state, "a")
    state.store.set_account_proxy("a", None)
    send = AsyncMock(return_value="ok")
    with pytest.raises(RelayError) as caught:
        await state.with_proxy("a", send)
    assert caught.value.status == 503
    send.assert_not_awaited()
    assert await state.with_fixed_proxy("a", None, send) == "ok"
    send.assert_awaited_once_with(None)


async def test_compact_path_and_generation_are_preserved(state, monkeypatch):
    add_account(state, "a")
    response = httpx.Response(200, json={"output": []})
    stream = AsyncMock(return_value=response)
    monkeypatch.setattr(state.upstream, "stream_responses", stream)
    received, stack = await state.open_responses_stream(
        "a", b"{}", path=RESPONSES_COMPACT_PATH, query_string="x=1", session_id="isolated")
    assert received is response
    assert stream.await_args.kwargs["path"] == RESPONSES_COMPACT_PATH
    assert stream.await_args.kwargs["query_string"] == "x=1"
    assert response.extensions[ACCOUNT_GENERATION_EXTENSION] == state.store.account_generation("a")
    await stack.aclose()
    assert response.is_closed


@pytest.mark.parametrize("body", [b"plain 429", b'{"error":{"type":"rate_limit_error"}}'])
async def test_responses_429_raises_for_failover_and_closes_response(state, monkeypatch, body):
    add_account(state, "a")
    response = httpx.Response(429, content=body)
    monkeypatch.setattr(state.upstream, "stream_responses", AsyncMock(return_value=response))
    with pytest.raises(RelayError) as caught:
        await state.open_responses_stream("a", b"{}")
    assert caught.value.status == 429 and response.is_closed


def test_uuid_caller_is_always_hashed_per_account(state):
    caller = "b9710b83-a8e2-4999-9527-61fe459605b1"
    a = state.relay_session_id(caller, "", {}, account="a")
    b = state.relay_session_id(caller, "", {}, account="b")
    assert a != caller and b != caller and a != b
    assert state.relay_session_id(caller.upper(), "", {}, account="a") == a
    assert uuid.UUID(a).version == 4


def test_catalog_ignores_quota_but_not_health_or_disabled(state):
    add_account(state, "a")
    state.store.merge_metadata("a", {"limits": limits(100)})
    assert state.pick_catalog_account("") == "a"
    assert state.pick_catalog_account("a") == "a"
    state.store.merge_metadata("a", {"disabled": True})
    with pytest.raises(RelayError):
        state.pick_catalog_account("a")
