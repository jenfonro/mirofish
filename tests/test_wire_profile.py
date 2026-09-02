"""Wire-order measurements for the HTTP/1.1 request head.

A raw loopback server records exactly what the relay writes to the socket, so
these tests see the serializer's output rather than the HTTPX request model.
The official clients end every request with ``Host`` and ``Connection``; h11
would move ``Host`` to the front unless ``mirofish.wire`` is installed.
"""

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from mirofish.api.state import AppState
from mirofish.upstream import CLAUDE_AGENT_SYSTEM_MARKER
from mirofish.wire import (install_profile_header_order,
                           profile_header_order_installed)
from tests.conftest import add_account
from tests.test_request_profile import _body as captured_messages_body
from tests.test_request_profile import _headers as captured_messages_headers

FIXTURE_DIR = Path(__file__).parent / "fixtures/request_profiles"


class _RawServer:
    """Answers every request with canned JSON and keeps the raw request heads."""

    def __init__(self) -> None:
        self.heads: list[bytes] = []

    async def handle(self, reader, writer) -> None:
        try:
            while True:
                try:
                    head = await reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    return
                length = re.search(rb"content-length:\s*(\d+)", head, re.I)
                if length:
                    await reader.readexactly(int(length.group(1)))
                self.heads.append(head)
                path = head.split(b" ", 2)[1]
                if path.startswith(b"/v1/device/session"):
                    payload = {"ticket": "device-ticket", "expiresIn": 900}
                elif path.startswith(b"/v1/models"):
                    payload = {"data": []}
                else:
                    payload = {"content": [], "usage": {}}
                body = json.dumps(payload).encode()
                writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                             b"content-length: %d\r\n\r\n" % len(body) + body)
                await writer.drain()
        finally:
            writer.close()


def _wire_names(head: bytes) -> list[str]:
    lines = head.decode("latin1").split("\r\n")
    return [line.partition(":")[0] for line in lines[1:] if line]


def _golden_names(fixture: str) -> list[str]:
    profile = json.loads((FIXTURE_DIR / fixture).read_text())
    return [entry["name"] for entry in profile["headers"]]


def test_profile_header_order_installs_once():
    assert install_profile_header_order() is True
    assert install_profile_header_order() is True
    assert profile_header_order_installed() is True


async def test_wire_order_matches_the_captured_profiles(settings):
    server = _RawServer()
    listener = await asyncio.start_server(server.handle, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    settings.relay_base = f"http://127.0.0.1:{port}"
    state = AppState(settings)
    try:
        add_account(state, "work")
        await state.upstream.signed_json("work", "GET", "/v1/models")
        session_id = "0f20cf48-c292-42e9-a99e-994511307deb"
        payload = json.loads(captured_messages_body())
        payload["system"][1]["text"] = CLAUDE_AGENT_SYSTEM_MARKER
        await state.upstream.messages(
            "work", payload,
            request_headers=httpx.Headers(captured_messages_headers(session_id)),
            session_id=session_id, beta=True)
    finally:
        await state.aclose()
        listener.close()
        await listener.wait_closed()

    device, models, messages = server.heads
    assert device.startswith(b"POST /v1/device/session HTTP/1.1\r\n")
    assert models.startswith(b"GET /v1/models HTTP/1.1\r\n")
    assert messages.startswith(b"POST /v1/messages?beta=true HTTP/1.1\r\n")
    # Byte-level field order and casing equal the capture: Host and Connection
    # last, Host not hoisted to the front by the serializer.
    assert _wire_names(device) == _golden_names("device_session_official.json")
    assert _wire_names(models) == _golden_names("models_official.json")
    assert _wire_names(messages) == _golden_names("messages_beta_official.json")
    assert messages.rstrip(b"\r\n").endswith(
        b"Host: 127.0.0.1:%d\r\nConnection: keep-alive" % port)
    # The serializer adds nothing of its own.
    for head in server.heads:
        assert head.count(b"\r\n\r\n") == 1
        assert b"transfer-encoding" not in head.lower()
        assert b"user-agent: python-httpx" not in head.lower()


@pytest.mark.parametrize("fixture", sorted(
    path.name for path in FIXTURE_DIR.glob("*_official.json")))
def test_every_captured_profile_ends_with_host_and_connection(fixture):
    names = _golden_names(fixture)
    assert names[-2:] == ["Host", "Connection"]
