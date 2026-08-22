"""Request-level helpers: local proxy-key auth and safe JSON body reading."""

from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi import Request

from ..errors import RelayError
from .state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.relay


def require_auth(request: Request) -> None:
    """Accept X-Mirofish-Proxy-Key, X-Api-Key, or Authorization: Bearer so
    standard Anthropic/OpenAI clients can authenticate without custom headers."""
    state = get_state(request)
    supplied = (
        request.headers.get("X-Mirofish-Proxy-Key", "")
        or request.headers.get("X-Api-Key", "")
        or request.headers.get("Authorization", "").removeprefix("Bearer ")
           .removeprefix("bearer ").strip()
    )
    if not supplied or not secrets.compare_digest(supplied, state.proxy_key):
        raise RelayError("invalid local proxy key", 401)


async def read_json_body(request: Request) -> dict[str, Any]:
    state = get_state(request)
    length_header = request.headers.get("content-length")
    try:
        if length_header is not None and int(length_header) > state.settings.max_body_bytes:
            raise RelayError("request body too large", 413)
    except ValueError:
        pass
    body = await request.body()
    if len(body) > state.settings.max_body_bytes:
        raise RelayError("request body too large", 413)
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RelayError("request body must be valid JSON", 400) from exc
    if not isinstance(value, dict):
        raise RelayError("request body must be a JSON object", 400)
    return value
