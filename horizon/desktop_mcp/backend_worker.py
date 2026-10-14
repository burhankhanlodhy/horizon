"""Private bounded-JSON worker that owns the local snapshot store."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Any

from .broker import BrokerError, _load_local_settings
from .broker_transport import MAX_FRAME_BYTES, BrokerTransportError, _validate_operation_args
from .settings import ConfigurationError, DesktopSettings


def _read_exact(size: int) -> bytes:
    data = bytearray()
    stream = sys.stdin.buffer
    while len(data) < size:
        piece = stream.read(size - len(data))
        if not piece:
            return b""
        data.extend(piece)
    return bytes(data)


def _write_frame(value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        payload = b'{"error":{"code":"result_too_large"}}'
    stream = sys.__stdout__.buffer
    stream.write(b"CSM\x01" + len(payload).to_bytes(4, "big") + payload)
    stream.flush()


def _make_backend(settings: DesktopSettings) -> Any:
    try:
        from .local_backend import LocalBackend
        from .source_access import SourceReader
    except ImportError:
        raise BrokerError("local_backend_unavailable") from None
    reader = SourceReader(settings.approved_roots, max_source_bytes=settings.max_source_bytes)
    return LocalBackend(
        reader,
        artifact_root=settings.artifact_root,
        ttl_seconds=settings.snapshot_ttl_seconds,
        max_snapshot_bytes=settings.max_snapshot_bytes,
        max_store_bytes=settings.max_store_bytes,
        max_snapshots=settings.max_snapshots,
        result_token_budget=getattr(settings, "result_token_budget", 2000),
        max_result_bytes=settings.max_result_bytes,
        counting_model=getattr(settings, "counting_model", None),
        eval_arm=getattr(settings, "eval_arm", "optimized"),
        min_compaction_gain_tokens=getattr(settings, "min_compaction_gain_tokens", 64),
        min_compaction_gain_percent=getattr(settings, "min_compaction_gain_percent", 5.0),
        usage_stats_path=getattr(settings, "usage_stats_path", None),
    )


def _close_backend(backend: Any | None) -> None:
    close = getattr(backend, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


class Runtime:
    def __init__(
        self,
        config_path: Path,
        *,
        expected_broker_id: str,
        expected_max_clients: int,
        expected_timeout: int,
    ) -> None:
        self.config_path = config_path
        self.expected_identity = (expected_broker_id, expected_max_clients, expected_timeout)
        self.settings = _load_local_settings(config_path)
        self._check_broker_identity(self.settings)
        self.backend: Any | None = _make_backend(self.settings)

    def _drop_backend(self) -> None:
        backend, self.backend = self.backend, None
        _close_backend(backend)

    def close(self) -> None:
        """Release the backend's artifact store on worker shutdown."""
        self._drop_backend()

    def _check_broker_identity(self, settings: DesktopSettings) -> None:
        actual = (settings.broker_id, settings.max_broker_clients, settings.broker_timeout_seconds)
        if actual != self.expected_identity:
            self._drop_backend()
            self.settings = settings
            raise BrokerError("broker_restart_required")

    def _reload(self) -> None:
        try:
            updated = _load_local_settings(self.config_path)
        except ConfigurationError:
            self._drop_backend()
            raise BrokerError("configuration_unavailable") from None
        self._check_broker_identity(updated)
        rebuild = self.backend is None or any(
            getattr(updated, name) != getattr(self.settings, name)
            for name in (
                "approved_roots",
                "max_source_bytes",
                "snapshot_ttl_seconds",
                "max_snapshot_bytes",
                "max_store_bytes",
                "max_snapshots",
                "result_token_budget",
                "max_result_bytes",
                "counting_model",
                "eval_arm",
                "min_compaction_gain_tokens",
                "min_compaction_gain_percent",
                "artifact_root",
                "usage_stats_path",
            )
        )
        if rebuild:
            # A grant or bound change drops snapshots and every outstanding ID.
            self._drop_backend()
            self.settings = updated
            try:
                self.backend = _make_backend(updated)
            except Exception:
                self._drop_backend()
                raise BrokerError("configuration_unavailable") from None
        self.settings = updated

    @staticmethod
    def _validate(operation: str, args: Any) -> None:
        try:
            _validate_operation_args(operation, args)
        except BrokerTransportError:
            raise BrokerError("invalid_request") from None

    def request(self, operation: str, args: Any) -> dict[str, Any]:
        self._reload()
        self._validate(operation, args)
        assert self.backend is not None
        try:
            if operation == "search":
                result = self.backend.search(args["query"], limit=args["limit"])
            elif operation == "open":
                result = self.backend.open(
                    args["source"],
                    start_line=args["start_line"],
                    end_line=args["end_line"],
                    mode=args.get("mode", "exact"),
                    query=args.get("query"),
                    result_token_budget=args.get("result_token_budget"),
                    cursor=args.get("cursor"),
                )
            elif operation == "analyze":
                result = self.backend.analyze(
                    args["source_id"],
                    args["operation"],
                    result_token_budget=args.get("result_token_budget"),
                )
            elif operation == "render":
                result = self.backend.render(
                    args["source_id"],
                    fmt=args["format"],
                    title=args["title"],
                    operation=args.get("operation"),
                    formula_policy=args.get("formula_policy", "escape"),
                )
            elif operation == "retrieve_batch":
                result = self.backend.retrieve_batch(
                    args["requests"],
                    result_token_budget=args.get("result_token_budget"),
                )
            elif operation == "retrieve":
                selector = args.get("selector")
                if selector is None:
                    selector = {
                        "type": "lines",
                        "start": args["start_line"],
                        "end": args["end_line"],
                    }
                result = self.backend.retrieve(
                    args["source_id"],
                    selector,
                    result_token_budget=args.get("result_token_budget"),
                    cursor=args.get("cursor"),
                )
            # Companion-only: the broker has already checked the capability, and
            # _validate above checked the arguments.
            elif operation == "artifact_list":
                result = self.backend.artifact_list()
            elif operation == "artifact_read":
                result = self.backend.artifact_read(
                    args["artifact_id"], args["offset"], args["length"]
                )
            elif operation == "artifact_delete":
                result = self.backend.artifact_delete(args["artifact_id"])
            else:
                result = self.backend.stats()
        except Exception as exc:
            code = getattr(exc, "code", None)
            if isinstance(code, str) and code.replace("_", "").isalnum() and len(code) <= 64:
                raise BrokerError(
                    code, _safe_error_metadata(getattr(exc, "metadata", None))
                ) from None
            raise BrokerError("source_operation_failed") from None
        if not isinstance(result, dict):
            raise BrokerError("source_unavailable")
        result = dict(result)
        if operation in {"open", "retrieve"} and "text" in result:
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > self.settings.max_result_bytes:
                metadata = {
                    key: result[key]
                    for key in ("source_id", "version", "line_count")
                    if key in result and isinstance(result[key], (str, int))
                }
                return {"error": {"code": "result_too_large"}, "metadata": metadata}
        return result


