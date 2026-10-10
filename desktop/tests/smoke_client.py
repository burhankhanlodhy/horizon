"""Smoke test of a built desktop client (Linux or macOS).

Runs `horizon vault`, `horizon desktop` and `horizon wrap/unwrap` for every
tool the app launches, each in a throwaway HOME with stand-in tool binaries
that record the environment they receive. Never touches the real user profile.
Exits non-zero on any failure. Usage: python3 smoke_client.py <horizon binary>
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HORIZON = str(Path(sys.argv[1]).resolve())  # the checks run from temporary folders
PORT = "18788"

# The exact wrap/unwrap arguments desktop/app/src-tauri/src/client.rs uses,
# and the environment variable each tool must receive.
TOOLS = {
    "claude": (["--no-proxy", "--no-mcp", "--code-memory", "none"], ["--no-stop-proxy", "--keep-mcp"], None),
    "codex": (["--no-proxy", "--no-mcp", "--code-memory", "none"], None, "OPENAI_BASE_URL"),
    "opencode": (["--no-proxy", "--no-mcp", "--no-serena"], ["--no-stop-proxy"], "HORIZON_PROXY_URL"),
    "aider": (["--no-proxy"], None, "OPENAI_API_BASE"),
    "copilot": (["--no-proxy"], None, "COPILOT_PROVIDER_BASE_URL"),
    "goose": (["--no-proxy"], None, "OPENAI_BASE_URL"),
    "grok": (["--no-proxy", "--no-mcp", "--code-memory", "none"], None, "GROK_MODELS_BASE_URL"),
    "kimi": (["--no-proxy"], None, "KIMI_CODE_BASE_URL"),
    "bob": (["--no-proxy"], None, "BOB_GATEWAY_URL"),
    "vibe": (["--no-proxy"], None, "VIBE_PROVIDERS"),
    "omp": (["--no-proxy"], ["--no-stop-proxy"], None),
    "openclaude": (["--no-proxy"], None, "ANTHROPIC_BASE_URL"),
    "openhands": (["--no-proxy"], None, "LLM_BASE_URL"),
}

SEED = {
    ".grok/config.toml": '[ui]\ntheme = "dark"\n\n[mcp_servers.horizon]\ncommand = "/opt/my/horizon"\n',
    ".omp/agent/models.yml": "providers:\n  mine:\n    baseUrl: https://example.invalid/v1\n",
    ".codex/config.toml": 'model = "gpt-x"\n\n[mcp_servers.horizon]\ncommand = "/opt/my/horizon"\n',
    ".claude/settings.json": '{\n  "model": "opus"\n}\n',
}

STAND_IN = """#!/bin/sh
{ echo "ARGS=[$*]"; env; } >> "$(dirname "$0")/calls.txt"
exit 0
"""

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("ok    " if ok else "FAIL  ") + what, flush=True)
    if not ok:
        failures.append(what)


def files(base: Path) -> dict[str, str]:
    return {
        str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in base.rglob("*")
        if p.is_file() and "bin" not in p.parts and ".horizon" not in p.parts
    }


def sandbox() -> tuple[Path, dict[str, str]]:
    root = Path(tempfile.mkdtemp(prefix="cs-smoke-"))
    home = root / "home"
    for rel, text in SEED.items():
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text(text)
    env = {"PATH": f"{root / 'bin'}:/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(root), "LANG": "C.UTF-8"}
    return root, env


def run(args: list[str], env: dict[str, str], cwd: Path, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([HORIZON, *args], env=env, cwd=cwd, input=stdin, capture_output=True, text=True, timeout=180)


def test_version() -> None:
    out = subprocess.run([HORIZON, "--version"], capture_output=True, text=True, timeout=60)
    check(out.returncode == 0 and "version" in out.stdout, f"--version ({out.stdout.strip()})")


def test_vault() -> None:
    # Uses the real OS credential store of the CI user (Keychain / Secret Service).
    env = dict(os.environ)
    set_ = subprocess.run([HORIZON, "vault", "set", "--stdin"], input="hz_smoke_test_not_a_key\n",
                          env=env, capture_output=True, text=True, timeout=60)
    get = subprocess.run([HORIZON, "vault", "get"], env=env, capture_output=True, text=True, timeout=60)
    subprocess.run([HORIZON, "vault", "clear"], env=env, capture_output=True, timeout=60)
    gone = subprocess.run([HORIZON, "vault", "get"], env=env, capture_output=True, timeout=60)
    check(set_.returncode == 0 and get.returncode == 0 and gone.returncode != 0,
          f"vault set/get/clear ({(set_.stderr or get.stderr).strip()[:120]})")


def test_editor() -> None:
    root, env = sandbox()
    settings = Path(env["HOME"]) / ".claude/settings.json"
    before = settings.read_bytes()
    c = run(["desktop", "connect", "vscode-claude", "--port", PORT], env, root)
    st = run(["desktop", "status", "vscode-claude", "--port", PORT], env, root)
    d = run(["desktop", "disconnect", "vscode-claude", "--port", PORT], env, root)
    check(c.returncode == 0 and '"connected"' in st.stdout and d.returncode == 0 and settings.read_bytes() == before,
          "VS Code Claude connect/status/disconnect restores settings.json")
    shutil.rmtree(root, ignore_errors=True)


def test_wrap(tool: str) -> None:
    wrap_flags, unwrap_flags, expect_var = TOOLS[tool]
    root, env = sandbox()
    proj = root / "proj"
    proj.mkdir()
    (root / "bin").mkdir()
    stand_in = root / "bin" / tool
    stand_in.write_text(STAND_IN)
    stand_in.chmod(0o755)
    if tool == "copilot":
        env["ANTHROPIC_API_KEY"] = "sk-ant-dummy-not-real"
    home = Path(env["HOME"])
    before = files(home)
    w = run(["wrap", tool, *wrap_flags, "--port", PORT], env, proj)
    if unwrap_flags is not None:
        run(["unwrap", tool, *unwrap_flags, "--port", PORT], env, proj)
    calls = (root / "bin" / "calls.txt").read_text() if (root / "bin" / "calls.txt").exists() else ""
    got_var = expect_var is None or any(
        line.startswith(f"{expect_var}=") and "127.0.0.1:" + PORT in line for line in calls.splitlines()
    )
    restored = files(home) == before
    check(w.returncode == 0 and bool(calls) and got_var and restored,
          f"wrap {tool}: launched={bool(calls)} env={got_var} user files restored={restored}")
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    test_version()
    test_vault()
    test_editor()
    for t in TOOLS:
        test_wrap(t)
    print(json.dumps({"failures": failures}))
    sys.exit(1 if failures else 0)
