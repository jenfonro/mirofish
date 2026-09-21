"""Fixed-exit safety across persistence, state execution and two-stage login."""

import datetime
import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from mirofish.errors import RelayError
from mirofish.proxy import DIRECT, ProxyPool, proxy_url
from mirofish.store import (HEALTH_ERROR, HEALTH_OK, HEALTH_PARKED_STATES,
                            HEALTH_SUSPENDED, Store)
from mirofish.vault import make_credential_store

NODE = {"scheme": "http", "host": "fixed.test", "port": 8080,
        "username": "user", "password": "secret"}


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("real network is forbidden in fixed-proxy tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", unexpected)


def save_account(state, binding=None, alias="acct"):
    state.store.save(alias, f"{alias}@example.com", "access", "refresh", {},
                     proxy_id=binding)


@pytest.mark.parametrize("populated", [False, True])
async def test_historical_null_never_selects_or_goes_direct(state, populated):
    if populated:
        state.pool.add(NODE)
    save_account(state)
    op = AsyncMock()

    with pytest.raises(RelayError) as raised:
        await state.with_proxy("acct", op)

    assert raised.value.status == 503
    op.assert_not_awaited()
    assert state.store.row("acct")["proxy_id"] is None
    assert state.store.proxy_assignment_counts() == {}


@pytest.mark.parametrize("proxy_id", [None, "", "missing", "DIRECT"])
def test_lookup_never_confuses_missing_id_with_direct(state, proxy_id):
    with pytest.raises(RelayError) as raised:
        state.pool.by_id(proxy_id)
    assert raised.value.status == 503
    assert state.pool.by_id(DIRECT) is None


@pytest.mark.parametrize("populated", [False, True])
async def test_only_explicit_direct_binding_is_direct(state, populated):
    if populated:
        state.pool.add(NODE)
    save_account(state, DIRECT)
    op = AsyncMock(return_value="ok")
    assert state.pool.for_account("acct") is None
    assert await state.with_proxy("acct", op) == "ok"
    op.assert_awaited_once_with(None)
    assert state.store.row("acct")["proxy_id"] == DIRECT


@pytest.mark.parametrize("damage", ["config_missing", "row_missing", "invalid", "incomplete",
                                   "legacy_mihomo"])
async def test_broken_binding_fails_before_dispatch_and_does_not_move(state, damage):
    node = state.pool.add(NODE)
    state.pool.add({**NODE, "host": "other.test"})
    save_account(state, node["id"])
    if damage == "config_missing":
        state.pool.configs.pop(node["id"])
    elif damage == "row_missing":
        state.store.db.execute("DELETE FROM proxies WHERE proxy_id=?", (node["id"],))
        state.store.db.commit()
    elif damage == "invalid":
        state.pool.configs[node["id"]]["scheme"] = "vmess"
    elif damage == "incomplete":
        state.pool.configs[node["id"]].pop("scheme")
    else:
        state.pool.configs[node["id"]]["mihomo_node"] = "old-selector-node"
    op = AsyncMock()
    with pytest.raises(RelayError) as raised:
        await state.with_proxy("acct", op)
    assert raised.value.status == 503
    op.assert_not_awaited()
    assert state.store.row("acct")["proxy_id"] == node["id"]


async def test_repeated_network_failure_retries_only_same_exit_on_later_requests(state):
    node = state.pool.add(NODE)
    state.pool.add({**NODE, "host": "other.test"})
    save_account(state, node["id"])
    error = RelayError("upstream network error", 502, {"proxy_network": True})
    op = AsyncMock(side_effect=error)
    for attempt in range(1, 5):
        with pytest.raises(RelayError) as raised:
            await state.with_proxy("acct", op)
        assert raised.value is error
        assert op.await_count == attempt
        assert op.await_args.args == (proxy_url(node),)
        assert state.store.row("acct")["proxy_id"] == node["id"]
    row = next(row for row in state.store.proxy_rows() if row["proxy_id"] == node["id"])
    assert row["active"] == 1 and row["failure_count"] == 4
    assert "health" not in json.loads(state.store.row("acct")["metadata_json"])
    recovered = AsyncMock(return_value="ok")
    assert await state.with_proxy("acct", recovered) == "ok"
    recovered.assert_awaited_once_with(proxy_url(node))


@pytest.mark.parametrize("error", [
    RelayError("region refused", 502, {"region_blocked": True}),
    RelayError("credit exhausted", 429, {"error": {"type": "credit_exhausted_shared"}}),
])
async def test_upstream_refusal_never_rotates_or_changes_global_proxy_status(state, error):
    node = state.pool.add(NODE)
    state.pool.add({**NODE, "host": "other.test"})
    save_account(state, node["id"])
    op = AsyncMock(side_effect=error)
    with pytest.raises(RelayError) as raised:
        await state.with_proxy("acct", op)
    assert raised.value is error
    op.assert_awaited_once_with(proxy_url(node))
    assert state.store.row("acct")["proxy_id"] == node["id"]
    assert all(row["failure_count"] == 0 for row in state.store.proxy_rows())


