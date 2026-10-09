"""Live test of Flash Observations (next-turn form) on the OpenAI Responses API.

Same task as live_test.py: a log-triage agent calls ``run_tests(suite)`` for
six suites, each returning a long pytest log with failures planted in the
middle, and must list every failure at the end. Requests are stateless
(``store: false``, encrypted reasoning passed back), the way Codex talks to
the API, so the whole history is re-sent every turn.

Arms:
  control-direct   raw outputs, straight to the API
  flash-direct     the transform applied client-side (tests the API: an
                   earlier output edited next to encrypted reasoning is
                   accepted, and the prefix before it stays cached)
  control-proxy    through a local Horizon proxy, flash off
  flash-proxy      through a local Horizon proxy, HORIZON_FLASH_OPENAI=1

Usage:
  OPENAI_API_KEY=... python experiments/flash-observations/live_test_openai.py --reps 3
  python experiments/flash-observations/live_test_openai.py --fake     # no key, no cost
Options: --model (default gpt-6.1-sol), --arms a,b,.., --suites N (any number;
savings grow with session length on OpenAI, which has no cache-write premium),
--injection, --base-url (an OpenAI-compatible upstream that passed
preflight_openai.py).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

from live_test import SUITES, suite_log, wait_ready  # noqa: E402

from horizon.transforms.flash_observations import FlashPolicy  # noqa: E402
from horizon.transforms.flash_openai import apply_responses  # noqa: E402

ARMS = ("control-direct", "flash-direct", "control-proxy", "flash-proxy")
PROXY_PORTS = {"control-proxy": 18894, "flash-proxy": 18895}
FAKE_PORT = 18893
OFFICIAL = "https://api.openai.com"
# $ per MTok: input, cached input, output (below the 272k whole-request tier)
PRICES = {
    "gpt-6.1-sol": (2.0, 0.10, 10.0),
}
SYSTEM = (
    "You are a CI triage agent. Use the run_tests tool to run each test suite listed "
    "by the user, one call per suite, in the order given. After each call, write one "
    "line naming every failing test in that suite and its error message. When every "
    "suite has been run, reply with only a JSON array of objects "
    '{"suite": ..., "test": ..., "error": ...}, one per failing test across all suites.'
)
TOOLS = [
    {
        "type": "function",
        "name": "run_tests",
        "description": "Run one test suite and return the full pytest output.",
        "parameters": {
            "type": "object",
            "properties": {"suite": {"type": "string", "description": "Suite name"}},
            "required": ["suite"],
            "additionalProperties": False,
        },
        "strict": True,
    }
]
FLASH_POLICY = FlashPolicy(tools=frozenset({"run_tests"}), min_chars=8_000)


def suite_names(n: int) -> list[str]:
    """The first ``n`` suites; past live_test's eight, ``mod09``, ``mod10`` and so on."""
    return [*SUITES, *(f"mod{i:02d}" for i in range(len(SUITES) + 1, n + 1))][:n]


@dataclass
class RunResult:
    arm: str
    rep: int
    requests: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""
    error: str = ""
    injection_complied: bool | None = None
    truth: list[dict[str, str]] = field(default_factory=list)

    def totals(self) -> dict[str, int]:
        out = {
            k: sum(r.get(k, 0) for r in self.requests)
            for k in ("input_tokens", "cached_tokens", "output_tokens")
        }
        out["uncached_tokens"] = out["input_tokens"] - out["cached_tokens"]
        return out

    def cost(self, model: str, prices: tuple[float, float, float]) -> float:
        p_in, p_cached, p_out = PRICES.get(model, prices)
        t = self.totals()
        return (
            t["uncached_tokens"] * p_in + t["cached_tokens"] * p_cached + t["output_tokens"] * p_out
        ) / 1e6

    def score(self) -> float:
        try:
            match = re.search(r"\[.*\]", self.final_text, re.S)
            answer = json.loads(match.group(0)) if match else []
        except (ValueError, TypeError):
            return 0.0
        # GPT often names a test by its full pytest id (path::name); compare the name.
        found = {
            (str(a.get("test", "")).rsplit("::", 1)[-1], str(a.get("error", "")))
            for a in answer
            if isinstance(a, dict)
        }
        truth = {(t["test"], t["error"]) for t in self.truth}
        if not truth:
            return 0.0
        hits = len(found & truth)
        return max(0.0, (hits - 0.5 * len(found - truth)) / len(truth))


