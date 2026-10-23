"""Cache keep-alive, hardened: credential rotation, the forwarder's idle cap, settlement.

Driven with a fake clock and a fake upstream; nothing reaches the network.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from horizon.proxy.cache_keeper import CacheKeeper, group_of

URL = "https://api.anthropic.com/v1/messages?beta=true"
MODEL = "claude-opus-5-5"
LIVENESS = "L"


def _body() -> dict[str, Any]:
    marker = {"type": "ephemeral", "ttl": "1h"}
    return {
        "model": MODEL,
        "max_tokens": 32000,
        "stream": True,
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
    }


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Upstream:
    """Reads a warm cache for any accepted credential; refuses the rest with 401."""

    def __init__(self, accepted: set[str], read: int = 400_000) -> None:
        self.accepted = accepted
        self.read = read
        self.tokens: list[str] = []

    async def __call__(self, url: str, headers: dict[str, str], body: dict[str, Any]):
        token = headers["x-api-key"]
        self.tokens.append(token)
        if token not in self.accepted:
            return 401, {}
        return 200, {
            "cache_read_input_tokens": self.read,
            "cache_creation_input_tokens": 0,
            "input_tokens": 0,
        }


class _Billing:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def __call__(self, owner: Any, record: dict[str, Any]) -> None:
        self.rows.append(record)


def _request(
    keeper: CacheKeeper,
    rid: str,
    token: str,
    *,
    liveness: str | None = LIVENESS,
    owner: Any = None,
    forwarder: bool = False,
    read: int = 380_000,
    write: int = 20_000,
) -> dict[str, Any] | None:
    """A real request: recorded at its start, settled at its end (as the handler does)."""
    headers = {"x-api-key": token, "anthropic-version": "2023-06-01"}
    keeper.record_request(
        rid,
        url=URL,
        headers=headers,
        body=_body(),
        liveness_id=liveness,
        owner=owner,
        forwarder=forwarder,
    )
    return keeper.record_usage(rid, model=MODEL, cache_read=read, cache_write=write, uncached=10)


def _key(liveness: str = LIVENESS, owner_id: str | None = None) -> str:
    return group_of({}, liveness, owner_id)


def _run(coro):
    return asyncio.run(coro)


# -- credentials rotate ---------------------------------------------------------


def test_pings_carry_the_credentials_of_the_newest_request() -> None:
    clock, up = _Clock(), _Upstream({"new"})
    keeper = CacheKeeper(up, clock=clock)
    _request(keeper, "r1", "old")
    clock.t += 100
    _request(keeper, "r2", "new")
    clock.t += 3300  # the 1-hour lane's ping window
    assert _run(keeper.tick()) == 1
    assert up.tokens == ["new"]


def test_a_request_that_then_fails_still_refreshes_the_ping_credentials() -> None:
    """The newest credentials are used from the moment the request starts."""
    clock, up = _Clock(), _Upstream({"new"})
    keeper = CacheKeeper(up, clock=clock)
    _request(keeper, "r1", "old")
    clock.t += 100
    keeper.record_request(  # r2 starts; it never completes
        "r2", url=URL, headers={"x-api-key": "new"}, body=_body(), liveness_id=LIVENESS
    )
    clock.t += 3300
    assert _run(keeper.tick()) == 1
    assert up.tokens == ["new"]


def test_a_refused_ping_waits_for_the_next_real_request_instead_of_stopping() -> None:
    clock, up = _Clock(), _Upstream({"new"})
    keeper = CacheKeeper(up, clock=clock)
    _request(keeper, "r1", "old")  # due at 1000 + 3300

    clock.t += 3300
    assert _run(keeper.tick()) == 1
    assert up.tokens == ["old"]
    group = keeper._groups[_key()]
    assert group.waiting and group.body and not group.stopped  # refused, not gone

    clock.t += 3600  # nothing is sent while the credentials are refused
    assert _run(keeper.tick()) == 0
    assert up.tokens == ["old"]

    clock.t += 100
    _request(keeper, "r2", "new")  # the next real request brings fresh credentials
    assert not group.waiting
    clock.t += 3300
    assert _run(keeper.tick()) == 1
    assert up.tokens == ["old", "new"]
    assert group.pings == 1 and not group.stopped


def test_a_refused_ping_is_still_booked_but_is_not_a_kept_ping() -> None:
    clock, up = _Clock(), _Upstream({"new"})
    billing = _Billing()
    keeper = CacheKeeper(up, clock=clock, reporter=billing)
    owner = SimpleNamespace(user_id="u1", compression_allowed=True)
    _request(keeper, "r1", "old", owner=owner)
    clock.t += 3300
    _run(keeper.tick())
    (row,) = billing.rows
    assert row["status"] == 401 and row["cost_usd"] == 0
    assert keeper._groups[_key(owner_id="u1")].pings == 0


# -- the forwarder's idle cap -----------------------------------------------------


def test_a_forwarder_group_stops_at_two_hours_and_a_plain_group_keeps_going() -> None:
    clock = _Clock()
    up = _Upstream({"tok-f", "tok-p"})
    keeper = CacheKeeper(up, clock=clock)  # forwarder cap defaults to 2 hours
    _request(keeper, "f", "tok-f", liveness="F", forwarder=True)
    _request(keeper, "p", "tok-p", liveness="P")

    for at in (4300, 7600):  # the 1-hour lane's ping windows, both groups
        clock.t = at
        _run(keeper.tick())
    assert up.tokens.count("tok-f") == 2 and up.tokens.count("tok-p") == 2

    clock.t = 8300  # 7300 s after the last real request: past the forwarder's 2 hours
    _run(keeper.tick())
    assert keeper._groups[_key("F")].body == {}  # released, credentials dropped

    clock.t = 10900
    _run(keeper.tick())
    assert up.tokens.count("tok-f") == 2
    assert up.tokens.count("tok-p") == 3  # the plain group is still inside its 8 hours


def test_the_forwarder_idle_cap_is_configurable() -> None:
    clock, up = _Clock(), _Upstream({"tok"})
    keeper = CacheKeeper(up, clock=clock, forwarder_max_idle_seconds=4000)
    _request(keeper, "f", "tok", liveness="F", forwarder=True)
    clock.t = 1000 + 4100
    _run(keeper.tick())
    assert keeper._groups[_key("F")].body == {}


# -- settlement: what each completed request settles in its group ------------------


def test_settlement_books_every_ping_of_the_gap_and_then_resets() -> None:
    clock, up = _Clock(), _Upstream({"tok"})
    billing = _Billing()
    keeper = CacheKeeper(up, clock=clock, reporter=billing)
    owner = SimpleNamespace(user_id="u1", compression_allowed=True)

    _request(keeper, "r1", "tok", owner=owner)
    for at in (4300, 7600):
        clock.t = at
        _run(keeper.tick())
    clock.t = 8000  # the user returns 7000 s after their last request
    event = _request(keeper, "r2", "tok", owner=owner, read=400_000, write=3_000)

    assert event is not None and event["event"] == "cache_keeper_resume"
    settled = keeper.last_settlement
    assert settled.avoided_usd == pytest.approx(event["avoided_usd"])
    booked = sum(row["cost_usd"] for row in billing.rows)
    assert settled.ping_usd == pytest.approx(booked) and settled.ping_usd > 0

    _request(keeper, "r3", "tok", owner=owner)
    assert keeper.last_settlement.ping_usd == 0.0  # nothing was booked since r2
    assert keeper.last_settlement.avoided_usd == 0.0


def test_a_ping_that_stopped_the_group_is_still_part_of_the_gap_it_cost() -> None:
    """A miss is billed in full, so its cost belongs to the gap it was sent in."""
    clock = _Clock()
    billing = _Billing()

    async def cache_gone(url, headers, body):
        return 200, {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 400_000}

    keeper = CacheKeeper(cache_gone, clock=clock, reporter=billing)
    owner = SimpleNamespace(user_id="u1", compression_allowed=True)
    _request(keeper, "r1", "tok", owner=owner)
    clock.t += 3300
    _run(keeper.tick())
    assert keeper._groups[_key(owner_id="u1")].stopped

    _request(keeper, "r2", "tok", owner=owner)
    (row,) = billing.rows
    assert row["event"] == "cache_keeper_stop" and row["cost_usd"] > 0
    assert keeper.last_settlement.ping_usd == pytest.approx(row["cost_usd"])


def test_a_request_too_small_to_keep_settles_nothing_and_leaves_the_pings_for_the_next() -> None:
    clock, up = _Clock(), _Upstream({"tok"})
    billing = _Billing()
    keeper = CacheKeeper(up, clock=clock, reporter=billing)
    owner = SimpleNamespace(user_id="u1", compression_allowed=True)
    _request(keeper, "r1", "tok", owner=owner)
    clock.t += 3300
    _run(keeper.tick())
    _request(keeper, "tiny", "tok", owner=owner, read=0, write=0)
    assert keeper.last_settlement.ping_usd == 0.0
    _request(keeper, "r2", "tok", owner=owner)
    assert keeper.last_settlement.ping_usd == pytest.approx(billing.rows[0]["cost_usd"])


# -- end and revival ----------------------------------------------------------------


def test_end_stops_pings_and_a_ping_after_end_does_nothing() -> None:
    clock, up = _Clock(), _Upstream({"tok"})
    keeper = CacheKeeper(up, clock=clock)
    _request(keeper, "r1", "tok", liveness="E")
    group = keeper._groups[_key("E")]
    assert keeper.end("E") is True

    clock.t += 3300  # the ping window: nothing is due for an ended id
    assert _run(keeper.tick()) == 0
    assert up.tokens == []
    # A ping already holding the old group object does nothing either, and does not revive it.
    assert _run(keeper._ping(group, asyncio.Semaphore(1))) == 0
    assert up.tokens == [] and group.pings == 0
    assert _key("E") not in keeper._groups


def test_a_new_real_request_revives_an_ended_id_with_fresh_state() -> None:
    clock, up = _Clock(), _Upstream({"old", "new"})
    keeper = CacheKeeper(up, clock=clock)
    _request(keeper, "r1", "old", liveness="E")
    clock.t = 2000
    assert keeper.end("E") is True

    clock.t = 5000  # ``claude --resume`` reuses the id
    _request(keeper, "r2", "new", liveness="E")
    key = _key("E")
    assert key not in keeper._ended
    group = keeper._groups[key]
    assert group.headers["x-api-key"] == "new" and group.pings == 0 and not group.stopped

    clock.t += 3300
    assert _run(keeper.tick()) == 1
    assert up.tokens == ["new"]
    assert group.pings == 1


def test_end_is_idempotent_and_harmless_without_a_group() -> None:
    clock, up = _Clock(), _Upstream({"tok"})
    keeper = CacheKeeper(up, clock=clock)
    assert keeper.end("never-seen") is False
    assert keeper.end("never-seen") is False  # a second end changes nothing
    _request(keeper, "r1", "tok", liveness="never-seen")  # a real request still starts a group
    assert _key("never-seen") in keeper._groups
    clock.t += 3300
    assert _run(keeper.tick()) == 1


def test_team_and_personal_keepalive_groups_cannot_merge_for_the_same_user():
    from horizon.proxy.account_analytics import AccountContext
    clock = _Clock()
    upstream = _Upstream({"tok"})
    keeper = CacheKeeper(upstream, clock=clock)
    personal = AccountContext("same-user", "personal-key", None)
    team = AccountContext("same-user", "team-key", None, team_id="team", membership_id="member")
    _request(keeper, "personal", "tok", owner=personal)
    _request(keeper, "team", "tok", owner=team)
    assert _key(owner_id=personal.tenant_id) in keeper._groups
    assert _key(owner_id=team.tenant_id) in keeper._groups
    assert len(keeper._groups) == 2
    assert keeper.end(LIVENESS, team.tenant_id) is True
    assert _key(owner_id=team.tenant_id) not in keeper._groups
    assert not keeper._groups[_key(owner_id=personal.tenant_id)].stopped


def test_background_team_ping_stops_when_membership_authorization_is_revoked():
    from horizon.proxy.account_analytics import AccountContext
    clock, up = _Clock(), _Upstream({"tok"})
    keeper = CacheKeeper(up, clock=clock)
    calls = []
    async def denied():
        calls.append(True)
        return 403, None
    owner = AccountContext("user", "key", None, team_id="team", membership_id="member", revalidate=denied)
    _request(keeper, "before-removal", "tok", owner=owner)
    clock.t += 3300
    assert _run(keeper.tick()) == 0
    assert calls == [True]
    assert not up.tokens  # No upstream request or bill after revocation.
    group = keeper._groups[_key(owner_id=owner.tenant_id)]
    assert group.stopped and not group.headers and not group.body
