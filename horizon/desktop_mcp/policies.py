"""Bounded source presentation policies for the local Desktop MCP profile."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Literal

from .compression import (
    COMPACT_ENCODING_VERSION,
    CompactOutcome,
    compact_text,
    compression_decoding_hint,
    infer_compaction_kind,
)
from .extractors import DOCUMENT_KINDS
from .find import block_text, build_blocks, match_lines, parse_terms
from .retrieval import Passage, rank_passages
from .selectors import SelectedScope, markdown_headings
from .snapshots import SourceSnapshot
from .text_lines import split_physical_lines

POLICY_VERSION = "desktop-presentation-v1"
PresentationMode = Literal["exact", "compact", "focused", "overview", "find"]
FIND_CONTEXT_LINES_MAX = 50


class PolicyError(ValueError):
    """Sanitized policy failure with a stable code and safe metadata."""

    def __init__(self, code: str, metadata: dict[str, Any] | None = None) -> None:
        self.code = code
        self.metadata = metadata or {}
        super().__init__(code)


class CursorSigner:
    """Issue short, opaque, worker-local cursor capabilities.

    Cursor state stays in a small bounded map. Tokens carry only a random
    nonce and MAC, never source paths, queries, selectors, or other payload.
    """

    def __init__(self, secret: bytes | None = None, *, max_states: int = 1024) -> None:
        self._secret = secret or secrets.token_bytes(32)
        self._max_states = max_states
        self._states: OrderedDict[str, tuple[dict[str, Any], float]] = OrderedDict()

    def encode(self, payload: dict[str, Any]) -> str:
        if not isinstance(payload, dict) or self._max_states < 1:
            raise PolicyError("cursor_invalid")
        nonce = secrets.token_urlsafe(18)
        raw = nonce.encode("ascii")
        signature = hmac.new(self._secret, raw, hashlib.sha256).digest()[:18]
        token = nonce + "." + base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
        self._states[nonce] = (dict(payload), time.monotonic())
        self._states.move_to_end(nonce)
        while len(self._states) > self._max_states:
            self._states.popitem(last=False)
        return token

    def decode(self, cursor: str) -> dict[str, Any]:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 128:
            raise PolicyError("cursor_invalid")
        parts = cursor.split(".")
        if (
            len(parts) != 2
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", parts[0])
            or not re.fullmatch(r"[A-Za-z0-9_-]{24}", parts[1])
        ):
            raise PolicyError("cursor_invalid") from None
        nonce, supplied_signature = parts
        expected_signature = (
            base64.urlsafe_b64encode(
                hmac.new(self._secret, nonce.encode("ascii"), hashlib.sha256).digest()[:18]
            )
            .decode("ascii")
            .rstrip("=")
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise PolicyError("cursor_invalid")
        state = self._states.get(nonce)
        if state is None:
            raise PolicyError("cursor_invalid")
        self._states.move_to_end(nonce)
        return dict(state[0])


@dataclass(frozen=True)
class ResultLimits:
    token_budget: int = 32000
    max_result_bytes: int = 262144

    def __post_init__(self) -> None:
        if isinstance(self.token_budget, bool) or not 128 <= self.token_budget <= 64000:
            raise ValueError("token_budget must be between 128 and 64000")
        if isinstance(self.max_result_bytes, bool) or self.max_result_bytes < 1024:
            raise ValueError("max_result_bytes must be at least 1024")


def query_digest(query: str | None) -> str:
    return hashlib.sha256((query or "").encode("utf-8")).hexdigest()


def _serialize(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _measure_tokens(payload: dict[str, Any], estimator: Any | None) -> int | None:
    if estimator is None:
        return None
    text = _serialize(payload)
    try:
        value = estimator.count_text(text)
    except Exception:
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _fit(payload: dict[str, Any], limits: ResultLimits, estimator: Any | None) -> bool:
    if len(_serialize(payload).encode("utf-8")) > limits.max_result_bytes:
        return False
    count = _measure_tokens(payload, estimator)
    return count is None or count <= limits.token_budget


def _fit_reserved(payload: dict[str, Any], limits: ResultLimits, estimator: Any | None) -> bool:
    """Conservative pre-accounting fit; the chosen result is finalized and rechecked."""
    byte_reserve = min(1024, max(128, limits.max_result_bytes // 4))
    token_reserve = min(256, max(32, limits.token_budget // 4))
    if len(_serialize(payload).encode("utf-8")) > limits.max_result_bytes - byte_reserve:
        return False
    count = _measure_tokens(payload, estimator)
    return count is None or count <= limits.token_budget - token_reserve


def _accounted_fits(payload: dict[str, Any], limits: ResultLimits, estimator: Any | None) -> bool:
    result = payload.get("accounting", {}).get("result", {})
    delivered_bytes = result.get("delivered_utf8_bytes")
    delivered_tokens = result.get("delivered_token_estimate")
    if isinstance(delivered_bytes, int) and delivered_bytes > limits.max_result_bytes:
        return False
    if (
        estimator is not None
        and isinstance(delivered_tokens, int)
        and delivered_tokens > limits.token_budget
    ):
        return False
    if not isinstance(delivered_bytes, int):
        return _fit(payload, limits, estimator)
    return True


def _is_document(snapshot: SourceSnapshot) -> bool:
    return isinstance(snapshot.kind, str) and snapshot.kind.lower().lstrip(".") in DOCUMENT_KINDS


def _document_coverage(snapshot: SourceSnapshot, coverage: str) -> str:
    """Name extracted text and any omitted content; text snapshots are unchanged.

    This is the single place where document coverage wording is derived, so every
    result path (open, retrieve, focused, overview) reports omissions the same way.
    """
    if not _is_document(snapshot):
        return coverage
    text = coverage
    if text == "complete source snapshot":
        text = "complete extracted text"
    elif text == "exact selected lines at immutable snapshot version":
        text = "exact selected extracted text at immutable snapshot version"
    if snapshot.warnings:
        text += "; omitted: " + ", ".join(snapshot.warnings)
    return text


def _extraction_metadata(snapshot: SourceSnapshot) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for block in snapshot.blocks:
        counts[block.kind] = counts.get(block.kind, 0) + 1
    return {
        "extractor_version": snapshot.extractor_version,
        "warnings": list(snapshot.warnings),
        "block_counts": dict(sorted(counts.items())),
    }


def _document_inventory(snapshot: SourceSnapshot) -> tuple[list[dict[str, Any]], str]:
    """Overview items for documents: PDF pages, or DOCX headings in document order."""
    if snapshot.kind.lower().lstrip(".") == "pdf":
        items: list[dict[str, Any]] = []
        for block in snapshot.blocks:
            if block.kind != "page":
                continue
            has_text = block.line_start <= block.line_end
            items.append(
                {
                    "page": block.index,
                    "line": block.line_start if has_text else None,
                    "has_text": has_text,
                }
            )
        return items, "page"
    lines = split_physical_lines(snapshot.text)
    items = []
    for block in snapshot.blocks:
        if block.kind != "heading":
            continue
        first = lines[block.line_start - 1] if 1 <= block.line_start <= len(lines) else ""
        items.append(
            {
                "line": block.line_start,
                "heading": first.strip()[:200],
                "heading_level": block.attributes.get("heading_level"),
            }
        )
    return items, "heading"


def _base_payload(snapshot: SourceSnapshot, mode: str, text: str) -> dict[str, Any]:
    line_count = max(1, len(split_physical_lines(snapshot.text)))
    payload: dict[str, Any] = {
        "source_id": snapshot.source_id,
        "version": snapshot.version,
        "kind": snapshot.kind,
        "mode": mode,
        "coverage": _document_coverage(snapshot, "complete source snapshot"),
        "selected_lines": {"start": 1, "end": line_count},
        "line_count": line_count,
        "text": text,
        "approved_local_files": True,
        "digest": snapshot.digest,
        "byte_count": snapshot.byte_count,
        "extractor_version": snapshot.extractor_version,
        "policy_version": POLICY_VERSION,
        "fidelity": "byte_exact",
        "more_available": False,
        "next_cursor": None,
        "result_token_budget_applied": False,
    }
    if _is_document(snapshot):
        payload["extraction"] = _extraction_metadata(snapshot)
    return payload


def _finalize(
    payload: dict[str, Any],
    raw_payload: dict[str, Any],
    *,
    raw_text: str,
    rendered_text: str,
    estimator: Any | None,
) -> dict[str, Any]:
    """Attach metadata-only accounting through the shared measurement helper."""
    try:
        from .measurement import finalize_accounting

        return finalize_accounting(
            payload,
            raw_payload,
            raw_text,
            rendered_text,
            estimator=estimator,
        )
    except ImportError:
        raw_count = _measure_tokens(raw_payload, estimator)
        result_count = _measure_tokens(payload, estimator)
        count_basis = "configured_estimator" if estimator is not None else "unknown"
        provenance: dict[str, Any] = {}
        if estimator is not None:
            try:
                provenance = estimator.provenance()
            except Exception:
                provenance = {}
        payload["accounting"] = {
            "count_basis": count_basis,
            "counter_version": provenance.get("counter_version"),
            "configured_model_assumption": provenance.get("configured_model_assumption"),
            "tokenizer_name": provenance.get("tokenizer_name"),
            "host_overhead": "unknown",
            "measured_stage": "inner_mcp_result_json_text",
            "source": {
                "raw_utf8_bytes": len(raw_text.encode("utf-8")),
                "rendered_utf8_bytes": len(rendered_text.encode("utf-8")),
                "raw_token_estimate": None,
                "rendered_token_estimate": None,
                "token_delta_estimate": None,
                "byte_delta": len(raw_text.encode("utf-8")) - len(rendered_text.encode("utf-8")),
            },
            "result": {
                "raw_equivalent_utf8_bytes": len(_serialize(raw_payload).encode("utf-8")),
                "delivered_utf8_bytes": len(_serialize(payload).encode("utf-8")),
                "raw_equivalent_token_estimate": raw_count,
                "delivered_token_estimate": result_count,
                "token_delta_estimate": raw_count - result_count
                if raw_count is not None and result_count is not None
                else None,
                "byte_delta": len(_serialize(raw_payload).encode("utf-8"))
                - len(_serialize(payload).encode("utf-8")),
            },
            "retrieval": {
                "call_count": 0,
                "delivered_utf8_bytes": 0,
                "delivered_token_estimate": 0 if estimator is not None else None,
            },
        }
        return payload


def _delivered_ratio(result: dict[str, Any]) -> float | None:
    """Delivered over raw-equivalent estimated tokens, two decimals, from accounting."""
    accounting = result.get("accounting", {}).get("result", {})
    raw_count = accounting.get("raw_equivalent_token_estimate")
    delivered_count = accounting.get("delivered_token_estimate")
    if (
        not isinstance(raw_count, int)
        or isinstance(raw_count, bool)
        or raw_count <= 0
        or not isinstance(delivered_count, int)
        or isinstance(delivered_count, bool)
    ):
        return None
    return round(delivered_count / raw_count, 2)


def _finalize_compact(
    payload: dict[str, Any],
    raw_payload: dict[str, Any],
    *,
    raw_text: str,
    rendered_text: str,
    estimator: Any | None,
) -> dict[str, Any]:
    """Finalize a compact result; when compression was applied, state its ratio.

    ``compression_ratio`` is part of the delivered result, so its own tokens are
    counted. The accounting is recomputed (at most four passes, which settle in
    practice) until the reported value matches it. A result that was not
    compressed carries no ratio.
    """
    result = _finalize(
        payload,
        raw_payload,
        raw_text=raw_text,
        rendered_text=rendered_text,
        estimator=estimator,
    )
    if payload.get("compression_applied") is not True:
        return result
    for _ in range(4):
        ratio = _delivered_ratio(result)
        if ratio is None or result.get("compression_ratio") == ratio:
            break
        result = _finalize(
            {**payload, "compression_ratio": ratio},
            raw_payload,
            raw_text=raw_text,
            rendered_text=rendered_text,
            estimator=estimator,
        )
    return result


_EXACT_RAW_EXCLUDED_KEYS = frozenset({"accounting", "next_cursor", "result_token_budget_applied"})


def _exact_raw_equivalent(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the delivered exact content without ContextShrink additions.

    An exact page is not compared with the unread remainder of its scope: its
    raw-equivalent is the same text, line selection and line ranges it returns,
    minus only the pagination and budget fields that ContextShrink adds. The
    delivered payload therefore never shows tokens avoided, and the difference
    is metadata overhead.
    """
    return {key: value for key, value in payload.items() if key not in _EXACT_RAW_EXCLUDED_KEYS}


