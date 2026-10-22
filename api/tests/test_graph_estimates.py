"""Graph diagnostics remain in the ledger without becoming dollar credit."""

import sys
import asyncio
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analytics  # noqa: E402
import billing  # noqa: E402


def event():
    return {
        "event_id": uuid4(),
        "user_id": uuid4(),
        "key_id": uuid4(),
        "runtime_id": uuid4(),
        "occurred_at": "2026-10-21T01:00:00Z",
        "request_id": "graph_example",
        "status": 200,
        "kind": "graph_estimate",
        "savings_usd": -0.001,
        "graph_tokens_avoided": -500,
        "graph_estimate": {
            "v": 1,
            "id": "a" * 64,
            "snapshot": "b" * 64,
            "project": "c" * 16,
            "baseline_tokens": 100,
            "actual_tokens": 600,
            "overhead_tokens": 1024,
            "tokenizer": "o200k_base",
            "basis": "bounded-literal-search-v1",
            "confidence": "client-measured-counterfactual",
            "rate_per_million": 2,
        },
    }


def test_signed_estimates_keep_their_basis_without_inference_counters():
    row = analytics.EventIn.model_validate(event())
    assert row.savings_usd == 0 and row.graph_tokens_avoided == -500
    assert (
        row.tokens_in
        == row.tokens_out
        == row.tokens_saved
        == row.tokens_before
        == row.tokens_after
        == 0
    )
    assert row.graph_estimate.confidence == "client-measured-counterfactual"
    assert row.cost_usd == 0


@pytest.mark.parametrize(
    "change",
    [
        {"baseline_tokens": 4001},
        {"overhead_tokens": 0},
        {"raw_source": "not allowed"},
        {"confidence": "verified"},
    ],
)
def test_unbounded_or_source_bearing_metadata_is_rejected(change):
    row = event()
    row["graph_estimate"].update(change)
    with pytest.raises(ValidationError):
        analytics.EventIn.model_validate(row)


def test_totals_exclude_graph_dollars_and_inference_counts():
    assert analytics.REQUEST == "COALESCE(data->>'kind','request') = 'request'"
    assert "AS graph_savings_usd" in analytics.TOTALS
    assert "AS graph_tokens_avoided" in analytics.TOTALS
    savings_line = next(line for line in analytics.TOTALS.splitlines() if " AS savings_usd" in line)
    assert "FILTER" in savings_line and "<> 'graph_estimate'" in savings_line


@pytest.mark.parametrize("dollars", [500, -500])
def test_legacy_graph_rows_cannot_change_fee_or_analytics_savings(dollars):
    # Execute the real shared aggregate and the real billing query. SQLite's
    # JSON operators/FILTER support the same portable SQL used by PostgreSQL.
    connection = sqlite3.connect(":memory:")
    connection.execute("ATTACH DATABASE ':memory:' AS metrics")
    connection.execute("CREATE TABLE metrics.proxy_events (user_id TEXT, occurred_at TEXT, data TEXT)")
    start, end = datetime(2026, 10, 1), datetime(2026, 11, 1)
    for kind, savings in [(None, 10), ("request", 15), ("keepalive", -1), ("graph_estimate", dollars)]:
        data = {"savings_usd": savings}
        if kind:
            data["kind"] = kind
        connection.execute("INSERT INTO metrics.proxy_events VALUES (?,?,?)", ("account", "2026-10-21", json.dumps(data)))
    # Other accounts and periods must not affect the fee either.
    connection.execute("INSERT INTO metrics.proxy_events VALUES (?,?,?)", ("other", "2026-10-21", '{"savings_usd":900}'))
    connection.execute("INSERT INTO metrics.proxy_events VALUES (?,?,?)", ("account", "2026-11-02", '{"savings_usd":900}'))

    class LedgerConnection:
        async def fetchval(self, sql, *args):
            return connection.execute(sql, args).fetchone()[0]

    try:
        total = asyncio.run(billing.ledger_savings("account", start, end, LedgerConnection()))
        assert total == Decimal("24")
        assert billing.savings_fee(total) == Decimal("1.20")
        analytical_total = connection.execute(
            f"SELECT {analytics.SAVINGS_USD} FROM metrics.proxy_events WHERE user_id=? AND occurred_at >= ? AND occurred_at < ?",
            ("account", start, end),
        ).fetchone()[0]
        assert analytical_total == 24
    finally:
        connection.close()


def test_old_proxy_graph_payloads_cannot_add_any_usage_or_dollar_totals():
    payload = event()
    payload.update(savings_usd=99, cost_usd=10, tokens_in=100, tokens_out=50,
                   tokens_before=100, tokens_after=50, tokens_saved=50,
                   cache_read=20, keepalive_usd=3, policy_usd={"flash":2}, response_cached=True)
    row = analytics.EventIn.model_validate(payload)
    assert row.savings_usd == row.cost_usd == row.keepalive_usd == 0
    assert row.tokens_in == row.tokens_out == row.tokens_saved == 0
    assert row.tokens_before == row.tokens_after == row.cache_read == 0
    assert not row.response_cached and not row.policy_usd
    assert row.graph_estimate.baseline_tokens == 100  # keep diagnostic evidence
