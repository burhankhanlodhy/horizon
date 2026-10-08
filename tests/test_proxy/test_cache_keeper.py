"""Cache keep-alive: pre-warm an idle session's prompt cache before it expires.

A paused agent session otherwise rewrites its whole context on the next turn
(2x input on the 1-hour lane) instead of reading it (0.1x). These tests drive
the keeper with a fake clock and a fake upstream.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from horizon.cli.wrap import _apply_keepalive_header_env, _end_keepalive
from horizon.proxy.cache_keeper import (
    EXTENDED_TTL_BETA,
    TTL_1H,
    TTL_5M,
    CacheKeeper,
    _rates,
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
    def __init__(
        self, read: int = 400_000, write: int = 0, status: int = 200, uncached: int = 0
    ) -> None:
        self.sent: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self.read, self.write, self.status, self.uncached = read, write, status, uncached

    async def __call__(self, url: str, headers: dict[str, str], body: dict[str, Any]):
        self.sent.append((url, headers, body))
        return self.status, {
            "cache_read_input_tokens": self.read,
            "cache_creation_input_tokens": self.write,
            "input_tokens": self.uncached,
        }


class _Billing:
    """Collects what the keeper bills to each owner."""

    def __init__(self) -> None:
        self.rows: list[tuple[Any, dict[str, Any]]] = []

    async def __call__(self, owner: Any, record: dict[str, Any]) -> None:
        self.rows.append((owner, record))


def _keeper(upstream: Any, clock: _Clock, **kw: Any) -> CacheKeeper:
    return CacheKeeper(upstream, clock=clock, **kw)


def _account(user: str = "u1", allowed: bool = True) -> SimpleNamespace:
    return SimpleNamespace(user_id=user, compression_allowed=allowed)


def _turn(
    keeper: CacheKeeper,
    rid: str,
    body: dict[str, Any],
    *,
    read=380_000,
    write=20_000,
    liveness=None,
    owner=None,
):
    keeper.record_request(
        rid, url=URL, headers=dict(HEADERS), body=body, liveness_id=liveness, owner=owner
    )
    return keeper.record_usage(
        rid, model="claude-opus-5-5", cache_read=read, cache_write=write, uncached=10
    )


def _run(coro):
    return asyncio.run(coro)


# -- request shapes -----------------------------------------------------------


def test_lane_follows_the_markers() -> None:
    assert ttl_of(_body("1h")) == TTL_1H
    assert ttl_of(_body(None)) == TTL_5M


def test_the_shortest_entry_sets_the_lane() -> None:
    """1-hour tools with 5-minute messages: the conversation expires in five minutes."""
    body = _body("1h")
    body["messages"][0]["content"][0]["cache_control"] = {"type": "ephemeral"}
    assert ttl_of(body) == TTL_5M


def test_automatic_caching_counts_and_no_markers_means_nothing_to_keep() -> None:
    bare = {"tools": [{"name": "Read"}], "messages": [{"role": "user", "content": "hi"}]}
    assert ttl_of(bare) is None
    assert ttl_of({**bare, "cache_control": {"type": "ephemeral", "ttl": "1h"}}) == TTL_1H


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
    live = group_of(HEADERS, "abc")
    assert live.startswith("live:") and "abc" not in live
    key = group_of(HEADERS)
    assert key.startswith("cred:") and "secret" not in key
    assert group_of({"x-api-key": "other"}) != key


def test_the_same_liveness_id_never_crosses_accounts() -> None:
    assert group_of(HEADERS, "abc", "u1") != group_of(HEADERS, "abc", "u2")
    assert group_of(HEADERS, "abc", "u1") == group_of({"x-api-key": "x"}, "abc", "u1")


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


def _rates_at(read_multiplier: float):
    base = 4e-6
    return lambda model: {
        "read": base * read_multiplier,
        "w5m": base * 1.25,
        "w1h": base * 2,
        "input": base,
        "basis": "catalog",
    }


@pytest.mark.parametrize(
    ("ttl", "step", "read_multiplier", "expected"),
    [
        ("1h", 3400, 0.1, 9),
        (None, 270, 0.1, 5),
        # 0.05x reads (Opus / Sonnet 5.5): a ping costs half, so twice as many pay.
        ("1h", 3400, 0.05, 19),
        (None, 270, 0.05, 12),
    ],
)
def test_pings_stop_when_they_cost_more_than_they_can_save(
    ttl, step, read_multiplier, expected, monkeypatch
) -> None:
    monkeypatch.setattr("horizon.proxy.cache_keeper._rates", _rates_at(read_multiplier))
    clock, up = _Clock(), _Upstream()
    keeper = _keeper(
        up, clock, max_idle_seconds=10**6
    )  # isolate the price budget from the idle cap
    _turn(keeper, "r1", _body(ttl))
    for _ in range(30):
        clock.t += step
        _run(keeper.tick())
    assert len(up.sent) == expected


def test_an_unpriced_model_uses_the_structural_ratio(monkeypatch) -> None:
    monkeypatch.setattr(
        "horizon.proxy.cache_keeper._rates",
        lambda model: {"read": 0.0, "w5m": 0.0, "w1h": 0.0, "input": 0.0, "basis": "fallback"},
    )
    keeper = _keeper(_Upstream(), _Clock())
    assert keeper.max_pings(TTL_1H, "mystery-model") == 9
    assert keeper.max_pings(TTL_5M, "mystery-model") == 5


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
    clock.t += 3400
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


def test_a_partial_write_stops_the_group_and_is_billed_in_full() -> None:
    clock, billing = _Clock(), _Billing()
    up = _Upstream(read=100_000, write=50_000, uncached=10_000)
    keeper = _keeper(up, clock, reporter=billing)
    _turn(keeper, "r1", _body("1h"), liveness="L", owner=_account())
    clock.t += 3400
    _run(keeper.tick())
    ((_, record),) = billing.rows
    assert record["event"] == "cache_keeper_stop"
    assert (record["read"], record["write"], record["uncached"]) == (100_000, 50_000, 10_000)
    rates = _rates("claude-opus-5-5")
    assert record["cost_usd"] == pytest.approx(
        100_000 * rates["read"] + 50_000 * rates["w1h"] + 10_000 * rates["input"], rel=1e-4
    )
    clock.t += 3400
    assert keeper.due() == []


def test_a_ping_that_reads_nothing_stops_the_group() -> None:
    clock, up = _Clock(), _Upstream(read=0, write=0)
    keeper = _keeper(up, clock)
    _turn(keeper, "r1", _body("1h"))
    clock.t += 3400
    _run(keeper.tick())
    clock.t += 3400
    assert keeper.due() == []


def test_a_timed_out_ping_is_booked_as_a_full_read_and_stops() -> None:
    clock, billing = _Clock(), _Billing()

    async def hang(url, headers, body):
        await asyncio.sleep(10)

    keeper = CacheKeeper(hang, clock=clock, reporter=billing, ping_timeout=0.01)
    _turn(keeper, "r1", _body("1h"), liveness="L", owner=_account())
    clock.t += 3400
    _run(keeper.tick())
    ((_, record),) = billing.rows
    assert record["estimated"] and record["read"] == 400_010 and record["cost_usd"] > 0
    clock.t += 3400
    assert keeper.due() == []


def test_a_missed_window_is_never_pinged() -> None:
    """A scheduler that wakes after expiry would only pay to rebuild the cache."""
    clock, up = _Clock(), _Upstream()
    keeper = _keeper(up, clock)
    _turn(keeper, "r1", _body("1h"))
    clock.t += 4000
    assert _run(keeper.tick()) == 0
    clock.t += 3300
    assert _run(keeper.tick()) == 0 and up.sent == []


def test_idle_expiry_drops_the_request_and_credentials() -> None:
    clock, up = _Clock(), _Upstream()
    keeper = _keeper(up, clock, max_idle_seconds=10)
    _turn(keeper, "r1", _body("1h"))
    clock.t += 20
    assert keeper.due() == []
    (group,) = keeper._groups.values()
    assert group.body == {} and group.headers == {}


def test_memory_cap_releases_the_oldest_sessions_first() -> None:
    clock, up = _Clock(), _Upstream()
    keeper = _keeper(up, clock, max_context_tokens=900_000)
    for i in range(3):
        _turn(keeper, f"r{i}", _body("1h"), liveness=f"L{i}")
    assert [bool(g.body) for g in keeper._groups.values()] == [False, True, True]


def test_a_completion_older_than_the_target_is_ignored() -> None:
    clock, up = _Clock(), _Upstream()
    keeper = _keeper(up, clock)
    old, new = _body("1h"), _body("1h")
    new["messages"].append({"role": "assistant", "content": "newer"})
    keeper.record_request("old", url=URL, headers=dict(HEADERS), body=old)
    clock.t += 100
    keeper.record_request("new", url=URL, headers=dict(HEADERS), body=new)
    for rid in ("new", "old"):
        keeper.record_usage(rid, model="m", cache_read=380_000, cache_write=20_000, uncached=0)
    (group,) = keeper._groups.values()
    assert group.body["messages"][-1]["content"] == "newer"
    assert group.last_request_at == 1100


def test_a_ping_in_flight_never_touches_a_newer_target() -> None:
    clock, billing = _Clock(), _Billing()
    holder: dict[str, CacheKeeper] = {}

    async def slow(url, headers, body):
        clock.t += 100  # a real request lands while the ping is out
        _turn(holder["k"], "r2", _body("1h"), liveness="L", owner=_account())
        return 200, {"cache_read_input_tokens": 400_000}

    keeper = holder["k"] = CacheKeeper(slow, clock=clock, reporter=billing)
    _turn(keeper, "r1", _body("1h"), liveness="L", owner=_account())
    clock.t += 3300
    _run(keeper.tick())
    (group,) = keeper._groups.values()
    assert group.pings == 0 and group.ping_cost_usd == 0
    assert group.last_touch_at == group.last_request_at == 4400
    ((_, record),) = billing.rows  # still billed: the provider charged for it
    assert record["superseded"] is True and record["cost_usd"] > 0


def test_wrap_exit_ends_its_session() -> None:
    clock, up = _Clock(), _Upstream()
    keeper = _keeper(up, clock)
    _turn(keeper, "r1", _body("1h"), liveness="L1")
    assert keeper.end("L1") is True
    _turn(keeper, "r2", _body("1h"), liveness="L1")  # a late request after exit is ignored
    clock.t += 3400
    assert _run(keeper.tick()) == 0


def test_a_request_still_in_flight_at_exit_cannot_revive_the_session() -> None:
    clock, up = _Clock(), _Upstream()
    keeper = _keeper(up, clock)
    keeper.record_request("r1", url=URL, headers=dict(HEADERS), body=_body("1h"), liveness_id="L")
    keeper.end("L")
    keeper.record_usage("r1", model="m", cache_read=380_000, cache_write=20_000, uncached=0)
    clock.t += 3400
    assert keeper.due() == [] and not keeper._groups


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


# -- hosted proxy ---------------------------------------------------------------


def test_hosted_sessions_need_a_liveness_id_and_an_entitled_plan() -> None:
    clock, up = _Clock(), _Upstream()
    keeper = _keeper(up, clock)
    _turn(keeper, "anon", _body("1h"), owner=_account("u1"))
    _turn(keeper, "capped", _body("1h"), liveness="L2", owner=_account("u2", allowed=False))
    assert not keeper._groups
    _turn(keeper, "ok", _body("1h"), liveness="L3", owner=_account("u3"))
    assert len(keeper._groups) == 1


def test_an_account_can_end_only_its_own_session() -> None:
    clock, up = _Clock(), _Upstream()
    keeper = _keeper(up, clock)
    _turn(keeper, "r1", _body("1h"), liveness="L", owner=_account("u1"))
    assert keeper.end("L", "u2") is False
    assert keeper.end("L") is False
    assert keeper.end("L", "u1") is True


def test_every_ping_is_billed_to_its_owner() -> None:
    clock, up, billing = _Clock(), _Upstream(read=400_000), _Billing()
    keeper = _keeper(up, clock, reporter=billing)
    owner = _account()
    _turn(keeper, "r1", _body("1h"), liveness="L", owner=owner)
    _turn(keeper, "local", _body("1h"), liveness="M")  # no owner: nobody to bill
    for _ in range(2):
        clock.t += 3400
        _run(keeper.tick())
    assert len(up.sent) == 4
    assert [o for o, _ in billing.rows] == [owner, owner]
    assert all(r["event"] == "cache_keeper_ping" and r["cost_usd"] > 0 for _, r in billing.rows)


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
    # The ping's cost is its read at the model's own rate.
    rates = _rates("claude-opus-5-5")
    assert event["ping_cost_usd"] == pytest.approx(400_000 * rates["read"], rel=1e-4)
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
    body["tools"][-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
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
