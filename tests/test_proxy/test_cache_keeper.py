"""Cache keep-alive: pre-warm an idle session's prompt cache before it expires.

A paused agent session otherwise rewrites its whole context on the next turn
(2x input on the 1-hour lane) instead of reading it (0.1x). These tests drive
the keeper with a fake clock and a fake upstream.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from horizon.cli.wrap import _apply_keepalive_header_env, _end_keepalive
from horizon.proxy.cache_keeper import (
    EXTENDED_TTL_BETA,
    TTL_1H,
    TTL_5M,
    CacheKeeper,
    add_beta,
    group_of,
    prewarm_body,
    ttl_of,
    upgrade_cache_ttl_to_1h,
)

URL = "https://api.anthropic.com/v1/messages?beta=true"
HEADERS = {"x-api-key": "sk-ant-secret", "anthropic-version": "2023-06-01", "content-length": "99"}


def _body(ttl: str | None = "1h", **extra: Any) -> dict[str, Any]:
    marker = {"type": "ephemeral", **({"ttl": ttl} if ttl else {})}
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 32000,
        "stream": True,
        "thinking": {"type": "adaptive"},
        "tools": [
            {"name": "Read", "input_schema": {"type": "object"}, "cache_control": dict(marker)}
        ],
        "system": [{"type": "text", "text": "You are an agent.", "cache_control": dict(marker)}],
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "hi", "cache_control": dict(marker)}],
            }
        ],
        **extra,
    }


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Upstream:
    def __init__(self, read: int = 400_000, write: int = 0, status: int = 200) -> None:
        self.sent: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self.read, self.write, self.status = read, write, status

    async def __call__(self, url: str, headers: dict[str, str], body: dict[str, Any]):
        self.sent.append((url, headers, body))
        return self.status, {
            "cache_read_input_tokens": self.read,
            "cache_creation_input_tokens": self.write,
        }


def _keeper(upstream: _Upstream, clock: _Clock, **kw: Any) -> CacheKeeper:
    return CacheKeeper(upstream, clock=clock, **kw)


def _turn(
    keeper: CacheKeeper,
    rid: str,
    body: dict[str, Any],
    *,
    read=380_000,
    write=20_000,
    liveness=None,
):
    keeper.record_request(rid, url=URL, headers=dict(HEADERS), body=body, liveness_id=liveness)
    return keeper.record_usage(
        rid, model="claude-opus-5-5", cache_read=read, cache_write=write, uncached=10
    )


def _run(coro):
    return asyncio.run(coro)


# -- request shapes -----------------------------------------------------------


def test_lane_follows_the_markers() -> None:
    assert ttl_of(_body("1h")) == TTL_1H
    assert ttl_of(_body(None)) == TTL_5M


def test_prewarm_asks_for_no_output_and_leaves_the_original_alone() -> None:
    body = _body()
    warm = prewarm_body(body)
    assert warm["max_tokens"] == 0 and warm["stream"] is False
    assert warm["messages"] == body["messages"] and warm["thinking"] == {"type": "adaptive"}
    assert body["max_tokens"] == 32000 and body["stream"] is True


@pytest.mark.parametrize(
    "extra",
    [
        {"thinking": {"type": "enabled", "budget_tokens": 2048}},
        {"tool_choice": {"type": "any"}},
        {"output_config": {"format": {"type": "json_schema"}}},
    ],
)
def test_prewarm_skips_requests_the_api_would_reject(extra) -> None:
    assert prewarm_body(_body(**extra)) is None


def test_groups_never_carry_the_credential() -> None:
    assert group_of(HEADERS, "abc") == "live:abc"
    key = group_of(HEADERS)
    assert key.startswith("cred:") and "secret" not in key
    assert group_of({"x-api-key": "other"}) != key


def test_ttl_upgrade_moves_every_short_marker() -> None:
    body = _body(None)
    assert upgrade_cache_ttl_to_1h(body) == 3
    assert ttl_of(body) == TTL_1H
    assert upgrade_cache_ttl_to_1h(body) == 0


def test_beta_header_added_once() -> None:
    headers = {"Anthropic-Beta": "claude-code-20250219"}
    add_beta(headers, EXTENDED_TTL_BETA)
    add_beta(headers, EXTENDED_TTL_BETA)
    assert headers == {"Anthropic-Beta": f"claude-code-20250219,{EXTENDED_TTL_BETA}"}
    fresh: dict[str, str] = {}
    add_beta(fresh, EXTENDED_TTL_BETA)
    assert fresh == {"anthropic-beta": EXTENDED_TTL_BETA}


# -- scheduling -----------------------------------------------------------------


def test_one_hour_lane_is_pinged_five_minutes_before_expiry() -> None:
    clock, up = _Clock(), _Upstream()
    keeper = _keeper(up, clock)
    _turn(keeper, "r1", _body("1h"))
    clock.t += 3600 - 301
    assert _run(keeper.tick()) == 0
    clock.t += 2
    assert _run(keeper.tick()) == 1
    url, headers, body = up.sent[0]
    assert url == URL and body["max_tokens"] == 0 and headers["x-api-key"] == "sk-ant-secret"
    clock.t += 3600 - 300
    assert _run(keeper.tick()) == 1


def test_five_minute_lane_pings_inside_the_window() -> None:
    clock, up = _Clock(), _Upstream()
    keeper = _keeper(up, clock)
    _turn(keeper, "r1", _body(None))
    clock.t += 300 - 44
    assert _run(keeper.tick()) == 1


@pytest.mark.parametrize(("ttl", "expected"), [("1h", 9), (None, 5)])
def test_pings_stop_when_they_cost_more_than_they_can_save(ttl, expected) -> None:
    clock, up = _Clock(), _Upstream()
    keeper = _keeper(
        up, clock, max_idle_seconds=10**6
    )  # isolate the price budget from the idle cap
    _turn(keeper, "r1", _body(ttl))
    for _ in range(30):
        clock.t += 3600 if ttl else 300
        _run(keeper.tick())
    assert len(up.sent) == expected


def test_idle_cap_stops_pinging() -> None:
    clock, up = _Clock(), _Upstream()
    keeper = _keeper(up, clock, max_idle_seconds=2 * 3600)
    _turn(keeper, "r1", _body("1h"))
    for _ in range(5):
        clock.t += 3300
        _run(keeper.tick())
    assert len(up.sent) == 2


def test_side_calls_and_small_contexts_are_not_kept_warm() -> None:
    clock, up = _Clock(), _Upstream()
    keeper = _keeper(up, clock)
    side = _body("1h")
    side.pop("tools")
    _turn(keeper, "side", side)
    _turn(keeper, "small", _body("1h"), read=5_000, write=1_000)
    clock.t += 3600
    assert _run(keeper.tick()) == 0


def test_a_ping_that_has_to_write_stops_the_group() -> None:
    clock, up = _Clock(), _Upstream(read=0, write=400_000)
    keeper = _keeper(up, clock)
    _turn(keeper, "r1", _body("1h"))
    clock.t += 3400
    _run(keeper.tick())
    clock.t += 3400
    _run(keeper.tick())
    assert len(up.sent) == 1


def test_wrap_exit_ends_its_session() -> None:
    clock, up = _Clock(), _Upstream()
    keeper = _keeper(up, clock)
    _turn(keeper, "r1", _body("1h"), liveness="L1")
    assert keeper.end("L1") is True
    _turn(keeper, "r2", _body("1h"), liveness="L1")  # a late request after exit is ignored
    clock.t += 3600
    assert _run(keeper.tick()) == 0


def test_the_latest_request_in_a_group_is_the_one_kept_warm() -> None:
    clock, up = _Clock(), _Upstream()
    keeper = _keeper(up, clock)
    _turn(keeper, "r1", _body("1h"))
    later = _body("1h")
    later["messages"].append({"role": "assistant", "content": "done"})
    clock.t += 60
    _turn(keeper, "r2", later)
    clock.t += 3400
    _run(keeper.tick())
    assert up.sent[0][2]["messages"][-1] == {"role": "assistant", "content": "done"}


# -- measurement ----------------------------------------------------------------


def test_a_resume_that_reads_the_kept_cache_records_the_rewrite_avoided(tmp_path) -> None:
    clock, up = _Clock(), _Upstream(read=400_000)
    keeper = _keeper(up, clock, ledger_path=tmp_path / "ledger.jsonl")
    # A new session finds only the shared system prompt and tools warm (8k).
    _turn(keeper, "r1", _body("1h"), read=8_000, write=392_000)
    clock.t += 3400
    _run(keeper.tick())
    clock.t += 1200  # user returns after 77 minutes: past the 1-hour lifetime
    event = _turn(keeper, "r2", _body("1h"), read=400_000, write=3_000)
    assert event is not None and event["event"] == "cache_keeper_resume"
    assert event["pings"] == 1 and event["idle_s"] == 4600
    assert event["kept_tokens"] == 392_000  # the shared 8k would have been read anyway
    assert event["avoided_usd"] > event["ping_cost_usd"] > 0
    assert event["net_usd"] == pytest.approx(event["avoided_usd"] - event["ping_cost_usd"])
    kinds = [
        json.loads(line)["event"] for line in (tmp_path / "ledger.jsonl").read_text().splitlines()
    ]
    assert kinds == ["cache_keeper_ping", "cache_keeper_resume"]


def test_tool_definitions_are_never_credited() -> None:
    """Other sessions on the account keep the shared tools warm; a resume reads them regardless."""
    clock, up = _Clock(), _Upstream(read=400_000)
    keeper = _keeper(up, clock)
    body = _body("1h")
    body["tools"] = [
        {"name": f"T{i}", "description": "x" * 400, "input_schema": {}} for i in range(100)
    ]
    tools = len(json.dumps(body["tools"])) // 4
    _turn(keeper, "r1", body, read=0, write=400_000)  # nothing warm at the start
    clock.t += 3400
    _run(keeper.tick())
    clock.t += 1200
    event = _turn(keeper, "r2", body, read=400_000, write=3_000)
    assert event["kept_tokens"] == 400_000 - tools


def test_an_active_session_records_nothing() -> None:
    clock, up = _Clock(), _Upstream()
    keeper = _keeper(up, clock)
    _turn(keeper, "r1", _body("1h"))
    clock.t += 120
    assert _turn(keeper, "r2", _body("1h")) is None
    assert up.sent == []


# -- wrap -------------------------------------------------------------------------


def test_wrap_tags_each_launch_and_respects_a_user_header() -> None:
    env = {"ANTHROPIC_CUSTOM_HEADERS": "X-Horizon-Project: demo"}
    kid = _apply_keepalive_header_env(env)
    assert (
        kid
        and env["ANTHROPIC_CUSTOM_HEADERS"]
        == f"X-Horizon-Project: demo\nX-Horizon-Keepalive-Id: {kid}"
    )
    mine = {"ANTHROPIC_CUSTOM_HEADERS": "x-horizon-keepalive-id: fixed"}
    assert _apply_keepalive_header_env(mine) is None
    assert mine["ANTHROPIC_CUSTOM_HEADERS"] == "x-horizon-keepalive-id: fixed"


def test_wrap_exit_notice_never_raises() -> None:
    _end_keepalive("http://127.0.0.1:9", "abc")  # nothing listening
    _end_keepalive(None, "abc")
