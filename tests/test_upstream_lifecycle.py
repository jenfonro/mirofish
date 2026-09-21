"""Concurrency and credential-generation invariants for upstream auth state."""

import asyncio
import time
import uuid

import httpx
import pytest
import respx

from mirofish.errors import RelayError
from mirofish.upstream import (LIMITS_PATH, MESSAGES_PATH, _DeviceTicket,
                               account_overloaded_503, account_scoped_429,
                               account_suspension_403, credit_exhausted_429)
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


async def test_fixed_exit_scopes_session_and_401_invalidation(
        state, monkeypatch):
    add_account(state, "work")
    route_a = "http://exit-a:8080"
    route_b = "http://exit-b:8080"
    minted: list[str] = []

    async def mint(_alias, _access, proxy_url=None):
        minted.append(proxy_url)
        return _DeviceTicket(
            "ticket-" + proxy_url + f"-{len(minted)}",
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
    assert minted == [route_a, route_b, route_a]


@pytest.mark.parametrize("status,error", [
    (429, {"type": "shared_quota_unavailable"}),
    (503, {"type": "overloaded_error"}),
    (403, {"type": "permission_error", "message": "this account is suspended; contact support"}),
])
@pytest.mark.parametrize("path", ["/v1/messages", "/v1/responses", "/v1/responses/compact"])
@respx.mock
async def test_fixed_exit_refusals_keep_account_error_envelope(state, status, error, path):
    add_account(state, "work")
    respx.post(RELAY_BASE + "/v1/device/session").mock(
        return_value=_device_response("ticket"))
    respx.post(RELAY_BASE + path).mock(return_value=httpx.Response( status, json={"error": error}))
    with pytest.raises(RelayError) as failure:
        if path == "/v1/messages":
            await state.upstream.messages("work", {"model": "test", "max_tokens": 8},
                                          "http://fixed:8080")
        else:
            await state.upstream.stream_responses("work", b"{}", "http://fixed:8080", path=path)
    assert failure.value.status == status
    assert failure.value.data == {"error": error}


def test_health_classifiers_distinguish_quota_capacity_and_suspension():
    assert account_scoped_429(429, {"error": {"type": "shared_quota_unavailable"}})
    assert not account_overloaded_503(503, {"_raw": "edge unavailable"})
    assert account_overloaded_503(503, {"error": {"type": "overloaded_error"}})
    assert credit_exhausted_429(429, {"error": {"type": "rate_limit_error",
                                               "code": "credit_exhausted_5h"}})
    assert not credit_exhausted_429(429, {"error": {"type": "rate_limit_error"}})
    assert account_suspension_403(403, {"error": {"type": "permission_error",
        "message": "temporarily suspended; access resumes at 2026-09-22T00:00:00Z."}}) \
        == (False, 1790035200.0)
    assert account_suspension_403(403, {"error": {"type": "permission_error",
        "message": "this account is suspended; contact support"}}) == (True, None)
    assert account_suspension_403(403, {"error": {"type": "permission_error",
                                                "message": "not allowed"}}) is None


async def test_alias_replacement_cannot_reuse_old_connections(state):
    first = await state.upstream.client("direct", "work")
    state.upstream.forget_account("work")
    second = await state.upstream.client("direct", "work")
    assert first is not second
    assert first.is_closed


async def test_reset_during_mint_discards_old_key_ticket(state, monkeypatch):
    add_account(state, "work")
    before = state.upstream._signer("work").device_id
    started, released = asyncio.Event(), asyncio.Event()
    devices = []

    async def mint(alias, access, proxy_url):
        device = state.upstream._signer(alias).device_id
        devices.append(device)
        if len(devices) == 1:
            started.set()
            await released.wait()
        return _DeviceTicket(device, time.monotonic() + 900)

    monkeypatch.setattr(state.upstream, "_mint_device_ticket", mint)
    task = asyncio.create_task(state.upstream._device_ticket("work"))
    await started.wait()
    after = state.upstream.reset_device_identity("work")
    released.set()
    assert await task == after != before
    assert devices == [before, after]
