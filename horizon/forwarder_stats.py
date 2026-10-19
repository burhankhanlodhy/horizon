"""Local stats for the Claude Code forwarder: what it records, and where it goes.

The forwarder relays ``/v1/messages`` and, for each response, does three things
that never touch prompt content or auth values:

* strips the proxy's verified-savings SSE comment before the client sees it
  (:class:`SseCommentFilter`), reading the usage counters it needs along the way;
* folds the response into per-session and per-period counters
  (:class:`ClaudeCodeStats`);
* writes ``status.json`` and ``status.txt`` atomically, plus a heartbeat so the
  tray app and the status line can tell the forwarder is alive.

Files live under ``<state dir>/claude-code/``. The counters are numbers only:
no request or response body is stored, and nothing identifying the caller is
written beyond the Claude Code session id the client already sends.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from horizon.verified_savings import SSE_COMMENT_PREFIX, parse_sse_comment_value

logger = logging.getLogger(__name__)

STATUS_VERSION = 1
CACHE_TTL_SECONDS = 300
MAX_SESSIONS = 200
#: Models tallied per session (the main model plus a few helper models).
_MODEL_TALLY_MAX = 16
HEARTBEAT_SECONDS = 10.0
STALE_AFTER_SECONDS = 30.0
HISTORY_DAYS = 400
#: Longest partial SSE line the filter will hold back while it decides whether the
#: line is the verified-savings comment. The real comment is about 50 bytes.
MAX_HELD_LINE = 512
#: Upper bound on a buffered non-streamed body scanned for cache-write usage.
MAX_BUFFERED_BODY = 4 * 1024 * 1024

_PREFIX = SSE_COMMENT_PREFIX.encode("ascii")
_CACHE_MARK = b"cache_creation_input_tokens"

COUNTER_FIELDS = (
    "requests",
    "tokens_before",
    "tokens_after",
    "trimmed_list_usd",
    "verified_saved_usd",
    "verified_requests",
)


def default_state_dir() -> Path:
    """``%LOCALAPPDATA%\\ContextShrink`` on Windows; a per-user dir elsewhere."""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "ContextShrink"
    return Path.home() / ".contextshrink"


def claude_code_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "claude-code"


# --------------------------------------------------------------------------- SSE


class SseCommentFilter:
    """Strip the verified-savings comment from a relayed SSE stream, chunk by chunk.

    ``feed`` takes raw upstream bytes and returns the bytes to relay. A line is
    only held back while it could still be the comment, so ordinary events pass
    through without delay. ``finish`` flushes whatever is still held.
    """

    def __init__(self) -> None:
        self._held = b""
        self._drop_blank = False
        self.verified: float | None = None
        self.cache_written = False

    def feed(self, chunk: bytes) -> bytes:
        data = self._held + chunk
        self._held = b""
        out: list[bytes] = []
        start = 0
        while True:
            newline = data.find(b"\n", start)
            if newline < 0:
                rest = data[start:]
                if rest:
                    if self._could_be_marker(rest):
                        self._held = rest
                    else:
                        out.append(rest)
                break
            self._line(data[start : newline + 1], out)
            start = newline + 1
        return b"".join(out)

    def finish(self) -> bytes:
        held, self._held = self._held, b""
        self._drop_blank = False
        return held

    def _could_be_marker(self, partial: bytes) -> bool:
        if len(partial) > MAX_HELD_LINE:
            return False
        head = partial[: len(_PREFIX)]
        return _PREFIX.startswith(head) or partial.startswith(_PREFIX)

    def _line(self, line: bytes, out: list[bytes]) -> None:
        if self._drop_blank and line.rstrip(b"\r\n") == b"":
            self._drop_blank = False
            return
        self._drop_blank = False
        value = parse_sse_comment_value(line)
        if value is not None:
            self.verified = value
            self._drop_blank = True
            return
        if line.startswith(b"data:") and _CACHE_MARK in line:
            self._observe_usage(line[5:])
        out.append(line)

    def _observe_usage(self, payload: bytes) -> None:
        try:
            event = json.loads(payload)
        except ValueError:
            return
        if isinstance(event, dict):
            self._observe_usage_dict(event.get("usage"))
            message = event.get("message")
            if isinstance(message, dict):
                self._observe_usage_dict(message.get("usage"))

    def _observe_usage_dict(self, usage: Any) -> None:
        if isinstance(usage, dict) and _as_int(usage.get("cache_creation_input_tokens")) > 0:
            self.cache_written = True


def body_has_cache_write(body: bytes) -> bool:
    """For a non-streamed JSON reply: did it write to the prompt cache?"""
    if _CACHE_MARK not in body:
        return False
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    usage = payload.get("usage")
    return isinstance(usage, dict) and _as_int(usage.get("cache_creation_input_tokens")) > 0


# ------------------------------------------------------------------------ pricing

_price_lock = threading.Lock()
_price_cache: dict[str, float | None] = {}


def list_input_price_per_token(model: str) -> float | None:
    """List input price in USD per token from ``horizon.pricing``, or ``None``.

    Cached per model. Any failure reads as "unpriced", which the caller records
    as zero dollars rather than guessing a rate.
    """
    if not model:
        return None
    with _price_lock:
        if model in _price_cache:
            return _price_cache[model]
    price: float | None
    try:
        from horizon.pricing.litellm_pricing import pricing_per_1m

        rates = pricing_per_1m(model)
        price = None if rates is None else float(rates[0]) / 1_000_000
    except Exception:  # noqa: BLE001 - pricing is best-effort for a local display
        price = None
    with _price_lock:
        _price_cache[model] = price
    return price


# -------------------------------------------------------------------------- stats


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float | None:
    """A finite number, signed: the verified figure can be negative (keep-alive cost)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _empty_counters() -> dict[str, Any]:
    return {
        "requests": 0,
        "tokens_before": 0,
        "tokens_after": 0,
        "trimmed_list_usd": 0.0,
        "verified_saved_usd": 0.0,
        "verified_requests": 0,
    }


