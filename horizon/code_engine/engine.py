"""The engine's state: the license gate, the usage meter, and the background cycle.

The gate is open while the cached plan note verifies for this device and the
current time. The proxy consults it on every request through
``horizon.proxy.engine_hooks``; nothing here sits on the request path beyond
an expiry comparison.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from horizon._version import __version__ as ENGINE_VERSION
from horizon.bundled_client import is_bundled_client
from horizon.code_engine import runtime
from horizon.code_engine.device import Device, MachineIdError, load_device, local_device_hash
from horizon.code_engine.store import home_dir
from horizon.code_engine.usage import UsageMeter

logger = logging.getLogger("horizon.code_engine")

try:
    from horizon.code_engine import license as _license
except ImportError:  # cryptography is missing: serve pass-through, never fail to start
    _license = None
    logger.error("ContextShrink engine: cryptography is not installed; compression stays off")

#: How often the cached plan note is refreshed. The note's own ``exp`` is at most 72 hours away.
PLAN_REFRESH_SECONDS = 3600
#: Buckets are uploaded this long after the first unsent change. A busy engine uploads at
#: most once per interval; an idle engine uploads nothing.
USAGE_DEBOUNCE_SECONDS = 30
#: Safety net: pending buckets are attempted at least this often, whatever else happened.
USAGE_FLUSH_SECONDS = 15 * 60
_CYCLE_TICK_SECONDS = 1.0
#: How often the watchdog checks that the cycle thread is still running.
WATCHDOG_SECONDS = 5.0


class Engine:
    """One engine process's license gate, usage meter and background cycle."""

    def __init__(
        self,
        home: Path | None = None,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
        on_stop_request: Callable[[], None] | None = None,
    ) -> None:
        self.home = home or home_dir()
        self._clock = clock
        self._client = client or httpx.Client(timeout=10.0, trust_env=False)
        self._on_stop_request = on_stop_request or _interrupt_main
        self._entitlement: Any = None  # a license.Entitlement once a note verifies
        self._state_lock = threading.Lock()
        self.device: Device | None = load_device(self.home)
        self.local_hash: str | None = None
        try:
            self.local_hash = local_device_hash()
        except MachineIdError:
            logger.warning("this computer has no machine id; compression stays off")
        self.usage = UsageMeter(self.home, clock=clock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_refresh = 0.0
        self._debounce = _test_seconds("CONTEXTSHRINK_TEST_USAGE_DEBOUNCE_SECONDS", USAGE_DEBOUNCE_SECONDS)
        self._watchdog: threading.Thread | None = None
        self._refresh_every = _test_seconds("CONTEXTSHRINK_TEST_PLAN_REFRESH_SECONDS", PLAN_REFRESH_SECONDS)
        self._flush_every = _test_seconds("CONTEXTSHRINK_TEST_USAGE_FLUSH_SECONDS", USAGE_FLUSH_SECONDS)
        self._tick = _test_seconds("CONTEXTSHRINK_TEST_CYCLE_TICK_SECONDS", _CYCLE_TICK_SECONDS)

    # ── License gate ───────────────────────────────────────────────────

    def load_cached_note(self) -> None:
        """Adopt the cached note if it still verifies. Runs before the proxy serves."""
        if self.device is None or self.local_hash is None or _license is None:
            return
        entitlement = _license.read_license(
            self.home, device_hash=self.local_hash, now=self._clock()
        )
        with self._state_lock:
            self._entitlement = entitlement

    def plan(self) -> str | None:
        """``trial`` or ``active`` while the gate is open, else ``None``."""
        with self._state_lock:
            entitlement = self._entitlement
        if entitlement is None or not entitlement.valid_at(self._clock()):
            return None
        return entitlement.plan

    def allows_compression(self) -> bool:
        return self.plan() is not None

    def health(self) -> dict[str, Any]:
        """The body of ``GET /__cs/health``."""
        plan = self.plan()
        return {"ok": plan is not None, "engine": ENGINE_VERSION, "plan": plan}

    def refresh(self) -> str:
        """Fetch a fresh plan note and cache it. Returns a short status for the log."""
        device = self.device
        if device is None or self.local_hash is None:
            return "no_device"
        if _license is None:
            return "no_cryptography"
        if device.device_hash != self.local_hash:
            return "device_mismatch"
        try:
            response = self._client.post(
                f"{device.api_url}/code/entitlement",
                json={"device_hash": device.device_hash},
                headers={"Authorization": f"Bearer {device.device_key}"},
            )
        except httpx.HTTPError as exc:
            # Offline: keep the cached note until its own expiry.
            logger.info("plan refresh unreachable (%s)", type(exc).__name__)
            return "unreachable"
        if response.status_code == 200:
            return self._adopt(response)
        if response.status_code in (401, 402, 403):
            # Not entitled, or the device was disconnected: the gate closes now.
            with self._state_lock:
                self._entitlement = None
            _license.clear_license(self.home)
            return "not_entitled" if response.status_code == 402 else "device_revoked"
        logger.info("plan refresh answered status %d", response.status_code)
        return f"http_{response.status_code}"

    def _adopt(self, response: httpx.Response) -> str:
        assert self.local_hash is not None
        now = self._clock()
        try:
            token = response.json()["token"]
            entitlement = _license.verify_note(token, device_hash=self.local_hash, now=now)
        except (ValueError, KeyError, TypeError, _license.NoteError):
            # A note we cannot verify never unlocks anything; keep the cached one.
            logger.warning("plan note refused by the local check")
            return "invalid_note"
        _license.write_license(self.home, token, fetched_at=now)
        with self._state_lock:
            self._entitlement = entitlement
        return "ok"

    # ── Usage ──────────────────────────────────────────────────────────

    def record_usage(self, tool: str, tokens_before: int, tokens_after: int) -> None:
        """Count a served request. Ignored while the gate is closed."""
        if self.allows_compression():
            self.usage.record(tool, tokens_before, tokens_after)

    def flush_usage(self) -> bool:
        device = self.device
        if device is None:
            return False

        def send(buckets: list[dict[str, Any]]) -> bool:
            # Counts only: bucket numbers and HTTP status, never the bodies.
            try:
                response = self._client.post(
                    f"{device.api_url}/code/usage",
                    json={"buckets": buckets},
                    headers={"Authorization": f"Bearer {device.device_key}"},
                    timeout=5.0,
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "usage upload failed (%s); %d buckets stay pending",
                    type(exc).__name__,
                    len(buckets),
                )
                return False
            logger.info("usage upload: %d buckets -> %d", len(buckets), response.status_code)
            if not 200 <= response.status_code < 300:
                logger.warning(
                    "usage upload refused (status %d); %d buckets stay pending",
                    response.status_code,
                    len(buckets),
                )
                return False
            return True

        return self.usage.flush(send)

    # ── Background cycle ───────────────────────────────────────────────

    def start(self) -> None:
        """Start the refresh/flush loop and its watchdog on daemon threads."""
        if self._thread is not None:
            return
        self._start_cycle()
        self._watchdog = threading.Thread(target=self._watch, name="code-engine-watchdog", daemon=True)
        self._watchdog.start()

    def _start_cycle(self) -> None:
        self._thread = threading.Thread(target=self._run, name="code-engine-cycle", daemon=True)
        self._thread.start()

    def supervise(self) -> bool:
        """Restart the cycle thread if it has ended while the engine is running. True when restarted."""
        if self._stop.is_set() or self._thread is None or self._thread.is_alive():
            return False
        logger.warning("engine cycle thread ended unexpectedly; restarting it")
        self._start_cycle()
        return True

    def _watch(self) -> None:
        while not self._stop.wait(WATCHDOG_SECONDS):
            try:
                self.supervise()
            except Exception:  # noqa: BLE001 - the watchdog itself must keep running
                logger.warning("engine watchdog check failed; retrying", exc_info=True)

    def stop(self) -> None:
        """Stop the loop and upload what is pending. Safe to call twice."""
        self._stop.set()
        for thread in (self._thread, self._watchdog):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5.0)
        self._thread = None
        self._watchdog = None
        if self.device is not None:
            try:
                self.flush_usage()
            except Exception:  # noqa: BLE001 - shutdown must finish even if the upload breaks
                logger.warning("usage upload at shutdown failed")

    def _run(self) -> None:
        # The first refresh happens as soon as the loop starts.
        while not self._stop.wait(self._tick):
            try:
                if runtime.consume_stop_request(self.home):
                    logger.info("stop requested")
                    self._stop.set()  # the watchdog must not restart a loop that was asked to end
                    self._on_stop_request()
                    return
            except Exception:  # noqa: BLE001 - a bad stop file must not end the loop
                logger.warning("engine stop check failed; retrying next tick", exc_info=True)
            self._cycle_once()

    def _cycle_once(self) -> None:
        now = self._clock()
        if now - self._last_refresh >= self._refresh_every:
            self._last_refresh = now
            try:
                logger.info("plan refresh: %s", self.refresh())
            except Exception:  # noqa: BLE001 - one failed refresh must not stop uploads
                logger.warning("plan refresh failed; retrying next interval", exc_info=True)
        if self.usage.flush_due(now, debounce=self._debounce, safety=self._flush_every):
            try:
                self.flush_usage()
            except Exception:  # noqa: BLE001 - one failed upload must not end the loop
                logger.warning("usage upload failed; retrying", exc_info=True)


def _test_seconds(name: str, default: float) -> float:
    """A test-only interval override. Ignored inside the shipped client."""
    raw = os.environ.get(name, "").strip()
    if not raw or is_bundled_client():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _interrupt_main() -> None:
    """Ask the proxy's main thread to shut down gracefully (uvicorn handles SIGINT)."""
    import _thread
    import signal

    _thread.interrupt_main(signal.SIGINT)
