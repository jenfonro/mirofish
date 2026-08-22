"""Shared relay error type carrying an HTTP status and optional detail payload."""

from __future__ import annotations

from typing import Any


class RelayError(RuntimeError):
    def __init__(self, message: str, status: int = 500, data: Any = None):
        super().__init__(message)
        self.status = status
        self.data = data

    def payload(self) -> dict[str, Any]:
        if isinstance(self.data, dict) and "error" in self.data:
            # Upstream already returned an Anthropic-style error envelope.
            return self.data
        body: dict[str, Any] = {"error": {"type": "relay_error", "message": str(self)}}
        if self.data is not None:
            body["error"]["data"] = self.data
        return body
