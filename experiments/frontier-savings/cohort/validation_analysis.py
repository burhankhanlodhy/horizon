"""Reprice recorded usage; distinguish official list-price models from CLI quotes."""

from __future__ import annotations

import json
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
RATES = {"input": 2.0, "write_5m": 2.5, "write_1h": 4.0, "read": 0.1, "output": 10.0}


def cost(usage):
    split = usage.get("cache_creation", {})
    five = split.get("ephemeral_5m_input_tokens", 0)
    hour = split.get("ephemeral_1h_input_tokens", 0)
    assert five + hour == usage.get("cache_creation_input_tokens", 0), "unknown write TTL"
    assert usage.get("service_tier") == "standard"
    assert usage.get("inference_geo") == "global"
    return (
        usage["input_tokens"] * RATES["input"]
        + five * RATES["write_5m"]
        + hour * RATES["write_1h"]
        + usage.get("cache_read_input_tokens", 0) * RATES["read"]
        + usage["output_tokens"] * RATES["output"]
    ) / 1_000_000


def main():
    ledger = json.loads((HERE / "validation_results.json").read_text())
    table = []
    for count in (4, 12):
        means = {}
        cold = {}
        for arm in ("native", "script", "compact"):
            rows = [r for r in ledger["rows"] if r["arm"] == arm and r["clone_count"] == count]
            prices = [sum(cost(a["usage"]) for a in r["provider_attempts"]) for r in rows]
            means[arm] = statistics.mean(prices)
            # Sensitivity only: convert the first request's observed warm read
            # into a cold 1h write. Does not rerun agents or certify a cold trial.
            cold[arm] = statistics.mean(
                p
                + r["provider_attempts"][0]["usage"].get("cache_read_input_tokens", 0)
                * (RATES["write_1h"] - RATES["read"])
                / 1_000_000
                for p, r in zip(prices, rows, strict=True)
            )
            table.append(
                {
                    "clone_count": count,
                    "arm": arm,
                    "repetitions": len(rows),
                    "all_passed": all(r["passed"] for r in rows),
                    "mean_modeled_usd": means[arm],
                    "cold_sensitivity_usd": cold[arm],
                    "mean_seconds": statistics.mean(r["seconds"] for r in rows),
                }
            )
        table.append(
            {
                "clone_count": count,
                "compact_cost_increase_vs_native_pct": (means["compact"] / means["native"] - 1)
                * 100,
                "compact_cost_increase_vs_script_pct": (means["compact"] / means["script"] - 1)
                * 100,
                "cold_sensitivity_saving_vs_script_pct": (1 - cold["compact"] / cold["script"])
                * 100,
                "cold_sensitivity_saving_vs_script_usd": cold["script"] - cold["compact"],
            }
        )
    result = {
        "rates_per_million_usd": RATES,
        "price_source": SOURCE,
        "price_verified_date": "2026-10-09",
        "modeled_list_price_usd": sum(
            cost(a["usage"]) for r in ledger["rows"] for a in r["provider_attempts"]
        ),
        "cli_api_equivalent_quote_usd": sum(r["client_api_equivalent_usd"] for r in ledger["rows"]),
        "conservative_reserved_upper_usd": ledger["shared_upper_usd"],
        "budget_usd": ledger["budget_usd"],
        "actual_invoice_reconciled": False,
        "production_qualified": False,
        "table": table,
        "workflows": len(ledger["rows"]),
        "all_workflows_passed": all(r["passed"] for r in ledger["rows"]),
        "provider_attempts": sum(len(r["provider_attempts"]) for r in ledger["rows"]),
    }
    (HERE / "validation_analysis.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
