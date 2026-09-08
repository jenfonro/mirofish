import base64
import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

from mirofish.device import (INSTALLATION_KEY_ALIAS, DEVICE_KEY_KIND,
                             DeviceSigner)


def test_device_signer_persists_identity_and_verifiable_signature(state):
    signer = DeviceSigner(state.store, "0.0.228", ("work",))
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

    reloaded = DeviceSigner(state.store, "0.0.228", alias="")
    assert reloaded.device_id == signer.device_id
    assert reloaded.public_key == signer.public_key


def test_each_account_gets_its_own_device_identity(state):
    """One device driving every account is what relates them upstream.

    26 accounts were suspended within ten seconds of each other while the
    request rate was at a low — a shared identifier, not rate limiting. Each
    account signs as its own installation instead.
    """
    first = DeviceSigner(state.store, "0.0.272", alias="alpha")
    second = DeviceSigner(state.store, "0.0.272", alias="beta")

    assert first.device_id != second.device_id
    assert first.public_key != second.public_key
    # Each identity is stable across reloads.
    assert DeviceSigner(state.store, "0.0.272", alias="alpha").device_id \
        == first.device_id


def test_an_existing_account_keeps_the_identity_it_has_been_using(state):
    """Rotating every account at once would present the upstream with a pool
    of accounts that all changed device on the same day. An account inherits
    the installation key it has signed with until it is logged in again."""
    shared = Ed25519PrivateKey.generate()
    pem = shared.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    state.store.vault.put(INSTALLATION_KEY_ALIAS, DEVICE_KEY_KIND, pem)

    signer = DeviceSigner(state.store, "0.0.272", alias="work")

    assert signer.public_key == base64.b64encode(
        shared.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )).decode("ascii")
    # It is copied into the account's own slot, so a later rotation of one
    # account cannot disturb another.
    assert state.store.vault.get("work", DEVICE_KEY_KIND) == pem


def test_rotating_gives_an_account_a_fresh_identity(state):
    """A login is the one moment a new device is expected — and the only way
    an account leaves the shared identity behind."""
    signer = DeviceSigner(state.store, "0.0.272", alias="work")
    before = signer.device_id

    after = signer.rotate()

    assert after != before
    assert signer.device_id == after
    assert DeviceSigner(state.store, "0.0.272", alias="work").device_id == after


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


async def test_each_account_gets_its_own_connection_pool(state):
    """Two accounts must not share a TLS connection.

    Each account is meant to look like its own client installation, and
    interleaving several accounts' bearers over one connection is the opposite
    of that — a real installation opens its own.
    """
    first = await state.upstream.client(None, "alpha")
    second = await state.upstream.client(None, "beta")

    assert first is not second
    # Stable per account, so pooling still works within one account.
    assert await state.upstream.client(None, "alpha") is first


def test_the_same_prompt_on_two_accounts_gets_different_session_ids(state):
    """The upstream session id is content-derived, so without the account in
    the hash two accounts answering the same prompt sent the same id — which
    says they are one client."""
    payload = {"messages": [{"role": "user", "content": "hi"}]}

    first = state.relay_session_id("", "", payload, "alpha")
    second = state.relay_session_id("", "", payload, "beta")

    assert first != second
    # Still deterministic per account, which is what affinity needs.
    assert state.relay_session_id("", "", payload, "alpha") == first


def test_a_caller_supplied_uuid_session_is_still_passed_through(state):
    """A real client's own session id belongs to that client, not to us."""
    given = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"

    assert state.relay_session_id(given, "", {}, "alpha") == given
    assert state.relay_session_id(given, "", {}, "beta") == given
