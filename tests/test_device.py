import base64
import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from mirofish.device import DeviceSigner


def test_device_signer_persists_identity_and_verifiable_signature(state):
    signer = DeviceSigner(state.store, "work", "0.0.220")
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

    assert headers["x-mirasim-client"] == "0.0.220"

    reloaded = DeviceSigner(state.store, "work", "0.0.220")
    assert reloaded.device_id == signer.device_id
    assert reloaded.public_key == signer.public_key
