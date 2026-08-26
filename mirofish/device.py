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
from collections.abc import Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from .errors import RelayError
from .store import Store

DEVICE_KEY_KIND = "device_private_key"
DEVICE_KEY_ALIAS = "mirasim-installation"
SIG_VERSION = "mrs-sig-v1"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


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

    def headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        """Create the ``mrs-sig-v1`` headers for an exact request body."""
        self._ensure_identity()
        timestamp = str(int(time.time() * 1000))
        nonce = _base64url(secrets.token_bytes(12))
        body_hash = hashlib.sha256(body).hexdigest()
        signing_payload = "\n".join((
            SIG_VERSION,
            method.upper(),
            path,
            timestamp,
            nonce,
            body_hash,
        ))
        signature = _base64url(
            self._load_or_create().sign(signing_payload.encode("utf-8")))
        return {
            "x-mirasim-device": self.device_id,
            "x-mirasim-ts": timestamp,
            "x-mirasim-nonce": nonce,
            "x-mirasim-sig": signature,
            "x-mirasim-client": self.client_version,
        }
