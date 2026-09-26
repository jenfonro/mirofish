"""Per-account background behavior replay: telemetry, roster, update checks.

The official desktop client does more than answer model calls: it posts
gzipped ``/events`` telemetry about every half hour, refreshes the model
roster from ``/v1/model-roster``, and polls ``cdn-assets`` for updates. A
relay that only answers relayed model calls leaves a distinctive silence
upstream. This module runs one task per account that replays those behaviors
on the account's own proxy exit, with random jitter so the fleet does not
beat in lockstep.

Replay is controlled by ``MIROFISH_BEHAVIOR_REPLAY`` (default on). A task
pauses while the account is switched off in the panel (sleep), is inside a
shared-quota / 429 cooldown (fuse), or is parked after the upstream refused
the account itself (a ban or rejected credentials), and exits when the alias
disappears. A parked account's only upstream contact is its recovery probe.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import random
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, TYPE_CHECKING

from .config import Settings

if TYPE_CHECKING:
    from .upstream import Upstream
    from .api.state import AppState

logger = logging.getLogger("mirofish.behavior")

# Observed app.heartbeat spacing on a live 0.0.303 desktop is ~26–32 min.
# Roster and update checks are daily housekeeping.
EVENTS_MIN_SECONDS = 20.0 * 60.0
EVENTS_MAX_SECONDS = 40.0 * 60.0
ROSTER_MIN_SECONDS = 12.0 * 3600.0
ROSTER_MAX_SECONDS = 24.0 * 3600.0
UPDATE_MIN_SECONDS = 6.0 * 3600.0
UPDATE_MAX_SECONDS = 12.0 * 3600.0

# How often a paused account re-checks its status. Short enough to resume
# quickly after a cooldown, long enough to keep idle accounts quiet.
PAUSE_CHECK_SECONDS = 30.0

#: Analytics device id key prefix in the non-secret settings table. This UUID
#: is the desktop's ``device.json`` id and is deliberately *not* the
#: Ed25519-derived ``x-mirasim-device`` used on signed model calls.  It is
#: stored per account so a multi-account relay does not present one shared
#: installation UUID to upstream clustering.
ANALYTICS_DEVICE_SETTING_PREFIX = "analytics_device_id"

_ANALYTICS_TENANT = "external"
_ANALYTICS_SURFACE = "desktop"


def _jitter_seconds(min_seconds: float, max_seconds: float) -> float:
    """Uniform jitter inside the observed client interval."""
    return random.uniform(min_seconds, max_seconds)


def _platform_tag(settings: Settings) -> str:
    """The desktop's ``platform`` value, e.g. ``darwin-arm64``.

    Uses the same configured OS identity as the appeal envelope so the
    analytics tag and ``feedback`` app.platform/arch agree for one deviceId,
    rather than leaking the real (containerised) host.
    """
    return f"{settings.mirasim_os_platform}-{settings.mirasim_os_arch}"


def _analytics_locale() -> str:
    # The desktop's analytics locale tracks the OS locale, which is
    # independent of the zh-HK product locale stamped on relay requests.
    for name in ("MIRASIM_ANALYTICS_LOCALE", "LC_ALL", "LANG"):
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        return raw.split(".")[0].replace("_", "-") or "en-US"
    return "en-US"


def _iso_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _heartbeat_event(*, event_id: str, boot_id: str, seq: int,
                     device_id: str, user_id: Optional[str],
                     settings: Settings, uptime_sec: int) -> dict[str, Any]:
    """One ``app.heartbeat`` record in the live desktop's field order."""
    return {
        "v": 1,
        "id": event_id,
        "name": "app.heartbeat",
        "ts": _iso_now(),
        "bootId": boot_id,
        "seq": seq,
        "userId": user_id,
        "deviceId": device_id,
        "clientId": None,
        "accountId": None,
        "tenant": _ANALYTICS_TENANT,
        "surface": _ANALYTICS_SURFACE,
        "appVersion": settings.mirasim_client_version,
        "platform": _platform_tag(settings),
        "locale": _analytics_locale(),
        "props": {"uptimeSec": uptime_sec, "clients": 1},
    }


