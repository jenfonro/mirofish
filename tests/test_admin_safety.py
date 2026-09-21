import json
import time

import httpx
import pytest
import respx

from mirofish.errors import RelayError
from tests.conftest import AUTH_BASE, RELAY_BASE, add_account


@respx.mock
async def test_profile_never_queries_relay_or_limits(client, state, auth_headers):
    add_account(state, 'work')
    respx.get(AUTH_BASE + '/auth/me').respond(200, json={'id':'u-work','plan':'pro'})
    respx.get(AUTH_BASE + '/auth/referral').respond(200, json={'current_plan':'pro'})
    assert (await client.get('/accounts/work/status?probe=1', headers=auth_headers)).status_code == 200
    assert len(respx.calls) == 2
    assert all(str(c.request.url).startswith(AUTH_BASE) for c in respx.calls)


@pytest.mark.parametrize('bad', [0, -1, 1.01, 2, 'nan', True, None])
async def test_schedule_threshold_rejects_invalid_values(client, auth_headers, bad):
    response = await client.post('/api/schedule', headers=auth_headers,
                                 json={'max_utilization': bad})
    assert response.status_code == 400


async def test_schedule_persists_without_probes(client, state, settings, auth_headers):
    from mirofish.api.state import AppState
    result = await client.post('/api/schedule', headers=auth_headers,
                               json={'max_utilization': .95})
    assert result.status_code == 200
    other = AppState(settings)
    try:
        assert other.settings.quota_ceiling == .95
    finally:
        await other.aclose()


async def test_login_proxy_removed_between_code_and_verify_fails_closed(client, state, auth_headers, monkeypatch):
    calls = []
    state.put_pending_login('work', 'work@example.com', 'removed-node')
    async def forbidden(*args, **kwargs):
        calls.append(1)
    monkeypatch.setattr(state.accounts, 'finish_login', forbidden)
    result = await client.post('/api/login/finish', headers=auth_headers,
                               json={'alias':'work','code':'123456'})
    assert result.status_code == 503
    assert calls == []


@respx.mock
async def test_relogin_preserves_disabled_and_direct_and_profile_survives_limits_403(client, state, auth_headers):
    add_account(state, 'work')
    state.store.merge_metadata('work', {'disabled': True, 'display_name': 'Work'})
    state.put_pending_login('work', 'work@example.com', 'direct')
    respx.post(AUTH_BASE + '/auth/verify').respond(200, json={
        'access_token':'new-access','refresh_token':'new-refresh'})
    respx.get(AUTH_BASE + '/auth/me').respond(200, json={'id':'u-work','plan':'pro'})
    respx.get(AUTH_BASE + '/auth/referral').respond(200, json={'current_plan':'pro'})
    respx.get(RELAY_BASE + '/v1/limits').respond(403, json={'error': {
        'type':'permission_error','message':'this account is suspended; contact support'}})
    result = await client.post('/api/login/finish', headers=auth_headers,
                               json={'alias':'work','code':'123456'})
    assert result.status_code == 200
    body = result.json()
    assert body['disabled'] is True
    assert body['display_name'] == 'Work'
    assert body['plan'] == 'pro'
    assert body['health']['state'] == 'suspended'
    assert state.store.row('work')['proxy_id'] == 'direct'
