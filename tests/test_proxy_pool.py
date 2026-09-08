"""Sticky account-to-node behaviour over a manually curated pool."""

import httpx
import pytest
import respx

from mirofish.errors import RelayError

from tests.conftest import AUTH_BASE, RELAY_BASE, add_account

NODES = [
    {"name": "node-a", "scheme": "socks5", "host": "a.example.com", "port": 1080,
     "username": "", "password": ""},
    {"name": "node-b", "scheme": "socks5", "host": "b.example.com", "port": 1080,
     "username": "", "password": ""},
]


def seed(state, nodes=None):
    """Add nodes the way an operator would, and return their ids in order."""
    return [state.pool.add(node)["id"] for node in (nodes or NODES)]


def test_a_node_is_added_and_listed(state):
    seed(state)
    summary = state.pool.public_summary()

    assert summary["total"] == 2
    assert summary["active"] == 2
    assert {node["name"] for node in summary["nodes"]} == {"node-a", "node-b"}


def test_re_adding_an_endpoint_renames_it_instead_of_duplicating(state):
    """Identity is the endpoint, so an operator re-entering a node they
    already have is editing it, not pooling a second identical exit."""
    first = seed(state, [NODES[0]])[0]
    again = state.pool.add({**NODES[0], "name": "Tokyo"})

    assert again["id"] == first
    assert state.pool.public_summary()["total"] == 1
    assert again["name"] == "Tokyo"


def test_renaming_keeps_accounts_pinned(state):
    """Accounts are pinned by id: a rename that re-identified the node would
    silently unpin every account using it."""
    node_id = seed(state, [NODES[0]])[0]
    add_account(state, "acct")
    state.store.set_account_proxy("acct", node_id)

    updated = state.pool.update(node_id, {"name": "Tokyo"})

    assert updated["id"] == node_id
    assert str(state.store.row("acct")["proxy_id"]) == node_id


def test_moving_an_endpoint_releases_its_accounts(state):
    """Editing host/port points the entry at a different server, so accounts
    must be re-pinned rather than silently following it there."""
    node_id = seed(state, [NODES[0]])[0]
    add_account(state, "acct")
    state.store.set_account_proxy("acct", node_id)

    updated = state.pool.update(node_id, {"host": "moved.example.com"})

    assert updated["id"] != node_id
    assert state.store.row("acct")["proxy_id"] is None


def test_deleting_a_node_releases_its_accounts(state):
    node_id = seed(state, [NODES[0]])[0]
    add_account(state, "acct")
    state.store.set_account_proxy("acct", node_id)

    state.pool.remove(node_id)

    assert state.store.row("acct")["proxy_id"] is None
    assert state.pool.public_summary()["total"] == 0
    with pytest.raises(RelayError):
        state.pool.remove(node_id)


async def test_an_account_sticks_to_one_node(state):
    seed(state)
    add_account(state, "acct")

    first = await state.pool.for_account("acct")
    assert await state.pool.for_account("acct") == first


async def test_accounts_spread_across_nodes(state):
    """Each account gets its own exit where there are enough to go around."""
    seed(state)
    add_account(state, "one")
    add_account(state, "two")

    a = await state.pool.for_account("one")
    b = await state.pool.for_account("two")

    assert a["id"] != b["id"]


@respx.mock
async def test_region_blocked_node_is_rotated_away(state):
    """A region refusal is a property of the exit, so the account moves to
    another node rather than being taken out of service."""
    seed(state)
    add_account(state, "acct")
    region_error = RelayError(
        "upstream does not serve this proxy exit region", 502,
        {"region_blocked": True, "upstream": "shared_quota_unavailable: ..."})
    attempts = []

    async def op(proxy_url):
        attempts.append(proxy_url)
        if len(attempts) == 1:
            raise region_error
        return "ok"

    assert await state.with_proxy("acct", op) == "ok"
    assert len(attempts) == 2
    assert attempts[0] != attempts[1]
    # A region refusal must not mark the node unhealthy for everyone else.
    assert all(int(row["failure_count"]) == 0 for row in state.store.proxy_rows())


async def test_region_refused_everywhere_cools_account_not_pool(state):
    """When every exit refuses one account, the account is taken out of
    service, not the pool: other accounts keep their nodes, and the refused
    account's immediate retry fails fast without another sweep."""
    seed(state)
    add_account(state, "acct")
    add_account(state, "other")
    region_error = RelayError(
        "upstream does not serve this proxy exit region", 502,
        {"region_blocked": True, "upstream": "shared_quota_unavailable: ..."})
    attempts = []

    async def op(proxy_url):
        attempts.append(proxy_url)
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


