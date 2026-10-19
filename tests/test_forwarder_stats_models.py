"""A session's model is the one that carried its tokens; it is unpriced only when nothing priced."""

from __future__ import annotations

import time
from pathlib import Path

from horizon.forwarder_stats import ClaudeCodeStats


def _record(stats: ClaudeCodeStats, **kw) -> None:
    base = {
        "session_id": "s",
        "model": "",
        "tokens_before": 100,
        "tokens_after": 50,
        "tokens_saved": 50,
        "list_usd": 0.0,
        "unpriced": False,
        "priced": False,
        "verified_usd": None,
        "cache_written": False,
        "keepalive": False,
        "now": time.time(),
    }
    base.update(kw)
    stats.record(**base)


def _session(stats: ClaudeCodeStats) -> dict:
    return stats.snapshot(now=time.time(), forwarder={"pid": 1, "version": "t", "port": 1})[
        "sessions"
    ]["s"]


def test_the_session_model_is_the_one_with_the_most_tokens(tmp_path: Path) -> None:
    stats = ClaudeCodeStats(tmp_path / "claude-code")
    _record(stats, model="claude-opus-5-5", tokens_before=50_000, priced=True, list_usd=1.0)
    _record(stats, model="claude-haiku-5-5", tokens_before=900, priced=True, list_usd=0.01)
    _record(stats, model="claude-haiku-5-5", tokens_before=900, priced=True, list_usd=0.01)
    assert _session(stats)["model"] == "claude-opus-5-5"  # the helper's calls do not win
    stats.close()


def test_a_helper_model_that_grows_past_the_main_one_takes_over(tmp_path: Path) -> None:
    stats = ClaudeCodeStats(tmp_path / "claude-code")
    _record(stats, model="claude-opus-5-5", tokens_before=1_000)
    _record(stats, model="claude-haiku-5-5", tokens_before=5_000)
    assert _session(stats)["model"] == "claude-haiku-5-5"
    stats.close()


def test_a_session_with_one_priced_request_is_not_unpriced(tmp_path: Path) -> None:
    stats = ClaudeCodeStats(tmp_path / "claude-code")
    _record(stats, model="no-such-model-xyz", unpriced=True)
    _record(stats, model="claude-opus-5-5", priced=True, list_usd=0.4)
    session = _session(stats)
    assert session["unpriced"] is False
    assert session["trimmed_list_usd"] == 0.4  # the priced request keeps its dollars
    stats.close()


def test_a_session_where_nothing_priced_is_unpriced(tmp_path: Path) -> None:
    stats = ClaudeCodeStats(tmp_path / "claude-code")
    _record(stats, model="no-such-model-xyz", unpriced=True)
    _record(stats, model="no-such-model-xyz", unpriced=True)
    session = _session(stats)
    assert session["unpriced"] is True
    assert session["trimmed_list_usd"] == 0
    stats.close()


def test_the_model_tally_survives_a_restart(tmp_path: Path) -> None:
    first = ClaudeCodeStats(tmp_path / "claude-code")
    _record(first, model="claude-opus-5-5", tokens_before=5_000, priced=True, list_usd=0.2)
    first.flush(forwarder={"pid": 1, "version": "t", "port": 1})
    first.close()

    second = ClaudeCodeStats(tmp_path / "claude-code")
    _record(second, model="claude-haiku-5-5", tokens_before=100, priced=True, list_usd=0.01)
    session = _session(second)
    second.close()
    assert session["model"] == "claude-opus-5-5"
    assert session["model_tokens"]["claude-opus-5-5"] == 5_000