def _add_counters(target: dict[str, Any], delta: dict[str, Any]) -> None:
    for name in COUNTER_FIELDS:
        target[name] = target.get(name, 0) + delta[name]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _local_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _round_counters(counters: dict[str, Any]) -> dict[str, Any]:
    out = dict(counters)
    out["trimmed_list_usd"] = round(float(out.get("trimmed_list_usd", 0.0)), 6)
    out["verified_saved_usd"] = round(float(out.get("verified_saved_usd", 0.0)), 6)
    return out


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with a tempfile and ``os.replace``.

    Readers never see a half-written file. Windows refuses a replace while a
    reader holds the target open, so that case retries briefly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        last: OSError | None = None
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:
                last = exc
                time.sleep(0.05 * (attempt + 1))
        if last is not None:
            raise last
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def render_status_line(status: dict[str, Any], now: float) -> str:
    """The one-line status for the latest session, as the status line shows it."""
    forwarder = status.get("forwarder") or {}
    updated = _parse_iso(status.get("updated_at"))
    if updated is None or now - updated > STALE_AFTER_SECONDS:
        return "ContextShrink off"
    if status.get("paused"):
        return "ContextShrink paused"
    if not forwarder.get("healthy", False):
        return "ContextShrink off"
    sessions = status.get("sessions") or {}
    if not sessions:
        return "ContextShrink ✓ waiting for Claude Code"
    latest = max(sessions.values(), key=lambda s: str(s.get("last_at") or ""))
    before = _as_int(latest.get("tokens_before"))
    after = _as_int(latest.get("tokens_after"))
    pct = round(100 * (1 - after / before)) if before > 0 else 0
    # Clamped like the Rust status helper: a request that grew reads 0%, never negative.
    pct = max(0, min(100, pct))
    if latest.get("unpriced"):
        money = "list price unknown"
    else:
        money = f"${float(latest.get('trimmed_list_usd', 0.0)):.2f} of tokens at list price"
    if _as_int(latest.get("verified_requests")) > 0:
        verified = float(latest.get("verified_saved_usd", 0.0))
        if verified < 0:
            saved = f"cost ${-verified:.2f} verified"
        else:
            saved = f"saved ${verified:.2f} verified"
    else:
        saved = "saved pending verified"
    requests = _as_int(latest.get("requests"))
    return f"ContextShrink ✓ trimmed {pct}% · {money} · {saved} · {requests} req"


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


