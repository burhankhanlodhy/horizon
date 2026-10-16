"""Small deterministic presentation transforms for approved source snapshots.

This module intentionally has no model or network dependencies.  Every compact
candidate is checked locally, and callers retain the immutable source snapshot
for exact recovery.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from horizon.desktop_mcp import json_table
from horizon.desktop_mcp.log_templates import (
    encode_log_templates,
    expand_log_templates,
    looks_like_log,
)

Fidelity = Literal[
    "byte_exact",
    "reversible_fold",
    "normalized_json_whitespace",
    "normalized_json_table",
    "normalized_log_ansi",
    "normalized_diff_metadata",
]

_RUN_MARKER = re.compile(r"^\.\.\. \(repeated \d+ times\)$", re.M)
_BLOCK_MARKER = re.compile(r"^\.\.\. \(repeats \d+ lines from \d+ lines back\)$", re.M)
COMPACT_ENCODING_VERSION = "contextshrink-compact-v1"

_DECODING_HINTS = {
    "json_whitespace": (
        "JSON whitespace between tokens was removed; string and number lexemes are retained. "
        "This is semantic JSON normalization, not byte recovery; use the original snapshot for exact text."
    ),
    "json_table": (
        "Lossless JSON table: values, key order and number spelling are exact; whitespace and string escapes "
        "are not kept, so use the original snapshot for exact text. Indent 2 spaces per level. "
        "`k: v` is a field; `k:` opens an object one level deeper; `k: {}` is empty. `k[N]: a,b` is an array "
        "of N values; `k[N]:` with `- item` lines one level deeper is a list. `k[N]{c1,c2}:` is a table of N "
        "rows one level deeper, cells in column order; an empty cell is an object or array, written as a field "
        "named by its column one level below the row. Top-level `[N]:` is an unnamed array. Bare text and keys "
        "are literal strings, double-quoted ones are JSON strings; null, true, false and numbers are literals."
    ),
    "log_runs": (
        "For each `... (repeated N times)` marker, emit N total copies of the immediately "
        "preceding line (N includes the visible line). ANSI color was removed only when fidelity says normalized_log_ansi."
    ),
    "log_templates": (
        "Lines `@k = <text>` at the top are a legend: entry k is <text>. `## date D` sets the date "
        "for the lines after it (D ends in T); a line `HH:MM:SS...` after it is the full timestamp D + "
        "that line, with no space added. A line ending ` @k` is its timestamp, one space, then entry k. "
        "Other lines are complete. Expand dates, then the legend; then, for each `... (repeated N times)` "
        "marker, emit N total copies of the preceding line. ANSI color, if any, was removed from the source "
        "before compaction; fidelity says normalized_log_ansi when that happened."
    ),
    "repeated_lines": (
        "For each `... (repeated N times)` marker, emit N total copies of the immediately "
        "preceding line (N includes the visible line)."
    ),
    "config_blocks": (
        "First expand each `... (repeats K lines from D lines back)` by copying K lines "
        "starting D lines back in the block-expanded stream, leaving repeated-line markers intact. "
        "Then expand each `... (repeated N times)` to N total copies of the preceding line."
    ),
    "search_paths": (
        "For `path:line:content` results, a file header supplies the path for following line rows; "
        "a directory header ending in `/` prefixes following `base:line:content` rows. "
        "A non-result row ends the active header."
    ),
    "path_prefixes": (
        "A directory header ending in `/` prefixes following basename rows; blank or non-path rows "
        "end the active header."
    ),
    "diff_index": (
        "`index` bookkeeping lines were removed. The result remains an applicable diff but cannot "
        "reconstruct those lines; use the original snapshot for exact text."
    ),
}


def compression_decoding_hint(transform: str | None, fidelity: str) -> str | None:
    """Return the static, body-free decoder contract for an applied transform."""
    hint = _DECODING_HINTS.get(transform or "")
    if hint is None:
        return None
    if fidelity == "normalized_log_ansi" and transform == "log_runs":
        return hint.replace(
            "ANSI color was removed only when fidelity says normalized_log_ansi.",
            "ANSI color was removed, so expansion recovers the ANSI-normalized source only.",
        )
    return hint


@dataclass(frozen=True)
class CompactOutcome:
    text: str
    applied: bool
    fidelity: Fidelity
    transform: str | None = None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _parse_json(text: str) -> object:
    return json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_float=Decimal,
        parse_constant=_reject_constant,
    )


def _minify_json_whitespace(text: str) -> str:
    """Remove insignificant JSON whitespace without changing token lexemes."""
    out: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            out.append(char)
        elif char not in " \t\r\n":
            out.append(char)
    return "".join(out)


def compact_json(text: str) -> CompactOutcome:
    """Prefer the lossless table form; otherwise minify JSON whitespace.

    The table form (``json_table``) is used only when decoding it reproduces the
    parsed source exactly (types, key order and number spelling included) and it
    is at least 15% smaller in UTF-8 bytes than the whitespace-minified text.
    Duplicate keys and non-standard numeric constants are rejected.  Both compact
    forms are labeled normalized because the original whitespace is not carried
    in the model-facing text.
    """
    try:
        before = _parse_json(text)
        candidate = _minify_json_whitespace(text)
        after = _parse_json(candidate)
    except (ValueError, TypeError, json.JSONDecodeError, RecursionError):
        return CompactOutcome(text, False, "byte_exact")
    if before != after:
        return CompactOutcome(text, False, "byte_exact")
    table: str | None
    try:
        table = json_table.encode(json_table.parse_lexical(text))
        if not json_table.same_json(json_table.decode(table), before):
            table = None
    except (ValueError, TypeError, RecursionError):
        table = None
    candidate_bytes = len(candidate.encode("utf-8"))
    if table is not None and len(table.encode("utf-8")) * 100 <= candidate_bytes * 85:
        return CompactOutcome(table, True, "normalized_json_table", "json_table")
    if candidate_bytes >= len(text.encode("utf-8")):
        return CompactOutcome(text, False, "byte_exact")
    return CompactOutcome(candidate, True, "normalized_json_whitespace", "json_whitespace")


def compact_text(text: str, kind: str) -> CompactOutcome:
    """Return a verified deterministic compact candidate for a known format."""
    if not isinstance(text, str) or not text:
        return CompactOutcome(text, False, "byte_exact")

    from horizon.transforms.lossless_compaction import (
        compact_lossless,
        diff_strip_index,
        expand_runs,
        path_unheading,
        search_dir_unheading,
        search_unheading,
        strip_ansi,
        unfold_repeated_blocks,
    )

    if kind == "json":
        return compact_json(text)

    # These helper inverses intentionally expand marker lines. A source can
    # contain a marker as ordinary data, so never feed such text to the
    # verifier where an attacker-controlled count could trigger huge output.
    marker_check_text = text
    if kind == "log":
        from horizon.transforms.lossless_compaction import strip_ansi

        marker_check_text = strip_ansi(text)
    if _RUN_MARKER.search(marker_check_text) or _BLOCK_MARKER.search(marker_check_text):
        return CompactOutcome(text, False, "byte_exact")

    candidate = compact_lossless(text, kind)

    if kind == "log":
        # Candidates: log_runs, and log_templates applied to the run result or
        # to the plain text. Each must reproduce the ANSI-normalized source
        # exactly; a template candidate must also save at least 15% of the raw
        # bytes. The smallest verified candidate wins.
        normalized = strip_ansi(text)
        fidelity: Fidelity = "normalized_log_ansi" if normalized != text else "reversible_fold"
        options: list[tuple[str, str]] = []
        runs_ok = candidate != text and (
            candidate == normalized or expand_runs(candidate) == normalized
        )
        if runs_ok:
            options.append((candidate, "log_runs"))
        template_limit = len(text.encode("utf-8")) * 85 // 100
        for base in ([candidate] if runs_ok else []) + [normalized]:
            encoded = encode_log_templates(base)
            if encoded is None or expand_runs(expand_log_templates(encoded)) != normalized:
                continue
            if len(encoded.encode("utf-8")) <= template_limit:
                options.append((encoded, "log_templates"))
        if not options:
            return CompactOutcome(text, False, "byte_exact")
        best, transform = min(options, key=lambda option: len(option[0].encode("utf-8")))
        return CompactOutcome(best, True, fidelity, transform)

    if candidate == text:
        return CompactOutcome(text, False, "byte_exact")

    if kind == "search":
        if search_unheading(candidate) == text or search_dir_unheading(candidate) == text:
            return CompactOutcome(candidate, True, "reversible_fold", "search_paths")
    elif kind == "paths":
        if path_unheading(candidate) == text:
            return CompactOutcome(candidate, True, "reversible_fold", "path_prefixes")
    elif kind == "text":
        if expand_runs(candidate) == text:
            return CompactOutcome(candidate, True, "reversible_fold", "repeated_lines")
    elif kind == "config":
        if expand_runs(unfold_repeated_blocks(candidate)) == text:
            return CompactOutcome(candidate, True, "reversible_fold", "config_blocks")
    elif kind == "diff":
        normalized = diff_strip_index(text)
        if candidate == normalized and len(candidate) < len(text):
            return CompactOutcome(candidate, True, "normalized_diff_metadata", "diff_index")

    return CompactOutcome(text, False, "byte_exact")


def infer_compaction_kind(kind: str, text: str) -> str | None:
    """Map the snapshot's allowlisted format labels to supported transforms."""
    normalized = kind.lower().lstrip(".")
    aliases = {
        "json": "json",
        "jsonl": "log",
        "ndjson": "log",
        "log": "log",
        "search": "search",
        "rg": "search",
        "grep": "search",
        "paths": "paths",
        "text": "text",
        "txt": "text",
        "md": "text",
        "markdown": "text",
        "yaml": "config",
        "yml": "config",
        "toml": "config",
        "config": "config",
        "ini": "config",
        "conf": "config",
        "diff": "diff",
        "patch": "diff",
    }
    # A .txt file is a log only when most of its non-blank lines start with a
    # timestamp; other .txt files keep the prose/text transform.
    if normalized == "txt" and looks_like_log(text):
        return "log"
    selected = aliases.get(normalized)
    if selected == "json" and not text.lstrip().startswith(("{", "[")):
        return None
    return selected


__all__ = [
    "COMPACT_ENCODING_VERSION",
    "CompactOutcome",
    "Fidelity",
    "compact_json",
    "compact_text",
    "compression_decoding_hint",
    "expand_log_templates",
    "infer_compaction_kind",
]
