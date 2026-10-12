from horizon.proxy.handlers import openai as openai_module

ENV = "HORIZON_CODEX_WS_COMPRESSION_TIMEOUT_SECONDS"


def test_default_is_five_seconds(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setattr(openai_module, "COMPRESSION_TIMEOUT_SECONDS", 30.0)
    assert openai_module._codex_ws_compression_timeout_seconds() == 5.0


def test_env_raises_the_budget(monkeypatch):
    monkeypatch.setenv(ENV, "12.5")
    monkeypatch.setattr(openai_module, "COMPRESSION_TIMEOUT_SECONDS", 30.0)
    assert openai_module._codex_ws_compression_timeout_seconds() == 12.5


def test_env_never_exceeds_the_general_compression_timeout(monkeypatch):
    monkeypatch.setenv(ENV, "90")
    monkeypatch.setattr(openai_module, "COMPRESSION_TIMEOUT_SECONDS", 30.0)
    assert openai_module._codex_ws_compression_timeout_seconds() == 30.0


def test_invalid_values_fall_back_to_default(monkeypatch):
    monkeypatch.setattr(openai_module, "COMPRESSION_TIMEOUT_SECONDS", 30.0)
    for raw in ("abc", "0", "-3", "  "):
        monkeypatch.setenv(ENV, raw)
        assert openai_module._codex_ws_compression_timeout_seconds() == 5.0
