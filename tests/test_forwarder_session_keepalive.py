"""The forwarder's per-session keep-alive groups, its session-end route, and signed savings.

All HTTP goes through ``httpx.MockTransport``; nothing touches the network.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from horizon.forwarder import (
    SESSION_END_PATH,
    build_app,
    per_session_path,
    session_keepalive_id,
)
from horizon.forwarder_stats import ClaudeCodeStats, SseCommentFilter, render_status_line
from horizon.verified_savings import format_verified_usd, parse_sse_comment_value, sse_comment_line

REMOTE = "https://remote.example.test"
MACHINE = "0123456789abcdef"  # Connect's per-machine id: token_hex(8)
ROUTE = f"/k/{MACHINE}/v1/messages"
END_PATH = "/v1/horizon/keepalive/end"
EVENT = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


def _digest(session: str) -> str:
    return hashlib.sha256(session.encode("utf-8")).hexdigest()[:12]


def _app(handler, **kwargs):
    return build_app(REMOTE, lambda: "hz_test", transport=httpx.MockTransport(handler), **kwargs)


def _recording(seen: list[httpx.Request], *, end_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == END_PATH:
            return httpx.Response(end_status, json={"ended": True})
        if request.url.path.endswith("/v1/messages"):
            return httpx.Response(
                200,
                content=EVENT,
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json={"ok": True})

    return handler


# ----------------------------------------------------------------- derived ids


def test_each_session_gets_its_own_id_under_the_machine_id() -> None:
    a = session_keepalive_id(MACHINE, "sess-A")
    b = session_keepalive_id(MACHINE, "sess-B")
    assert a == f"{MACHINE}-{_digest('sess-A')}" and b != a
    assert a.startswith(MACHINE + "-") and len(a) == len(MACHINE) + 1 + 12


def test_the_derived_id_fits_the_proxy_id_rules() -> None:
    import re

    derived = session_keepalive_id(MACHINE, "any session at all")
    assert derived is not None
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", derived)
    assert session_keepalive_id("x" * 60, "sess") is None  # would not fit: the machine id stands


def test_per_session_path_rewrites_only_a_keepalive_path_with_a_session() -> None:
    rewritten, derived = per_session_path(ROUTE, "sess-A")
    assert rewritten == f"/k/{derived}/v1/messages" and derived == f"{MACHINE}-{_digest('sess-A')}"
    assert per_session_path(ROUTE, "") == (ROUTE, None)  # no session header: machine id
    assert per_session_path("/v1/messages", "sess-A") == ("/v1/messages", None)


# ----------------------------------------------------------------- relay


def test_messages_are_relayed_under_the_session_keepalive_id() -> None:
    seen: list[httpx.Request] = []
    client = TestClient(_app(_recording(seen)))

    client.post(ROUTE, json={"model": "m"}, headers={"x-claude-code-session-id": "sess-A"})
    client.post(ROUTE, json={"model": "m"}, headers={"x-claude-code-session-id": "sess-B"})
    client.post(ROUTE, json={"model": "m"})  # no session header: the machine id as before

    paths = [r.url.path for r in seen]
    assert paths[0] == f"/k/{MACHINE}-{_digest('sess-A')}/v1/messages"
    assert paths[1] == f"/k/{MACHINE}-{_digest('sess-B')}/v1/messages"
    assert paths[2] == ROUTE
    assert all(r.headers["x-horizon-forwarder"] == "claude-code" for r in seen)


# ----------------------------------------------------------------- session end


def _end_calls(seen: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in seen if r.url.path == END_PATH]


def test_a_session_end_ends_that_sessions_keepalive_on_the_proxy() -> None:
    seen: list[httpx.Request] = []
    client = TestClient(_app(_recording(seen)))
    client.post(ROUTE, json={"model": "m"}, headers={"x-claude-code-session-id": "sess-A"})
    client.post(ROUTE, json={"model": "m"}, headers={"x-claude-code-session-id": "sess-B"})
    seen.clear()

    resp = client.post(SESSION_END_PATH, json={"session_id": "sess-A"})

    assert resp.status_code == 202 and resp.json() == {"accepted": 1}
    (end,) = _end_calls(seen)
    assert json.loads(end.content) == {"id": f"{MACHINE}-{_digest('sess-A')}"}
    assert end.headers["x-horizon-proxy-token"] == "hz_test"
    assert not any(r.url.path == SESSION_END_PATH for r in seen)  # never relayed


def test_a_second_session_end_or_an_unknown_session_ends_nothing() -> None:
    seen: list[httpx.Request] = []
    client = TestClient(_app(_recording(seen)))
    client.post(ROUTE, json={"model": "m"}, headers={"x-claude-code-session-id": "sess-A"})
    seen.clear()

    assert client.post(SESSION_END_PATH, json={"session_id": "sess-A"}).json() == {"accepted": 1}
    assert client.post(SESSION_END_PATH, json={"session_id": "sess-A"}).json() == {"accepted": 0}
    assert client.post(SESSION_END_PATH, json={"session_id": "never-seen"}).json() == {
        "accepted": 0
    }
    assert len(_end_calls(seen)) == 1


def test_a_failed_proxy_end_is_logged_not_raised() -> None:
    """The proxy's idle cap ends the session in that case; the hook never sees an error."""
    seen: list[httpx.Request] = []
    client = TestClient(_app(_recording(seen, end_status=503)))
    client.post(ROUTE, json={"model": "m"}, headers={"x-claude-code-session-id": "sess-A"})

    resp = client.post(SESSION_END_PATH, json={"session_id": "sess-A"})

    assert resp.status_code == 202
    assert len(_end_calls(seen)) == 1


