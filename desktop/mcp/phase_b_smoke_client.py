"""End-to-end synthetic local-source and shared-broker smoke probe.

The probe creates only temporary text fixtures and an explicit temporary
settings file. It never edits Claude Desktop configuration or prints fixture
paths, source text, or raw errors.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

_STARTUP_TIMEOUT_SECONDS = 15.0
_CLIENT_TIMEOUT_SECONDS = 12.0


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in tuple(environment):
        upper = key.upper()
        if (
            upper.endswith("_API_KEY")
            or upper.endswith("_TOKEN")
            or upper
            in {
                "ANTHROPIC_API_KEY",
                "CLAUDE_API_KEY",
                "HORIZON_API_KEY",
            }
        ):
            environment.pop(key, None)
    return environment


def _decode_tool_result(result: Any) -> dict[str, Any]:
    content = getattr(result, "content", ())
    if len(content) != 1 or getattr(content[0], "type", None) != "text":
        raise RuntimeError("unexpected_result")
    payload = json.loads(content[0].text)
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected_result")
    return {"is_error": bool(getattr(result, "isError", False)), "payload": payload}


async def _run_sdk_calls(config: Path, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "horizon.desktop_mcp", "serve", "--config", str(config)],
        cwd=_repo_root(),
    )
    results: list[dict[str, Any]] = []
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=5),
        ) as session:
            await session.initialize()
            for call in calls:
                result = await session.call_tool(call["tool"], call["arguments"])
                results.append(_decode_tool_result(result))
    return results


def _client_worker() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError("invalid_request")
        config = Path(request["config"])
        calls = request["calls"]
        if not isinstance(calls, list) or len(calls) > 8:
            raise ValueError("invalid_request")
        results = asyncio.run(_run_sdk_calls(config, calls))
        json.dump({"results": results}, sys.stdout, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0
    except BaseException:
        # The parent surfaces only a failed check name, never an exception or
        # stderr that might contain local paths or source-provider details.
        sys.stdout.write('{"status":"client_failed"}\n')
        return 1


def _run_client(
    config: Path,
    calls: list[dict[str, Any]],
    *,
    timeout: float = _CLIENT_TIMEOUT_SECONDS,
) -> list[dict[str, Any]] | None:
    request = json.dumps({"config": str(config), "calls": calls}, separators=(",", ":"))
    try:
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--client-worker"],
            cwd=_repo_root(),
            env=_child_environment(),
            input=request,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            return None
        response = json.loads(completed.stdout)
        results = response.get("results") if isinstance(response, dict) else None
        if not isinstance(results, list) or len(results) != len(calls):
            return None
        if not all(
            isinstance(item, dict) and isinstance(item.get("payload"), dict) for item in results
        ):
            return None
        return results
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return None


def _write_settings(path: Path, roots: list[Path], broker_id: str) -> None:
    settings = {
        "profile": "local",
        "approved_roots": [str(root) for root in roots],
        "broker_id": broker_id,
        "max_result_bytes": 4096,
        "max_source_bytes": 1 * 1024 * 1024,
        "max_snapshot_bytes": 4 * 1024 * 1024,
        "max_store_bytes": 16 * 1024 * 1024,
        "max_snapshots": 16,
        "broker_timeout_seconds": 2,
        "snapshot_ttl_seconds": 3600,
    }
    path.write_text(json.dumps(settings, separators=(",", ":")), encoding="utf-8")


def _call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"tool": tool, "arguments": arguments}


def _run_one(
    config: Path,
    call: dict[str, Any],
    *,
    timeout: float = _CLIENT_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    results = _run_client(config, [call], timeout=timeout)
    return results[0] if results else None


def _is_open(result: dict[str, Any] | None) -> bool:
    return bool(result and not result["is_error"] and "source_id" in result["payload"])


def _error_code(result: dict[str, Any] | None) -> str | None:
    if not result:
        return None
    error = result["payload"].get("error")
    return error.get("code") if isinstance(error, dict) else None


def _stop_broker(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def run_smoke() -> tuple[dict[str, Any], int]:
    checks = {
        "broker_started": False,
        "filename_search": False,
        "approved_file_open": False,
        "cross_process_snapshot_retrieval": False,
        "old_snapshot_unchanged_after_mutation": False,
        "new_version_after_mutation": False,
        "oversized_open_returns_retrieval_handle": False,
        "exact_range_retrieval": False,
        "outside_path_denied": False,
        "revoked_handle_denied": False,
        "children_stopped": False,
    }
    broker: subprocess.Popen[str] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="contextshrink-phase-b-") as temporary:
            work = Path(temporary)
            approved = work / "approved"
            approved.mkdir()
            source = approved / "meeting-notes.txt"
            original_text = "alpha\r\nbeta line\ncharlie\r\n"
            source.write_bytes(original_text.encode("utf-8"))
            large = approved / "large.txt"
            large.write_bytes(
                "".join(f"row-{line:04d} synthetic payload\n" for line in range(3000)).encode(
                    "utf-8"
                )
            )
            outside = work / "outside.txt"
            outside.write_text("synthetic unauthorized fixture", encoding="utf-8")
            config = work / "settings.json"
            broker_id = f"smoke_{uuid.uuid4().hex[:16]}"
            _write_settings(config, [approved], broker_id)
            broker = subprocess.Popen(
                [sys.executable, "-m", "horizon.desktop_mcp", "broker", "--config", str(config)],
                cwd=_repo_root(),
                env=_child_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )

            deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
            ready = False
            while time.monotonic() < deadline and broker.poll() is None:
                probe = _run_one(config, _call("contextshrink_stats", {}), timeout=4)
                if probe and not probe["is_error"]:
                    ready = True
                    break
                time.sleep(0.1)
            checks["broker_started"] = ready
            if not ready:
                raise RuntimeError("broker_start_failed")

            search_result = _run_one(
                config,
                _call("contextshrink_search", {"query": "meeting", "limit": 5}),
            )
            opened = _run_one(
                config,
                _call("contextshrink_open", {"source": str(source), "mode": "exact"}),
            )
            checks["filename_search"] = bool(
                search_result
                and not search_result["is_error"]
                and any(
                    item.get("name") == source.name
                    for item in search_result["payload"].get("hits", [])
                )
            )
            checks["approved_file_open"] = bool(
                _is_open(opened) and opened["payload"].get("text") == original_text
            )
            if not _is_open(opened):
                raise RuntimeError("approved_open_failed")
            first_payload = opened["payload"]
            handle = first_payload["source_id"]

            # This independent client process launches a distinct MCP stdio
            # server, proving that a broker-owned handle crosses processes.
            second_client = _run_client(
                config,
                [
                    _call(
                        "contextshrink_retrieve",
                        {"source_id": handle, "selector": {"type": "lines", "start": 2, "end": 2}},
                    )
                ],
            )
            retrieved = second_client[0] if second_client else None
            checks["cross_process_snapshot_retrieval"] = bool(
                retrieved
                and not retrieved["is_error"]
                and retrieved["payload"].get("text") == "beta line\n"
            )

            source.write_bytes(b"new version after user edit\n")
            old_result = _run_one(
                config,
                _call("contextshrink_open", {"source": handle, "mode": "exact"}),
            )
            new_result = _run_one(
                config,
                _call("contextshrink_open", {"source": str(source), "mode": "exact"}),
            )
            checks["old_snapshot_unchanged_after_mutation"] = bool(
                old_result
                and not old_result["is_error"]
                and old_result["payload"].get("text") == original_text
            )
            checks["new_version_after_mutation"] = bool(
                _is_open(new_result)
                and new_result["payload"].get("text") == "new version after user edit\n"
            )

            large_open = _run_one(
                config,
                _call("contextshrink_open", {"source": str(large), "mode": "exact"}),
            )
            large_payload = large_open["payload"] if large_open else {}
            error_metadata = large_payload.get("metadata", {})
            error_handle = (
                error_metadata.get("source_id") if isinstance(error_metadata, dict) else None
            )
            page_handle = large_payload.get("source_id")
            paged_exact = bool(
                large_open
                and not large_open["is_error"]
                and large_payload.get("mode") == "exact"
                and large_payload.get("more_available") is True
                and isinstance(large_payload.get("next_cursor"), str)
            )
            error_with_handle = bool(
                large_open
                and large_open["is_error"]
                and _error_code(large_open) == "result_too_large"
                and isinstance(error_handle, str)
                and isinstance(error_metadata.get("line_count"), int)
            )
            checks["oversized_open_returns_retrieval_handle"] = paged_exact or error_with_handle
            large_handle = page_handle if paged_exact else error_handle
            if isinstance(large_handle, str):
                range_result = _run_one(
                    config,
                    _call(
                        "contextshrink_retrieve",
                        {
                            "source_id": large_handle,
                            "selector": {"type": "lines", "start": 100, "end": 101},
                        },
                    ),
                )
                checks["exact_range_retrieval"] = bool(
                    range_result
                    and not range_result["is_error"]
                    and range_result["payload"].get("selected_lines") == {"start": 100, "end": 101}
                    and range_result["payload"].get("text")
                    == "row-0099 synthetic payload\nrow-0100 synthetic payload\n"
                )

            outside_result = _run_one(
                config,
                _call("contextshrink_open", {"source": str(outside), "mode": "exact"}),
            )
            checks["outside_path_denied"] = bool(
                outside_result
                and outside_result["is_error"]
                and _error_code(outside_result) == "source_unapproved"
            )

            _write_settings(config, [], broker_id)
            revoked = _run_one(
                config,
                _call("contextshrink_open", {"source": handle, "mode": "exact"}),
            )
            checks["revoked_handle_denied"] = bool(revoked and revoked["is_error"])

    except BaseException:
        pass
    finally:
        if broker is not None:
            _stop_broker(broker)
            checks["children_stopped"] = broker.poll() is not None
        else:
            checks["children_stopped"] = True

    return _report(checks), 0 if all(checks.values()) else 1


def _report(checks: dict[str, bool]) -> dict[str, Any]:
    try:
        sdk_version = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        sdk_version = None
    return {
        "phase": "B",
        "status": "passed" if all(checks.values()) else "failed",
        "runtime": {
            "python": platform.python_version(),
            "platform": sys.platform,
            "mcp_sdk": sdk_version,
        },
        "synthetic_fixtures_only": True,
        "user_configuration_modified": False,
        "api_key_or_model_used": False,
        "checks": dict(checks),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the synthetic Phase B broker smoke probe")
    parser.add_argument("--client-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.client_worker:
        return _client_worker()
    report, status = run_smoke()
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
