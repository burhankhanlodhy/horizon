"""Supervised per-user broker and the read-only MCP pipe client adapter."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .broker_transport import (
    ARTIFACT_CHUNK_BYTES,
    MAX_FRAME_BYTES,
    BrokerTransportError,
    current_user_sid,
    exchange,
    pipe_name_for,
    serve_pipe,
    wake_server,
)
from .roles import issue_capability, load_capability, revoke_capability
from .settings import ConfigurationError, DesktopSettings, load_settings

_ARTIFACT_ID = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class BrokerError(Exception):
    """Sanitized broker failure, optionally carrying exact-retrieval metadata."""

    def __init__(self, code: str, metadata: dict[str, Any] | None = None) -> None:
        self.code = code
        self.metadata = metadata or {}
        super().__init__(code)


_WORKER_ENVIRONMENT_KEYS = ("PATH", "SystemRoot", "TEMP", "TMP", "LOCALAPPDATA", "USERPROFILE")


def _worker_environment(source: Any | None = None) -> dict[str, str]:
    """Pass only required runtime and local-cache paths to the isolated worker."""
    parent = os.environ if source is None else source
    return {
        key: value
        for key in _WORKER_ENVIRONMENT_KEYS
        if isinstance((value := parent.get(key)), str)
    }


def _load_local_settings(path: Path) -> DesktopSettings:
    settings = load_settings(path)
    if settings.profile != "local":
        raise ConfigurationError("local_profile_required")
    return settings


def _frame(payload: bytes) -> bytes:
    if len(payload) > MAX_FRAME_BYTES:
        raise BrokerError("frame_too_large")
    return b"CSM\x01" + len(payload).to_bytes(4, "big") + payload


def _read_frame(stream: Any) -> dict[str, Any]:
    header = _read_stream_exact(stream, 8)
    if len(header) != 8 or header[:4] != b"CSM\x01":
        raise BrokerError("worker_protocol_error")
    size = int.from_bytes(header[4:], "big")
    if size > MAX_FRAME_BYTES or size == 0:
        raise BrokerError("worker_protocol_error")
    payload = _read_stream_exact(stream, size)
    if len(payload) != size:
        raise BrokerError("worker_protocol_error")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise BrokerError("worker_protocol_error") from None
    if not isinstance(value, dict):
        raise BrokerError("worker_protocol_error")
    return value


def _read_stream_exact(stream: Any, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        piece = stream.read(size - len(chunks))
        if not piece:
            break
        chunks.extend(piece)
    return bytes(chunks)


def _worker_reader(process: subprocess.Popen[bytes], output: queue.Queue[Any]) -> None:
    try:
        while True:
            output.put(_read_frame(process.stdout))
    except Exception:
        try:
            output.put_nowait(None)
        except queue.Full:
            pass


class _BackendWorker:
    """Persistent isolated backend with a hard per-operation response deadline."""

    def __init__(
        self,
        config_path: Path,
        timeout_seconds: int,
        *,
        broker_id: str,
        max_clients: int,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.timeout_seconds = timeout_seconds
        self.broker_id = broker_id
        self.max_clients = max_clients
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._responses: queue.Queue[Any] | None = None

    def _start(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        bootstrap = (
            "import sys; "
            f"sys.path.insert(0, {str(repo_root)!r}); "
            "from horizon.desktop_mcp.backend_worker import main; "
            "raise SystemExit(main())"
        )
        environment = _worker_environment()
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    bootstrap,
                    "--config",
                    str(self.config_path),
                    "--expected-broker-id",
                    self.broker_id,
                    "--expected-max-clients",
                    str(self.max_clients),
                    "--expected-timeout",
                    str(self.timeout_seconds),
                ],
                cwd=repo_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                env=environment,
            )
        except OSError:
            raise BrokerError("backend_worker_unavailable") from None
        self._process = process
        self._responses = queue.Queue(maxsize=1)
        threading.Thread(
            target=_worker_reader,
            args=(process, self._responses),
            daemon=True,
            name="contextshrink-backend-reader",
        ).start()

    def _stop(self, *, graceful: bool = False) -> None:
        process = self._process
        self._process = None
        self._responses = None
        if process is None:
            return
        if graceful:
            # End of input lets the worker leave its loop and run its own
            # cleanup (artifact files, staged state) before any hard kill.
            try:
                if process.stdin is not None:
                    process.stdin.close()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def request(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            deadline = time.monotonic() + self.timeout_seconds
            if self._process is None or self._process.poll() is not None:
                self._stop()
                self._start()
            if time.monotonic() >= deadline:
                self._stop()
                raise BrokerError("source_operation_timeout")
            assert self._process is not None and self._process.stdin is not None
            assert self._responses is not None
            payload = json.dumps(
                {"operation": operation, "args": args},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            writer_status: queue.Queue[bool] = queue.Queue(maxsize=1)
            process = self._process

            def write_request() -> None:
                try:
                    frame = memoryview(_frame(payload))
                    offset = 0
                    while offset < len(frame):
                        count = process.stdin.write(frame[offset:])
                        if not count:
                            raise OSError("short worker write")
                        offset += count
                    process.stdin.flush()
                    writer_status.put_nowait(True)
                except Exception:
                    try:
                        writer_status.put_nowait(False)
                    except queue.Full:
                        pass

            writer = threading.Thread(
                target=write_request,
                daemon=True,
                name="contextshrink-backend-writer",
            )
            writer.start()
            try:
                response = None
                while time.monotonic() < deadline:
                    try:
                        if writer_status.get_nowait() is False:
                            raise BrokerError("backend_worker_unavailable")
                    except queue.Empty:
                        pass
                    try:
                        response = self._responses.get(
                            timeout=min(0.05, max(0.001, deadline - time.monotonic()))
                        )
                        break
                    except queue.Empty:
                        if process.poll() is not None:
                            raise BrokerError("backend_worker_unavailable") from None
                if response is None:
                    raise BrokerError("source_operation_timeout")
            except BrokerError:
                self._stop()
                writer.join(timeout=0.5)
                raise
            except Exception:
                self._stop()
                writer.join(timeout=0.5)
                raise BrokerError("backend_worker_unavailable") from None
            if response is None:
                self._stop()
                raise BrokerError("backend_worker_unavailable")
            if set(response) != {"result"} or not isinstance(response["result"], dict):
                self._stop()
                raise BrokerError("worker_protocol_error")
            return response["result"]


class BrokerService:
    """Serve one serialized request at a time through a bounded worker process."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)
        self.settings = _load_local_settings(self.config_path)
        self.worker = _BackendWorker(
            self.config_path,
            self.settings.broker_timeout_seconds,
            broker_id=self.settings.broker_id,
            max_clients=self.settings.max_broker_clients,
        )
        self._lock = threading.RLock()

    def handle(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        # Config reload and all source IO stay inside the cancellable worker.
        # The pipe service keeps only the startup pipe identity and worker handle.
        with self._lock:
            result = self.worker.request(operation, args)
            if "error" in result:
                code = result.get("error", {}).get("code", "source_operation_failed")
                metadata = result.get("metadata")
                raise BrokerError(code, metadata if isinstance(metadata, dict) else None)
            return result

    def serve(
        self,
        *,
        stop_event: threading.Event | None = None,
        capability_base_dir: Path | None = None,
    ) -> None:
        sid = current_user_sid()
        pipe_name = pipe_name_for(sid, self.settings.broker_id)
        issued: list[str] = []

        def issue() -> str:
            issued.append(issue_capability(self.settings, base_dir=capability_base_dir))
            return issued[-1]

        try:
            serve_pipe(
                pipe_name,
                self.handle,
                stop_event=stop_event,
                max_clients=self.settings.max_broker_clients,
                timeout_seconds=self.settings.broker_timeout_seconds,
                expected_sid=sid,
                capability_factory=issue,
            )
        finally:
            try:
                self.worker._stop(graceful=True)
            finally:
                for capability in issued:
                    try:
                        revoke_capability(self.settings, capability, base_dir=capability_base_dir)
                    except OSError:
                        # Cleanup must not mask the error that ended serving.
                        pass


class BrokerClient:
    """MCP-facing adapter; every tool call connects to the existing broker."""

    def __init__(self, settings: DesktopSettings) -> None:
        self.settings = settings
        self.sid = current_user_sid()
        self.pipe_name = pipe_name_for(self.sid, settings.broker_id)

    def _request(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = exchange(
                self.pipe_name,
                operation,
                args,
                timeout_seconds=self.settings.broker_timeout_seconds,
            )
        except BrokerTransportError as exc:
            raise BrokerError(exc.code) from None
        error = result.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            metadata = result.get("metadata")
            raise BrokerError(error["code"], metadata if isinstance(metadata, dict) else None)
        return result

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        return self._request("search", {"query": query, "limit": limit})

    def open(
        self,
        source_id: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        mode: str = "exact",
        query: str | None = None,
        result_token_budget: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "source": source_id,
            "start_line": start_line,
            "end_line": end_line,
        }
        # Preserve the Phase B wire shape for callers using exact reads with
        # no presentation controls. New MCP calls use the extended envelope.
        if (
            mode != "exact"
            or query is not None
            or result_token_budget is not None
            or cursor is not None
        ):
            args["mode"] = mode
            for key, value in (
                ("query", query),
                ("result_token_budget", result_token_budget),
                ("cursor", cursor),
            ):
                if value is not None:
                    args[key] = value
        return self._request(
            "open",
            args,
        )

    def retrieve(
        self,
        source_id: str,
        selector: dict[str, Any] | None = None,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        result_token_budget: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if selector is not None:
            args: dict[str, Any] = {
                "source_id": source_id,
                "selector": selector,
            }
            if result_token_budget is not None:
                args["result_token_budget"] = result_token_budget
            if cursor is not None:
                args["cursor"] = cursor
        else:
            args = {
                "source_id": source_id,
                "start_line": start_line,
                "end_line": end_line,
            }
            if result_token_budget is not None or cursor is not None:
                args.update({"result_token_budget": result_token_budget, "cursor": cursor})
        return self._request(
            "retrieve",
            args,
        )

    def analyze(
        self,
        source_id: str,
        operation: dict[str, Any],
        *,
        result_token_budget: int | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"source_id": source_id, "operation": operation}
        if result_token_budget is not None:
            args["result_token_budget"] = result_token_budget
        return self._request("analyze", args)

    def render(
        self,
        source_id: str,
        *,
        fmt: str,
        title: str,
        operation: dict[str, Any] | None = None,
        formula_policy: str = "escape",
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"source_id": source_id, "format": fmt, "title": title}
        if operation is not None:
            args["operation"] = operation
        args["formula_policy"] = formula_policy
        return self._request("render", args)

    def retrieve_batch(
        self,
        requests: list[dict[str, Any]],
        *,
        result_token_budget: int | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"requests": requests}
        if result_token_budget is not None:
            args["result_token_budget"] = result_token_budget
        return self._request("retrieve_batch", args)

    def stats(self) -> dict[str, Any]:
        return self._request("stats", {})


def _parse_artifact_chunk(
    result: dict[str, Any], artifact_id: str, offset: int
) -> tuple[bytes, int, str, bool]:
    """Validate one artifact_read reply; any shape problem is artifact_unavailable."""
    unavailable = BrokerError("artifact_unavailable")
    try:
        total = result["total_bytes"]
        digest = result["sha256"]
        eof = result["eof"]
        if (
            result.get("artifact_id") != artifact_id
            or result.get("offset") != offset
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total < 0
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or not isinstance(eof, bool)
            or not isinstance(result["data_base64"], str)
        ):
            raise unavailable
        chunk = base64.b64decode(result["data_base64"], validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error):
        raise unavailable from None
    if len(chunk) > ARTIFACT_CHUNK_BYTES:
        raise unavailable
    return chunk, total, digest, eof


def _check_artifact_id(artifact_id: object) -> str:
    if not isinstance(artifact_id, str) or _ARTIFACT_ID.fullmatch(artifact_id) is None:
        raise BrokerError("invalid_arguments")
    return artifact_id


class CompanionBrokerClient:
    """Companion-only client for the artifact operations.

    The capability is loaded for each request, so a broker restart that issues
    a new capability is picked up without restarting the companion.
    """

    def __init__(self, settings: DesktopSettings, *, base_dir: Path | None = None) -> None:
        self.settings = settings
        self.sid = current_user_sid()
        self.pipe_name = pipe_name_for(self.sid, settings.broker_id)
        self._base_dir = None if base_dir is None else Path(base_dir)

    def _request(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        capability = load_capability(self.settings, base_dir=self._base_dir)
        try:
            result = exchange(
                self.pipe_name,
                operation,
                args,
                timeout_seconds=self.settings.broker_timeout_seconds,
                capability=capability,
            )
        except BrokerTransportError as exc:
            raise BrokerError(exc.code) from None
        error = result.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            raise BrokerError(error["code"])
        return result

    def list_artifacts(self) -> list[dict[str, Any]]:
        artifacts = self._request("artifact_list", {}).get("artifacts")
        if not isinstance(artifacts, list) or not all(isinstance(item, dict) for item in artifacts):
            raise BrokerError("artifact_unavailable")
        return artifacts

    def read_artifact(self, artifact_id: str) -> bytes:
        """Read every chunk and verify the total length and SHA-256 end to end."""
        _check_artifact_id(artifact_id)
        unavailable = BrokerError("artifact_unavailable")
        data = bytearray()
        offset = 0
        total: int | None = None
        digest: str | None = None
        while True:
            result = self._request(
                "artifact_read",
                {"artifact_id": artifact_id, "offset": offset, "length": ARTIFACT_CHUNK_BYTES},
            )
            chunk, chunk_total, chunk_digest, eof = _parse_artifact_chunk(
                result, artifact_id, offset
            )
            if total is None:
                total, digest = chunk_total, chunk_digest
            elif chunk_total != total or chunk_digest != digest:
                # The artifact changed between chunks; never splice versions.
                raise unavailable
            data.extend(chunk)
            offset += len(chunk)
            if offset > total or (eof and offset != total):
                raise unavailable
            if eof:
                break
            if not chunk:
                raise unavailable
        if len(data) != total or hashlib.sha256(data).hexdigest() != digest:
            raise unavailable
        return bytes(data)

    def delete_artifact(self, artifact_id: str) -> bool:
        _check_artifact_id(artifact_id)
        deleted = self._request("artifact_delete", {"artifact_id": artifact_id}).get("deleted")
        if not isinstance(deleted, bool):
            raise BrokerError("artifact_unavailable")
        return deleted


def stop_broker(pipe_name: str, stop_event: threading.Event) -> None:
    stop_event.set()
    wake_server(pipe_name)