def _cursor_payload(
    snapshot: SourceSnapshot,
    *,
    mode: str,
    requested_mode: str | None = None,
    query: str | None,
    budget: int,
    selector: dict[str, Any],
    offset: int,
    tool: str = "open",
    context_lines: int | None = None,
) -> dict[str, Any]:
    return {
        "policy": POLICY_VERSION,
        "tool": tool,
        "source_id": snapshot.source_id,
        "version": snapshot.version,
        "mode": mode,
        "requested_mode": requested_mode or mode,
        "query_digest": query_digest(query),
        "budget": budget,
        "selector": selector,
        "offset": offset,
        "context_lines": context_lines,
    }


def _check_cursor(
    cursor_signer: CursorSigner,
    cursor: str,
    snapshot: SourceSnapshot,
    *,
    mode: str,
    requested_mode: str | None = None,
    query: str | None,
    budget: int,
    selector: dict[str, Any] | None,
    tool: str = "open",
    context_lines: int | None = None,
) -> tuple[dict[str, Any], int]:
    payload = cursor_signer.decode(cursor)
    bound_selector = payload.get("selector")
    # open() offsets are line numbers and retrieve() offsets are unit
    # indices, so a cursor is valid only for the tool that issued it.
    # find cursors also bind context_lines, so a cursor cannot be replayed
    # with a different context width.
    if (
        payload.get("policy") != POLICY_VERSION
        or payload.get("tool") != tool
        or payload.get("source_id") != snapshot.source_id
        or payload.get("version") != snapshot.version
        or payload.get("mode") != mode
        or payload.get("requested_mode") != (requested_mode or mode)
        or payload.get("query_digest") != query_digest(query)
        or payload.get("budget") != budget
        or payload.get("context_lines") != context_lines
        or not isinstance(bound_selector, dict)
    ):
        raise PolicyError("cursor_mismatch")
    resolved = bound_selector
    if selector is not None and selector != resolved:
        raise PolicyError("cursor_mismatch")
    offset = payload.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise PolicyError("cursor_invalid")
    return resolved, offset


