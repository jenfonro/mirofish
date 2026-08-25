from mirofish.config import Settings


def test_default_upstream_endpoints_match_current_official_client(monkeypatch):
    monkeypatch.delenv("MIROFISH_AUTH_BASE", raising=False)
    monkeypatch.delenv("MIROFISH_RELAY_BASE", raising=False)

    settings = Settings.from_env()

    assert settings.auth_base == "https://auth.mirasim.ai"
    assert settings.relay_base == "https://relay.mirasim.ai"


def test_relay_endpoint_can_be_overridden(monkeypatch):
    monkeypatch.setenv("MIROFISH_RELAY_BASE", "https://relay.example.test/root/")

    assert Settings.from_env().relay_base == "https://relay.example.test/root"


def test_default_model_can_be_overridden(monkeypatch):
    monkeypatch.setenv("MIROFISH_DEFAULT_MODEL", "gpt-5.6-terra")

    assert Settings.from_env().default_model == "gpt-5.6-terra"


def test_empty_default_model_uses_serviceable_fallback(monkeypatch):
    monkeypatch.setenv("MIROFISH_DEFAULT_MODEL", "")

    assert Settings.from_env().default_model == "gpt-5.6-luna"


def test_claude_cli_user_agent_matches_the_capture_and_is_overridable(monkeypatch):
    monkeypatch.delenv("MIROFISH_CLAUDE_CLI_USER_AGENT", raising=False)
    assert Settings.from_env().claude_cli_user_agent == \
        "claude-cli/2.1.241 (external, mirasim)"

    monkeypatch.setenv("MIROFISH_CLAUDE_CLI_USER_AGENT", "claude-cli/9.9.9 (x)")
    assert Settings.from_env().claude_cli_user_agent == "claude-cli/9.9.9 (x)"

    monkeypatch.setenv("MIROFISH_CLAUDE_CLI_USER_AGENT", "  ")
    assert Settings.from_env().claude_cli_user_agent == \
        "claude-cli/2.1.241 (external, mirasim)"


def test_upstream_transport_defaults_cover_observed_long_reuse(monkeypatch):
    for name in (
        "MIROFISH_KEEPALIVE_EXPIRY", "MIROFISH_MAX_CONNECTIONS",
        "MIROFISH_MAX_KEEPALIVE_CONNECTIONS", "MIROFISH_STREAM_READ_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()

    assert settings.keepalive_expiry == 75.0
    assert settings.max_connections == 100
    assert settings.max_keepalive_connections == 20
    assert settings.stream_read_timeout == 600.0


def test_transport_limits_are_configurable_and_keepalive_is_capped(monkeypatch):
    monkeypatch.setenv("MIROFISH_KEEPALIVE_EXPIRY", "90")
    monkeypatch.setenv("MIROFISH_MAX_CONNECTIONS", "7")
    monkeypatch.setenv("MIROFISH_MAX_KEEPALIVE_CONNECTIONS", "12")
    monkeypatch.setenv("MIROFISH_STREAM_READ_TIMEOUT", "720")

    settings = Settings.from_env()

    assert settings.keepalive_expiry == 90.0
    assert settings.max_connections == 7
    assert settings.max_keepalive_connections == 7
    assert settings.stream_read_timeout == 720.0
