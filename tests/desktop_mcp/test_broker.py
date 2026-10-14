from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp.backend_worker import Runtime
from horizon.desktop_mcp.broker import (
    BrokerClient,
    BrokerError,
    BrokerService,
    _BackendWorker,
    _worker_environment,
)
from horizon.desktop_mcp.broker_transport import (
    current_user_sid,
    pipe_name_for,
    serve_pipe,
    wake_server,
)
from horizon.desktop_mcp.measurement import TokenEstimator
from horizon.desktop_mcp.settings import load_settings


def _write_settings(path: Path, roots: list[Path], **updates: Any) -> None:
    data: dict[str, Any] = {
        "profile": "local",
        "approved_roots": [str(root) for root in roots],
        "max_source_bytes": 1024,
        "max_snapshot_bytes": 2048,
        "max_store_bytes": 4096,
    }
    data.update(updates)
    path.write_text(json.dumps(data), encoding="utf-8")


def _runtime(config: Path) -> Runtime:
    settings = load_settings(config)
    return Runtime(
        config,
        expected_broker_id=settings.broker_id,
        expected_max_clients=settings.max_broker_clients,
        expected_timeout=settings.broker_timeout_seconds,
    )


def test_worker_service_can_open_and_retrieve_an_opaque_snapshot(tmp_path: Path) -> None:
    source_root = tmp_path / "approved"
    source_root.mkdir()
    source = source_root / "pilot.md"
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")
    config = tmp_path / "settings.json"
    _write_settings(config, [source_root])

    runtime = _runtime(config)
    hits = runtime.request("search", {"query": "pilot", "limit": 5})["hits"]
    assert len(hits) == 1
    source_id = hits[0]["source_id"]
    assert source_id.startswith("cs1_")
    opened = runtime.request("open", {"source": source_id, "start_line": None, "end_line": None})
    assert opened["text"] == "one\r\ntwo\r\nthree\r\n"
    selected = runtime.request("retrieve", {"source_id": source_id, "start_line": 2, "end_line": 2})
    assert selected["text"] == "two\r\n"
    assert selected["tool"] == "retrieve"


def test_worker_forwards_extended_controls_and_keeps_legacy_retrieve_envelope(
    tmp_path: Path,
) -> None:
    config = tmp_path / "settings.json"
    _write_settings(config, [])
    runtime = _runtime(config)

    class RecordingBackend:
        def __init__(self) -> None:
            self.open_call = None
            self.retrieve_call = None

        def open(self, source, **kwargs):
            self.open_call = (source, kwargs)
            return {"source_id": "cs1_example", "version": "v1", "text": "page"}

        def retrieve(self, source_id, selector, **kwargs):
            self.retrieve_call = (source_id, selector, kwargs)
            return {"source_id": source_id, "version": "v1", "text": "excerpt"}

        def stats(self):
            return {}

        def search(self, query, limit=5):
            return {"hits": [], "hit_count": 0}

    backend = RecordingBackend()
    runtime.backend = backend
    opened = runtime.request(
        "open",
        {
            "source": "cs1_example",
            "start_line": None,
            "end_line": None,
            "mode": "focused",
            "query": "renewal",
            "result_token_budget": 512,
            "cursor": "opaque-cursor",
        },
    )
    assert opened["text"] == "page"
    assert backend.open_call == (
        "cs1_example",
        {
            "start_line": None,
            "end_line": None,
            "mode": "focused",
            "query": "renewal",
            "result_token_budget": 512,
            "cursor": "opaque-cursor",
        },
    )

    legacy = runtime.request(
        "retrieve", {"source_id": "cs1_example", "start_line": 2, "end_line": 3}
    )
    assert legacy["text"] == "excerpt"
    assert backend.retrieve_call == (
        "cs1_example",
        {"type": "lines", "start": 2, "end": 3},
        {"result_token_budget": None, "cursor": None},
    )

    selected = runtime.request(
        "retrieve",
        {
            "source_id": "cs1_example",
            "selector": {"type": "json_pointer", "pointer": "/items/0"},
            "result_token_budget": 256,
            "cursor": "retrieve-cursor",
        },
    )
    assert selected["text"] == "excerpt"
    assert backend.retrieve_call == (
        "cs1_example",
        {"type": "json_pointer", "pointer": "/items/0"},
        {"result_token_budget": 256, "cursor": "retrieve-cursor"},
    )


