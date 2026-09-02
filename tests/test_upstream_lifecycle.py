"""Concurrency and credential-generation invariants for upstream auth state."""

import asyncio
import time
import uuid

import httpx
import respx

from mirofish.errors import RelayError
from mirofish.proxy.mihomo import RoutedProxyURL
from mirofish.upstream import LIMITS_PATH, MESSAGES_PATH, _DeviceTicket
from tests.conftest import AUTH_BASE, RELAY_BASE, add_account
from tests.mirasim_protocol import relay_metadata


def _device_response(ticket: str) -> httpx.Response:
    return httpx.Response(200, json={"ticket": ticket, "expiresIn": 900})


@respx.mock
async def test_initial_limits_prewarms_ticket_then_switches_to_signed(state):
    add_account(state, "work")
    device = respx.post(RELAY_BASE + "/v1/device/session").mock(
        return_value=_device_response("prewarmed-ticket"))
    limits = respx.get(RELAY_BASE + LIMITS_PATH).mock(
        return_value=httpx.Response(200, json={"windows": []}))

    first = await state.upstream.limits("work")
    second = await state.upstream.limits("work")

    assert first[0] == second[0] == 200
    assert device.call_count == 1
    assert limits.call_count == 2
    initial, signed = (call.request for call in limits.calls)
    assert initial.headers["authorization"] == "Bearer access-work"
    assert "x-mirasim-device" not in initial.headers
    assert signed.headers["authorization"] == "Bearer prewarmed-ticket"
    assert signed.headers["x-mirasim-device"]
    assert state.upstream.has_device_session("work") is True


@respx.mock
async def test_concurrent_initial_limits_mint_one_route_ticket(state, monkeypatch):
    add_account(state, "work")
    limits = respx.get(RELAY_BASE + LIMITS_PATH).mock(
        return_value=httpx.Response(200, json={"windows": []}))
    mint_started = asyncio.Event()
    release_mint = asyncio.Event()
    mint_count = 0

    async def mint(_alias, access, _proxy_url=None):
        nonlocal mint_count
        mint_count += 1
        assert access == "access-work"
        mint_started.set()
        await release_mint.wait()
        return _DeviceTicket("shared-ticket", time.monotonic() + 900.0)

    monkeypatch.setattr(state.upstream, "_mint_device_ticket", mint)
    tasks = [asyncio.create_task(state.upstream.limits("work"))
             for _ in range(8)]
    await mint_started.wait()
    await asyncio.sleep(0)
    release_mint.set()
    results = await asyncio.gather(*tasks)

    assert all(result[0] == 200 for result in results)
    assert limits.call_count == 8
    assert mint_count == 1
    assert state.upstream.has_device_session("work") is True


async def test_concurrent_old_ticket_401s_trigger_only_one_new_mint(
        state, monkeypatch):
    add_account(state, "work")
    workers = 6
    key = state.upstream._ticket_key("work", None)
    state.upstream._ticket_cache[key] = _DeviceTicket(
        "old-ticket", time.monotonic() + 900.0)
    state.upstream._device_sessions.add(key)
    all_old_requests_started = asyncio.Event()
    old_requests = 0
    new_requests = 0
    mint_count = 0

    async def mint(_alias, access, _proxy_url=None):
        nonlocal mint_count
        mint_count += 1
        assert access == "access-work"
        # Keep the first remint in flight long enough for every waiter to
        # reach the route lock after conditionally invalidating old-ticket.
        await asyncio.sleep(0.01)
        return _DeviceTicket("new-ticket", time.monotonic() + 900.0)

    async def send(method, url, headers, body=b"", proxy_url=None, **_kwargs):
        nonlocal old_requests, new_requests
        request = httpx.Request(method, url, headers=headers, content=body)
        authorization = request.headers["authorization"]
        if authorization == "Bearer old-ticket":
            old_requests += 1
            if old_requests == workers:
                all_old_requests_started.set()
            await all_old_requests_started.wait()
            return httpx.Response(
                401, json={"error": {"type": "authentication_error"}},
                request=request)
        assert authorization == "Bearer new-ticket"
        new_requests += 1
        return httpx.Response(200, json={"windows": []}, request=request)

    monkeypatch.setattr(state.upstream, "_mint_device_ticket", mint)
    monkeypatch.setattr(state.upstream, "send_explicit", send)
    results = await asyncio.gather(*(
        state.upstream.signed_json("work", "GET", LIMITS_PATH)
        for _ in range(workers)))

    assert all(result[0] == 200 for result in results)
    assert old_requests == new_requests == workers
    assert mint_count == 1
    assert state.upstream._ticket_cache[key].value == "new-ticket"


async def test_stale_ticket_mint_is_discarded_after_relogin_generation(
        state, monkeypatch):
    add_account(state, "work")
    first_mint_started = asyncio.Event()
    release_first_mint = asyncio.Event()
    observed_access: list[str] = []

    async def mint(_alias, access, _proxy_url=None):
        observed_access.append(access)
        if len(observed_access) == 1:
            first_mint_started.set()
            await release_first_mint.wait()
        return _DeviceTicket(
            "ticket-for-" + access, time.monotonic() + 900.0)

    monkeypatch.setattr(state.upstream, "_mint_device_ticket", mint)
    ticket_task = asyncio.create_task(state.upstream._device_ticket("work"))
    await first_mint_started.wait()

    # AccountService performs these credential writes before notifying the
    # Upstream generation, so an old in-flight mint cannot become authoritative.
    state.store.vault.put("work", "refresh", "login-refresh")
    state.store.vault.put("work", "access", "login-access")
    state.upstream.credentials_changed("work")
    release_first_mint.set()
    ticket = await ticket_task

    key = state.upstream._ticket_key("work", None)
    assert observed_access == ["access-work", "login-access"]
    assert ticket == "ticket-for-login-access"
    assert state.upstream._ticket_cache[key].value == ticket


