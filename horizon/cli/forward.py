"""``horizon forward`` — run the local relay to a remote Horizon proxy.

Binds loopback only, reads the per-user credential from the OS key store
(``horizon vault set``), and streams requests to the remote Horizon proxy.
"""

from __future__ import annotations

import sys

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
def forward_start(remote_url: str, port: int, host: str, upstream: str | None) -> None:
    """Start the loopback relay to REMOTE_URL."""
    from horizon.forwarder import run_forwarder

    try:
        run_forwarder(remote_url=remote_url, port=port, host=host, upstream=upstream)
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)
