"""Graph estimates flow through the durable account outbox, never as requests."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from horizon.proxy import account_analytics as accounts
from horizon.proxy import graph_savings as graph
from horizon.proxy.outcome import RequestOutcome


def receipt(**changes):
    return {
        "v": 1,
        "id": "a" * 64,
        "snapshot": "b" * 64,
        "project": "c" * 16,
        "baseline_tokens": 4000,
        "overhead_tokens": 1024,
        "tokenizer": "o200k_base",
        "basis": "bounded-literal-search-v1",
        **changes,
    }


def items(value=None, name="mcp__contextshrink_graph__graph_search"):
    text = json.dumps(
        {
            "data": {"results": [{"name": "example"}]},
            "contextshrink_graph_estimate": value or receipt(),
        }
    )
    return [
        {"type": "function_call", "call_id": "call-1", "name": name, "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call-1", "output": text},
    ]


def outcome(**changes):
    return RequestOutcome(
        request_id="request-1",
        provider="openai",
        model="priced-model",
        original_tokens=5000,
        optimized_tokens=4800,
        output_tokens=10,
        tokens_saved=200,
        attempted_input_tokens=5000,
        **changes,
    )


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setattr(
        graph,
        "get_model_pricing",
        lambda _: SimpleNamespace(input_cost_per_1m=2, cache_read_per_1m=0.1),
    )
    return accounts.AccountContext(
        str(uuid4()),
        str(uuid4()),
        SimpleNamespace(runtime_id=str(uuid4())),
        graph_estimates_supported=True,
    )


def test_only_paired_graph_tool_outputs_can_carry_receipts():
    text = items()[1]["output"]
    assert graph.extract([{"role": "user", "content": text}]) == {}
    assert graph.extract(items(name="exec_command")) == {}
    assert graph.extract(items()[1:]) == {}
    assert graph.extract(items())["a" * 64]["baseline_tokens"] == 4000


def test_code_mode_wrappers_and_mcp_envelopes_are_supported():
    raw = items()
    raw[0].update(
        type="custom_tool_call",
        name="exec",
        input="const r=await tools.mcp__contextshrink_graph__graph_search({query:'example'});text(r)",
    )
    raw[1].update(
        type="custom_tool_call_output",
        output=json.dumps({"content": [{"type": "text", "text": raw[1]["output"]}]}),
    )
    assert "a" * 64 in graph.extract(raw)
    raw[0]["input"] = "await tools.exec_command({cmd:'echo example'})"
    assert graph.extract(raw) == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"baseline_tokens": 4001},
        {"baseline_tokens": True},
        {"overhead_tokens": 0},
        {"id": "bad"},
        {"basis": "full-repo"},
    ],
)
def test_bad_or_unbounded_estimates_are_rejected(changes):
    assert graph.extract(items(receipt(**changes))) == {}


def test_retrieval_differences_are_diagnostics_without_dollar_credit(context):
    context.graph_estimates = graph.extract(items())
    event = graph.events(context, outcome())[0]
    avoided = event["graph_tokens_avoided"]
    assert 0 < avoided < 4000
    assert event["savings_usd"] == 0
    assert event["pricing_basis"] == "graph-estimate:retrieval-only:v1"
    assert "output" not in event["graph_estimate"] and "results" not in json.dumps(event)
    context.graph_estimates = graph.extract(items(receipt(baseline_tokens=10)))
    loss = graph.events(context, outcome())[0]
    assert loss["graph_tokens_avoided"] < 0 and loss["savings_usd"] == 0


def test_unknown_models_remain_unpriced_and_failures_never_credit(context, monkeypatch):
    context.graph_estimates = graph.extract(items())
    monkeypatch.setattr(graph, "get_model_pricing", lambda _: None)
    row = graph.events(context, outcome())[0]
    assert row["savings_usd"] == 0 and row["pricing_basis"].endswith("retrieval-only:v1")
    assert graph.events(context, outcome(status_code=429)) == []
    assert graph.events(context, outcome(from_response_cache=True)) == []


def test_receipt_ids_survive_replays_key_changes_and_proxy_restarts(context):
    context.graph_estimates = graph.extract(items())
    first = graph.events(context, outcome())[0]["event_id"]
    context.key_id, context.service.runtime_id = str(uuid4()), str(uuid4())
    assert graph.events(context, outcome())[0]["event_id"] == first
    context.user_id = str(uuid4())
    assert graph.events(context, outcome())[0]["event_id"] != first


def test_older_control_plane_never_receives_new_event_kind(context):
    context.graph_estimates = graph.extract(items())
    context.graph_estimates_supported = False
    assert graph.events(context, outcome()) == []


def test_successful_consumption_uses_existing_idempotent_durable_outbox(tmp_path, monkeypatch):
    service = accounts.AccountAnalytics()
    service.path = tmp_path / "outbox.sqlite3"
    with service._db() as conn:
        conn.execute("CREATE TABLE outbox(event_id TEXT PRIMARY KEY,payload TEXT NOT NULL)")
    context = accounts.AccountContext(
        str(uuid4()), str(uuid4()), service, graph_estimates_supported=True
    )
    monkeypatch.setattr(
        graph,
        "get_model_pricing",
        lambda _: SimpleNamespace(input_cost_per_1m=2, cache_read_per_1m=0.1),
    )
    token = accounts._account.set(context)
    try:
        accounts.capture_graph_estimates(items())
        asyncio.run(accounts.record_account_outcome(outcome(), saved=200))
        asyncio.run(
            accounts.record_account_outcome(replace(outcome(), request_id="request-2"), saved=200)
        )
    finally:
        accounts._account.reset(token)
    rows = [json.loads(payload) for _, payload in service._batch()]
    assert len(rows) == 3
    estimates = [r for r in rows if r.get("kind") == "graph_estimate"]
    assert len(estimates) == 1
    assert not any(
        estimates[0].get(k)
        for k in ("tokens_in", "tokens_out", "tokens_saved", "tokens_before", "cost_usd")
    )


def test_websocket_capture_precedes_optional_traffic_learner(context):
    from horizon.proxy.handlers.openai import OpenAIHandlerMixin

    token = accounts._account.set(context)
    try:
        asyncio.run(
            OpenAIHandlerMixin._observe_openai_ws_response_create(
                SimpleNamespace(traffic_learner=None),
                {"input": items()},
                seen_call_ids=set(),
                baseline=True,
                request_id="ws",
            )
        )
        assert context.graph_estimates
    finally:
        accounts._account.reset(token)
