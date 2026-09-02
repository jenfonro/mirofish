import base64
import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

from mirofish.device import (DEVICE_KEY_ALIAS, DEVICE_KEY_KIND, DeviceSigner,
                             metadata_digest)


def test_device_signer_persists_identity_and_verifiable_signature(state):
    signer = DeviceSigner(state.store, "0.0.228", ("work",))
    body = b'{"hello":"world"}'
    meta = {"x-mirasim-agent": "claude", "x-mirasim-session": "s-1"}
    headers = signer.headers("POST", "/v1/messages", body,
                             credential="ticket-value", meta=meta)

    public_der = base64.b64decode(signer.public_key)
    public = serialization.load_der_public_key(public_der)
    assert isinstance(public, Ed25519PublicKey)
    expected_id = base64.urlsafe_b64encode(
        hashlib.sha256(signer.public_key.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")[:22]
    assert signer.device_id == expected_id

    # mrs-sig-v2 binds the device id, client version, credential and relay
    # metadata in addition to the body.
    payload = "\n".join((
        "mrs-sig-v2", "POST", "/v1/messages", headers["x-mirasim-ts"],
        headers["x-mirasim-nonce"], signer.device_id, "0.0.228",
        hashlib.sha256(b"ticket-value").hexdigest(),
        hashlib.sha256(
            b"x-mirasim-agent:claude\nx-mirasim-session:s-1").hexdigest(),
        hashlib.sha256(body).hexdigest(),
    )).encode("utf-8")
    signature = base64.urlsafe_b64decode(
        headers["x-mirasim-sig"] + "=" * (-len(headers["x-mirasim-sig"]) % 4))
    public.verify(signature, payload)

    assert headers["x-mirasim-client"] == "0.0.228"

    reloaded = DeviceSigner(state.store, "0.0.228", ("another-account",))
    assert reloaded.device_id == signer.device_id
    assert reloaded.public_key == signer.public_key


def test_device_signer_migrates_one_legacy_account_key(state):
    legacy = Ed25519PrivateKey.generate()
    pem = legacy.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    state.store.vault.put("work", DEVICE_KEY_KIND, pem)

    signer = DeviceSigner(state.store, "0.0.228", ("work",))
    public_key = signer.public_key

    assert state.store.vault.get(DEVICE_KEY_ALIAS, DEVICE_KEY_KIND) == pem
    assert public_key == base64.b64encode(legacy.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )).decode("ascii")


def test_device_id_shape_is_identical_signed_and_unsigned(state):
    """One installation, one device id.

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


def test_metadata_digest_is_order_independent_and_empty_for_no_meta():
    # The client sorts by header name, so header emission order cannot change
    # the signature.
    assert metadata_digest({"b": "2", "a": "1"}) == metadata_digest({"a": "1", "b": "2"})
    assert metadata_digest({"a": "1", "b": "2"}) == hashlib.sha256(
        b"a:1\nb:2").hexdigest()
    # No metadata contributes an empty field, not the hash of an empty string.
    assert metadata_digest({}) == ""
