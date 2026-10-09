"""Offline source-slice feasibility experiment. No imports from application code.

Run from any directory with Python + tiktoken. Exact known entry points are
an oracle input; the coverage checks are NOT agent task-success measurements.
"""
from __future__ import annotations

import ast
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

import tiktoken

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
ENC = tiktoken.get_encoding("cl100k_base")

# Fixed before running: entry point, source scope, required support, factual
# literal strings. No retrieval is allowed to consult required support/facts.
TASKS = [
    dict(id="prewarm_eligibility", module="horizon.proxy.cache_keeper", entry="prewarm_body",
         scopes=["horizon.proxy.cache_keeper"], support=[],
         facts=['thinking.get("type") == "enabled"', 'warm["max_tokens"] = 0', 'copy.deepcopy(body)']),
    dict(id="keepalive_identity", module="horizon.proxy.cache_keeper", entry="group_of",
         scopes=["horizon.proxy.cache_keeper"], support=[],
         facts=['liveness_id[:200]', 'lowered.get("x-api-key")', 'hexdigest()[:24]']),
    dict(id="cache_region_pricing", module="horizon.pricing.counterfactual", entry="split_tokens",
         scopes=["horizon.pricing.counterfactual", "horizon.pricing.cache_ttl"],
         support=["CacheMix", "TokenSplit", "Region", "_coerce_int"],
         facts=['mix = mix.normalized()', 'min(tokens, mix.read)', 'uncached=rest - w5 - w1h']),
    dict(id="response_identity", module="horizon.proxy.conversation_savings", entry="savings_conversation_key",
         scopes=["horizon.proxy.conversation_savings", "horizon.proxy.output_savings_policy"],
         support=["_explicit_id", "_unwrap_response_create_body"],
         facts=['body.get("previous_response_id") or body.get("conversation")', '"codex_session_id"',
                'body.get("type") == "response.create"']),
    dict(id="stratification", module="horizon.proxy.output_savings_policy", entry="stratum_key",
         scopes=["horizon.proxy.output_savings_policy"], support=["input_bucket", "model_family"],
         facts=['_INPUT_BUCKETS = (2_000, 8_000, 32_000, 128_000)', 'return "xl"',
                '"tools" if has_tools else "notools"']),
    dict(id="schema_headline", module="horizon.proxy.tool_schema_savings_policy", entry="headline_tokens_saved",
         scopes=["horizon.proxy.tool_schema_savings_policy"], support=["tool_schema_saved_from_tags"],
         facts=['"tool_search_deferred_tokens"', 'not isinstance(tokens_saved, bool)',
                'max(0, base + tool_schema_saved_from_tags(tags))']),
    dict(id="ttl_break_even", module="horizon.pricing.cache_ttl", entry="ttl_breakeven_share",
         scopes=["horizon.pricing.cache_ttl"], support=[],
         facts=['{"5m": 1.25, "1h": 2.00}', 'CACHE_READ_MULTIPLIER = 0.10',
                '(w1h - w5) / (w1h - CACHE_READ_MULTIPLIER)']),
]


def count(text):
    return len(ENC.encode(text, disallowed_special=()))


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class Unit:
    module: str
    name: str
    node: ast.AST
    text: str
    start: int
    end: int


