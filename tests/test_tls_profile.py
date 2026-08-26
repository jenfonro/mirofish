"""ClientHello measurements for the upstream TLS profile.

The handshake never completes and never leaves the loopback interface: a raw
socket accepts one connection, keeps the first record, and closes. That is
enough to see every field a JA3/JA4 fingerprint is computed from.

These assertions pin the decisions in ``upstream.tls_context`` that are visible
on the wire. They deliberately do not assert a fingerprint hash: the cipher
list, extension order and GREASE values belong to OpenSSL, and this relay's
fidelity to the official client is header- and body-level, not TLS-level.
"""

import socket
import ssl
import struct
import threading

import httpx
import pytest

from mirofish.upstream import tls_context


def _client_hello_fields(data: bytes) -> tuple[list[int], dict[int, bytes]]:
    """Return the extension types in order, plus each extension's raw body."""
    assert data[:1] == b"\x16", "not a TLS handshake record"
    body = data[5:]
    assert body[:1] == b"\x01", "not a ClientHello"
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


def _uint16_list(payload: bytes) -> list[int]:
    """Decode a 16-bit-prefixed vector of 16-bit values."""
    if not payload:
        return []
    size = struct.unpack(">H", payload[:2])[0]
    return [struct.unpack(">H", payload[2 + index:4 + index])[0]
            for index in range(0, size, 2)]


async def _captured_hello(state) -> tuple[list[int], dict[int, bytes]]:
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

    reader = threading.Thread(target=accept_once, daemon=True)
    reader.start()
    client = await state.upstream.client(None)
    with pytest.raises(httpx.HTTPError):
        # localhost rather than the literal address so SNI is exercised.
        await client.get(f"https://localhost:{port}/", timeout=5.0)
    reader.join(5)
    listener.close()
    assert "hello" in captured, "no ClientHello reached the listener"
    return _client_hello_fields(captured["hello"])


async def test_client_hello_keeps_the_alpn_extension(state):
    """The official client sends ALPN too, so removing it would stand out.

    httpcore assigns ``http/1.1`` into whatever context it is given. If a future
    release stopped doing that, extension 16 would silently disappear from every
    request and the hello would no longer resemble the desktop's.
    """
    order, bodies = await _captured_hello(state)

    assert 16 in order
    assert bodies[16] == b"\x00\x09\x08http/1.1"
    assert 0 in order, "SNI must be present"


async def test_client_hello_offers_no_obsolete_tls_versions(state):
    _, bodies = await _captured_hello(state)

    # supported_versions is length-prefixed with a single byte, unlike the
    # 16-bit vectors elsewhere in the hello.
    payload = bodies[43]
    versions = [struct.unpack(">H", payload[1 + index:3 + index])[0]
                for index in range(0, payload[0], 2)]
    assert 0x0304 in versions
    assert not {0x0301, 0x0302} & set(versions)


@pytest.mark.skipif(not hasattr(ssl.SSLContext, "set_groups"),
                    reason="SSLContext.set_groups needs Python 3.13+")
async def test_client_hello_offers_browser_shaped_groups(state):
    """No browser-derived client offers the finite-field ffdhe groups."""
    _, bodies = await _captured_hello(state)
    groups = _uint16_list(bodies[10])

    assert groups[0] == 0x001d, "x25519 must be the preferred group"
    # ffdhe2048/ffdhe3072 are an OpenSSL default, not a browser's.
    assert not {0x0100, 0x0101} & set(groups)


async def test_tls_context_is_shared_and_still_verifies_certificates(state):
    context = tls_context()

    assert context is tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
