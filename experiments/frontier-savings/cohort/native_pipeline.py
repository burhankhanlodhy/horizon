"""Installed Claude through create_app/auth/compression/cache/outbox; no paid calls.

Uses the existing zero-network integration fixture and native CLI driver. Source
collector and qualification are synthetic, not automatic customer activation.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


native = load("native_driver", HERE / "native_claude.py")
pipeline = load("proxy_driver", native.ROOT / "tests/test_proxy/test_compact_edit_pipeline.py")
OriginalScenario = native.Scenario


class PipelineScenario(OriginalScenario):
    def __init__(self, work, case):
        super().__init__(work, case)
        self.patch = pytest.MonkeyPatch()
        self.harness = pipeline.Harness(work, self.patch, cache=True)
        self.harness.journal.close()
        self.harness.journal = self.journal
        self.harness.app.state.compact_edit_cohort_resolver = None
        self.bound = False
        self.provider_requests = []

    def provider(self, body):
        self.provider_requests.append(body)
        for message in body.get("messages", []):
            for block in (
                message.get("content", []) if isinstance(message.get("content"), list) else []
            ):
                if block.get("type") == "tool_result":
                    self.results[block["tool_use_id"]] = block
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
            definition = next(
                t for t in body["tools"] if t.get("name") == "horizon_compact_edit_v1"
            )
            props = definition["input_schema"]["properties"]
            args = {
                k: (v["items"]["enum"] if k == "keys" else v["enum"][0]) for k, v in props.items()
            }
            if self.case == "invalid_preview" and self.harness.cohort.recoveries == 0:
                args["receipt"] = "invalid-private-preview"
                self.harness.answers.append(self.provider)
            if body.get("tool_choice", {}).get("type") == "tool":
                call = {
                    "name": "Edit",
                    "input": {
                        "file_path": str(self.path),
                        "old_string": self.original,
                        "new_string": self.edit.after,
                        "replace_all": False,
                    },
                }
            else:
                call = {"name": "horizon_compact_edit_v1", "input": args}
            return self.message([dict(call, type="tool_use", id="edit_cohort")], "tool_use")
        return self.message([{"type": "text", "text": "DONE"}], "end_turn")

    def respond(self, body):
        self.requests.append(body)
        h = self.harness
        if not self.bound:
            from horizon.proxy.compact_edits import (
                CostBounds,
                NativeContract,
                Qualification,
                fingerprint,
            )
            from horizon.proxy.compact_edits.registry import ManagedClaudeRegistry

            definition = next(t for t in body["tools"] if t.get("name") == "Edit")
            self.contract = NativeContract("claude_edit", fingerprint(definition))
            self.scope = replace(self.scope, account=h.user)
            qualification = Qualification(
                "scripted-native-pipeline-only",
                self.scope.provider,
                self.scope.model,
                self.contract,
                self.scope.workspace,
                "catalog.py",
                str(self.path),
                "horizon-model-pricing-literals-v1",
                "exact_full_source_match",
                __import__("time").time() + 600,
                CostBounds(0.1, 0.01),
            )
            h.registry = ManagedClaudeRegistry(
                controller=h.controller, journal=self.journal, worker_processes=1
            )
            h.cohort = h.registry.bind(
                scope=self.scope,
                candidate=self.candidate,
                qualification=qualification,
                request_path="/p/fixture/v1/messages",
                cold_boundary_ready=True,
            )
            h.registry.install(h.app)
            self.bound = True
        h.answers.append(self.provider)
        request = dict(body, stream=False)
        reply = h.post(request, headers={"x-horizon-session-id": self.scope.conversation})
        if reply.status_code != 200:
            raise RuntimeError("full proxy rejected scripted workflow: " + str(reply.status_code))
        return reply.json()

    def summary(self, returncode, timed_out):
        h = self.harness
        rows = h.events()
        result = self.results.get("edit_cohort", {})
        passed = (
            self.path.read_bytes() == self.edit.after.encode()
            and bool(result)
            and not result.get("is_error")
            and returncode == 0
            and not timed_out
        )
        record = self.journal.get(self.scope.key, "call:edit_cohort")
        public = {
            "case": self.case,
            "passed": passed,
            "client_requests": len(self.requests),
            "provider_attempts": len(h.calls),
            "account_outbox_rows": len(rows),
            "recoveries": h.cohort.recoveries,
            "native_result_acknowledged": bool(record and record.get("native_result")),
            "provider_virtual_replay": any(
                b.get("name") == "horizon_compact_edit_v1"
                for item in (
                    self.provider_requests[-1].get("messages", []) if self.provider_requests else []
                )
                for b in (item.get("content", []) if isinstance(item.get("content"), list) else [])
            ),
            "external_model_calls": 0,
            "external_api_spend_usd": 0,
            "production_qualified": False,
        }
        self.patch.undo()
        h.client.close()
        asyncio.run(h.proxy.http_client.aclose())
        h.proxy._background_compression_executor.shutdown(wait=False)
        return public


def main():
    logging.disable(logging.CRITICAL)
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    native.Scenario = PipelineScenario
    root = (
        HERE
        / "runs"
        / ("native-pipeline-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    )
    root.mkdir(parents=True)
    binary = (
        Path(os.environ["APPDATA"]) / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
    )
    rows = []
    for case in ("compact_roundtrip", "invalid_preview"):
        row = native.run_case(binary, root, case)
        rows.append(row)
        print(json.dumps(row), flush=True)
    (HERE / "native_pipeline_results.json").write_text(json.dumps(rows, indent=2) + "\n")


if __name__ == "__main__":
    main()
