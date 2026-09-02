"""Mirasim relay-metadata sealing.

Recent Mirasim clients no longer put their ``x-mirasim-*`` request metadata
on the wire in clear text.  They keep the client build marker visible and
seal the rest into ``x-mirasim-enc`` using the public relay key.  The format
is deliberately small and dependency-light so it can be used by both the
Claude and Codex relay paths:

``ephemeral X25519 public key || 12-byte nonce || ChaCha20-Poly1305 data``

The symmetric key is HKDF-SHA256(shared-secret, salt=ephemeral-public,
info=``mrs-seal-v1``).  The HTTP method and canonical pathname are associated
data, which prevents a sealed envelope being copied to another endpoint.
"""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Iterable, Mapping, Sequence

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


SEAL_VERSION = "mrs-seal-v1"
SEAL_HEADER = "x-mirasim-enc"
# Embedded in the official 0.0.272 client.  It is a public key, not a relay
# credential; deployments can override it when the upstream rotates keys.
DEFAULT_SEAL_PUBLIC_KEY = "HlyNMMeGXryasYLJuYQ/9ksCD4AYVVy1zXKAtJdpJn4="

_CLIENT_HEADER = "x-mirasim-client"
_EXCLUDED_HEADERS = frozenset({_CLIENT_HEADER, SEAL_HEADER})
_EPHEMERAL_BYTES = 32
_NONCE_BYTES = 12
_TAG_BYTES = 16
_MIN_SEALED_BYTES = _EPHEMERAL_BYTES + _NONCE_BYTES + _TAG_BYTES
_MAX_METADATA_VALUE = 16 * 1024


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode_seal_public_key(value: str | bytes) -> bytes:
    """Decode and validate the 32-byte relay seal public key.

    The official environment variable uses ordinary padded base64.  URL-safe
    variants are accepted as well, which makes rotating the key less brittle;
    whitespace and any other non-base64 characters are rejected explicitly.
    """
    if isinstance(value, bytes):
        # Internal callers/tests may already have decoded the X25519 key.  A
        # 32-byte value is unambiguous; other byte strings retain the
        # environment-variable (base64 text) interpretation.
        if len(value) == _EPHEMERAL_BYTES:
            return bytes(value)
        try:
            raw_value = value.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("Mirasim seal public key is not valid base64") from exc
    elif isinstance(value, str):
        raw_value = value.strip()
    else:
        raise ValueError("Mirasim seal public key must be base64 text")
    if not raw_value:
        raise ValueError("Mirasim seal public key is empty")
    # ``b64decode(validate=True)`` accepts the URL-safe alphabet only when
    # altchars are supplied.  Padding is optional in environment overrides.
    try:
        decoded = base64.b64decode(
            raw_value + "=" * (-len(raw_value) % 4),
            altchars=b"-_", validate=True)
    except (ValueError, TypeError, base64.binascii.Error, UnicodeError) as exc:
        raise ValueError("Mirasim seal public key is not valid base64") from exc
    if len(decoded) != _EPHEMERAL_BYTES:
        raise ValueError(
            "Mirasim seal public key must decode to 32 bytes "
            f"(got {len(decoded)})")
    return decoded


def _seal(
        public_key: bytes,
        method: str,
        path: str,
        plaintext: bytes,
        *,
        ephemeral_secret: bytes | None = None,
        nonce: bytes | None = None,
) -> bytes:
    """Return one binary ``mrs-seal-v1`` envelope.

    ``ephemeral_secret`` and ``nonce`` are keyword-only test hooks.  Production
    callers leave them unset so every request gets fresh randomness.
    """
    if len(public_key) != _EPHEMERAL_BYTES:
        raise ValueError("Mirasim seal public key must be 32 bytes")
    secret = (secrets.token_bytes(_EPHEMERAL_BYTES)
              if ephemeral_secret is None else bytes(ephemeral_secret))
    if len(secret) != _EPHEMERAL_BYTES:
        raise ValueError("Mirasim seal ephemeral secret must be 32 bytes")
    iv = (secrets.token_bytes(_NONCE_BYTES)
          if nonce is None else bytes(nonce))
    if len(iv) != _NONCE_BYTES:
        raise ValueError("Mirasim seal nonce must be 12 bytes")

    ephemeral = X25519PrivateKey.from_private_bytes(secret)
    ephemeral_public = ephemeral.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(public_key))
    key = HKDF(
        algorithm=hashes.SHA256(), length=32,
        salt=ephemeral_public, info=SEAL_VERSION.encode("ascii"),
    ).derive(shared)
    aad = (SEAL_VERSION + "\n" + method.upper() + "\n" + path).encode("utf-8")
    encrypted = ChaCha20Poly1305(key).encrypt(iv, plaintext, aad)
    return ephemeral_public + iv + encrypted


