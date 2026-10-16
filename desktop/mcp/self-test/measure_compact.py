"""Measure what compact open delivers for each document in a folder.

    python desktop/mcp/self-test/measure_compact.py --documents DIR

For every regular file in DIR (sorted by name) this opens the file through the
real ``LocalBackend`` in compact mode, with the local profile's default budgets
unless --result-token-budget is given,
and prints one row: raw bytes and tokens, compact bytes and tokens, the tokens of
the model view that Claude receives, the transform applied, the stated
``compression_ratio`` and whether the compact result covers the whole file.

Token counts are o200k estimates from the local vocabulary cache. When that cache
is missing the byte heuristic is used and the header says so. Nothing is uploaded.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from horizon.desktop_mcp.local_backend import LocalBackend, LocalBackendError  # noqa: E402
from horizon.desktop_mcp.measurement import TokenEstimator, measure_payload  # noqa: E402
from horizon.desktop_mcp.model_view import model_view  # noqa: E402
from horizon.desktop_mcp.settings import DesktopSettings  # noqa: E402
from horizon.desktop_mcp.snapshots import SnapshotError  # noqa: E402
from horizon.desktop_mcp.source_access import SourceAccessError, SourceReader  # noqa: E402

# Column name (also the row key) and print width, in print order.
COLUMNS = (
    ("document", 26),
    ("raw_bytes", 10),
    ("compact_bytes", 13),
    ("raw_tokens", 10),
    ("compact_tokens", 14),
    ("view_tokens", 11),
    ("ratio", 6),
    ("transform", 22),
    ("complete", 8),
)


def measure_document(
    backend: LocalBackend, path: Path, estimator: TokenEstimator
) -> dict[str, Any]:
    """Open one file in compact mode and return its measured row."""
    try:
        payload = backend.open(str(path), mode="compact")
    except (LocalBackendError, SourceAccessError, SnapshotError) as exc:
        return {"document": path.name, "error": getattr(exc, "code", type(exc).__name__)}
    if payload is None:
        return {"document": path.name, "error": "no_result"}
    source = payload.get("accounting", {}).get("source", {})
    delivered = measure_payload(model_view(payload), estimator)
    return {
        "document": path.name,
        "raw_bytes": source["raw_utf8_bytes"],
        "compact_bytes": source["rendered_utf8_bytes"],
        "raw_tokens": source["raw_token_estimate"],
        "compact_tokens": source["rendered_token_estimate"],
        "view_tokens": delivered["token_estimate"],
        "ratio": payload.get("compression_ratio"),
        "transform": payload.get("compression_transform") or "none (exact)",
        "complete": "yes" if payload.get("more_available") is not True else "no",
    }


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def format_table(rows: list[dict[str, Any]]) -> str:
    """Fixed-width table; error rows show their sanitized code in the transform column."""
    lines = ["  ".join(name.ljust(width) for name, width in COLUMNS)]
    for row in rows:
        if "error" in row:
            cells = [row["document"], *["-"] * 6, f"error: {row['error']}", "-"]
        else:
            cells = [_cell(row[name]) for name, _ in COLUMNS]
        padded = (cell[:width].ljust(width) for cell, (_, width) in zip(cells, COLUMNS))
        lines.append("  ".join(padded))
    return "\n".join(lines)


def measure(
    documents: Path, result_token_budget: int | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Measure every regular file in ``documents``; return the header and the rows.

    ``result_token_budget`` defaults to the product default; a larger value (up to
    the contract maximum of 64000) shows whole-file numbers for large documents.
    """
    settings = DesktopSettings(profile="local", approved_roots=(documents,))
    backend = LocalBackend(
        SourceReader((documents,)),
        result_token_budget=result_token_budget or settings.result_token_budget,
        max_result_bytes=settings.max_result_bytes,
        counting_model=settings.counting_model,
        min_compaction_gain_tokens=settings.min_compaction_gain_tokens,
        min_compaction_gain_percent=settings.min_compaction_gain_percent,
    )
    estimator = TokenEstimator(settings.counting_model)
    provenance = estimator.provenance()
    if provenance.get("fallback"):
        header = "tokens: byte heuristic (o200k cache unavailable; not comparable)"
    else:
        header = f"tokens: o200k estimate ({provenance.get('tokenizer_name')})"
    files = sorted(path for path in documents.iterdir() if path.is_file())
    rows = [measure_document(backend, path, estimator) for path in files]
    return header, rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--documents", required=True, type=Path, help="folder of documents to measure"
    )
    parser.add_argument(
        "--result-token-budget",
        type=int,
        default=None,
        help="result token budget (default: the product default, 32000; maximum 64000)",
    )
    args = parser.parse_args(argv)
    if args.result_token_budget is not None and not 128 <= args.result_token_budget <= 64000:
        parser.error("--result-token-budget must be between 128 and 64000")
    documents = args.documents.resolve()
    if not documents.is_dir():
        print(f"not a folder: {args.documents}", file=sys.stderr)
        return 2
    header, rows = measure(documents, args.result_token_budget)
    print(header)
    print(format_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