_ERROR_METADATA_KEYS = ("source_id", "version", "line_count", "output_rows")


def _safe_error_metadata(metadata: Any) -> dict[str, Any] | None:
    """Keep only the known opaque identifiers and counts an error may carry."""
    if not isinstance(metadata, dict):
        return None
    safe = {
        key: metadata[key]
        for key in _ERROR_METADATA_KEYS
        if isinstance(metadata.get(key), (str, int))
        and not isinstance(metadata.get(key), bool)
        and len(str(metadata[key])) <= 256
    }
    return safe or None


def _read_request() -> dict[str, Any] | None:
    header = _read_exact(8)
    if not header:
        return None
    if len(header) != 8 or header[:4] != b"CSM\x01":
        raise BrokerError("worker_protocol_error")
    size = int.from_bytes(header[4:], "big")
    if size == 0 or size > MAX_FRAME_BYTES:
        raise BrokerError("worker_protocol_error")
    payload = _read_exact(size)
    if len(payload) != size:
        raise BrokerError("worker_protocol_error")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise BrokerError("invalid_json") from None
    if not isinstance(value, dict) or set(value) != {"operation", "args"}:
        raise BrokerError("invalid_request")
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--expected-broker-id", required=True)
    parser.add_argument("--expected-max-clients", type=int, required=True)
    parser.add_argument("--expected-timeout", type=int, required=True)
    args = parser.parse_args(argv)
    # The broker owns this process: it ends it by closing stdin (so cleanup
    # runs) and kills it on a timeout. A console Ctrl+C/Ctrl+Break reaches the
    # whole process group and must not kill the worker before that cleanup.
    for name in ("SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), signal.SIG_IGN)
            except (ValueError, OSError):
                pass
    protocol_stdout = sys.__stdout__
    sys.stdout = sys.stderr
    try:
        runtime = Runtime(
            args.config,
            expected_broker_id=args.expected_broker_id,
            expected_max_clients=args.expected_max_clients,
            expected_timeout=args.expected_timeout,
        )
    except (ConfigurationError, BrokerError):
        return 2
    try:
        while True:
            try:
                request = _read_request()
                if request is None:
                    return 0
                try:
                    result = runtime.request(request["operation"], request["args"])
                except BrokerError as exc:
                    result = {"error": {"code": exc.code}, "metadata": exc.metadata}
                except Exception:
                    result = {"error": {"code": "source_operation_failed"}}
                payload = json.dumps(
                    {"result": result}, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                if len(payload) > MAX_FRAME_BYTES:
                    payload = b'{"result":{"error":{"code":"result_too_large"}}}'
                protocol_stdout.buffer.write(b"CSM\x01" + len(payload).to_bytes(4, "big") + payload)
                protocol_stdout.buffer.flush()
            except BrokerError as exc:
                payload = json.dumps(
                    {"result": {"error": {"code": exc.code}}}, separators=(",", ":")
                ).encode()
                protocol_stdout.buffer.write(b"CSM\x01" + len(payload).to_bytes(4, "big") + payload)
                protocol_stdout.buffer.flush()
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
