"""Offline, guarded edit-recipe benchmark. Never writes outside this directory.

Fixtures are hypothetical changes to copies of real repository sources; these
are not proposed production changes. No model/provider/API calls are made.
"""
from __future__ import annotations

import ast
import copy
import difflib
import hashlib
import io
import json
import math
from pathlib import Path
import tokenize

import tiktoken

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
ENC = tiktoken.get_encoding("cl100k_base")

SPEC = """guarded_edit receives {r:read_receipt,ops:[operations]}. A read receipt binds
an immutable file snapshot and is invalid after that file changes. Operations:
[rename,qualified_scope,old_identifier,new_identifier,expected_occurrences]
renames Python NAME tokens only within that scope; it is not symbol resolution.
[literal,qualified_scope,old_value,new_value,expected_occurrences] replaces exact
AST constants, preserving other bytes. [keyword,callee,keyword,old_value,new_value,
expected_occurrences] replaces exact literal keyword values in calls.
[replace_function,qualified_scope,new_source] replaces one complete function.
[clone_dict,variable,source_key,new_keys] clones a dictionary entry and changes
only its key and the model keyword equal to the old key. All targets must be
unique, counts exact, inserted keys absent, and output parseable; failures apply
nothing. Request an ordinary patch for unsupported edits. Read returns a receipt;
success returns changed ranges and a new receipt. No implicit fuzzy fallback.
"""


def tokens(text: str) -> int:
    return len(ENC.encode(text))


