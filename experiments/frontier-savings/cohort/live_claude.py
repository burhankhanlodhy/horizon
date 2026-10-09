"""One paired native-client pilot; existing login/model and shared $3 ceiling.

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
CEILING = 3.0
OLD = float(
    json.loads((HERE.parent / "pilot/analysis_results.json").read_text())[
        "total_api_equivalent_usd"
    ]
)
SUMMARY = HERE / "live_results.json"


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
    parser.add_argument("--script-control", action="store_true")
    args = parser.parse_args()
    if SUMMARY.exists() and not args.script_control:
        raise RuntimeError("live summary already exists; do not reset the shared spend ledger")
    previous = json.loads(SUMMARY.read_text()) if SUMMARY.exists() else None
    if args.script_control and (
        previous is None or any(row["arm"] == "script" for row in previous["rows"])
    ):
        raise RuntimeError("script control requires the existing ledger and can run only once")
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

    # Prior pilot actual estimate + this run's conservative usage bounds must
    # remain below $3. Persist each response before exposing it to the client.
    def save():
        SUMMARY.write_text(
            json.dumps(
                {
                    "prior_api_equivalent_usd": OLD,
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

    for arm in ("script",) if args.script_control else ("native", "compact"):
        work = root / arm
        work.mkdir()
        path = work / "catalog.py"
        path.write_bytes(source.encode())
        candidate = Candidate(
            SourceSnapshot("catalog.py", str(path), source),
            "ANTHROPIC_PRICES",
            "claude-3-5-sonnet-latest",
            tuple(f"fixture-sonnet-{i}" for i in range(4)),
        )
        expected = compile_candidate(candidate).after
        journal = ReplayJournal(work / "journal.sqlite")
        controller = CompactEditController()
        scope = Scope("isolated-live-cohort", arm, str(work), "anthropic", MODEL)
        state = {"cohort": None, "attempts": [], "errors": [], "requests": 0, "stopped": False}

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
            ):
                nonlocal spent_upper
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
                        if arm == "compact" and state["cohort"] is None:
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
                            nonlocal spent_upper
                            if state["stopped"]:
                                raise RuntimeError("study stopped after an earlier error")
                            payload = dict(body, stream=False)
                            payload["max_tokens"] = min(int(payload.get("max_tokens", 4096)), 4096)
                            if payload.get("model") != MODEL:
                                raise RuntimeError("routed model changed")
                            # UTF-8 byte count is a conservative text-token bound;
                            # only this small text/tool fixture is permitted.
                            reserve = (
                                len(json.dumps(payload).encode()) * 40 / 1_000_000
                                + payload["max_tokens"] * 100 / 1_000_000
                            )
                            if OLD + spent_upper + reserve > CEILING:
                                raise RuntimeError("shared $3 reserve guard stopped request")
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
                                spent_upper += upper_cost(usage) - reserve
                                state["attempts"].append(
                                    {
                                        "usage": usage,
                                        "cost_upper_usd": upper_cost(usage),
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
                                save()
                            return Reply(
                                result.status_code,
                                [(b"content-type", b"application/json")],
                                result.content,
                            )

                        async def handle():
                            if arm == "compact":
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
        prompt = f'Read {path} first. Add four exact clones of ANTHROPIC_PRICES["claude-3-5-sonnet-latest"] at the end of the dictionary under fixture-sonnet-0 through fixture-sonnet-3, in order. Change only each cloned key and model keyword. Preserve every other value, row, comment and function. Use the cheapest suitable available tool, including a script if useful. Then say DONE.'
        if arm == "script":
            prompt += " For this native-script control, use Bash to run a concise local script that copies the existing source template; avoid printing the repeated source rows."
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
            "--system-prompt",
            "Edit only the isolated catalog.py fixture. No network, package installs, or unrelated files. Use native Read before any change.",
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
            ast_equal = ast.dump(ast.parse(actual), include_attributes=False) == ast.dump(
                ast.parse(expected), include_attributes=False
            )

            def comments(text):
                return [
                    token.string
                    for token in tokenize.generate_tokens(io.StringIO(text).readline)
                    if token.type == tokenize.COMMENT
                ]

            comments_equal = comments(actual) == comments(expected)
            passed = ast_equal and comments_equal
            row = {
                "arm": arm,
                "passed": passed,
                "client_exit_code": process.returncode,
                "ast_matches": ast_equal,
                "comments_match": comments_equal,
                "client_api_equivalent_usd": result.get("total_cost_usd"),
                "provider_attempts": state["attempts"],
                "errors": state["errors"],
                "seconds": round(time.perf_counter() - started, 3),
                "recoveries": state["cohort"].recoveries if state["cohort"] else 0,
            }
        except (subprocess.TimeoutExpired, ValueError, SyntaxError):
            row = {
                "arm": arm,
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
        if row.get("errors") or not row.get("passed"):
            break


if __name__ == "__main__":
    main()