def test_supervised_backend_runs_in_separate_process(tmp_path: Path) -> None:
    source_root = tmp_path / "approved"
    source_root.mkdir()
    (source_root / "pilot.md").write_text("worker result\n", encoding="utf-8")
    config = tmp_path / "settings.json"
    _write_settings(config, [source_root])
    service = BrokerService(config)
    try:
        result = service.handle("search", {"query": "pilot", "limit": 5})
        assert result["hits"][0]["source_id"].startswith("cs1_")
        assert service.worker._process is not None
        assert service.worker._process.pid != __import__("os").getpid()
    finally:
        service.worker._stop()


def test_config_root_change_and_empty_roots_revoke_snapshots(tmp_path: Path) -> None:
    source_root = tmp_path / "approved"
    source_root.mkdir()
    (source_root / "pilot.md").write_text("private sample\n", encoding="utf-8")
    config = tmp_path / "settings.json"
    _write_settings(config, [source_root])
    runtime = _runtime(config)
    hit = runtime.request("search", {"query": "pilot", "limit": 5})["hits"][0]
    source_id = hit["source_id"]

    _write_settings(config, [])
    stats = runtime.request("stats", {})
    assert stats["snapshot_count"] == 0
    with pytest.raises(BrokerError) as denied:
        runtime.request("open", {"source": source_id, "start_line": None, "end_line": None})
    assert denied.value.code == "snapshot_unknown"

    # Restoring the prior grant constructs a fresh store and does not revive IDs.
    _write_settings(config, [source_root])
    with pytest.raises(BrokerError) as still_denied:
        runtime.request("open", {"source": source_id, "start_line": None, "end_line": None})
    assert still_denied.value.code == "snapshot_unknown"


def test_worker_identity_change_requires_restart_and_drops_snapshots(tmp_path: Path) -> None:
    source_root = tmp_path / "approved"
    source_root.mkdir()
    (source_root / "pilot.md").write_text("private sample\n", encoding="utf-8")
    config = tmp_path / "settings.json"
    _write_settings(config, [source_root])
    runtime = _runtime(config)
    source_id = runtime.request("search", {"query": "pilot", "limit": 5})["hits"][0]["source_id"]

    _write_settings(config, [source_root], broker_id="renamed")
    with pytest.raises(BrokerError) as restart:
        runtime.request("stats", {})
    assert restart.value.code == "broker_restart_required"
    assert runtime.backend is None

    _write_settings(config, [source_root])
    with pytest.raises(BrokerError) as stale:
        runtime.request("open", {"source": source_id, "start_line": None, "end_line": None})
    assert stale.value.code == "snapshot_unknown"


def test_parent_does_not_reload_settings_on_each_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "approved"
    source_root.mkdir()
    (source_root / "pilot.md").write_text("worker result\n", encoding="utf-8")
    config = tmp_path / "settings.json"
    _write_settings(config, [source_root])
    service = BrokerService(config)

    def unexpected_parent_reload(_path: Path) -> Any:
        raise AssertionError("the parent must not read config during a request")

    monkeypatch.setattr("horizon.desktop_mcp.broker._load_local_settings", unexpected_parent_reload)
    try:
        result = service.handle("search", {"query": "pilot", "limit": 5})
        assert result["hits"][0]["source_id"].startswith("cs1_")
    finally:
        service.worker._stop()


