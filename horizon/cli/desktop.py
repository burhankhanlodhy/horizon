"""``horizon desktop`` — Claude Code hookups managed by the ContextShrink desktop app.

The app keeps a loopback forwarder running and points Claude Code at it. Two
editors are supported:

* ``vscode-claude`` — the VS Code extension (user-level settings route).
* ``claude-code`` — the terminal and the Desktop Code tab's Claude Code: the
  same settings route through the keep-alive path, plus the status line and the
  plugin when the user has none.

Unlike ``horizon wrap vscode-claude`` these commands start no proxy and do not
block: ``connect`` writes the settings and exits, ``disconnect`` restores them.
They refuse to take over a setup that points somewhere else (for example the
user's own ``horizon wrap vscode-claude`` on another port). Settings are written
under the same lock ``horizon wrap`` uses.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from horizon.cli.main import main

EDITORS = ("vscode-claude", "claude-code")


def _paths(settings_file: Path | None) -> tuple[Path, Path]:
    from horizon.providers.claude.vscode import _state_path, claude_user_settings_path

    settings = settings_file or claude_user_settings_path()
    return settings, _state_path(settings)


def _managed_url(state_path: Path) -> str | None:
    """Base URL Horizon wrote, per its state file; None when not set up."""
    if not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return str(state["managed"]["ANTHROPIC_BASE_URL"])
    except (OSError, ValueError, KeyError, TypeError):
        raise click.ClickException(
            f"Horizon state {state_path} is unreadable; refusing to edit."
        ) from None


def _settings_lock(settings: Path):  # noqa: ANN202 - a context manager from wrap
    from horizon.cli.wrap import _wrap_settings_lock

    return _wrap_settings_lock(settings)


@main.group()
def desktop() -> None:
    """Claude Code hookups managed by the ContextShrink desktop app."""


_settings_option = click.option(
    "--settings-file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override the Claude Code user settings file (tests, portable profiles).",
)


def _claude_code_targets(settings: Path, state_path: Path, port: int) -> dict[str, str]:
    """Per-target state for the Claude Code connector, read back from disk."""
    from horizon.providers.claude.vscode import (
        _claude_code_record,
        _read_object,
        claude_code_connection_status,
    )

    terminal = claude_code_connection_status(settings, port)
    targets = {"terminal": terminal, "status_line": "off", "plugin": "off"}
    if not state_path.exists():
        return targets
    state = _read_object(state_path, label="Horizon state")
    record = _claude_code_record(state)
    payload = _read_object(settings, label="Claude settings") if settings.exists() else {}
    status = record.get("status_line") if isinstance(record.get("status_line"), dict) else {}
    if "statusLine" in payload:
        if status.get("owned") and payload["statusLine"] == {
            "type": "command",
            "command": status.get("command"),
        }:
            targets["status_line"] = "installed"
        else:
            targets["status_line"] = "kept-user"
    plugin = record.get("plugin") if isinstance(record.get("plugin"), dict) else {}
    if plugin.get("marketplace_added") or plugin.get("plugin_enabled_added"):
        targets["plugin"] = "installed"
    elif "enabledPlugins" in payload or "extraKnownMarketplaces" in payload:
        targets["plugin"] = "kept-user"
    return targets


@desktop.command("status")
@click.argument("editor", type=click.Choice(EDITORS))
@click.option("--port", required=True, type=click.IntRange(1, 65535))
@_settings_option
def desktop_status(editor: str, port: int, settings_file: Path | None) -> None:
    """Print EDITOR's status as JSON: connected / other / off, plus per-target detail."""
    from horizon.providers.claude.vscode import (
        claude_code_connection_status,
        vscode_connection_status,
    )

    settings, state_path = _paths(settings_file)
    if editor == "claude-code":
        status = claude_code_connection_status(settings, port)
        payload: dict[str, object] = {
            "editor": editor,
            "status": status,
            "targets": _claude_code_targets(settings, state_path, port),
        }
    else:
        payload = {"editor": editor, "status": vscode_connection_status(settings, port)}
    click.echo(json.dumps(payload))


@desktop.command("connect")
@click.argument("editor", type=click.Choice(EDITORS))
@click.option("--port", required=True, type=click.IntRange(1, 65535))
@click.option(
    "--status-exe",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="claude-code: the status line helper (default %LOCALAPPDATA%\\ContextShrink\\bin).",
)
@click.option(
    "--plugin-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="claude-code: the plugin marketplace directory (default under %LOCALAPPDATA%).",
)
@_settings_option
def desktop_connect(
    editor: str,
    port: int,
    status_exe: Path | None,
    plugin_dir: Path | None,
    settings_file: Path | None,
) -> None:
    """Point EDITOR at the forwarder on PORT (reversible with ``disconnect``)."""
    from horizon.providers.claude.vscode import (
        configure_vscode_claude_settings,
        connect_claude_code_settings,
        plugin_dir_default,
        route_port,
        status_exe_default,
        vscode_claude_proxy_url,
    )

    settings, state_path = _paths(settings_file)
    with _settings_lock(settings):
        existing = _managed_url(state_path)
        if existing is not None and route_port(existing) != port:
            raise click.ClickException(
                f"Claude Code is already routed to {existing} by Horizon. "
                "Run `horizon unwrap vscode-claude` first."
            )
        if editor == "claude-code":
            result = connect_claude_code_settings(
                settings,
                port=port,
                status_exe=status_exe or status_exe_default(),
                plugin_dir=plugin_dir or plugin_dir_default(),
            )
            click.echo(json.dumps({"editor": editor, **result}))
            return
        action = configure_vscode_claude_settings(settings, vscode_claude_proxy_url(port))
    click.echo(f"Claude Code settings {action}: {settings}")


@desktop.command("disconnect")
@click.argument("editor", type=click.Choice(EDITORS))
@click.option("--port", required=True, type=click.IntRange(1, 65535))
@_settings_option
def desktop_disconnect(editor: str, port: int, settings_file: Path | None) -> None:
    """Restore EDITOR's settings, if they point at the forwarder on PORT."""
    from horizon.providers.claude.vscode import (
        remove_claude_code_settings,
        remove_vscode_claude_settings,
        route_port,
    )

    settings, state_path = _paths(settings_file)
    with _settings_lock(settings):
        if editor == "claude-code":
            result = remove_claude_code_settings(settings, port=port)
            click.echo(json.dumps({"editor": editor, "result": result}))
            return
        existing = _managed_url(state_path)
        if existing is None:
            click.echo("Nothing to restore.")
            return
        if route_port(existing) != port:
            # Someone else's setup (e.g. the user's own wrap); not ours to undo.
            click.echo(f"Left alone: Claude Code is routed to {existing}, not this app.")
            return
        if remove_vscode_claude_settings(settings):
            click.echo(f"Disconnected vscode-claude: {settings}")
        else:
            click.echo("Nothing to restore.")