def seal_metadata(
        metadata: Mapping[str, str], method: str, path: str,
        public_key: str | bytes = DEFAULT_SEAL_PUBLIC_KEY, *,
        ephemeral_secret: bytes | None = None,
        nonce: bytes | None = None) -> str:
    """Serialize and seal an ordered relay metadata mapping.

    Python dictionaries preserve insertion order, matching the object order of
    the official Node client.  Header names are expected to already be lower
    case; callers using :func:`seal_header_pairs` get that normalization.
    """
    if not isinstance(metadata, Mapping) or not metadata:
        raise ValueError("Mirasim seal metadata must be a non-empty mapping")
    clean: dict[str, str] = {}
    for name, value in metadata.items():
        if not isinstance(name, str) or not name:
            raise ValueError("Mirasim seal metadata has an invalid header name")
        if not isinstance(value, str) \
                or len(value) > _MAX_METADATA_VALUE \
                or any(char in value for char in "\r\n\0"):
            raise ValueError("Mirasim seal metadata has an invalid header value")
        if any(char in name for char in "\r\n\0"):
            raise ValueError("Mirasim seal metadata has an invalid header name")
        clean[name.lower()] = value
    plaintext = json.dumps(
        clean, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    binary = _seal(
        decode_seal_public_key(public_key), method, path, plaintext,
        ephemeral_secret=ephemeral_secret, nonce=nonce)
    return _b64url(binary)


def seal_header_pairs(
        headers: Sequence[tuple[str, str]] | Iterable[tuple[str, str]],
        method: str, path: str,
        public_key: str | bytes = DEFAULT_SEAL_PUBLIC_KEY,
) -> list[tuple[str, str]]:
    """Replace relay metadata headers with one ``x-mirasim-enc`` field.

    ``x-mirasim-client`` is intentionally left clear.  Every other
    ``x-mirasim-*`` field is included, including the device/signature fields;
    the latter are excluded only when constructing the *signature* metadata in
    the official client, not when sealing the final request.  Duplicate names
    are coalesced with last-value semantics while retaining the first slot,
    mirroring Node's request-header object.
    """
    pairs = list(headers)
    metadata: dict[str, str] = {}
    metadata_names: set[str] = set()
    for raw_name, raw_value in pairs:
        name = str(raw_name)
        lower = name.lower()
        if not lower.startswith("x-mirasim-") or lower in _EXCLUDED_HEADERS:
            continue
        metadata_names.add(lower)
        metadata[lower] = str(raw_value)
    if not metadata:
        # A caller-supplied envelope must not survive just because it also
        # supplied a stale encrypted field.  Relay callers rebuild this field
        # on every request, so dropping it is the least surprising behavior.
        return [(name, value) for name, value in pairs
                if name.lower() != SEAL_HEADER]

    sealed = seal_metadata(metadata, method, path, public_key)
    result = [
        (name, value) for name, value in pairs
        if name.lower() not in metadata_names and name.lower() != SEAL_HEADER
    ]
    result.append((SEAL_HEADER, sealed))
    return result


def sealed_size(value: str) -> int:
    """Return decoded envelope size, useful for profile validation only."""
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError, base64.binascii.Error) as exc:
        raise ValueError("invalid Mirasim sealed metadata") from exc
    if len(raw) < _MIN_SEALED_BYTES:
        raise ValueError("Mirasim sealed metadata is truncated")
    return len(raw)
