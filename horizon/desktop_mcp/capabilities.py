"""Runtime metadata and a fixture-only MCP stdio smoke probe.

The report intentionally describes the installed MCP SDK, not compatibility
with a particular desktop application.  The probe launches this package as a
real child process and exercises only the synthetic fixtures.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import platform
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

_EXPECTED_TOOLS = (
    "contextshrink_search",
    "contextshrink_open",
    "contextshrink_retrieve",
    "contextshrink_stats",
)
_PROBE_TIMEOUT_SECONDS = 15
_SESSION_READ_TIMEOUT_SECONDS = 3


def _sdk_metadata() -> dict[str, Any]:
    try:
        sdk_version: str | None = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        sdk_version = None

    protocol_version: str | None = None
    try:
        from mcp import types

        value = getattr(types, "LATEST_PROTOCOL_VERSION", None)
        if isinstance(value, str):
            protocol_version = value
    except ImportError:
        pass

    return {
        "installed": sdk_version is not None,
        "version": sdk_version,
        "sdk_protocol_version": protocol_version,
        "desktop_application_compatibility": "not_tested",
    }


def build_runtime_report() -> dict[str, Any]:
    """Return machine/runtime metadata without reading config or user content."""
    return {
        "phase": "C",
        "runtime": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "mcp_sdk": _sdk_metadata(),
        },
        "capabilities": {
            "protocol_compatibility": "installed_sdk_only",
            "claude_desktop": "not_tested",
            "desktop_apps": "not_tested",
            "billing": "unknown",
            "filesystem_access": "available_in_local_profile",
            "compression": "local_profile_only; exact/compact/focused policies",
            "token_counting": "offline_estimate_not_claude_count",
            "payload_accounting": "inner_result_json_only; sdk_and_host_overhead_unknown",
            "usage_ledger": "ephemeral_local_integrity_only",
            "enterprise_savings": "not_measured",
            "broker": "available_in_local_profile",
            "model_required": False,
            "api_key_required": False,
        },
    }


def _write_report(report: dict[str, Any], output: Path | None) -> str | None:
    if output is None:
        return None
    path = Path(output)
    try:
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, TypeError, ValueError) as error:
        return f"report_write_failed:{type(error).__name__}"
    return None


def run_doctor(output: Path | None = None) -> dict[str, Any]:
    """Return runtime capability metadata and optionally save it to a chosen path."""
    report = build_runtime_report()
    report["check"] = "doctor"
    report["status"] = "ready" if report["runtime"]["mcp_sdk"]["installed"] else "missing_sdk"
    output_error = _write_report(report, output)
    if output_error:
        report["output_error"] = output_error
    return report


def _text_from_result(result: Any) -> str:
    """Return a result's sole text block, rejecting unexpected content shapes."""
    content = getattr(result, "content", ())
    text_blocks = [
        block.text
        for block in content
        if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str)
    ]
    if len(content) != 1 or len(text_blocks) != 1:
        raise RuntimeError("unexpected_text_content")
    return text_blocks[0]


def _json_result(result: Any, *, error: bool = False) -> dict[str, Any]:
    if bool(getattr(result, "isError", False)) != error:
        raise RuntimeError("unexpected_tool_error_state")
    try:
        value = json.loads(_text_from_result(result))
    except (json.JSONDecodeError, TypeError):
        raise RuntimeError("tool_result_not_json") from None
    if not isinstance(value, dict):
        raise RuntimeError("tool_result_not_object")
    return value


def _contains_usd_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key.casefold() in {"usd", "cost_usd", "billing_usd"} or _contains_usd_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_usd_key(item) for item in value)
    return False


