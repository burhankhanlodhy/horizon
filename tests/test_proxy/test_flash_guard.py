"""Flash safety net: a rejected flashed request is retried unflashed once."""

from __future__ import annotations

import pytest

from horizon.proxy import flash_guard


@pytest.fixture(autouse=True)
def _clean():
    flash_guard.reset_for_tests()
    yield
    flash_guard.reset_for_tests()


def test_not_armed_means_no_retry() -> None:
    assert flash_guard.fallback_body({"messages": []}, 400) is None


def test_retry_once_then_switch_off_on_success() -> None:
    flash_guard.arm("messages", ["raw"], host="api.example", model="m1")
    assert flash_guard.fallback_body({"messages": ["stub"]}, 429) is None  # still armed
    retry = flash_guard.fallback_body({"model": "m1", "messages": ["stub"]}, 400)
    assert retry == {"model": "m1", "messages": ["raw"]}
    assert flash_guard.fallback_body(retry, 400) is None  # one retry per request
    flash_guard.record_retry(200)
    assert flash_guard.is_disabled("API.example", "M1")
    stats = flash_guard.stats()
    assert stats["rejected_then_retried"] == 1
    assert stats["switched_off"][0]["host"] == "api.example"


def test_accepted_request_disarms_before_continuations() -> None:
    flash_guard.arm("messages", ["raw"], host="h", model="m")
    assert flash_guard.fallback_body({"messages": ["stub"]}, 200) is None
    # A CCR continuation in the same request is rejected: its own messages stay.
    assert flash_guard.fallback_body({"messages": ["stub", "a", "t"]}, 400) is None
    assert not flash_guard.is_disabled("h", "m")


def test_failed_retry_does_not_blame_flash() -> None:
    flash_guard.arm("input", ["raw"], host="h", model="m")
    assert flash_guard.fallback_body({"input": ["stub"]}, 400) is not None
    flash_guard.record_retry(400)
    assert not flash_guard.is_disabled("h", "m")


def test_counters_and_pauses() -> None:
    flash_guard.record_flash(shown_once=1, stubbed=3, chars_kept_out=4000)
    flash_guard.record_rerun_pause("conv")
    flash_guard.record_rerun_pause("conv")  # once per conversation
    flash_guard.record_price_skip("deepseek-v4-pro", "cheap reads")
    stats = flash_guard.stats()
    assert stats["outputs_stubbed"] == 3 and stats["tokens_kept_out"] == 1000
    assert stats["rerun_pauses"] == 1
    assert "deepseek-v4-pro" in stats["price_skips"]
