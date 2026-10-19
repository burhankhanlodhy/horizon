"""The Claude Code path of the forwarder: health, tagging, comment stripping, stats.

All HTTP goes through ``httpx.MockTransport``; nothing touches the network.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from horizon.forwarder import HEALTH_PATH, build_app
from horizon.forwarder_stats import (
    ClaudeCodeStats,
    SseCommentFilter,
    list_input_price_per_token,
    render_status_line,
)
from horizon.verified_savings import sse_comment_line

REMOTE = "https://remote.example.test"
EVENT_A = b'event: message_start\ndata: {"type":"message_start"}\n\n'
EVENT_B = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
COMMENT = sse_comment_line(0.25)
STREAM = EVENT_A + COMMENT + EVENT_B


def _spec_events(stream: bytes) -> list[tuple[str, str]]:
    """A minimal SSE parser per the spec: comments are ignored, events end on a blank line."""
    events: list[tuple[str, str]] = []
    for block in stream.split(b"\n\n"):
        name, data = "message", []
        for raw in block.split(b"\n"):
            if not raw or raw.startswith(b":"):
                continue
            field, _, value = raw.partition(b":")
            value = value[1:] if value.startswith(b" ") else value
            if field == b"event":
                name = value.decode()
            elif field == b"data":
                data.append(value.decode())
        if data:
            events.append((name, "\n".join(data)))
    return events


def _app(handler, **kwargs):
    return build_app(REMOTE, lambda: "hz_test", transport=httpx.MockTransport(handler), **kwargs)


def _stream_handler(seen: dict, body: bytes = STREAM, extra: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.setdefault("calls", []).append(request)
        headers = {
            "content-type": "text/event-stream",
            "x-horizon-tokens-before": "1000",
            "x-horizon-tokens-after": "600",
            "x-horizon-tokens-saved": "400",
            "x-horizon-model": "claude-sonnet-4-5",
        }
        headers.update(extra or {})
        return httpx.Response(200, content=body, headers=headers)

    return handler


# ----------------------------------------------------------------- SSE comment filter


def test_filter_strips_comment_and_keeps_every_other_byte() -> None:
    f = SseCommentFilter()
    out = f.feed(STREAM) + f.finish()
    assert out == EVENT_A + EVENT_B
    assert f.verified == pytest.approx(0.25)


def test_filter_is_correct_for_every_chunk_boundary() -> None:
    """Feeding one byte at a time must give the same bytes as one chunk."""
    f = SseCommentFilter()
    out = b"".join(f.feed(bytes([b])) for b in STREAM) + f.finish()
    assert out == EVENT_A + EVENT_B
    assert f.verified == pytest.approx(0.25)


def test_filter_leaves_other_comments_and_unrelated_lines_alone() -> None:
    stream = b": keep-alive\n\n" + EVENT_A + b": x-horizon-verified-savings-usd=nope\n\n"
    f = SseCommentFilter()
    out = f.feed(stream) + f.finish()
    assert out == stream
    assert f.verified is None


def test_filter_strips_a_crlf_comment_too() -> None:
    f = SseCommentFilter()
    out = f.feed(EVENT_A + b": x-horizon-verified-savings-usd=0.5\r\n\r\n" + EVENT_B)
    assert out + f.finish() == EVENT_A + EVENT_B
    assert f.verified == pytest.approx(0.5)


def test_claude_code_client_parser_sees_the_same_events_with_or_without_the_comment() -> None:
    """Per the SSE spec the comment is ignored: the parsed events do not change."""
    assert _spec_events(STREAM) == _spec_events(EVENT_A + EVENT_B)
    assert [name for name, _ in _spec_events(STREAM)] == ["message_start", "message_stop"]


# ----------------------------------------------------------------- relay


def test_health_is_answered_locally_and_never_relayed() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.setdefault("calls", []).append(request)
        return httpx.Response(200)

    client = TestClient(_app(handler, port=18788, started_at="2026-10-18T00:00:00+00:00"))
    resp = client.get(HEALTH_PATH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["port"] == 18788
    assert body["remote"] == REMOTE
    assert set(body) >= {"ok", "version", "pid", "port", "remote", "started_at"}
    assert "calls" not in seen


def test_messages_are_tagged_and_the_client_cannot_claim_the_tag() -> None:
    seen: dict = {}
    client = TestClient(
        _app(_stream_handler(seen, body=b'{"ok":true}', extra={"content-type": "application/json"}))
    )

    client.post("/v1/messages", json={"model": "m"}, headers={"x-horizon-forwarder": "spoof"})
    client.post("/v1/other", json={}, headers={"x-horizon-forwarder": "spoof"})

    first, second = seen["calls"]
    assert first.headers["x-horizon-forwarder"] == "claude-code"
    assert "x-horizon-forwarder" not in second.headers


def test_pause_file_bypasses_horizon_on_model_calls(tmp_path: Path) -> None:
    seen: dict = {}
    pause = tmp_path / "paused"
    client = TestClient(
        _app(
            _stream_handler(seen, body=b"{}", extra={"content-type": "application/json"}),
            pause_file=pause,
        )
    )

    client.post("/v1/messages", json={"model": "m"})
    assert "x-horizon-bypass" not in seen["calls"][-1].headers

    pause.write_text("", encoding="utf-8")
    client.post("/v1/messages", json={"model": "m"})
    assert seen["calls"][-1].headers["x-horizon-bypass"] == "true"


def test_streamed_response_reaches_the_client_without_the_comment_and_is_counted(
    tmp_path: Path,
) -> None:
    seen: dict = {}
    stats = ClaudeCodeStats(tmp_path / "claude-code")
    client = TestClient(_app(_stream_handler(seen), stats=stats))

    resp = client.post(
        "/v1/messages",
        json={"model": "claude-sonnet-4-5", "stream": True},
        headers={"x-claude-code-session-id": "sess-1"},
    )

    assert resp.status_code == 200
    assert resp.content == EVENT_A + EVENT_B  # upstream bytes minus the comment
    stats.close()
    status = json.loads((tmp_path / "claude-code" / "status.json").read_text(encoding="utf-8"))
    session = status["sessions"]["sess-1"]
    assert session["requests"] == 1
    assert session["tokens_before"] == 1000
    assert session["tokens_after"] == 600
    assert session["verified_saved_usd"] == pytest.approx(0.25)
    assert session["verified_requests"] == 1
    price = list_input_price_per_token("claude-sonnet-4-5")
    assert price is not None
    assert session["trimmed_list_usd"] == pytest.approx(400 * price, rel=1e-4)
    assert session["unpriced"] is False
    assert status["all_time"]["requests"] == 1
    text = (tmp_path / "claude-code" / "status.txt").read_text(encoding="utf-8")
    assert text.startswith("ContextShrink ") and "trimmed 40%" in text and "verified" in text


def test_non_streamed_reply_takes_the_verified_figure_from_the_header(tmp_path: Path) -> None:
    body = json.dumps({"usage": {"cache_creation_input_tokens": 900}}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={
                "content-type": "application/json",
                "x-horizon-tokens-before": "50",
                "x-horizon-tokens-after": "50",
                "x-horizon-tokens-saved": "0",
                "x-horizon-model": "claude-sonnet-4-5",
                "x-horizon-verified-savings-usd": "0.125000",
            },
        )

    stats = ClaudeCodeStats(tmp_path / "claude-code")
    client = TestClient(_app(handler, stats=stats))
    resp = client.post(
        "/v1/messages",
        json={"model": "claude-sonnet-4-5"},
        headers={"x-claude-code-session-id": "s2"},
    )

    assert resp.content == body
    stats.close()
    status = json.loads((tmp_path / "claude-code" / "status.json").read_text(encoding="utf-8"))
    session = status["sessions"]["s2"]
    assert session["verified_saved_usd"] == pytest.approx(0.125)
    assert session["cache"]["last_write_at"] != ""
    assert session["cache"]["ttl_s"] == 300


def test_keepalive_path_gives_the_session_id_when_no_header_is_sent(tmp_path: Path) -> None:
    stats = ClaudeCodeStats(tmp_path / "claude-code")
    client = TestClient(_app(_stream_handler({}), stats=stats))
    client.post("/k/abc123/v1/messages", json={"model": "claude-sonnet-4-5"})
    stats.close()
    status = json.loads((tmp_path / "claude-code" / "status.json").read_text(encoding="utf-8"))
    assert "abc123" in status["sessions"]
    assert status["sessions"]["abc123"]["cache"]["keepalive"] is True


def test_unpriced_model_contributes_zero_and_marks_the_session(tmp_path: Path) -> None:
    stats = ClaudeCodeStats(tmp_path / "claude-code")
    client = TestClient(
        _app(_stream_handler({}, extra={"x-horizon-model": "no-such-model-xyz"}), stats=stats)
    )
    client.post(
        "/v1/messages",
        json={"model": "no-such-model-xyz"},
        headers={"x-claude-code-session-id": "u"},
    )
    stats.close()
    session = json.loads((tmp_path / "claude-code" / "status.json").read_text(encoding="utf-8"))[
        "sessions"
    ]["u"]
    assert session["unpriced"] is True
    assert session["trimmed_list_usd"] == 0


def test_stats_survive_a_restart(tmp_path: Path) -> None:
    first = ClaudeCodeStats(tmp_path / "claude-code")
    first.record(
        session_id="a",
        model="",
        tokens_before=10,
        tokens_after=5,
        tokens_saved=5,
        list_usd=0.0,
        unpriced=False,
        verified_usd=None,
        cache_written=False,
        keepalive=False,
        now=time.time(),
    )
    first.flush(forwarder={"pid": 1, "version": "t", "port": 1})
    first.close()

    second = ClaudeCodeStats(tmp_path / "claude-code")
    snap = second.snapshot(now=time.time(), forwarder={"pid": 2, "version": "t", "port": 1})
    second.close()
    assert snap["all_time"]["requests"] == 1
    assert "a" in snap["sessions"]


# ----------------------------------------------------------------- status line


def _status(updated_at: str, **overrides) -> dict:
    base = {
        "updated_at": updated_at,
        "forwarder": {"healthy": True},
        "paused": False,
        "sessions": {
            "s": {
                "last_at": updated_at,
                "tokens_before": 1000,
                "tokens_after": 780,
                "trimmed_list_usd": 1.84,
                "verified_saved_usd": 0.41,
                "verified_requests": 3,
                "requests": 18,
                "unpriced": False,
            }
        },
    }
    base.update(overrides)
    return base


def test_status_line_matches_the_contract_example() -> None:
    now = time.time()
    from datetime import datetime, timezone

    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    assert render_status_line(_status(stamp), now) == (
        "ContextShrink ✓ trimmed 22% · $1.84 of tokens at list price · "
        "saved $0.41 verified · 18 req"
    )


def test_status_line_reads_off_when_the_heartbeat_is_stale_and_paused_when_paused() -> None:
    now = time.time()
    from datetime import datetime, timezone

    old = datetime.fromtimestamp(now - 60, timezone.utc).isoformat(timespec="seconds")
    fresh = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    assert render_status_line(_status(old), now) == "ContextShrink off"
    assert render_status_line(_status(fresh, paused=True), now) == "ContextShrink paused"


def test_status_line_says_pending_until_a_verified_figure_exists() -> None:
    now = time.time()
    from datetime import datetime, timezone

    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    status = _status(stamp)
    status["sessions"]["s"]["verified_requests"] = 0
    assert "saved pending verified" in render_status_line(status, now)
