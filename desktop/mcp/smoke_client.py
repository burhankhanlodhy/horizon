"""Run the local fixture-only Desktop MCP stdio smoke probe."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    # Executing a file under desktop/mcp does not guarantee the checkout root
    # is on sys.path, so make this workspace script usable before installation.
    root = _repository_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    parser = argparse.ArgumentParser(
        description="Probe the local fixture-only MCP server over stdio."
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional explicit path for a JSON capability report",
    )
    args = parser.parse_args(argv)

    from horizon.desktop_mcp.capabilities import run_probe

    report = asyncio.run(run_probe(args.output))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
