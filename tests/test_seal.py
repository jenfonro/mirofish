"""0.0.272 protocol units: ``mrs-seal-v1`` envelopes and ``mrs-sig-v2``.

The request-profile goldens pin the wire shape of whole requests; these tests
pin the primitives underneath them and the two operator switches: a malformed
seal key fails closed, and the explicit compatibility switch restores clear
metadata without weakening the signature.
"""

import base64
import hashlib
import json

import httpx
import pytest
import respx
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization

from mirofish.config import Settings
from mirofish.device import (LEGACY_SIG_VERSION, SIG_VERSION, DeviceSigner,
                             canonical_metadata, uses_v2)
from mirofish.seal import (DEFAULT_SEAL_PUBLIC_KEY, SEAL_HEADER,
                           decode_seal_public_key, seal_header_pairs,
                           seal_metadata, sealed_size)
from tests.conftest import RELAY_BASE, add_account
from tests.mirasim_protocol import (SEAL_PUBLIC_KEY, relay_metadata, unseal,
                                    verify_signature)

MESSAGE = {"id": "msg_1", "type": "message", "role": "assistant",
           "content": [{"type": "text", "text": "ok"}],
           "usage": {"input_tokens": 1, "output_tokens": 1}}


def _mock_device_session():
    return respx.post(RELAY_BASE + "/v1/device/session").mock(
        return_value=httpx.Response(200, json={"ticket": "device-ticket",
                                               "expiresIn": 900}))


# --------------------------------------------------------------------------
# seal primitives
# --------------------------------------------------------------------------

def test_seal_roundtrip_is_bound_to_method_and_path():
    metadata = {"x-mirasim-session": "s-1", "x-mirasim-agent": "claude"}
    sealed = seal_metadata(metadata, "POST", "/v1/messages", SEAL_PUBLIC_KEY)

    assert unseal(sealed, "POST", "/v1/messages") == metadata
    assert unseal(sealed, "post", "/v1/messages") == metadata
    with pytest.raises(InvalidTag):
        unseal(sealed, "GET", "/v1/messages")
    with pytest.raises(InvalidTag):
        unseal(sealed, "POST", "/v1/messages?beta=true")
    # The envelope is ephemeral-public || nonce || ciphertext+tag.
    assert sealed_size(sealed) == 32 + 12 + len(
        json.dumps(metadata, separators=(",", ":")).encode()) + 16


def test_seal_uses_fresh_randomness_unless_pinned_by_test_hooks():
    metadata = {"x-mirasim-session": "s-1"}
    first = seal_metadata(metadata, "POST", "/v1/messages", SEAL_PUBLIC_KEY)
    second = seal_metadata(metadata, "POST", "/v1/messages", SEAL_PUBLIC_KEY)
    assert first != second

    pinned = dict(ephemeral_secret=bytes(range(32)), nonce=bytes(12))
    assert seal_metadata(metadata, "POST", "/v1/messages", SEAL_PUBLIC_KEY,
                         **pinned) == \
        seal_metadata(metadata, "POST", "/v1/messages", SEAL_PUBLIC_KEY,
                      **pinned)


def test_seal_metadata_rejects_header_injection_and_empty_envelopes():
    with pytest.raises(ValueError):
        seal_metadata({}, "POST", "/v1/messages", SEAL_PUBLIC_KEY)
    with pytest.raises(ValueError):
        seal_metadata({"x-mirasim-session": "a\r\nx-evil: 1"},
                      "POST", "/v1/messages", SEAL_PUBLIC_KEY)
    with pytest.raises(ValueError):
        seal_metadata({"x-mirasim-session": 7},  # type: ignore[dict-item]
                      "POST", "/v1/messages", SEAL_PUBLIC_KEY)
    with pytest.raises(ValueError):
        seal_metadata({"x-mirasim-session": "x" * (16 * 1024 + 1)},
                      "POST", "/v1/messages", SEAL_PUBLIC_KEY)


