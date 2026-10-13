"""Trusted source indexes for smaller native Read calls on explicitly named symbols.

The model still reads original source bytes and can expand the read. No tool
arguments/results are intercepted, no code is summarized, and no content is
silently truncated. Callers pin a plan for a fresh task/conversation.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

SENTINEL = "<horizon_symbol_reads>"


def build_symbol_read_plan(root: Path, relative_path: str, symbol: str | list[str]) -> dict:
    root = root.resolve()
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Source must be a file inside the explicit codebase root")
    raw = path.read_bytes()
    if len(raw) > 2_000_000:
        raise ValueError("Source exceeds symbol-index limit")
    source = raw.decode("utf-8-sig")
    tree = ast.parse(source)
    definitions = {
        n.name: n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    # A method request returns the original owner class, preserving class-level
    # bindings, decorators and sibling behavior without synthesizing a skeleton.
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    definitions[f"{node.name}.{member.name}"] = node
    requested = list(dict.fromkeys([symbol] if isinstance(symbol, str) else symbol))
    if not requested:
        raise ValueError("At least one symbol is required")
    for name in requested:
        if name not in definitions:
            raise ValueError(f"No top-level symbol {name!r} in {relative_path}")
    bindings = dict(definitions)
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store):
                        bindings[name.id] = node
    needed = set()
    pending = requested.copy()
    referenced = set()
    while pending:
        name = pending.pop()
        if name in needed:
            continue
        needed.add(name)
        names = {
            n.id
            for n in ast.walk(bindings[name])
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        referenced |= names
        pending.extend(names & bindings.keys() - needed)
    nodes = list({id(bindings[name]): bindings[name] for name in sorted(needed)}.values())
    start = min(
        min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])]) for n in nodes
    )
    end = max(n.end_lineno for n in nodes)
    imports = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
            (alias.asname or alias.name.split(".")[0]) in referenced for alias in node.names
        ):
            imports.append(ast.get_source_segment(source, node))
    text = (
        f"{SENTINEL}\nTrusted index for this task: {relative_path}, symbols {', '.join(requested)}; "
        f"definitions/dependencies occupy one-based lines {start}-{end}. "
        f"Start with Read(file_path={relative_path!r}, offset={start}, limit={end - start + 1}) "
        "instead of reading the entire file. The range contains original source. "
        "Expand the read if more context is needed for correctness. "
        "Referenced import statements: "
        + ("; ".join(imports) or "none")
        + "\n</horizon_symbol_reads>"
    )
    return {
        "path": relative_path,
        "symbol": requested[0] if len(requested) == 1 else ", ".join(requested),
        "symbols": requested,
        "offset": start,
        "limit": end - start + 1,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "text": text,
        "ranges": _merge_ranges(
            [
                (
                    min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])]),
                    n.end_lineno,
                )
                for n in nodes
            ]
        ),
        "imports": imports,
    }


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out = []
    for start, end in sorted(ranges):
        if out and start <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(end, out[-1][1]))
        else:
            out.append((start, end))
    return out


def read_symbol_context(root: Path, relative_path: str, symbol: str | list[str]) -> dict:
    """Return current original source for the definition and module dependencies.

    Refreshes the AST every call, never imports or executes the source module.
    Returns disjoint spans, with one-based locations, rather than a summary.
    """
    plan = build_symbol_read_plan(root, relative_path, symbol)
    path = (root.resolve() / relative_path).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Source must remain inside the explicit codebase root")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != plan["source_sha256"]:
        raise ValueError("Source changed while reading; retry the source read")
    lines = raw.decode("utf-8-sig").splitlines(keepends=True)
    return {
        "path": relative_path,
        "symbol": plan["symbol"],
        "symbols": plan["symbols"],
        "source_sha256": plan["source_sha256"],
        "imports": plan["imports"],
        "ranges": [
            {"start": start, "end": end, "source": "".join(lines[start - 1 : end])}
            for start, end in plan["ranges"]
        ],
        "scope": "Named symbol and local bindings only; expand for other context or a whole-file review.",
    }


def apply_symbol_read_plan(body: dict[str, Any], text: str | None) -> bool:
    """Append a pinned trusted plan without changing any tool schemas/arguments."""
    if not text:
        return False
    system = body.get("system")
    if isinstance(system, str):
        if text in system:
            return False
        body["system"] = system + "\n\n" + text
    elif system is None or isinstance(system, list):
        blocks = system or []
        if any(isinstance(b, dict) and text == b.get("text") for b in blocks):
            return False
        body["system"] = [*blocks, {"type": "text", "text": text}]
    else:
        return False
    return True


def standalone_main() -> int:
    """Entrypoint used by the dependency-free project-local reader export."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Read exact Python symbol source and dependencies")
    default_root = (
        Path(__file__).resolve().parent.parent
        if Path(__file__).parent.name == ".horizon"
        else Path.cwd()
    )
    parser.add_argument("--repo", type=Path, default=default_root)
    parser.add_argument("--file", required=True)
    parser.add_argument(
        "--symbol",
        required=True,
        action="append",
        help="Repeat to read several symbols with shared dependencies once",
    )
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args()
    try:
        result = read_symbol_context(args.repo, args.file, args.symbol)
    except (ValueError, OSError, SyntaxError, UnicodeError) as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "fallback": "Use native search/read tools for the required context.",
                }
            )
        )
        return 2
    if args.format == "text":
        import sys

        # No escaped-source envelope: emit original UTF-8 even on Windows.
        parts = ["<horizon-symbol-source>\n", f"{result['path']} :: {result['symbol']}\n"]
        parts.extend(value + "\n" for value in result["imports"])
        for span in result["ranges"]:
            parts.append(f"\n# lines {span['start']}-{span['end']}\n")
            parts.append(span["source"])
        parts.append("\n</horizon-symbol-source>\n")
        sys.stdout.buffer.write("".join(parts).encode("utf-8"))
        return 0
    # ASCII JSON also works when a Windows shell pipes through a legacy code
    # page. Decoding JSON reconstructs the original Unicode source exactly.
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(standalone_main())