async def _probe_session() -> dict[str, Any]:
    # Keep the metadata-only doctor usable in environments that did not install
    # the optional MCP extra; only the live stdio probe needs these imports.
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    repo_root = Path(__file__).resolve().parents[2]
    if not (repo_root / "horizon" / "desktop_mcp").is_dir():
        raise RuntimeError("repository_root_unavailable")

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "horizon.desktop_mcp", "serve"],
        cwd=repo_root,
    )

    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=_SESSION_READ_TIMEOUT_SECONDS),
        ) as session:
            initialized = await session.initialize()

            listed = await session.list_tools()
            tool_names = tuple(tool.name for tool in listed.tools)
            if tool_names != _EXPECTED_TOOLS:
                raise RuntimeError("unexpected_tool_set")

            search = await session.call_tool(
                "contextshrink_search",
                {"query": "", "limit": 5},
            )
            search_payload = _json_result(search)
            hits = search_payload.get("hits")
            if not isinstance(hits, list):
                raise RuntimeError("search_hits_invalid")
            discovered_ids = {
                hit.get("source_id")
                for hit in hits
                if isinstance(hit, dict) and isinstance(hit.get("source_id"), str)
            }
            expected_ids = {"fixture:note", "fixture:log", "fixture:json"}
            if (
                discovered_ids != expected_ids
                or search_payload.get("hit_count") != len(expected_ids)
                or search_payload.get("returned_hit_count") != len(expected_ids)
            ):
                raise RuntimeError("fixture_discovery_failed")

            opened = await session.call_tool(
                "contextshrink_open",
                {"source": "fixture:note", "mode": "exact"},
            )
            repeated = await session.call_tool(
                "contextshrink_open",
                {"source": "fixture:note", "mode": "exact"},
            )
            opened_payload = _json_result(opened)
            repeated_payload = _json_result(repeated)
            from .backend import FixtureBackend

            expected_note = FixtureBackend().get_fixture("fixture:note")
            if expected_note is None:
                raise RuntimeError("expected_fixture_missing")
            if (
                opened_payload.get("source_id") != expected_note.source_id
                or opened_payload.get("text") != expected_note.text
                or repeated_payload.get("text") != expected_note.text
            ):
                raise RuntimeError("exact_open_repeat_failed")

            retrieved = await session.call_tool(
                "contextshrink_retrieve",
                {
                    "source_id": "fixture:note",
                    "selector": {"type": "lines", "start": 1, "end": 2},
                },
            )
            retrieved_payload = _json_result(retrieved)
            expected_lines = "".join(expected_note.text.splitlines(keepends=True)[:2])
            if (
                retrieved_payload.get("source_id") != expected_note.source_id
                or retrieved_payload.get("text") != expected_lines
            ):
                raise RuntimeError("retrieve_failed")

            unauthorized = await session.call_tool(
                "contextshrink_open",
                {"source": "file:///Windows/win.ini", "mode": "exact"},
            )
            unsupported = await session.call_tool(
                "contextshrink_open",
                {"source": "fixture:note", "mode": "compact"},
            )
            unauthorized_payload = _json_result(unauthorized, error=True)
            unsupported_payload = _json_result(unsupported, error=True)
            if unauthorized_payload.get("error", {}).get("code") != "source_unavailable":
                raise RuntimeError("unauthorized_source_not_rejected")
            if unsupported_payload.get("error", {}).get("code") != "feature_unavailable":
                raise RuntimeError("negative_case_not_rejected")

            stats = await session.call_tool("contextshrink_stats", {})
            stats_payload = _json_result(stats)
            if (
                stats_payload.get("mode") != "exact"
                or stats_payload.get("compression_applied") is not False
            ):
                raise RuntimeError("stats_not_raw")
            if _contains_usd_key(stats_payload):
                raise RuntimeError("unexpected_billing_field")

            protocol_version = initialized.protocolVersion
            server_info = initialized.serverInfo
            if not isinstance(protocol_version, str) or server_info is None:
                raise RuntimeError("handshake_metadata_missing")
            negotiated_server = {"name": server_info.name, "version": server_info.version}

    return {
        **build_runtime_report(),
        "check": "stdio_probe",
        "status": "passed",
        "probe": {
            "stdio_child_process": "passed",
            "tools_listed": list(_EXPECTED_TOOLS),
            "search_empty_query": "passed",
            "fixture_ids_discovered": sorted(discovered_ids),
            "exact_open_repeat": "passed",
            "line_retrieve": "passed",
            "unauthorized_source_rejected": True,
            "unsupported_mode_rejected": True,
            "stats_raw_only": True,
            "cost_usd_reported": False,
            "negotiated_protocol_version": protocol_version,
            "server_info": negotiated_server,
            "desktop_application_compatibility": "not_tested",
        },
    }


async def run_probe(output: Path | None = None) -> dict[str, Any]:
    """Exercise the real MCP stdio child using fixtures only; optionally save JSON."""
    try:
        report = await asyncio.wait_for(_probe_session(), timeout=_PROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        report = {
            **build_runtime_report(),
            "check": "stdio_probe",
            "status": "failed",
            "error": "probe_timeout",
        }
    except Exception as error:
        # Exception strings may contain implementation details or user values.
        report = {
            **build_runtime_report(),
            "check": "stdio_probe",
            "status": "failed",
            "error": f"probe_failed:{type(error).__name__}",
        }
    output_error = _write_report(report, output)
    if output_error:
        report["output_error"] = output_error
    return report
