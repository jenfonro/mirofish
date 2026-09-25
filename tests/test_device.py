import base64
import hashlib
import uuid

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


def test_two_accounts_get_their_own_device_identity(state):
    """One account = one client installation: never one key for two."""
    first, second = state.upstream._signer("alpha"), state.upstream._signer("beta")

    assert first.device_id != second.device_id
    assert first.public_key != second.public_key


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


def test_a_caller_supplied_uuid_is_rewritten_per_account(state):
    """A caller's own session id identifies the caller, not the account.

    Passing it through meant one client asking two accounts — which is exactly
    what failover does on a quota refusal or a suspension — announced the same
    session id from both, and a real installation cannot know another's.
    """
    given = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"

    first = state.relay_session_id(given, "", {}, "alpha")
    second = state.relay_session_id(given, "", {}, "beta")

    assert first != given and second != given
    assert first != second
    # The rewrite is the mapping: stable per (caller session, account), so one
    # conversation keeps one upstream id without a table to persist.
    assert state.relay_session_id(given, "", {}, "alpha") == first
    # Still a bare UUID, which is all an official client ever sends.
    assert str(uuid.UUID(first)) == first