def test_session_end_refuses_a_browser_origin_and_a_foreign_host() -> None:
    seen: list[httpx.Request] = []
    client = TestClient(_app(_recording(seen), port=18788))
    client.post(
        ROUTE,
        json={"model": "m"},
        headers={"host": "127.0.0.1:18788", "x-claude-code-session-id": "sess-A"},
    )
    seen.clear()

    cross = client.post(
        SESSION_END_PATH,
        json={"session_id": "sess-A"},
        headers={"host": "127.0.0.1:18788", "origin": "https://evil.example"},
    )
    rebound = client.post(
        SESSION_END_PATH,
        json={"session_id": "sess-A"},
        headers={"host": "attacker.example:18788"},
    )

    assert cross.status_code == 403 and rebound.status_code == 403
    assert not _end_calls(seen)  # no credential left the process


def test_session_end_rejects_a_missing_or_oversized_session_id() -> None:
    client = TestClient(_app(_recording([])))
    assert client.post(SESSION_END_PATH, json={}).status_code == 400
    assert client.post(SESSION_END_PATH, content=b"not json").status_code == 400
    assert client.post(SESSION_END_PATH, json={"session_id": "x" * 300}).status_code == 400
    assert client.post(SESSION_END_PATH, content=b"{" + b"x" * 5000 + b"}").status_code == 413


# ----------------------------------------------------------------- signed savings


def test_the_verified_figure_is_signed_on_the_wire() -> None:
    assert format_verified_usd(-0.0123) == "-0.012300"
    assert format_verified_usd(-0.0000001) == "0.000000"  # no "-0.000000"
    assert parse_sse_comment_value(b": x-horizon-verified-savings-usd=-0.012300\n") == -0.0123
    assert sse_comment_line(-0.5) == b": x-horizon-verified-savings-usd=-0.500000\n\n"


def test_a_negative_verified_figure_is_summed_as_a_cost(tmp_path: Path) -> None:
    f = SseCommentFilter()
    body = EVENT + b": x-horizon-verified-savings-usd=-0.250000\n\n"
    assert f.feed(body) + f.finish() == EVENT
    assert f.verified == -0.25

    stats = ClaudeCodeStats(tmp_path / "claude-code")
    now = time.time()
    stats.record(
        session_id="s",
        model="claude-sonnet-4-5",
        tokens_before=100,
        tokens_after=100,
        tokens_saved=0,
        list_usd=0.0,
        unpriced=False,
        verified_usd=0.5,
        cache_written=False,
        keepalive=True,
        now=now,
    )
    stats.record(
        session_id="s",
        model="claude-sonnet-4-5",
        tokens_before=100,
        tokens_after=100,
        tokens_saved=0,
        list_usd=0.0,
        unpriced=False,
        verified_usd=-0.75,
        cache_written=False,
        keepalive=True,
        now=now,
    )
    session = stats.snapshot(now=now, forwarder={"pid": 1, "version": "t", "port": 1})["sessions"][
        "s"
    ]
    stats.close()
    assert session["verified_saved_usd"] == -0.25
    assert session["verified_requests"] == 2


def test_the_status_line_says_cost_when_the_session_is_net_negative() -> None:
    from datetime import datetime, timezone

    now = time.time()
    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    status = {
        "updated_at": stamp,
        "forwarder": {"healthy": True},
        "paused": False,
        "sessions": {
            "s": {
                "last_at": stamp,
                "tokens_before": 1000,
                "tokens_after": 1000,
                "trimmed_list_usd": 0.0,
                "verified_saved_usd": -0.42,
                "verified_requests": 3,
                "requests": 3,
                "unpriced": False,
            }
        },
    }
    line = render_status_line(status, now)
    assert line.endswith("cost $0.42 verified · 3 req")
    assert "saved" not in line.split("trimmed")[1]


def test_a_proxy_with_no_group_to_end_is_harmless() -> None:
    """The proxy answers {"ended": false} when it holds no group: nothing to do, no error."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == END_PATH:
            return httpx.Response(200, json={"ended": False})
        return httpx.Response(200, json={"ok": True})

    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = TestClient(_app(recording))
    client.post(ROUTE, json={"model": "m"}, headers={"x-claude-code-session-id": "sess-Z"})
    seen.clear()

    resp = client.post(SESSION_END_PATH, json={"session_id": "sess-Z"})

    assert resp.status_code == 202 and resp.json() == {"accepted": 1}
    assert len(_end_calls(seen)) == 1
    again = client.post(SESSION_END_PATH, json={"session_id": "sess-Z"})
    assert again.json() == {"accepted": 0} and len(_end_calls(seen)) == 1
