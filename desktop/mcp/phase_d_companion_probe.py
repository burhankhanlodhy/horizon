"""Real-process integration probe for the Phase D local companion.

The report is deliberately metadata-only: it contains no source text, handles,
temporary paths, authentication token, or local URL.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT = Path(__file__).with_name("phase-d-companion-report.json")
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class ProbeFailure(RuntimeError):
    pass


class HttpClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url
        self.token = token

    def call(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        raw = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Origin": self.base_url.rstrip("/"),
        }
        if raw is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url.rstrip("/") + path,
            data=raw,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            return error.code, payload


def require(condition: bool, assertion: str) -> None:
    if not condition:
        raise ProbeFailure(assertion)


def wait_for_broker(config: Path, timeout: float = 30.0) -> Any:
    from horizon.desktop_mcp.broker import BrokerClient
    from horizon.desktop_mcp.settings import load_settings

    settings = load_settings(config)
    client = BrokerClient(settings)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            client.stats()
            return client
        except Exception:
            time.sleep(0.25)
    raise ProbeFailure("broker_readiness_timeout")


def start_broker(config: Path, temp_root: Path) -> subprocess.Popen[bytes]:
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    stdout_path = temp_root / "broker.stdout"
    stderr_path = temp_root / "broker.stderr"
    # Keep streams on owned files so an unhandled process failure is diagnosable
    # locally without placing fixture text or config paths in the report.
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        return subprocess.Popen(
            [sys.executable, "-m", "horizon.desktop_mcp", "broker", "--config", str(config)],
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
        )


def stop_broker(process: subprocess.Popen[bytes], config: Path) -> None:
    if process.poll() is not None:
        return
    try:
        from horizon.desktop_mcp.broker_transport import pipe_name_for, wake_server
        from horizon.desktop_mcp.settings import load_settings

        pipe_name = pipe_name_for(
            __import__(
                "horizon.desktop_mcp.broker_transport", fromlist=["current_user_sid"]
            ).current_user_sid(),
            load_settings(config).broker_id,
        )
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGTERM)
        wake_server(pipe_name, timeout_seconds=1.0)
        process.wait(timeout=8)
    except Exception:
        # The process object was created by this probe and is the only process
        # this fallback may terminate.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=4)


def companion_server(
    config: Path, vault: Path, *, ttl_seconds: int = 55
) -> tuple[threading.Thread, threading.Event, dict[str, str], Path]:
    from horizon.desktop_mcp import companion as companion_module

    original_init = companion_module.SessionVault.__init__

    def short_ttl_init(self: Any, settings: Any, staging_root: Path) -> None:
        original_init(self, settings, staging_root)
        self.ttl_seconds = ttl_seconds

    companion_module.SessionVault.__init__ = short_ttl_init
    ready = threading.Event()
    details: dict[str, str] = {}
    stopped = threading.Event()

    def on_ready(url: str, token: str) -> None:
        details["url"] = url
        details["token"] = token
        # Vault path is a generated child of the approved root. It is observed
        # solely for cleanup verification; it is not returned in reports.
        try:
            children = list(vault.glob(".contextshrink-session-*"))
            if children:
                details["session_dir"] = str(children[-1])
        except OSError:
            pass
        ready.set()

    def run() -> None:
        try:
            companion_module.serve_companion(
                config=config,
                staging_root=vault,
                open_browser=False,
                port=0,
                on_ready=on_ready,
            )
        finally:
            companion_module.SessionVault.__init__ = original_init
            stopped.set()

    thread = threading.Thread(target=run, name="phase-d-companion-probe", daemon=True)
    thread.start()
    if not ready.wait(10):
        companion_module.SessionVault.__init__ = original_init
        raise ProbeFailure("companion_readiness_timeout")
    return thread, stopped, details, vault


def run_probe(report_path: Path) -> dict[str, Any]:
    if os.name != "nt":
        return {
            "schema_version": 1,
            "status": "skipped",
            "reason": "windows_named_pipe_runtime_required",
            "checks": {},
        }

    checks: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="cs-phase-d-probe-") as temp_name:
        temp_root = Path(temp_name)
        vault = temp_root / "approved-vault"
        vault.mkdir()
        config = temp_root / "probe-settings.json"
        broker_id = f"dprobe-{secrets.token_hex(10)}"
        config.write_text(
            json.dumps(
                {
                    "profile": "local",
                    "approved_roots": [str(vault.resolve())],
                    "broker_id": broker_id,
                    "max_source_bytes": 2 * 1024 * 1024,
                    "max_snapshot_bytes": 4 * 1024 * 1024,
                    "max_store_bytes": 8 * 1024 * 1024,
                    "snapshot_ttl_seconds": 60,
                }
            ),
            encoding="utf-8",
        )

        broker = start_broker(config, temp_root)
        companion_thread: threading.Thread | None = None
        companion_stopped: threading.Event | None = None
        client: HttpClient | None = None
        session_dir: Path | None = None
        try:
            broker_client = wait_for_broker(config)
            checks["real_broker_process"] = "passed"
            companion_thread, companion_stopped, ready, _ = companion_server(
                config, vault, ttl_seconds=6
            )
            client = HttpClient(ready["url"], ready["token"])
            session_info_status, session_info = client.call("GET", "/api/session")
            require(
                session_info_status == 200 and session_info.get("status") == "ready", "session_api"
            )
            require(session_info["staging"].get("enabled") is True, "staging_enabled")
            session_dir = Path(ready.get("session_dir", "")) if ready.get("session_dir") else None
            checks["session_http_api"] = "passed"

            text = (
                "# Probe source\n"
                + "\n".join(
                    f"record {index:02d}: ordinary diagnostic note" for index in range(1, 18)
                )
                + "\nrecord 18: HEARTBEAT_MARKER explicit recovery target\n"
            )
            # An absent consent value must not stage pasted content.
            status, rejected = client.call("POST", "/api/staging", {"text": text})
            require(
                status == 400 and rejected.get("error") == "invalid_request", "consent_required"
            )
            status, imported = client.call(
                "POST",
                "/api/staging",
                {"text": text, "consent": "store_for_session_recovery"},
            )
            require(status == 201 and imported.get("status") == "imported", "explicit_import")
            source = imported.get("source")
            require(
                isinstance(source, dict)
                and str(source.get("source_id", "")).startswith("cs1_")
                and str(source.get("version", "")).startswith("sha256:"),
                "real_broker_handle_metadata",
            )
            metadata_wire = json.dumps(imported, ensure_ascii=False)
            require(
                text not in metadata_wire and "HEARTBEAT_MARKER" not in metadata_wire,
                "import_no_text_leak",
            )
            checks["consented_import_and_handle"] = "passed"

            status, staged = client.call("GET", "/api/staging")
            require(status == 200 and len(staged.get("items", [])) == 1, "staging_list")
            require(
                "HEARTBEAT_MARKER" not in json.dumps(staged, ensure_ascii=False),
                "listing_no_text_leak",
            )
            checks["safe_staging_listing"] = "passed"

            status, reference = client.call(
                "POST",
                "/api/prepare",
                {
                    "request": "recover the marker",
                    "mode": "reference_only",
                    "source_id": source["source_id"],
                },
            )
            require(
                status == 200 and reference.get("status") == "preview_ready",
                "reference_only_prepare",
            )
            packet = json.loads(reference["packet_json"])
            packet_wire = json.dumps(packet, ensure_ascii=False)
            require("HEARTBEAT_MARKER" not in packet_wire, "reference_only_omits_source_body")
            exact_recovery = packet.get("supporting_context", {}).get("exact_recovery", {})
            require(
                exact_recovery.get("source_id") == source["source_id"], "reference_only_same_handle"
            )
            checks["reference_only_without_source_body"] = "passed"

            exact = broker_client.open(source["source_id"], mode="exact")
            require(exact.get("text") == text, "broker_exact_recovery")
            checks["exact_broker_recovery"] = "passed"

            status, focused = client.call(
                "POST",
                "/api/prepare",
                {
                    "request": "find the heartbeat record",
                    "mode": "focused",
                    "source_id": source["source_id"],
                    "query": "HEARTBEAT_MARKER",
                },
            )
            require(
                status == 200 and "partial" in str(focused.get("coverage", "")).lower(),
                "focused_partial_coverage",
            )
            focused_packet = json.loads(focused["packet_json"])
            require(
                "HEARTBEAT_MARKER" in json.dumps(focused_packet, ensure_ascii=False),
                "focused_target_retained",
            )
            checks["focused_bounded_coverage"] = "passed"

            long_text = "L" * (256 * 1024)
            status, long_import = client.call(
                "POST",
                "/api/staging",
                {"text": long_text, "consent": "store_for_session_recovery"},
            )
            require(status == 201, "long_single_line_import")
            status, long_reference = client.call(
                "POST",
                "/api/prepare",
                {
                    "request": "retain exact recovery for the long source",
                    "mode": "reference_only",
                    "source_id": long_import["source"]["source_id"],
                },
            )
            require(status == 200, "long_single_line_reference_only")
            long_packet = json.loads(long_reference["packet_json"])
            require("L" * 1024 not in json.dumps(long_packet), "long_source_omitted")
            checks["large_single_line_source"] = "passed"

            # Force a short lifetime only in this test server; the broker keeps
            # its normal TTL. The sweeper deletes expired files/records.
            time.sleep(10)
            status, after_ttl = client.call("GET", "/api/staging")
            require(status == 200 and after_ttl.get("items") == [], "ttl_cleanup")
            checks["ttl_cleanup"] = "passed"

            # Re-import one source and prove deletion blocks future broker reads.
            status, final_import = client.call(
                "POST",
                "/api/staging",
                {"text": "session cleanup proof\n", "consent": "store_for_session_recovery"},
            )
            require(status == 201, "reimport_for_revoke")
            final_entry = final_import["id"]
            final_source_id = final_import["source"]["source_id"]
            status, deleted = client.call("DELETE", f"/api/staging/{final_entry}")
            require(status == 200 and deleted.get("status") == "deleted", "explicit_remove")
            try:
                broker_client.open(final_source_id, mode="exact")
            except Exception as error:
                require(
                    getattr(error, "code", None) == "source_unavailable", "removed_handle_blocked"
                )
            else:
                raise ProbeFailure("removed_handle_still_recoverable")
            checks["remove_revokes_future_recovery"] = "passed"

            # Test graceful authenticated session quit and owned directory cleanup.
            status, quit_response = client.call("DELETE", "/api/session")
            require(status == 200 and quit_response.get("status") == "revoking", "session_quit")
            assert companion_stopped is not None
            require(companion_stopped.wait(8), "session_server_stopped")
            if session_dir is not None:
                deadline = time.monotonic() + 3
                while session_dir.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                require(not session_dir.exists(), "session_directory_cleanup")
            checks["authenticated_quit_cleanup"] = "passed"
        finally:
            if (
                client is not None
                and companion_stopped is not None
                and not companion_stopped.is_set()
            ):
                try:
                    client.call("DELETE", "/api/session")
                except Exception:
                    pass
                companion_stopped.wait(5)
            stop_broker(broker, config)

    return {
        "schema_version": 1,
        "status": "passed",
        "checks": checks,
        "evidence_scope": "real broker subprocess plus authenticated local HTTP companion",
    }


def run_browser_review() -> int:
    """Keep a real broker/companion alive for a manual browser review."""
    if os.name != "nt":
        print(json.dumps({"status": "skipped", "reason": "windows_named_pipe_runtime_required"}))
        return 0

    from horizon.desktop_mcp import companion as companion_module

    with tempfile.TemporaryDirectory(prefix="cs-phase-d-browser-") as temp_name:
        temp_root = Path(temp_name)
        vault = temp_root / "approved-vault"
        vault.mkdir()
        config = temp_root / "review-settings.json"
        config.write_text(
            json.dumps(
                {
                    "profile": "local",
                    "approved_roots": [str(vault.resolve())],
                    "broker_id": f"dreview-{secrets.token_hex(10)}",
                    "max_source_bytes": 2 * 1024 * 1024,
                    "max_snapshot_bytes": 4 * 1024 * 1024,
                    "max_store_bytes": 8 * 1024 * 1024,
                    "snapshot_ttl_seconds": 3600,
                }
            ),
            encoding="utf-8",
        )
        broker = start_broker(config, temp_root)
        url_path = temp_root / "browser-review.url"

        def write_url(local_url: str, token: str) -> None:
            # This is a short-lived local handoff for the reviewer only. It is
            # removed when the companion quits and is never included in reports.
            descriptor = os.open(url_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(f"{local_url}#{token}")
            print(
                json.dumps({"status": "browser_review_ready", "url_file": str(url_path)}),
                flush=True,
            )

        try:
            wait_for_broker(config)
            return companion_module.serve_companion(
                config=config,
                staging_root=vault,
                open_browser=False,
                port=0,
                on_ready=write_url,
            )
        finally:
            stop_broker(broker, config)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--browser-review",
        action="store_true",
        help="run a real local companion and keep it alive for manual UI review",
    )
    args = parser.parse_args()
    if args.browser_review:
        return run_browser_review()
    try:
        report = run_probe(args.output)
    except ProbeFailure as error:
        report = {
            "schema_version": 1,
            "status": "failed",
            "failed_check": str(error),
            "checks": {},
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] in {"passed", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