def test_decode_seal_public_key_accepts_official_and_urlsafe_forms():
    raw = decode_seal_public_key(DEFAULT_SEAL_PUBLIC_KEY)
    assert len(raw) == 32
    urlsafe = base64.urlsafe_b64encode(raw).decode("ascii")
    assert decode_seal_public_key(urlsafe) == raw
    assert decode_seal_public_key(urlsafe.rstrip("=")) == raw
    assert decode_seal_public_key("  " + DEFAULT_SEAL_PUBLIC_KEY + "\n") == raw
    assert decode_seal_public_key(raw) == raw
    assert decode_seal_public_key(DEFAULT_SEAL_PUBLIC_KEY.encode()) == raw

    for bad in ("", "   ", "not base64!", base64.b64encode(b"short").decode(),
                base64.b64encode(bytes(33)).decode()):
        with pytest.raises(ValueError):
            decode_seal_public_key(bad)
    with pytest.raises(ValueError):
        decode_seal_public_key(None)  # type: ignore[arg-type]


def test_seal_header_pairs_keeps_client_clear_and_drops_stale_envelopes():
    headers = [
        ("content-type", "application/json"),
        ("authorization", "Bearer device-ticket"),
        ("X-Mirasim-Session", "s-1"),
        ("x-mirasim-agent", "first"),
        ("x-mirasim-client", "0.0.272"),
        # A caller-supplied envelope must never survive into the request.
        ("x-mirasim-enc", "stale"),
        ("x-mirasim-agent", "last-wins"),
        ("accept-encoding", "identity"),
    ]
    sealed = seal_header_pairs(headers, "POST", "/v1/messages", SEAL_PUBLIC_KEY)

    assert [name for name, _ in sealed] == [
        "content-type", "authorization", "x-mirasim-client",
        "accept-encoding", SEAL_HEADER]
    envelope = dict(sealed)[SEAL_HEADER]
    assert envelope != "stale"
    assert unseal(envelope, "POST", "/v1/messages") == {
        "x-mirasim-session": "s-1", "x-mirasim-agent": "last-wins"}

    # Nothing to seal: the stale envelope still goes, no new one is minted.
    assert seal_header_pairs(
        [("x-mirasim-client", "0.0.272"), ("x-mirasim-enc", "stale")],
        "POST", "/v1/messages", SEAL_PUBLIC_KEY,
    ) == [("x-mirasim-client", "0.0.272")]


# --------------------------------------------------------------------------
# mrs-sig-v2
# --------------------------------------------------------------------------

@pytest.mark.parametrize("version, expected", [
    ("0.0.228", False), ("0.0.271", False), ("0.0.272", True),
    ("0.1.0", True), ("1.0.0", True),
    # Labelled private builds and unknown strings speak the current protocol.
    ("0.0.272+local", True), ("", True), ("dev", True),
])
def test_uses_v2_switches_at_the_272_build(version, expected):
    assert uses_v2(version) is expected


def test_v2_signature_covers_credential_metadata_and_body(state):
    signer = DeviceSigner(state.store, "0.0.272", ("work",))
    body = b'{"hello":"world"}'
    metadata = {"x-mirasim-session": "s-1", "x-mirasim-agent": "claude",
                "x-mirasim-call": "c-1"}
    headers = signer.headers("POST", "/v1/messages", body,
                             credential="device-ticket", metadata=metadata)
    assert headers["x-mirasim-client"] == "0.0.272"
    public = serialization.load_der_public_key(
        base64.b64decode(signer.public_key))
    signature = base64.urlsafe_b64decode(
        headers["x-mirasim-sig"] + "=" * (-len(headers["x-mirasim-sig"]) % 4))

    def payload(credential=b"device-ticket", meta=metadata, content=body,
                path="/v1/messages"):
        return "\n".join((
            SIG_VERSION, "POST", path, headers["x-mirasim-ts"],
            headers["x-mirasim-nonce"], signer.device_id, "0.0.272",
            hashlib.sha256(credential).hexdigest(),
            hashlib.sha256(canonical_metadata(meta).encode()).hexdigest(),
            hashlib.sha256(content).hexdigest(),
        )).encode()

    public.verify(signature, payload())
    # Metadata order does not matter; its content does.
    public.verify(signature, payload(meta=dict(reversed(metadata.items()))))
    for tampered in (
        payload(credential=b"other-ticket"),
        payload(meta={**metadata, "x-mirasim-agent": "codex"}),
        payload(content=b'{"hello":"tampered"}'),
        payload(path="/v1/messages?beta=true"),
    ):
        with pytest.raises(InvalidSignature):
            public.verify(signature, tampered)
    # The secret itself is never part of the signed record.
    assert b"device-ticket" not in payload()


