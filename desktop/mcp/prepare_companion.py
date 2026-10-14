"""Launch the checkout's local graphical request-preparation companion."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    repository = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repository))
    from horizon.desktop_mcp.__main__ import main as desktop_main

    return desktop_main(["companion", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