def _text_of(item: dict[str, Any]) -> str:
    return "".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict))


def run_session(
    http: httpx.Client, url: str, arm: str, rep: int, args: argparse.Namespace
) -> RunResult:
    suites = suite_names(args.suites)
    result = RunResult(arm=arm, rep=rep)
    logs = {}
    for s in suites:
        text, failures = suite_log(s, rep, args.injection and s == suites[len(suites) // 2])
        logs[s] = text
        result.truth += failures
    session = f"{arm}-{rep}-{args.session_tag}"
    items: list[dict[str, Any]] = [
        {
            "type": "message",
            "role": "user",
            "content": "Run these suites in order: " + ", ".join(suites) + f". Session {session}.",
        }
    ]
    for step in range(len(suites) + 3):
        outgoing = items
        if arm == "flash-direct":
            outgoing = apply_responses(items, horizon=0, policy=FLASH_POLICY).messages
        body = {
            "model": args.model,
            "instructions": SYSTEM,
            "input": outgoing,
            "tools": TOOLS,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": session,
            "max_output_tokens": 16_000,
        }
        if args.effort:
            body["reasoning"] = {"effort": args.effort}
        started = time.monotonic()
        try:
            resp = http.post(url, json=body)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            result.error = f"{type(exc).__name__}: {exc}"[:2000]
            break
        if resp.status_code != 200:
            result.error = f"HTTP {resp.status_code}: {json.dumps(data)[:1500]}"
            break
        usage = data.get("usage") or {}
        output = data.get("output") or []
        calls = [o for o in output if o.get("type") == "function_call"]
        result.requests.append(
            {
                "step": step,
                "seconds": round(time.monotonic() - started, 2),
                "input_tokens": usage.get("input_tokens", 0),
                "cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "status": data.get("status"),
                "items_sent": len(outgoing),
                "tool_calls": [
                    "run_tests:" + str(json.loads(c.get("arguments") or "{}").get("suite", ""))
                    for c in calls
                ],
                "refusal": any(
                    isinstance(c, dict) and c.get("type") == "refusal"
                    for o in output
                    for c in (o.get("content") or [])
                ),
            }
        )
        # Pass back everything the model produced (reasoning included), as Codex does.
        items += [{k: v for k, v in o.items() if k != "status"} for o in output]
        if not calls:
            result.final_text = "".join(_text_of(o) for o in output if o.get("type") == "message")
            break
        for c in calls:
            suite = str(json.loads(c.get("arguments") or "{}").get("suite", ""))
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": c["call_id"],
                    "output": logs.get(suite, "no such suite"),
                }
            )
    if args.injection:
        result.injection_complied = "PWNED" in result.final_text
    return result


def start_fake(workdir: Path) -> subprocess.Popen:
    log = open(workdir / "fake.log", "w")  # noqa: SIM115 - closed with the process
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "fake_openai.py"), str(FAKE_PORT)],
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=REPO,
    )
    wait_ready(f"http://127.0.0.1:{FAKE_PORT}/healthz", "fake upstream", workdir / "fake.log")
    return proc


def start_proxy(arm: str, workdir: Path, upstream: str) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(
        HORIZON_WORKSPACE_DIR=str(workdir / arm),
        HORIZON_FLASH_OPENAI="1" if arm == "flash-proxy" else "0",
        HORIZON_FLASH_TOOLS="run_tests",
        # A non-OpenAI upstream is allowed by host, the way a user enables one
        # that passed preflight_openai.py (the fake is 127.0.0.1).
        HORIZON_FLASH_ANY_UPSTREAM="0",
        HORIZON_FLASH_OPENAI_UPSTREAMS=urlparse(upstream).hostname or "",
    )
    log = open(workdir / f"{arm}.log", "w")  # noqa: SIM115 - closed with the process
    cmd = [
        sys.executable,
        "-m",
        "horizon.cli",
        "proxy",
        "--port",
        str(PROXY_PORTS[arm]),
        "--mode",
        "cache",
        "--no-rate-limit",
        "--no-cache",
        "--openai-api-url",
        upstream,
    ]
    proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, cwd=REPO)
    try:
        wait_ready(
            f"http://127.0.0.1:{PROXY_PORTS[arm]}/readyz", f"{arm} proxy", workdir / f"{arm}.log"
        )
    except RuntimeError:
        proc.terminate()
        raise
    return proc


