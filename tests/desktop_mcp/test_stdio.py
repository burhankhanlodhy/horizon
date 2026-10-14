from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path

from horizon.desktop_mcp.capabilities import run_probe


def test_real_stdio_child_completes_and_can_be_restarted() -> None:
    # Run twice in series: this catches child/session cleanup failures that
    # would otherwise only appear on the next launch on Windows.
    first = asyncio.run(run_probe())
    second = asyncio.run(run_probe())

    for report in (first, second):
        assert report["check"] == "stdio_probe"
        assert report["status"] == "passed", report.get("error")
        assert report["probe"]["stdio_child_process"] == "passed"
        assert report["probe"]["exact_open_repeat"] == "passed"
        assert report["probe"]["unauthorized_source_rejected"] is True
        assert report["probe"]["unsupported_mode_rejected"] is True
        assert report["probe"]["stats_raw_only"] is True
        assert report["probe"]["cost_usd_reported"] is False
        assert report["probe"]["desktop_application_compatibility"] == "not_tested"

    assert first["probe"]["tools_listed"] == second["probe"]["tools_listed"]
    assert first["probe"]["negotiated_protocol_version"]
    assert first["probe"]["server_info"]["name"]
    assert first["probe"]["server_info"]["version"]
    assert first["probe"]["fixture_ids_discovered"] == [
        "fixture:json",
        "fixture:log",
        "fixture:note",
    ]


def test_absolute_launcher_starts_from_an_arbitrary_working_directory(tmp_path) -> None:
    async def launch_from_temp_directory() -> tuple[str, ...]:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        repository = Path(__file__).resolve().parents[2]
        launcher = repository / "desktop" / "mcp" / "launch_stdio.py"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(launcher)],
            cwd=tmp_path,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=3),
            ) as session:
                initialized = await session.initialize()
                assert "Diagnostic-only" in initialized.instructions
                tools = await session.list_tools()
                return tuple(tool.name for tool in tools.tools)

    names = asyncio.run(asyncio.wait_for(launch_from_temp_directory(), timeout=15))
    assert names == (
        "contextshrink_search",
        "contextshrink_open",
        "contextshrink_retrieve",
        "contextshrink_stats",
    )
