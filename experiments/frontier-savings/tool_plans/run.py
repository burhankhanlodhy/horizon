"""Offline guarded-plan experiment; reads source text, never imports or executes it.

Costs are illustrative token-ledger scenarios, not paid provider observations.
Only JSON, AST traversal, substring search and slicing are supported operations.
"""
from __future__ import annotations

import ast
import hashlib
import json
import time
from pathlib import Path

import tiktoken

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
ENC = tiktoken.get_encoding("cl100k_base")
SOURCE_PATHS = [
    "horizon/proxy/conversation_savings.py",
    "horizon/proxy/tool_schema_savings_policy.py",
    "horizon/pricing/counterfactual.py",
    "horizon/proxy/cache_keeper.py",
    "horizon/proxy/savings_tracker.py",
]
TEST_PATHS = [
    "tests/test_proxy/test_conversation_savings.py",
    "tests/test_tool_schema_savings_policy.py",
    "tests/test_counterfactual_pricing.py",
    "tests/test_proxy/test_cache_keeper.py",
    "tests/test_savings_tracker.py",
]
SYMBOLS = [
    "savings_conversation_key", "tool_schema_saved_from_tags",
    "headline_tokens_saved", "split_tokens", "ttl_of", "group_of",
    "prewarm_body", "estimate_request_savings_usd",
]
RATES = {"input": 3.0, "cache_read": 0.30, "cache_write": 3.75, "output": 15.0}


