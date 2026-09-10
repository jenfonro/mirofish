"""Per-account background behaviour replay."""

from __future__ import annotations

import asyncio
import gzip
import json
import time
import uuid

import httpx
import respx

from mirofish.behavior import (
    ANALYTICS_DEVICE_SETTING_PREFIX,
    BehaviorReplayer,
    _heartbeat_event,
    _jitter_seconds,
    _telemetry_envelope,
    _update_url,
)
from tests.conftest import RELAY_BASE, add_account


def test_jitter_stays_inside_the_desktop_window():
    for lo, hi in ((1200, 2400), (43200, 86400), (21600, 43200)):
        for _ in range(20):
            value = _jitter_seconds(lo, hi)
            assert lo <= value <= hi


def test_telemetry_envelope_matches_the_desktop_shape(state):
    device_id = str(uuid.uuid4())
    event = _heartbeat_event(
        event_id=f"{device_id}:abc123def456:1",
        boot_id="abc123def456",
        seq=1,
        device_id=device_id,
        user_id="usr_test",
        settings=state.settings,
        uptime_sec=42,
    )
    body = _telemetry_envelope(device_id, [event])
    envelope = json.loads(gzip.decompress(body))
    assert list(envelope) == ["deviceId", "sentAt", "events"]
    assert envelope["deviceId"] == device_id
    assert envelope["sentAt"].endswith("Z")
    record = envelope["events"][0]
    assert record["v"] == 1
    assert record["name"] == "app.heartbeat"
    assert record["id"] == f"{device_id}:abc123def456:1"
    assert record["bootId"] == "abc123def456"
    assert record["seq"] == 1
    assert record["userId"] == "usr_test"
    assert record["deviceId"] == device_id
    assert record["clientId"] is None
    assert record["accountId"] is None
    assert record["tenant"] == "external"
    assert record["surface"] == "desktop"
    assert record["appVersion"] == state.settings.mirasim_client_version
    assert record["platform"].count("-") == 1
    assert record["props"] == {"uptimeSec": 42, "clients": 1}


def test_analytics_device_id_is_per_account_and_persisted(state):
    add_account(state, "work")
    add_account(state, "personal")
    replayer = BehaviorReplayer(state)
    work = replayer._analytics_device("work")
    personal = replayer._analytics_device("personal")
    assert uuid.UUID(work)
    assert uuid.UUID(personal)
    # A multi-account relay must not share one install UUID across accounts.
    assert work != personal
    assert state.store.setting(f"{ANALYTICS_DEVICE_SETTING_PREFIX}:work") == work
    assert (state.store.setting(
        f"{ANALYTICS_DEVICE_SETTING_PREFIX}:personal") == personal)

    # A second replayer (fresh process) reloads the same per-account ids.
    again = BehaviorReplayer(state)
    assert again._analytics_device("work") == work
    assert again._analytics_device("personal") == personal

    # bootId/seq are also per-account cluster keys.
    assert replayer._boot_id("work") != replayer._boot_id("personal")
    assert replayer._next_seq("work") == 1
    assert replayer._next_seq("work") == 2
    assert replayer._next_seq("personal") == 1


def test_update_check_targets_cdn_assets(state):
    assert _update_url(state.settings) == (
        "https://cdn-assets.mirasim.ai/mirasim/releases/latest.json")


def test_start_is_a_noop_when_replay_is_disabled(state):
    state.settings.behavior_replay = False
    replayer = BehaviorReplayer(state)
    replayer.start()
    assert replayer._stop_event is None
    assert replayer._tasks == {}


async def test_start_is_idempotent(state):
    state.settings.behavior_replay = True
    replayer = BehaviorReplayer(state)
    replayer.start()
    first = replayer._stop_event
    assert first is not None
    replayer.start()
    assert replayer._stop_event is first


def test_paused_when_disabled_or_cooling_down(state):
    add_account(state, "work")
    replayer = BehaviorReplayer(state)
    assert replayer._paused("work") is False

    state.store.merge_metadata("work", {"disabled": True})
    assert replayer._paused("work") is True
    state.store.merge_metadata("work", {"disabled": False})

    state._exhausted_until["work"] = time.time() + 30.0
    assert replayer._paused("work") is True


@respx.mock
async def test_send_events_posts_gzip_envelope_through_the_account_exit(state):
    add_account(state, "work")
    route = respx.post(RELAY_BASE + "/events").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    replayer = BehaviorReplayer(state)

    await replayer._send_events("work")

    request = route.calls.last.request
    assert request.headers["content-encoding"] == "gzip"
    assert request.headers["authorization"] == "Bearer access-work"
    envelope = json.loads(gzip.decompress(request.content))
    assert list(envelope) == ["deviceId", "sentAt", "events"]
    record = envelope["events"][0]
    assert record["name"] == "app.heartbeat"
    assert record["userId"] == "u-work"
    assert record["deviceId"] == envelope["deviceId"]
    # Relay-local alias never appears as its own telemetry field.
    assert "alias" not in record
    assert set(record) >= {
        "v", "id", "name", "ts", "bootId", "seq", "userId", "deviceId",
        "clientId", "accountId", "tenant", "surface", "appVersion",
        "platform", "locale", "props",
    }


@respx.mock
async def test_send_roster_uses_the_signed_control_path(state):
    add_account(state, "work")
    respx.post(RELAY_BASE + "/v1/device/session").mock(
        return_value=httpx.Response(
            200, json={"ticket": "device-ticket", "expiresIn": 900}))
    route = respx.get(RELAY_BASE + "/v1/model-roster").mock(
        return_value=httpx.Response(200, json={"models": []}))
    replayer = BehaviorReplayer(state)

    await replayer._send_roster("work")

    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer device-ticket"
    assert "x-mirasim-sig" in request.headers


@respx.mock
async def test_send_update_check_hits_cdn_with_desktop_user_agent(state):
    add_account(state, "work")
    route = respx.get(
        "https://cdn-assets.mirasim.ai/mirasim/releases/latest.json").mock(
            return_value=httpx.Response(200, json={"version": "0.0.0"}))
    replayer = BehaviorReplayer(state)

    await replayer._send_update_check("work")

    request = route.calls.last.request
    assert request.headers["user-agent"] == (
        "mirasim-desktop/" + state.settings.mirasim_client_version)


async def test_aclose_stops_running_tasks(state):
    add_account(state, "work")
    replayer = BehaviorReplayer(state)
    replayer.start()
    # The supervisor needs a tick to spawn the per-account loop.
    await asyncio.sleep(0.05)
    assert "work" in replayer._tasks
    await replayer.aclose()
    assert replayer._tasks == {}
    assert replayer._stop_event is None
