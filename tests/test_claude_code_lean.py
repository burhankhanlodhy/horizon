"""Keep sessions lean: ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` for the terminal and the Code tab.

Covers the terminal settings (on, off, restore of an absent or present value,
user edits left alone, idempotence, window updates, disconnect restore, shared
owners with ``vscode-claude``), ``codetab.json`` (set, clear, and preservation
across a re-install), and the ``horizon lean`` CLI contract.

Every path is under ``tmp_path``. The environment roots are pointed at temp
folders by an autouse fixture, so the real ``~/.claude`` and the real
``%LOCALAPPDATA%\\ContextShrink`` are never read or written.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from horizon import desktop_codetab as dc
from horizon.cli.main import main
from horizon.providers.claude import vscode as vs

KEY = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
PORT = 18788
USER_SETTINGS = {"model": "opus", "env": {"FOO": "1"}}


@pytest.fixture(autouse=True)
def _fake_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("CONTEXTSHRINK_DATA_DIR", str(tmp_path / "ctx-data"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude-config"))
    monkeypatch.delenv(KEY, raising=False)


def _settings(tmp_path: Path, payload: dict | None = USER_SETTINGS) -> Path:
    path = tmp_path / "claude" / "settings.json"
    if payload is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _env(path: Path) -> dict:
    return _read(path).get("env", {})


def _state(path: Path) -> dict:
    return _read(vs._state_path(path))


def _connect_claude_code(path: Path) -> None:
    vs.connect_claude_code_settings(path, port=PORT)


def _codetab(tmp_path: Path, *, tool_search: bool = False) -> Path:
    """Install a Code tab config with no Claude versions. Returns the claude-code folder."""
    data = tmp_path / "ctx-data" / "claude-code"
    launcher = tmp_path / "launcher.exe"
    launcher.write_bytes(b"LAUNCHER")
    dc.install(
        roots=[],
        data=data,
        launcher=launcher,
        health_port=PORT,
        tool_search=tool_search,
        environ={"CLAUDE_CONFIG_DIR": str(tmp_path / "no-claude-config")},
    )
    return data


def _run(*args: str) -> tuple[int, str, dict | None]:
    result = CliRunner().invoke(main, ["lean", *args])
    payload = None
    if "--json" in args:
        payload = json.loads(result.output.strip().splitlines()[-1])
    return result.exit_code, result.output, payload


def _cli_args(tmp_path: Path, path: Path, *extra: str) -> list[str]:
    return [
        *extra,
        "--settings-file",
        str(path),
        "--codetab-dir",
        str(tmp_path / "ctx-data" / "claude-code"),
    ]


# ---------------------------------------------------------------------------
# Terminal settings: on, off, restore
# ---------------------------------------------------------------------------


def test_on_writes_the_window_and_records_an_absent_previous_value(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    assert vs.lean_on(path, 200_000) == "on"
    assert _env(path)[KEY] == "200000"
    record = _state(path)["claude_code"]["lean"]
    assert record == {"window": 200000, "previous": {"present": False, "value": None}}


def test_off_removes_the_key_when_it_was_absent(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    vs.lean_on(path, 200_000)
    assert vs.lean_off(path) == "off"
    assert KEY not in _env(path)
    assert "lean" not in _state(path)["claude_code"]
    # The rest of the env is untouched.
    assert _env(path)["FOO"] == "1"


def test_off_restores_a_previous_value_the_user_had(tmp_path: Path) -> None:
    path = _settings(tmp_path, {**USER_SETTINGS, "env": {"FOO": "1", KEY: "500000"}})
    _connect_claude_code(path)
    vs.lean_on(path, 200_000)
    assert _state(path)["claude_code"]["lean"]["previous"] == {
        "present": True,
        "value": "500000",
    }
    assert vs.lean_off(path) == "off"
    assert _env(path)[KEY] == "500000"


def test_user_edit_is_left_alone_on_off(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    vs.lean_on(path, 200_000)
    payload = _read(path)
    payload["env"][KEY] = "150000"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert vs.lean_off(path) == "user-edited"
    assert _env(path)[KEY] == "150000"
    assert "lean" not in _state(path)["claude_code"]


def test_user_edit_blocks_a_window_change_without_overwriting(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    vs.lean_on(path, 200_000)
    payload = _read(path)
    payload["env"][KEY] = "150000"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert vs.lean_on(path, 300_000) == "user-edited"
    assert _env(path)[KEY] == "150000"


def test_on_is_idempotent(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    assert vs.lean_on(path, 200_000) == "on"
    before = _state(path)
    assert vs.lean_on(path, 200_000) == "unchanged"
    assert _state(path) == before
    assert _env(path)[KEY] == "200000"


def test_window_update_keeps_the_original_previous_value(tmp_path: Path) -> None:
    path = _settings(tmp_path, {**USER_SETTINGS, "env": {"FOO": "1", KEY: "500000"}})
    _connect_claude_code(path)
    vs.lean_on(path, 200_000)
    assert vs.lean_on(path, 150_000) == "on"
    lean = _state(path)["claude_code"]["lean"]
    assert lean["window"] == 150000
    assert lean["previous"] == {"present": True, "value": "500000"}
    assert _env(path)[KEY] == "150000"
    vs.lean_off(path)
    assert _env(path)[KEY] == "500000"


def test_on_requires_the_claude_code_connector(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    with pytest.raises(click.ClickException, match="not connected"):
        vs.lean_on(path, 200_000)
    # A VS Code-only route is not the Claude Code connector.
    vs.configure_vscode_claude_settings(path, vs.vscode_claude_proxy_url(PORT))
    assert vs.claude_code_connected(path) is False
    with pytest.raises(click.ClickException, match="not connected"):
        vs.lean_on(path, 200_000)


def test_off_when_lean_was_never_on_changes_nothing(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    before = _read(path)
    assert vs.lean_off(path) == "unchanged"
    assert _read(path) == before


def test_out_of_range_windows_are_refused(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    for bad in (99_999, 1_000_001, 0, True, "200000"):
        with pytest.raises(click.ClickException):
            vs.lean_on(path, bad)  # type: ignore[arg-type]
    assert KEY not in _env(path)


def test_lean_status_reports_enabled_window_and_terminal_value(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    assert vs.lean_status(path) == {"enabled": False, "window": None, "terminal_value": None}
    _connect_claude_code(path)
    vs.lean_on(path, 200_000)
    assert vs.lean_status(path) == {
        "enabled": True,
        "window": 200000,
        "terminal_value": "200000",
    }
    vs.lean_off(path)
    assert vs.lean_status(path)["enabled"] is False


def test_incomplete_lean_record_is_refused_not_guessed(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    vs.lean_on(path, 200_000)
    state = _state(path)
    state["claude_code"]["lean"]["window"] = "abc"
    vs._write_json(vs._state_path(path), state)
    with pytest.raises(click.ClickException, match="incomplete lean record"):
        vs.lean_off(path)


# ---------------------------------------------------------------------------
# Disconnect and shared owners
# ---------------------------------------------------------------------------


def test_claude_code_disconnect_restores_the_window(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    vs.lean_on(path, 200_000)
    assert vs.remove_claude_code_settings(path, port=PORT) == "restored"
    assert _read(path) == USER_SETTINGS
    assert not vs._state_path(path).exists()


def test_claude_code_disconnect_restores_a_previous_value(tmp_path: Path) -> None:
    path = _settings(tmp_path, {**USER_SETTINGS, "env": {"FOO": "1", KEY: "500000"}})
    _connect_claude_code(path)
    vs.lean_on(path, 200_000)
    assert vs.remove_claude_code_settings(path, port=PORT) == "restored"
    assert _read(path)["env"] == {"FOO": "1", KEY: "500000"}


def test_disconnect_with_vscode_still_holding_the_route_restores_the_window(
    tmp_path: Path,
) -> None:
    path = _settings(tmp_path)
    vs.configure_vscode_claude_settings(path, vs.vscode_claude_proxy_url(PORT))
    _connect_claude_code(path)
    vs.lean_on(path, 200_000)
    assert vs.remove_claude_code_settings(path, port=PORT) == "kept"
    env = _env(path)
    assert KEY not in env
    assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    assert "claude_code" not in _state(path)


def test_vscode_disconnect_keeps_the_window_for_claude_code(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    vs.configure_vscode_claude_settings(path, vs.vscode_claude_proxy_url(PORT))
    vs.lean_on(path, 200_000)
    assert vs.remove_vscode_claude_settings(path) is True
    assert _env(path)[KEY] == "200000"
    state = _state(path)
    assert state["owners"] == ["claude-code"]
    assert state["claude_code"]["lean"]["window"] == 200000
    assert vs.lean_off(path) == "off"
    assert KEY not in _env(path)


def test_state_without_a_lean_record_still_disconnects_cleanly(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    assert vs.lean_status(path)["enabled"] is False
    assert vs.remove_claude_code_settings(path, port=PORT) == "restored"
    assert _read(path) == USER_SETTINGS


# ---------------------------------------------------------------------------
# codetab.json
# ---------------------------------------------------------------------------


def test_codetab_set_and_clear_the_window(tmp_path: Path) -> None:
    data = _codetab(tmp_path)
    assert dc.code_tab_lean(data=data) == {"state": "off", "window": None}
    assert dc.set_code_tab_lean(200_000, data=data) == {"state": "on", "window": 200000}
    config = json.loads((data / dc.CONFIG_NAME).read_text(encoding="utf-8"))
    assert config["env"] == {KEY: "200000"}
    assert dc.code_tab_lean(data=data) == {"state": "on", "window": 200000}
    assert dc.set_code_tab_lean(None, data=data) == {"state": "off", "window": None}
    config = json.loads((data / dc.CONFIG_NAME).read_text(encoding="utf-8"))
    assert config["env"] == {}


def test_codetab_reinstall_preserves_a_valid_window(tmp_path: Path) -> None:
    data = _codetab(tmp_path)
    dc.set_code_tab_lean(150_000, data=data)
    launcher = tmp_path / "launcher.exe"
    dc.install(
        roots=[],
        data=data,
        launcher=launcher,
        health_port=PORT,
        tool_search=True,
        environ={"CLAUDE_CONFIG_DIR": str(tmp_path / "no-claude-config")},
    )
    config = json.loads((data / dc.CONFIG_NAME).read_text(encoding="utf-8"))
    assert config["env"] == {"ENABLE_TOOL_SEARCH": "true", KEY: "150000"}


def test_codetab_reinstall_drops_an_invalid_window(tmp_path: Path) -> None:
    data = _codetab(tmp_path)
    config = json.loads((data / dc.CONFIG_NAME).read_text(encoding="utf-8"))
    config["env"] = {KEY: "200k"}
    (data / dc.CONFIG_NAME).write_text(json.dumps(config), encoding="utf-8")
    dc.install(
        roots=[],
        data=data,
        launcher=tmp_path / "launcher.exe",
        health_port=PORT,
        environ={"CLAUDE_CONFIG_DIR": str(tmp_path / "no-claude-config")},
    )
    config = json.loads((data / dc.CONFIG_NAME).read_text(encoding="utf-8"))
    assert config["env"] == {}


def test_codetab_not_installed_is_left_alone(tmp_path: Path) -> None:
    data = tmp_path / "ctx-data" / "claude-code"
    assert dc.set_code_tab_lean(200_000, data=data) == {"state": "not-installed", "window": None}
    assert not data.exists()
    assert dc.code_tab_lean(data=data) == {"state": "not-installed", "window": None}


def test_codetab_disabled_config_is_not_changed(tmp_path: Path) -> None:
    data = _codetab(tmp_path)
    config = json.loads((data / dc.CONFIG_NAME).read_text(encoding="utf-8"))
    config["enabled"] = False
    (data / dc.CONFIG_NAME).write_text(json.dumps(config), encoding="utf-8")
    assert dc.set_code_tab_lean(200_000, data=data)["state"] == "not-installed"
    assert dc.code_tab_lean(data=data)["state"] == "not-installed"
    config = json.loads((data / dc.CONFIG_NAME).read_text(encoding="utf-8"))
    assert KEY not in config["env"]


def test_codetab_rejects_out_of_range_windows(tmp_path: Path) -> None:
    data = _codetab(tmp_path)
    with pytest.raises(dc.CodetabError):
        dc.set_code_tab_lean(99_999, data=data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_on_and_off_json_contract(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    _codetab(tmp_path)

    code, _, payload = _run(*_cli_args(tmp_path, path, "on", "--json"))
    assert code == 0
    assert payload == {"window": 200000, "terminal": "on", "code_tab": "on"}

    code, _, payload = _run(*_cli_args(tmp_path, path, "off", "--json"))
    assert code == 0
    assert payload == {"window": None, "terminal": "off", "code_tab": "off"}


def test_cli_window_option_sets_the_value(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    _codetab(tmp_path)
    code, _, payload = _run(*_cli_args(tmp_path, path, "on", "--window", "150000", "--json"))
    assert code == 0
    assert payload["window"] == 150000
    assert _env(path)[KEY] == "150000"


@pytest.mark.parametrize("window", ["99999", "1000001", "200k", "0"])
def test_cli_rejects_windows_outside_the_range(tmp_path: Path, window: str) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    code, _, _ = _run(*_cli_args(tmp_path, path, "on", "--window", window))
    assert code != 0
    assert KEY not in _env(path)


def test_cli_status_json_follows_on_and_off(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    _codetab(tmp_path)

    _, _, before = _run(*_cli_args(tmp_path, path, "status", "--json"))
    assert before == {
        "enabled": False,
        "window": None,
        "terminal": "off",
        "terminal_value": None,
        "code_tab": "off",
    }

    _run(*_cli_args(tmp_path, path, "on", "--json"))
    _, _, after = _run(*_cli_args(tmp_path, path, "status", "--json"))
    assert after == {
        "enabled": True,
        "window": 200000,
        "terminal": "on",
        "terminal_value": "200000",
        "code_tab": "on",
    }


def test_cli_human_output_is_plain_sentences(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    code, output, _ = _run(*_cli_args(tmp_path, path, "on"))
    assert code == 0
    assert (
        "Claude Code will compact at 200,000 tokens. New sessions pick this up; restart open ones."
        in output
    )
    code, output, _ = _run(*_cli_args(tmp_path, path, "status"))
    assert "Lean sessions: on (compact at 200,000 tokens)" in output


def test_cli_reports_not_connected_terminal_and_not_installed_code_tab(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    code, output, _ = _run(*_cli_args(tmp_path, path, "on"))
    assert code != 0
    assert "Nothing to change" in output
    assert KEY not in _env(path)


def test_cli_partial_on_reports_the_skipped_target(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    code, _, payload = _run(*_cli_args(tmp_path, path, "on", "--json"))
    assert code == 0
    assert payload == {"window": 200000, "terminal": "on", "code_tab": "not-installed"}


def test_cli_off_without_connection_reports_not_connected(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    code, _, payload = _run(*_cli_args(tmp_path, path, "off", "--json"))
    assert code == 0
    assert payload == {"window": None, "terminal": "not-connected", "code_tab": "not-installed"}
    assert not (tmp_path / "claude" / ".horizon_wrap_settings.lock").exists()


def test_cli_user_edit_is_reported_and_left_alone(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    _run(*_cli_args(tmp_path, path, "on"))
    payload = _read(path)
    payload["env"][KEY] = "150000"
    path.write_text(json.dumps(payload), encoding="utf-8")

    code, output, data = _run(*_cli_args(tmp_path, path, "off", "--json"))
    assert code == 0
    assert data["terminal"] == "user-edited"
    assert _env(path)[KEY] == "150000"
    assert "lean" not in _state(path)["claude_code"]


def test_cli_human_output_says_a_user_edit_was_left_alone(tmp_path: Path) -> None:
    path = _settings(tmp_path)
    _connect_claude_code(path)
    _run(*_cli_args(tmp_path, path, "on"))
    payload = _read(path)
    payload["env"][KEY] = "150000"
    path.write_text(json.dumps(payload), encoding="utf-8")
    _, output, _ = _run(*_cli_args(tmp_path, path, "off"))
    assert "left alone" in output
    assert _env(path)[KEY] == "150000"