async def test_legacy_auto_disabled_node_can_retry_its_fixed_binding(state):
    node = state.pool.add(NODE)
    save_account(state, node["id"])
    state.store.db.execute(
        "UPDATE proxies SET active=0,failure_count=9,last_error='old threshold' WHERE proxy_id=?",
        (node["id"],))
    state.store.db.commit()
    op = AsyncMock(return_value="ok")
    assert await state.with_proxy("acct", op) == "ok"
    op.assert_awaited_once_with(proxy_url(node))
    assert state.store.row("acct")["proxy_id"] == node["id"]
    assert state.store.proxy_rows()[0]["last_error"] is None


def test_pending_login_resolves_only_explicit_choice_or_existing_binding(state):
    node = state.pool.add(NODE)
    with pytest.raises(RelayError) as raised:
        state.pool.pending_proxy("new")
    assert raised.value.status == 503
    assert state.pool.pending_proxy("new", node["id"]) == node
    assert state.pool.pending_proxy("new", DIRECT) is None
    assert state.store.aliases() == []
    save_account(state, node["id"])
    assert state.pool.pending_proxy("acct") == node
    state.store.set_account_proxy("acct", None)
    with pytest.raises(RelayError) as raised:
        state.pool.pending_proxy("acct")
    assert raised.value.status == 503
    assert state.store.row("acct")["proxy_id"] is None


async def test_empty_pending_login_id_fails_closed(client, state, auth_headers, monkeypatch):
    state.put_pending_login("new", "new@example.com", None)
    finish = AsyncMock()
    monkeypatch.setattr(state.accounts, "finish_login", finish)
    response = await client.post("/api/login/finish", headers=auth_headers,
                                 json={"alias": "new", "code": "123456"})
    assert response.status_code == 503
    finish.assert_not_awaited()
    assert state.store.aliases() == []


@pytest.mark.parametrize("kind", ["messages", "responses"])
async def test_stream_network_errors_are_single_attempt_and_keep_binding(state, monkeypatch, kind):
    node = state.pool.add(NODE)
    save_account(state, node["id"])
    op = AsyncMock(side_effect=RelayError("network", 502, {"proxy_network": True}))
    monkeypatch.setattr(state.upstream, "stream_" + kind, op)
    for attempt in (1, 2):
        with pytest.raises(RelayError) as raised:
            if kind == "messages":
                await state.open_messages_stream("acct", {"model": "test"})
            else:
                await state.open_responses_stream("acct", b'{"model":"test"}')
        assert raised.value.status == 502
        assert op.await_count == attempt
        assert op.await_args.args[2] == proxy_url(node)
        assert state.store.row("acct")["proxy_id"] == node["id"]


async def test_deleted_pending_snapshot_cannot_be_dispatched(state):
    node = state.pool.add(NODE)
    state.pool.remove(node["id"])
    op = AsyncMock()
    with pytest.raises(RelayError) as raised:
        await state.with_fixed_proxy("new", node, op)
    assert raised.value.status == 503
    op.assert_not_awaited()


async def test_two_stage_login_keeps_start_id_even_when_account_is_rebound(
        client, state, auth_headers, monkeypatch):
    first = state.pool.add(NODE)
    second = state.pool.add({**NODE, "host": "second.test"})
    save_account(state, first["id"])
    start = AsyncMock()
    monkeypatch.setattr(state.accounts, "start_login", start)
    result = await client.post("/api/login/start", headers=auth_headers,
                               json={"alias": "acct", "email": "acct@example.com",
                                     "proxy_id": first["id"]})
    assert result.status_code == 200
    start.assert_awaited_once_with("acct", "acct@example.com", proxy_url=proxy_url(first))
    state.store.set_account_proxy("acct", second["id"])

    async def save_login(alias, email, code, *, proxy_url, proxy_id):
        state.store.save(alias, email, "new-access", "new-refresh", {}, proxy_id=proxy_id)
        return {"alias": alias}

    finish = AsyncMock(side_effect=save_login)
    monkeypatch.setattr(state.accounts, "finish_login", finish)
    result = await client.post("/api/login/finish", headers=auth_headers,
                               json={"alias": "acct", "code": "123456", "proxy_id": second["id"]})
    assert result.status_code == 200
    finish.assert_awaited_once_with(
        "acct", "acct@example.com", "123456", proxy_url=proxy_url(first), proxy_id=first["id"])
    assert state.store.row("acct")["proxy_id"] == first["id"]


