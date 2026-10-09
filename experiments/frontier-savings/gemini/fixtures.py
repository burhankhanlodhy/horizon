"""Prepare/grade isolated Gemini tool payloads; never receives provider credentials."""

from __future__ import annotations

import argparse
import ast
import copy
import io
import json
import shlex
import sys
import tokenize
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from horizon.proxy.compact_edits import (  # noqa: E402
    Candidate,
    SourceSnapshot,
    compile_candidate,
    fingerprint,
)
from horizon.proxy.compact_edits.wire import virtual_definition  # noqa: E402

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
RESULTS = HERE / "results.json"


def candidate(count):
    source = (ROOT / "horizon/pricing/anthropic_prices.py").read_text(encoding="utf-8")
    return Candidate(
        SourceSnapshot("catalog.py", "catalog.py", source),
        "ANTHROPIC_PRICES",
        "claude-3-5-sonnet-latest",
        tuple(f"fixture-gemini-{i}" for i in range(count)),
    )


def receipt(cand):
    return fingerprint({"experiment": "gemini-payload-only-v1", "candidate": asdict(cand)})


def prepare():
    RUNS.mkdir(exist_ok=True)
    specs = {}
    for count in (4, 12):
        cand = candidate(count)
        read = {
            "type": "function",
            "function": {
                "name": "Read",
                "description": "Read the complete isolated catalog.py fixture.",
                "parameters": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                    "required": ["file_path"],
                },
            },
        }
        edit = {
            "type": "function",
            "function": {
                "name": "Edit",
                "description": "Replace one exact unique old_string in catalog.py. Preserve all unrelated bytes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                    },
                    "required": ["file_path", "old_string", "new_string"],
                },
            },
        }
        bash = {
            "type": "function",
            "function": {
                "name": "Bash",
                "description": "Run a Python-only transformation of catalog.py. Use python -c with a shell-quoted script; no other shell commands. Stdlib only. Read/write only catalog.py.",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        }
        virtual = virtual_definition(
            "codex_function_patch",
            receipt(cand),
            cand.table,
            cand.template,
            cand.keys,
            path="catalog.py",
            source_sha256=cand.snapshot.sha256,
        )
        virtual = {
            "type": "function",
            "function": {key: value for key, value in virtual.items() if key != "type"},
        }
        base = [
            {
                "role": "system",
                "content": "Edit only the isolated catalog.py source. The preceding Read result is complete. Preserve all existing code, comments and row order. Append exactly the requested rows at the dictionary end. Change only each clone's dictionary key and model string. Select one tool call to complete the requested change; no commentary or unrelated operations.",
            },
            {"role": "user", "content": "Read catalog.py first."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "read-fixture",
                        "type": "function",
                        "function": {"name": "Read", "arguments": '{"file_path":"catalog.py"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "read-fixture", "content": cand.snapshot.source},
        ]
        for arm in ("edit", "compact", "script"):
            instruction = {
                "edit": "Use the native Edit tool for this comparison.",
                "compact": "Use horizon_compact_edit_v1 for this supported clone operation.",
                "script": "Use Bash with a Python script for this comparison. Do not execute the source module or its imports.",
            }[arm]
            body = {
                "messages": base
                + [
                    {
                        "role": "user",
                        "content": f"In {cand.table}, clone {cand.template} into {json.dumps(cand.keys)}. "
                        + instruction,
                    }
                ],
                "tools": [read, edit, bash] + ([virtual] if arm == "compact" else []),
                "tool_choice": "auto",
                "parallel_tool_calls": False,
                "stream": False,
                "max_tokens": 4096,
                "temperature": 0,
            }
            specs[f"{count}:{arm}"] = body
    (RUNS / "specs.json").write_text(json.dumps(specs), encoding="utf-8")
    print(json.dumps({"prepared": len(specs), "credential_received": False}))


def independent_expected(cand):
    tree = ast.parse(cand.snapshot.source)
    table = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == cand.table
    )
    template = next(
        value
        for key, value in zip(table.keys, table.values, strict=True)
        if isinstance(key, ast.Constant) and key.value == cand.template
    )
    for key in cand.keys:
        clone = copy.deepcopy(template)
        next(kw for kw in clone.keywords if kw.arg == "model").value = ast.Constant(key)
        table.keys.append(ast.Constant(key))
        table.values.append(clone)
    return ast.dump(tree, include_attributes=False)


def comment_list(source):
    return [
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]


def script_from_command(command):
    parts = shlex.split(command)
    if len(parts) == 3 and parts[:2] in (["python", "-c"], ["python3", "-c"]):
        return parts[2]
    raise ValueError("only a single python -c script can be reviewed")


def grade():
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    for row in results["rows"]:
        if row.get("phase") != "fixture" or row.get("status") != 200:
            continue
        cand = candidate(row["count"])
        raw_path = RUNS / row["id"] / "response.json"
        message = json.loads(raw_path.read_text(encoding="utf-8"))["choices"][0]["message"]
        calls = message.get("tool_calls", [])
        row["passed"] = False
        row["script_pending_review"] = False
        try:
            if len(calls) != 1:
                raise ValueError("one tool call required")
            tool = calls[0]["function"]
            row["selected_tool"] = tool["name"]
            args = json.loads(tool["arguments"])
            actual = None
            if tool["name"] == "Edit":
                if args.get("file_path") != "catalog.py" or set(args) != {
                    "file_path",
                    "old_string",
                    "new_string",
                }:
                    raise ValueError("unexpected native edit arguments")
                old = args["old_string"]
                if not isinstance(old, str) or not old or cand.snapshot.source.count(old) != 1:
                    raise ValueError("native exact match not unique")
                actual = cand.snapshot.source.replace(old, args["new_string"], 1)
            elif tool["name"] == "horizon_compact_edit_v1":
                if args != {
                    "receipt": receipt(cand),
                    "table": cand.table,
                    "template": cand.template,
                    "keys": list(cand.keys),
                }:
                    raise ValueError("compact arguments do not match bound intent")
                actual = compile_candidate(cand).after
            elif tool["name"] == "Bash":
                if set(args) != {"command"}:
                    raise ValueError("unexpected script arguments")
                script = script_from_command(args["command"])
                ast.parse(script)
                (raw_path.parent / "script.py").write_text(script, encoding="utf-8")
                row["script_sha256"] = fingerprint(script)
                row["script_pending_review"] = True
                actual_path = raw_path.parent / "catalog.py"
                marker = raw_path.parent / "reviewed.json"
                if (
                    marker.exists()
                    and actual_path.exists()
                    and json.loads(marker.read_text())["script_sha256"] == fingerprint(script)
                ):
                    actual = actual_path.read_text(encoding="utf-8")
                    row["script_pending_review"] = False
            else:
                raise ValueError("unsupported tool selection")
            if actual is not None:
                row["ast_matches"] = ast.dump(
                    ast.parse(actual), include_attributes=False
                ) == independent_expected(cand)
                row["comments_match"] = comment_list(actual) == comment_list(cand.snapshot.source)
                row["passed"] = row["ast_matches"] and row["comments_match"]
        except (ValueError, KeyError, TypeError, SyntaxError, tokenize.TokenError) as exc:
            row["validation_error"] = str(exc)[:160]
    RESULTS.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            [
                {
                    key: row.get(key)
                    for key in (
                        "id",
                        "model",
                        "arm",
                        "count",
                        "passed",
                        "selected_tool",
                        "script_pending_review",
                        "cost_upper_usd",
                        "validation_error",
                    )
                }
                for row in results["rows"]
            ]
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "grade"))
    args = parser.parse_args()
    (prepare if args.mode == "prepare" else grade)()
