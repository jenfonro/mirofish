"""Runtime settings resolved from environment variables and CLI flags.

Environment variable names are kept identical to the legacy single-file relay
so existing .env files and Docker volumes keep working.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field


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


DEFAULT_DATA_DIR = pathlib.Path.home() / ".config" / "mirofish-relay"


@dataclass
class Settings:
    auth_base: str = ""
    relay_base: str = ""
    anthropic_version: str = "2023-06-01"
    mirasim_client_version: str = "0.0.220"
    mirasim_locale: str = "zh-HK"
    keychain_service: str = "open-reverselab.mirofish-relay"
    default_model: str = "gpt-5.6-luna"
    data_dir: pathlib.Path = field(default_factory=lambda: DEFAULT_DATA_DIR)
    timeout: float = 30.0
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
            # mirasim-relay.mirofish.ai distribution can still advertise the
            # catalog while returning false model-unavailable responses.
            relay_base=os.environ.get(
                "MIROFISH_RELAY_BASE", "https://relay.mirasim.ai").rstrip("/"),
            mirasim_client_version=(
                os.environ.get("MIROFISH_MIRASIM_CLIENT_VERSION", "0.0.220").strip()
                or "0.0.220"),
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
        return settings
