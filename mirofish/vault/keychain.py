"""macOS Keychain credential backend (host installs)."""

from __future__ import annotations

import subprocess
import sys

from ..errors import RelayError
from ..validate import alias_value


class Keychain:
    def __init__(self, service: str) -> None:
        self.service = service

    def account_name(self, alias: str, kind: str) -> str:
        return alias_value(alias) + ":" + kind

    def _require_darwin(self) -> None:
        if sys.platform != "darwin":
            raise RelayError("persistent credentials require macOS Keychain", 500)

    def put(self, alias: str, kind: str, value: str) -> None:
        self._require_darwin()
        result = subprocess.run(
            ["security", "add-generic-password", "-U", "-a", self.account_name(alias, kind),
             "-s", self.service, "-w", value],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RelayError("could not save credential to macOS Keychain", 500)

    def get(self, alias: str, kind: str) -> str:
        self._require_darwin()
        result = subprocess.run(
            ["security", "find-generic-password", "-a", self.account_name(alias, kind),
             "-s", self.service, "-w"],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise RelayError("credential is missing from macOS Keychain", 500)
        return result.stdout.strip()

    def delete(self, alias: str, kind: str) -> None:
        if sys.platform != "darwin":
            return
        subprocess.run(
            ["security", "delete-generic-password", "-a", self.account_name(alias, kind),
             "-s", self.service],
            capture_output=True, text=True,
        )
