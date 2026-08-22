import base64
import hashlib
import hmac
import json
import secrets

import pytest

from mirofish.errors import RelayError
from mirofish.vault.filevault import FileVault


def legacy_v1_encrypt(master_key: bytes, plaintext: str) -> str:
    """Reproduce the legacy relay's v1 file format for migration tests."""
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(16)
    enc_key = hashlib.pbkdf2_hmac("sha256", master_key, salt + b"enc", 60000, dklen=32)
    mac_key = hashlib.pbkdf2_hmac("sha256", master_key, salt + b"mac", 60000, dklen=32)
    data = plaintext.encode("utf-8")
    stream = bytearray()
    counter = 0
    while len(stream) < len(data):
        stream.extend(hashlib.sha256(enc_key + nonce + counter.to_bytes(8, "big")).digest())
        counter += 1
    cipher = bytes(a ^ b for a, b in zip(data, bytes(stream[:len(data)])))
    tag = hmac.new(mac_key, salt + nonce + cipher, hashlib.sha256).digest()
    return "v1." + base64.urlsafe_b64encode(salt + nonce + cipher + tag).decode("ascii")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("MIROFISH_MASTER_KEY", "unit-test-master-key")
    return FileVault(tmp_path / "secrets.enc")


def test_roundtrip(vault):
    vault.put("work", "access", "token-value")
    assert vault.get("work", "access") == "token-value"
    assert vault.secrets_path.read_text().startswith("v2.")


def test_delete_removes_entry(vault):
    vault.put("work", "access", "a")
    vault.put("work", "refresh", "b")
    vault.delete("work", "access")
    with pytest.raises(RelayError):
        vault.get("work", "access")
    assert vault.get("work", "refresh") == "b"


def test_legacy_v1_migrates_to_v2(vault):
    blob = legacy_v1_encrypt(b"unit-test-master-key",
                             json.dumps({"work": {"access": "legacy-token"}}))
    vault.secrets_path.write_text(blob + "\n")
    assert vault.get("work", "access") == "legacy-token"
    # The read path rewrites the file in the current format.
    assert vault.secrets_path.read_text().startswith("v2.")
    assert vault.get("work", "access") == "legacy-token"


def test_wrong_master_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MIROFISH_MASTER_KEY", "first-master-key-000")
    first = FileVault(tmp_path / "secrets.enc")
    first.put("work", "access", "secret")
    monkeypatch.setenv("MIROFISH_MASTER_KEY", "other-master-key-000")
    second = FileVault(tmp_path / "secrets.enc")
    with pytest.raises(RelayError):
        second.get("work", "access")


def test_master_key_required(tmp_path, monkeypatch):
    monkeypatch.delenv("MIROFISH_MASTER_KEY", raising=False)
    monkeypatch.delenv("MIROFISH_MASTER_KEY_FILE", raising=False)
    with pytest.raises(RelayError):
        FileVault(tmp_path / "secrets.enc")
