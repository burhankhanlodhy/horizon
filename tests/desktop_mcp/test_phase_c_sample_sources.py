from __future__ import annotations

from pathlib import Path

from horizon.desktop_mcp.local_backend import LocalBackend
from horizon.desktop_mcp.source_access import SourceReader

SAMPLE_ROOT = Path(__file__).resolve().parents[2] / "desktop" / "mcp" / "sample-sources"
TOKEN_BUDGET = 2000


def _assert_within_budget(payload: dict[str, object]) -> None:
    accounting = payload["accounting"]
    assert isinstance(accounting, dict)
    result = accounting["result"]
    assert isinstance(result, dict)
    assert result["delivered_token_estimate"] <= TOKEN_BUDGET
    assert result["delivered_utf8_bytes"] <= 65536


def test_phase_c_sample_compact_and_focused_results_fit_default_budget() -> None:
    backend = LocalBackend(SourceReader((SAMPLE_ROOT,)), result_token_budget=TOKEN_BUDGET)

    repeated = backend.open(str(SAMPLE_ROOT / "phase-c-repeated.log"), mode="compact")
    assert repeated is not None
    _assert_within_budget(repeated)
    assert repeated["compression_applied"] is True
    assert repeated["fidelity"] == "reversible_fold"
    assert repeated["text"].count("worker=alpha status=ready event=periodic-heartbeat") == 1
    assert "(repeated 48 times)" in repeated["text"]

    focused = backend.open(
        str(SAMPLE_ROOT / "phase-c-constraints.md"),
        mode="focused",
        query="retention legal hold 7 years",
    )
    assert focused is not None
    _assert_within_budget(focused)
    assert focused["fidelity"] == "partial_exact_excerpt"
    assert "partial" in focused["coverage"]
    assert "exactly 7 years" in focused["text"]
    assert "must not be deleted" in focused["text"]
    recovery_line = next(
        index
        for index, line in enumerate(
            (SAMPLE_ROOT / "phase-c-constraints.md").read_text(encoding="utf-8").splitlines(),
            start=1,
        )
        if "within 90 minutes" in line
    )
    assert "Escalation: notify the on-call desk within 90 minutes." not in focused["text"]
    recovered = backend.retrieve(
        focused["source_id"],
        {"type": "lines", "start": recovery_line, "end": recovery_line},
    )
    _assert_within_budget(recovered)
    assert recovered["fidelity"] == "byte_exact"
    assert "within 90 minutes" in recovered["text"]


def test_phase_c_structured_samples_show_normalized_json_and_exact_selectors() -> None:
    backend = LocalBackend(SourceReader((SAMPLE_ROOT,)), result_token_budget=TOKEN_BUDGET)
    compact = backend.open(str(SAMPLE_ROOT / "phase-c-structured.json"), mode="compact")
    assert compact is not None
    _assert_within_budget(compact)
    assert compact["compression_applied"] is True
    assert compact["fidelity"] == "normalized_json_whitespace"

    json_value = backend.retrieve(
        compact["source_id"],
        {"type": "json_pointer", "pointer": "/records/0/legal_hold/delete_allowed"},
    )
    _assert_within_budget(json_value)
    assert json_value["fidelity"] == "byte_exact"
    assert json_value["text"] == "false"

    table = backend.open(str(SAMPLE_ROOT / "phase-c-table.csv"), mode="exact")
    assert table is not None
    rows = backend.retrieve(table["source_id"], {"type": "rows", "start": 1, "end": 2})
    _assert_within_budget(rows)
    assert rows["text"].splitlines() == [
        "record_id,retention_years,legal_hold,delete_allowed",
        "SYN-001,7,true,false",
    ]


def test_local_stats_report_retained_successful_compact_transformations_only() -> None:
    backend = LocalBackend(SourceReader((SAMPLE_ROOT,)), result_token_budget=TOKEN_BUDGET)
    before = backend.stats()
    assert before["compression_applied"] is False
    assert before["usage"]["retained_successful_compact_result_count"] == 0

    compact = backend.open(str(SAMPLE_ROOT / "phase-c-repeated.log"), mode="compact")
    assert compact is not None and compact["compression_applied"] is True
    after = backend.stats()
    assert after["compression_applied"] is True
    assert after["compression_enabled"] is True
    assert "compact" in after["supported_modes"]
    assert after["usage"]["retained_successful_compact_result_count"] == 1
    assert after["usage"]["retained_successful_compact_result_scope"].startswith(
        "retained successful compact"
    )
    assert after["token_count_status"] == backend.estimator.provenance()["count_status"]

    raw = LocalBackend(
        SourceReader((SAMPLE_ROOT,)),
        result_token_budget=TOKEN_BUDGET,
        eval_arm="raw",
    )
    raw_result = raw.open(str(SAMPLE_ROOT / "phase-c-repeated.log"), mode="compact")
    assert raw_result is not None and raw_result["compression_applied"] is False
    raw_stats = raw.stats()
    assert raw_stats["compression_applied"] is False
    assert raw_stats["usage"]["retained_successful_compact_result_count"] == 0
