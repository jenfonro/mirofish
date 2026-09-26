"""Chromium-shaped TLS impersonation via curl-impersonate."""

from __future__ import annotations

import socket
import struct
import threading

import httpx
import pytest

from mirofish.config import Settings, _normalize_impersonate
from mirofish.tls_impersonate import (
    ImpersonatingClient,
    _ordered_headers,
    impersonation_available,
)

pytestmark = pytest.mark.skipif(
    not impersonation_available(), reason="curl_cffi extra not installed")


def _client_hello_fields(data: bytes) -> tuple[list[int], dict[int, bytes]]:
    assert data[:1] == b"\x16"
    body = data[5:]
    assert body[:1] == b"\x01"
    offset = 4 + 2 + 32
    offset += 1 + body[offset]
    suites = struct.unpack(">H", body[offset:offset + 2])[0]
    offset += 2 + suites
    offset += 1 + body[offset]
    length = struct.unpack(">H", body[offset:offset + 2])[0]
    offset += 2
    end = offset + length
    order: list[int] = []
    bodies: dict[int, bytes] = {}
    while offset < end:
        kind, size = struct.unpack(">HH", body[offset:offset + 4])
        order.append(kind)
        bodies[kind] = body[offset + 4:offset + 4 + size]
        offset += 4 + size
    return order, bodies


def test_normalize_impersonate():
    assert _normalize_impersonate("") == ""
    assert _normalize_impersonate("off") == ""
    assert _normalize_impersonate("chrome") == "chrome136"
    assert _normalize_impersonate("Chrome136") == "chrome136"
    assert _normalize_impersonate("chrome150") == "chrome150"


def test_ordered_headers_keep_wire_order_and_casing():
    request = httpx.Request(
        "POST",
        "https://relay.mirasim.ai/v1/messages",
        headers=[
            ("x-zzz", "1"),
            ("Authorization", "Bearer t"),
            ("x-mirasim-client", "0.0.367"),
            ("Host", "relay.mirasim.ai"),
        ],
        content=b"{}",
    )
    pairs = _ordered_headers(request)
    names = [name for name, _ in pairs]
    # ``headers.raw`` keeps the caller's order *and* casing; only
    # ``Headers.items()`` lowercases. Golden profiles depend on both.
    assert names[0] == "x-zzz"
    assert "Authorization" in names
    assert names.index("Authorization") > names.index("x-zzz")
    assert "Host" in names


def _is_grease(value: int) -> bool:
    """RFC 8701 GREASE: both bytes equal, low nibble 0xA (0x0A0A…0xFAFA)."""
    return (value & 0x0F0F) == 0x0A0A and (value >> 8) == (value & 0xFF)


async def test_impersonated_https_hello_is_chromium_shaped():
    """GREASE + x25519-first groups + TLS1.3 — not OpenSSL's ffdhe list."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    captured: dict[str, bytes] = {}

    def accept_once() -> None:
        connection, _ = listener.accept()
        try:
            captured["hello"] = connection.recv(65535)
        finally:
            connection.close()
            listener.close()

    reader = threading.Thread(target=accept_once, daemon=True)
    reader.start()
    client = ImpersonatingClient("chrome136", None, 5.0, verify=False)
    request = httpx.Request("GET", f"https://localhost:{port}/")
    with pytest.raises(Exception):
        await client.send(request)
    await client.aclose()
    reader.join(5)

    assert "hello" in captured
    order, bodies = _client_hello_fields(captured["hello"])
    # A GREASE extension is a BoringSSL tell OpenSSL does not emit here.
    assert any(_is_grease(ext) for ext in order[:3])
    assert 0 in order, "SNI must be present"
    assert 16 in order
    # Forced HTTP/1.1 ALPN, matching the desktop's HTTP/1.1 wire.
    assert bodies[16] == b"\x00\x09\x08http/1.1"
    groups = bodies[10]
    # First non-GREASE group is x25519 (0x001d).
    values = [struct.unpack(">H", groups[2 + i:4 + i])[0]
              for i in range(0, struct.unpack(">H", groups[:2])[0], 2)]
    non_grease = [g for g in values if not _is_grease(g)]
    # Modern Chromium leads with X25519MLKEM768 (0x11EC) then x25519 (0x001d).
    assert non_grease[0] in (0x001D, 0x11EC)
    assert 0x001D in non_grease
    assert not {0x0100, 0x0101} & set(values), "no OpenSSL ffdhe groups"


def test_settings_default_has_no_impersonation(settings: Settings):
    assert settings.tls_impersonate == ""
