"""Fixed account-to-node behaviour over a manually curated pool."""

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


def test_an_account_sticks_to_one_node(state):
    seed(state)
    add_account(state, "acct")

    first = state.pool.for_account("acct")
    assert state.pool.for_account("acct") == first


def test_accounts_spread_across_nodes(state):
    """Each account gets its own exit where there are enough to go around."""
    seed(state)
    add_account(state, "one")
    add_account(state, "two")

    a = state.pool.for_account("one")
    b = state.pool.for_account("two")

    assert a["id"] != b["id"]


async def test_a_failing_exit_never_moves_the_account(state):
    """The whole point of the pool: a request goes out through the account's
    own exit or not at all.

    Retrying through another node would change the account's upstream IP
    without anyone asking, and two accounts sharing an exit is exactly the
    correlation that gets them flagged. So the failure propagates, the
    binding survives, and the second attempt uses the same exit as the first.
    """
    seed(state)
    add_account(state, "acct")
    bound = state.pool.for_account("acct")["id"]
    attempts = []

    async def op(proxy_url):
        attempts.append(proxy_url)
        raise RelayError("upstream network error", 502, {"proxy_network": True})

    with pytest.raises(RelayError) as raised:
        await state.with_proxy("acct", op)

    assert raised.value.status == 502
    assert len(attempts) == 1  # no second node was tried
    assert state.store.row("acct")["proxy_id"] == bound

    with pytest.raises(RelayError):
        await state.with_proxy("acct", op)
    assert attempts[1] == attempts[0]  # same exit, not the other node


def test_a_network_failure_marks_the_node_but_keeps_its_accounts(state):
    """A failing node leaves *new* assignments while its accounts stay put:
    they have nowhere else to go by design."""
    ids = seed(state)
    add_account(state, "acct")
    state.store.set_account_proxy("acct", ids[0])

    state.pool.fail({"id": ids[0]}, "proxy network failure")

    assert state.pool.active_count() == 1
    assert state.store.row("acct")["proxy_id"] == ids[0]
    assert state.pool.for_account("acct")["id"] == ids[0]


async def test_a_successful_test_returns_a_failed_node_to_service(state):
    """Testing is how an operator puts a node they have fixed back in."""
    ids = seed(state, [NODES[0]])
    add_account(state, "acct")
    state.pool.fail({"id": ids[0]}, "proxy network failure")
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


def test_an_empty_pool_routes_directly(state):
    """No nodes configured is not an error: requests go out unproxied."""
    add_account(state, "acct")

    assert state.pool.for_account("acct") is None
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
