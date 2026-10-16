"""CLI: ``horizon gateway report`` summarises the gateway usage sink.

Reads ``gateway_usage.jsonl`` (written by ``horizon proxy`` for every
``/v1/messages`` request) and prints, per session, the request count, token
totals per billing class, the cache hit rate, and the list-price cost split by
class, followed by grand totals. Reading never prices anything: each line
already carries its cost, so a report does not change when the price table does.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click

from .main import main


def _parse_since(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise click.BadParameter(f"expected an ISO-8601 timestamp, got {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@main.group(name="gateway")
def gateway() -> None:
    """Desktop gateway usage metering (``gateway_usage.jsonl``)."""


@gateway.command(name="report")
@click.option(
    "--workspace",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Workspace directory holding gateway_usage.jsonl (default: HORIZON_WORKSPACE_DIR or ~/.horizon).",
)
@click.option(
    "--since",
    default=None,
    help="Only count requests at or after this ISO-8601 timestamp.",
)
@click.option(
    "--session",
    "session_id",
    default=None,
    help="Only count requests from this session id.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
def report_cmd(
    workspace: Path | None,
    since: str | None,
    session_id: str | None,
    as_json: bool,
) -> None:
    """Summarise gateway usage per session, with grand totals."""
    from horizon.gateway_usage import SINK_FILENAME, build_report, render_report, sink_path

    since_dt = _parse_since(since)
    path = (workspace / SINK_FILENAME) if workspace is not None else sink_path()
    report = build_report(path, since=since_dt, session=session_id)
    if as_json:
        click.echo(json.dumps(report, indent=2, sort_keys=True))
    else:
        click.echo(render_report(report))
