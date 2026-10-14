"""Launch the explicit local pilot-metadata consent and export CLI."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    repository = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repository))
    from horizon.desktop_mcp.pilot_metadata import main as metadata_main

    return metadata_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
