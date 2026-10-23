"""Bake the Horizon version into the frozen desktop client.

A frozen client has no git checkout and, unless PyInstaller copies it, no
package metadata, so ``horizon --version`` fell back to whatever stale
distribution the build machine's venv still carried (``horizon-ai 0.39.1``).
``horizon._version`` reads ``horizon._build_info.BUILD_VERSION`` before any
metadata, the same file the Docker image bakes, so the build scripts write it
from ``pyproject.toml`` before PyInstaller runs and remove it afterwards.

    python build_info.py write            # prints the version written (pyproject's)
    python build_info.py write 0.8.0     # the engine: its own version (engine-version.txt)
    python build_info.py remove
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "horizon" / "_build_info.py"


def pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project = text.split("[project]", 1)[1].split("\n[", 1)[0]
    match = re.search(r'^version\s*=\s*"([^"]+)"', project, re.MULTILINE)
    if not match:
        raise SystemExit("no [project] version in pyproject.toml")
    return match.group(1)


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "write":
        if len(sys.argv) > 2:
            version = sys.argv[2]
            if not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise SystemExit(f"not a release version: {version!r}")
        else:
            version = pyproject_version()
        TARGET.write_text(f"BUILD_VERSION = {version!r}\n", encoding="utf-8")
        print(version)
    elif action == "remove":
        TARGET.unlink(missing_ok=True)
    else:
        raise SystemExit("usage: build_info.py write|remove")


if __name__ == "__main__":
    main()