def test_worker_environment_has_only_runtime_and_local_cache_paths() -> None:
    environment = _worker_environment(
        {
            "PATH": "C:/runtime",
            "SystemRoot": "C:/Windows",
            "TEMP": "C:/Users/test/AppData/Local/Temp",
            "TMP": "C:/Users/test/AppData/Local/Temp",
            "LOCALAPPDATA": "C:/Users/test/AppData/Local",
            "USERPROFILE": "C:/Users/test",
            "PYTHONPATH": "C:/untrusted/python",
            "ANTHROPIC_API_KEY": "do-not-inherit",
            "OPENAI_API_KEY": "do-not-inherit",
            "TIKTOKEN_CACHE_DIR": "C:/user/cache",
        }
    )
    assert environment == {
        "PATH": "C:/runtime",
        "SystemRoot": "C:/Windows",
        "TEMP": "C:/Users/test/AppData/Local/Temp",
        "TMP": "C:/Users/test/AppData/Local/Temp",
        "LOCALAPPDATA": "C:/Users/test/AppData/Local",
        "USERPROFILE": "C:/Users/test",
    }


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows named pipe broker")
def test_broker_client_uses_same_offline_token_count_provenance(tmp_path: Path) -> None:
    source_root = tmp_path / "approved"
    source_root.mkdir()
    (source_root / "pilot.md").write_text("A bounded local source.\n", encoding="utf-8")
    config = tmp_path / "settings.json"
    _write_settings(
        config,
        [source_root],
        counting_model="claude-sonnet-4-5-20250929",
    )
    settings = load_settings(config)
    service = BrokerService(config)
    pipe_name = pipe_name_for(current_user_sid(), settings.broker_id)
    stop = threading.Event()
    server = threading.Thread(
        target=serve_pipe,
        args=(pipe_name, service.handle),
        kwargs={
            "stop_event": stop,
            "max_clients": settings.max_broker_clients,
            "timeout_seconds": settings.broker_timeout_seconds,
            "expected_sid": current_user_sid(),
        },
        daemon=True,
    )
    server.start()
    try:
        payload = BrokerClient(settings).open(str(source_root / "pilot.md"))
    finally:
        stop.set()
        wake_server(pipe_name)
        server.join(3)
        service.worker._stop()

    parent_provenance = TokenEstimator(settings.counting_model).provenance()
    accounting = payload["accounting"]
    assert accounting["count_basis"] == parent_provenance["count_status"]
    assert accounting["counter_version"] == parent_provenance["counter_version"]
    assert accounting["tokenizer_name"] == parent_provenance["tokenizer_name"]
    assert accounting["configured_model_assumption"] == settings.counting_model
    assert accounting["fallback"] == parent_provenance["fallback"]
    assert not server.is_alive()


@pytest.mark.parametrize(
    "operation,args",
    [
        ("unknown", {}),
        ("stats", {"role": "admin"}),
        ("retrieve", {"source_id": "C:/private.txt", "start_line": 1, "end_line": 1}),
        ("retrieve", {"source_id": "cs1_fake", "start_line": None, "end_line": None}),
    ],
)
def test_worker_rejects_unknown_privileged_and_non_handle_requests(
    tmp_path: Path, operation: str, args: dict[str, Any]
) -> None:
    config = tmp_path / "settings.json"
    _write_settings(config, [])
    runtime = _runtime(config)
    with pytest.raises(BrokerError):
        runtime.request(operation, args)


class _FakeStdin:
    def write(self, value: bytes | memoryview) -> int:
        return len(value)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _HungProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = None
        self.killed = False

    def poll(self) -> None:
        return None

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float) -> int:
        return 0


class _BlockingStdin:
    def __init__(self, process: _BlockingProcess) -> None:
        self.process = process

    def write(self, value: bytes | memoryview) -> int:
        self.process.killed_event.wait(5)
        raise OSError("worker exited")

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.process.killed_event.set()


class _BlockingProcess:
    def __init__(self) -> None:
        self.killed_event = threading.Event()
        self.stdin = _BlockingStdin(self)
        self.stdout = None

    def poll(self) -> None:
        return None

    def kill(self) -> None:
        self.killed_event.set()

    def wait(self, timeout: float) -> int:
        return 0


def test_backend_worker_timeout_kills_process_and_drops_state(tmp_path: Path) -> None:
    worker = _BackendWorker(
        tmp_path / "settings.json", timeout_seconds=0, broker_id="desktop", max_clients=4
    )
    process = _HungProcess()
    worker._process = process  # type: ignore[assignment]
    worker._responses = queue.Queue()
    with pytest.raises(BrokerError) as caught:
        worker.request("stats", {})
    assert caught.value.code == "source_operation_timeout"
    assert process.killed
    assert worker._process is None
    assert worker._responses is None


def test_backend_worker_deadline_covers_blocked_request_write(tmp_path: Path) -> None:
    worker = _BackendWorker(
        tmp_path / "settings.json", timeout_seconds=0.1, broker_id="desktop", max_clients=4
    )
    process = _BlockingProcess()
    worker._process = process  # type: ignore[assignment]
    worker._responses = queue.Queue()
    started = time.monotonic()
    with pytest.raises(BrokerError) as caught:
        worker.request("search", {"query": "x" * 16000, "limit": 5})
    assert caught.value.code == "source_operation_timeout"
    assert time.monotonic() - started < 1
    assert process.killed_event.is_set()
