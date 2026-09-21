"""Fixed-exit transport identity is exactly (account, proxy URL)."""

import time

import pytest

from mirofish.upstream import _DeviceTicket
from tests.conftest import add_account


async def test_client_cache_is_scoped_to_account_and_fixed_exit(state):
    first = await state.upstream.client("http://exit-a:8080", "acct")
    assert await state.upstream.client("http://exit-a:8080", "acct") is first
    assert await state.upstream.client("http://exit-b:8080", "acct") is not first
    assert await state.upstream.client("http://exit-a:8080", "other") is not first


async def test_direct_never_uses_legacy_tls_proxy_or_environment(state, monkeypatch):
    monkeypatch.setattr(state.settings, "tls_proxy", "http://unused:9999", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://unused:9998")
    direct = await state.upstream.client("direct", "acct")
    assert await state.upstream.client(None, "acct") is direct
    assert await state.upstream.client("", "acct") is direct
    assert direct._mounts == {}
    assert state.upstream._ticket_key("acct", "direct") == ("acct", "")


async def test_device_tickets_use_only_account_and_transport_url(state, monkeypatch):
    add_account(state, "acct")
    minted = []

    async def mint(alias, _access, proxy_url):
        minted.append((alias, proxy_url))
        return _DeviceTicket(f"ticket-{len(minted)}", time.monotonic() + 900)

    monkeypatch.setattr(state.upstream, "_mint_device_ticket", mint)
    first = await state.upstream._device_ticket("acct", "http://exit-a:8080")
    assert await state.upstream._device_ticket("acct", "http://exit-a:8080") == first
    assert await state.upstream._device_ticket("acct", "http://exit-b:8080") != first
    assert len(minted) == 2


@pytest.mark.parametrize("selected", [None, "", "direct", "http://exit:8080"])
async def test_impersonation_preserves_explicit_route(state, monkeypatch, selected):
    from mirofish import tls_impersonate

    clients = []

    class FakeClient:
        def __init__(self, profile, proxy, timeout):
            clients.append((profile, proxy))

        async def aclose(self):
            pass

    monkeypatch.setattr(tls_impersonate, "impersonation_available", lambda: True)
    monkeypatch.setattr(tls_impersonate, "ImpersonatingClient", FakeClient)
    state.settings.tls_impersonate = "chrome136"
    monkeypatch.setattr(state.settings, "tls_proxy", "http://unused:9999", raising=False)
    await state.upstream.client(selected, "acct")
    assert clients == [("chrome136", None if selected in (None, "", "direct") else selected)]
