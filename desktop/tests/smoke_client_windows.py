"""Smoke test of the Windows desktop client (desktop/client/build-client-embed.ps1).

Runs `horizon --version` and the wrap/unwrap check for claude, the way
smoke_client.py does on Linux and macOS. Every child process gets a clean
environment whose profile variables (USERPROFILE, HOME, LOCALAPPDATA, APPDATA,
TEMP, TMP) point into a throwaway folder, and PATH holds a stand-in claude.cmd
that records its arguments and environment. The Windows credential store is not
used (`vault` is not run), and the forwarder port is not the app's (18788).

Asserts that the user's files come back unchanged and that nothing else is left
in the fake home except under .horizon. Exits non-zero on any failure.

Usage: python smoke_client_windows.py <path to horizon.exe>
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HORIZON = Path(sys.argv[1]).resolve()  # the checks run from temporary folders
PORT = "18791"  # not the desktop app's forwarder port (18788)

# The wrap/unwrap arguments desktop/app/src-tauri/src/client.rs uses for claude.
WRAP_FLAGS = ["--no-proxy", "--no-mcp", "--code-memory", "none"]
UNWRAP_FLAGS = ["--no-stop-proxy", "--keep-mcp"]

SEED = {
    ".claude/settings.json": '{\n  "model": "opus"\n}\n',
}

STAND_IN = (
    '@echo off\r\n>> "%~dp0calls.txt" echo ARGS=[%*]\r\n>> "%~dp0calls.txt" set\r\nexit /b 0\r\n'
)

SYSTEM_ROOT = os.environ.get("SystemRoot", r"C:\Windows")

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("ok    " if ok else "FAIL  ") + what, flush=True)
    if not ok:
        failures.append(what)


def snapshot(home: Path) -> dict[str, str]:
    """Relative path -> SHA-256 of every file under `home`, except under .horizon."""
    return {
        str(path.relative_to(home)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in home.rglob("*")
        if path.is_file() and ".horizon" not in path.relative_to(home).parts
    }


def sandbox_env(root: Path) -> dict[str, str]:
    home = root / "home"
    temp = root / "tmp"
    return {
        "PATH": f"{root / 'bin'};{SYSTEM_ROOT}\\System32;{SYSTEM_ROOT}",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "SYSTEMROOT": SYSTEM_ROOT,
        "COMSPEC": os.environ.get("COMSPEC", rf"{SYSTEM_ROOT}\System32\cmd.exe"),
        "USERPROFILE": str(home),
        "HOME": str(home),
        "LOCALAPPDATA": str(root / "local"),
        "APPDATA": str(root / "roaming"),
        "TEMP": str(temp),
        "TMP": str(temp),
    }


def run(args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(HORIZON), *args],
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


def make_sandbox() -> tuple[Path, dict[str, str]]:
    root = Path(tempfile.mkdtemp(prefix="cs-smoke-"))
    env = sandbox_env(root)
    # Never run against a real profile: the sandbox must live under the temp folder.
    if not Path(env["USERPROFILE"]).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise SystemExit("refusing to run: the sandbox profile is outside the temp folder")
    for folder in ("bin", "tmp", "local", "roaming", "home"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    home = Path(env["HOME"])
    for rel, text in SEED.items():
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text(text, encoding="utf-8")
    stand_in = root / "bin" / "claude.cmd"
    stand_in.write_text(STAND_IN, encoding="utf-8")
    return root, env


def test_version() -> None:
    root, env = make_sandbox()
    try:
        out = run(["--version"], env, root)
        check(out.returncode == 0 and "version" in out.stdout, f"--version ({out.stdout.strip()})")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_wrap_claude() -> None:
    root, env = make_sandbox()
    try:
        home = Path(env["HOME"])
        proj = root / "proj"
        proj.mkdir()
        before = snapshot(home)
        wrap = run(["wrap", "claude", *WRAP_FLAGS, "--port", PORT], env, proj)
        unwrap = run(["unwrap", "claude", *UNWRAP_FLAGS, "--port", PORT], env, proj)
        calls_file = root / "bin" / "calls.txt"
        calls = (
            calls_file.read_text(encoding="utf-8", errors="replace") if calls_file.exists() else ""
        )
        launched = bool(calls)
        restored = snapshot(home) == before
        check(
            wrap.returncode == 0,
            f"wrap claude exit code {wrap.returncode}: {wrap.stderr.strip()[-200:]}",
        )
        check(
            unwrap.returncode == 0,
            f"unwrap claude exit code {unwrap.returncode}: {unwrap.stderr.strip()[-200:]}",
        )
        check(
            launched and restored,
            f"wrap claude: launched={launched} user files restored and nothing stray={restored}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    if not HORIZON.is_file():
        sys.exit(f"no such file: {HORIZON}")
    test_version()
    test_wrap_claude()
    print(json.dumps({"failures": failures}))
    sys.exit(1 if failures else 0)