def _fit_exact_page(
    snapshot: SourceSnapshot,
    *,
    start: int,
    end: int,
    limits: ResultLimits,
    estimator: Any | None,
    cursor_signer: CursorSigner,
    cursor_context: dict[str, Any],
) -> dict[str, Any]:
    lines = split_physical_lines(snapshot.text)
    if not lines:
        lines = [""]
    if start < 1 or end < start or end > len(lines):
        raise PolicyError("selector_out_of_range")

    def page(candidate_end: int, more: bool) -> dict[str, Any]:
        text = "".join(lines[start - 1 : candidate_end])
        payload = _base_payload(snapshot, "exact", text)
        payload["selected_lines"] = {"start": start, "end": candidate_end}
        payload["coverage"] = _document_coverage(
            snapshot, "exact selected lines at immutable snapshot version"
        )
        payload["fidelity"] = "byte_exact"
        payload["more_available"] = more
        payload["result_token_budget_applied"] = estimator is not None
        if more:
            token_data = _cursor_payload(
                snapshot,
                mode=cursor_context["mode"],
                requested_mode=cursor_context.get("requested_mode"),
                query=cursor_context["query"],
                budget=limits.token_budget,
                selector=cursor_context["selector"],
                offset=candidate_end + 1,
            )
            payload["next_cursor"] = cursor_signer.encode(token_data)
        requested_mode = cursor_context.get("requested_mode", cursor_context["mode"])
        if requested_mode != "exact":
            payload["requested_mode"] = requested_mode
            payload["compression_applied"] = False
        if cursor_context.get("eval_arm") == "raw":
            payload["eval_arm"] = "raw"
        return _finalize(
            payload,
            _exact_raw_equivalent(payload),
            raw_text=text,
            rendered_text=text,
            estimator=estimator,
        )

    # Select a conservative first candidate by one linear prefix pass. The
    # previous binary search finalized each midpoint against the *entire* raw
    # scope; on multi-megabyte sources the counterfactual recount dominated
    # latency. Final accounting below is still authoritative and exact.
    prefix_bytes = [0]
    for line in lines:
        prefix_bytes.append(prefix_bytes[-1] + len(line.encode("utf-8")))
    bytes_per_estimated_token = 3
    if estimator is not None:
        try:
            if not estimator.provenance().get("fallback", False):
                bytes_per_estimated_token = 4
        except Exception:
            pass
    content_allowance = max(
        1,
        min(
            limits.max_result_bytes,
            limits.token_budget * bytes_per_estimated_token,
        )
        - 768,
    )
    start_index = start - 1

    def end_for_allowance(allowance: int) -> int:
        target_bytes = prefix_bytes[start_index] + allowance
        boundary = bisect_right(prefix_bytes, target_bytes, lo=start_index + 1, hi=end + 1)
        return max(start, min(end, boundary - 1))

    candidate_end = end_for_allowance(content_allowance)
    for _attempt in range(4):
        candidate = page(candidate_end, candidate_end < end)
        result_accounting = candidate.get("accounting", {}).get("result", {})
        actual_bytes = result_accounting.get("delivered_utf8_bytes")
        actual_tokens = result_accounting.get("delivered_token_estimate")
        fits_bytes = isinstance(actual_bytes, int) and actual_bytes <= limits.max_result_bytes
        fits_tokens = (
            estimator is None
            or not isinstance(actual_tokens, int)
            or actual_tokens <= limits.token_budget
        )
        if fits_bytes and fits_tokens:
            return candidate
        if candidate_end <= start:
            break
        text_bytes = prefix_bytes[candidate_end] - prefix_bytes[start_index]
        excess_bytes = max(0, (actual_bytes or 0) - limits.max_result_bytes)
        if isinstance(actual_tokens, int) and actual_tokens > limits.token_budget:
            observed_bytes_per_token = max(1.0, (actual_bytes or 0) / max(1, actual_tokens))
            excess_bytes = max(
                excess_bytes,
                int((actual_tokens - limits.token_budget) * observed_bytes_per_token),
            )
        shrink = max(excess_bytes + 64, int(text_bytes * 0.1), 1)
        content_allowance = max(1, text_bytes - shrink)
        candidate_end = end_for_allowance(content_allowance)

    if candidate_end > start:
        single_unit = page(start, start < end)
        if _accounted_fits(single_unit, limits, estimator):
            return single_unit

    metadata = {
        "source_id": snapshot.source_id,
        "version": snapshot.version,
        "line_count": len(lines),
    }
    raise PolicyError("result_too_large", metadata)