async def test_a_network_failure_takes_the_node_out_for_everyone(state):
    """Unlike a region refusal, a dead exit is dead for every account."""
    ids = seed(state)
    add_account(state, "acct")

    state.pool.fail("acct", {"id": ids[0]}, "proxy network failure")

    assert state.pool.active_count() == 1
    assert state.store.row("acct")["proxy_id"] is None


async def test_a_successful_test_returns_a_failed_node_to_the_rotation(state):
    """Testing is how an operator puts a node they have fixed back in."""
    ids = seed(state, [NODES[0]])
    add_account(state, "acct")
    state.pool.fail("acct", {"id": ids[0]}, "proxy network failure")
    assert state.pool.active_count() == 0

    with respx.mock:
        respx.get("http://www.gstatic.com/generate_204").mock(
            return_value=httpx.Response(204))
        result = await state.pool.test(ids[0])

    assert result["ok"] is True
    assert state.pool.active_count() == 1


async def test_a_failed_test_records_the_reason(state):
    ids = seed(state, [NODES[0]])

    with respx.mock:
        respx.get("http://www.gstatic.com/generate_204").mock(
            side_effect=httpx.ConnectError("refused"))
        result = await state.pool.test(ids[0])

    assert result["ok"] is False
    assert result["error"]
    node = state.pool.public_summary()["nodes"][0]
    assert node["active"] is False
    assert node["last_error"]


async def test_an_unexpected_status_is_a_failure(state):
    """The probe expects exactly 204; anything else means something is
    intercepting the request rather than passing it through."""
    ids = seed(state, [NODES[0]])

    with respx.mock:
        respx.get("http://www.gstatic.com/generate_204").mock(
            return_value=httpx.Response(200, text="captive portal"))
        result = await state.pool.test(ids[0])

    assert result["ok"] is False
    assert "200" in result["error"]


async def test_an_empty_pool_routes_directly(state):
    """No nodes configured is not an error: requests go out unproxied."""
    add_account(state, "acct")

    assert await state.pool.for_account("acct") is None
    assert state.pool.configured is False


async def test_the_admin_api_adds_edits_and_deletes(client, state, auth_headers):
    added = await client.post("/api/proxies", headers=auth_headers, json={
        "scheme": "socks5", "host": "a.example.com", "port": 1080})
    assert added.status_code == 200
    node_id = added.json()["id"]
    # A blank name falls back to the endpoint rather than staying empty.
    assert added.json()["name"] == "a.example.com:1080"

    edited = await client.patch(f"/api/proxies/{node_id}", headers=auth_headers,
                                json={"name": "Tokyo", "scheme": "socks5",
                                      "host": "a.example.com", "port": 1080})
    assert edited.json()["name"] == "Tokyo"
    assert edited.json()["id"] == node_id

    listed = await client.get("/proxies", headers=auth_headers)
    assert listed.json()["total"] == 1

    dropped = await client.delete(f"/api/proxies/{node_id}", headers=auth_headers)
    assert dropped.json()["pool"]["total"] == 0


async def test_bulk_import_reports_bad_lines_without_losing_good_ones(
        client, state, auth_headers):
    """A pasted list routinely has a stray line in it; losing the twenty good
    entries over one bad one is not useful."""
    text = "\n".join([
        "socks5://user:pass@198.51.100.24:1080",
        "203.0.113.77:8080",
        "# a comment",
        "",
        "vmess://unsupported",
    ])

    response = await client.post("/api/proxies/import", headers=auth_headers,
                                 json={"text": text})

    body = response.json()
    assert body["added"] == 2
    assert body["failed"] == ["vmess://unsupported"]
    assert body["pool"]["total"] == 2


async def test_an_account_can_be_pinned_and_released(client, state, auth_headers):
    add_account(state, "acct")
    node_id = state.pool.add(NODES[0])["id"]

    pinned = await client.post("/api/accounts/acct/proxy", headers=auth_headers,
                               json={"proxy_id": node_id})
    assert pinned.json()["proxy"]["id"] == node_id
    assert str(state.store.row("acct")["proxy_id"]) == node_id

    released = await client.post("/api/accounts/acct/proxy", headers=auth_headers,
                                 json={"proxy_id": ""})
    assert released.json()["proxy"] is None

    unknown = await client.post("/api/accounts/acct/proxy", headers=auth_headers,
                                json={"proxy_id": "nope"})
    assert unknown.status_code == 404
