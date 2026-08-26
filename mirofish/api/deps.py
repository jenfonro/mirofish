"""Request-level helpers: local proxy-key auth and safe JSON body reading."""

from __future__ import annotations

import io
import json
import secrets
import zlib
from typing import Any

from fastapi import Request

from ..errors import RelayError
from .state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.relay


def require_auth(request: Request) -> None:
    """Accept X-Mirofish-Proxy-Key, X-Api-Key, or Authorization: Bearer so
    standard Anthropic/OpenAI clients can authenticate without custom headers."""
    state = get_state(request)
    supplied = (
        request.headers.get("X-Mirofish-Proxy-Key", "")
        or request.headers.get("X-Api-Key", "")
        or request.headers.get("Authorization", "").removeprefix("Bearer ")
           .removeprefix("bearer ").strip()
    )
    if not supplied or not secrets.compare_digest(supplied, state.proxy_key):
        raise RelayError("invalid local proxy key", 401)


_DECODE_CHUNK = 64 * 1024


def _inflate_limited(body: bytes, wbits: int, limit: int) -> bytes:
    decoder = zlib.decompressobj(wbits)
    output = decoder.decompress(body, limit + 1)
    if len(output) > limit or decoder.unconsumed_tail:
        raise RelayError("decompressed request body too large", 413)
    output += decoder.flush(limit + 1 - len(output))
    if len(output) > limit:
        raise RelayError("decompressed request body too large", 413)
    if not decoder.eof or decoder.unused_data:
        raise RelayError("request body has invalid compressed framing", 400)
    return output


def _brotli_limited(body: bytes, limit: int) -> bytes:
    """Decode Brotli without ever allocating past ``limit``.

    ``Decompressor.process`` expands its whole input in one call, so bounding
    the *input* chunk size is not a bound at all -- a single small chunk can
    materialise hundreds of megabytes.  ``output_buffer_limit`` is the only
    real bound; once it is hit the decoder stops accepting input and must be
    drained with empty calls until ``can_accept_more_data`` returns True.
    """
    import brotli

    decoder = brotli.Decompressor()
    if not hasattr(decoder, "can_accept_more_data"):
        raise RelayError(
            "content encoding br is unavailable in this installation", 415)
    output = bytearray()
    offset = 0
    while True:
        budget = limit + 1 - len(output)
        if budget <= 0:
            raise RelayError("decompressed request body too large", 413)
        if decoder.can_accept_more_data():
            chunk = body[offset:offset + _DECODE_CHUNK]
            offset += len(chunk)
        else:
            chunk = b""
        produced = decoder.process(chunk, output_buffer_limit=budget)
        output.extend(produced)
        if len(output) > limit:
            raise RelayError("decompressed request body too large", 413)
        if not chunk and not produced and not decoder.can_accept_more_data():
            # The decoder can neither accept input nor drain further; refuse
            # rather than spin on empty calls.
            raise RelayError("request body has invalid compressed framing", 400)
        if offset >= len(body) and decoder.can_accept_more_data():
            break
    if not decoder.is_finished():
        raise RelayError("request body has invalid compressed framing", 400)
    return bytes(output)


def _zstd_limited(body: bytes, limit: int) -> bytes:
    """Decode Zstandard with both an output bound and framing validation."""
    import zstandard

    # A sized frame declares its output length up front, which rejects the
    # bomb case before a single byte is allocated.  Streaming frames report
    # -1 and fall through to the bounded read below.
    if zstandard.frame_content_size(body) > limit:
        raise RelayError("decompressed request body too large", 413)
    reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(body))
    output = reader.read(limit + 1)
    if len(output) > limit or reader.read(1):
        raise RelayError("decompressed request body too large", 413)
    # stream_reader returns a silent short read for a truncated frame and
    # ignores trailing bytes, so it cannot validate framing on its own.
    # decompressobj reports both, and the check above already bounds what it
    # can allocate.
    decoder = zstandard.ZstdDecompressor().decompressobj()
    decoder.decompress(body)
    if not decoder.eof or decoder.unused_data:
        raise RelayError("request body has invalid compressed framing", 400)
    return output


def _decode_body(body: bytes, content_encoding: str, limit: int) -> bytes:
    encodings = [item.strip().lower() for item in content_encoding.split(",")
                 if item.strip()]
    if not encodings or encodings == ["identity"]:
        return body
    if len(encodings) != 1:
        raise RelayError("multiple content encodings are not supported", 415)
    encoding = encodings[0]
    try:
        if encoding == "gzip":
            return _inflate_limited(body, zlib.MAX_WBITS | 16, limit)
        if encoding == "deflate":
            return _inflate_limited(body, zlib.MAX_WBITS, limit)
        if encoding == "br":
            return _brotli_limited(body, limit)
        if encoding == "zstd":
            return _zstd_limited(body, limit)
    except RelayError:
        raise
    except ImportError as exc:
        raise RelayError(
            f"content encoding {encoding} is unavailable in this installation", 415) from exc
    except Exception as exc:  # decoder-specific error classes differ by package
        raise RelayError("request body has invalid compressed framing", 400) from exc
    raise RelayError("unsupported content encoding: " + encoding, 415)


async def _read_bounded(request: Request, limit: int) -> bytes:
    """Buffer the request body, stopping at the first byte past ``limit``.

    ``request.body()`` materialises the whole upload before any length check
    could run, and a ``Transfer-Encoding: chunked`` request carries no
    Content-Length to pre-check, so stream it instead.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise RelayError("request body too large", 413)
        chunks.append(chunk)
    return b"".join(chunks)


async def read_body(request: Request) -> bytes:
    """Read and, when necessary, decode a bounded request body.

    Mirasim's Codex MITM removes request compression before signing and relaying.
    Doing it here guarantees that Content-Length, the body hash and the bytes
    sent upstream all describe the same representation.
    """
    state = get_state(request)
    length_header = request.headers.get("content-length")
    try:
        if length_header is not None:
            declared = int(length_header)
            if declared < 0:
                raise ValueError
            if declared > state.settings.max_body_bytes:
                raise RelayError("request body too large", 413)
    except ValueError:
        raise RelayError("invalid content-length", 400) from None
    body = await _read_bounded(request, state.settings.max_body_bytes)
    content_encoding = request.headers.get("content-encoding", "")
    if content_encoding:
        body = _decode_body(body, content_encoding, state.settings.max_body_bytes)
    return body


def parse_json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RelayError("request body must be valid JSON", 400) from exc
    if not isinstance(value, dict):
        raise RelayError("request body must be a JSON object", 400)
    return value


async def read_json_body_bytes(request: Request) -> tuple[bytes, dict[str, Any]]:
    body = await read_body(request)
    return body, parse_json_object(body)


async def read_json_body(request: Request) -> dict[str, Any]:
    _, value = await read_json_body_bytes(request)
    return value
