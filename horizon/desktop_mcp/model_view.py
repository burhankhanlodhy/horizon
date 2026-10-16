"""Model-facing projection of open and retrieve results (0.1.6).

``model_view`` turns a full presentation payload into what Claude receives as
the MCP tool result. The broker, the worker and the stats ledger keep the full
payload; only the serialized MCP result is projected.

The projection is an allowlist. A key that is not in ``KEPT`` is dropped, so a
new payload field never reaches the model by accident. Every key in ``DROPPED``
is listed explicitly so the tests can tell a deliberate omission from a key that
nobody has classified yet.
"""

from __future__ import annotations

from typing import Any

KEPT: tuple[str, ...] = (
    "source_id",
    "mode",
    "requested_mode",
    "coverage",
    "fidelity",
    "compression_applied",
    "compression_transform",
    "compression_encoding_version",
    "compression_decoding_hint",
    "line_count",
    "selected_lines",
    "match_count",
    "headings",
    "omitted_ranges",
    "warnings",
    "text",
    "more_available",
    "next_cursor",
)

DROPPED: frozenset[str] = frozenset(
    {
        "accounting",
        "approved_local_files",
        "blocks",
        "byte_count",
        "context_lines",
        "digest",
        "eval_arm",
        "extraction",
        "extractor_version",
        "kind",
        "line_ranges",
        "match_count_complete",
        "matches",
        "policy_version",
        "result_token_budget_applied",
        "returned_text_is_raw",
        "selector",
        "tool",
        "version",
    }
)

_COMPRESSION_KEYS = frozenset(
    {
        "compression_applied",
        "compression_transform",
        "compression_encoding_version",
        "compression_decoding_hint",
    }
)
_PAGING_KEYS = frozenset({"more_available", "next_cursor"})


def model_view(payload: Any) -> Any:
    """Return the model-facing view of a presentation payload.

    Pure and total: the input is never mutated, and a non-dict value (or an
    error payload) is returned unchanged.
    """
    if not isinstance(payload, dict) or "error" in payload:
        return payload
    view: dict[str, Any] = {}
    for key in KEPT:
        if key == "warnings":
            extraction = payload.get("extraction")
            warnings = extraction.get("warnings") if isinstance(extraction, dict) else None
            if isinstance(warnings, list) and warnings:
                view["warnings"] = list(warnings)
            continue
        if key not in payload:
            continue
        value = payload[key]
        if key == "requested_mode" and value == payload.get("mode"):
            continue
        if key == "fidelity" and value == "byte_exact":
            continue
        if key in _COMPRESSION_KEYS and payload.get("compression_applied") is not True:
            continue
        if key == "selected_lines" and value is None:
            continue
        if key == "omitted_ranges" and not value:
            continue
        if key in _PAGING_KEYS and payload.get("more_available") is not True:
            continue
        view[key] = value
    return view


__all__ = ["DROPPED", "KEPT", "model_view"]