class Index:
    def __init__(self, modules):
        self.sources = {}
        self.units = {}
        self.aliases = {}
        self.preludes = {}
        for module in sorted(modules):
            source = ROOT.joinpath(*module.split('.')).with_suffix('.py').read_text(encoding='utf-8')
            self.sources[module] = source
            lines = source.splitlines(keepends=True)
            tree = ast.parse(source)
            self.aliases[module] = {}
            self.preludes[module] = []
            for n in tree.body:
                first = min([n.lineno] + [x.lineno for x in getattr(n, 'decorator_list', [])])
                text = ''.join(lines[first - 1:n.end_lineno])
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    self.units[(module, n.name)] = Unit(module, n.name, n, text, first, n.end_lineno)
                else:
                    # Preserve every module-level statement and docstring, rather
                    # than pretending static Name traversal proves global safety.
                    self.preludes[module].append(text)
                if isinstance(n, ast.ImportFrom) and n.module in modules:
                    for alias in n.names:
                        self.aliases[module][alias.asname or alias.name] = (n.module, alias.name)

    def closure(self, seed):
        selected = set()
        pending = [seed]
        while pending:
            key = pending.pop()
            if key in selected or key not in self.units:
                continue
            selected.add(key)
            unit = self.units[key]
            for node in ast.walk(unit.node):
                if isinstance(node, ast.Name):
                    local = (unit.module, node.id)
                    dep = local if local in self.units else self.aliases[unit.module].get(node.id)
                    if dep in self.units:
                        pending.append(dep)
        return selected

    def pack(self, selected):
        modules = sorted({m for m, _ in selected})
        parts = ["Exact source evidence. Omitted symbols remain retrievable by path and source hash.\n"]
        for module in modules:
            source_hash = sha(self.sources[module])
            parts.append(f"SOURCE {module.replace('.', '/')}.py SHA256 {source_hash}\n")
            parts.extend(self.preludes[module])
            for key in sorted((x for x in selected if x[0] == module), key=lambda x: self.units[x].start):
                unit = self.units[key]
                parts.append(f"\nSYMBOL {unit.name} LINES {unit.start}-{unit.end} EXACT\n{unit.text}")
            omitted = [name for mod, name in self.units if mod == module and (mod, name) not in selected]
            parts.append("\nAVAILABLE ON REQUEST: " + ', '.join(sorted(omitted)) + '\n')
        return '\n'.join(parts)


# Illustrative sensitivity profiles, NOT fetched current provider/model prices.
# Unit: USD per million tokens. These deliberately do not name a billed model.
PROFILES = {
    'anthropic_like_1h': dict(write=6.0, read=0.30, fresh=3.0, output=15.0),
    'openai_like_4x_cache_discount': dict(write=2.0, read=0.50, fresh=2.0, output=8.0),
}


def price(full, sliced, turns, rates, cold, retrievals=1, reread_fraction=0.10):
    """Complete illustrative request stream, including recovery overhead.

    Each task starts with one source pack plus 4096 stable instruction tokens.
    Per normal request, 256 input + 400 output tokens are appended and retained.
    Slicing has no model summary call; one extra retrieval cycle is charged by
    default: re-read the current context, emit 100 output tokens, fetch 10% of
    the full source, charge that source as fresh/write, retain everything.
    Retrieval occurs just after request 1; its tokens grow later contexts.
    """
    common = 4096
    baseline = 0.0
    treatment = 0.0
    retrieval_tokens = round(full * reread_fraction)
    for turn in range(turns):
        baseline += (full + common) * (rates['write'] if cold and turn == 0 else rates['read'])
        treatment += common * (rates['write'] if cold and turn == 0 else rates['read'])
        treatment += sliced * (rates['write'] if turn == 0 else rates['read'])
        # Ordinary progress: prior deltas read, newly appended input and output.
        shared = turn * (256 + 400) * rates['read'] + 256 * rates['fresh'] + 400 * rates['output']
        baseline += shared
        treatment += shared
        if turn > 0:
            treatment += retrievals * (retrieval_tokens + 100) * rates['read']
    for cycle in range(retrievals):
        treatment += (common + sliced + 656 + cycle * (retrieval_tokens + 100)) * rates['read']
        treatment += 100 * rates['output'] + retrieval_tokens * rates['write']
    return dict(baseline_usd=baseline / 1e6, treatment_usd=treatment / 1e6,
                saving_usd=(baseline - treatment) / 1e6,
                saving_pct=100 * (baseline - treatment) / baseline,
                retrieval_rounds=retrievals, retrieval_source_tokens_each=retrieval_tokens)