def test_signer_rejects_nul_and_unknown_versions(state):
    signer = DeviceSigner(state.store, "0.0.272", ("work",))
    with pytest.raises(ValueError):
        signer.headers("POST", "/v1/mes\x00sages", b"{}")
    with pytest.raises(ValueError):
        signer.headers("POST", "/v1/messages", b"{}", credential="a\x00b")
    with pytest.raises(ValueError):
        signer.headers("POST", "/v1/messages", b"{}", signature_version="mrs-sig-v9")
    with pytest.raises(TypeError):
        signer.headers("POST", "/v1/messages", "{}")  # type: ignore[arg-type]
    # An explicit legacy version still produces the compact v1 record.
    legacy = signer.headers("POST", "/v1/messages", b"{}",
                            signature_version=LEGACY_SIG_VERSION)
    public = serialization.load_der_public_key(
        base64.b64decode(signer.public_key))
    public.verify(
        base64.urlsafe_b64decode(
            legacy["x-mirasim-sig"] + "=" * (-len(legacy["x-mirasim-sig"]) % 4)),
        "\n".join((LEGACY_SIG_VERSION, "POST", "/v1/messages",
                   legacy["x-mirasim-ts"], legacy["x-mirasim-nonce"],
                   hashlib.sha256(b"{}").hexdigest())).encode())


# --------------------------------------------------------------------------
# relay behaviour under the two operator switches
# --------------------------------------------------------------------------

@respx.mock
async def test_malformed_seal_key_fails_closed(client, state, auth_headers):
    """A bad key must not quietly turn the envelope back into clear headers."""
    state.settings.mirasim_seal_public_key = "definitely-not-a-key"
    add_account(state, "work")
    _mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=MESSAGE))

    response = await client.post("/v1/messages", headers=auth_headers, json={
        "model": "claude-haiku-4-5", "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert response.status_code == 502
    assert "unable to seal" in response.text
    assert "definitely-not-a-key" not in response.text
    assert route.call_count == 0


@respx.mock
async def test_seal_switch_restores_clear_metadata_but_keeps_v2_signature(
        client, state, auth_headers):
    """``MIROFISH_MIRASIM_SEAL_METADATA=0`` is the explicit legacy clear mode."""
    state.settings.mirasim_seal_metadata = False
    add_account(state, "work")
    _mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=MESSAGE))

    response = await client.post("/v1/messages", headers=auth_headers, json={
        "model": "claude-haiku-4-5", "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert response.status_code == 200
    sent = route.calls.last.request
    assert SEAL_HEADER not in sent.headers
    assert sent.headers["x-mirasim-client"] == "0.0.272"
    assert sent.headers["x-mirasim-agent"] == "claude"
    assert sent.headers["x-mirasim-session"]
    assert relay_metadata(sent) == {
        name.lower(): value for name, value in sent.headers.items()
        if name.lower().startswith("x-mirasim-")}
    verify_signature(state, sent, "/v1/messages")


def test_seal_settings_come_from_the_environment(monkeypatch):
    for name in ("MIROFISH_MIRASIM_SEAL_PUBLIC_KEY", "MIROFISH_MIRASIM_SEAL_PUBKEY",
                 "MIRASIM_SEAL_PUBKEY", "MIROFISH_MIRASIM_SEAL_METADATA"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.mirasim_seal_public_key == DEFAULT_SEAL_PUBLIC_KEY
    assert settings.mirasim_seal_metadata is True

    # The official client's own override name is honoured for shared env files.
    monkeypatch.setenv("MIRASIM_SEAL_PUBKEY", " " + SEAL_PUBLIC_KEY + " ")
    assert Settings.from_env().mirasim_seal_public_key == SEAL_PUBLIC_KEY
    monkeypatch.setenv("MIROFISH_MIRASIM_SEAL_PUBLIC_KEY", DEFAULT_SEAL_PUBLIC_KEY)
    assert Settings.from_env().mirasim_seal_public_key == DEFAULT_SEAL_PUBLIC_KEY

    monkeypatch.setenv("MIROFISH_MIRASIM_SEAL_METADATA", "0")
    assert Settings.from_env().mirasim_seal_metadata is False
    monkeypatch.setenv("MIROFISH_MIRASIM_SEAL_METADATA", "off")
    assert Settings.from_env().mirasim_seal_metadata is False
    # A typo keeps the protective default rather than disabling the seal.
    monkeypatch.setenv("MIROFISH_MIRASIM_SEAL_METADATA", "nope")
    assert Settings.from_env().mirasim_seal_metadata is True
