"""A removal whose config write fails returns False and says why, per registrar."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from horizon.mcp_registry.base import MCPRegistrar, RegisterStatus, ServerSpec
from horizon.mcp_registry.claude import ClaudeRegistrar
from horizon.mcp_registry.codex import CodexRegistrar
from horizon.mcp_registry.grok import GrokRegistrar


def _no_space(fd: int) -> None:
    raise OSError(28, "No space left on device")


@pytest.mark.parametrize(
    ("make", "config", "logger_name"),
    [
        (
            lambda home: ClaudeRegistrar(claude_cli=None, home_dir=home),
            ".claude.json",
            "horizon.mcp_registry.claude",
        ),
        (
            lambda home: CodexRegistrar(home_dir=home),
            ".codex/config.toml",
            "horizon.mcp_registry.codex",
        ),
        (
            lambda home: GrokRegistrar(home_dir=home),
            ".grok/config.toml",
            "horizon.mcp_registry.grok",
        ),
    ],
    ids=["claude", "codex", "grok"],
)
def test_failed_removal_keeps_the_config_and_logs_why(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    make,
    config: str,
    logger_name: str,
) -> None:
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    registrar: MCPRegistrar = make(tmp_path)
    spec = ServerSpec(name="horizon", command="/usr/bin/python", args=("-m", "horizon.cli"))
    assert registrar.register_server(spec).status == RegisterStatus.REGISTERED
    path = tmp_path / config
    before = path.read_bytes()

    monkeypatch.setattr("horizon.fsutil.os.fsync", _no_space)
    with caplog.at_level(logging.WARNING, logger=logger_name):
        assert registrar.unregister_server("horizon") is False

    assert path.read_bytes() == before
    messages = [r.getMessage() for r in caplog.records if r.name == logger_name]
    assert messages
    assert all(
        m.startswith(f"could not remove 'horizon' from {path}") and "No space left" in m
        for m in messages
    )
