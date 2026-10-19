"""``horizon lean`` — keep Claude Code sessions lean.

Claude Code on 1M-context models only auto-compacts near 967k tokens, so long
sessions resend very large contexts every turn. ``on`` sets
``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` (a token count) for the two surfaces the
ContextShrink desktop app manages:

* the terminal's Claude Code, in the user settings, when the Claude Code
  connector is connected (``horizon desktop connect claude-code``);
* the Desktop Code tab, in ``codetab.json``, when it is installed and enabled.

``off`` puts back what Horizon replaced. Lean is off by default. Only sessions
started after a change pick it up.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from horizon import desktop_codetab as dc
from horizon.cli.main import main

DEFAULT_WINDOW = 200_000
_USER_EDITED = (
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW was changed in your Claude settings, so it was left alone."
)
_TERMINAL_SKIPPED = "Terminal Claude Code is not connected in the ContextShrink app; skipped."
_CODE_TAB_SKIPPED = "Desktop Code tab is not installed; skipped."


def _format_tokens(window: int) -> str:
    return f"{window:,} tokens"


@main.group()
def lean() -> None:
    """Keep Claude Code sessions lean: compact at a smaller window than 1M."""


_settings_option = click.option(
    "--settings-file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override the Claude Code user settings file (tests, portable profiles).",
)
_codetab_option = click.option(
    "--codetab-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Folder holding codetab.json (default %LOCALAPPDATA%\\ContextShrink\\claude-code).",
)
_json_option = click.option("--json", "as_json", is_flag=True, help="Print JSON.")


def _terminal(settings: Path, window: int | None) -> str:
    """Apply lean to the terminal settings; ``not-connected`` when Claude Code is not linked."""
    from horizon.cli.desktop import _settings_lock
    from horizon.providers.claude import vscode as vs

    if not vs.claude_code_connected(settings):
        return "not-connected"
    with _settings_lock(settings):
        if window is None:
            return vs.lean_off(settings)
        return vs.lean_on(settings, window)


def _terminal_status(settings: Path) -> dict[str, Any]:
    from horizon.providers.claude import vscode as vs

    status = vs.lean_status(settings)
    if status["enabled"]:
        state = "on"
    elif vs.claude_code_connected(settings):
        state = "off"
    else:
        state = "not-connected"
    return {**status, "terminal": state}


def _emit(payload: dict[str, Any], as_json: bool, lines: list[str]) -> None:
    if as_json:
        click.echo(json.dumps(payload))
    else:
        for line in lines:
            click.echo(line)


def _code_tab(window: int | None, codetab_dir: Path | None) -> str:
    try:
        return dc.set_code_tab_lean(window, data=codetab_dir)["state"]
    except dc.CodetabError as exc:
        raise click.ClickException(str(exc)) from exc


@lean.command("on")
@click.option(
    "--window",
    type=click.IntRange(dc.LEAN_WINDOW_MIN, dc.LEAN_WINDOW_MAX),
    default=DEFAULT_WINDOW,
    show_default=True,
    help="Compact at this many tokens (100000 to 1000000).",
)
@_json_option
@_settings_option
@_codetab_option
def lean_on(
    window: int, as_json: bool, settings_file: Path | None, codetab_dir: Path | None
) -> None:
    """Compact Claude Code sessions at --window tokens."""
    from horizon.cli.desktop import _paths

    settings, _ = _paths(settings_file)
    terminal = _terminal(settings, window)
    code_tab = _code_tab(window, codetab_dir)
    if terminal == "not-connected" and code_tab == "not-installed":
        raise click.ClickException(
            "Nothing to change: connect Claude Code in the ContextShrink app, or install the "
            "Desktop Code tab, first."
        )
    payload = {"window": window, "terminal": terminal, "code_tab": code_tab}
    where = _format_tokens(window)
    lines: list[str] = []
    if terminal in ("on", "unchanged"):
        lines.append(
            f"Claude Code will compact at {where}. New sessions pick this up; restart open ones."
        )
    elif terminal == "user-edited":
        lines.append(_USER_EDITED)
    else:
        lines.append(_TERMINAL_SKIPPED)
    if code_tab == "on":
        lines.append(f"Desktop Code tab will compact at {where}. Restart it to pick this up.")
    else:
        lines.append(_CODE_TAB_SKIPPED)
    _emit(payload, as_json, lines)


@lean.command("off")
@_json_option
@_settings_option
@_codetab_option
def lean_off(as_json: bool, settings_file: Path | None, codetab_dir: Path | None) -> None:
    """Put back the compaction setting Horizon replaced."""
    from horizon.cli.desktop import _paths

    settings, _ = _paths(settings_file)
    terminal = _terminal(settings, None)
    code_tab = _code_tab(None, codetab_dir)
    payload = {"window": None, "terminal": terminal, "code_tab": code_tab}
    lines: list[str] = []
    if terminal == "off":
        lines.append("Claude Code is back to its own compaction point. New sessions pick this up.")
    elif terminal == "user-edited":
        lines.append(_USER_EDITED)
    elif terminal == "not-connected":
        lines.append(_TERMINAL_SKIPPED)
    if code_tab == "off":
        lines.append("Desktop Code tab is back to its own compaction point.")
    elif code_tab == "not-installed":
        lines.append(_CODE_TAB_SKIPPED)
    if not lines:
        lines.append("Keep sessions lean is off.")
    _emit(payload, as_json, lines)


@lean.command("status")
@_json_option
@_settings_option
@_codetab_option
def lean_status(as_json: bool, settings_file: Path | None, codetab_dir: Path | None) -> None:
    """Show whether lean is on, and at what window, for each surface."""
    from horizon.cli.desktop import _paths

    settings, _ = _paths(settings_file)
    terminal = _terminal_status(settings)
    code_tab = dc.code_tab_lean(data=codetab_dir)
    enabled = terminal["terminal"] == "on" or code_tab["state"] == "on"
    window = terminal["window"] if terminal["terminal"] == "on" else code_tab["window"]
    payload = {
        "enabled": enabled,
        "window": window if enabled else None,
        "terminal": terminal["terminal"],
        "terminal_value": terminal["terminal_value"],
        "code_tab": code_tab["state"],
    }
    if enabled and window is not None:
        headline = f"Lean sessions: on (compact at {window:,} tokens)"
    else:
        headline = "Lean sessions: off"
    _emit(payload, as_json, [headline])
