"""Hourly usage buckets for the engine (spec section 5: usage)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from horizon.code_engine.usage import (
    MAX_BUCKETS_PER_UPLOAD,
    USAGE_FILE,
    UsageMeter,
    hour_of,
)

T0 = datetime(2026, 10, 22, 14, 5, 0, tzinfo=timezone.utc).timestamp()


class _Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _Server:
    """Stands in for POST /code/usage: replaces each (hour, tool) bucket's values."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self.calls: list[list[dict]] = []
        self.reject = False

    def __call__(self, buckets: list[dict]) -> bool:
        self.calls.append(buckets)
        if self.reject:
            return False
        for row in buckets:
            self.rows[(row["hour"], row["tool"])] = row
        return True


def test_hour_of_is_the_utc_hour_in_api_form() -> None:
    assert hour_of(T0) == "2026-10-22T14:00:00Z"


def test_records_accumulate_per_hour_and_tool(tmp_path) -> None:
    meter = UsageMeter(tmp_path, clock=_Clock())
    meter.record("claude", 1000, 600)
    meter.record("claude", 500, 500)
    meter.record("codex", 200, 50)
    server = _Server()
    assert meter.flush(server)
    assert server.rows[("2026-10-22T14:00:00Z", "claude")] == {
        "hour": "2026-10-22T14:00:00Z",
        "tool": "claude",
        "requests": 2,
        "tokens_before": 1500,
        "tokens_after": 1100,
    }
    assert server.rows[("2026-10-22T14:00:00Z", "codex")]["requests"] == 1


def test_negative_token_counts_are_floored_at_zero(tmp_path) -> None:
    meter = UsageMeter(tmp_path, clock=_Clock())
    meter.record("claude", -5, 10)
    server = _Server()
    meter.flush(server)
    row = server.rows[("2026-10-22T14:00:00Z", "claude")]
    assert (row["tokens_before"], row["tokens_after"]) == (0, 10)


def test_upload_body_has_only_counts(tmp_path) -> None:
    meter = UsageMeter(tmp_path, clock=_Clock())
    meter.record("claude", 10, 5)
    server = _Server()
    meter.flush(server)
    for row in server.calls[0]:
        assert set(row) == {"hour", "tool", "requests", "tokens_before", "tokens_after"}


def test_flush_sends_nothing_when_nothing_changed(tmp_path) -> None:
    meter = UsageMeter(tmp_path, clock=_Clock())
    meter.record("claude", 10, 5)
    server = _Server()
    assert meter.flush(server)
    assert meter.pending_count() == 0
    assert meter.flush(server)
    assert len(server.calls) == 1


def test_later_records_resend_the_full_cumulative_total(tmp_path) -> None:
    meter = UsageMeter(tmp_path, clock=_Clock())
    server = _Server()
    meter.record("claude", 100, 40)
    meter.flush(server)
    meter.record("claude", 100, 40)
    meter.flush(server)
    # Replace semantics: the second upload carries the running total, not the delta.
    assert server.calls[1][0]["requests"] == 2
    assert server.rows[("2026-10-22T14:00:00Z", "claude")]["tokens_after"] == 80


def test_reuploading_the_same_buckets_is_idempotent(tmp_path) -> None:
    meter = UsageMeter(tmp_path, clock=_Clock())
    meter.record("codex", 300, 120)
    server = _Server()
    meter.flush(server)
    before = dict(server.rows)
    for row in server.calls[0]:
        server.rows[(row["hour"], row["tool"])] = row
    assert server.rows == before


def test_rejected_upload_stays_pending_and_is_persisted(tmp_path) -> None:
    meter = UsageMeter(tmp_path, clock=_Clock())
    meter.record("claude", 10, 5)
    server = _Server()
    server.reject = True
    assert not meter.flush(server)
    assert meter.pending_count() == 1
    saved = json.loads((tmp_path / USAGE_FILE).read_text(encoding="utf-8"))
    assert saved["buckets"][0]["requests"] == 1


