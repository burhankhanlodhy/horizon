"""`horizon desktop` editor hookups used by the ContextShrink desktop app."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from horizon.cli.main import main

USER_SETTINGS = {
    "model": "opus",
    "env": {"ANTHROPIC_BASE_URL": "https://my.gateway.example", "FOO": "1"},
    "permissions": {"allow": ["Bash(ls)"]},
}


def _run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(main, ["desktop", *args])
    return result.exit_code, result.output


def _settings(tmp_path: Path, payload: dict | None = USER_SETTINGS) -> Path:
    path = tmp_path / ".claude" / "settings.json"
    if payload is not None:
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _status(path: Path, port: int = 18788) -> str:
    code, out = _run("status", "vscode-claude", "--port", str(port), "--settings-file", str(path))
    assert code == 0, out
    return json.loads(out)["status"]


def test_connect_then_disconnect_restores_user_settings(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    before = json.loads(path.read_text(encoding="utf-8"))
    assert _status(path) == "off"

    code, out = _run("connect", "vscode-claude", "--port", "18788", "--settings-file", str(path))
    assert code == 0, out
    env = json.loads(path.read_text(encoding="utf-8"))["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:18788"
    assert env["FOO"] == "1"
    assert _status(path) == "connected"

    # Reconnecting (app restart after a crash) is idempotent.
    code, out = _run("connect", "vscode-claude", "--port", "18788", "--settings-file", str(path))
    assert code == 0, out

    code, out = _run("disconnect", "vscode-claude", "--port", "18788", "--settings-file", str(path))
    assert code == 0, out
    assert json.loads(path.read_text(encoding="utf-8")) == before
    assert _status(path) == "off"


def test_connect_without_settings_file_removes_it_again(tmp_path: Path) -> None:
    path = _settings(tmp_path, payload=None)
    assert _run("connect", "vscode-claude", "--port", "18788", "--settings-file", str(path))[0] == 0
    assert path.exists()
    assert _run("disconnect", "vscode-claude", "--port", "18788", "--settings-file", str(path))[0] == 0
    assert not path.exists()


def test_does_not_take_over_or_undo_another_horizon_setup(tmp_path: Path) -> None:
    """The user's own `horizon wrap vscode-claude` on 8787 stays theirs."""
    path = _settings(tmp_path)
    assert _run("connect", "vscode-claude", "--port", "8787", "--settings-file", str(path))[0] == 0
    own = path.read_text(encoding="utf-8")
    assert _status(path) == "other"

    code, out = _run("connect", "vscode-claude", "--port", "18788", "--settings-file", str(path))
    assert code != 0
    assert "already routed" in out
    code, out = _run("disconnect", "vscode-claude", "--port", "18788", "--settings-file", str(path))
    assert code == 0
    assert "Left alone" in out
    assert path.read_text(encoding="utf-8") == own


def test_disconnect_refuses_to_clobber_a_user_edit(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    assert _run("connect", "vscode-claude", "--port", "18788", "--settings-file", str(path))[0] == 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["env"]["ANTHROPIC_BASE_URL"] = "https://changed.by.user"
    path.write_text(json.dumps(payload), encoding="utf-8")
    code, _ = _run("disconnect", "vscode-claude", "--port", "18788", "--settings-file", str(path))
    assert code != 0
    assert json.loads(path.read_text(encoding="utf-8"))["env"]["ANTHROPIC_BASE_URL"] == "https://changed.by.user"
