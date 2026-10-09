"""Real Claude Code against a scripted, loopback-only Messages provider.

No external model, real API key, OAuth/keychain access, permission bypass or
production source modification. All edits target disposable isolated fixtures.
This validates client mechanics, NOT model adoption or financial qualification.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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

HERE = Path(__file__).resolve().parent
CASES = (
    "native_control",
    "compact_roundtrip",
    "changed_after_read",
    "whitespace_before_read",
    "denied_edit",
)


class Scenario:
    def __init__(self, work: Path, case: str):
        self.work, self.case = work, case
        self.path = work / "catalog.py"
        self.original = (ROOT / "horizon/pricing/anthropic_prices.py").read_text(encoding="utf-8")
        self.path.write_bytes(self.original.encode("utf-8"))
        self.candidate = Candidate(
            SourceSnapshot("catalog.py", str(self.path), self.original),
            "ANTHROPIC_PRICES",
            "claude-3-5-sonnet-latest",
            tuple(f"fixture-sonnet-{index}" for index in range(4)),
        )
        self.edit = compile_candidate(self.candidate)
        self.changed = self.original.replace("input_per_1m=3.00,", "input_per_1m=3.01,", 1)
        if case == "whitespace_before_read":
            self.changed = self.original.replace("input_per_1m=3.00,", "input_per_1m = 3.00,", 1)
            self.path.write_bytes(self.changed.encode("utf-8"))
        self.requests: list[dict[str, Any]] = []
        self.results: dict[str, dict[str, Any]] = {}
        self.errors: list[str] = []
        self.controller = CompactEditController()
        self.journal = ReplayJournal((work / "journal.sqlite").resolve())
        self.scope = Scope("isolated-cohort", case, str(work), "anthropic", "claude-sonnet-5-5")
        self.contract: NativeContract | None = None
        self.turn = None

    def message(self, content: list[dict[str, Any]], stop: str) -> dict[str, Any]:
        return {
            "id": "msg_cohort_" + str(len(self.requests)),
            "type": "message",
            "role": "assistant",
            "model": self.scope.model,
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }

    def respond(self, body: dict[str, Any]) -> dict[str, Any]:
        if len(self.requests) >= 12:
            raise RuntimeError("bounded cohort request limit reached")
        self.requests.append(body)
        for message in body.get("messages", []):
            for block in (
                message.get("content", []) if isinstance(message.get("content"), list) else []
            ):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self.results[block["tool_use_id"]] = block
        if self.contract is None:
            definitions = [tool for tool in body.get("tools", []) if tool.get("name") == "Edit"]
            if len(definitions) != 1 and self.case != "denied_edit":
                raise RuntimeError("installed client did not supply native Edit")
            if len(definitions) == 1:
                self.contract = NativeContract("claude_edit", fingerprint(definitions[0]))
                self.contract.validate(body["tools"])
                (self.work / "native_contract.json").write_text(
                    json.dumps(definitions[0], indent=2), encoding="utf-8"
                )
        if "read_cohort" not in self.results:
            return self.message(
                [
                    {
                        "type": "tool_use",
                        "id": "read_cohort",
                        "name": "Read",
                        "input": {"file_path": str(self.path)},
                    }
                ],
                "tool_use",
            )
        if "edit_cohort" not in self.results:
            (self.work / "read_result.json").write_text(
                json.dumps(self.results["read_cohort"], indent=2), encoding="utf-8"
            )
            if self.case == "changed_after_read":
                self.path.write_bytes(self.changed.encode("utf-8"))
            native = {
                "type": "tool_use",
                "id": "edit_cohort",
                "name": "Edit",
                "input": {
                    "file_path": str(self.path),
                    "old_string": self.original,
                    "new_string": self.edit.after,
                    "replace_all": False,
                },
            }
            if self.case == "compact_roundtrip":
                # Test-only attestation, deliberately NOT published as a
                # production qualification or measured cost profile.
                qualification = Qualification(
                    "scripted-mechanics-only",
                    self.scope.provider,
                    self.scope.model,
                    self.contract,
                    self.scope.workspace,
                    "catalog.py",
                    str(self.path),
                    "horizon-model-pricing-literals-v1",
                    "exact_full_source_match",
                    time.time() + 600,
                    CostBounds(0.10, 0.01),
                )
                provider_body = dict(
                    body,
                    stream=False,
                    tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                )
                admitted = self.controller.prepare(
                    provider_body,
                    scope=self.scope,
                    candidate=self.candidate,
                    qualification=qualification,
                    journal=self.journal,
                    cold_boundary=True,
                    execution_guard_ready=True,
                )
                if admitted.turn is None:
                    raise RuntimeError("mechanical admission rejected: " + admitted.reason)
                self.turn = admitted.turn
                compact = dict(
                    native,
                    name="horizon_compact_edit_v1",
                    input={
                        "receipt": self.turn.receipt,
                        "table": self.candidate.table,
                        "template": self.candidate.template,
                        "keys": list(self.candidate.keys),
                    },
                )
                return self.controller.translate_response(
                    self.message([compact], "tool_use"), self.turn
                )
            return self.message([native], "tool_use")
        if self.turn is not None:
            restored = self.controller.normalize_replay(
                body, scope=self.scope, kind="claude_edit", journal=self.journal
            )
            calls = [
                block
                for message in restored["messages"]
                for block in message.get("content", [])
                if isinstance(message.get("content"), list)
                and isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("id") == "edit_cohort"
            ]
            if len(calls) != 1 or calls[0]["name"] != "horizon_compact_edit_v1":
                raise RuntimeError("native replay did not restore compact provider call")
        return self.message([{"type": "text", "text": "DONE"}], "end_turn")

    def summary(self, returncode: int, timed_out: bool) -> dict[str, Any]:
        actual = self.path.read_bytes()
        tool_result = self.results.get("edit_cohort", {})
        rejected = bool(tool_result.get("is_error"))
        should_reject = self.case in {"changed_after_read", "whitespace_before_read", "denied_edit"}
        expected = (
            self.changed
            if self.case in {"changed_after_read", "whitespace_before_read"}
            else self.original
        )
        passed = (
            rejected and actual == expected.encode()
            if should_reject
            else not rejected and actual == self.edit.after.encode()
        )
        result_text = tool_result.get("content", "")
        if not isinstance(result_text, str):
            result_text = json.dumps(result_text)
        return {
            "case": self.case,
            "client_exit_code": returncode,
            "timed_out": timed_out,
            "requests": len(self.requests),
            "tool_result_received": bool(tool_result),
            "edit_rejected": rejected,
            "source_unchanged_from_expected": actual == expected.encode(),
            "expanded_edit_matches": actual == self.edit.after.encode(),
            "replay_restored": self.turn is not None
            and "edit_cohort" in self.results
            and not self.errors,
            "guard_passed": bool(passed and tool_result and not timed_out and not self.errors),
            "tool_error_excerpt": result_text[:600] if rejected else None,
            "errors": self.errors,
            "native_contract_sha256": self.contract.definition_sha256 if self.contract else None,
            "external_model_calls": 0,
            "external_api_spend_usd": 0,
        }


def run_case(binary: Path, root: Path, case: str) -> dict[str, Any]:
    work = root / case
    work.mkdir()
    scenario = Scenario(work, case)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def do_POST(self) -> None:
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4_000_000:
                    raise ValueError("invalid request size")
                body = json.loads(self.rfile.read(size))
                if self.path.split("?")[0].endswith("/count_tokens"):
                    response = json.dumps({"input_tokens": 100}).encode()
                    content_type = "application/json"
                elif self.path.split("?")[0].endswith("/messages"):
                    message = scenario.respond(body)
                    response = (
                        b"".join(render_anthropic_sse_response(message))
                        if body.get("stream")
                        else json.dumps(message).encode()
                    )
                    content_type = "text/event-stream" if body.get("stream") else "application/json"
                else:
                    raise ValueError("unexpected endpoint")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
            except Exception as exc:
                scenario.errors.append(type(exc).__name__ + ": " + str(exc))
                response = json.dumps(
                    {
                        "type": "error",
                        "error": {"type": "invalid_request_error", "message": "cohort failed"},
                    }
                ).encode()
                self.send_response(400)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = dict(os.environ)
    for name in ("CLAUDECODE", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(name, None)
    env.update(
        ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
        ANTHROPIC_API_KEY="cohort-local-placeholder",
        CLAUDE_CONFIG_DIR=str(work / "client-state"),
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        DISABLE_TELEMETRY="1",
        DISABLE_ERROR_REPORTING="1",
    )
    command = [
        str(binary),
        "--bare",
        "-p",
        "Read catalog.py, perform the isolated fixture edit, then say DONE.",
        "--model",
        scenario.scope.model,
        "--output-format",
        "json",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--tools",
        "Read,Edit",
        "--max-turns",
        "4",
        "--system-prompt",
        "Use only the provided tools inside this isolated fixture workspace.",
    ]
    command.extend(["--allowedTools", "Read" if case == "denied_edit" else "Read,Edit"])
    if case == "denied_edit":
        settings = work / "settings.json"
        settings.write_text(json.dumps({"permissions": {"deny": ["Edit"]}}), encoding="utf-8")
        command.extend(["--settings", str(settings)])
    timed_out = False
    try:
        result = subprocess.run(
            command,
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        (work / "client_stdout.json").write_text(result.stdout, encoding="utf-8")
        (work / "client_stderr.txt").write_text(result.stderr, encoding="utf-8")
        return scenario.summary(result.returncode, timed_out)
    except subprocess.TimeoutExpired:
        return scenario.summary(-1, True)
    finally:
        server.shutdown()
        server.server_close()
        scenario.journal.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument(
        "--binary",
        type=Path,
        default=Path(os.environ["APPDATA"])
        / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
    )
    args = parser.parse_args()
    root = HERE / "runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    root.mkdir(parents=True)
    results = []
    for case in args.cases:
        value = run_case(args.binary, root, case)
        results.append(value)
        print(json.dumps(value), flush=True)
    public = {
        "client": "Claude Code 2.1.295",
        "provider": "scripted-loopback-only",
        "model_adoption_tested": False,
        "financial_qualification": False,
        "external_model_calls": 0,
        "external_api_spend_usd": 0,
        "results": results,
    }
    (HERE / "native_results.json").write_text(json.dumps(public, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