def test_restart_continues_the_hour_instead_of_resetting_it(tmp_path) -> None:
    clock = _Clock()
    first = UsageMeter(tmp_path, clock=clock)
    first.record("claude", 100, 40)
    server = _Server()
    first.flush(server)

    restarted = UsageMeter(tmp_path, clock=clock)
    restarted.record("claude", 100, 40)
    restarted.flush(server)
    # The server must end with the full hour, not the count since the restart.
    assert server.rows[("2026-10-22T14:00:00Z", "claude")]["requests"] == 2
    assert server.rows[("2026-10-22T14:00:00Z", "claude")]["tokens_before"] == 200


def test_pending_buckets_survive_a_restart_until_uploaded(tmp_path) -> None:
    clock = _Clock()
    meter = UsageMeter(tmp_path, clock=clock)
    meter.record("codex", 70, 30)
    failing = _Server()
    failing.reject = True
    meter.flush(failing)

    restarted = UsageMeter(tmp_path, clock=clock)
    assert restarted.pending_count() == 1
    server = _Server()
    assert restarted.flush(server)
    assert server.rows[("2026-10-22T14:00:00Z", "codex")]["tokens_before"] == 70


def test_uploads_are_split_into_batches(tmp_path) -> None:
    clock = _Clock()
    meter = UsageMeter(tmp_path, clock=clock)
    for hour_offset in range(MAX_BUCKETS_PER_UPLOAD + 10):
        clock.now = T0 - hour_offset * 3600
        meter.record("claude", 1, 1)
    server = _Server()
    assert meter.flush(server)
    assert len(server.calls) == 2
    assert all(len(batch) <= MAX_BUCKETS_PER_UPLOAD for batch in server.calls)
    assert sum(len(batch) for batch in server.calls) == MAX_BUCKETS_PER_UPLOAD + 10


def test_hours_older_than_the_api_window_are_dropped(tmp_path) -> None:
    clock = _Clock()
    meter = UsageMeter(tmp_path, clock=clock)
    clock.now = T0 - 40 * 86400
    meter.record("claude", 1, 1)
    clock.now = T0
    server = _Server()
    assert meter.flush(server)
    assert server.calls == []
    assert meter.pending_count() == 0


def test_finished_confirmed_hours_are_not_kept_in_memory(tmp_path) -> None:
    clock = _Clock()
    meter = UsageMeter(tmp_path, clock=clock)
    meter.record("claude", 1, 1)
    server = _Server()
    meter.flush(server)
    clock.now = T0 + 3 * 3600
    meter.flush(server)
    saved = json.loads((tmp_path / USAGE_FILE).read_text(encoding="utf-8"))
    assert saved["buckets"] == []


@pytest.mark.parametrize("bad", [b"{", json.dumps({"version": 2, "buckets": []})])
def test_unreadable_pending_file_is_ignored(tmp_path, bad) -> None:
    (tmp_path / USAGE_FILE).write_bytes(bad if isinstance(bad, bytes) else bad.encode())
    assert UsageMeter(tmp_path, clock=_Clock()).pending_count() == 0


def test_recorded_requests_survive_a_restart_before_any_flush(tmp_path) -> None:
    """An engine killed or restarted between flushes must still upload what it served."""
    clock = _Clock()
    first = UsageMeter(tmp_path, clock=clock)
    first.record("claude", 1000, 600)
    # No flush: the process ends here (a kill, a crash, a reboot).
    restarted = UsageMeter(tmp_path, clock=clock)
    assert restarted.pending_count() == 1
    server = _Server()
    assert restarted.flush(server)
    row = server.rows[("2026-10-22T14:00:00Z", "claude")]
    assert (row["requests"], row["tokens_before"], row["tokens_after"]) == (1, 1000, 600)


def test_every_record_is_on_disk_before_record_returns(tmp_path) -> None:
    meter = UsageMeter(tmp_path, clock=_Clock())
    meter.record("codex", 300, 120)
    meter.record("codex", 100, 50)
    saved = json.loads((tmp_path / USAGE_FILE).read_text(encoding="utf-8"))
    assert saved["buckets"] == [
        {
            "hour": "2026-10-22T14:00:00Z",
            "tool": "codex",
            "requests": 2,
            "tokens_before": 400,
            "tokens_after": 170,
        }
    ]


