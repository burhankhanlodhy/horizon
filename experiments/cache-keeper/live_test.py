"""Live test: does an idle Claude Code session read its cache on resume, or rewrite it?

Each case starts a session that reads two large files (a ~70k-token context),
lets it sit idle, then resumes it with ``--resume``. The resumed request's own
usage, from the proxy's log, answers the question: cache read (kept warm) or
cache write (expired). Cases run concurrently, each through one proxy
(start.ps1) with its own keep-alive id so no two share a group.
Usage: python live_test.py <a|b|all>
  a: 5-minute lane (FORCE_PROMPT_CACHING_5M), 12-minute pause: control, keepalive, ttl
  b: 1-hour lane (Claude Code's subscription default), 70-minute pause: control, keepalive
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
STDLIB = Path(sysconfig.get_paths()["stdlib"])  # sample code: never the owner's own
LOGS = Path(os.environ["LOCALAPPDATA"]) / "ContextShrinkExperiment" / "cache-keeper"
PORTS = {"control": 18851, "keepalive": 18852, "ttl": 18853}
FIRST = "Read vendor/argparse.py and vendor/difflib.py in full, every line. Then reply READY and nothing else."
SECOND = "Without reading any file again, name three public classes defined in vendor/difflib.py."
CASES = {
    "a": [("control", True, 12 * 60), ("keepalive", True, 12 * 60), ("ttl", True, 12 * 60)],
    "b": [("control", False, 70 * 60), ("keepalive", False, 70 * 60)],
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def claude(work: Path, arm: str, five_min: bool, case: str, args: list[str]) -> dict:
    env = dict(
        os.environ,
        ANTHROPIC_BASE_URL=f"http://127.0.0.1:{PORTS[arm]}",
        ANTHROPIC_CUSTOM_HEADERS=f"X-Horizon-Keepalive-Id: live-{case}-{arm}",
    )
    env.pop("FORCE_PROMPT_CACHING_5M", None)
    if five_min:
        env["FORCE_PROMPT_CACHING_5M"] = "1"
    p = subprocess.run(
        [
            "claude",
            "-p",
            *args,
            "--output-format",
            "json",
            "--model",
            "sonnet",
            "--permission-mode",
            "acceptEdits",
            "--strict-mcp-config",
            "--allowedTools",
            "Read",
        ],
        cwd=work,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        shell=sys.platform == "win32",
    )
    for line in reversed(p.stdout.splitlines()):
        if line.strip().startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    return {"error": (p.stderr or p.stdout)[-400:]}


def first_request_after(arm: str, start: str) -> dict:
    rows = []
    for line in (LOGS / arm / "requests.jsonl").open(encoding="utf-8"):
        r = json.loads(line)
        if (
            r["timestamp"] >= start
            and r["model"].startswith("claude-sonnet")
            and (r.get("input_tokens_original") or 0) >= 2000
        ):
            rows.append(r)
    rows.sort(key=lambda r: r["timestamp"])
    if not rows:
        return {}
    r = rows[0]
    return {
        "cache_read": r.get("cache_read_tokens"),
        "cache_write": r.get("cache_write_tokens"),
        "uncached": r.get("uncached_input_tokens"),
    }


def run_case(case: str, arm: str, five_min: bool, pause: int, out: list) -> None:
    work = HERE / "runs" / f"{case}-{arm}"
    shutil.rmtree(work, ignore_errors=True)
    (work / "vendor").mkdir(parents=True)
    for module in ("argparse.py", "difflib.py"):
        shutil.copy(STDLIB / module, work / "vendor" / module)
    t0 = now()
    first = claude(work, arm, five_min, case, [FIRST])
    sid = first.get("session_id")
    time.sleep(pause)
    t1 = now()
    second = (
        claude(work, arm, five_min, case, ["--resume", sid, SECOND])
        if sid
        else {"error": "no session"}
    )
    row = {
        "case": case,
        "arm": arm,
        "lane": "5m" if five_min else "1h",
        "pause_s": pause,
        "started": t0,
        "resumed": t1,
        "first_cost": first.get("total_cost_usd"),
        "second_cost": second.get("total_cost_usd"),
        "resume_request": first_request_after(arm, t1),
        "answer": (second.get("result") or "")[:160],
        "error": first.get("error") or second.get("error"),
    }
    out.append(row)
    print(json.dumps(row), flush=True)


def main() -> None:
    which = sys.argv[1]
    cases = [(c, *spec) for c in ("a", "b") if which in (c, "all") for spec in CASES[c]]
    out: list = []
    threads = [threading.Thread(target=run_case, args=(*c, out)) for c in cases]
    for t in threads:
        t.start()
        time.sleep(2)
    for t in threads:
        t.join()
    with (HERE / "results.jsonl").open("a", encoding="utf-8") as f:
        for row in out:
            f.write(json.dumps(row) + "\n")
    ledger = LOGS / "keepalive" / "cache_keeper.jsonl"
    if ledger.exists():
        print("\nkeep-alive ledger:")
        for line in ledger.read_text(encoding="utf-8").splitlines():
            print(" ", line)


if __name__ == "__main__":
    main()
