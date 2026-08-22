"""Credential backends: macOS Keychain (host) or encrypted file vault (containers).

SQLite never stores credentials; both backends implement put/get/delete on
(alias, kind) pairs.
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import Protocol

from ..errors import RelayError


class CredentialStore(Protocol):
    def put(self, alias: str, kind: str, value: str) -> None: ...
    def get(self, alias: str, kind: str) -> str: ...
    def delete(self, alias: str, kind: str) -> None: ...


def make_credential_store(data_dir: pathlib.Path, backend: str = "",
                          in_docker: bool = False,
                          keychain_service: str = "open-reverselab.mirofish-relay") -> CredentialStore:
    from .filevault import FileVault
    from .keychain import Keychain

    if not backend:
        backend = "keychain" if sys.platform == "darwin" and not in_docker else "file"
    if backend == "keychain":
        return Keychain(keychain_service)
    if backend == "file":
        return FileVault(data_dir / "secrets.enc")
    raise RelayError("unknown MIROFISH_CRED_BACKEND: " + backend, 500)


__all__ = ["CredentialStore", "make_credential_store"]