def wire(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def digest(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def scope_node(tree: ast.AST, scope: str) -> ast.AST:
    if scope == "*":
        return tree
    current = tree
    for name in scope.split("."):
        matches = [n for n in current.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name]
        if len(matches) != 1:
            raise ValueError("scope missing or ambiguous")
        current = matches[0]
    return current


def offsets(s: str) -> list[int]:
    out = [0]
    for line in s.splitlines(keepends=True):
        out.append(out[-1] + len(line))
    return out


def node_span(s: str, node: ast.AST) -> tuple[int, int]:
    lines = s.splitlines(keepends=True)
    ofs = offsets(s)
    # AST columns are UTF-8 byte offsets, unlike tokenize's character columns.
    start = len(lines[node.lineno - 1].encode()[:node.col_offset].decode())
    end = len(lines[node.end_lineno - 1].encode()[:node.end_col_offset].decode())
    return ofs[node.lineno - 1] + start, ofs[node.end_lineno - 1] + end


def splice(s: str, edits: list[tuple[int, int, str]]) -> str:
    ordered = sorted(edits, reverse=True)
    previous = len(s) + 1
    for start, end, replacement in ordered:
        if end > previous or start < 0 or end < start:
            raise ValueError("overlapping edits")
        s = s[:start] + replacement + s[end:]
        previous = start
    return s


def apply_operation(s: str, op: list) -> str:
    tree = ast.parse(s)
    edits = []
    kind = op[0]
    if kind in ("rename", "literal"):
        _, scope, old, new, expected = op
        target = scope_node(tree, scope)
        if kind == "literal":
            for n in ast.walk(target):
                if isinstance(n, ast.Constant) and type(n.value) is type(old) and n.value == old:
                    a, b = node_span(s, n)
                    edits.append((a, b, repr(new)))
        else:
            if not isinstance(new, str) or not new.isidentifier():
                raise ValueError("invalid identifier")
            a, b = (0, len(s)) if scope == "*" else node_span(s, target)
            ofs = offsets(s)
            for t in tokenize.generate_tokens(io.StringIO(s).readline):
                start = ofs[t.start[0] - 1] + t.start[1]
                end = ofs[t.end[0] - 1] + t.end[1]
                if t.type == tokenize.NAME and t.string == old and a <= start < b:
                    edits.append((start, end, new))
    elif kind == "keyword":
        _, callee, keyword, old, new, expected = op
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == callee:
                for kw in n.keywords:
                    if kw.arg == keyword and isinstance(kw.value, ast.Constant) and type(kw.value.value) is type(old) and kw.value.value == old:
                        a, b = node_span(s, kw.value)
                        edits.append((a, b, repr(new)))
    elif kind == "replace_function":
        _, scope, replacement = op
        target = scope_node(tree, scope)
        if not isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
            raise ValueError("not a function")
        a, b = node_span(s, target)
        edits.append((a,b,replacement))
        expected = 1
    elif kind == "clone_dict":
        _, variable, old_key, new_keys = op
        matches = [n.value for n in ast.walk(tree) if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == variable and isinstance(n.value, ast.Dict)]
        if len(matches) != 1:
            raise ValueError("dictionary missing or ambiguous")
        table = matches[0]
        keys = [n.value for n in table.keys if isinstance(n, ast.Constant)]
        if len(set(new_keys)) != len(new_keys) or any(k in keys for k in new_keys):
            raise ValueError("duplicate key")
        positions = [i for i, n in enumerate(table.keys) if isinstance(n, ast.Constant) and n.value == old_key]
        if len(positions) != 1:
            raise ValueError("source key missing or ambiguous")
        i = positions[0]
        key, value = table.keys[i], table.values[i]
        if not isinstance(value, ast.Call):
            raise ValueError("not a call value")
        kw = [n.value for n in value.keywords if n.arg == "model" and isinstance(n.value, ast.Constant) and n.value.value == old_key]
        if len(kw) != 1:
            raise ValueError("model keyword missing or ambiguous")
        start, key_end = node_span(s, key)
        _, end = node_span(s, value)
        ka, kb = node_span(s, kw[0])
        original = s[start:end]
        clones = []
        for new_key in new_keys:
            # Keep source quote style to match the independent expected fixture.
            replacement = json.dumps(new_key)
            clones.append("    " + splice(original, [(0, key_end - start, replacement), (ka - start, kb - start, replacement)]) + ",\n")
        point = node_span(s, table)[1] - 1
        edits.append((point, point, "".join(clones)))
        expected = 1
    else:
        raise ValueError("unsupported operation")
    if len(edits) != expected or expected <= 0:
        raise ValueError("occurrence count mismatch")
    out = splice(s, edits)
    ast.parse(out)
    return out


def expand(current: str, receipt_source: str, recipe: dict) -> str:
    if recipe.get("r") != "r0" or digest(current) != digest(receipt_source):
        raise ValueError("stale or unknown receipt")
    working = current
    for op in recipe["ops"]:
        working = apply_operation(working, op)
    return working


def native_patch(path: str, before: str, after: str) -> str:
    # The Codex patch syntax requires context lines when available, not line nums.
    diff = list(difflib.unified_diff(before.splitlines(True), after.splitlines(True), n=3))
    parts = ["*** Begin Patch\n", f"*** Update File: {path}\n"]
    for line in diff[2:]:
        parts.append("@@\n" if line.startswith("@@") else line)
    parts.append("*** End Patch\n")
    return "".join(parts)


def minimal_edit(path: str, before: str, after: str) -> str:
    # Valid exact old/new blocks with zero context where unique. Context expands
    # only until the old block is unique; this is a strong existing Edit baseline.
    a, b = before.splitlines(True), after.splitlines(True)
    blocks = []
    for tag, i, j, k, l in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        old, new = "".join(a[i:j]), "".join(b[k:l])
        while not old or before.count(old) != 1:
            if i > 0:
                i -= 1
                old = a[i] + old
                new = a[i] + new
            elif j < len(a):
                old += a[j]
                new += a[j]
                j += 1
            else:
                raise ValueError("no unique edit baseline")
        blocks.append({"old_string": old, "new_string": new})
    # Existing Claude Edit supports replace_all. Avoid overstating the codec's
    # advantage by comparing a repeated single-line change to N verbose hunks.
    raw = [("".join(a[i:j]), "".join(b[k:l])) for tag,i,j,k,l in difflib.SequenceMatcher(None,a,b,autojunk=False).get_opcodes() if tag != "equal"]
    if raw and all(pair == raw[0] for pair in raw) and raw[0][0] and before.replace(*raw[0]) == after:
        blocks = [{"old_string":raw[0][0],"new_string":raw[0][1],"replace_all":True}]
    return wire({"file_path": path, "edits": blocks})


def fixtures() -> list[dict]:
    cases = []
    def add(name, path, op, expected):
        before = (ROOT / path).read_text(encoding="utf-8")
        after = expected(before)
        cases.append(dict(name=name, path=path, before=before, after=after, recipe={"r":"r0", "ops":[op]}))
    security = "api/security.py"
    add("one_literal", security, ["literal", "new_session_token", 32, 48, 1], lambda s:s.replace("token_urlsafe(32)", "token_urlsafe(48)"))
    add("one_parameter", security, ["rename", "hash_token", "token", "session_token", 2], lambda s:s.replace("def hash_token(token:", "def hash_token(session_token:").replace("sha256(token.encode", "sha256(session_token.encode"))
    registry = "horizon/pricing/registry.py"
    source = (ROOT/registry).read_text(encoding="utf-8")
    target = scope_node(ast.parse(source), "PricingRegistry.estimate_cost")
    sa, sb = node_span(source,target)
    count = sum(1 for t in tokenize.generate_tokens(io.StringIO(source[sa:sb]).readline) if t.type == tokenize.NAME and t.string == "cached_input_tokens")
    def rename_expected(s):
        # Separate exact substitutions preserve the untouched documentation text.
        return s.replace("cached_input_tokens: int", "cache_read_tokens: int").replace("if cached_input_tokens >", "if cache_read_tokens >").replace("(cached_input_tokens /", "(cache_read_tokens /").replace('"tokens": cached_input_tokens', '"tokens": cache_read_tokens')
    add("local_parameter_multiple_uses",registry,["rename","PricingRegistry.estimate_cost","cached_input_tokens","cache_read_tokens",count],rename_expected)
    anthropic = "horizon/pricing/anthropic_prices.py"
    add("five_repeated_keyword_updates", anthropic,["keyword","ModelPricing","context_window",200000,260000,5],lambda s:s.replace("context_window=200_000", "context_window=260000"))
    def clones(s,n):
        anchor='    "claude-3-5-sonnet-latest": ModelPricing('
        start=s.index(anchor)
        end=s.index("    ),\n",start)+len("    ),\n")
        block=s[start:end]
        out="".join(block.replace("claude-3-5-sonnet-latest",f"fixture-sonnet-{i}") for i in range(n))
        return s.replace("\n}\n", "\n"+out+"}\n",1)
    for n in (1,4,12):
        add(f"clone_catalog_row_{n}",anthropic,["clone_dict","ANTHROPIC_PRICES","claude-3-5-sonnet-latest",[f"fixture-sonnet-{i}" for i in range(n)]],lambda s,n=n:clones(s,n))
    # Dense short-line additions are a negative case for metadata-heavy recipes.
    add("one_short_constant", "horizon/pricing/cache_ttl.py", ["literal","*",0.1,0.11,1],lambda s:s.replace("CACHE_READ_MULTIPLIER = 0.10", "CACHE_READ_MULTIPLIER = 0.11"))
    # A novel control-flow expression has no efficient codec primitive. Sending
    # the whole enclosing function repeats bytes and should lose to native diff.
    source=(ROOT/registry).read_text(encoding="utf-8")
    scope="PricingRegistry.estimate_cost"
    start,end=node_span(source,scope_node(ast.parse(source),scope))
    replacement=source[start:end].replace("if input_tokens > 0:", "if input_tokens > 0 and model.strip():")
    add("novel_logic_falls_back_to_full_function",registry,["replace_function",scope,replacement],lambda s:s.replace("if input_tokens > 0:", "if input_tokens > 0 and model.strip():"))
    return cases


def main():
    rows=[]
    for fixture in fixtures():
        before, after = fixture["before"], fixture["after"]
        expanded = expand(before, before, fixture["recipe"])
        if expanded != after:
            raise AssertionError(fixture["name"]+" exact expansion mismatch")
        ast.parse(expanded)
        payload = wire(fixture["recipe"])
        native = native_patch(fixture["path"],before,after)
        udiff = "".join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile=fixture["path"],tofile=fixture["path"],n=3))
        edit = minimal_edit(fixture["path"],before,after)
        receipt = wire({"r":"r0","file":fixture["path"]})
        baseline = min(tokens(native),tokens(udiff),tokens(edit))
        row={"case":fixture["name"],"path":fixture["path"],"source_sha256":digest(before),"after_sha256":digest(after),"recipe":fixture["recipe"],"exact_bytes":expanded==after,"ast_valid":True,"whole_file_tokens":tokens(after),"native_patch_tokens":tokens(native),"unified_diff_tokens":tokens(udiff),"minimal_search_replace_tokens":tokens(edit),"best_baseline_tokens":baseline,"recipe_output_tokens":tokens(payload),"receipt_input_tokens":tokens(receipt),"savings_vs_best_baseline_pct":round((1-tokens(payload)/baseline)*100,3)}
        rows.append(row)
        # Only artifacts under this directory; fixture source is public repo code.
        dest=HERE/"fixtures"/fixture["name"]
        dest.mkdir(parents=True,exist_ok=True)
        (dest/"before.py").write_text(before,encoding="utf-8")
        (dest/"after.py").write_text(after,encoding="utf-8")
        (dest/"recipe.json").write_text(payload+"\n",encoding="utf-8")
        (dest/"native.patch").write_text(native,encoding="utf-8")
        (dest/"minimal_edit.json").write_text(edit+"\n",encoding="utf-8")
    base=fixtures()[0]
    malformed=copy.deepcopy(base["recipe"])
    malformed["ops"][0][-1]=2
    malformed_unknown=copy.deepcopy(base["recipe"])
    malformed_unknown["ops"][0][0]="fuzzy"
    checks=[]
    for name, current, recipe in [("stale_snapshot",base["before"]+"\n",base["recipe"]),("wrong_count",base["before"],malformed),("unknown_opcode",base["before"],malformed_unknown)]:
        try:
            expand(current,base["before"],recipe)
        except ValueError as exc:
            checks.append({"case":name,"rejected":True,"reason":str(exc)})
        else:
            raise AssertionError(name+" was not rejected")
    total_baseline=sum(r["best_baseline_tokens"] for r in rows)
    total_codec=sum(r["recipe_output_tokens"] for r in rows)
    summary={"date":"2026-10-07","model_calls":0,"tokenizer":"cl100k_base (comparative proxy, not Claude billing tokenizer)","protocol_instruction_tokens":tokens(SPEC),"fixture_count":len(rows),"baseline_tokens":total_baseline,"codec_output_tokens":total_codec,"output_reduction_pct":round(100*(1-total_codec/total_baseline),3),"input_receipt_tokens":sum(r["receipt_input_tokens"] for r in rows),"checks":checks,"cases":rows,"limits":["Fixtures and edit intents hand authored; no proof a model emits valid recipes.","Selected favorable clone cases are not representative task frequency.","AST validity does not prove functional correctness.","Output savings require model adoption; post-hoc compression cannot refund output tokens.","Original sources are not modified; hypothetical pricing values are not recommendations."]}
    (HERE/"results.json").write_text(json.dumps(summary,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,indent=2))


if __name__ == "__main__":
    main()