def main():
    started = time.perf_counter()
    idx = Index({m for task in TASKS for m in task['scopes']})
    rows = []
    for task in TASKS:
        seed = (task['module'], task['entry'])
        selected = idx.closure(seed)
        selected_text = idx.pack(selected)
        entry_text = idx.pack({seed})
        full_text = '\n'.join(f"FILE {m.replace('.', '/')}.py\n{idx.sources[m]}" for m in task['scopes'])
        full_tokens, selected_tokens = count(full_text), count(selected_text)
        # Strong control: the same statically selected source without receipts,
        # omission lists or protocol labels. No credit over an already narrow
        # reader should be inferred from whole-file comparisons.
        narrow_text = '\n'.join(''.join(idx.preludes[m]) + '\n' +
                      '\n'.join(idx.units[k].text for k in sorted(selected)
                                if k[0] == m) for m in sorted({m for m, _ in selected}))
        narrow_tokens = count(narrow_text)
        checks = {f"fact_{i + 1}": fact in selected_text for i, fact in enumerate(task['facts'])}
        checks.update({f"support_{name}": any(name == s for _, s in selected) for name in task['support']})
        entry_checks = {f"fact_{i + 1}": fact in entry_text for i, fact in enumerate(task['facts'])}
        entry_checks.update({f"support_{name}": name == task['entry'] for name in task['support']})
        # Certificate means byte identity + the enumerated static edges ONLY.
        exact_units = all(u.text in idx.sources[u.module] for u in (idx.units[k] for k in selected))
        hashes = {m: sha(idx.sources[m]) for m, _ in selected}
        changed_source = idx.sources[task['module']] + '\n# simulated later file change\n'
        invalidation_detected = hashes[task['module']] != sha(changed_source)
        row = dict(id=task['id'], entry=f"{seed[0]}:{seed[1]}", full_tokens=full_tokens,
                   narrow_control_tokens=narrow_tokens,
                   slice_tokens=selected_tokens, source_reduction_pct=100 * (1 - selected_tokens / full_tokens),
                   selected_symbols=[f"{m}:{name}" for m, name in sorted(selected)],
                   source_hashes=hashes, checks=checks, checks_passed=sum(checks.values()), checks_total=len(checks),
                   entry_only_checks_passed=sum(entry_checks.values()), exact_source=exact_units,
                   changed_file_invalidates_certificate=invalidation_detected, models={})
        for profile, rates in PROFILES.items():
            row['models'][profile] = {}
            for cold in (True, False):
                key = 'cold_boundary' if cold else 'warm_cache'
                row['models'][profile][key] = price(full_tokens, selected_tokens, 20, rates, cold)
                row['models'][profile][key]['break_even_turns_1_to_100'] = next(
                    (n for n in range(1, 101) if price(full_tokens, selected_tokens, n, rates, cold)['saving_usd'] > 0), None)
            row['models'][profile]['cold_boundary_three_retrievals'] = price(full_tokens, selected_tokens, 20, rates, True, 3)
        rows.append(row)
    totals = dict(full_tokens=sum(r['full_tokens'] for r in rows), slice_tokens=sum(r['slice_tokens'] for r in rows),
                  narrow_control_tokens=sum(r['narrow_control_tokens'] for r in rows),
                  checks_passed=sum(r['checks_passed'] for r in rows), checks_total=sum(r['checks_total'] for r in rows),
                  entry_only_checks_passed=sum(r['entry_only_checks_passed'] for r in rows))
    totals['source_reduction_pct'] = 100 * (1 - totals['slice_tokens'] / totals['full_tokens'])
    totals['receipt_overhead_vs_same_narrow_source_pct'] = 100 * (totals['slice_tokens']/totals['narrow_control_tokens'] - 1)
    totals['modeled_20_turn_costs'] = {}
    for profile in PROFILES:
        totals['modeled_20_turn_costs'][profile] = {}
        for mode in ('cold_boundary', 'warm_cache', 'cold_boundary_three_retrievals'):
            base = sum(r['models'][profile][mode]['baseline_usd'] for r in rows)
            treated = sum(r['models'][profile][mode]['treatment_usd'] for r in rows)
            totals['modeled_20_turn_costs'][profile][mode] = dict(baseline_usd=base, treatment_usd=treated,
                saving_usd=base-treated, saving_pct=100*(base-treated)/base)
    result = dict(method='cache-boundary exact source slices with dependency receipts',
                  evaluation='offline fixed-entrypoint oracle; no model task-success measurement',
                  tokenizer='cl100k_base; estimates for both providers, not provider token counts',
                  illustrative_rates_usd_per_1m=PROFILES, normal_turns_per_task=20,
                  observed_paid_provider_calls=0, local_runtime_seconds=time.perf_counter()-started,
                  totals=totals, tasks=rows)
    (OUT / 'results.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(totals=totals, local_runtime_seconds=result['local_runtime_seconds']), indent=2))
    assert totals['checks_passed'] == totals['checks_total']
    assert all(r['exact_source'] and r['changed_file_invalidates_certificate'] for r in rows)


if __name__ == '__main__':
    main()
