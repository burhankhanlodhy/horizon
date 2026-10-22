"""Consume the real Node-generated receipts through the Python ledger path locally."""

import asyncio
import argparse
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))
import analytics  # noqa: E402

from horizon.proxy import account_analytics as accounts  # noqa: E402
from horizon.proxy.outcome import RequestOutcome  # noqa: E402


async def main(output=None):
    samples = json.loads(
        (ROOT / "desktop/dist-windows/npx-test/graph-ledger-proof.json").read_text()
    )
    with TemporaryDirectory(prefix="graph-ledger-proof-") as directory:
        service = accounts.AccountAnalytics()
        service.path = Path(directory) / "outbox.sqlite3"
        with service._db() as conn:
            conn.execute("CREATE TABLE outbox(event_id TEXT PRIMARY KEY,payload TEXT NOT NULL)")
        context = accounts.AccountContext(
            str(uuid4()), str(uuid4()), service, graph_estimates_supported=True
        )
        token = accounts._account.set(context)
        try:
            for replay in range(2):
                inputs = []
                for index, sample in enumerate(samples):
                    inputs += [
                        sample["call"],
                        {
                            "type": "function_call_output",
                            "call_id": f"graph-{index}",
                            "output": sample["text"],
                        },
                    ]
                accounts.capture_graph_estimates(inputs)
                await accounts.record_account_outcome(
                    RequestOutcome(
                        request_id=f"proof-{replay}",
                        provider="openai",
                        model="gpt-6.1-sol",
                        original_tokens=5000,
                        optimized_tokens=5000,
                        output_tokens=10,
                        tokens_saved=0,
                        attempted_input_tokens=0,
                    )
                )
        finally:
            accounts._account.reset(token)
        rows = [json.loads(payload) for _, payload in service._batch()]
        graphs = [row for row in rows if row.get("kind") == "graph_estimate"]
        assert len(graphs) == 3 and len(rows) == 5, "Replay must not credit graph receipts twice"
        for row in rows:
            analytics.EventIn.model_validate(row)
        for row, sample in zip(graphs, samples, strict=True):
            assert row["savings_usd"] == 0, "Retrieval diagnostics cannot claim session savings"
            assert (
                row["graph_estimate"]["actual_tokens"]
                == sample["outputTokens"] + sample["invocationTokens"] + 1024
            )
        report = {
            "scope": "local end-to-end, real Node receipts -> proxy tokenizer -> SQLite outbox -> API schema",
            "production_account_modified": False,
            "inference_rows": len(rows) - len(graphs),
            "graph_rows_after_two_replays": len(graphs),
            "estimated_tokens_avoided": sum(row["graph_tokens_avoided"] for row in graphs),
            "estimated_savings_usd": sum(row["savings_usd"] for row in graphs),
            "pricing_basis": sorted({row["pricing_basis"] for row in graphs}),
            "tokenizers_agree": True,
        }
        (Path(output) if output else Path(__file__).parent / "ledger-result.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", help="Keep an earlier experiment report unchanged")
    asyncio.run(main(parser.parse_args().output))
