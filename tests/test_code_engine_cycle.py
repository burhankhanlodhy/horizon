"""The engine's background cycle: restart after a death, failures that do not stop it, engine.log.

Real threads run here with short intervals. Clock-driven schedule rules are tested with a
fake clock in test_code_engine_usage.py.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import httpx
import pytest

from horizon.code_engine import runtime
from horizon.code_engine.device import device_hash
from horizon.code_engine.engine import Engine
from horizon.code_engine.store import HOME_ENV

MACHINE = "cycle-test-machine"


class _Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.setenv("CONTEXTSHRINK_MACHINE_ID", MACHINE)
    (home / "device.json").write_text(
        json.dumps({
            "version": 1,
            "api_url": "https://api.example.test",
            "device_id": "dev-1",
            "device_key": "cs_dev_test",
            "device_hash": device_hash(MACHINE),
            "tools": ["claude"],
            "created_at": "2026-10-23T00:00:00Z",
        }),
        encoding="utf-8",
    )
    return home


class _Api:
    """A fake /code/entitlement and /code/usage that records what arrived."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.usage_bodies: list[dict] = []
        self.refuse_entitlement = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.url.path == "/code/usage":
            self.usage_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if self.refuse_entitlement:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(402, json={"detail": {"code": "plan_required"}})

    def uploads(self) -> int:
        return self.paths.count("/code/usage")


def _engine(home, api: _Api, clock: _Clock | None = None) -> Engine:
    client = httpx.Client(transport=httpx.MockTransport(api), timeout=5.0)
    return Engine(home, client=client, clock=clock or _Clock(), on_stop_request=lambda: None)


def _finished_thread() -> threading.Thread:
    thread = threading.Thread(target=lambda: None, name="dead-cycle")
    thread.start()
    thread.join()
    return thread


def test_watchdog_restarts_a_cycle_thread_that_ended(home, caplog) -> None:
    engine = _engine(home, _Api())
    engine._thread = _finished_thread()  # the cycle died while the engine was running
    with caplog.at_level(logging.WARNING, logger="horizon.code_engine"):
        assert engine.supervise()
    assert engine._thread is not None and engine._thread.is_alive()
    assert any("ended unexpectedly" in record.getMessage() for record in caplog.records)
    engine.stop()


def test_watchdog_leaves_a_running_cycle_alone(home) -> None:
    engine = _engine(home, _Api())
    engine.start()
    first = engine._thread
    assert not engine.supervise()
    assert engine._thread is first
    engine.stop()


def test_watchdog_does_not_restart_after_a_stop(home) -> None:
    engine = _engine(home, _Api())
    engine.start()
    engine.stop()
    assert not engine.supervise()
    assert engine._thread is None


def test_a_stop_request_ends_the_cycle_without_a_restart(home) -> None:
    fired: list[bool] = []
    client = httpx.Client(transport=httpx.MockTransport(_Api()), timeout=5.0)
    engine = Engine(home, client=client, on_stop_request=lambda: fired.append(True))
    engine._tick = 0.01
    engine.start()
    runtime.request_stop(home)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not fired:
        time.sleep(0.01)
    assert fired == [True]
    assert not engine.supervise()
    engine.stop()


def test_a_failing_refresh_does_not_stop_uploads(home, caplog) -> None:
    api = _Api()
    clock = _Clock()
    engine = _engine(home, api, clock)
    engine.usage.record("claude", 100, 40)

    def broken_refresh() -> str:
        raise RuntimeError("boom")

    engine.refresh = broken_refresh  # type: ignore[method-assign]
    clock.now += engine._debounce + 1
    with caplog.at_level(logging.WARNING, logger="horizon.code_engine"):
        engine._cycle_once()
    assert api.uploads() == 1
    assert any("plan refresh failed" in record.getMessage() for record in caplog.records)


def test_cycle_uploads_soon_after_the_first_change_on_a_real_thread(home) -> None:
    api = _Api()
    engine = _engine(home, api)
    engine._tick = 0.01
    engine._debounce = 0.05
    engine.usage.record("claude", 100, 40)
    engine.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and api.uploads() == 0:
        time.sleep(0.01)
    engine.stop()
    assert api.uploads() >= 1
    body = api.usage_bodies[0]["buckets"][0]
    assert set(body) == {"hour", "tool", "requests", "tokens_before", "tokens_after"}


def test_engine_log_receives_the_engine_records(home) -> None:
    logger = logging.getLogger("horizon.code_engine")
    runtime.attach_engine_log(home)
    try:
        logger.info("cycle-probe-line")
        for handler in logger.handlers:
            handler.flush()
        text = (home / runtime.LOG_FILE).read_text(encoding="utf-8")
        assert "cycle-probe-line" in text
    finally:
        for handler in list(logger.handlers):
            if handler.get_name() == "engine-log":
                logger.removeHandler(handler)
                handler.close()


def test_attaching_the_engine_log_twice_adds_one_handler(home) -> None:
    logger = logging.getLogger("horizon.code_engine")
    before = len(logger.handlers)
    try:
        runtime.attach_engine_log(home)
        runtime.attach_engine_log(home)
        assert len(logger.handlers) == before + 1
    finally:
        for handler in list(logger.handlers):
            if handler.get_name() == "engine-log":
                logger.removeHandler(handler)
                handler.close()
