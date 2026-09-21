"""Quota reads are demand-driven, single-flight, and failures consume the TTL too."""
import asyncio
import time

import pytest

from mirofish.errors import RelayError
from tests.conftest import add_account


def invalidate(state):
    state.store.merge_metadata('work', {'limits': None, 'limits_attempt_epoch': None,
                                        'limits_error': None})


def response():
    return {'windows': [{'name': name, 'used': 10, 'budget': 100,
                         'reset_at': time.time() + 7200}
                        for name in ('5h', '7d', '7d_claude', '7d_fable')]}


async def test_parallel_expired_reads_share_one_request(state, monkeypatch):
    add_account(state, 'work')
    invalidate(state)
    calls = []
    async def read(alias, proxy_url=None):
        calls.append(alias)
        await asyncio.sleep(.02)
        return 200, {}, response()
    monkeypatch.setattr(state.upstream, 'limits', read)
    values = await asyncio.gather(*(state.accounts.fetch_limits('work') for _ in range(10)))
    assert calls == ['work']
    assert all(value == values[0] for value in values)
    await state.accounts.fetch_limits('work')
    assert len(calls) == 1


async def test_parallel_force_reads_share_one_request(state, monkeypatch):
    add_account(state, 'work')
    calls = []
    async def read(alias, proxy_url=None):
        calls.append(alias)
        await asyncio.sleep(.02)
        return 200, {}, response()
    monkeypatch.setattr(state.upstream, 'limits', read)
    await asyncio.gather(*(state.accounts.fetch_limits('work', force=True) for _ in range(10)))
    assert calls == ['work']


async def test_failed_read_is_not_repeated_on_each_request_or_restart(state, settings, monkeypatch):
    from mirofish.api.state import AppState
    add_account(state, 'work')
    invalidate(state)
    calls = []
    async def read(alias, proxy_url=None):
        calls.append(alias)
        raise RelayError('synthetic network failure', 502)
    monkeypatch.setattr(state.upstream, 'limits', read)
    for _ in range(4):
        with pytest.raises(RelayError):
            await state.accounts.fetch_limits('work')
    assert calls == ['work']
    other = AppState(settings)
    monkeypatch.setattr(other.upstream, 'limits', read)
    try:
        with pytest.raises(RelayError):
            await other.accounts.fetch_limits('work')
        assert calls == ['work']
    finally:
        await other.aclose()


async def test_force_bypasses_fresh_cache_once(state, monkeypatch):
    add_account(state, 'work')
    state.store.merge_metadata('work', {'limits_attempt_epoch': time.time() - 10})
    calls = []
    async def read(alias, proxy_url=None):
        calls.append(alias)
        return 200, {}, response()
    monkeypatch.setattr(state.upstream, 'limits', read)
    await state.accounts.fetch_limits('work')
    assert calls == []
    # The normal fixture cache is brand new; age it beyond the burst-dedup floor.
    cached = state.accounts.cached_limits('work')
    cached['fetched_epoch'] -= 10
    state.store.merge_metadata('work', {'limits': cached})
    await state.accounts.fetch_limits('work', force=True)
    assert calls == ['work']


async def test_panel_and_lifespan_do_not_contact_upstream(client, state, auth_headers, monkeypatch):
    add_account(state, 'work')
    calls = []
    async def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('unexpected upstream call')
    for name in ('limits', 'authed_json', 'signed_json', 'messages'):
        monkeypatch.setattr(state.upstream, name, forbidden)
    from mirofish.api import create_app
    app = create_app(state)
    async with app.router.lifespan_context(app):
        for path in ('/health', '/accounts', '/api/limits', '/proxies', '/api/usage', '/api/schedule'):
            assert (await client.get(path, headers=auth_headers)).status_code == 200
        await asyncio.sleep(.05)
    assert calls == []


async def test_manual_refresh_does_not_refresh_disabled_accounts(client, state, auth_headers, monkeypatch):
    add_account(state, 'work')
    state.store.merge_metadata('work', {'disabled': True})
    async def forbidden(*args, **kwargs):
        raise AssertionError('disabled account must not be batch-refreshed')
    monkeypatch.setattr(state.upstream, 'limits', forbidden)
    result = await client.post('/api/limits/refresh', headers=auth_headers)
    assert result.status_code == 200
    assert result.json()['accounts'] == []
