"""Fuse several exact selector reads into bounded, exact-text result blocks.

Every block carries source text copied from the immutable snapshot by physical
line number. Blocks are never re-serialized. A block may contain whole lines
that hold more than one requested scope; each requested scope is still listed
under the block's ``scopes`` with its own line span and selector, so the caller
can locate it inside ``text``.
"""

from __future__ import annotations

import json
from typing import Any

from .selectors import SelectorError, select_exact
from .text_lines import split_physical_lines

MAX_BATCH_REQUESTS = 16
COVERAGE = "exact selected scopes"
_REQUEST_KEYS = frozenset({"source_id", "selector"})


class BatchError(ValueError):
    """Whole-call batch failure identified by a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _serialize(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _validate_requests(requests: Any) -> None:
    if not isinstance(requests, list):
        raise BatchError("batch_invalid")
    if not 1 <= len(requests) <= MAX_BATCH_REQUESTS:
        raise BatchError("batch_invalid")
    for item in requests:
        if (
            not isinstance(item, dict)
            or set(item) != _REQUEST_KEYS
            or not isinstance(item["source_id"], str)
        ):
            raise BatchError("batch_invalid")


def _fits(
    payload: dict[str, Any],
    max_result_bytes: int,
    token_budget: int,
    estimator: Any | None,
) -> bool:
    text = _serialize(payload)
    if len(text.encode("utf-8")) > max_result_bytes:
        return False
    if estimator is None:
        return True
    # Mirrors policies.py: an estimator that fails or returns no usable count
    # leaves the token limit unapplied; the byte limit always applies.
    try:
        count = estimator.count_text(text)
    except Exception:
        return True
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return True
    return count <= token_budget


def _merge_spans(items: list[tuple[int, int, int, Any]]) -> list[list[Any]]:
    """Merge (start, end, index, scope) items that overlap or touch.

    Returns clusters of ``[start, end, members]`` with members in sorted order.
    """
    clusters: list[list[Any]] = []
    for start, end, index, scope in sorted(items, key=lambda entry: (entry[0], entry[1], entry[2])):
        if clusters and start <= clusters[-1][1] + 1:
            clusters[-1][1] = max(clusters[-1][1], end)
            clusters[-1][2].append((index, scope))
        else:
            clusters.append([start, end, [(index, scope)]])
    return clusters


def _build_blocks(
    requests: list[dict[str, Any]],
    scoped: dict[tuple[str, str], tuple[Any, list[tuple[int, int, int, Any]]]],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for (source_id, version), (snapshot, items) in scoped.items():
        lines = split_physical_lines(snapshot.text)
        for start, end, members in _merge_spans(items):
            members.sort(key=lambda member: member[0])
            blocks.append(
                {
                    "source_id": source_id,
                    "version": version,
                    "line_ranges": [{"start_line": start, "end_line": end}],
                    "text": "".join(lines[start - 1 : end]),
                    "requests": [index for index, _scope in members],
                    "scopes": [
                        {
                            "index": index,
                            "start_line": scope.line_start,
                            "end_line": scope.line_end,
                            "selector": requests[index]["selector"],
                        }
                        for index, scope in members
                    ],
                }
            )
    blocks.sort(key=lambda block: block["requests"][0])
    return blocks


def _deferred_entry(block: dict[str, Any], requests: list[dict[str, Any]], reason: str) -> dict:
    return {
        "source_id": block["source_id"],
        "version": block["version"],
        "line_ranges": block["line_ranges"],
        "requests": list(block["requests"]),
        "selectors": [
            {"index": index, "selector": requests[index]["selector"]} for index in block["requests"]
        ],
        "reason": reason,
    }


def fuse_reads(
    snapshots: Any,
    requests: Any,
    *,
    max_result_bytes: int,
    token_budget: int,
    estimator: Any | None = None,
) -> dict[str, Any]:
    """Resolve 1-16 exact selector reads into merged, budgeted text blocks.

    ``snapshots`` maps source_id to ``SourceSnapshot``. Whole-call problems raise
    ``BatchError``; per-item problems are listed under ``errors`` with the request
    index. Blocks are admitted in order of their first request index. A block
    that does not fit with the blocks already admitted is deferred (reason
    ``budget``), and later smaller blocks may still be admitted. A block that
    would not fit alone is deferred with reason ``too_large``.
    """
    _validate_requests(requests)

    errors: list[dict[str, Any]] = []
    scoped: dict[tuple[str, str], tuple[Any, list[tuple[int, int, int, Any]]]] = {}
    for index, item in enumerate(requests):
        snapshot = snapshots.get(item["source_id"])
        if snapshot is None:
            errors.append({"index": index, "code": "source_unknown"})
            continue
        try:
            scope = select_exact(
                snapshot.text,
                item["selector"],
                kind=snapshot.kind,
                blocks=snapshot.blocks,
            )
        except SelectorError as error:
            errors.append({"index": index, "code": error.code})
            continue
        key = (snapshot.source_id, snapshot.version)
        scoped.setdefault(key, (snapshot, []))[1].append(
            (scope.line_start, scope.line_end, index, scope)
        )

    blocks = _build_blocks(requests, scoped)

    def envelope(admitted: list[dict[str, Any]], deferred: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "mode": "batch",
            "blocks": admitted,
            "deferred": deferred,
            "errors": errors,
            "coverage": COVERAGE,
            "request_count": len(requests),
        }

    admitted: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for block in blocks:
        if not _fits(envelope([block], []), max_result_bytes, token_budget, estimator):
            deferred.append(_deferred_entry(block, requests, "too_large"))
            continue
        if _fits(envelope(admitted + [block], deferred), max_result_bytes, token_budget, estimator):
            admitted.append(block)
            continue
        deferred.append(_deferred_entry(block, requests, "budget"))

    result = envelope(admitted, deferred)
    # Deferral entries added after a block was admitted can push the envelope
    # over budget; give back the most recently admitted blocks first.
    while admitted and not _fits(result, max_result_bytes, token_budget, estimator):
        deferred.append(_deferred_entry(admitted.pop(), requests, "budget"))
        result = envelope(admitted, deferred)
    if not _fits(result, max_result_bytes, token_budget, estimator):
        # Only the bounded error and deferral lists can overflow here.
        raise BatchError("batch_result_too_small")
    return result


__all__ = ["BatchError", "COVERAGE", "MAX_BATCH_REQUESTS", "fuse_reads"]
