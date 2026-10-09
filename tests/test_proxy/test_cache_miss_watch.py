"""Predicted client-caused cache misses, priced per cause."""

from __future__ import annotations

import copy

import pytest

from horizon.proxy.cache_miss_watch import CacheMissWatch, session_key


def _body(**extra):
    body = {
        "model": "claude-opus-5-5",
        "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
        "system": [{"type": "text", "text": "You are an agent." * 200}],
        "output_config": {"effort": "xhigh"},
        "thinking": {"type": "adaptive"},
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "fix the bug"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "reading" * 500}]},
            {"role": "user", "content": [{"type": "text", "text": "ok"}]},
        ],
    }
    body.update(extra)
    return body


def _next_turn(body):
    nxt = copy.deepcopy(body)
    nxt["messages"] += [
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        {"role": "user", "content": [{"type": "text", "text": "thanks"}]},
    ]
    return nxt


def test_appending_turns_is_not_a_miss() -> None:
    watch = CacheMissWatch()
    body = _body()
    assert watch.observe("s", body) is None
    for _ in range(5):
        body = _next_turn(body)
        assert watch.observe("s", body) is None
    assert watch.stats()["predicted_misses"] == 0


def test_moving_cache_markers_is_not_a_miss() -> None:
    watch = CacheMissWatch()
    body = _body()
    watch.observe("s", body)
    nxt = _next_turn(body)
    nxt["messages"][0]["content"][0]["cache_control"] = {"type": "ephemeral"}
    assert watch.observe("s", nxt) is None


@pytest.mark.parametrize(
    ("change", "cause", "scope"),
    [
        ({"output_config": {"effort": "low"}}, "effort", "messages"),
        ({"thinking": {"type": "enabled", "budget_tokens": 2048}}, "thinking", "messages"),
        ({"tool_choice": {"type": "any"}}, "tool_choice", "messages"),
        ({"speed": "fast"}, "speed", "system+messages"),
        ({"system": [{"type": "text", "text": "git status: dirty"}]}, "system", "system+messages"),
        ({"model": "claude-sonnet-5-5"}, "model", "all"),
        ({"tools": [{"name": "Grep", "input_schema": {"type": "object"}}]}, "tools", "all"),
    ],
)
def test_each_documented_invalidator_is_reported(change, cause, scope) -> None:
    watch = CacheMissWatch()
    body = _body()
    watch.observe("s", body)
    nxt = _next_turn(body)
    nxt.update(change)
    miss = watch.observe("s", nxt)
    assert miss is not None
    assert miss.causes == [cause]
    assert miss.scope == scope
    assert miss.tokens > 0 and miss.usd > 0


def test_a_rewritten_earlier_message_is_a_history_miss() -> None:
    watch = CacheMissWatch()
    body = _next_turn(_body())
    watch.observe("s", body)
    nxt = _next_turn(body)
    nxt["messages"][1]["content"][0]["text"] = "compacted summary"
    miss = watch.observe("s", nxt)
    assert miss is not None
    assert miss.causes == ["history"]
    assert miss.changed_from_message == 1


def test_client_compaction_that_shrinks_history_is_a_miss() -> None:
    watch = CacheMissWatch()
    body = _next_turn(_next_turn(_body()))
    watch.observe("s", body)
    compacted = _body()
    compacted["messages"] = [{"role": "user", "content": [{"type": "text", "text": "fix the bug"}]}]
    miss = watch.observe("s", compacted)
    assert miss is not None and "history" in miss.causes


def test_a_bigger_scope_prices_more_tokens() -> None:
    watch = CacheMissWatch()
    body = _body()
    watch.observe("a", body)
    watch.observe("b", body)
    effort = _next_turn(body)
    effort["output_config"] = {"effort": "low"}
    model = _next_turn(body)
    model["model"] = "claude-opus-5"
    small = watch.observe("a", effort)
    big = watch.observe("b", model)
    assert big.tokens > small.tokens


def test_sessions_are_separate_and_bounded() -> None:
    watch = CacheMissWatch(max_sessions=2)
    watch.observe("a", _body())
    watch.observe("b", _body(model="claude-sonnet-5-5"))
    watch.observe("c", _body())
    # "a" was evicted, so a different model there is a first sight, not a miss.
    assert watch.observe("a", _body(model="claude-haiku-5-5")) is None


def test_stats_count_each_miss_once_and_attribute_each_cause() -> None:
    watch = CacheMissWatch()
    body = _body()
    watch.observe("s", body)
    nxt = _next_turn(body)
    nxt["output_config"] = {"effort": "low"}
    nxt["thinking"] = {"type": "disabled"}
    watch.observe("s", nxt)
    stats = watch.stats()
    assert stats["predicted_misses"] == 1
    assert set(stats["by_cause"]) == {"effort", "thinking"}
    assert stats["estimated"] is True


def test_session_key_survives_model_and_setting_changes() -> None:
    a = session_key(_body())
    b = session_key(_body(model="claude-sonnet-5-5", output_config={"effort": "low"}))
    assert a == b
    other = _body()
    other["messages"][0]["content"][0]["text"] = "a different conversation"
    assert session_key(other) != a
    assert session_key(_body(), {"x-horizon-keepalive-id": "launch-1"}) != a


def test_never_raises_on_odd_bodies() -> None:
    watch = CacheMissWatch()
    assert watch.observe("s", {"messages": "not a list"}) is None
    assert watch.observe("", _body()) is None