async def test_near_expiry_ticket_is_kept_during_transient_refresh_failure(
        state, monkeypatch):
    add_account(state, "work")
    key = state.upstream._ticket_key("work", None)
    state.upstream._ticket_cache[key] = _DeviceTicket(
        "still-valid", time.monotonic() + 90.0)
    calls = 0

    async def unavailable(_alias, _access, _proxy_url=None):
        nonlocal calls
        calls += 1
        raise RelayError("device session request rejected", 503)

    monkeypatch.setattr(state.upstream, "_mint_device_ticket", unavailable)

    first = await state.upstream._device_ticket("work")
    second = await state.upstream._device_ticket("work")

    assert first == second == "still-valid"
    assert calls == 1


async def test_stale_access_refresh_cannot_overwrite_relogin(state, monkeypatch):
    add_account(state, "work")
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def delayed_json(method, base, path, payload=None, **_kwargs):
        assert (method, base, path) == ("POST", AUTH_BASE, "/auth/refresh")
        assert payload == {"refresh_token": "refresh-work"}
        refresh_started.set()
        await release_refresh.wait()
        return 200, {}, {
            "access_token": "stale-rotated-access",
            "refresh_token": "stale-rotated-refresh",
        }

    monkeypatch.setattr(state.upstream, "json", delayed_json)
    refresh_task = asyncio.create_task(
        state.upstream.refresh_access("work", "access-work"))
    await refresh_started.wait()

    state.store.vault.put("work", "refresh", "login-refresh")
    state.store.vault.put("work", "access", "login-access")
    state.upstream.credentials_changed("work")
    release_refresh.set()
    result = await refresh_task

    assert result == "login-access"
    assert state.store.credentials("work") == ("login-access", "login-refresh")


@respx.mock
async def test_401_retry_keeps_the_session_but_renews_the_call(state):
    add_account(state, "work")
    device = respx.post(RELAY_BASE + "/v1/device/session").mock(side_effect=[
        _device_response("ticket-before-401"),
        _device_response("ticket-after-401"),
    ])
    messages = respx.post(RELAY_BASE + MESSAGES_PATH).mock(side_effect=[
        httpx.Response(401, json={"error": {"type": "authentication_error"}}),
        httpx.Response(200, json={"content": [], "usage": {}}),
    ])
    session_id = "0f20cf48-c292-42e9-a99e-994511307deb"
    payload = {"model": "model-under-test", "messages": [], "max_tokens": 8}

    await state.upstream.messages("work", payload, session_id=session_id)

    assert device.call_count == messages.call_count == 2
    first, second = (call.request for call in messages.calls)
    assert first.headers["authorization"] == "Bearer ticket-before-401"
    assert second.headers["authorization"] == "Bearer ticket-after-401"
    first_meta, second_meta = relay_metadata(first), relay_metadata(second)
    assert first_meta["x-mirasim-session"] == \
        second_meta["x-mirasim-session"] == session_id
    # x-mirasim-session spans a conversation; x-mirasim-call identifies one HTTP
    # request. A credential-refresh retry is a second request and gets its own.
    assert first_meta["x-mirasim-call"] != second_meta["x-mirasim-call"]
    assert uuid.UUID(second_meta["x-mirasim-call"]).version == 4
    assert first_meta["x-mirasim-nonce"] != second_meta["x-mirasim-nonce"]
    assert first_meta["x-mirasim-sig"] != second_meta["x-mirasim-sig"]
    # Each retry is re-sealed with a fresh ephemeral key as well.
    assert first.headers["x-mirasim-enc"] != second.headers["x-mirasim-enc"]
    assert first.content == second.content


async def test_route_identity_scopes_session_and_401_invalidation(
        state, monkeypatch):
    add_account(state, "work")
    route_a = RoutedProxyURL("http://mihomo:7891", "route-a")
    route_b = RoutedProxyURL("http://mihomo:7891", "route-b")
    minted: list[str] = []

    async def mint(_alias, _access, proxy_url=None):
        minted.append(proxy_url.route_identity)
        return _DeviceTicket(
            "ticket-" + proxy_url.route_identity + f"-{len(minted)}",
            time.monotonic() + 900.0,
        )

    monkeypatch.setattr(state.upstream, "_mint_device_ticket", mint)
    ticket_a = await state.upstream._device_ticket("work", route_a)
    ticket_b = await state.upstream._device_ticket("work", route_b)
    key_a = state.upstream._ticket_key("work", route_a)
    key_b = state.upstream._ticket_key("work", route_b)

    state.upstream._invalidate_route_ticket("work", route_a, ticket_a)

    assert key_a not in state.upstream._ticket_cache
    assert state.upstream._ticket_cache[key_b].value == ticket_b
    assert state.upstream.has_device_session("work", route_a) is True
    assert state.upstream.has_device_session("work", route_b) is True
    replacement_a = await state.upstream._device_ticket("work", route_a)
    assert replacement_a != ticket_a
    assert await state.upstream._device_ticket("work", route_b) == ticket_b
    assert minted == ["route-a", "route-b", "route-a"]
