"""Sticky-pool recovery when the Mihomo provider renames its nodes."""

import json

import httpx
import pytest
import respx

from mirofish.api.state import AppState
from mirofish.config import Settings
from mirofish.errors import RelayError

from tests.conftest import AUTH_BASE, RELAY_BASE, add_account

CTRL = "http://ctrl.test"


@pytest.fixture
def mihomo_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MIROFISH_MASTER_KEY", "unit-test-master-key")
    monkeypatch.delenv("MIROFISH_PROXY_SUBSCRIPTION_URL", raising=False)
    monkeypatch.delenv("MIROFISH_PROXY_SUBSCRIPTION_URL_FILE", raising=False)
    return Settings(auth_base=AUTH_BASE, relay_base=RELAY_BASE,
                    data_dir=tmp_path / "data", cred_backend="file", timeout=5.0,
                    mihomo_controller=CTRL, mihomo_proxy="http://mihomo:7890",
                    mihomo_slots=2)


@pytest.fixture
async def mihomo_state(mihomo_settings):
    app_state = AppState(mihomo_settings)
    yield app_state
    await app_state.aclose()


@respx.mock
async def test_provider_rename_resyncs_and_rotates(mihomo_state):
    state = mihomo_state
    add_account(state, "acct")

    # The provider currently serves node-new; the pool still believes in
    # node-old (stored below), as after a provider auto-update.
    respx.get(f"{CTRL}/proxies/MirofishSlot0").mock(
        return_value=httpx.Response(200, json={"all": ["node-new"], "now": "node-new"}))
    respx.get(f"{CTRL}/proxies/MirofishPool").mock(
        return_value=httpx.Response(200, json={"all": ["node-new"], "now": "node-new"}))

    def selector_put(request):
        name = json.loads(request.content).get("name")
        if name == "node-new":
            return httpx.Response(204)
        return httpx.Response(400, json={"message": "proxy not exist"})

    respx.put(f"{CTRL}/proxies/MirofishSlot0").mock(side_effect=selector_put)
    respx.put(f"{CTRL}/proxies/MirofishSlot1").mock(side_effect=selector_put)

    # Seed the store with the stale node set and pin the account to it.
    stale = state.pool._configs_from_names(["node-old"])
    state.pool._store_nodes(stale, skipped=0)
    stale_id = next(iter(stale))
    state.store.set_account_proxy("acct", stale_id)

    used = []

    async def op(proxy_url):
        used.append(proxy_url)
        return "ok"

    # First attempt PUTs node-old -> Mihomo 400 -> resync + rotate -> node-new.
    result = await state.with_proxy("acct", op)
    assert result == "ok"
    assert used and used[-1].startswith("http://mihomo:789")

    row = state.store.row("acct")
    assert str(row["proxy_id"]) != stale_id
    active = {str(r["proxy_id"]) for r in state.store.proxy_rows(active_only=True)}
    assert str(row["proxy_id"]) in active


@respx.mock
async def test_region_blocked_node_is_rotated_away(mihomo_state):
    """A 429 region refusal must retire the exit, not dead-end the account."""
    state = mihomo_state
    add_account(state, "acct")

    respx.get(f"{CTRL}/proxies/MirofishSlot0").mock(
        return_value=httpx.Response(200, json={"all": ["node-a", "node-b"],
                                               "now": "node-a"}))
    respx.get(f"{CTRL}/proxies/MirofishPool").mock(
        return_value=httpx.Response(200, json={"all": ["node-a", "node-b"],
                                               "now": "node-a"}))
    respx.put(url__regex=rf"{CTRL}/proxies/MirofishSlot\d+").mock(
        return_value=httpx.Response(204))

    configs = state.pool._configs_from_names(["node-a", "node-b"])
    state.pool._store_nodes(configs, skipped=0)
    blocked_id = str(state.store.row("acct")["proxy_id"] or "")
    if not blocked_id:
        blocked_id = next(iter(configs))
        state.store.set_account_proxy("acct", blocked_id)

    region_error = RelayError(
        "upstream does not serve this proxy exit region", 502,
        {"region_blocked": True, "upstream": "shared_quota_unavailable: ..."})

    attempts = []

    async def op(proxy_url):
        attempts.append(proxy_url)
        # Only the first exit is region-blocked; the rotation target works.
        if len(attempts) == 1:
            raise region_error
        return "ok"

    assert await state.with_proxy("acct", op) == "ok"
    assert len(attempts) == 2

    # The account moved off the blocked node, and the node carries the failure.
    assert str(state.store.row("acct")["proxy_id"]) != blocked_id
    failed = next(r for r in state.store.proxy_rows()
                  if str(r["proxy_id"]) == blocked_id)
    assert int(failed["failure_count"]) >= 1
    assert "region" in str(failed["last_error"])


@respx.mock
async def test_region_rotation_walks_past_four_nodes(mihomo_state):
    """The old four-attempt cap could miss a served exit later in the pool."""
    state = mihomo_state
    add_account(state, "acct")
    names = [f"node-{index}" for index in range(5)]

    respx.get(f"{CTRL}/proxies/MirofishSlot0").mock(
        return_value=httpx.Response(200, json={"all": names, "now": names[0]}))
    respx.get(f"{CTRL}/proxies/MirofishPool").mock(
        return_value=httpx.Response(200, json={"all": names, "now": names[0]}))
    respx.put(url__regex=rf"{CTRL}/proxies/MirofishSlot\d+").mock(
        return_value=httpx.Response(204))

    configs = state.pool._configs_from_names(names)
    state.pool._store_nodes(configs, skipped=0)
    region_error = RelayError(
        "upstream does not serve this proxy exit region", 502,
        {"region_blocked": True, "upstream": "shared_quota_unavailable: ..."})
    attempts = []

    async def op(proxy_url):
        attempts.append(proxy_url)
        if len(attempts) < 5:
            raise region_error
        return "ok"

    assert await state.with_proxy("acct", op) == "ok"
    assert len(attempts) == 5
    rows = state.store.proxy_rows()
    assert sum(int(row["failure_count"]) > 0 for row in rows) == 4


@respx.mock
async def test_last_region_blocked_node_is_quarantined(mihomo_state):
    state = mihomo_state
    add_account(state, "acct")
    names = ["node-a", "node-b"]

    respx.get(f"{CTRL}/proxies/MirofishSlot0").mock(
        return_value=httpx.Response(200, json={"all": names, "now": names[0]}))
    respx.get(f"{CTRL}/proxies/MirofishPool").mock(
        return_value=httpx.Response(200, json={"all": names, "now": names[0]}))
    respx.put(url__regex=rf"{CTRL}/proxies/MirofishSlot\d+").mock(
        return_value=httpx.Response(204))

    configs = state.pool._configs_from_names(names)
    state.pool._store_nodes(configs, skipped=0)
    region_error = RelayError(
        "upstream does not serve this proxy exit region", 502,
        {"region_blocked": True, "upstream": "shared_quota_unavailable: ..."})

    async def op(_proxy_url):
        raise region_error

    with pytest.raises(RelayError) as raised:
        await state.with_proxy("acct", op)
    assert raised.value.data["region_blocked"] is True
    assert all(int(row["failure_count"]) > 0 for row in state.store.proxy_rows())
    assert state.store.row("acct")["proxy_id"] is None
