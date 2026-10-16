"""Guidance in tool descriptions and local instructions that steers read-mode choice."""

from __future__ import annotations

import json
from typing import Any

import pytest

from horizon.desktop_mcp.contracts import tool_specs
from horizon.desktop_mcp.measurement import TokenEstimator
from horizon.desktop_mcp.server import create_server
from horizon.desktop_mcp.settings import DesktopSettings

# Measured before the guidance text was added (o200k estimate, same method as below).
# Re-measured on HEAD (0.1.3 contracts, o200k): the earlier 1131 figure was stale.
# The 0.1.4 find-mode text adds about 134 tokens to the source tool listing.
BASELINE_SOURCE_SPEC_TOKENS = 1288
BASELINE_LOCAL_INSTRUCTION_TOKENS = 90
ADDED_TOKEN_BUDGET = 200


def _spec(name: str) -> dict[str, Any]:
    return next(spec for spec in tool_specs("source") if spec["name"] == name)


def _open_description() -> str:
    return _spec("contextshrink_open")["description"]


def _local_instructions() -> str:
    server = create_server(settings=DesktopSettings(profile="local"), backend=object())
    return server.instructions or ""


def _diagnostic_instructions() -> str:
    server = create_server(settings=DesktopSettings(profile="diagnostic"))
    return server.instructions or ""


@pytest.mark.parametrize(
    ("mode", "guidance"),
    [
        ("compact:", "JSON, logs, config or diff files"),
        ("compact:", "falls back to exact when the saving is small"),
        ("focused:", "with a query"),
        ("focused:", "coverage is partial"),
        ("focused:", "contextshrink_retrieve"),
        ("overview:", "first"),
        ("overview:", "headings or pages only"),
        ("exact (default):", "quoting"),
        ("exact (default):", "when the user asks for everything"),
    ],
)
def test_open_description_gives_each_mode_its_guidance(mode: str, guidance: str) -> None:
    description = _open_description()
    assert mode in description
    assert guidance in description


def test_open_description_states_focused_requires_a_query() -> None:
    description = _open_description()
    assert "focused: with a query" in description
    assert "query" in _spec("contextshrink_open")["inputSchema"]["properties"]


def test_open_default_mode_is_exact_and_enum_is_unchanged() -> None:
    mode = _spec("contextshrink_open")["inputSchema"]["properties"]["mode"]
    assert mode["default"] == "exact"
    assert mode["enum"] == ["exact", "compact", "focused", "overview", "find"]
    assert mode["type"] == "string"


def test_open_argument_names_are_unchanged() -> None:
    schema = _spec("contextshrink_open")["inputSchema"]
    assert set(schema["properties"]) == {
        "source",
        "mode",
        "result_token_budget",
        "query",
        "cursor",
        "context_lines",
    }
    assert schema["required"] == ["source"]


def test_retrieve_description_names_exact_sections_and_follow_up_use() -> None:
    description = _spec("contextshrink_retrieve")["description"]
    for section in (
        "lines",
        "markdown section",
        "JSON pointer",
        "CSV/TSV rows",
        "PDF page",
        "DOCX paragraph or table",
    ):
        assert section in description
    assert "focused or overview results" in description


def test_source_profile_token_growth_is_within_budget() -> None:
    estimator = TokenEstimator()
    if estimator.provenance().get("fallback", False):
        pytest.skip("verified o200k cache unavailable; byte heuristic counts are not comparable")
    specs_tokens = estimator.count_text(json.dumps(tool_specs("source")))
    instruction_tokens = estimator.count_text(_local_instructions())
    added = (specs_tokens - BASELINE_SOURCE_SPEC_TOKENS) + (
        instruction_tokens - BASELINE_LOCAL_INSTRUCTION_TOKENS
    )
    assert added < ADDED_TOKEN_BUDGET


def test_local_instructions_carry_mode_guidance() -> None:
    instructions = _local_instructions()
    assert "compact for JSON, logs or config" in instructions
    assert "focused with a query" in instructions
    assert "overview to orient" in instructions
    assert "exact for quoting or full review" in instructions


def test_diagnostic_instructions_do_not_carry_mode_guidance() -> None:
    instructions = _diagnostic_instructions()
    assert "focused with a query" not in instructions
    assert "compact for JSON" not in instructions