class ClaudeCodeStats:
    """Counters for the Claude Code forwarder, persisted under ``claude-code/``."""

    def __init__(self, directory: Path, *, pause_file: Path | None = None) -> None:
        self.directory = Path(directory)
        self.pause_file = pause_file or self.directory / "paused"
        self.status_path = self.directory / "status.json"
        self.text_path = self.directory / "status.txt"
        self.history_path = self.directory / "usage-history.json"
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cs-stats")
        self._sessions: dict[str, dict[str, Any]] = {}
        self._daily: dict[str, dict[str, Any]] = {}
        self._all_time = _empty_counters()
        self._load()

    def background(self, fn: Any, *args: Any) -> None:
        """Run ``fn`` on the single writer thread, in submission order."""
        self._writer.submit(fn, *args)

    # ---- persistence -------------------------------------------------------

    def _load(self) -> None:
        history = _read_history(self.history_path)
        if isinstance(history, dict):
            daily = history.get("daily")
            if isinstance(daily, dict):
                self._daily = {str(k): v for k, v in daily.items() if _counters_ok(v)}
            all_time = history.get("all_time")
            if _counters_ok(all_time):
                self._all_time = {**_empty_counters(), **all_time}
        status = _read_json(self.status_path)
        if isinstance(status, dict) and isinstance(status.get("sessions"), dict):
            # A malformed entry is dropped here; record() would reset it anyway.
            self._sessions = {str(k): v for k, v in status["sessions"].items() if _session_ok(v)}

    def is_paused(self) -> bool:
        return self.pause_file.exists()

    # ---- recording ---------------------------------------------------------

    def record(
        self,
        *,
        session_id: str,
        model: str,
        tokens_before: int,
        tokens_after: int,
        tokens_saved: int,
        list_usd: float,
        unpriced: bool,
        priced: bool = False,
        verified_usd: float | None,
        cache_written: bool,
        keepalive: bool,
        now: float,
    ) -> None:
        delta = _empty_counters()
        delta["requests"] = 1
        delta["tokens_before"] = _as_int(tokens_before)
        delta["tokens_after"] = _as_int(tokens_after)
        delta["trimmed_list_usd"] = max(float(list_usd), 0.0)
        if verified_usd is not None:
            # Signed: a request whose keep-alive pings outweighed its saving is a cost.
            delta["verified_saved_usd"] = float(verified_usd)
            delta["verified_requests"] = 1
        stamp = _iso(now)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and not _session_ok(session):
                logger.warning("claude-code stats: resetting malformed session entry")
                session = None
            if session is None:
                session = {
                    **_empty_counters(),
                    "first_at": stamp,
                    "last_at": stamp,
                    "model": "",
                    "unpriced": False,
                    "cache": {"last_write_at": "", "ttl_s": CACHE_TTL_SECONDS, "keepalive": False},
                }
                self._sessions[session_id] = session
            _add_counters(session, delta)
            session["last_at"] = stamp
            if model:
                # The model that carried the most of the session's tokens: Claude Code
                # mixes in small helper calls (Haiku) beside the main model.
                tally = session.setdefault("model_tokens", {})
                tally[model] = tally.get(model, 0) + _as_int(tokens_before)
                if len(tally) > _MODEL_TALLY_MAX:
                    del tally[min(tally, key=tally.get)]
                session["model"] = max(tally, key=tally.get)
            # A session is unpriced only when none of its requests could be priced;
            # the priced ones keep their dollars either way.
            if priced:
                session["priced_requests"] = _as_int(session.get("priced_requests")) + 1
            elif unpriced:
                session["unpriced_requests"] = _as_int(session.get("unpriced_requests")) + 1
            session["unpriced"] = (
                _as_int(session.get("unpriced_requests")) > 0
                and _as_int(session.get("priced_requests")) == 0
            )
            cache = session["cache"]
            if cache_written:
                cache["last_write_at"] = stamp
            if keepalive:
                cache["keepalive"] = True
            day = _local_day(now)
            _add_counters(self._daily.setdefault(day, _empty_counters()), delta)
            _add_counters(self._all_time, delta)
            self._prune_locked(now)

    def _prune_locked(self, now: float) -> None:
        if len(self._sessions) > MAX_SESSIONS:
            ordered = sorted(self._sessions, key=lambda k: str(self._sessions[k].get("last_at")))
            for key in ordered[: len(self._sessions) - MAX_SESSIONS]:
                del self._sessions[key]
        cutoff = _local_day(now - HISTORY_DAYS * 86400)
        for day in [d for d in self._daily if d < cutoff]:
            del self._daily[day]

    # ---- snapshot and files ---------------------------------------------------

    def snapshot(
        self,
        *,
        now: float,
        forwarder: dict[str, Any],
    ) -> dict[str, Any]:
        """The full ``status.json`` document (see the W1 contract)."""
        with self._lock:
            sessions = {
                key: {**value, "cache": dict(value.get("cache", {}))}
                for key, value in self._sessions.items()
            }
            today = _local_day(now)
            last_7 = [_local_day(now - offset * 86400) for offset in range(7)]
            today_counters = _empty_counters()
            _add_counters(today_counters, self._daily.get(today, _empty_counters()))
            seven = _empty_counters()
            for day in last_7:
                _add_counters(seven, self._daily.get(day, _empty_counters()))
            all_time = dict(self._all_time)
        return {
            "v": STATUS_VERSION,
            "updated_at": _iso(now),
            "forwarder": {"healthy": True, **forwarder},
            "paused": self.is_paused(),
            "sessions": {key: _round_session(value) for key, value in sessions.items()},
            "today": _round_counters(today_counters),
            "last_7_days": _round_counters(seven),
            "all_time": _round_counters(all_time),
        }

    def flush(self, *, now: float | None = None, forwarder: dict[str, Any]) -> None:
        """Write status.json, status.txt and the usage history, atomically."""
        ts = time.time() if now is None else now
        status = self.snapshot(now=ts, forwarder=forwarder)
        with self._lock:
            history = {"v": STATUS_VERSION, "daily": self._daily, "all_time": self._all_time}
            history_text = json.dumps(history, indent=2) + "\n"
        with self._write_lock:
            atomic_write_text(self.status_path, json.dumps(status, indent=2) + "\n")
            atomic_write_text(self.text_path, render_status_line(status, ts) + "\n")
            atomic_write_text(self.history_path, history_text)

    def close(self) -> None:
        self._writer.shutdown(wait=True)