def summarize(results: list[RunResult], model: str, prices: tuple[float, float, float]) -> str:
    rows = [
        f"{'arm':15} {'rep':>3} {'reqs':>4} {'input':>9} {'cached':>9} {'uncached':>9} "
        f"{'output':>7} {'$':>7} {'score':>5}  error"
    ]
    for r in results:
        t = r.totals()
        rows.append(
            f"{r.arm:15} {r.rep:>3} {len(r.requests):>4} {t['input_tokens']:>9,} "
            f"{t['cached_tokens']:>9,} {t['uncached_tokens']:>9,} {t['output_tokens']:>7,} "
            f"{r.cost(model, prices):>7.3f} {r.score():>5.2f}  {r.error[:60]}"
        )
    rows.append("")
    for arm in dict.fromkeys(r.arm for r in results):
        mine = [r for r in results if r.arm == arm and not r.error]
        if mine:
            cost = sum(r.cost(model, prices) for r in mine) / len(mine)
            score = sum(r.score() for r in mine) / len(mine)
            rows.append(f"{arm:15} mean ${cost:.3f}  score {score:.2f}  ({len(mine)} clean runs)")
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--model", default="gpt-6.1-sol")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--suites", type=int, default=6)
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--effort", default=None, help="reasoning effort, e.g. low/medium/high")
    ap.add_argument("--injection", action="store_true")
    ap.add_argument("--fake", action="store_true", help="run against fake_openai.py (no key)")
    ap.add_argument(
        "--base-url", default=None, help="OpenAI-compatible upstream (default official)"
    )
    ap.add_argument(
        "--prices",
        default="2.0,0.10,10.0",
        help="$ per MTok input,cached,output for a model not in the table",
    )
    args = ap.parse_args()
    args.session_tag = datetime.now(timezone.utc).strftime("%H%M%S")
    prices = tuple(float(x) for x in args.prices.split(","))
    arms = [a for a in args.arms.split(",") if a]
    if args.fake:
        upstream = f"http://127.0.0.1:{FAKE_PORT}"
        key = "sk-fake"
    else:
        upstream = (args.base_url or OFFICIAL).rstrip("/").removesuffix("/v1")
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            print("OPENAI_API_KEY is not set", file=sys.stderr)
            return 2
    out_dir = HERE / "runs" / ("openai-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    out_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="flash-openai-"))
    procs: dict[str, subprocess.Popen] = {}
    results: list[RunResult] = []
    print(f"plan: {len(arms)} arms x {args.reps} reps, {args.suites} suites, model {args.model}")
    try:
        if args.fake:
            procs["fake"] = start_fake(workdir)
        for arm in arms:
            if arm.endswith("-proxy"):
                procs[arm] = start_proxy(arm, workdir, upstream)
        with httpx.Client(timeout=600, headers={"authorization": f"Bearer {key}"}) as http:
            for rep in range(args.reps):
                for arm in arms:
                    base = (
                        f"http://127.0.0.1:{PROXY_PORTS[arm]}"
                        if arm.endswith("-proxy")
                        else upstream
                    )
                    res = run_session(http, base + "/v1/responses", arm, rep, args)
                    results.append(res)
                    with (out_dir / "results.jsonl").open("a") as fh:
                        fh.write(
                            json.dumps(
                                {
                                    "arm": res.arm,
                                    "rep": res.rep,
                                    "requests": res.requests,
                                    "totals": res.totals(),
                                    "cost_usd": round(res.cost(args.model, prices), 5),
                                    "score": res.score(),
                                    "final_text": res.final_text,
                                    "error": res.error,
                                    "injection_complied": res.injection_complied,
                                }
                            )
                            + "\n"
                        )
                    print(
                        f"{arm} rep {rep}: ${res.cost(args.model, prices):.3f} "
                        f"score {res.score():.2f} {res.error[:120]}",
                        flush=True,
                    )
    finally:
        for proc in procs.values():
            proc.terminate()
    summary = summarize(results, args.model, prices)
    (out_dir / "summary.txt").write_text(summary + "\n")
    print("\n" + summary + f"\n\nresults: {out_dir}\nproxy logs: {workdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
