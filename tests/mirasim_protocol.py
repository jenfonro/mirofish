"""Test-side view of the 0.0.272 Mirasim relay protocol.

The relay seals its own ``x-mirasim-*`` metadata into ``x-mirasim-enc`` and
signs requests with ``mrs-sig-v2``.  Production only needs the upstream's
*public* seal key, so nothing in the package can open an envelope.  The test
suite therefore runs with its own X25519 pair (installed by ``conftest``) and
uses these helpers to look inside a captured request exactly the way the
upstream would.
"""

from __future__ import annotations

import base64
import hashlib
import json

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mirofish.device import SIG_VERSION, canonical_metadata
from mirofish.seal import SEAL_HEADER, SEAL_VERSION

# One receiving key for the whole test process.  Only its public half enters
# the relay's settings; the private half stays here so tests can decrypt.
SEAL_PRIVATE_KEY = X25519PrivateKey.generate()
SEAL_PUBLIC_KEY = base64.b64encode(SEAL_PRIVATE_KEY.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode("ascii")

_IDENTITY_FIELDS = frozenset({
    "x-mirasim-device", "x-mirasim-ts", "x-mirasim-nonce", "x-mirasim-sig",
    "x-mirasim-client", SEAL_HEADER,
})


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def unseal(sealed: str, method: str, path: str,
           private_key: X25519PrivateKey = SEAL_PRIVATE_KEY) -> dict[str, str]:
    """Open one ``mrs-seal-v1`` envelope bound to ``method``/``path``."""
    raw = _b64url_decode(sealed)
    ephemeral_public, nonce, ciphertext = raw[:32], raw[32:44], raw[44:]
    shared = private_key.exchange(
        X25519PublicKey.from_public_bytes(ephemeral_public))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=ephemeral_public,
               info=SEAL_VERSION.encode("ascii")).derive(shared)
    aad = (SEAL_VERSION + "\n" + method.upper() + "\n" + path).encode("utf-8")
    plaintext = ChaCha20Poly1305(key).decrypt(nonce, ciphertext, aad)
    metadata = json.loads(plaintext.decode("utf-8"))
    assert isinstance(metadata, dict)
    return metadata


def relay_metadata(request: httpx.Request, path: str | None = None) -> dict[str, str]:
    """Every ``x-mirasim-*`` field the upstream sees, clear or sealed.

    A sealed request carries ``x-mirasim-client`` in clear and everything else
    inside ``x-mirasim-enc``; a legacy (clear) request carries all of them as
    headers.  Both shapes collapse to one mapping here so assertions read the
    same regardless of protocol version.
    """
    fields = {name.lower(): value for name, value in request.headers.items()
              if name.lower().startswith("x-mirasim-") and name.lower() != SEAL_HEADER}
    sealed = request.headers.get(SEAL_HEADER)
    if sealed:
        fields.update(unseal(sealed, request.method, path or request.url.path))
    return fields


def signing_payload(request: httpx.Request, path: str, credential: str,
                    fields: dict[str, str] | None = None) -> bytes:
    """Rebuild the ``mrs-sig-v2`` canonical record for ``request``."""
    fields = relay_metadata(request, path) if fields is None else fields
    metadata = {name: value for name, value in fields.items()
                if name not in _IDENTITY_FIELDS}
    return "\n".join((
        SIG_VERSION,
        request.method.upper(),
        path,
        fields["x-mirasim-ts"],
        fields["x-mirasim-nonce"],
        fields["x-mirasim-device"],
        fields.get("x-mirasim-client", ""),
        hashlib.sha256(credential.encode("utf-8")).hexdigest(),
        hashlib.sha256(canonical_metadata(metadata).encode("utf-8")).hexdigest(),
        hashlib.sha256(request.content).hexdigest(),
    )).encode("utf-8")


def verify_signature(state, request: httpx.Request, path: str,
                     credential: str | None = None, alias: str = "work") -> bytes:
    """Verify the request's ``mrs-sig-v2`` signature; return its raw bytes.

    ``credential`` defaults to the bearer the request itself carries, which is
    what the upstream hashes into the signing context.
    """
    fields = relay_metadata(request, path)
    if credential is None:
        authorization = request.headers["authorization"]
        assert authorization.startswith("Bearer ")
        credential = authorization[len("Bearer "):]
    signature = _b64url_decode(fields["x-mirasim-sig"])
    public = serialization.load_der_public_key(
        base64.b64decode(state.upstream._signer(alias).public_key))
    public.verify(signature, signing_payload(request, path, credential, fields))
    return signature