def _finalize_full(
    payload: dict[str, Any],
    snapshot: SourceSnapshot,
    *,
    estimator: Any | None,
    raw_scope: str,
) -> dict[str, Any]:
    raw_payload = _base_payload(snapshot, "exact", raw_scope)
    for key in ("requested_mode", "eval_arm", "returned_text_is_raw"):
        if key in payload:
            raw_payload[key] = payload[key]
    return _finalize(
        payload,
        raw_payload,
        raw_text=raw_scope,
        rendered_text=payload.get("text", ""),
        estimator=estimator,
    )


def present_selected_scope(
    snapshot: SourceSnapshot,
    selected: SelectedScope,
    *,
    result_token_budget: int,
    max_result_bytes: int,
    estimator: Any | None,
    cursor: str | None,
    cursor_signer: CursorSigner,
) -> dict[str, Any]:
    """Return an exact structured-selector result, paginating only whole units."""
    try:
        limits = ResultLimits(result_token_budget, max_result_bytes)
    except ValueError:
        raise PolicyError("invalid_arguments") from None
    offset = 0
    if cursor is not None:
        _bound, offset = _check_cursor(
            cursor_signer,
            cursor,
            snapshot,
            mode="exact",
            requested_mode="exact",
            query=None,
            budget=limits.token_budget,
            selector=selected.selector,
            tool="retrieve",
        )
    units = selected.units or ((selected.line_start, selected.line_end, selected.text),)
    if offset > len(units):
        raise PolicyError("cursor_mismatch")

    def result_for(next_offset: int) -> dict[str, Any]:
        chosen = units[offset:next_offset]
        excerpt = "".join(item[2] for item in chosen)
        start_line = chosen[0][0] if chosen else selected.line_start
        end_line = chosen[-1][1] if chosen else selected.line_end
        more = next_offset < len(units)
        payload = _base_payload(snapshot, "exact", excerpt)
        payload.update(
            {
                "coverage": _document_coverage(snapshot, selected.coverage),
                "selected_lines": {"start": start_line, "end": end_line},
                "line_ranges": [{"start_line": item[0], "end_line": item[1]} for item in chosen],
                "selector": selected.selector,
                "fidelity": "byte_exact",
                "more_available": more,
                "result_token_budget_applied": estimator is not None,
                "returned_text_is_raw": True,
                "tool": "retrieve",
            }
        )
        if more:
            payload["next_cursor"] = cursor_signer.encode(
                _cursor_payload(
                    snapshot,
                    mode="exact",
                    requested_mode="exact",
                    query=None,
                    budget=limits.token_budget,
                    selector=selected.selector,
                    offset=next_offset,
                    tool="retrieve",
                )
            )
        return _finalize(
            payload,
            _exact_raw_equivalent(payload),
            raw_text=excerpt,
            rendered_text=excerpt,
            estimator=estimator,
        )

    if not selected.paginable:
        result = result_for(len(units))
        if _accounted_fits(result, limits, estimator):
            return result
        raise PolicyError(
            "result_too_large",
            {
                "source_id": snapshot.source_id,
                "version": snapshot.version,
                "line_count": max(1, len(split_physical_lines(snapshot.text))),
            },
        )

    low, high = max(offset + 1, 1), len(units)
    best: dict[str, Any] | None = None
    while low <= high:
        middle = (low + high) // 2
        candidate = result_for(middle)
        if _fit(candidate, limits, estimator):
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    if best is None:
        raise PolicyError(
            "result_too_large",
            {
                "source_id": snapshot.source_id,
                "version": snapshot.version,
                "line_count": max(1, len(split_physical_lines(snapshot.text))),
            },
        )
    return best


