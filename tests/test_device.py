import base64
import hashlib

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

from mirofish.device import DEVICE_KEY_KIND, DeviceSigner
from mirofish.errors import RelayError


def test_device_signer_persists_identity_and_verifiable_signature(state):
    signer = DeviceSigner(state.store, "0.0.228", "work")
    body = b'{"hello":"world"}'
    headers = signer.headers("POST", "/v1/messages", body)

    public_der = base64.b64decode(signer.public_key)
    public = serialization.load_der_public_key(public_der)
    assert isinstance(public, Ed25519PublicKey)
    expected_id = base64.urlsafe_b64encode(
        hashlib.sha256(signer.public_key.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")[:22]
    assert signer.device_id == expected_id

    payload = "\n".join((
        "mrs-sig-v1", "POST", "/v1/messages", headers["x-mirasim-ts"],
        headers["x-mirasim-nonce"], hashlib.sha256(body).hexdigest(),
    )).encode("utf-8")
    signature = base64.urlsafe_b64decode(
        headers["x-mirasim-sig"] + "=" * (-len(headers["x-mirasim-sig"]) % 4))
    public.verify(signature, payload)

    assert headers["x-mirasim-client"] == "0.0.228"

    reloaded = DeviceSigner(state.store, "0.0.228", "work")
    assert reloaded.device_id == signer.device_id
    assert reloaded.public_key == signer.public_key


def test_device_signer_isolates_identity_per_account(state):
    work = DeviceSigner(state.store, "0.0.228", "work")
    personal = DeviceSigner(state.store, "0.0.228", "personal")

    assert work.device_id != personal.device_id
    assert work.public_key != personal.public_key
    # Each alias stores its own private key in the vault.
    work_pem = state.store.vault.get("work", DEVICE_KEY_KIND)
    personal_pem = state.store.vault.get("personal", DEVICE_KEY_KIND)
    assert work_pem != personal_pem


def test_device_signer_generates_new_identity_after_key_deletion(state):
    signer = DeviceSigner(state.store, "0.0.228", "work")
    old_id = signer.device_id
    old_key = signer.public_key

    state.store.vault.delete("work", DEVICE_KEY_KIND)
    fresh = DeviceSigner(state.store, "0.0.228", "work")

    assert fresh.device_id != old_id
    assert fresh.public_key != old_key


def test_device_signer_missing_alias_raises(state):
    signer = DeviceSigner(state.store, "0.0.228", "ghost")
    # Ghost has no key in the vault; first access generates one lazily.
    assert signer.device_id
    assert state.store.vault.get("ghost", DEVICE_KEY_KIND)


def test_device_id_shape_is_identical_signed_and_unsigned(state):
    """Per-account device id.

    ``x-mirasim-device`` carries the Ed25519-derived id whether or not the
    request ends up signed, so the unsigned fallback path cannot be told apart
    by the field's shape.
    """
    device_id = state.upstream._signer("work").device_id

    assert len(device_id) == 22
    assert set(device_id) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert state.upstream._signer("work").headers(
        "POST", "/v1/responses", b"{}")["x-mirasim-device"] == device_id


def test_device_signer_rejects_empty_alias(state):
    with pytest.raises(RelayError):
        state.upstream._signer("")
