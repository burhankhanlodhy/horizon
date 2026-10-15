"""Build the portable single-day self-test kit for a laptop without Python.

    python desktop/mcp/self-test/build_kit.py [--skip-build] [--seed 20261015]

It builds (or reuses) the frozen extension, generates the fictional test
documents and pack, and writes ``dist/ContextShrink-SelfTest-<version>.zip``
holding the launcher, the bundled runtime, the ``.mcpb``, the documents, the
pack and a short guide. Nothing is uploaded anywhere.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
EXTENSION_DIR = REPO_ROOT / "desktop" / "mcp" / "extension"
DIST_DIR = EXTENSION_DIR / "dist"
# Fixed timestamp so the same inputs give the same zip bytes.
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LAUNCHER = r"""@echo off
setlocal
title ContextShrink self test
rem Runs entirely from this folder; no Python is needed.
set "KIT=%~dp0"
set "EXE=%KIT%runtime\contextshrink-desktop.exe"
set "STATE=%LOCALAPPDATA%\ContextShrink\self-test\{study_id}"

if not exist "%EXE%" (
  echo The runtime folder is missing. Unzip the whole kit, then try again.
  goto end
)

if not exist "%STATE%\ledger.jsonl" (
  echo Setting up the study ...
  "%EXE%" self-test init --pack "%KIT%pack.json" --state-dir "%STATE%"
  if errorlevel 1 goto end
)

:menu
echo.
echo ================================================================
echo   ContextShrink self test
echo   1  Continue the test (next step, or the end-of-day check)
echo   2  Show progress
echo   3  Make the final report
echo   4  Open the guide
echo   5  Quit
echo ================================================================
choice /c 12345 /n /m "Press 1, 2, 3, 4 or 5: "
if errorlevel 5 goto end
if errorlevel 4 goto guide
if errorlevel 3 goto report
if errorlevel 2 goto status
"%EXE%" self-test run --state-dir "%STATE%"
goto menu

:status
"%EXE%" self-test status --state-dir "%STATE%"
goto menu

:report
"%EXE%" self-test report --state-dir "%STATE%" --output "%KIT%report.json"
echo The full report was saved as report.json in this folder. Send it to Claude Code.
goto menu

:guide
start "" notepad "%KIT%GUIDE.txt"
goto menu

:end
echo.
pause
"""

GUIDE = """ContextShrink self test - quick guide
=====================================

What this does
  Measures, on your own Claude Enterprise account, whether ContextShrink
  makes Claude Desktop work cheaper without worse answers. You do 12 tasks,
  each with 4 methods (48 chats), in one day: about 3 hours. Cost comes from
  the usage total Claude Desktop shows you. The documents in the
  "documents" folder are invented test data.

One-time setup
  1. Unzip this whole folder somewhere permanent, for example your Desktop.
  2. Fully quit Claude Desktop from the tray. In Settings > Extensions,
     remove any old ContextShrink, then install
     contextshrink-desktop-{version}.mcpb from this folder.
  3. In the extension settings, set the approved folder to the "documents"
     folder inside this kit. Leave Pilot method on "normal" for now.
  4. Set Claude Desktop's model to {model_label} and keep it all day.

On the test day
  - Use no other Claude product on this account all day: no other chats,
    no Claude Code, no Codex. They show up in the usage total.
  - Double-click "Start Self Test.cmd" and press 1. The wizard shows every
    step:
      * Each of the 8 phases starts with a setup list. When it says to
        change Pilot method, fully quit and reopen Claude Desktop, then ask
        Claude "show ContextShrink stats" in a new chat to confirm
        evaluation_arm (raw for method A, optimized for B).
      * For each task: type the usage total (Settings > Usage, for example
        20.23), start a NEW chat, paste the prompt (press C to copy),
        attach the file if the method says so, wait for the full reply,
        then paste the line that starts with ANSWER:.
      * Wait for the countdown, then type the usage total again.
  - You can quit any time (q) and press 1 again later to continue.

About 4 hours after your last task
  Double-click the launcher and press 1. It asks for that date's
  Claude Desktop line from the daily usage breakdown by product. If you
  worked across more than one date, it asks for each date.

At the end
  Press 3. It prints the result and saves report.json in this folder.
  Send report.json to Claude Code. It holds no document text and no answer
  text, only costs, right/wrong counts and the verdict.
"""


def _zip_dir(source: Path, target: Path, root_name: str) -> None:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            name = f"{root_name}/{path.relative_to(source).as_posix()}"
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


def build_kit(*, skip_build: bool, seed: int) -> Path:
    build_extension = _load("cs_build_extension", EXTENSION_DIR / "build_extension.py")
    make_dataset = _load("cs_make_dataset", HERE / "make_dataset.py")
    from horizon.desktop_mcp.version import VERSION

    if not skip_build:
        if build_extension.main(["all"]) != 0:
            raise SystemExit("extension build failed")
    onedir = build_extension.ONEDIR
    mcpb = DIST_DIR / f"contextshrink-desktop-{VERSION}.mcpb"
    if not (onedir / f"{build_extension.EXE_NAME}.exe").is_file() or not mcpb.is_file():
        raise SystemExit("build output missing; run without --skip-build")

    kit_name = f"ContextShrink-SelfTest-{VERSION}"
    with tempfile.TemporaryDirectory() as scratch:
        generated = Path(scratch) / "generated"
        if make_dataset.main(["--out", str(generated), "--seed", str(seed)]) != 0:
            raise SystemExit("dataset generation failed")
        pack = json.loads((generated / "pack.json").read_text(encoding="utf-8"))

        kit = Path(scratch) / kit_name
        kit.mkdir()
        shutil.copytree(onedir, kit / "runtime")
        shutil.copy2(mcpb, kit / mcpb.name)
        shutil.copytree(generated / "documents", kit / "documents")
        shutil.copy2(generated / "pack.json", kit / "pack.json")
        launcher = LAUNCHER.replace("{study_id}", pack["study_id"])
        (kit / "Start Self Test.cmd").write_bytes(launcher.replace("\n", "\r\n").encode("ascii"))
        guide = GUIDE.format(version=VERSION, model_label=pack["model_label"])
        (kit / "GUIDE.txt").write_bytes(guide.replace("\n", "\r\n").encode("utf-8"))

        DIST_DIR.mkdir(parents=True, exist_ok=True)
        target = DIST_DIR / f"{kit_name}.zip"
        _zip_dir(kit, target, kit_name)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-build", action="store_true", help="reuse the existing build")
    parser.add_argument("--seed", type=int, default=20261015)
    args = parser.parse_args(argv)
    target = build_kit(skip_build=args.skip_build, seed=args.seed)
    print(f"kit: {target}")
    print(f"size: {target.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
