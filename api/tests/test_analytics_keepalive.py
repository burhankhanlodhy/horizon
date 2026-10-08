"""Keep-alive rows in the proxy event ledger (no Postgres: model and SQL shape only).

Run from the repo root with the API's requirements installed:
    python -m pytest api/tests
"""

import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analytics  # noqa: E402


def _event(**extra):
    return {
        "event_id": str(uuid4()),
        "user_id": str(uuid4()),
        "key_id": str(uuid4()),
        "runtime_id": str(uuid4()),
        "occurred_at": "2026-10-08T12:00:00+00:00",
        "request_id": "keepalive_abc",
        "status": 200,
        **extra,
    }


def test_rows_from_an_older_proxy_are_requests():
    e = analytics.EventIn.model_validate(_event())
    assert e.kind == "request" and e.keepalive_usd == 0


def test_a_ping_row_carries_its_cost_as_negative_savings():
    e = analytics.EventIn.model_validate(_event(kind="keepalive", savings_usd=-0.08, cost_usd=0.08))
    assert e.kind == "keepalive" and e.savings_usd == -0.08


@pytest.mark.parametrize("bad", [{"kind": "other"}, {"keepalive_usd": -1}])
def test_unknown_kinds_and_negative_credits_are_rejected(bad):
    with pytest.raises(ValidationError):
        analytics.EventIn.model_validate(_event(**bad))


def test_pings_never_count_as_requests():
    """Every count and average of user requests excludes ping rows."""
    for column in ("requests", "completed", "failed", "rate_limited", "latency_ms", "overhead_ms"):
        line = next(x for x in analytics.TOTALS.splitlines() if f"AS {column}" in x)
        assert analytics.REQUEST in line, column
    for column in ("keepalive_pings", "keepalive_spend_usd", "keepalive_avoided_usd"):
        assert f"AS {column}" in analytics.TOTALS
