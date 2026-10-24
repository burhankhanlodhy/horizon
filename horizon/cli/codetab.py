"""``horizon codetab`` — route Claude Desktop's Code tab through ContextShrink.

Windows only in practice. The Code tab ignores ``ANTHROPIC_BASE_URL``, so the
patcher swaps Desktop's ``claude.exe`` for the ContextShrink launcher. See
``horizon/desktop_codetab.py`` for the safety rules.
"""

from __future__ import annotations

from pathlib import Path

import click

from horizon import desktop_codetab as dc
from horizon.cli.main import main


def _path_option(*names: str, **kwargs):
    return click.option(
        *names, type=click.Path(file_okay=False, dir_okay=True, path_type=Path), **kwargs
    )


_ROOT_HELP = (
    "Claude Code root to patch (repeatable). Default: Claude Desktop's "
    "claude-code folders on this machine, or $CONTEXTSHRINK_CODETAB_ROOTS."
)


def _print_folders(folders: list[dc.FolderReport]) -> None:
    if not folders:
        click.echo("No Claude Code versions found under the roots.")
        return
    for report in folders:
        click.echo(report.line())


@main.group()
def codetab() -> None:
    """Route Claude Desktop's Code tab through the ContextShrink forwarder."""


@codetab.command("install")
@click.option("--root", "roots", multiple=True, type=click.Path(path_type=Path), help=_ROOT_HELP)
@_path_option(
    "--data-dir",
    default=None,
    help="ContextShrink data folder (default %LOCALAPPDATA%\\ContextShrink).",
)
@click.option(
    "--launcher",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Path to claude-code-launcher.exe (default <data dir>\\bin).",
)
@click.option(
    "--base-url",
    default=None,
    help="Loopback base URL for the Code tab, e.g. http://127.0.0.1:18788/k/<id>.",
)
@click.option(
    "--health-port",
    type=click.IntRange(1, 65535),
    default=dc.DEFAULT_HEALTH_PORT,
    show_default=True,
)
@click.option(
    "--tool-search/--no-tool-search",
    default=False,
    show_default=True,
    help="Set ENABLE_TOOL_SEARCH=true for the Code tab. Enable only after the proxy is verified.",
)
@click.option("--dry-run", is_flag=True, help="Report what would change without changing anything.")
@click.option("--watch", is_flag=True, help="Keep running and re-check every --interval seconds.")
@click.option("--interval", type=float, default=dc.WATCH_INTERVAL_SECONDS, show_default=True)
def codetab_install(
    roots: tuple[Path, ...],
    data_dir: Path | None,
    launcher: Path | None,
    base_url: str | None,
    health_port: int,
    tool_search: bool,
    dry_run: bool,
    watch: bool,
    interval: float,
) -> None:
    """Patch every Claude Code version folder and write codetab.json."""
    kwargs = {
        "roots": list(roots) or None,
        "data": data_dir,
        "launcher": launcher,
        "base_url": base_url,
        "health_port": health_port,
        "tool_search": tool_search,
        "dry_run": dry_run,
    }
    try:
        if not watch:
            result = dc.install(**kwargs)
            _print_folders(result.folders)
            if result.config_written:
                click.echo(f"Wrote {result.config_path}")
            return

        last: list[tuple[str, str]] | None = None
        last_error: str | None = None

        def show(result: dc.InstallResult) -> None:
            nonlocal last, last_error
            last_error = None
            current = [(r.action, r.status, str(r.folder)) for r in result.folders]
            if current != last:
                _print_folders(result.folders)
                last = current

        def show_error(exc: Exception) -> None:
            # Reported and retried on the next pass; the watcher keeps running.
            nonlocal last_error
            message = str(exc)
            if message != last_error:
                click.echo(f"waiting: {message}", err=True)
                last_error = message

        click.echo(f"Watching every {interval:g} s. Press Ctrl+C to stop.")
        dc.watch(
            dc.QuietInstaller(**kwargs),
            interval=interval,
            on_result=show,
            on_error=show_error,
        )
    except KeyboardInterrupt:
        click.echo("Stopped.")
    except dc.CodetabError as exc:
        raise click.ClickException(str(exc)) from exc


@codetab.command("remove")
@click.option("--root", "roots", multiple=True, type=click.Path(path_type=Path), help=_ROOT_HELP)
@_path_option("--data-dir", default=None, help="ContextShrink data folder.")
@click.option("--dry-run", is_flag=True, help="Report what would change without changing anything.")
def codetab_remove(roots: tuple[Path, ...], data_dir: Path | None, dry_run: bool) -> None:
    """Restore Claude Desktop's real claude.exe and delete the patcher's files."""
    try:
        result = dc.remove(roots=list(roots) or None, data=data_dir, dry_run=dry_run)
    except dc.CodetabError as exc:
        raise click.ClickException(str(exc)) from exc
    if result.left_for_npx:
        click.echo(
            "Left the Code tab alone: codetab.json belongs to npx contextshrink-claude "
            "(owner npx), so nothing was restored or switched off."
        )
        return
    _print_folders(result.folders)
    if result.config_removed:
        click.echo("Removed codetab.json")
    elif result.config_disabled:
        click.echo(
            "Kept codetab.json with enabled=false: some folders are still patched or locked."
        )


@codetab.command("status")
@click.option("--root", "roots", multiple=True, type=click.Path(path_type=Path), help=_ROOT_HELP)
@_path_option("--data-dir", default=None, help="ContextShrink data folder.")
@click.option(
    "--launcher",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Path to claude-code-launcher.exe (default <data dir>\\bin).",
)
def codetab_status(roots: tuple[Path, ...], data_dir: Path | None, launcher: Path | None) -> None:
    """Show each Claude Code version folder as patched, real, foreign or locked."""
    try:
        result = dc.status(roots=list(roots) or None, data=data_dir, launcher=launcher)
    except dc.CodetabError as exc:
        raise click.ClickException(str(exc)) from exc
    if not result.roots:
        click.echo("No Claude Desktop claude-code roots found.")
    click.echo(
        f"Launcher: {result.launcher} ({'present' if result.launcher_present else 'missing'})"
    )
    if result.config is None:
        click.echo(f"Config: {result.config_path} (not written)")
    else:
        click.echo(
            f"Config: enabled={result.config.get('enabled')} "
            f"base_url={result.config.get('base_url')} health_port={result.config.get('health_port')}"
        )
    _print_folders(result.folders)
