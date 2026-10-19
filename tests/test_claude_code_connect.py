"""Claude Code Connect (``horizon desktop connect claude-code``) and its coexistence.

Covers the shared route with ``vscode-claude``, the status line and plugin that
are installed only when absent, exact restore on disconnect, ``CLAUDE_CONFIG_DIR``,
and the regression that a live Connect route survives ``horizon unwrap claude``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from horizon.cli import wrap as wrap_cli
from horizon.cli.main import main
from horizon.providers.claude import vscode as vscode_mod
from horizon.providers.claude.vscode import claude_user_settings_path

USER_SETTINGS = {
    "model": "opus",
    "env": {"FOO": "1"},
    "permissions": {"allow": ["Bash(ls)"]},
}
PLUGIN_KEY = "contextshrink@contextshrink"


def _run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(main, ["desktop", *args])
    return result.exit_code, result.output


def _settings(tmp_path: Path, payload: dict | None = USER_SETTINGS) -> Path:
    path = tmp_path / ".claude" / "settings.json"
    if payload is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _status_exe(tmp_path: Path) -> Path:
    exe = tmp_path / "bin" / "contextshrink-status.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")
    return exe


def _marketplace(tmp_path: Path) -> Path:
    root = tmp_path / "claude-code" / "plugin-marketplace"
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "marketplace.json").write_text("{}", encoding="utf-8")
    return root


def _connect_claude_code(path: Path, exe: Path, marketplace: Path, port: int = 18788) -> dict:
    code, out = _run(
        "connect",
        "claude-code",
        "--port",
        str(port),
        "--settings-file",
        str(path),
        "--status-exe",
        str(exe),
        "--plugin-dir",
        str(marketplace),
    )
    assert code == 0, out
    return json.loads(out)


def test_connect_routes_terminal_and_installs_status_line_and_plugin(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    before = _read(path)
    exe = _status_exe(tmp_path)
    marketplace = _marketplace(tmp_path)

    result = _connect_claude_code(path, exe, marketplace)

    assert result["terminal"] == "connected"
    assert result["status_line"] == "installed"
    assert result["plugin"] == "installed"
    assert result["base_url"].startswith("http://127.0.0.1:18788/k/")
    payload = _read(path)
    assert payload["env"]["ANTHROPIC_BASE_URL"] == result["base_url"]
    assert payload["env"]["ENABLE_TOOL_SEARCH"] == "false"
    assert payload["env"]["FOO"] == "1"
    assert payload["statusLine"] == {"type": "command", "command": f'"{exe}"'}
    assert payload["extraKnownMarketplaces"]["contextshrink"] == {
        "source": {"source": "directory", "path": str(marketplace)}
    }
    assert payload["enabledPlugins"][PLUGIN_KEY] is True

    code, out = _run("status", "claude-code", "--port", "18788", "--settings-file", str(path))
    status = json.loads(out)
    assert code == 0
    assert status["status"] == "connected"
    assert status["targets"] == {
        "terminal": "connected",
        "status_line": "installed",
        "plugin": "installed",
    }

    code, out = _run("disconnect", "claude-code", "--port", "18788", "--settings-file", str(path))
    assert code == 0, out
    assert json.loads(out)["result"] == "restored"
    assert _read(path) == before
    assert not path.with_name(".horizon-vscode-claude.json").exists()


def test_missing_status_exe_and_marketplace_are_skipped_not_dangling(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    result = _connect_claude_code(path, tmp_path / "missing.exe", tmp_path / "no-marketplace")

    assert result["terminal"] == "connected"
    assert result["status_line"] == "skipped-missing"
    assert result["plugin"] == "skipped-missing"
    payload = _read(path)
    assert "statusLine" not in payload
    assert "extraKnownMarketplaces" not in payload
    assert "enabledPlugins" not in payload


def test_user_status_line_is_kept_and_survives_disconnect(tmp_path: Path) -> None:
    users_line = {"type": "command", "command": "my-own-statusline"}
    path = _settings(tmp_path, {**USER_SETTINGS, "statusLine": users_line})

    result = _connect_claude_code(path, _status_exe(tmp_path), _marketplace(tmp_path))

    assert result["status_line"] == "kept-user"
    assert _read(path)["statusLine"] == users_line
    code, _ = _run("disconnect", "claude-code", "--port", "18788", "--settings-file", str(path))
    assert code == 0
    assert _read(path)["statusLine"] == users_line


def test_vscode_and_claude_code_share_the_route_and_restore_only_their_own(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    before = _read(path)
    code, out = _run("connect", "vscode-claude", "--port", "18788", "--settings-file", str(path))
    assert code == 0, out

    result = _connect_claude_code(path, _status_exe(tmp_path), _marketplace(tmp_path))
    assert result["terminal"] == "joined"
    for editor in ("vscode-claude", "claude-code"):
        code, out = _run("status", editor, "--port", "18788", "--settings-file", str(path))
        assert json.loads(out)["status"] == "connected", editor

    # Claude Code leaves; VS Code still holds the route, so the env stays.
    code, out = _run("disconnect", "claude-code", "--port", "18788", "--settings-file", str(path))
    assert code == 0, out
    assert json.loads(out)["result"] == "kept"
    payload = _read(path)
    assert payload["env"]["ANTHROPIC_BASE_URL"] == result["base_url"]
    assert "statusLine" not in payload
    code, out = _run("status", "vscode-claude", "--port", "18788", "--settings-file", str(path))
    assert json.loads(out)["status"] == "connected"
    code, out = _run("status", "claude-code", "--port", "18788", "--settings-file", str(path))
    assert json.loads(out)["status"] == "off"

    # VS Code leaves last: everything goes back to the user's original settings.
    code, out = _run("disconnect", "vscode-claude", "--port", "18788", "--settings-file", str(path))
    assert code == 0, out
    assert _read(path) == before


def test_claude_code_refuses_another_port(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    code, out = _run("connect", "vscode-claude", "--port", "8787", "--settings-file", str(path))
    assert code == 0, out

    code, out = _run(
        "connect",
        "claude-code",
        "--port",
        "18788",
        "--settings-file",
        str(path),
        "--status-exe",
        str(tmp_path / "x.exe"),
        "--plugin-dir",
        str(tmp_path / "pm"),
    )
    assert code != 0
    assert "already routed" in out


def test_claude_config_dir_selects_the_settings_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "custom-config"
    assert claude_user_settings_path({"CLAUDE_CONFIG_DIR": str(config_dir)}) == (
        config_dir / "settings.json"
    )


def _unwrap_claude_no_side_effects(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Point user settings at ``home`` and keep the unwrap away from real Claude state."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(wrap_cli, "_find_persistent_manifest", lambda _port: None)


def test_unwrap_claude_keeps_a_live_connect_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the desktop app runs ``unwrap claude`` when a console closes."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    _unwrap_claude_no_side_effects(monkeypatch, home)
    path = _settings(home)
    result = _connect_claude_code(path, _status_exe(tmp_path), _marketplace(tmp_path))
    connected = _read(path)

    unwrap = CliRunner().invoke(main, ["unwrap", "claude", "--keep-mcp", "--no-stop-proxy"])

    assert unwrap.exit_code == 0, unwrap.output
    after = _read(path)
    assert after["env"]["ANTHROPIC_BASE_URL"] == result["base_url"]
    assert after["env"]["ENABLE_TOOL_SEARCH"] == "false"
    assert after == connected
    code, out = _run("status", "claude-code", "--port", "18788", "--settings-file", str(path))
    assert json.loads(out)["status"] == "connected"


def test_unwrap_claude_still_strips_a_route_no_connect_state_owns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    _unwrap_claude_no_side_effects(monkeypatch, home)
    path = _settings(home, {**USER_SETTINGS, "env": {"ANTHROPIC_BASE_URL": "http://x", "FOO": "1"}})

    unwrap = CliRunner().invoke(main, ["unwrap", "claude", "--keep-mcp", "--no-stop-proxy"])

    assert unwrap.exit_code == 0, unwrap.output
    assert _read(path)["env"] == {"FOO": "1"}


def _connect_args(editor: str, path: Path, tmp_path: Path) -> list[str]:
    args = ["connect", editor, "--port", "18788", "--settings-file", str(path)]
    if editor == "claude-code":
        args += [
            "--status-exe",
            str(_status_exe(tmp_path)),
            "--plugin-dir",
            str(_marketplace(tmp_path)),
        ]
    return args


@pytest.mark.parametrize("editor", ["claude-code", "vscode-claude"])
@pytest.mark.parametrize("failing_write", ["settings", "final-state"])
def test_failed_write_leaves_a_recorded_intent_that_disconnect_clears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    editor: str,
    failing_write: str,
) -> None:
    """A crash between the state and the settings must not leave an unrecorded route.

    ``settings``: the second write (the settings) fails, so no route was written.
    ``final-state``: the settings are written and the closing state write fails,
    so the route exists while the state still reads pending. Either way the
    state is on disk first, and disconnect restores the original settings.
    """
    path = _settings(tmp_path)
    before = _read(path)
    state_path = path.with_name(".horizon-vscode-claude.json")
    real_write = vscode_mod._write_json

    def failing(target: Path, payload: dict) -> None:
        if failing_write == "settings" and target == path:
            raise OSError("simulated disk failure")
        if failing_write == "final-state" and target == state_path and "pending" not in payload:
            raise OSError("simulated disk failure")
        real_write(target, payload)

    monkeypatch.setattr(vscode_mod, "_write_json", failing)
    result = CliRunner().invoke(main, ["desktop", *_connect_args(editor, path, tmp_path)])
    monkeypatch.setattr(vscode_mod, "_write_json", real_write)

    assert result.exit_code != 0
    recorded = json.loads(state_path.read_text(encoding="utf-8"))
    assert recorded["pending"] is True
    assert recorded["managed"]["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:18788")
    if failing_write == "final-state":
        assert _read(path)["env"]["ANTHROPIC_BASE_URL"] == recorded["managed"]["ANTHROPIC_BASE_URL"]

    code, out = _run("disconnect", editor, "--port", "18788", "--settings-file", str(path))
    assert code == 0, out
    assert _read(path) == before
    assert not state_path.exists()


def test_a_held_settings_lock_times_out_with_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock nobody releases must fail the command, not hang it (L8)."""
    import click

    monkeypatch.setattr(wrap_cli, "_LOCK_TIMEOUT_SECONDS", 0.3)
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)

    with wrap_cli._wrap_settings_lock(settings):
        with pytest.raises(click.ClickException, match="Timed out after"):
            with wrap_cli._wrap_settings_lock(settings):
                pass  # pragma: no cover - the lock is held, so this must not run
