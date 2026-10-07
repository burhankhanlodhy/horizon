"""Run the Codex output-shaper experiment: each task once per arm, then grade.

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
PORTS = {"treatment": 18801, "control": 18802}

TASKS = {
    "fix_pricing": "The pricing tests fail. Find and fix the bug in inventory/pricing.py. Run the tests to confirm.",
    "durations": "Implement parse_duration in inventory/durations.py so that tests/test_durations.py passes. "
                 "Follow the docstring. Run the tests to confirm.",
    "refactor": "Refactor build_report in inventory/report.py for readability without changing its output. "
                "Keep tests/test_report.py passing.",
    "explain": "Explain how TTLCache in inventory/cache.py works, including how expiry and eviction interact. "
               "Do not change any files.",
    "validation": "Make apply_discount in inventory/pricing.py raise ValueError when pct is outside 0 to 100 "
                  "(0 and 100 are allowed), and add tests for it in tests/test_pricing.py.",
    "debug_stock": "tests/test_stock.py is failing. Find the cause in inventory/stock.py and fix it.",
}

# Grading: pytest node ids that must pass, plus hidden checks run after the task.
CHECKS = {
    "fix_pricing": ["tests/test_pricing.py"],
    "durations": ["tests/test_durations.py", "hidden_test.py::test_durations_more"],
    "refactor": ["tests/test_report.py", "hidden_test.py::test_report_refactored"],
    "explain": [],
    "validation": ["hidden_test.py::test_discount_raises"],
    "debug_stock": ["tests/test_stock.py", "hidden_test.py::test_stock_more"],
}

HIDDEN = '''
import inspect
import pytest
from inventory import pricing, report, stock


def test_discount_raises():
    for bad in (-1, 100.5, 101):
        with pytest.raises(ValueError):
            pricing.apply_discount(10, bad)
    pricing.apply_discount(10, 0)
    pricing.apply_discount(10, 100)


def test_durations_more():
    from inventory.durations import parse_duration
    assert parse_duration("90m") == 5400 and parse_duration("1d1h1m1s") == 90061
    for bad in ("h", "1hh", "-1h", "1.5h"):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_report_refactored():
    src = inspect.getsource(report)
    assert "pass" not in src.split() and "else:" not in src


def test_stock_more():
    assert stock.low_stock([{"name": "z", "qty": 0}, {"name": "y", "qty": 2}], threshold=3) == ["z", "y"]
'''


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_one(task: str, arm: str, rep: int) -> dict:
    work = HERE / "runs" / f"{task}-r{rep}-{arm}"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(HERE / "template", work)
    before = {p.relative_to(work).as_posix(): p.read_bytes() for p in work.rglob("*") if p.is_file()}
    cmd = [str(REPO / ".venv/Scripts/python.exe"), "-m", "horizon.cli", "wrap", "codex",
           "--no-proxy", "--no-mcp", "--code-memory", "none", "--port", str(PORTS[arm]), "--",
           "exec", "--skip-git-repo-check", "--ephemeral", "-s", "workspace-write",
           "-o", str(work / "_last_message.txt"), TASKS[task]]
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONIOENCODING="utf-8")
    start, t0 = now(), time.time()
    # stdin must be closed: with a pipe, codex exec waits to read more prompt from it.
    try:
        p = subprocess.run(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired as e:
        p = subprocess.CompletedProcess(cmd, -1, str(e.stdout or ""), "timed out after 600 s")
    end, secs = now(), round(time.time() - t0, 1)
    (work / "_codex_output.txt").write_text(p.stdout + "\n--- stderr ---\n" + p.stderr, encoding="utf-8")
    last = (work / "_last_message.txt").read_text(encoding="utf-8", errors="replace") if (work / "_last_message.txt").exists() else ""

    changed = sorted(
        rel for rel in {p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file()} | set(before)
        if not rel.startswith("_") and "__pycache__" not in rel and ".pytest_cache" not in rel
        and (not (work / rel).exists() or (work / rel).read_bytes() != before.get(rel))
    )
    passed = True
    if CHECKS[task]:
        (work / "hidden_test.py").write_text(HIDDEN, encoding="utf-8")
        g = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *CHECKS[task]],
                           cwd=work, capture_output=True, text=True, timeout=300)
        passed = g.returncode == 0
    else:  # explain: must not change files and must cover both mechanisms
        low = last.lower()
        passed = not changed and "expir" in low and ("evict" in low or "least recently" in low or "oldest" in low)
    return {"task": task, "arm": arm, "rep": rep, "start": start, "end": end, "secs": secs,
            "exit": p.returncode, "passed": passed, "files_changed": changed, "answer_chars": len(last)}


def main() -> None:
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    tasks = sys.argv[2:] or list(TASKS)
    rng = random.Random()
    out = HERE / "results.jsonl"
    for rep in range(1, reps + 1):
        for task in tasks:
            arms = ["treatment", "control"]
            rng.shuffle(arms)  # alternate which arm goes first
            for arm in arms:
                r = run_one(task, arm, rep)
                with out.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(r) + "\n")
                print(json.dumps(r), flush=True)


if __name__ == "__main__":
    main()