@pytest.mark.parametrize("readd", [False, True])
async def test_login_finish_missing_proxy_is_503_not_direct(
        client, state, auth_headers, monkeypatch, readd):
    node = state.pool.add(NODE)
    monkeypatch.setattr(state.accounts, "start_login", AsyncMock())
    started = await client.post("/api/login/start", headers=auth_headers,
                                json={"alias": "new", "email": "new@example.com",
                                      "proxy_id": node["id"]})
    assert started.status_code == 200
    state.pool.remove(node["id"])
    if readd:
        assert state.pool.add(NODE)["id"] != node["id"]
    finish = AsyncMock()
    monkeypatch.setattr(state.accounts, "finish_login", finish)
    response = await client.post("/api/login/finish", headers=auth_headers,
                                 json={"alias": "new", "code": "123456"})
    assert response.status_code == 503
    finish.assert_not_awaited()
    assert state.pending_logins["new"]["proxy_id"] == node["id"]
    assert state.store.aliases() == []


@pytest.mark.parametrize("choice", ["omitted", "", DIRECT])
async def test_new_login_without_node_is_persisted_as_explicit_direct(
        client, state, auth_headers, monkeypatch, choice):
    state.pool.add(NODE)
    start = AsyncMock()
    monkeypatch.setattr(state.accounts, "start_login", start)
    payload = {"alias": "new", "email": "new@example.com"}
    if choice != "omitted":
        payload["proxy_id"] = choice
    response = await client.post("/api/login/start", headers=auth_headers, json=payload)
    assert response.status_code == 200
    assert state.pending_logins["new"]["proxy_id"] == DIRECT
    start.assert_awaited_once_with("new", "new@example.com", proxy_url=None)

    async def save_login(alias, email, code, *, proxy_url, proxy_id):
        state.store.save(alias, email, "access", "refresh", {}, proxy_id=proxy_id)
        return {"alias": alias}

    finish = AsyncMock(side_effect=save_login)
    monkeypatch.setattr(state.accounts, "finish_login", finish)
    response = await client.post("/api/login/finish", headers=auth_headers,
                                 json={"alias": "new", "code": "123456"})
    assert response.status_code == 200
    finish.assert_awaited_once_with("new", "new@example.com", "123456",
                                   proxy_url=None, proxy_id=DIRECT)
    assert state.store.row("new")["proxy_id"] == DIRECT


async def test_login_network_failure_never_tries_second_node_or_changes_binding(
        client, state, auth_headers, monkeypatch):
    node = state.pool.add(NODE)
    state.pool.add({**NODE, "host": "other.test"})
    save_account(state, node["id"])
    start = AsyncMock(side_effect=RelayError("network", 502, {"proxy_network": True}))
    monkeypatch.setattr(state.accounts, "start_login", start)
    result = await client.post("/api/login/start", headers=auth_headers,
                               json={"alias": "acct", "email": "acct@example.com",
                                     "proxy_id": node["id"]})
    assert result.status_code == 502
    start.assert_awaited_once_with("acct", "acct@example.com", proxy_url=proxy_url(node))
    assert state.store.row("acct")["proxy_id"] == node["id"]
    assert "acct" not in state.pending_logins


async def test_admin_deleting_bound_node_is_409(client, state, auth_headers):
    node = state.pool.add(NODE)
    save_account(state, node["id"])
    response = await client.delete(f"/api/proxies/{node['id']}", headers=auth_headers)
    assert response.status_code == 409
    assert state.store.row("acct")["proxy_id"] == node["id"]
    assert state.pool.by_id(node["id"]) == node


async def test_direct_transport_ignores_legacy_tls_and_environment_proxy(state, monkeypatch):
    monkeypatch.setenv("MIROFISH_TLS_PROXY", "http://legacy.test:9000")
    monkeypatch.setenv("HTTPS_PROXY", "http://environment.test:9001")
    monkeypatch.setattr(state.settings, "tls_proxy", "http://legacy.test:9000", raising=False)
    client = MagicMock()
    client.aclose = AsyncMock()
    constructor = MagicMock(return_value=client)
    monkeypatch.setattr("mirofish.upstream.httpx.AsyncClient", constructor)
    assert await state.upstream.client(proxy_url(None), "acct") is client
    assert constructor.call_args.kwargs["proxy"] is None
    assert constructor.call_args.kwargs["trust_env"] is False


