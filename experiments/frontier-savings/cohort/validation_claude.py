"""Balanced native-client validation; existing login/model and separate allocation.

Forward only isolated, previously approved pricing fixtures to Anthropic.
Native scripts are available in BOTH arms. Validation bounds are not production
qualification. Headers and raw traffic are neither printed nor persisted.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import io
import json
import os
import subprocess
import sys
import threading
import time
import tokenize
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from horizon.proxy.anthropic_wire import render_anthropic_sse_response  # noqa: E402
from horizon.proxy.compact_edits import (  # noqa: E402
    Candidate,
    CompactEditController,
    CostBounds,
    NativeContract,
    Qualification,
    ReplayJournal,
    Scope,
    SourceSnapshot,
    compile_candidate,
    fingerprint,
)
from horizon.proxy.compact_edits.cohort import ClaudeCohort, Reply  # noqa: E402

HERE = Path(__file__).resolve().parent
MODEL = "claude-sonnet-5-5"
CEILING = 6.0
OLD = 0.0
SUMMARY = HERE / "validation_results.json"
# Balanced order across two repetitions, 4 and 12 literal clones. Native arm
# chooses freely; script control explicitly requests a model-generated script.
PLAN = [
    ("script", 4),
    ("compact", 4),
    ("native", 4),
    ("native", 12),
    ("compact", 12),
    ("script", 12),
    ("compact", 4),
    ("native", 4),
    ("script", 4),
    ("script", 12),
    ("native", 12),
    ("compact", 12),
    ("compact-thinking", 4),
    ("native-unused", 4),
    ("compact-unused", 4),
    ("compact-thinking-puzzle", 4),
]


def upper_cost(usage):
    # Deliberately conservative tariff envelope, NOT a savings estimate.
    return (
        int(usage.get("input_tokens", 0)) * 20
        + int(usage.get("cache_creation_input_tokens", 0)) * 40
        + int(usage.get("cache_read_input_tokens", 0)) * 2
        + int(usage.get("output_tokens", 0)) * 100
    ) / 1_000_000


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if SUMMARY.exists() and not args.resume:
        raise RuntimeError("append-only ledger exists; use --resume, never delete it")
    previous = json.loads(SUMMARY.read_text()) if SUMMARY.exists() else None
    if previous and previous.get("unresolved_attempt"):
        raise RuntimeError("unresolved attempt retains escrow; reconcile before resume")
    if previous and previous["plan"] != [list(item) for item in PLAN[: len(previous["plan"])]]:
        raise RuntimeError("existing plan changed; only append validation controls")
    upstream = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    if upstream != "https://api.anthropic.com":
        raise RuntimeError("existing nonstandard provider needs explicit route verification")
    binary = (
        Path(os.environ["APPDATA"]) / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
    )
    source = (ROOT / "horizon/pricing/anthropic_prices.py").read_text(encoding="utf-8")
    root = HERE / "runs" / ("live-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    root.mkdir(parents=True)
    spent_upper, rows = (
        (previous["new_conservative_cost_upper_usd"], previous["rows"]) if previous else (0.0, [])
    )

    # Additional allocation began at $5, increased to $6 under user authorization.
    # Prior ledgers remain unchanged.
    # Persist escrow before every call; no retries or provider-key logging.
    unresolved_attempt = False

    def save():
        SUMMARY.write_text(
            json.dumps(
                {
                    "allocation": "additional-anthropic-validation-2026-10-09",
                    "unresolved_attempt": unresolved_attempt,
                    "plan": PLAN,
                    "new_conservative_cost_upper_usd": spent_upper,
                    "shared_upper_usd": OLD + spent_upper,
                    "budget_usd": CEILING,
                    "model": MODEL,
                    "native_scripts_in_catalog": True,
                    "successful_script_control": any(
                        row["arm"] == "script" and row.get("passed") for row in rows
                    ),
                    "production_qualified": False,
                    "rows": rows,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    for index, (arm, clone_count) in enumerate(PLAN):
        if index < len(rows):
            continue
        work = root / f"{index:02d}-{arm}-{clone_count}"
        work.mkdir()
        path = work / "catalog.py"
        path.write_bytes(source.encode())
        candidate = Candidate(
            SourceSnapshot("catalog.py", str(path), source),
            "ANTHROPIC_PRICES",
            "claude-3-5-sonnet-latest",
            tuple(f"fixture-sonnet-{i}" for i in range(clone_count)),
        )
        expected = compile_candidate(candidate).after
        journal = ReplayJournal(work / "journal.sqlite")
        controller = CompactEditController()
        scope = Scope("isolated-validation-cohort", f"{index}-{arm}", str(work), "anthropic", MODEL)
        state = {
            "cohort": None,
            "attempts": [],
            "errors": [],
            "requests": 0,
            "stopped": False,
            "thinking": set(),
            "signed_replay_exact": True,
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(
                self,
                state=state,
                arm=arm,
                scope=scope,
                path=path,
                controller=controller,
                candidate=candidate,
                journal=journal,
                index=index,
            ):
                nonlocal spent_upper, unresolved_attempt
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size < 100_000:
                        raise RuntimeError("unexpected request size")
                    request = json.loads(self.rfile.read(size))
                    if self.path.split("?")[0].endswith("/count_tokens"):
                        reply = Reply(
                            200, [(b"content-type", b"application/json")], b'{"input_tokens":1000}'
                        )
                    elif self.path.split("?")[0].endswith("/messages"):
                        if state["requests"] >= 6:
                            raise RuntimeError("bounded provider-call limit reached")
                        state["requests"] += 1
                        definitions = [
                            tool for tool in request.get("tools", []) if tool.get("name") == "Edit"
                        ]
                        if arm.startswith("compact") and state["cohort"] is None:
                            if len(definitions) != 1:
                                raise RuntimeError("native Edit contract missing")
                            contract = NativeContract("claude_edit", fingerprint(definitions[0]))
                            qualification = Qualification(
                                "live-validation-only-not-certified",
                                "anthropic",
                                MODEL,
                                contract,
                                scope.workspace,
                                "catalog.py",
                                str(path),
                                "horizon-model-pricing-literals-v1",
                                "exact_full_source_match",
                                time.time() + 600,
                                CostBounds(0.10, 0.01),
                            )
                            state["cohort"] = ClaudeCohort(
                                controller=controller,
                                scope=scope,
                                candidate=candidate,
                                qualification=qualification,
                                journal=journal,
                                cold_boundary_ready=True,
                            )

                        async def invoke(body):
                            nonlocal spent_upper, unresolved_attempt
                            if state["stopped"]:
                                raise RuntimeError("study stopped after an earlier error")
                            payload = dict(body, stream=False)
                            payload["max_tokens"] = min(int(payload.get("max_tokens", 4096)), 4096)
                            if index >= 12:
                                payload["max_tokens"] = min(payload["max_tokens"], 2048)
                            if arm.startswith("compact-thinking"):
                                payload["thinking"] = {"type": "adaptive"}
                                payload["output_config"] = dict(
                                    payload.get("output_config") or {}, effort="high"
                                )
                            replayed = [
                                fingerprint(block)
                                for item in payload.get("messages", [])
                                for block in (
                                    item.get("content", [])
                                    if isinstance(item.get("content"), list)
                                    else []
                                )
                                if block.get("type") == "thinking" and block.get("signature")
                            ]
                            if any(digest not in state["thinking"] for digest in replayed):
                                state["signed_replay_exact"] = False
                                raise RuntimeError("signed thinking changed on replay")
                            if payload.get("model") != MODEL:
                                raise RuntimeError("routed model changed")
                            # UTF-8 byte count is a conservative text-token bound;
                            # only this small text/tool fixture is permitted.
                            reserve = (
                                len(json.dumps(payload).encode()) * 40 / 1_000_000
                                + payload["max_tokens"] * 100 / 1_000_000
                            )
                            if OLD + spent_upper + reserve > CEILING:
                                raise RuntimeError(
                                    "additional allocation reserve guard stopped request"
                                )
                            headers = {
                                key: value
                                for key, value in self.headers.items()
                                if key.lower()
                                not in {"host", "content-length", "connection", "accept-encoding"}
                            }
                            if not any(
                                key.lower() in {"authorization", "x-api-key"} for key in headers
                            ):
                                raise RuntimeError(
                                    "existing login supplied no provider authentication"
                                )
                            spent_upper += reserve
                            unresolved_attempt = True
                            save()  # escrow covers network timeouts/ambiguous replies
                            async with httpx.AsyncClient(
                                timeout=60, follow_redirects=False
                            ) as client:
                                result = await client.post(
                                    upstream + "/v1/messages?beta=true",
                                    json=payload,
                                    headers=headers,
                                )
                            if result.status_code == 200:
                                message = result.json()
                                if message.get("model") != MODEL or not isinstance(
                                    message.get("usage"), dict
                                ):
                                    # Stop future calls on unknown usage/model; reserve
                                    # the whole authorized request envelope.
                                    save()
                                    raise RuntimeError(
                                        "unknown billed usage/model; further calls stopped"
                                    )
                                usage = message["usage"]
                                state["thinking"].update(
                                    fingerprint(block)
                                    for block in message.get("content", [])
                                    if block.get("type") == "thinking" and block.get("signature")
                                )
                                bound = upper_cost(usage)
                                if bound > reserve:
                                    raise RuntimeError("usage exceeded reservation")
                                spent_upper += bound - reserve
                                unresolved_attempt = False
                                state["attempts"].append(
                                    {
                                        "usage": usage,
                                        "cost_upper_usd": upper_cost(usage),
                                        "signed_thinking_blocks": sum(
                                            block.get("type") == "thinking"
                                            and bool(block.get("signature"))
                                            for block in message.get("content", [])
                                        ),
                                        "signed_thinking_replayed": sum(
                                            block.get("type") == "thinking"
                                            and bool(block.get("signature"))
                                            for item in payload.get("messages", [])
                                            for block in item.get("content", [])
                                            if isinstance(item.get("content"), list)
                                        ),
                                        "tool_names": [
                                            block.get("name")
                                            for block in message.get("content", [])
                                            if block.get("type") == "tool_use"
                                        ],
                                    }
                                )
                                save()
                            elif 400 <= result.status_code < 500:
                                spent_upper -= reserve  # known provider rejection
                                unresolved_attempt = False
                                save()
                            return Reply(
                                result.status_code,
                                [(b"content-type", b"application/json")],
                                result.content,
                            )

                        async def handle():
                            if arm.startswith("compact"):
                                return await state["cohort"].respond(request, invoke)
                            result = await invoke(request)
                            if result.status == 200 and request.get("stream"):
                                return Reply(
                                    200,
                                    [(b"content-type", b"text/event-stream")],
                                    b"".join(
                                        render_anthropic_sse_response(json.loads(result.body))
                                    ),
                                )
                            return result

                        reply = asyncio.run(handle())
                    else:
                        raise RuntimeError("unexpected endpoint")
                    self.send_response(reply.status)
                    for key, value in reply.headers:
                        self.send_header(key.decode(), value.decode())
                    self.send_header("Content-Length", str(len(reply.body)))
                    self.end_headers()
                    self.wfile.write(reply.body)
                except Exception as exc:
                    # Never include raw provider errors/headers in public output.
                    state["stopped"] = True
                    state["errors"].append(type(exc).__name__)
                    data = b'{"type":"error","error":{"type":"invalid_request_error","message":"bounded cohort stopped"}}'
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

        # Serialize validation HTTP handling and spend escrow updates. This is
        # not a proxy throughput test; parallel callbacks cannot race the cap.
        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        env.update(
            ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
            CLAUDE_CODE_MAX_OUTPUT_TOKENS="4096",
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            DISABLE_TELEMETRY="1",
            DISABLE_ERROR_REPORTING="1",
        )
        settings = work / "settings.json"
        settings.write_text(json.dumps({"disableAllHooks": True}), encoding="utf-8")
        mcp = work / "mcp.json"
        mcp.write_text('{"mcpServers":{}}', encoding="utf-8")
        prompt = f'Read {path} first. Add {clone_count} exact clones of ANTHROPIC_PRICES["claude-3-5-sonnet-latest"] at the end of the dictionary under fixture-sonnet-0 through fixture-sonnet-{clone_count - 1}, in order. Change only each cloned key and model keyword. Preserve every other value, row, comment and function. Use the cheapest suitable available tool, including a script if useful. Then say DONE.'
        if arm == "script":
            prompt += " For this native-script control, use Bash to run a concise local script that copies the existing source template; avoid printing the repeated source rows."
        if arm.startswith("compact-thinking"):
            prompt += " Think carefully before each tool call about AST equivalence, key order and preserving all untouched source. Deliberate on the constraints before taking any action."
        if arm == "compact-thinking-puzzle":
            prompt += " Before reading, solve 17^13 mod 97 by mental modular exponentiation and explain the derivation. Then perform the requested file edit. Do not run a calculation tool or script for the arithmetic. This is a protocol validation: take time to think through the modular powers before using Read."
        if arm.endswith("unused"):
            prompt = f"Read {path} and report the dictionary name. Do not edit any file."
        command = [
            str(binary),
            "-p",
            prompt,
            "--model",
            MODEL,
            "--output-format",
            "json",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--tools",
            "Read,Edit,Bash",
            "--allowedTools",
            "Read,Edit,Bash",
            "--permission-mode",
            "dontAsk",
            "--system-prompt",
            "Edit only the isolated catalog.py fixture. No network, package installs, or unrelated files. Use native Read before any change. For Bash use one standalone Python command, without compound commands or shell pipelines.",
            "--settings",
            str(settings),
            "--strict-mcp-config",
            "--mcp-config",
            str(mcp),
            "--max-turns",
            "5",
            "--setting-sources",
            "",
            "--max-budget-usd",
            ".25",
        ]
        started = time.perf_counter()
        try:
            process = subprocess.run(
                command,
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            (work / "client_stdout.json").write_text(process.stdout, encoding="utf-8")
            (work / "client_stderr.txt").write_text(process.stderr, encoding="utf-8")
            result = json.loads(process.stdout)
            actual = path.read_text(encoding="utf-8")
            expected_result = source if arm.endswith("unused") else expected
            ast_equal = ast.dump(ast.parse(actual), include_attributes=False) == ast.dump(
                ast.parse(expected_result), include_attributes=False
            )

            def comments(text):
                return [
                    token.string
                    for token in tokenize.generate_tokens(io.StringIO(text).readline)
                    if token.type == tokenize.COMMENT
                ]

            comments_equal = comments(actual) == comments(expected_result)
            passed = ast_equal and comments_equal
            row = {
                "arm": arm,
                "clone_count": clone_count,
                "plan_index": index,
                "passed": passed,
                "client_exit_code": process.returncode,
                "ast_matches": ast_equal,
                "comments_match": comments_equal,
                "client_api_equivalent_usd": result.get("total_cost_usd"),
                "permission_denials": len(result.get("permission_denials", [])),
                "signed_replay_exact": state["signed_replay_exact"],
                "provider_attempts": state["attempts"],
                "errors": state["errors"],
                "seconds": round(time.perf_counter() - started, 3),
                "recoveries": state["cohort"].recoveries if state["cohort"] else 0,
            }
        except (subprocess.TimeoutExpired, ValueError, SyntaxError):
            row = {
                "arm": arm,
                "clone_count": clone_count,
                "plan_index": index,
                "passed": False,
                "errors": ["client_incomplete"],
                "provider_attempts": state["attempts"],
            }
        finally:
            server.shutdown()
            server.server_close()
            journal.close()
        rows.append(row)
        save()
        print(json.dumps(row), flush=True)
        if unresolved_attempt or state["stopped"]:
            break


if __name__ == "__main__":
    main()
