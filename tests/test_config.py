from mirofish.config import Settings


def test_default_model_can_be_overridden(monkeypatch):
    monkeypatch.setenv("MIROFISH_DEFAULT_MODEL", "gpt-5.6-terra")

    assert Settings.from_env().default_model == "gpt-5.6-terra"


def test_empty_default_model_uses_serviceable_fallback(monkeypatch):
    monkeypatch.setenv("MIROFISH_DEFAULT_MODEL", "")

    assert Settings.from_env().default_model == "gpt-5.6-luna"