def test_existing_database_migration_preserves_null_direct_ids_and_health(settings):
    settings.data_dir.mkdir(parents=True)
    db = sqlite3.connect(settings.data_dir / "accounts.sqlite3")
    db.execute("""CREATE TABLE accounts (
        alias TEXT PRIMARY KEY, email TEXT NOT NULL, user_id TEXT, plan TEXT, tenant TEXT,
        proxy_id TEXT, metadata_json TEXT NOT NULL, created_at TEXT, updated_at TEXT)""")
    health = {"health": {"state": HEALTH_SUSPENDED, "status": 403, "kind": "suspended"}}
    for alias, binding in (("unbound", None), ("direct", DIRECT), ("fixed", "legacy-id")):
        db.execute("INSERT INTO accounts VALUES(?,?,?,?,?,?,?,?,?)",
                   (alias, f"{alias}@example.com", "u", "pro", "t", binding,
                    json.dumps(health), "2026-01-01", "2026-01-01"))
    db.commit()
    db.close()

    store = Store(settings.data_dir, make_credential_store(settings.data_dir, "file"))
    try:
        store.save_proxy_configs({"legacy-id": NODE})
        store.upsert_proxy("legacy-id", NODE, active=False)
        pool = ProxyPool(store, settings)
        assert store.row("unbound")["proxy_id"] is None
        with pytest.raises(RelayError) as raised:
            pool.for_account("unbound")
        assert raised.value.status == 503
        assert pool.for_account("direct") is None
        assert pool.for_account("fixed")["id"] == "legacy-id"
        assert pool.add(NODE)["id"] == "legacy-id"
        assert json.loads(store.row("fixed")["metadata_json"]) == health
        assert store.account_generation("fixed")
    finally:
        store.db.close()


def test_store_health_contract_and_save_do_not_rebind(state):
    save_account(state, DIRECT)
    health = state.store.mark_account_error(
        "acct", 503, "unavailable", "capacity", retry_after=1234.0)["health"]
    assert HEALTH_OK == "ok" and HEALTH_PARKED_STATES == (HEALTH_ERROR, HEALTH_SUSPENDED)
    assert health["state"] == HEALTH_ERROR and health["retry_at"] == 1234.0
    state.store.clear_account_error("acct")
    assert "health" not in json.loads(state.store.row("acct")["metadata_json"])
    state.store.save("acct", "acct@example.com", "new-access", "new-refresh", {})
    assert state.store.row("acct")["proxy_id"] == DIRECT


def test_usage_groups_long_context_variants_without_rewriting_canonical_ids(state):
    save_account(state, DIRECT)
    canonical = ["claude-fable-5", "claude-fable-5[1m]",
                 "claude-fable-5-1", "claude-fable-5-1[1m]",
                 "claude-opus-5[1m]"]
    for index, model in enumerate(canonical, 1):
        state.store.log_usage("acct", model, {
            "input_tokens": index, "output_tokens": index * 10,
            "cache_read_input_tokens": index * 100,
            "cache_creation_input_tokens": index * 1000,
        })
    totals = state.store.usage_by_model_since(
        "acct", 0, ["claude-fable-5", "claude-fable-5-1"])
    assert totals == {
        "claude-fable-5": {"requests": 2, "input_tokens": 3, "output_tokens": 30,
                           "cache_read_tokens": 300, "cache_write_tokens": 3000},
        "claude-fable-5-1": {"requests": 2, "input_tokens": 7, "output_tokens": 70,
                             "cache_read_tokens": 700, "cache_write_tokens": 7000},
    }
    assert [row[0] for row in state.store.db.execute(
        "SELECT model FROM usage_log ORDER BY id")] == canonical
    assert state.store.usage_summary()["totals"]["requests"] == 5


def test_usage_window_end_is_exclusive_and_generation_still_scopes_results(state):
    save_account(state, DIRECT)
    generation = state.store.account_generation("acct")
    since, until = 1000.0, 2000.0
    for stamp in (since - 1, since, until - 1, until, until + 1):
        for owner, model in ((generation, "claude-fable-5"),
                             (generation, "claude-fable-5[1m]"),
                             ("old-generation", "claude-fable-5[1m]")):
            iso = datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).isoformat()
            state.store.db.execute(
                "INSERT INTO usage_log(alias,model,account_generation,input_tokens,created_at) "
                "VALUES(?,?,?,?,?)", ("acct", model, owner, 1, iso))
    state.store.db.commit()
    models = ["claude-fable-5"]
    bounded = state.store.usage_by_model_since("acct", since, models, until_epoch=until)
    assert bounded["claude-fable-5"]["requests"] == 4
    assert bounded["claude-fable-5"]["input_tokens"] == 4
    assert state.store.usage_by_model_since("acct", since, models)[
        "claude-fable-5"]["requests"] == 8
    historical = state.store.usage_by_model_since(
        "acct", since, models, "old-generation", until_epoch=until)
    assert historical["claude-fable-5"]["requests"] == 2
    assert state.store.usage_by_model_since("acct", until, models, until_epoch=until) == {}


@pytest.mark.parametrize("end", [float("nan"), float("inf"), "invalid"])
def test_usage_invalid_window_end_is_rejected(state, end):
    save_account(state, DIRECT)
    with pytest.raises(RelayError) as raised:
        state.store.usage_by_model_since("acct", 0, ["claude-fable-5"], until_epoch=end)
    assert raised.value.status == 400
