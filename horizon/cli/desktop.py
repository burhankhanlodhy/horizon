"""``horizon desktop`` — editor hookups managed by the ContextShrink desktop app.

The app keeps a loopback forwarder running and points editor extensions at it.
Unlike ``horizon wrap vscode-claude`` these commands start no proxy and do not
block: ``connect`` writes the settings and exits, ``disconnect`` restores them.
They refuse to take over a setup that points somewhere else (for example the
user's own ``horizon wrap vscode-claude`` on another port).
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from horizon.cli.main import main

EDITORS = ("vscode-claude",)


def _vscode_claude_paths(settings_file: Path | None) -> tuple[Path, Path]:
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
        raise click.ClickException(f"Horizon state {state_path} is unreadable; refusing to edit.")


@main.group()
def desktop() -> None:
    """Editor hookups managed by the ContextShrink desktop app."""


_settings_option = click.option(
    "--settings-file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override the editor's settings file (tests, portable profiles).",
)


@desktop.command("status")
@click.argument("editor", type=click.Choice(EDITORS))
@click.option("--port", required=True, type=click.IntRange(1, 65535))
@_settings_option
def desktop_status(editor: str, port: int, settings_file: Path | None) -> None:
    """Print connected / other / off for EDITOR as JSON."""
    from horizon.providers.claude.vscode import vscode_claude_proxy_url

    _, state_path = _vscode_claude_paths(settings_file)
    url = _managed_url(state_path)
    if url is None:
        status = "off"
    elif url == vscode_claude_proxy_url(port):
        status = "connected"
    else:
        status = "other"
    click.echo(json.dumps({"editor": editor, "status": status}))


@desktop.command("connect")
@click.argument("editor", type=click.Choice(EDITORS))
@click.option("--port", required=True, type=click.IntRange(1, 65535))
@_settings_option
def desktop_connect(editor: str, port: int, settings_file: Path | None) -> None:
    """Point EDITOR at the forwarder on PORT (reversible with ``disconnect``)."""
    from horizon.providers.claude.vscode import (
        configure_vscode_claude_settings,
        vscode_claude_proxy_url,
    )

    settings, state_path = _vscode_claude_paths(settings_file)
    url = vscode_claude_proxy_url(port)
    existing = _managed_url(state_path)
    if existing is not None and existing != url:
        raise click.ClickException(
            f"Claude Code is already routed to {existing} by Horizon. "
            "Run `horizon unwrap vscode-claude` first."
        )
    action = configure_vscode_claude_settings(settings, url)
    click.echo(f"Claude Code settings {action}: {settings}")


@desktop.command("disconnect")
@click.argument("editor", type=click.Choice(EDITORS))
@click.option("--port", required=True, type=click.IntRange(1, 65535))
@_settings_option
def desktop_disconnect(editor: str, port: int, settings_file: Path | None) -> None:
    """Restore EDITOR's settings, if they point at the forwarder on PORT."""
    from horizon.providers.claude.vscode import (
        remove_vscode_claude_settings,
        vscode_claude_proxy_url,
    )

    settings, state_path = _vscode_claude_paths(settings_file)
    existing = _managed_url(state_path)
    if existing is None:
        click.echo("Nothing to restore.")
        return
    if existing != vscode_claude_proxy_url(port):
        # Someone else's setup (e.g. the user's own wrap); not ours to undo.
        click.echo(f"Left alone: Claude Code is routed to {existing}, not this app.")
        return
    remove_vscode_claude_settings(settings)
    click.echo(f"Restored Claude Code settings: {settings}")