# ── Upload schedule: debounced, idle-silent, safety net (fake clock) ─────────

DEBOUNCE = 30.0
SAFETY = 900.0


def test_upload_waits_for_the_debounce_after_the_first_change(tmp_path) -> None:
    clock = _Clock()
    meter = UsageMeter(tmp_path, clock=clock)
    clock.now += 100  # boot, then the first change
    meter.record("claude", 10, 5)
    assert not meter.flush_due(clock.now + DEBOUNCE - 1, debounce=DEBOUNCE, safety=SAFETY)
    assert meter.flush_due(clock.now + DEBOUNCE, debounce=DEBOUNCE, safety=SAFETY)


def test_idle_engine_never_uploads(tmp_path) -> None:
    clock = _Clock()
    meter = UsageMeter(tmp_path, clock=clock)
    meter.record("claude", 10, 5)
    server = _Server()
    clock.now += DEBOUNCE
    assert meter.flush_due(clock.now, debounce=DEBOUNCE, safety=SAFETY)
    assert meter.flush(server)
    # Nothing has changed since the upload: no upload is due, however long the engine idles.
    for step in range(1, 200):
        assert not meter.flush_due(clock.now + step * 60, debounce=DEBOUNCE, safety=SAFETY)
    clock.now += 10 * 3600
    assert not meter.flush_due(clock.now, debounce=DEBOUNCE, safety=SAFETY)
    assert len(server.calls) == 1


def test_busy_engine_uploads_at_most_once_per_debounce(tmp_path) -> None:
    clock = _Clock()
    meter = UsageMeter(tmp_path, clock=clock)
    server = _Server()
    uploads = []
    # A request every 5 seconds for ten minutes.
    for second in range(0, 600, 5):
        clock.now = T0 + second
        meter.record("claude", 10, 5)
        if meter.flush_due(clock.now, debounce=DEBOUNCE, safety=SAFETY):
            assert meter.flush(server)
            uploads.append(second)
    clock.now = T0 + 600 + DEBOUNCE
    if meter.flush_due(clock.now, debounce=DEBOUNCE, safety=SAFETY):
        assert meter.flush(server)
        uploads.append(600 + DEBOUNCE)
    gaps = [b - a for a, b in zip(uploads, uploads[1:])]
    assert uploads and min(gaps) >= DEBOUNCE
    assert uploads[0] == 30  # the first change at t=0 is uploaded 30 seconds later
    assert len(uploads) <= 600 // DEBOUNCE + 1
    # Every upload carries the running total for the hour, not a delta.
    assert server.rows[("2026-10-22T14:00:00Z", "claude")]["requests"] == 120


def test_refused_upload_is_retried_one_interval_later(tmp_path) -> None:
    clock = _Clock()
    meter = UsageMeter(tmp_path, clock=clock)
    server = _Server()
    server.reject = True
    meter.record("claude", 10, 5)
    clock.now += DEBOUNCE
    assert meter.flush_due(clock.now, debounce=DEBOUNCE, safety=SAFETY)
    assert not meter.flush(server)
    assert not meter.flush_due(clock.now + DEBOUNCE - 1, debounce=DEBOUNCE, safety=SAFETY)
    assert meter.flush_due(clock.now + DEBOUNCE, debounce=DEBOUNCE, safety=SAFETY)
    server.reject = False
    assert meter.flush(server)
    assert meter.pending_count() == 0


def test_safety_flush_runs_fifteen_minutes_after_the_last_attempt(tmp_path) -> None:
    clock = _Clock()
    first = UsageMeter(tmp_path, clock=clock)
    first.record("codex", 50, 20)
    # A restart with the debounce disabled: only the safety interval can trigger the upload.
    clock.now += 60
    restarted = UsageMeter(tmp_path, clock=clock)
    assert restarted.pending_count() == 1
    huge = 10**9
    assert not restarted.flush_due(clock.now + SAFETY - 1, debounce=huge, safety=SAFETY)
    assert restarted.flush_due(clock.now + SAFETY, debounce=huge, safety=SAFETY)
