"""Runtime settings resolved from environment variables and CLI flags.

Environment variable names are kept identical to the legacy single-file relay
so existing .env files and Docker volumes keep working.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field

from .seal import DEFAULT_SEAL_PUBLIC_KEY


def _env_float(name: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        value = default
    return max(minimum, value) if minimum is not None else value


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        value = default
    return max(minimum, value) if minimum is not None else value


def _env_bool(name: str, default: bool) -> bool:
    """Read a forgiving boolean environment value.

    Empty/unrecognised values retain the safe configured default instead of
    accidentally disabling relay metadata protection because of a typo.
    """
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


DEFAULT_DATA_DIR = pathlib.Path.home() / ".config" / "mirofish-relay"

# Captured from an official client's /v1/messages request.  Callers that are
# not themselves a Claude CLI get this identity synthesized so the relay sees a
# coherent SDK fingerprint instead of a partial one.  Bump alongside
# ``mirasim_client_version`` when a newer client build is observed.
DEFAULT_CLAUDE_CLI_USER_AGENT = "claude-cli/2.1.252 (external, mirasim)"
DEFAULT_MIRASIM_CLIENT_VERSION = "0.0.272"
# The desktop's bundled Codex binary identifies itself as the product, not as
# ``codex_cli_rs``; this is the exact string from the 0.0.272 capture.
DEFAULT_CODEX_USER_AGENT = (
    "mirasim/0.150.1 (Mac OS 26.6.2; x86_64) Apple_Terminal/470.2 (mirasim; 0.1.0)")


@dataclass
class Settings:
    auth_base: str = ""
    relay_base: str = ""
    anthropic_version: str = "2023-06-01"
    claude_cli_user_agent: str = DEFAULT_CLAUDE_CLI_USER_AGENT
    codex_user_agent: str = DEFAULT_CODEX_USER_AGENT
    mirasim_client_version: str = DEFAULT_MIRASIM_CLIENT_VERSION
    # The 0.0.272 relay wraps all generated x-mirasim metadata (except the
    # visible client build marker) in an authenticated encrypted envelope.
    # Keep both the key and the switch configurable for staged upstream key
    # rotation and private relay deployments.
    mirasim_seal_public_key: str = DEFAULT_SEAL_PUBLIC_KEY
    mirasim_seal_metadata: bool = True
    mirasim_locale: str = "zh-HK"
    keychain_service: str = "open-reverselab.mirofish-relay"
    default_model: str = "gpt-5.6-luna"
    data_dir: pathlib.Path = field(default_factory=lambda: DEFAULT_DATA_DIR)
    timeout: float = 30.0
    stream_read_timeout: float = 600.0
    keepalive_expiry: float = 75.0
    max_connections: int = 100
    max_keepalive_connections: int = 20
    max_body_bytes: int = 8 * 1024 * 1024
    model_catalog_ttl: float = 300.0
    cred_backend: str = ""
    in_docker: bool = False
    default_account: str = ""
    session_ttl: float = 1800.0

    proxy_refresh_seconds: float = 600.0
    proxy_fetch_timeout: float = 10.0
    proxy_fetch_max_bytes: int = 8 * 1024 * 1024
    proxy_subscription_user_agent: str = "mihomo/1.19.0"
    proxy_failure_threshold: int = 2
    # Regex over node names; matches are dropped from the pool entirely
    # (Mihomo provider exclude-filter + direct-mode parse filter).
    proxy_node_exclude: str = ""

    mihomo_controller: str = ""
    mihomo_proxy: str = ""
    mihomo_selector: str = "MirofishPool"
    mihomo_provider: str = "mirofish"
    mihomo_controller_timeout: float = 5.0
    mihomo_slots: int = 8
    mihomo_slot_base_port: int = 7891

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            # admin.test.mirofish.ai was retired upstream (403 "client version
            # retired"); the deprecation notice points product login here.
            auth_base=os.environ.get("MIROFISH_AUTH_BASE", "https://auth.mirasim.ai").rstrip("/"),
            # Current official clients send relay traffic here.  The legacy
            # mirasim-relay.mirofish.ai distribution was observed advertising
            # the catalog while returning model-unavailable for the same
            # official-shape Claude requests.
            relay_base=os.environ.get(
                "MIROFISH_RELAY_BASE", "https://relay.mirasim.ai").rstrip("/"),
            claude_cli_user_agent=(
                os.environ.get(
                    "MIROFISH_CLAUDE_CLI_USER_AGENT",
                    DEFAULT_CLAUDE_CLI_USER_AGENT).strip()
                or DEFAULT_CLAUDE_CLI_USER_AGENT),
            codex_user_agent=(
                os.environ.get(
                    "MIROFISH_CODEX_USER_AGENT", DEFAULT_CODEX_USER_AGENT).strip()
                or DEFAULT_CODEX_USER_AGENT),
            mirasim_client_version=(
                os.environ.get("MIROFISH_MIRASIM_CLIENT_VERSION",
                               DEFAULT_MIRASIM_CLIENT_VERSION).strip()
                or DEFAULT_MIRASIM_CLIENT_VERSION),
            mirasim_seal_public_key=(
                next((os.environ[name].strip() for name in (
                    "MIROFISH_MIRASIM_SEAL_PUBLIC_KEY",
                    "MIROFISH_MIRASIM_SEAL_PUBKEY",
                    # Match the official client's documented override name so
                    # an existing deployment can share its environment file.
                    "MIRASIM_SEAL_PUBKEY",
                ) if os.environ.get(name, "").strip()), DEFAULT_SEAL_PUBLIC_KEY)),
            mirasim_seal_metadata=_env_bool(
                "MIROFISH_MIRASIM_SEAL_METADATA", True),
            mirasim_locale=(
                os.environ.get("MIROFISH_MIRASIM_LOCALE", "zh-HK").strip()
                or "zh-HK"),
            cred_backend=os.environ.get("MIROFISH_CRED_BACKEND", "").lower(),
            in_docker=bool(os.environ.get("MIROFISH_IN_DOCKER")),
            default_account=os.environ.get("MIROFISH_DEFAULT_ACCOUNT", "").strip(),
            default_model=(
                os.environ.get("MIROFISH_DEFAULT_MODEL", "gpt-5.6-luna").strip()
                or "gpt-5.6-luna"),
            session_ttl=_env_float("MIROFISH_SESSION_TTL", 1800.0, minimum=60.0),
            stream_read_timeout=_env_float(
                "MIROFISH_STREAM_READ_TIMEOUT", 600.0, minimum=30.0),
            keepalive_expiry=_env_float(
                "MIROFISH_KEEPALIVE_EXPIRY", 75.0, minimum=5.0),
            max_connections=_env_int(
                "MIROFISH_MAX_CONNECTIONS", 100, minimum=1),
            max_keepalive_connections=_env_int(
                "MIROFISH_MAX_KEEPALIVE_CONNECTIONS", 20, minimum=1),
            max_body_bytes=_env_int(
                "MIROFISH_MAX_BODY_BYTES", 8 * 1024 * 1024, minimum=1024),
            proxy_refresh_seconds=_env_float("MIROFISH_PROXY_REFRESH_SECONDS", 600.0, minimum=30.0),
            proxy_fetch_timeout=_env_float("MIROFISH_PROXY_FETCH_TIMEOUT", 10.0, minimum=3.0),
            proxy_subscription_user_agent=(
                os.environ.get("MIROFISH_PROXY_SUBSCRIPTION_USER_AGENT", "mihomo/1.19.0").strip()
                or "mihomo/1.19.0"),
            proxy_failure_threshold=_env_int("MIROFISH_PROXY_FAILURE_THRESHOLD", 2, minimum=1),
            proxy_node_exclude=os.environ.get("MIROFISH_PROXY_NODE_EXCLUDE", "").strip(),
            mihomo_controller=os.environ.get("MIROFISH_MIHOMO_CONTROLLER", "").rstrip("/"),
            mihomo_proxy=os.environ.get("MIROFISH_MIHOMO_PROXY", "").strip(),
            mihomo_selector=os.environ.get("MIROFISH_MIHOMO_SELECTOR", "MirofishPool").strip() or "MirofishPool",
            mihomo_provider=os.environ.get("MIROFISH_MIHOMO_PROVIDER", "mirofish").strip() or "mirofish",
            mihomo_slots=_env_int("MIROFISH_MIHOMO_SLOTS", 8, minimum=1),
            mihomo_slot_base_port=_env_int("MIROFISH_MIHOMO_SLOT_BASE_PORT", 7891, minimum=1025),
        )
        settings.mihomo_controller_timeout = max(
            1.0, min(settings.proxy_fetch_timeout,
                     _env_float("MIROFISH_MIHOMO_CONTROLLER_TIMEOUT", 5.0)))
        settings.max_keepalive_connections = min(
            settings.max_keepalive_connections, settings.max_connections)
        return settings
