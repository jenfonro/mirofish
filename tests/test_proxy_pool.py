"""Manual pool management; all Google 204 requests are mocked."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from mirofish.errors import RelayError
from mirofish.proxy import DIRECT, ProxyPool, proxy_url
from mirofish.proxy.pool import TEST_TIMEOUT, TEST_URL
from mirofish.store import Store
from mirofish.vault import make_credential_store

NODES = [
    {"name": "", "scheme": "socks5", "host": "a.test", "port": 1080,
     "username": "user", "password": "secret"},
    {"name": "node-b", "scheme": "http", "host": "b.test", "port": 8080,
     "username": "", "password": ""},
]


@pytest.fixture
def pool(settings):
    store = Store(settings.data_dir, make_credential_store(settings.data_dir, "file"))
    pool = ProxyPool(store, settings)
    yield pool
    store.db.close()


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("unexpected network request")

    monkeypatch.setattr(httpx.AsyncClient, "send", unexpected)


def test_add_list_reload_and_credential_storage(pool):
    node = pool.add(NODES[0])
    summary = pool.public_summary()
    assert summary["configured"] is True
    assert summary["total"] == summary["active"] == 1
    assert summary["assigned"] == 0
    assert summary["nodes"][0] == {
        **node, "active": True, "status": "untested", "assigned": 0,
        "failure_count": 0, "last_error": None, "last_checked": None,
    }
    assert ProxyPool(pool.store, pool.settings).by_id(node["id"]) == node
    assert b"secret" not in pool.store.db_path.read_bytes()
    assert b"secret" not in (pool.store.data_dir / "secrets.enc").read_bytes()


def test_add_renames_existing_endpoint_without_duplicate(pool):
    first = pool.add(NODES[0])
    again = pool.add({**NODES[0], "name": "renamed"})
    assert again["id"] == first["id"]
    assert pool.public_summary()["total"] == 1
    first["host"] = "mutated.test"
    assert pool.by_id(again["id"])["host"] == "a.test"


def test_edit_endpoint_retains_id_binding_and_failure_status(pool):
    node = pool.add(NODES[0])
    pool.store.save("acct", "a@test.com", "access", "refresh", {}, proxy_id=node["id"])
    pool.fail(node, "unreachable")

    updated = pool.update(node["id"], {"name": "new", "host": "new.test", "port": 444})

    assert updated["id"] == node["id"]
    assert pool.store.row("acct")["proxy_id"] == node["id"]
    assert pool.for_account("acct") == updated
    assert pool.public_summary()["nodes"][0]["last_error"] == "unreachable"
    assert pool.add({**updated, "name": "re-added"})["id"] == node["id"]
    # Re-adding the old endpoint must not overwrite the edited stable id.
    old_endpoint = pool.add(NODES[0])
    assert old_endpoint["id"] != node["id"]
    assert pool.for_account("acct")["host"] == "new.test"


def test_duplicate_endpoint_edit_is_conflict(pool):
    first, second = (pool.add(node) for node in NODES)
    with pytest.raises(RelayError) as raised:
        pool.update(first["id"], second)
    assert raised.value.status == 409
    assert pool.by_id(first["id"]) == first


def test_edit_omitted_credentials_are_preserved_and_empty_credentials_are_cleared(pool):
    node = pool.add(NODES[0])
    renamed = pool.update(node["id"], {"name": "renamed"})
    assert renamed["username"] == "user" and renamed["password"] == "secret"
    cleared = pool.update(node["id"], {"username": "", "password": ""})
    assert cleared["username"] == cleared["password"] == ""
    assert cleared["id"] == node["id"]
    assert pool.by_id(node["id"]) == cleared


def test_remove_requires_explicit_unbinding_and_preserves_vault_on_conflict(pool):
    first, second = (pool.add(node) for node in NODES)
    pool.store.save("acct", "a@test.com", "access", "refresh", {}, proxy_id=first["id"])
    before = pool.store.proxy_configs()
    with pytest.raises(RelayError) as raised:
        pool.remove(first["id"])
    assert raised.value.status == 409
    assert pool.store.proxy_configs() == before
    assert pool.store.row("acct")["proxy_id"] == first["id"]
    with pytest.raises(RelayError) as raised:
        pool.store.delete_proxy(first["id"])
    assert raised.value.status == 409

    pool.store.set_account_proxy("acct", DIRECT)
    pool.remove(first["id"])
    assert pool.store.row("acct")["proxy_id"] == DIRECT
    assert set(pool.store.proxy_configs()) == {second["id"]}
    assert [row["proxy_id"] for row in pool.store.proxy_rows()] == [second["id"]]
    with pytest.raises(RelayError) as raised:
        pool.remove(first["id"])
    assert raised.value.status == 404


def test_import_reports_bad_lines_and_does_not_probe(pool):
    result = pool.import_uris("""
        socks5://user:secret@a.test:1080
        https://b.test:443#HTTPS
        # ignored
        vmess://unsupported
        b.test:8080
        http://c.test:8080
    """)
    assert result["added"] == 3
    assert result["failed"] == ["line 5: invalid proxy URI", "line 6: invalid proxy URI"]
    assert result["pool"]["total"] == 3
    assert all(node["last_checked"] is None for node in result["pool"]["nodes"])
    assert pool.import_uris("socks5://user:secret@a.test:1080#rename")["pool"]["total"] == 3


def test_import_invalid_uri_does_not_return_credentials(pool):
    result = pool.import_uris("\nhttp://sensitive-user:secret-password@proxy.test:bad\n")
    assert result["added"] == 0
    assert result["failed"] == ["line 2: invalid proxy URI"]
    assert "secret-password" not in str(result)
    assert "sensitive-user" not in str(result)


def test_invalid_add_or_edit_does_not_write(pool):
    node = pool.add(NODES[0])
    for write in (lambda: pool.add({"scheme": "vmess"}),
                  lambda: pool.update(node["id"], {"port": -1})):
        with pytest.raises(RelayError) as raised:
            write()
        assert raised.value.status == 400
    assert pool.by_id(node["id"]) == node


def test_network_errors_are_status_not_disablement(pool):
    node = pool.add(NODES[0])
    pool.store.save("acct", "a@test.com", "access", "refresh", {}, proxy_id=node["id"])
    for _ in range(5):
        pool.fail(node, "refused")
        assert pool.for_account("acct") == node
    row = pool.store.proxy_rows()[0]
    assert row["active"] == 1 and row["failure_count"] == 5
    assert pool.public_summary()["nodes"][0]["status"] == "error"
    assert pool.active_count() == 1
    assert pool.store.row("acct")["proxy_id"] == node["id"]


def test_public_account_redacts_credentials_and_direct_is_not_an_assignment(pool):
    node = pool.add(NODES[0])
    for alias, binding in (("acct", node["id"]), ("direct", DIRECT), ("unbound", None)):
        pool.store.save(alias, f"{alias}@test.com", "access", "refresh", {}, proxy_id=binding)
    public = pool.account_public("acct")
    assert public["id"] == node["id"]
    assert "username" not in public and "password" not in public
    assert pool.account_public("direct") is None
    assert pool.account_public("unbound")["status"] == "unbound"
    assert pool.store.proxy_assignment_counts() == {node["id"]: 1}


def mock_test_client(monkeypatch, *, status=204, error=None):
    client = MagicMock()
    client.get = AsyncMock(return_value=httpx.Response(status), side_effect=error)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    constructor = MagicMock(return_value=context)
    monkeypatch.setattr("mirofish.proxy.pool.httpx.AsyncClient", constructor)
    return constructor, client


@pytest.mark.parametrize("scheme", ["http", "https", "socks5"])
async def test_manual_204_success_clears_errors_without_rebinding(pool, monkeypatch, scheme):
    node = pool.add({**NODES[0], "scheme": scheme})
    pool.store.save("acct", "a@test.com", "access", "refresh", {}, proxy_id=node["id"])
    pool.fail(node, "broken")
    constructor, client = mock_test_client(monkeypatch)

    result = await pool.test(node["id"])

    assert result["ok"] is True and result["latency_ms"] >= 0
    constructor.assert_called_once_with(proxy=proxy_url(node), trust_env=False,
                                        timeout=TEST_TIMEOUT, follow_redirects=False)
    client.get.assert_awaited_once_with(TEST_URL)
    public = pool.public_summary()["nodes"][0]
    assert public["failure_count"] == 0 and public["last_error"] is None
    assert public["status"] == "ok" and public["last_checked"]
    assert pool.store.row("acct")["proxy_id"] == node["id"]


@pytest.mark.parametrize("status", [200, 301, 403, 407, 500])
async def test_manual_non_204_status_never_follows_or_falls_back(pool, monkeypatch, status):
    node = pool.add(NODES[0])
    constructor, client = mock_test_client(monkeypatch, status=status)
    result = await pool.test(node["id"])
    assert result["ok"] is False and str(status) in result["error"]
    assert pool.public_summary()["nodes"][0]["status"] == "error"
    assert pool.by_id(node["id"]) == node
    assert constructor.call_count == client.get.await_count == 1


async def test_manual_network_failure_is_recorded_once(pool, monkeypatch):
    node = pool.add(NODES[0])
    constructor, client = mock_test_client(monkeypatch, error=httpx.ConnectError("refused"))
    result = await pool.test(node["id"])
    assert result == {"id": node["id"], "ok": False, "error": "refused"}
    assert pool.store.proxy_rows()[0]["active"] == 1
    assert constructor.call_count == client.get.await_count == 1


@pytest.mark.parametrize("proxy_id,status", [(DIRECT, 400), ("missing", 404)])
async def test_manual_test_rejects_non_nodes(pool, proxy_id, status):
    with pytest.raises(RelayError) as raised:
        await pool.test(proxy_id)
    assert raised.value.status == status
