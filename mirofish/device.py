"""Mirasim relay installation identity and request signatures.

The model relay accepts an account bearer token for control-plane calls, but
model traffic normally uses a short-lived device ticket and an Ed25519
signature over the exact request body.  The official desktop keeps one device
key per installation, not one per signed-in account.  This module mirrors that
boundary and migrates one legacy per-account key when upgrading an existing
relay installation.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from collections.abc import Iterable, Mapping, Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from .errors import RelayError
from .store import Store

DEVICE_KEY_KIND = "device_private_key"
DEVICE_KEY_ALIAS = "mirasim-installation"

# 0.0.272 introduced the relay's versioned signing envelope.  Keep the old
# name exported as well: installations pinned to an older relay build can
# still be used by passing an older client version to ``DeviceSigner``.
SIG_VERSION = "mrs-sig-v2"
LEGACY_SIG_VERSION = "mrs-sig-v1"
V2_CLIENT_VERSION = (0, 0, 272)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _version_tuple(value: str) -> tuple[int, ...] | None:
    """Return the numeric parts of a dotted client version, if well formed."""
    parts = str(value or "").strip().split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _uses_v2(client_version: str) -> bool:
    """Whether a client build speaks the 0.0.272 signing protocol."""
    version = _version_tuple(client_version)
    # Unknown/non-numeric versions are treated as current.  This is safer for
    # private relay builds that append a label (and avoids silently emitting a
    # protocol the current upstream no longer accepts).
    return version is None or version >= V2_CLIENT_VERSION


def uses_v2(client_version: str) -> bool:
    """Public protocol-version predicate used by the relay request builder.

    Keeping this decision in one place is important: a legacy client profile
    must not accidentally receive the 0.0.272 encrypted-header envelope, while
    a private build with a suffix (for example ``0.0.272+local``) should still
    use the current protocol.
    """
    return _uses_v2(client_version)


def canonical_metadata(
        metadata: Mapping[str, str] | Iterable[tuple[str, str]] | None,
) -> str:
    """Build the v2 metadata string before its SHA-256 digest.

    The Rust crypto core lowercases header names, sorts them lexicographically,
    and joins each pair as ``name:value`` with a newline between pairs.  Empty
    values are omitted by the desktop wrapper before they reach the core.
    ``dict``/ordered pairs are accepted so callers can preserve the wire
    header order separately from this canonical representation.
    """
    if metadata is None:
        return ""
    items = metadata.items() if isinstance(metadata, Mapping) else metadata
    normalized: list[tuple[str, str]] = []
    for raw_name, raw_value in items:
        # Header values arrive as text from HTTPX/Node.  Do not silently turn
        # arbitrary Python objects into a different signed representation: an
        # accidental ``None``/integer here should fail before a request leaves
        # the process.  ``None`` is retained as the one convenient spelling of
        # an omitted metadata value because the desktop wrapper skips it.
        if raw_name is None:
            raise ValueError("Mirasim signature metadata has an invalid name")
        name = str(raw_name).lower()
        value = "" if raw_value is None else str(raw_value)
        if not name or not value:
            continue
        if "\x00" in name or "\x00" in value:
            raise ValueError("Mirasim signature metadata contains NUL")
        # The native canonicalizer emits one ``name:value`` record per line;
        # embedded line breaks make it return a null pointer rather than a
        # signature.  Reject them here instead of producing a locally valid
        # Ed25519 signature the relay can never verify.
        if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
            raise ValueError("Mirasim signature metadata contains a line break")
        normalized.append((name, value))
    normalized.sort(key=lambda pair: pair[0])
    return "\n".join(f"{name}:{value}" for name, value in normalized)


class DeviceSigner:
    """Load or create the installation's persistent Ed25519 identity."""

    def __init__(self, store: Store, client_version: str,
                 legacy_aliases: Sequence[str] = ()) -> None:
        self.store = store
        self.client_version = client_version
        self.legacy_aliases = tuple(dict.fromkeys(legacy_aliases))
        self._private_key: Ed25519PrivateKey | None = None
        self._device_id: str | None = None
        self._public_key: str | None = None
        self._lock = threading.RLock()

    def set_client_version(self, client_version: str) -> None:
        """Update the protocol marker used by subsequently signed requests.

        The Ed25519 identity remains installation-wide; only the advertised
        client build changes when an operator switches between a legacy relay
        profile and the current one.
        """
        with self._lock:
            self.client_version = client_version

    @property
    def uses_v2(self) -> bool:
        """Whether this signer emits the current versioned envelope."""
        return _uses_v2(str(self.client_version or ""))

    @staticmethod
    def _is_missing(exc: RelayError) -> bool:
        return "missing" in str(exc).lower()

    @staticmethod
    def _decode_private_key(pem: str) -> Ed25519PrivateKey:
        try:
            key = serialization.load_pem_private_key(
                pem.encode("ascii"), password=None)
        except (ValueError, TypeError, UnicodeEncodeError) as exc:
            raise RelayError("stored Mirasim device key is invalid", 500) from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise RelayError("stored Mirasim device key is not Ed25519", 500)
        return key

    def _legacy_key(self) -> tuple[str, Ed25519PrivateKey] | None:
        """Find a valid old per-account key to preserve the existing device id."""
        aliases = tuple(dict.fromkeys((*self.legacy_aliases, *self.store.aliases())))
        for alias in aliases:
            try:
                pem = self.store.vault.get(alias, DEVICE_KEY_KIND)
            except RelayError as exc:
                if self._is_missing(exc):
                    continue
                raise
            try:
                return pem, self._decode_private_key(pem)
            except RelayError:
                # One damaged legacy account must not prevent migration from a
                # second valid account.  The global slot, once written, remains
                # fail-closed and is never silently replaced.
                continue
        return None

    def _load_or_create(self) -> Ed25519PrivateKey:
        with self._lock:
            if self._private_key is not None:
                return self._private_key
            try:
                pem = self.store.vault.get(DEVICE_KEY_ALIAS, DEVICE_KEY_KIND)
                key = self._decode_private_key(pem)
            except RelayError as exc:
                if not self._is_missing(exc):
                    raise
                migrated = self._legacy_key()
                if migrated is None:
                    key = Ed25519PrivateKey.generate()
                    pem = key.private_bytes(
                        encoding=serialization.Encoding.PEM,
                        format=serialization.PrivateFormat.PKCS8,
                        encryption_algorithm=serialization.NoEncryption(),
                    ).decode("ascii")
                else:
                    pem, key = migrated
                self.store.vault.put(DEVICE_KEY_ALIAS, DEVICE_KEY_KIND, pem)
            self._private_key = key
            return key

    @property
    def device_id(self) -> str:
        self._ensure_identity()
        assert self._device_id is not None
        return self._device_id

    @property
    def public_key(self) -> str:
        self._ensure_identity()
        assert self._public_key is not None
        return self._public_key

    def _ensure_identity(self) -> None:
        key = self._load_or_create()
        if self._device_id is not None and self._public_key is not None:
            return
        public_der = key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        public_b64 = base64.b64encode(public_der).decode("ascii")
        self._public_key = public_b64
        # The relay's device id is derived from the base64 SPKI string, not
        # directly from the DER bytes.
        self._device_id = _base64url(
            hashlib.sha256(public_b64.encode("ascii")).digest())[:22]

    def headers(
            self, method: str, path: str, body: bytes, *,
            credential: str = "",
            metadata: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
            signature_version: str | None = None,
    ) -> dict[str, str]:
        """Create a versioned signature envelope for an exact request body.

        The current client (0.0.272+) signs a newline-delimited canonical
        record.  It includes hashes of the credential, relay metadata, and
        body so secrets never appear in the signed message itself.  Older
        client versions retain the compact v1 body-hash format for backwards
        compatibility with private/legacy relays.
        """
        self._ensure_identity()
        if not isinstance(body, bytes):
            # ``hashlib`` accepts bytearray/memoryview, but the official
            # signer hashes the exact Buffer passed by the HTTP layer.  Make
            # the conversion explicit and deterministic for callers that use
            # those common buffer types, while rejecting text bodies that
            # could otherwise be encoded inconsistently.
            if isinstance(body, (bytearray, memoryview)):
                body = bytes(body)
            else:
                raise TypeError("Mirasim request body must be bytes")
        method_text = str(method)
        path_text = str(path)
        credential_text = "" if credential is None else str(credential)
        client_text = "" if self.client_version is None else str(self.client_version)
        # The native core uses NUL as the context separator.  The desktop
        # wrapper rejects it before entering WASM; doing the same here avoids
        # ambiguous signatures and makes malformed input fail closed.
        for label, value in (("method", method_text), ("path", path_text),
                             ("credential", credential_text),
                             ("client version", client_text)):
            if "\x00" in value:
                raise ValueError(f"Mirasim signature {label} contains NUL")
        timestamp = str(int(time.time() * 1000))
        nonce = _base64url(secrets.token_bytes(12))
        version = signature_version or (
            SIG_VERSION if _uses_v2(client_text) else LEGACY_SIG_VERSION)
        if version == LEGACY_SIG_VERSION:
            signing_payload = "\n".join((
                LEGACY_SIG_VERSION,
                method_text.upper(),
                path_text,
                timestamp,
                nonce,
                hashlib.sha256(body).hexdigest(),
            ))
        elif version == SIG_VERSION:
            metadata_text = canonical_metadata(metadata)
            if "\x00" in timestamp or "\x00" in nonce:
                # Randomly generated values cannot contain NUL, but retain the
                # same explicit invariant as the native wrapper if these hooks
                # are ever made injectable for tests.
                raise ValueError("Mirasim signature context contains NUL")
            signing_payload = "\n".join((
                SIG_VERSION,
                method_text.upper(),
                path_text,
                timestamp,
                nonce,
                self.device_id,
                client_text,
                hashlib.sha256(credential_text.encode("utf-8")).hexdigest(),
                hashlib.sha256(metadata_text.encode("utf-8")).hexdigest(),
                hashlib.sha256(body).hexdigest(),
            ))
        else:
            raise ValueError(f"unsupported Mirasim signature version: {version}")
        signature = _base64url(
            self._load_or_create().sign(signing_payload.encode("utf-8")))
        result = {
            "x-mirasim-device": self.device_id,
            "x-mirasim-ts": timestamp,
            "x-mirasim-nonce": nonce,
            "x-mirasim-sig": signature,
        }
        # ``gj()`` in the desktop signer returns the client marker only when
        # it is truthy; omitting it is observably different from sending an
        # empty header (and lets private relay builds opt out cleanly).
        if client_text:
            result["x-mirasim-client"] = client_text
        return result