def present_snapshot(
    snapshot: SourceSnapshot,
    *,
    mode: PresentationMode = "exact",
    query: str | None = None,
    result_token_budget: int = 32000,
    max_result_bytes: int = 262144,
    estimator: Any | None = None,
    cursor: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    cursor_signer: CursorSigner | None = None,
    requested_mode: str | None = None,
    eval_arm: str = "optimized",
    min_compaction_gain_tokens: int = 64,
    min_compaction_gain_percent: int = 5,
    context_lines: int | None = None,
) -> dict[str, Any]:
    """Present one authorized immutable snapshot under an explicit policy."""
    try:
        limits = ResultLimits(result_token_budget, max_result_bytes)
    except ValueError:
        raise PolicyError("invalid_arguments") from None
    signer = cursor_signer or CursorSigner()
    lines = split_physical_lines(snapshot.text) or [""]
    scope = (start_line, end_line) if start_line is not None or end_line is not None else None
    if (start_line is None) != (end_line is None):
        raise PolicyError("selector_out_of_range")
    if scope is not None and (
        isinstance(start_line, bool)
        or isinstance(end_line, bool)
        or not isinstance(start_line, int)
        or not isinstance(end_line, int)
        or start_line < 1
        or end_line < start_line
        or end_line > len(lines)
    ):
        raise PolicyError("selector_out_of_range")

    if mode not in {"exact", "compact", "focused", "overview", "find"}:
        raise PolicyError("unsupported_mode")
    requested_mode = requested_mode or mode
    if mode != "exact" and scope is not None:
        raise PolicyError("selector_invalid")
    if mode == "focused" and (not isinstance(query, str) or not query.strip()):
        raise PolicyError("query_required")
    if mode == "find":
        if not isinstance(query, str) or not parse_terms(query):
            raise PolicyError("query_required")
        if context_lines is None:
            context_lines = 0
        if (
            isinstance(context_lines, bool)
            or not isinstance(context_lines, int)
            or not 0 <= context_lines <= FIND_CONTEXT_LINES_MAX
        ):
            raise PolicyError("invalid_arguments")
    elif context_lines is not None:
        raise PolicyError("invalid_arguments")

    if cursor is not None:
        requested_selector = (
            {"type": "lines", "start": scope[0], "end": scope[1]} if scope else None
        )
        resolved_selector, offset = _check_cursor(
            signer,
            cursor,
            snapshot,
            mode=mode,
            requested_mode=requested_mode,
            query=query,
            budget=limits.token_budget,
            selector=requested_selector,
            context_lines=context_lines,
        )
        if mode in {"exact", "compact"}:
            if resolved_selector.get("type") == "whole":
                resolved_scope = (1, len(lines))
            elif resolved_selector.get("type") == "lines":
                resolved_scope = (resolved_selector.get("start"), resolved_selector.get("end"))
            else:
                raise PolicyError("cursor_mismatch")
            if resolved_scope[0] < 1 or resolved_scope[1] > len(lines):
                raise PolicyError("cursor_mismatch")
        else:
            resolved_scope = (1, len(lines))
    else:
        resolved_scope = scope or (1, len(lines))
        offset = resolved_scope[0]
        resolved_selector = (
            {"type": "lines", "start": scope[0], "end": scope[1]} if scope else {"type": "whole"}
        )

    # Compact cursors are only issued by the exact-page fallback, so a
    # continuation resumes those exact pages under the same compact binding.
    if mode == "exact" or (mode == "compact" and cursor is not None):
        context = {
            "mode": mode,
            "requested_mode": requested_mode,
            "eval_arm": eval_arm,
            "query": query,
            "selector": resolved_selector,
            "scope_lines": resolved_scope,
        }
        return _fit_exact_page(
            snapshot,
            start=offset,
            end=resolved_scope[1],
            limits=limits,
            estimator=estimator,
            cursor_signer=signer,
            cursor_context=context,
        )

    raw_scope = "".join(lines[resolved_scope[0] - 1 : resolved_scope[1]])
    if mode == "compact":
        kind = infer_compaction_kind(snapshot.kind, raw_scope)
        candidate = (
            compact_text(raw_scope, kind)
            if kind
            else CompactOutcome(raw_scope, False, "byte_exact")
        )
        payload = _base_payload(
            snapshot, "compact" if candidate.applied else "exact", candidate.text
        )
        payload["requested_mode"] = "compact"
        if eval_arm == "raw":
            payload["eval_arm"] = "raw"
        payload["selected_lines"] = {"start": resolved_scope[0], "end": resolved_scope[1]}
        payload["coverage"] = _document_coverage(
            snapshot,
            "complete source snapshot"
            if resolved_scope == (1, len(lines))
            else "exact selected lines at immutable snapshot version",
        )
        payload["fidelity"] = candidate.fidelity
        payload["compression_applied"] = candidate.applied
        payload["compression_transform"] = candidate.transform
        if candidate.applied:
            decoding_hint = compression_decoding_hint(candidate.transform, candidate.fidelity)
            if decoding_hint is None:
                raise PolicyError("unsupported_compaction_transform")
            payload["compression_encoding_version"] = COMPACT_ENCODING_VERSION
            payload["compression_decoding_hint"] = decoding_hint
        raw_payload = _base_payload(snapshot, "exact", raw_scope)
        raw_payload["requested_mode"] = "compact"
        if eval_arm == "raw":
            raw_payload["eval_arm"] = "raw"
        raw_payload["selected_lines"] = {"start": resolved_scope[0], "end": resolved_scope[1]}
        raw_payload["coverage"] = _document_coverage(
            snapshot,
            "complete source snapshot"
            if resolved_scope == (1, len(lines))
            else "exact selected lines at immutable snapshot version",
        )
        raw_payload["compression_applied"] = False
        raw_payload["compression_transform"] = None
        result = _finalize_compact(
            payload,
            raw_payload,
            raw_text=raw_scope,
            rendered_text=candidate.text,
            estimator=estimator,
        )
        accounting = result.get("accounting", {}).get("result", {})
        raw_count = accounting.get("raw_equivalent_token_estimate")
        delivered_count = accounting.get("delivered_token_estimate")
        threshold = max(
            min_compaction_gain_tokens,
            int((raw_count or 0) * min_compaction_gain_percent / 100),
        )
        saves_enough = (
            candidate.applied
            and raw_count is not None
            and delivered_count is not None
            and raw_count - delivered_count >= threshold
        )
        if saves_enough and _fit(result, limits, estimator):
            return result
        # A compact candidate that does not clear the gate falls back to
        # exact pages bound to the same requested scope.
        context = {
            "mode": mode,
            "requested_mode": requested_mode,
            "eval_arm": eval_arm,
            "query": query,
            "selector": resolved_selector,
            "scope_lines": resolved_scope,
        }
        page_result = _fit_exact_page(
            snapshot,
            start=offset,
            end=resolved_scope[1],
            limits=limits,
            estimator=estimator,
            cursor_signer=signer,
            cursor_context=context,
        )
        return page_result

    if mode == "focused":
        all_passages = rank_passages(snapshot.text, query or "", limit=100)
        passage_offset = offset if cursor is not None else 0
        if cursor is not None:
            if passage_offset > len(all_passages):
                raise PolicyError("cursor_mismatch")
        passages = all_passages[passage_offset:]
        if not passages:
            payload = _base_payload(snapshot, "focused", "")
            payload.update(
                {
                    "requested_mode": "focused",
                    "coverage": _document_coverage(
                        snapshot,
                        "partial (no lexical matches; full document not represented)"
                        if not all_passages
                        else "partial (no passages remain on this cursor page)",
                    ),
                    "selected_lines": None,
                    "fidelity": "partial_exact_excerpt",
                    "blocks": [],
                }
            )
            result = _finalize_full(payload, snapshot, estimator=estimator, raw_scope=raw_scope)
            if not _accounted_fits(result, limits, estimator):
                raise PolicyError(
                    "result_too_large",
                    {
                        "source_id": snapshot.source_id,
                        "version": snapshot.version,
                        "line_count": len(lines),
                    },
                )
            return result
        next_index = passage_offset
        best_payload: dict[str, Any] | None = None
        selected_passages: list[Passage] = []
        selected_rank_indices: list[int] = []
        omitted_ranges: list[dict[str, Any]] = []
        for index, passage in enumerate(passages):
            absolute_index = passage_offset + index
            proposed = sorted([*selected_passages, passage], key=lambda item: item.start_line)
            text = "".join(
                f"[[lines {block.start_line}-{block.end_line}]]\n{block.text}" for block in proposed
            )
            payload = _base_payload(snapshot, "focused", text)
            payload.update(
                {
                    "requested_mode": "focused",
                    "coverage": _document_coverage(
                        snapshot, "partial (selected exact passages; omitted document content)"
                    ),
                    "selected_lines": None,
                    "line_ranges": [block.reference() for block in proposed],
                    "omitted_ranges": list(omitted_ranges),
                    "fidelity": "partial_exact_excerpt",
                }
            )
            has_more = absolute_index < len(all_passages) - 1
            payload["more_available"] = has_more
            if has_more:
                payload["next_cursor"] = "x" * 49
            if _fit_reserved(payload, limits, estimator):
                selected_passages = proposed
                selected_rank_indices.append(absolute_index)
                best_payload = payload
                next_index = absolute_index + 1
            else:
                if selected_passages:
                    break
                # A single overlarge passage must not block later exact
                # passages. Report its original range explicitly and advance
                # the cursor past it; content itself is never clipped.
                omitted_ranges.append(
                    {
                        "start_line": passage.start_line,
                        "end_line": passage.end_line,
                        "reason": "passage_exceeds_result_budget",
                    }
                )
                omission_payload = _base_payload(snapshot, "focused", "")
                omission_payload.update(
                    {
                        "requested_mode": "focused",
                        "coverage": _document_coverage(
                            snapshot, "partial (oversized passages omitted; exact ranges listed)"
                        ),
                        "selected_lines": None,
                        "line_ranges": [],
                        "omitted_ranges": list(omitted_ranges),
                        "fidelity": "partial_exact_excerpt",
                        "more_available": absolute_index < len(all_passages) - 1,
                        "next_cursor": "x" * 49 if absolute_index < len(all_passages) - 1 else None,
                    }
                )
                if not _fit_reserved(omission_payload, limits, estimator):
                    raise PolicyError(
                        "result_too_large",
                        {
                            "source_id": snapshot.source_id,
                            "version": snapshot.version,
                            "line_count": len(lines),
                        },
                    )
                next_index = absolute_index + 1
                best_payload = omission_payload
        if best_payload is None:
            raise PolicyError(
                "result_too_large",
                {
                    "source_id": snapshot.source_id,
                    "version": snapshot.version,
                    "line_count": len(lines),
                },
            )
        for _attempt in range(8):
            if next_index < len(all_passages):
                best_payload["next_cursor"] = signer.encode(
                    _cursor_payload(
                        snapshot,
                        mode=mode,
                        requested_mode=requested_mode,
                        query=query,
                        budget=limits.token_budget,
                        selector={"type": "whole"},
                        offset=next_index,
                    )
                )
            else:
                best_payload["next_cursor"] = None
            best_payload["more_available"] = next_index < len(all_passages)
            result = _finalize_full(
                best_payload, snapshot, estimator=estimator, raw_scope=raw_scope
            )
            if _accounted_fits(result, limits, estimator):
                return result
            if next_index <= passage_offset:
                break
            if selected_rank_indices:
                selected_rank_indices.pop()
                selected_passages = [all_passages[index] for index in selected_rank_indices]
            next_index = selected_rank_indices[-1] + 1 if selected_rank_indices else passage_offset
            proposed = sorted(selected_passages, key=lambda item: item.start_line)
            text = "".join(
                f"[[lines {block.start_line}-{block.end_line}]]\n{block.text}" for block in proposed
            )
            best_payload = _base_payload(snapshot, "focused", text)
            best_payload.update(
                {
                    "requested_mode": "focused",
                    "coverage": _document_coverage(
                        snapshot, "partial (selected exact passages; omitted document content)"
                    ),
                    "selected_lines": None,
                    "line_ranges": [block.reference() for block in proposed],
                    "omitted_ranges": list(omitted_ranges),
                    "fidelity": "partial_exact_excerpt",
                    "more_available": next_index < len(all_passages),
                    "next_cursor": "x" * 49 if next_index < len(all_passages) else None,
                }
            )
        raise PolicyError(
            "result_too_large",
            {
                "source_id": snapshot.source_id,
                "version": snapshot.version,
                "line_count": len(lines),
            },
        )

    if mode == "find":
        terms = parse_terms(query or "")
        matched = match_lines(snapshot.text, terms)
        blocks = build_blocks(lines, matched, context_lines or 0)
        block_texts = [block_text(lines, block) for block in blocks]
        find_coverage = _document_coverage(
            snapshot, "partial (matching lines with context; full document not represented)"
        )
        block_offset = offset if cursor is not None else 0
        if block_offset > len(blocks):
            raise PolicyError("cursor_mismatch")
        placeholder_cursor = "x" * 49

        def find_payload(
            first: int, stop: int, omitted: list[dict[str, Any]], next_cursor: str | None
        ) -> dict[str, Any]:
            chosen = range(first, stop)
            text = "".join(
                f"[[lines {blocks[index].start_line}-{blocks[index].end_line}]]\n"
                f"{block_texts[index]}"
                for index in chosen
            )
            payload = _base_payload(snapshot, "find", text)
            payload.update(
                {
                    "requested_mode": "find",
                    "context_lines": context_lines,
                    "coverage": find_coverage,
                    "selected_lines": None,
                    "fidelity": "partial_exact_excerpt",
                    "match_count": len(matched),
                    "match_count_complete": True,
                    "matches": [blocks[index].reference() for index in chosen],
                    "omitted_ranges": list(omitted),
                    "more_available": stop < len(blocks),
                    "next_cursor": next_cursor if stop < len(blocks) else None,
                }
            )
            return payload

        def largest_fitting_stop(first: int, omitted: list[dict[str, Any]]) -> int | None:
            # Fit is monotonic in the number of blocks, so a binary search finds
            # the largest page that starts at `first` and fits the budget.
            low, high, best = first + 1, len(blocks), None
            while low <= high:
                middle = (low + high) // 2
                candidate = find_payload(first, middle, omitted, placeholder_cursor)
                if _fit_reserved(candidate, limits, estimator):
                    best = middle
                    low = middle + 1
                else:
                    high = middle - 1
            return best

        # A block larger than the budget is listed by its range and skipped;
        # its content is never clipped.
        omitted: list[dict[str, Any]] = []
        first = block_offset
        stop: int | None = None
        while first < len(blocks):
            stop = largest_fitting_stop(first, omitted)
            if stop is not None:
                break
            block = blocks[first]
            omitted.append(
                {
                    "start_line": block.start_line,
                    "end_line": block.end_line,
                    "reason": "passage_exceeds_result_budget",
                }
            )
            first += 1
        if stop is None:
            stop = first
        for _attempt in range(8):
            next_cursor = None
            if stop < len(blocks):
                next_cursor = signer.encode(
                    _cursor_payload(
                        snapshot,
                        mode="find",
                        requested_mode=requested_mode,
                        query=query,
                        budget=limits.token_budget,
                        selector={"type": "whole"},
                        offset=stop,
                        context_lines=context_lines,
                    )
                )
            result = _finalize_full(
                find_payload(first, stop, omitted, next_cursor),
                snapshot,
                estimator=estimator,
                raw_scope=raw_scope,
            )
            if _accounted_fits(result, limits, estimator):
                return result
            if stop <= first + 1:
                break
            stop -= 1
        raise PolicyError(
            "result_too_large",
            {
                "source_id": snapshot.source_id,
                "version": snapshot.version,
                "line_count": len(lines),
            },
        )

    # Overview is intentionally an inventory, never an authoritative summary.
    if _is_document(snapshot):
        headings, inventory_label = _document_inventory(snapshot)
    else:
        headings = [
            {"line": number, "heading": title}
            for number, _level, title in markdown_headings(snapshot.text)
        ]
        inventory_label = "heading"
    inventory_coverage = _document_coverage(
        snapshot, f"partial ({inventory_label} inventory only; not a document summary)"
    )
    empty_coverage = _document_coverage(
        snapshot, f"partial (no {inventory_label}s indexed; not a document summary)"
    )
    heading_offset = offset if cursor is not None else 0
    if heading_offset > len(headings):
        raise PolicyError("cursor_mismatch")
    all_headings = headings
    headings = all_headings[heading_offset:]
    included_headings: list[dict[str, Any]] = []
    best_payload: dict[str, Any] | None = None
    for index, item in enumerate(headings):
        proposed = [*included_headings, item]
        absolute_index = heading_offset + index
        has_more = absolute_index < len(all_headings) - 1
        payload = _base_payload(snapshot, "overview", "")
        payload.update(
            {
                "requested_mode": "overview",
                "coverage": inventory_coverage,
                "selected_lines": None,
                "fidelity": "partial_inventory",
                "headings": proposed,
                "more_available": has_more,
            }
        )
        if has_more:
            payload["next_cursor"] = "x" * 49
        if _fit_reserved(payload, limits, estimator):
            included_headings = proposed
            best_payload = payload
        else:
            break
    if best_payload is None:
        payload = _base_payload(snapshot, "overview", "")
        payload.update(
            {
                "requested_mode": "overview",
                "coverage": empty_coverage,
                "selected_lines": None,
                "fidelity": "partial_inventory",
                "headings": [],
            }
        )
        result = _finalize_full(payload, snapshot, estimator=estimator, raw_scope=raw_scope)
        if _accounted_fits(result, limits, estimator):
            return result
        raise PolicyError(
            "result_too_large",
            {
                "source_id": snapshot.source_id,
                "version": snapshot.version,
                "line_count": len(lines),
            },
        )

    next_heading_offset = heading_offset + len(included_headings)
    for _attempt in range(8):
        if next_heading_offset < len(all_headings):
            best_payload["next_cursor"] = signer.encode(
                _cursor_payload(
                    snapshot,
                    mode=mode,
                    requested_mode=requested_mode,
                    query=query,
                    budget=limits.token_budget,
                    selector={"type": "whole"},
                    offset=next_heading_offset,
                )
            )
        else:
            best_payload["next_cursor"] = None
        best_payload["more_available"] = next_heading_offset < len(all_headings)
        result = _finalize_full(best_payload, snapshot, estimator=estimator, raw_scope=raw_scope)
        if _accounted_fits(result, limits, estimator):
            return result
        if next_heading_offset <= heading_offset:
            break
        next_heading_offset -= 1
        included_headings = all_headings[heading_offset:next_heading_offset]
        best_payload = _base_payload(snapshot, "overview", "")
        best_payload.update(
            {
                "requested_mode": "overview",
                "coverage": inventory_coverage,
                "selected_lines": None,
                "fidelity": "partial_inventory",
                "headings": included_headings,
                "more_available": next_heading_offset < len(all_headings),
                "next_cursor": "x" * 49 if next_heading_offset < len(all_headings) else None,
            }
        )
    raise PolicyError(
        "result_too_large",
        {"source_id": snapshot.source_id, "version": snapshot.version, "line_count": len(lines)},
    )


__all__ = [
    "CursorSigner",
    "POLICY_VERSION",
    "PolicyError",
    "PresentationMode",
    "ResultLimits",
    "present_selected_scope",
    "present_snapshot",
]
