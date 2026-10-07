"""Claude Code read-maturation experiment: each task once per arm, then grade.

Usage: python run.py <reps> [task ...]     (results appended to results.jsonl)
"""

import json
import os
import random
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
REPO = Path(r"C:\Users\Burhan\OneDrive\Desktop\ContextShrink")
PORTS = {"control": 18811, "treatment": 18812}

TASKS = {
    "shlex_bug": "tests/test_shlex.py fails. Find the bug in vendor/shlex.py and fix it. Run the tests to confirm.",
    "fnmatch_bug": "tests/test_fnmatch.py fails. Find the bug in vendor/fnmatch.py and fix it. Run the tests to confirm.",
    "textwrap_feature": "Add a function wrap_paragraphs(text, width) to vendor/textwrap.py: split the text into "
                        "paragraphs on blank lines, wrap each with the module's existing wrapping (fill), and join "
                        "them with one blank line. Make tests/test_textwrap.py pass.",
    "configparser_bool": "Make getboolean in vendor/configparser.py also accept 'enabled' (true) and 'disabled' "
                         "(false), case-insensitively, and add a test for it under tests/.",
    "configparser_explain": "Explain how value interpolation works in vendor/configparser.py: what BasicInterpolation "
                            "and ExtendedInterpolation each do and where the parser calls them. Do not change any files.",
}
TASKS["combined"] = (
    "Work through all of these in this repository, one at a time, running the relevant tests after each:\n"
    "1. tests/test_shlex.py fails: find and fix the bug in vendor/shlex.py.\n"
    "2. tests/test_fnmatch.py fails: find and fix the bug in vendor/fnmatch.py.\n"
    "3. Add wrap_paragraphs(text, width) to vendor/textwrap.py: split on blank lines, wrap each paragraph with the "
    "module's fill, join with one blank line. Make tests/test_textwrap.py pass.\n"
    "4. Make getboolean in vendor/configparser.py also accept 'enabled' (true) and 'disabled' (false), "
    "case-insensitively, and add a test for it under tests/.\n"
    "Finish by running the whole test suite."
)
CHECKS = {
    "combined": ["tests", "hidden_test.py"],
    "shlex_bug": ["tests/test_shlex.py"],
    "fnmatch_bug": ["tests/test_fnmatch.py"],
    "textwrap_feature": ["tests/test_textwrap.py", "hidden_test.py::test_wrap_paragraphs_more"],
    "configparser_bool": ["hidden_test.py::test_bool_words"],
    "configparser_explain": [],
}
CLAUDE_ARGS = [
    "-p", None, "--output-format", "json", "--model", "sonnet",
    "--permission-mode", "acceptEdits", "--no-session-persistence", "--strict-mcp-config",
    "--allowedTools", "Read", "Edit", "Write", "Grep", "Glob", "Bash(python*)", "PowerShell(python*)",
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
            and ".claude" not in p.parts and not p.name.startswith("_")}


def run_one(task: str, arm: str, rep: int) -> dict:
    work = HERE / "runs" / f"{task}-r{rep}-{arm}"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(HERE / "template", work)
    before = files(work)
    args = [a if a is not None else TASKS[task] for a in CLAUDE_ARGS]
    cmd = [str(REPO / ".venv/Scripts/python.exe"), "-m", "horizon.cli", "wrap", "claude",
           "--no-proxy", "--no-mcp", "--code-memory", "none", "--port", str(PORTS[arm]), "--", *args]
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONIOENCODING="utf-8")
    start, t0 = now(), time.time()
    try:
        p = subprocess.run(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
    except subprocess.TimeoutExpired as e:
        p = subprocess.CompletedProcess(cmd, -1, str(e.stdout or ""), "timed out after 900 s")
    end, secs = now(), round(time.time() - t0, 1)
    (work / "_claude_output.txt").write_text(p.stdout + "\n--- stderr ---\n" + p.stderr, encoding="utf-8")
    result = {}
    for line in reversed(p.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and '"type"' in line:
            try:
                result = json.loads(line)
                break
            except json.JSONDecodeError:
                pass
    answer = result.get("result", "") if isinstance(result.get("result"), str) else ""
    after = files(work)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    if CHECKS[task]:
        shutil.copy(HERE / "hidden_test.py", work / "hidden_test.py")
        g = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *CHECKS[task]],
                           cwd=work, capture_output=True, text=True, timeout=300)
        passed = g.returncode == 0
    else:
        passed = not changed and "BasicInterpolation" in answer and "ExtendedInterpolation" in answer
    usage = result.get("usage") or {}
    return {"task": task, "arm": arm, "rep": rep, "start": start, "end": end, "secs": secs, "exit": p.returncode,
            "passed": passed, "files_changed": changed, "answer_chars": len(answer),
            "claude_cost_usd": result.get("total_cost_usd"), "num_turns": result.get("num_turns"),
            "input_tokens": usage.get("input_tokens"), "cache_read": usage.get("cache_read_input_tokens"),
            "cache_write": usage.get("cache_creation_input_tokens"), "output_tokens": usage.get("output_tokens")}


def main() -> None:
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    tasks = sys.argv[2:] or list(TASKS)
    rng = random.Random()
    for rep in range(1, reps + 1):
        for task in tasks:
            arms = ["control", "treatment"]
            rng.shuffle(arms)
            for arm in arms:
                r = run_one(task, arm, rep)
                with (HERE / "results.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(r) + "\n")
                print(json.dumps(r), flush=True)


if __name__ == "__main__":
    main()
