"""PyInstaller entry script for the contextshrink-desktop executable.

The frozen executable serves the same subcommands as ``python -m
horizon.desktop_mcp``: ``serve --extension``, ``broker``, ``worker`` and the
developer commands. Arguments are read from ``sys.argv`` by ``main``.
"""

from __future__ import annotations

import multiprocessing

from horizon.desktop_mcp.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
