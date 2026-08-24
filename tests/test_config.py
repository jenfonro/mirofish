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
