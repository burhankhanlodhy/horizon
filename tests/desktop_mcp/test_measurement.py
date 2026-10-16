from __future__ import annotations

import json

from horizon.desktop_mcp.measurement import (
    LocalUsageLedger,
    TokenEstimator,
    finalize_accounting,
    measure_payload,
)
from horizon.desktop_mcp.model_view import model_view


class _CharacterEstimator:
    def count_text(self, text: str) -> int:
        return len(text)

    def provenance(self) -> dict[str, object]:
        return {
            "count_status": "test_estimate",
            "tokenizer_name": "test_chars",
            "counter_version": "test-v1",
            "counting_model_assumption": None,
            "fallback": False,
        }


def test_token_estimator_uses_only_cached_vocab_and_falls_back_without_one(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    estimator = TokenEstimator(counting_model="claude-sonnet-test")

    assert estimator.count_text("abc") >= 1
    provenance = estimator.provenance()
    assert provenance["counting_model_assumption"] == "claude-sonnet-test"
    assert provenance["count_status"] == "heuristic_estimate_not_claude_count"
    assert provenance["tokenizer_name"] == "utf8_bytes_div_3_heuristic"


def test_token_estimator_digest_cache_is_bounded_and_never_retains_text(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    estimator = TokenEstimator()
    samples = [f"unique synthetic payload {index}" for index in range(140)]
    first_count = estimator.count_text(samples[-1])
    assert estimator.count_text(samples[-1]) == first_count
    for sample in samples:
        estimator.count_text(sample)

    assert len(estimator._count_cache) == 128
    assert all(isinstance(key[2], bytes) and len(key[2]) == 32 for key in estimator._count_cache)
    assert all(isinstance(value, int) for value in estimator._count_cache.values())
    cache_repr = repr(estimator._count_cache)
    assert all(sample not in cache_repr for sample in samples)


def test_measure_payload_counts_complete_inner_json_and_labels_unknown_host_overhead() -> None:
    result = measure_payload({"text": "hello", "mode": "exact"}, _CharacterEstimator())
    encoded = '{"mode":"exact","text":"hello"}'

    assert result["utf8_bytes"] == len(encoded.encode("utf-8"))
    assert result["token_estimate"] == len(encoded)
    assert result["measured_stage"] == "inner_mcp_result_json_text"
    assert result["sdk_wrapping"] == "excluded"
    assert result["host_overhead"] == "unknown"


def test_finalize_accounting_separates_source_reduction_from_complete_result() -> None:
    estimator = _CharacterEstimator()
    delivered = {"source_id": "opaque", "version": "v1", "mode": "compact", "text": "short"}
    raw = {
        "source_id": "opaque",
        "version": "v1",
        "mode": "exact",
        "text": "a much longer exact source",
    }
    finalized = finalize_accounting(
        delivered,
        raw,
        "a much longer exact source",
        "short",
        estimator=estimator,  # type: ignore[arg-type]
    )
    accounting = finalized["accounting"]

    assert accounting["source"]["byte_delta"] > 0
    assert accounting["result"]["byte_delta"] > 0
    assert (
        accounting["result"]["raw_equivalent_token_estimate"]
        > accounting["result"]["delivered_token_estimate"]
    )
    assert accounting["measured_stage"] == "inner_mcp_result_json_text"
    assert accounting["host_overhead"] == "unknown"
    assert accounting["retrieval"]["call_count"] == 0
    assert "accounting" not in raw
    # Delivered figures describe the model view, which carries no accounting block.
    assert "accounting" not in model_view(finalized)
    serialized = json.dumps(
        model_view(finalized), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    assert len(serialized.encode("utf-8")) == accounting["result"]["delivered_utf8_bytes"]
    assert estimator.count_text(serialized) == accounting["result"]["delivered_token_estimate"]
    json.dumps(finalized)


def test_usage_ledger_is_bounded_signed_metadata_only_and_aggregates_without_usd() -> None:
    ledger = LocalUsageLedger(max_events=1, clock=lambda: 12.5)
    accounting = {
        "count_basis": "estimate_not_claude_count",
        "counter_version": "tiktoken-0.14.0",
        "tokenizer_name": "o200k_base",
        "fallback": False,
        "source": {
            "raw_utf8_bytes": 100,
            "rendered_utf8_bytes": 50,
            "raw_token_estimate": 30,
            "rendered_token_estimate": 15,
        },
        "result": {
            "raw_equivalent_utf8_bytes": 220,
            "delivered_utf8_bytes": 170,
            "raw_equivalent_token_estimate": 70,
            "delivered_token_estimate": 55,
        },
    }
    first = ledger.record(
        tool="contextshrink_open",
        mode="focused",
        source_version="C:/private/secret.txt@v1",
        policy_version="phase-c-v1",
        accounting=accounting,
        latency_ms=5,
    )
    assert ledger.verify(first)
    assert "C:/private" not in json.dumps(first)
    assert "secret.txt" not in json.dumps(first)
    assert "local_hmac_sha256" in first

    ledger.record(
        tool="contextshrink_retrieve",
        mode="exact",
        source_version=None,
        status="error",
        error_code="result_too_large",
    )
    stats = ledger.stats()
    assert stats["event_count"] == 1
    assert stats["error_count"] == 1
    assert stats["billing_attribution"] == "not_measured"
    assert stats["signature_scope"] == "ephemeral_local_integrity_only"
    assert not any("usd" in key.casefold() for key in stats)


def test_usage_ledger_rejects_tampered_event() -> None:
    ledger = LocalUsageLedger()
    event = ledger.record(tool="contextshrink_open", mode="exact", source_version="v1")
    event["status"] = "error"
    assert not ledger.verify(event)


def test_stats_count_only_retained_successful_compact_transformations() -> None:
    ledger = LocalUsageLedger(max_events=10)
    ledger.record(
        tool="contextshrink_open",
        mode="compact",
        source_version="version-1",
        status="success",
        compression_applied=True,
    )
    ledger.record(
        tool="contextshrink_open",
        mode="compact",
        source_version="version-2",
        status="fallback",
        compression_applied=False,
    )
    ledger.record(
        tool="contextshrink_open",
        mode="exact",
        source_version="version-3",
        status="success",
        compression_applied=True,
    )
    stats = ledger.stats()
    assert stats["retained_successful_compact_result_count"] == 1
    assert stats["retained_successful_compact_result_scope"].startswith(
        "retained successful compact"
    )


def test_ledger_keeps_versioned_counter_labels_but_not_unsafe_text() -> None:
    from horizon.desktop_mcp.measurement import LocalUsageLedger

    ledger = LocalUsageLedger()
    kept = ledger.record(
        tool="contextshrink_open",
        mode="exact",
        source_version=None,
        accounting={"counter_version": "tiktoken-0.14.0", "tokenizer_name": "o200k_base"},
    )
    assert kept["counter_version"] == "tiktoken-0.14.0"
    dropped = ledger.record(
        tool="contextshrink_open",
        mode="exact",
        source_version=None,
        accounting={"counter_version": "C:/Users/x path", "tokenizer_name": "a b"},
    )
    assert dropped["counter_version"] is None
    assert dropped["tokenizer_name"] is None
