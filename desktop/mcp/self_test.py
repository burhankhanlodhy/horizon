"""Development wrapper for the single-account self test.

The wizard lives in horizon/desktop_mcp/self_test_cli.py. The same commands also work as
``python -m horizon.desktop_mcp self-test ...`` and in the frozen app. Run from the
repository root::

    python desktop\\mcp\\self_test.py run --state-dir "<folder printed by init>"
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from horizon.desktop_mcp.self_test_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
