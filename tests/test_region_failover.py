"""Account failover after a proxy pool already remembers region refusals."""

import time

import pytest

from mirofish.errors import RelayError
from mirofish.proxy.parse import proxy_identity
from tests.conftest import add_account


def _add_proxy(state, name: str) -> dict:
    config = {"name": name, "scheme": "http", "host": f"{name}.example",
              "port": 8080, "username": "", "password": ""}
    proxy_id = proxy_identity(config)
    stored = {**config, "id": proxy_id}
    state.pool.configs[proxy_id] = stored
    state.store.upsert_proxy(proxy_id, config)
    state.pool.subscription_url = "https://subscription.test/nodes"
    state.pool.last_refresh = time.time()
    return stored


async def test_remembered_region_exhaustion_fails_over_account(state):
    add_account(state, "alpha")
    add_account(state, "beta")
    proxy = _add_proxy(state, "node-a")
    state.pool.mark_region_refused("alpha", proxy)
    attempted = []

    async def run(alias: str):
        attempted.append(alias)

        async def succeed(_proxy_url):
            return "ok"

        return await state.with_proxy(alias, succeed)

    account, result = await state.with_account_failover(
        "", "", {"messages": [{"role": "user", "content": "hello"}]}, run)

    assert (account, result) == ("beta", "ok")
    assert attempted == ["alpha", "beta"]
    assert state.exhausted_cooldown("alpha") > 0


async def test_new_login_reprobes_regions_refused_for_previous_identity(state):
    add_account(state, "alpha")
    proxy = _add_proxy(state, "node-a")
    state.pool.mark_region_refused("alpha", proxy)
    used = []

    async def run(proxy_url):
        used.append(proxy_url)
        return "sent"

    chosen, result = await state.with_pending_proxy("alpha", run)

    assert chosen == proxy
    assert result == "sent"
    assert used == ["http://node-a.example:8080"]
    assert "alpha" not in state.pool._region_refused


async def test_dead_pool_is_not_mislabeled_as_region_exhaustion(state):
    add_account(state, "alpha")
    proxy = _add_proxy(state, "node-a")
    state.pool.mark_region_refused("alpha", proxy)
    state.store.mark_proxy_failure(proxy["id"], "network failure")
    state.store.mark_proxy_failure(proxy["id"], "network failure")

    with pytest.raises(RelayError) as raised:
        state.pool._select("alpha")

    assert raised.value.status == 503
    assert not (isinstance(raised.value.data, dict)
                and raised.value.data.get("region_refused_everywhere") is True)


async def test_network_exclusion_is_not_mislabeled_as_region_exhaustion(state):
    add_account(state, "alpha")
    refused = _add_proxy(state, "node-a")
    network_failed = _add_proxy(state, "node-b")
    state.pool.mark_region_refused("alpha", refused)

    with pytest.raises(RelayError) as raised:
        state.pool._select("alpha", exclude=network_failed["id"])

    assert raised.value.status == 503
    assert not (isinstance(raised.value.data, dict)
                and raised.value.data.get("region_refused_everywhere") is True)
