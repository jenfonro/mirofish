"""Chromium-shaped TLS impersonation via curl-impersonate."""

from __future__ import annotations

import socket
import struct
import threading
import asyncio
import gzip
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from mirofish.config import Settings, _normalize_impersonate
from mirofish.errors import RelayError
from mirofish.tls_impersonate import (
    _CurlByteStream,
    ImpersonatingClient,
    _ordered_headers,
    _to_httpx_response,
    impersonation_available,
)
from tests.conftest import RELAY_BASE

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
            ("x-mirasim-client", "0.0.303"),
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


@pytest.mark.parametrize("error,httpx_error", [
    ("Timeout", httpx.TimeoutException), ("ProxyError", httpx.ProxyError),
    ("SSLError", httpx.ConnectError), ("ConnectionError", httpx.ConnectError),
    ("RequestException", httpx.RequestError)])
async def test_curl_errors_have_httpx_types_and_request(monkeypatch, error, httpx_error):
    from curl_cffi.requests import exceptions

    client = ImpersonatingClient("chrome136", None, 1)
    request = httpx.Request("POST", RELAY_BASE + "/v1/messages", content=b"{}")
    monkeypatch.setattr(client._session, "request", AsyncMock(
        side_effect=getattr(exceptions, error)("test failure")))
    try:
        with pytest.raises(httpx_error) as failure:
            await client.send(request)
        assert failure.value.request is request
    finally:
        await client.aclose()


@pytest.mark.parametrize("proxy,network", [("direct", False), ("http://exit:8080", True)])
async def test_curl_failure_reaches_normal_relay_network_error(state, monkeypatch, proxy, network):
    from curl_cffi.requests.exceptions import SSLError

    state.settings.tls_impersonate = "chrome136"
    client = await state.upstream.client(proxy, "work")
    monkeypatch.setattr(client._session, "request", AsyncMock(side_effect=SSLError("bad cert")))
    with pytest.raises(RelayError) as failure:
        await state.upstream.json("GET", RELAY_BASE, "/test", proxy_url=proxy, alias="work")
    assert failure.value.status == 502
    assert failure.value.data["proxy_network"] is network


async def test_curl_stream_errors_close_once_and_translate():
    from curl_cffi.requests.exceptions import Timeout

    async def chunks():
        yield b"first"
        raise Timeout("read timed out")

    source = SimpleNamespace(aiter_content=chunks, aclose=AsyncMock())
    request = httpx.Request("GET", RELAY_BASE)
    stream = _CurlByteStream(source, request)
    chunks_seen = []
    with pytest.raises(httpx.ReadTimeout) as failure:
        async for chunk in stream:
            chunks_seen.append(chunk)
    await stream.aclose()
    assert failure.value.request is request
    assert chunks_seen == [b"first"]
    source.aclose.assert_awaited_once()


async def test_curl_close_cancels_idle_stream_without_waiting_for_eof():
    async def idle():
        await asyncio.Event().wait()

    task = asyncio.create_task(idle())
    await asyncio.sleep(0)

    async def close():
        await task

    source = SimpleNamespace(astream_task=task, quit_now=asyncio.Event(),
                             aclose=AsyncMock(side_effect=close))
    stream = _CurlByteStream(source, httpx.Request("GET", RELAY_BASE))
    await asyncio.wait_for(stream.aclose(), 1)
    await stream.aclose()
    assert task.cancelled()
    assert source.quit_now.is_set()
    source.aclose.assert_awaited_once()


async def test_facade_preserves_request_cookies_headers_and_raw_compression():
    request = httpx.Request("GET", RELAY_BASE)
    compressed = gzip.compress(b'{"ok":true}')

    async def chunks():
        yield compressed

    source = SimpleNamespace(
        status_code=200, content=compressed,
        headers=httpx.Headers([("content-encoding", "gzip"),
                               ("set-cookie", "a=1; Path=/"), ("set-cookie", "b=2; Path=/")]),
        aiter_content=chunks, aclose=AsyncMock())
    response = _to_httpx_response(source, request=request, stream=True)
    assert response.request is request
    assert b"".join([part async for part in response.aiter_raw()]) == compressed
    jar = httpx.Cookies()
    jar.extract_cookies(response)
    assert jar.get("a") == "1" and jar.get("b") == "2"
    buffered = _to_httpx_response(source, request=request, stream=False)
    assert buffered.content == b'{"ok":true}'


async def test_real_curl_has_no_hidden_cookie_jar_or_proxy_fallback(monkeypatch):
    """Only loopback sockets, even with bogus process proxy environment."""
    seen = []

    async def handle(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        seen.append(request.lower())
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                     b"Set-Cookie: hidden=bad; Path=/\r\nConnection: close\r\n\r\n{}")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    client = ImpersonatingClient("chrome136", "direct", 2)
    try:
        for _ in range(2):
            response = await client.send(httpx.Request("GET", url))
            assert response.status_code == 200
        response = await client.send(httpx.Request("GET", url, headers={"cookie": "explicit=ok"}))
        assert response.status_code == 200
        assert len(client._session.cookies) == 0
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()
    assert b"\r\ncookie:" not in seen[0] and b"\r\ncookie:" not in seen[1]
    assert b"\r\ncookie: explicit=ok\r\n" in seen[2]
    assert b"hidden=bad" not in seen[2]
