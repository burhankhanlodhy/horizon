"""Launch the checkout's diagnostic MCP server from any working directory.

Claude Desktop may launch MCP commands outside the repository. This source
launcher resolves the checkout relative to its own file, without changing
Claude's configuration or importing the legacy proxy command tree.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    repository = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repository))
    from horizon.desktop_mcp.__main__ import main as desktop_main

    return desktop_main(["serve", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
