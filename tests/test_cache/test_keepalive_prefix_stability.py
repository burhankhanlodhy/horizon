"""A kept-warm session returns to byte-identical prefix bytes after a gap of over an hour.

The proxy's frozen prefix lives in a PrefixCacheTracker, which expires after its
own idle retention (about 61 minutes on the 1-hour lane). Keep-alive pings keep
the provider's cache warm past that, so without them touching the tracker the
prefix is rebuilt on return, and the rebuilt bytes miss the cache the pings paid
to keep. These tests run the tracker and the keeper on one fake clock.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

import horizon.cache.prefix_tracker as prefix_tracker
from horizon.cache.prefix_tracker import SessionTrackerStore
from horizon.proxy.cache_keeper import CacheKeeper

URL = "https://api.anthropic.com/v1/messages?beta=true"
MODEL = "claude-opus-5-5"
SESSION = "session-under-test"
TTL = 3600  # the 1-hour lane


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    """One wall clock for the tracker (time.time) and the keeper (passed in)."""
    fake = _Clock()
    monkeypatch.setattr(prefix_tracker, "time", SimpleNamespace(time=fake))
    return fake


def _conversation(extra: int = 0) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Read the module and explain the cache."},
        {"role": "assistant", "content": "The cache keeps prefixes; " * 40},
        {"role": "user", "content": "Now the tests, please."},
    ]
    for index in range(extra):
        messages.append({"role": "assistant", "content": f"step {index} " * 30})
        messages.append({"role": "user", "content": f"continue {index}"})
    return messages


def _body(messages: list[dict[str, Any]]) -> dict[str, Any]:
    marker = {"type": "ephemeral", "ttl": "1h"}
    return {
        "model": MODEL,
        "max_tokens": 32000,
        "tools": [{"name": "Read", "input_schema": {"type": "object"}, "cache_control": marker}],
        "messages": messages,
    }


def _upstream_warm(read: int = 400_000):
    """The provider's cache: every ping reads the whole context, nothing is rewritten."""

    async def send(url: str, headers: dict[str, str], body: dict[str, Any]):
        return 200, {"cache_read_input_tokens": read, "cache_creation_input_tokens": 0}

    return send


def _dump(messages: list[dict[str, Any]]) -> bytes:
    return json.dumps(messages, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _session(clock: _Clock, *, keep_alive_touch: bool):
    """A real session: one request, then a gap in which pings keep its cache warm.

    Returns (store, tracker, keeper) with the request's prefix recorded.
    """
    store = SessionTrackerStore()
    messages = _conversation()
    tracker = store.resolve_tracker(SESSION, "anthropic", messages=messages, cache_ttl_seconds=TTL)
    tracker.update_from_response(
        cache_read_tokens=0,
        cache_write_tokens=20_000,
        messages=messages,
        original_messages=messages,
    )
    keeper = CacheKeeper(_upstream_warm(), clock=clock)
    headers = {"x-api-key": "tok", "anthropic-version": "2023-06-01"}
    keeper.record_request(
        "r1",
        url=URL,
        headers=headers,
        body=_body(messages),
        liveness_id="live",
        touch=tracker.keep_alive_touch if keep_alive_touch else None,
    )
    keeper.record_usage("r1", model=MODEL, cache_read=380_000, cache_write=20_000, uncached=10)
    for at in (4300, 7600):  # the 1-hour lane's ping windows
        clock.t = at
        asyncio.run(keeper.tick())
    return store, tracker, messages


def test_the_prefix_is_byte_identical_after_a_two_hour_gap_kept_warm_by_pings(clock) -> None:
    store, tracker, messages = _session(clock, keep_alive_touch=True)
    before = _dump(tracker.get_last_forwarded_messages())
    frozen_before = tracker.get_frozen_message_count()

    clock.t = 8000  # 7000 s, past the tracker's own 61-minute retention
    continued = messages + [
        {"role": "assistant", "content": "Here is the cache module."},
        {"role": "user", "content": "And the tests?"},
    ]
    returned = store.resolve_tracker(
        SESSION, "anthropic", messages=continued, cache_ttl_seconds=TTL
    )

    assert returned is tracker  # the same conversation, not a rebuilt one
    assert _dump(returned.get_last_forwarded_messages()) == before
    assert returned.get_frozen_message_count() == frozen_before


def test_without_the_keep_alive_touch_the_tracker_expires_and_the_prefix_is_rebuilt(clock) -> None:
    """The cause the touch fixes: a warm cache does not keep the proxy's tracker alive."""
    store, tracker, messages = _session(clock, keep_alive_touch=False)

    clock.t = 8000
    returned = store.resolve_tracker(SESSION, "anthropic", messages=messages, cache_ttl_seconds=TTL)

    assert returned is not tracker
    assert returned.get_last_forwarded_messages() == []


def test_a_touch_extends_only_the_tracker_it_is_given(clock) -> None:
    store = SessionTrackerStore()
    messages = _conversation()
    mine = store.resolve_tracker("a", "anthropic", messages=messages, cache_ttl_seconds=TTL)
    other = store.resolve_tracker("b", "anthropic", messages=messages, cache_ttl_seconds=TTL)
    mine.update_from_response(0, 1, messages)
    other.update_from_response(0, 1, messages)
    clock.t += 3000
    mine.keep_alive_touch()
    clock.t += 1000  # 4000 s after the other one's last activity, 1000 s after this one's
    assert not mine.is_expired_for_cache_ttl(TTL)
    assert other.is_expired_for_cache_ttl(TTL)
