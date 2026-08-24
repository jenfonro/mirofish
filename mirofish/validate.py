"""Input validation helpers shared across trust boundaries."""

from __future__ import annotations

import pathlib
import re
import urllib.parse

from .errors import RelayError

ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
CODE_RE = re.compile(r"\d{6}")


def alias_value(value: str) -> str:
    value = value.strip()
    if not ALIAS_RE.fullmatch(value):
        raise RelayError("alias must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}", 400)
    return value


def email_value(value: str) -> str:
    value = value.strip()
    if not EMAIL_RE.fullmatch(value):
        raise RelayError("invalid email address", 400)
    return value


def node_exclude_pattern(value: str) -> re.Pattern[str] | None:
    """Compile MIROFISH_PROXY_NODE_EXCLUDE, or None when unset.

    The same string is handed to Mihomo's provider `exclude-filter` (Go RE2),
    so keep expressions to the common subset — plain alternations like
    `香港|HK|🇭🇰` behave identically in both engines."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return re.compile(value)
    except re.error as exc:
        raise RelayError(
            "MIROFISH_PROXY_NODE_EXCLUDE is not a valid regular expression", 500) from exc


def code_value(value: str) -> str:
    value = value.strip()
    if not CODE_RE.fullmatch(value):
        raise RelayError("verification code must be 6 digits", 400)
    return value


def model_value(value: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > 200 or any(ch in value for ch in "\r\n\0"):
        raise RelayError("invalid model name", 400)
    return value


def proxy_subscription_value(value: str) -> str:
    """Validate a subscription URL without ever returning it in diagnostics."""
    value = value.strip()
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError
        if parsed.username or parsed.password:
            raise ValueError
    except ValueError as exc:
        raise RelayError("proxy subscription must be an http(s) URL", 400) from exc
    return value


def proxy_subscription_file_value(value: str) -> str:
    """Validate a container-local provider file path without exposing its content."""
    value = value.strip()
    path = pathlib.PurePosixPath(value)
    if not value or not path.is_absolute() or ".." in path.parts:
        raise RelayError("proxy subscription file must be an absolute container path", 400)
    return value
