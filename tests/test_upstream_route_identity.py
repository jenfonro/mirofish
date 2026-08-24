"""Connection and ticket caches must follow the selected Mihomo node."""

import time

from mirofish.proxy.mihomo import SlotManager
from mirofish.upstream import _DeviceTicket
from tests.conftest import add_account


class _FakeMihomo:
    def __init__(self) -> None:
        self.selected = []

    async def has_group(self, _group: str) -> bool:
        return True

    async def set_selector(self, group: str, node: str) -> None:
        self.selected.append((group, node))


async def _route(manager: SlotManager, node: str):
    async with manager.route("acct", node) as proxy_url:
        return proxy_url


async def test_client_cache_uses_node_identity_behind_same_slot(state):
    manager = SlotManager(_FakeMihomo(), "http://mihomo:7890", 1, 7891,
                          "MirofishPool")

    node_a = await _route(manager, "node-a")
    node_a_again = await _route(manager, "node-a")
    node_b = await _route(manager, "node-b")

    # The value passed to httpx remains the listener's actual URL.  Only the
    # cache identity distinguishes which selector node owns its open tunnels.
    assert str(node_a) == str(node_a_again) == str(node_b) == "http://mihomo:7891"
    client_a = await state.upstream.client(node_a)
    assert await state.upstream.client(node_a_again) is client_a
    assert await state.upstream.client(node_b) is not client_a


async def test_device_ticket_cache_is_scoped_to_node_route(state, monkeypatch):
    add_account(state, "acct")
    manager = SlotManager(_FakeMihomo(), "http://mihomo:7890", 1, 7891,
                          "MirofishPool")
    node_a = await _route(manager, "node-a")
    node_b = await _route(manager, "node-b")
    minted = []

    async def mint(alias, _access, proxy_url):
        minted.append((alias, proxy_url.route_identity))
        return _DeviceTicket(f"ticket-{len(minted)}", time.monotonic() + 900.0)

    monkeypatch.setattr(state.upstream, "_mint_device_ticket", mint)

    first = await state.upstream._device_ticket("acct", node_a)
    assert await state.upstream._device_ticket("acct", node_a) == first
    second = await state.upstream._device_ticket("acct", node_b)

    assert second != first
    assert [route for _, route in minted] == [
        "mihomo:MirofishSlot0:node-a",
        "mihomo:MirofishSlot0:node-b",
    ]
