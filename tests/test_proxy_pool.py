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

    # The account moved off the refused node, remembers the refusal, and the
    # node's global health is untouched (other tiers may be served through it).
    assert str(state.store.row("acct")["proxy_id"]) != blocked_id
    assert blocked_id in state.pool._refused_ids("acct")
    refused = next(r for r in state.store.proxy_rows()
                   if str(r["proxy_id"]) == blocked_id)
    assert int(refused["failure_count"]) == 0


@respx.mock
async def test_tenant_profile_region_refusal_rotates_and_refreshes_status(mihomo_state):
    """Generic /me/tenant 429s must carry the same rotatable region marker."""
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
    blocked_id = next(iter(configs))
    state.store.set_account_proxy("acct", blocked_id)

    respx.get(AUTH_BASE + "/auth/me").mock(
        return_value=httpx.Response(200, json={"id": "u-acct", "email": "acct@example.com"}))
    respx.get(AUTH_BASE + "/auth/referral").mock(
        return_value=httpx.Response(200, json={"current_plan": "pro"}))
    tenant = respx.get(RELAY_BASE + "/me/tenant").mock(side_effect=[
        httpx.Response(429, json={
            "error": {
                "type": "shared_quota_unavailable",
                "message": "The cloud route is not served to this network region.",
            },
        }),
        httpx.Response(200, json={"tenant": "tenant-ok"}),
    ])

    result = await state.with_proxy(
        "acct", lambda url: state.accounts.fetch_status("acct", proxy_url=url))

    assert result["tenant"] == "tenant-ok"
    assert tenant.call_count == 2
    assert str(state.store.row("acct")["proxy_id"]) != blocked_id
    assert blocked_id in state.pool._refused_ids("acct")
    refused = next(row for row in state.store.proxy_rows()
                   if str(row["proxy_id"]) == blocked_id)
    assert int(refused["failure_count"]) == 0


@respx.mock
async def test_shared_credit_exhaustion_does_not_rotate_proxy(mihomo_state):
    """Account/shared quota errors are not properties of the proxy exit."""
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
    original_id = next(iter(configs))
    state.store.set_account_proxy("acct", original_id)

    respx.get(AUTH_BASE + "/auth/me").mock(
        return_value=httpx.Response(200, json={"id": "u-acct", "email": "acct@example.com"}))
    respx.get(AUTH_BASE + "/auth/referral").mock(
        return_value=httpx.Response(200, json={"current_plan": "free"}))
    tenant = respx.get(RELAY_BASE + "/me/tenant").mock(
        return_value=httpx.Response(429, json={
            "error": {
                "type": "credit_exhausted_shared",
                "message": "The relay's shared quota is used up.",
            },
        }))

    with pytest.raises(RelayError) as raised:
        await state.with_proxy(
            "acct", lambda url: state.accounts.fetch_status("acct", proxy_url=url))

    assert raised.value.status == 429
    assert raised.value.data["error"]["type"] == "credit_exhausted_shared"
    assert tenant.call_count == 1
    assert str(state.store.row("acct")["proxy_id"]) == original_id
    original = next(row for row in state.store.proxy_rows()
                    if str(row["proxy_id"]) == original_id)
    assert int(original["failure_count"]) == 0


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
    # The four refusals stay per-account memory; the pool itself is untouched.
    assert all(int(row["failure_count"]) == 0 for row in state.store.proxy_rows())
    assert len(state.pool._refused_ids("acct")) == 4


@respx.mock
async def test_region_refused_everywhere_cools_account_not_pool(mihomo_state):
    """When every exit region refuses one account, the account (not the pool)
    is taken out of service: other accounts keep their nodes, the refused
    account cools down, and its immediate retries fail fast without another
    node sweep."""
    state = mihomo_state
    add_account(state, "acct")
    add_account(state, "other")
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
    attempts = []

    async def op(_proxy_url):
        attempts.append(_proxy_url)
        raise region_error

    with pytest.raises(RelayError) as raised:
        await state.with_proxy("acct", op)
    assert raised.value.data["region_blocked"] is True
    assert raised.value.data["region_refused_everywhere"] is True
    assert len(attempts) == 2  # one sweep: each exit tried exactly once
    assert state.store.row("acct")["proxy_id"] is None

    # The pool stays healthy for everyone else.
    assert all(int(row["failure_count"]) == 0 for row in state.store.proxy_rows())
    assert await state.pool.for_account("other") is not None

    # The error is account-scoped: selection cools the account down.
    assert state.note_account_unserviceable("acct", raised.value) is True
    assert state.exhausted_cooldown("acct") > 0

    # An immediate retry fails fast instead of sweeping the pool again.
    attempts.clear()
    with pytest.raises(RelayError) as retried:
        await state.with_proxy("acct", op)
    assert retried.value.status == 503
    assert attempts == []


def test_node_exclude_filters_mihomo_group_names(mihomo_state):
    from mirofish.validate import node_exclude_pattern

    state = mihomo_state
    state.pool.node_exclude = node_exclude_pattern("香港|HK|🇭🇰")
    configs = state.pool._configs_from_names(
        ["🇭🇰 香港-01", "HK-Central-02", "🇯🇵 日本-01", "SG-Marina-03"])
    names = {config["name"] for config in configs.values()}
    assert names == {"🇯🇵 日本-01", "SG-Marina-03"}
