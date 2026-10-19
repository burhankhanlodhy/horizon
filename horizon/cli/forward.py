"""``horizon forward`` — run the local relay to a remote Horizon proxy.

Binds loopback only, reads the per-user credential from the OS key store
(``horizon vault set``), and streams requests to the remote Horizon proxy.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from horizon.cli.main import main


@main.group()
def forward() -> None:
    """Relay local traffic to a remote Horizon proxy (loopback only)."""


@forward.command("start")
@click.option(
    "--remote",
    "remote_url",
    required=True,
    help="Base URL of the remote Horizon proxy (e.g. https://horizon.example.com).",
)
@click.option(
    "--port",
    default=18788,
    show_default=True,
    type=int,
    help="Loopback port to listen on.",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind address (must be loopback).",
)
@click.option(
    "--upstream",
    default=None,
    help=(
        "Provider base URL for tools on this port that use a provider the proxy "
        "is not configured for (e.g. https://api.kimi.com/coding/v1)."
    ),
)
@click.option(
    "--state-dir",
    "state_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Where Claude Code stats are written (default: %LOCALAPPDATA%\\ContextShrink).",
)
@click.option(
    "--pause-file",
    "pause_file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="While this file exists, model calls bypass Horizon (default: <state>/claude-code/paused).",
)
def forward_start(
    remote_url: str,
    port: int,
    host: str,
    upstream: str | None,
    state_dir: Path | None,
    pause_file: Path | None,
) -> None:
    """Start the loopback relay to REMOTE_URL."""
    from horizon.forwarder import run_forwarder

    try:
        run_forwarder(
            remote_url=remote_url,
            port=port,
            host=host,
            upstream=upstream,
            state_dir=state_dir,
            pause_file=pause_file,
        )
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)
