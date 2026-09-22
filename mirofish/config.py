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


#: Chromium family labels curl-impersonate understands. ``chrome136`` is a
#: stable mid-2025 build close to current Electron; newer labels keep working
#: as curl_cffi grows them.
def _normalize_impersonate(raw: str) -> str:
    value = (raw or "").strip().lower()
    if not value or value in {"0", "off", "false", "none"}:
        return ""
    if value in {"1", "on", "true", "chrome", "default"}:
        return "chrome136"
    return value


DEFAULT_DATA_DIR = pathlib.Path.home() / ".config" / "mirofish-relay"

# Captured from an official client's /v1/messages request.  Callers that are
# not themselves a Claude CLI get this identity synthesized so the relay sees a
# coherent SDK fingerprint instead of a partial one.  Since 0.0.303 the desktop
# no longer bundles Claude, so the claude-cli version in this UA floats with
# the locally installed binary; the value here is the build installed on the
# analysis machine.
DEFAULT_CLAUDE_CLI_USER_AGENT = "claude-cli/2.1.261 (external, mirasim)"
# Still the bare build marker, not a UA string.
DEFAULT_MIRASIM_CLIENT_VERSION = "0.0.303"

# The desktop OS identity stamped on client-shaped envelopes (the appeal
# ``app.platform``/``app.arch`` and the analytics ``platform`` tag). A Windows
# desktop is the least remarkable default and is what the reference capture
# used; keep both configurable so a deployment can present another host.
DEFAULT_MIRASIM_OS_PLATFORM = "win32"
DEFAULT_MIRASIM_OS_ARCH = "x64"
# The desktop's Codex binary identifies itself as the product, not as
# ``codex_cli_rs``.  Since 0.0.303 Codex is no longer bundled either, so the
# version floats with the locally installed binary while the OS/arch/terminal
# parts stay as captured; this value is the build installed on the analysis
# machine.
DEFAULT_CODEX_USER_AGENT = (
    "mirasim/0.153.4 (Mac OS 26.6.2; x86_64) Apple_Terminal/470.2 (mirasim; 0.1.0)")


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
    # The desktop OS identity presented to the upstream (feedback app.platform
    # /app.arch and the analytics platform tag). Windows/x64 by default.
    mirasim_os_platform: str = DEFAULT_MIRASIM_OS_PLATFORM
    mirasim_os_arch: str = DEFAULT_MIRASIM_OS_ARCH
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
    # A Messages request capped at one output token is an availability probe,
    # not work: the upstream refuses that shape outright and points the caller
    # at /v1/limits, so forwarding it only spends a signed round trip and a
    # device ticket per probe. Answer it locally instead. Turn this off to
    # restore plain passthrough for a caller that really wants one token of
    # model output (and the upstream 400 that comes with it).
    one_token_short_circuit: bool = True

    # Replays the whole-client background behavior of the official desktop:
    # gzipped /events telemetry, the /v1/model-roster probe, and the
    # cdn-assets update check. Each account gets its own task with its own
    # jitter so the fleet does not beat in lockstep.
    behavior_replay: bool = True

    # Optional outer forward proxy that terminates the TLS handshake instead
    # of the local OpenSSL stack (e.g. a fingerprint-spoofing gateway). The
    # upstream URL is left untouched: CONNECT tunnelling forwards the
    # ClientHello this proxy generates, while header/body fidelity stays here.
    tls_proxy: str = ""
    # Chromium-family ClientHello impersonation via curl-impersonate
    # (``curl_cffi``). Empty disables it (httpx + OpenSSL). A value such as
    # ``chrome136`` sends upstream traffic with a BoringSSL-shaped JA3/JA4
    # while still going through the account's own Mihomo exit.
    tls_impersonate: str = ""

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
            mirasim_os_platform=(
                os.environ.get("MIROFISH_MIRASIM_OS_PLATFORM",
                               DEFAULT_MIRASIM_OS_PLATFORM).strip()
                or DEFAULT_MIRASIM_OS_PLATFORM),
            mirasim_os_arch=(
                os.environ.get("MIROFISH_MIRASIM_OS_ARCH",
                               DEFAULT_MIRASIM_OS_ARCH).strip()
                or DEFAULT_MIRASIM_OS_ARCH),
            cred_backend=os.environ.get("MIROFISH_CRED_BACKEND", "").lower(),
            in_docker=bool(os.environ.get("MIROFISH_IN_DOCKER")),
            default_account=os.environ.get("MIROFISH_DEFAULT_ACCOUNT", "").strip(),
            default_model=(
                os.environ.get("MIROFISH_DEFAULT_MODEL", "gpt-5.6-luna").strip()
                or "gpt-5.6-luna"),
            session_ttl=_env_float("MIROFISH_SESSION_TTL", 1800.0, minimum=60.0),
            one_token_short_circuit=_env_bool(
                "MIROFISH_ONE_TOKEN_SHORT_CIRCUIT", True),
            behavior_replay=_env_bool("MIROFISH_BEHAVIOR_REPLAY", True),
            tls_proxy=os.environ.get("MIROFISH_TLS_PROXY", "").strip(),
            tls_impersonate=_normalize_impersonate(
                os.environ.get("MIROFISH_TLS_IMPERSONATE", "")),
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
