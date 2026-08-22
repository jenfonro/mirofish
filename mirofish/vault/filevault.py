"""Encrypted-at-rest credential file for Docker/Linux.

Current format (v2): ``v2.<base64url(salt16 || nonce12 || AES-256-GCM ciphertext)>``
with the key derived via scrypt (n=2**14, r=8, p=1) from MIROFISH_MASTER_KEY.

The legacy v1 format (HMAC-SHA256 keystream + HMAC tag, produced by the
single-file relay) is still readable; the vault transparently rewrites the
file as v2 the first time it decrypts a v1 blob.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import pathlib
import secrets
import threading

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from ..errors import RelayError
from ..validate import alias_value


class FileVault:
    def __init__(self, secrets_path: pathlib.Path) -> None:
        self.secrets_path = secrets_path
        self.master_key = self._load_master_key()
        self.lock = threading.Lock()

    def _load_master_key(self) -> bytes:
        key_file = os.environ.get("MIROFISH_MASTER_KEY_FILE")
        if key_file:
            key_text = pathlib.Path(key_file).read_text(encoding="utf-8").strip()
        else:
            key_text = os.environ.get("MIROFISH_MASTER_KEY", "").strip()
        if not key_text:
            raise RelayError(
                "file credential backend requires MIROFISH_MASTER_KEY "
                "(or MIROFISH_MASTER_KEY_FILE)", 500)
        if len(key_text) < 16:
            raise RelayError("MIROFISH_MASTER_KEY must be at least 16 characters", 500)
        return key_text.encode("utf-8")

    # --- v2: AES-256-GCM -------------------------------------------------

    def _derive_v2(self, salt: bytes) -> bytes:
        return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(self.master_key)

    def _encrypt(self, plaintext: str) -> str:
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(12)
        key = self._derive_v2(salt)
        cipher = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), b"mirofish.v2")
        return "v2." + base64.urlsafe_b64encode(salt + nonce + cipher).decode("ascii")

    def _decrypt_v2(self, encoded: str) -> str:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        salt, nonce, cipher = raw[:16], raw[16:28], raw[28:]
        key = self._derive_v2(salt)
        return AESGCM(key).decrypt(nonce, cipher, b"mirofish.v2").decode("utf-8")

    # --- v1: legacy single-file relay format ------------------------------

    def _derive_v1(self, salt: bytes) -> tuple[bytes, bytes]:
        enc = hashlib.pbkdf2_hmac("sha256", self.master_key, salt + b"enc", 60000, dklen=32)
        mac = hashlib.pbkdf2_hmac("sha256", self.master_key, salt + b"mac", 60000, dklen=32)
        return enc, mac

    def _stream_v1(self, key: bytes, nonce: bytes, length: int) -> bytes:
        output = bytearray()
        counter = 0
        while len(output) < length:
            output.extend(hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest())
            counter += 1
        return bytes(output[:length])

    def _decrypt_v1(self, encoded: str) -> str:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        salt, nonce, rest = raw[:16], raw[16:32], raw[32:]
        cipher, tag = rest[:-32], rest[-32:]
        enc_key, mac_key = self._derive_v1(salt)
        expected = hmac.new(mac_key, salt + nonce + cipher, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise ValueError("integrity check failed")
        stream = self._stream_v1(enc_key, nonce, len(cipher))
        return bytes(a ^ b for a, b in zip(cipher, stream)).decode("utf-8")

    # --- file IO ----------------------------------------------------------

    def _decrypt(self, blob: str) -> tuple[str, bool]:
        """Return (plaintext, needs_upgrade)."""
        try:
            version, encoded = blob.split(".", 1)
            if version == "v2":
                return self._decrypt_v2(encoded), False
            if version == "v1":
                return self._decrypt_v1(encoded), True
            raise ValueError("unsupported version")
        except Exception as exc:
            raise RelayError("could not decrypt secrets file (wrong master key?)", 500) from exc

    def _read_all(self) -> dict[str, dict[str, str]]:
        if not self.secrets_path.exists():
            return {}
        text = self.secrets_path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        decoded, needs_upgrade = self._decrypt(text)
        value = json.loads(decoded)
        data = value if isinstance(value, dict) else {}
        if needs_upgrade:
            self._write_all(data)
        return data

    def _write_all(self, data: dict[str, dict[str, str]]) -> None:
        blob = self._encrypt(json.dumps(data, ensure_ascii=False))
        temp_path = self.secrets_path.with_name(self.secrets_path.name + ".tmp")
        fd = os.open(str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(blob + "\n")
            os.replace(temp_path, self.secrets_path)
        finally:
            if fd != -1:
                os.close(fd)

    # --- CredentialStore API ----------------------------------------------

    def put(self, alias: str, kind: str, value: str) -> None:
        with self.lock:
            data = self._read_all()
            entry = data.setdefault(alias_value(alias), {})
            entry[kind] = value
            self._write_all(data)

    def get(self, alias: str, kind: str) -> str:
        with self.lock:
            data = self._read_all()
        value = data.get(alias_value(alias), {}).get(kind, "")
        if not value:
            raise RelayError("credential is missing from secrets file", 500)
        return value

    def delete(self, alias: str, kind: str) -> None:
        with self.lock:
            data = self._read_all()
            entry = data.get(alias_value(alias))
            if not entry:
                return
            entry.pop(kind, None)
            if not entry:
                data.pop(alias_value(alias), None)
            self._write_all(data)