def dump(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def tokens(value):
    return len(ENC.encode(value if isinstance(value, str) else dump(value)))


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Replan(Exception):
    pass


class ReadInterpreter:
    """A closed operation set over an in-memory snapshot of allowlisted files."""

    def __init__(self, files):
        self.files = files
        self.seen = {}
        self.manifest = sorted(files)

    def read(self, path):
        if path not in self.files:
            raise Replan("file_missing")
        text = self.files[path]
        current_hash = sha(text)
        if path in self.seen and self.seen[path] != current_hash:
            raise Replan("source_changed_during_plan")
        self.seen[path] = current_hash
        return text

    def declarations(self, path):
        return [n for n in ast.walk(ast.parse(self.read(path)))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def locate(self, symbol):
        matches = []
        for path in SOURCE_PATHS:
            for node in self.declarations(path):
                if node.name == symbol:
                    matches.append({"path": path, "line": node.lineno, "end": node.end_lineno, "name": node.name})
        if len(matches) != 1:
            raise Replan("missing_or_ambiguous_symbol")
        return matches[0]

    def excerpt(self, item):
        text = self.read(item["path"])
        return {**item, "text": "\n".join(text.splitlines()[item["line"] - 1:item["end"]])}

    def test_matches(self, symbol):
        matches = []
        for path in self.manifest:
            if not path.startswith("tests/"):
                continue
            text = self.read(path)
            lines = text.splitlines()
            for node in self.declarations(path):
                if node.name.startswith("test_") and symbol in "\n".join(lines[node.lineno - 1:node.end_lineno]):
                    matches.append({"path": path, "line": node.lineno, "end": node.end_lineno, "name": node.name})
        return sorted(matches, key=lambda x: (x["path"], x["line"]))

    def validate(self):
        if sorted(self.files) != self.manifest:
            raise Replan("file_manifest_changed")
        for path, old_hash in self.seen.items():
            if path not in self.files or sha(self.files[path]) != old_hash:
                raise Replan("source_changed_during_plan")
        return {"snapshot_sha256": sha(dump(self.seen)), "files_checked": len(self.seen),
                "guard": "all_read_hashes_and_allowlist_manifest_unchanged"}


def plan(symbol):
    return {"language": "read_plan_v1", "symbol": symbol, "allowlist": SOURCE_PATHS + TEST_PATHS,
            "steps": [{"id": "definition", "op": "locate_unique", "symbol": symbol},
                      {"id": "implementation", "op": "excerpt", "from": "definition"},
                      {"id": "tests", "op": "test_matches", "symbol": symbol},
                      {"id": "examples", "op": "excerpt_first", "from": "tests", "limit": 3}],
            "guard": "validate_hashes_and_manifest", "on_guard_failure": "request_model_replan"}


def execute(files, symbol, mutate=None):
    vm = ReadInterpreter(files)
    definition = vm.locate(symbol)
    implementation = vm.excerpt(definition)
    if mutate:
        mutate(files, definition)
    matches = vm.test_matches(symbol)
    examples = [vm.excerpt(item) for item in matches[:3]]
    receipt = vm.validate()
    evidence = {"definition": definition, "implementation": implementation,
                "tests": matches, "examples": examples}
    answer = {"symbol": symbol, "definition": definition,
              "test_declarations": [{"path": x["path"], "line": x["line"], "name": x["name"]} for x in matches],
              "scope": "explicit substring references in named test declarations; no claim that every relevant test is found"}
    outputs = [definition, implementation, matches, examples]
    calls = [{"op": "locate_unique", "symbol": symbol}, {"op": "excerpt", "from": definition},
             {"op": "test_matches", "symbol": symbol}, {"op": "excerpt_first", "from": matches[:3]}]
    return evidence, answer, receipt, outputs, calls


def execute_compiled(files, symbol, mutate=None):
    """Interpret a declarative DAG; no eval, shell, file writes or arbitrary calls."""
    program = plan(symbol)
    vm = ReadInterpreter(files)
    values = {}
    for step in program["steps"]:
        op = step["op"]
        if op == "locate_unique":
            value = vm.locate(step["symbol"])
        elif op == "excerpt":
            value = vm.excerpt(values[step["from"]])
        elif op == "test_matches":
            value = vm.test_matches(step["symbol"])
        elif op == "excerpt_first":
            value = [vm.excerpt(item) for item in values[step["from"]][:step["limit"]]]
        else:
            raise Replan("unsupported_operation")
        values[step["id"]] = value
        if mutate and step["id"] == "implementation":
            mutate(files, values["definition"])
    receipt = vm.validate()
    answer = {"symbol": symbol, "definition": values["definition"],
              "test_declarations": [{"path": x["path"], "line": x["line"], "name": x["name"]} for x in values["tests"]],
              "scope": "explicit substring references in named test declarations; no claim that every relevant test is found"}
    return values, answer, receipt


def simulate_cost(groups, final, base_tokens, caching):
    """Each group is one model tool-call turn followed by its tool results.

    A last model turn consumes the final results and produces the same answer.
    Include output tool calls, tool results as fresh input, full earlier history
    as cache reads (or full uncached on no-cache scenario), and common final output.
    Approximate protocol overhead: 12 tokens per assistant/tool message.
    """
    totals = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "requests": len(groups) + 1}
    previous = 0
    history = base_tokens
    incoming = 0
    for call, result in groups + [(final, None)]:
        prompt = history + incoming
        if caching:
            totals["cache_read"] += previous
            totals["cache_write"] += prompt - previous
        else:
            totals["input"] += prompt
        out = tokens(call) + 12
        totals["output"] += out
        previous = prompt
        history = prompt + out
        incoming = tokens(result) + 12 if result is not None else 0
    totals["usd"] = sum(totals[k] * RATES[k] / 1e6 for k in RATES)
    return totals


def run():
    started = time.perf_counter()
    files = {p: (ROOT / p).read_text(encoding="utf-8") for p in SOURCE_PATHS + TEST_PATHS if (ROOT / p).exists()}
    tasks = []
    for symbol in SYMBOLS:
        sequential = execute(dict(files), symbol)
        compiled = execute_compiled(dict(files), symbol)
        evidence, answer, receipt, outputs, calls = sequential
        assert dump(evidence) == dump(compiled[0]) and dump(answer) == dump(compiled[1])
        call_plan = plan(symbol)
        compiled_output = {"evidence": evidence, "receipt": receipt}
        modes = {
            "sequential": list(zip(calls, outputs)),
            # Locate definition and find tests are independent; excerpts require their results.
            "dependency_wave_batch": [([calls[0], calls[2]], [outputs[0], outputs[2]]),
                                      ([calls[1], calls[3]], [outputs[1], outputs[3]])],
            "guarded_plan": [(call_plan, compiled_output)],
            # Agent already knows how to use a single script: no inference turn advantage remains.
            "single_script_control": [({"read_only_program": "locate unique symbol; excerpt definition; find test declarations; excerpt first 3; emit evidence", "symbol": symbol}, evidence)],
        }
        costs = {}
        for prefix in (2000, 20000, 100000):
            for caching in (True, False):
                label = f"prefix_{prefix}_{'warm_after_first' if caching else 'uncached'}"
                # Count task input and common tool instructions in every mode's base.
                task_prompt = tokens({"task": "Return the symbol's implementation and explicitly referencing test declarations with three exact test bodies.", "symbol": symbol})
                costs[label] = {name: simulate_cost(groups, answer, prefix + task_prompt, caching) for name, groups in modes.items()}
        tasks.append({"symbol": symbol, "exact_evidence_equal": evidence == compiled[0], "exact_answer_equal": answer == compiled[1],
                      "evidence_tokens": tokens(evidence), "plan_tokens": tokens(call_plan), "receipt_tokens": tokens(receipt),
                      "test_declarations": len(evidence["tests"]), "costs": costs})

    guards = []
    mutations = [
        ("source_edited_mid_plan", lambda f, d: f.__setitem__(d["path"], f[d["path"]] + "\n# simulated concurrent edit\n")),
        ("source_deleted_mid_plan", lambda f, d: f.pop(d["path"])),
        ("new_test_file_mid_plan", lambda f, d: f.__setitem__("tests/test_new_in_memory.py", "def test_ttl_of_new():\n    pass\n")),
    ]
    for name, mutation in mutations:
        try:
            execute_compiled(dict(files), "ttl_of", mutation)
            guards.append({"case": name, "blocked": False})
        except Replan as e:
            guards.append({"case": name, "blocked": True, "reason": str(e), "emitted_answer": False})
    for name, symbol, edit in [
        ("unknown_symbol", "__not_a_symbol__", lambda f: None),
        ("duplicate_symbol_requires_choice", "ttl_of", lambda f: f.__setitem__(SOURCE_PATHS[0], f[SOURCE_PATHS[0]] + "\ndef ttl_of(value):\n    return 1\n")),
    ]:
        altered = dict(files)
        edit(altered)
        try:
            execute_compiled(altered, symbol)
            guards.append({"case": name, "blocked": False})
        except Replan as e:
            guards.append({"case": name, "blocked": True, "reason": str(e), "emitted_answer": False})
    assert all(x["blocked"] for x in guards)

    aggregates = {}
    for scenario in tasks[0]["costs"]:
        by_mode = {m: sum(t["costs"][scenario][m]["usd"] for t in tasks) for m in tasks[0]["costs"][scenario]}
        plan_cost = by_mode["guarded_plan"]
        aggregates[scenario] = {"costs_usd": by_mode,
            "plan_reduction_vs_sequential_pct": 100 * (1 - plan_cost / by_mode["sequential"]),
            "plan_reduction_vs_wave_batch_pct": 100 * (1 - plan_cost / by_mode["dependency_wave_batch"]),
            "plan_reduction_vs_single_script_pct": 100 * (1 - plan_cost / by_mode["single_script_control"])}

    # A guard failure adds a charged failed plan request + terse result + complete retry.
    # Include a 700-token hypothetical extra planning/reasoning allowance separately.
    scenario = "prefix_20000_warm_after_first"
    sensitivity = {}
    base = aggregates[scenario]["costs_usd"]
    overhead_per_task = 700 * RATES["output"] / 1e6
    for failure_probability in (0, .05, .20, .50, 1):
        # Conservative fallback: pay full plan cost and then full sequential cost.
        modeled = base["guarded_plan"] + len(tasks) * overhead_per_task + failure_probability * base["sequential"]
        sensitivity[str(failure_probability)] = {"expected_usd": modeled,
            "reduction_vs_wave_batch_pct": 100 * (1 - modeled / base["dependency_wave_batch"]),
            "reduction_vs_sequential_pct": 100 * (1 - modeled / base["sequential"])}
    result = {"method": "guarded evidence-carrying tool plans", "measurement": "offline token counts and arithmetic; no model or provider calls",
              "tokenizer": ENC.name, "hypothetical_usd_per_million": RATES,
              "files": {p: {"sha256": sha(text), "bytes_utf8": len(text.encode())} for p, text in files.items()},
              "tasks": tasks, "guards": guards, "aggregate": aggregates,
              "failure_and_700_output_token_sensitivity": sensitivity, "runtime_seconds": time.perf_counter() - started}
    (HERE / "results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"tasks": len(tasks), "equality": sum(t["exact_evidence_equal"] and t["exact_answer_equal"] for t in tasks),
                      "guards": guards, "aggregate": aggregates, "sensitivity": sensitivity}, indent=2))


if __name__ == "__main__":
    run()