def _telemetry_envelope(device_id: str, events: list[dict[str, Any]]) -> bytes:
    """The gzipped ``/events`` body: ``{deviceId, sentAt, events}``."""
    payload = {
        "deviceId": device_id,
        "sentAt": _iso_now(),
        "events": events,
    }
    return gzip.compress(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _roster_url(settings: Settings) -> str:
    return settings.relay_base + "/v1/model-roster"


def _update_url(settings: Settings) -> str:
    # The desktop update check lives on the CDN, not the relay host.
    return "https://cdn-assets.mirasim.ai/mirasim/releases/latest.json"


class BehaviorReplayer:
    """Owns the per-account asyncio tasks and their pause/wake logic."""

    def __init__(self, state: AppState) -> None:
        self.state = state
        self.settings = state.settings
        self.upstream: Upstream = state.upstream
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_event: Optional[asyncio.Event] = None
        # Per-account boot identity: a shared bootId/seq across aliases would
        # look like one desktop process driving many accounts.
        self._boot_ids: dict[str, str] = {}
        self._seqs: dict[str, int] = {}
        self._started_at = time.time()
        self._analytics_device_ids: dict[str, str] = {}

    def _boot_id(self, alias: str) -> str:
        boot = self._boot_ids.get(alias)
        if boot is None:
            boot = secrets.token_hex(6)
            self._boot_ids[alias] = boot
        return boot

    def _analytics_device(self, alias: str) -> str:
        """Per-account analytics UUID (desktop ``device.json`` shape).

        Official desktops keep one install-wide UUID. A relay serving many
        accounts on one process must not reuse that UUID across accounts —
        shared analytics ids are a cheap multi-account cluster key. Each
        alias therefore gets its own stable UUID, persisted in settings.
        """
        cached = self._analytics_device_ids.get(alias)
        if cached:
            return cached
        key = f"{ANALYTICS_DEVICE_SETTING_PREFIX}:{alias}"
        stored = self.state.store.setting(key)
        if stored and stored.strip():
            self._analytics_device_ids[alias] = stored.strip()
            return stored.strip()
        value = str(uuid.uuid4())
        try:
            self.state.store.set_setting(key, value)
        except Exception:  # noqa: BLE001 - telemetry identity is best-effort
            pass
        self._analytics_device_ids[alias] = value
        return value

    def _next_seq(self, alias: str) -> int:
        value = self._seqs.get(alias, 0) + 1
        self._seqs[alias] = value
        return value

    def start(self) -> None:
        """Idempotent bootstrap. The AppState lifespan calls this."""
        if not self.settings.behavior_replay:
            logger.info("behavior replay disabled via MIROFISH_BEHAVIOR_REPLAY")
            return
        if self._stop_event is not None:
            return
        self._stop_event = asyncio.Event()
        asyncio.create_task(self._supervisor())

    async def aclose(self) -> None:
        if self._stop_event is None:
            return
        self._stop_event.set()
        self._stop_event = None
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # --- supervisor ---------------------------------------------------------

    async def _supervisor(self) -> None:
        """Keep one replay task alive per known alias."""
        while self._stop_event is not None and not self._stop_event.is_set():
            aliases = set(self.state.store.aliases())
            for alias in list(self._tasks):
                if alias not in aliases:
                    await self._drop_alias(alias)
            for alias in aliases:
                if alias not in self._tasks:
                    self._spawn(alias)
            try:
                await asyncio.wait_for(self._stop_event.wait(), 10.0)
            except TimeoutError:
                pass

    def _spawn(self, alias: str) -> None:
        task = asyncio.create_task(self._run_alias(alias))
        task.add_done_callback(lambda _: self._tasks.pop(alias, None))
        self._tasks[alias] = task
        logger.debug("behavior replay task started: account=%s", alias)

    async def _drop_alias(self, alias: str) -> None:
        task = self._tasks.pop(alias, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.debug("behavior replay task dropped: account=%s", alias)

    # --- per-account loop ---------------------------------------------------

    async def _run_alias(self, alias: str) -> None:
        """One account's replay loop: telemetry, roster, update checks."""
        # Spread first sends across the interval so a relay restart does not
        # synchronize every account.
        next_events = time.monotonic() + _jitter_seconds(
            EVENTS_MIN_SECONDS, EVENTS_MAX_SECONDS)
        next_roster = time.monotonic() + _jitter_seconds(
            ROSTER_MIN_SECONDS, ROSTER_MAX_SECONDS)
        next_update = time.monotonic() + _jitter_seconds(
            UPDATE_MIN_SECONDS, UPDATE_MAX_SECONDS)

        while self._stop_event is not None and not self._stop_event.is_set():
            if alias not in self.state.store.aliases():
                return

            if self._paused(alias):
                await self._wait(PAUSE_CHECK_SECONDS)
                continue

            now = time.monotonic()
            if now >= next_events:
                await self._send_events(alias)
                next_events = now + _jitter_seconds(
                    EVENTS_MIN_SECONDS, EVENTS_MAX_SECONDS)
            elif now >= next_roster:
                await self._send_roster(alias)
                next_roster = now + _jitter_seconds(
                    ROSTER_MIN_SECONDS, ROSTER_MAX_SECONDS)
            elif now >= next_update:
                await self._send_update_check(alias)
                next_update = now + _jitter_seconds(
                    UPDATE_MIN_SECONDS, UPDATE_MAX_SECONDS)

            await self._wait(1.0)

    def _paused(self, alias: str) -> bool:
        """Sleep = panel switch; fuse = shared-quota / 429 cooldown or a park."""
        return (self.state.account_disabled(alias)
                or self.state.account_parked(alias)
                or self.state.exhausted_cooldown(alias) > 0.0)

    async def _wait(self, seconds: float) -> None:
        if self._stop_event is None:
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), seconds)
        except TimeoutError:
            pass

    # --- replays ------------------------------------------------------------

    async def _send_events(self, alias: str) -> None:
        device_id = self._analytics_device(alias)
        try:
            user_id = self.state.store.row(alias)["user_id"]
        except Exception:  # noqa: BLE001 - missing account mid-delete
            user_id = None
        boot_id = self._boot_id(alias)
        seq = self._next_seq(alias)
        event = _heartbeat_event(
            event_id=f"{device_id}:{boot_id}:{seq}",
            boot_id=boot_id,
            seq=seq,
            device_id=device_id,
            user_id=user_id,
            settings=self.settings,
            uptime_sec=max(0, int(time.time() - self._started_at)),
        )
        body = _telemetry_envelope(device_id, [event])
        headers = [
            ("content-type", "application/json"),
            ("content-encoding", "gzip"),
            ("authorization", "Bearer " + self.state.store.credentials(alias)[0]),
            ("accept-encoding", "identity"),
            ("content-length", str(len(body))),
        ]
        url = self.settings.relay_base + "/events"
        try:
            response = await self.state.with_proxy(
                alias,
                lambda proxy_url: self.upstream.send_explicit(
                    "POST", url, headers, body, proxy_url=proxy_url, alias=alias))
            await response.aclose()
            if response.status_code >= 400:
                logger.debug("behavior /events rejected: account=%s status=%s",
                             alias, response.status_code)
        except Exception as exc:  # noqa: BLE001 - a dropped telemetry ping must not kill the task
            logger.debug("behavior /events failed: account=%s %s", alias, exc)

    async def _send_roster(self, alias: str) -> None:
        try:
            await self.state.with_proxy(
                alias,
                lambda proxy_url: self.upstream.signed_json(
                    alias, "GET", "/v1/model-roster", proxy_url=proxy_url))
        except Exception as exc:  # noqa: BLE001 - roster failure must not kill the task
            logger.debug("behavior /v1/model-roster failed: account=%s %s", alias, exc)

    async def _send_update_check(self, alias: str) -> None:
        headers = [
            ("Accept", "application/json"),
            ("User-Agent", "mirasim-desktop/" + self.settings.mirasim_client_version),
            ("accept-encoding", "identity"),
        ]
        url = _update_url(self.settings)
        try:
            response = await self.state.with_proxy(
                alias,
                lambda proxy_url: self.upstream.send_explicit(
                    "GET", url, headers, b"", proxy_url=proxy_url, alias=alias))
            await response.aclose()
        except Exception as exc:  # noqa: BLE001 - update check failure must not kill the task
            logger.debug("behavior update check failed: account=%s %s", alias, exc)