def _round_session(session: dict[str, Any]) -> dict[str, Any]:
    out = _round_counters(session)
    for key in (
        "first_at",
        "last_at",
        "model",
        "model_tokens",
        "unpriced",
        "priced_requests",
        "unpriced_requests",
        "cache",
    ):
        if key in session:
            out[key] = session[key]
    return out


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def _counters_ok(entry: Any) -> bool:
    """True when every counter field is present and numeric."""
    return isinstance(entry, dict) and all(_is_number(entry.get(name)) for name in COUNTER_FIELDS)


def _session_ok(entry: Any) -> bool:
    return _counters_ok(entry) and isinstance(entry.get("cache", {}), dict)


def _read_history(path: Path) -> Any:
    """Read the usage history. A file that does not parse is moved aside, not overwritten.

    Overwriting it would silently discard the all-time totals, so the corrupt
    copy is kept as ``usage-history.json.corrupt-<unix time>`` and counting
    starts fresh.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("claude-code stats: could not read %s: %s", path, exc)
        return None
    try:
        return json.loads(raw)
    except ValueError:
        aside = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            os.replace(path, aside)
        except OSError as exc:
            logger.warning(
                "claude-code stats: %s is unreadable and could not be moved: %s", path, exc
            )
        else:
            logger.warning(
                "claude-code stats: %s is unreadable; moved it to %s and starting fresh",
                path,
                aside,
            )
        return None


def heartbeat(stop: threading.Event, write: Any, interval: float = HEARTBEAT_SECONDS) -> None:
    """Call ``write()`` now and every ``interval`` seconds until ``stop`` is set."""
    while True:
        try:
            write()
        except Exception:  # noqa: BLE001 - a failed write must not stop the relay
            pass
        if stop.wait(interval):
            return


class RequestRecorder:
    """Accumulates one ``/v1/messages`` response and records it when it completes.

    ``feed`` runs on the relay path and only parses; ``complete_and_flush`` runs
    on the stats writer thread after the response has ended, so pricing and disk
    never sit between the upstream bytes and the client.
    """

    def __init__(
        self,
        stats: ClaudeCodeStats,
        *,
        session_id: str,
        model: str,
        keepalive: bool,
        forwarder_info: dict[str, Any],
    ) -> None:
        self.stats = stats
        self.session_id = session_id
        self.model = model
        self.keepalive = keepalive
        self.forwarder_info = forwarder_info
        self.filter = SseCommentFilter()
        self._buffer: bytearray | None = None
        self._is_stream = False

    def start(self, content_type: str) -> None:
        self._is_stream = "text/event-stream" in content_type.lower()
        if not self._is_stream:
            self._buffer = bytearray()

    def feed(self, chunk: bytes) -> bytes:
        if self._is_stream:
            return self.filter.feed(chunk)
        if self._buffer is not None and len(self._buffer) < MAX_BUFFERED_BODY:
            self._buffer.extend(chunk)
        return chunk

    def finish_bytes(self) -> bytes:
        return self.filter.finish() if self._is_stream else b""

    def schedule_complete(self, headers: dict[str, str], status_code: int) -> None:
        """Hand the finished response to the writer thread. Returns at once."""
        self.stats.background(self.complete_and_flush, headers, status_code)

    def complete_and_flush(self, headers: dict[str, str], status_code: int) -> None:
        """Record the response, then write the files. Never raises."""
        try:
            self._complete(headers, status_code=status_code)
        except Exception:  # noqa: BLE001 - stats must never break a relay
            pass
        try:
            self.stats.flush(forwarder=self.forwarder_info)
        except Exception:  # noqa: BLE001
            pass

    def _complete(self, headers: Any, *, status_code: int) -> None:
        before = _as_int(headers.get("x-horizon-tokens-before"))
        after = _as_int(headers.get("x-horizon-tokens-after"))
        saved_header = headers.get("x-horizon-tokens-saved")
        saved = _as_int(saved_header) if saved_header is not None else max(before - after, 0)
        model = str(headers.get("x-horizon-model") or self.model or "")

        verified = self.filter.verified
        if verified is None:
            verified = _as_float(headers.get("x-horizon-verified-savings-usd"))
        cache_written = self.filter.cache_written
        if self._buffer:
            cache_written = cache_written or body_has_cache_write(bytes(self._buffer))

        price = list_input_price_per_token(model) if saved > 0 else None
        unpriced = saved > 0 and price is None
        priced = saved > 0 and price is not None
        list_usd = saved * price if price is not None else 0.0
        self.stats.record(
            session_id=self.session_id,
            model=model,
            tokens_before=before,
            tokens_after=after,
            tokens_saved=saved,
            list_usd=list_usd,
            unpriced=unpriced,
            priced=priced,
            verified_usd=verified if status_code < 400 else None,
            cache_written=cache_written,
            keepalive=self.keepalive,
            now=time.time(),
        )
