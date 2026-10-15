from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp.broker_transport import BrokerTransportError, _validate_selector
from horizon.desktop_mcp.contracts import ToolInputError, tool_specs, validate_tool_arguments
from horizon.desktop_mcp.extractors import DocumentBlock
from horizon.desktop_mcp.selectors import SelectorError, select_exact

REPO_ROOT = Path(__file__).resolve().parents[2]

# Page 1: two lines. Page 2: no extractable text. Page 3: one line. Page 4: two lines.
PDF_TEXT = "page one line a\npage one line b\np3\np4 x\np4 y\n"
PDF_BLOCKS = (
    DocumentBlock("page", 1, 1, 2),
    DocumentBlock("page", 2, 3, 2),
    DocumentBlock("page", 3, 3, 3),
    DocumentBlock("page", 4, 4, 5),
)

# Heading (paragraph numbering), a two-line paragraph, a table, then a paragraph.
DOCX_TEXT = "Title\nBody one\nline two\na\tb\nc\td\nTail\n"
DOCX_BLOCKS = (
    DocumentBlock("heading", 1, 1, 1, {"heading_level": 1}),
    DocumentBlock("paragraph", 2, 2, 3),
    DocumentBlock("table", 1, 4, 5, {"rows": 2, "columns": 2}),
    DocumentBlock("paragraph", 3, 6, 6),
)


def _selector_error(code: str, call: Any) -> None:
    with pytest.raises(SelectorError) as caught:
        call()
    assert caught.value.code == code


# --- select_exact: page -------------------------------------------------------


def test_page_selector_returns_whole_pages_as_units() -> None:
    selected = select_exact(
        PDF_TEXT, {"type": "page", "start": 1, "end": 1}, kind="pdf", blocks=PDF_BLOCKS
    )
    assert selected.text == "page one line a\npage one line b\n"
    assert selected.units == ((1, 2, "page one line a\npage one line b\n"),)
    assert selected.paginable is True
    assert selected.scope_kind == "pages"
    assert selected.selector == {"type": "page", "start": 1, "end": 1}
    assert (
        selected.coverage
        == "exact extracted PDF text for selected pages at immutable snapshot version"
    )
    assert (selected.line_start, selected.line_end) == (1, 2)
    assert (selected.start_offset, selected.end_offset) == (0, len(selected.text))


def test_page_selection_skips_empty_pages_in_units_and_names_them_in_coverage() -> None:
    selected = select_exact(
        PDF_TEXT, {"type": "page", "start": 2, "end": 4}, kind="PDF", blocks=PDF_BLOCKS
    )
    assert selected.text == "p3\np4 x\np4 y\n"
    assert selected.units == ((3, 3, "p3\n"), (4, 5, "p4 x\np4 y\n"))
    assert selected.coverage.endswith("; pages without extractable text: 2")
    assert (selected.line_start, selected.line_end) == (3, 5)
    assert selected.start_offset == len("page one line a\npage one line b\n")


def test_page_selection_of_only_empty_pages_returns_empty_text_with_note() -> None:
    selected = select_exact(
        PDF_TEXT, {"type": "page", "start": 2, "end": 2}, kind="pdf", blocks=PDF_BLOCKS
    )
    assert selected.text == ""
    assert selected.units == ()
    assert selected.paginable is True
    assert selected.coverage.endswith("; pages without extractable text: 2")
    assert selected.start_offset == selected.end_offset


def test_page_coverage_has_no_note_when_every_page_has_text() -> None:
    selected = select_exact(
        PDF_TEXT, {"type": "page", "start": 3, "end": 4}, kind="pdf", blocks=PDF_BLOCKS
    )
    assert "without extractable text" not in selected.coverage
    assert len(selected.units) == 2


def test_page_span_boundary_passes_validation_and_reports_out_of_range() -> None:
    _selector_error(
        "selector_out_of_range",
        lambda: select_exact(
            PDF_TEXT, {"type": "page", "start": 1, "end": 200}, kind="pdf", blocks=PDF_BLOCKS
        ),
    )
    _selector_error(
        "selector_invalid",
        lambda: select_exact(
            PDF_TEXT, {"type": "page", "start": 1, "end": 201}, kind="pdf", blocks=PDF_BLOCKS
        ),
    )


def test_page_out_of_range_and_invalid_shapes() -> None:
    _selector_error(
        "selector_out_of_range",
        lambda: select_exact(
            PDF_TEXT, {"type": "page", "start": 1, "end": 9}, kind="pdf", blocks=PDF_BLOCKS
        ),
    )
    for selector in (
        {"type": "page", "start": 0, "end": 1},
        {"type": "page", "start": 3, "end": 2},
        {"type": "page", "start": True, "end": 2},
        {"type": "page", "start": 1, "end": 2.0},
        {"type": "page", "start": 1, "end": 2, "index": 1},
    ):
        _selector_error(
            "selector_invalid",
            lambda selector=selector: select_exact(
                PDF_TEXT, selector, kind="pdf", blocks=PDF_BLOCKS
            ),
        )


# --- select_exact: paragraph and table ---------------------------------------


def test_paragraph_numbering_counts_headings_and_multiline_paragraphs() -> None:
    selected = select_exact(
        DOCX_TEXT, {"type": "paragraph", "start": 1, "end": 2}, kind="docx", blocks=DOCX_BLOCKS
    )
    assert selected.text == "Title\nBody one\nline two\n"
    assert selected.units == ((1, 1, "Title\n"), (2, 3, "Body one\nline two\n"))
    assert selected.paginable is True
    assert selected.scope_kind == "paragraphs"
    assert selected.selector == {"type": "paragraph", "start": 1, "end": 2}
    assert selected.coverage.startswith("exact extracted DOCX paragraphs and headings")


def test_paragraph_selection_skips_tables_in_numbering() -> None:
    selected = select_exact(
        DOCX_TEXT, {"type": "paragraph", "start": 3, "end": 3}, kind="docx", blocks=DOCX_BLOCKS
    )
    assert selected.text == "Tail\n"
    assert selected.units == ((6, 6, "Tail\n"),)


def test_paragraph_out_of_range_and_invalid_shapes() -> None:
    _selector_error(
        "selector_out_of_range",
        lambda: select_exact(
            DOCX_TEXT, {"type": "paragraph", "start": 1, "end": 4}, kind="docx", blocks=DOCX_BLOCKS
        ),
    )
    _selector_error(
        "selector_invalid",
        lambda: select_exact(
            DOCX_TEXT, {"type": "paragraph", "start": 2, "end": 1}, kind="docx", blocks=DOCX_BLOCKS
        ),
    )
    _selector_error(
        "selector_invalid",
        lambda: select_exact(
            DOCX_TEXT,
            {"type": "paragraph", "start": 1, "end": 2001},
            kind="docx",
            blocks=DOCX_BLOCKS,
        ),
    )
    _selector_error(
        "selector_invalid",
        lambda: select_exact(
            DOCX_TEXT,
            {"type": "paragraph", "start": False, "end": 1},
            kind="docx",
            blocks=DOCX_BLOCKS,
        ),
    )


def test_table_selector_returns_one_table_unit() -> None:
    selected = select_exact(
        DOCX_TEXT, {"type": "table", "index": 1}, kind="docx", blocks=DOCX_BLOCKS
    )
    assert selected.text == "a\tb\nc\td\n"
    assert selected.units == ((4, 5, "a\tb\nc\td\n"),)
    assert selected.paginable is True
    assert selected.scope_kind == "table"
    assert selected.selector == {"type": "table", "index": 1}
    assert selected.coverage == "exact extracted DOCX table at immutable snapshot version"


def test_table_out_of_range_and_invalid_index() -> None:
    _selector_error(
        "selector_out_of_range",
        lambda: select_exact(
            DOCX_TEXT, {"type": "table", "index": 2}, kind="docx", blocks=DOCX_BLOCKS
        ),
    )
    _selector_error(
        "selector_format_unsupported",
        lambda: select_exact(DOCX_TEXT, {"type": "table", "index": 1}, kind="docx", blocks=()),
    )
    _selector_error(
        "selector_out_of_range",
        lambda: select_exact(
            DOCX_TEXT,
            {"type": "table", "index": 1},
            kind="docx",
            blocks=(DocumentBlock("paragraph", 1, 1, 1),),
        ),
    )
    for selector in (
        {"type": "table", "index": 0},
        {"type": "table", "index": True},
        {"type": "table", "index": 1, "start": 1},
    ):
        _selector_error(
            "selector_invalid",
            lambda selector=selector: select_exact(
                DOCX_TEXT, selector, kind="docx", blocks=DOCX_BLOCKS
            ),
        )


# --- wrong kind and missing blocks -------------------------------------------


@pytest.mark.parametrize(
    ("selector", "kind", "blocks"),
    [
        ({"type": "page", "start": 1, "end": 1}, "docx", PDF_BLOCKS),
        ({"type": "page", "start": 1, "end": 1}, "csv", PDF_BLOCKS),
        ({"type": "page", "start": 1, "end": 1}, "pdf", ()),
        ({"type": "paragraph", "start": 1, "end": 1}, "pdf", DOCX_BLOCKS),
        ({"type": "paragraph", "start": 1, "end": 1}, "docx", ()),
        ({"type": "table", "index": 1}, "pdf", DOCX_BLOCKS),
        ({"type": "table", "index": 1}, "txt", DOCX_BLOCKS),
        ({"type": "table", "index": 1}, "docx", ()),
    ],
)
def test_wrong_kind_or_missing_blocks_is_format_unsupported(
    selector: dict[str, Any], kind: str, blocks: tuple[DocumentBlock, ...]
) -> None:
    _selector_error(
        "selector_format_unsupported",
        lambda: select_exact(PDF_TEXT, selector, kind=kind, blocks=blocks),
    )


def test_unknown_selector_type_is_still_invalid() -> None:
    _selector_error(
        "selector_invalid",
        lambda: select_exact(PDF_TEXT, {"type": "sheet", "start": 1, "end": 1}, kind="pdf"),
    )


# --- existing selectors are unaffected by blocks ------------------------------


def test_existing_selectors_ignore_blocks() -> None:
    source = "# Intro\nfirst\n## Next\nsecond\n"
    cases = (
        ({"type": "lines", "start": 2, "end": 3}, "md"),
        ({"type": "markdown_section", "heading": "Intro"}, "md"),
    )
    for selector, kind in cases:
        with_blocks = select_exact(source, selector, kind=kind, blocks=PDF_BLOCKS)
        without = select_exact(source, selector, kind=kind)
        assert with_blocks == without


def test_existing_rows_selector_ignores_blocks() -> None:
    source = "a,b\n1,2\n"
    assert select_exact(
        source, {"type": "rows", "start": 1, "end": 2}, kind="csv", blocks=DOCX_BLOCKS
    ) == select_exact(source, {"type": "rows", "start": 1, "end": 2}, kind="csv")


def test_existing_json_pointer_ignores_blocks() -> None:
    source = '{"a": 1}'
    assert select_exact(
        source, {"type": "json_pointer", "pointer": "/a"}, kind="json", blocks=PDF_BLOCKS
    ) == select_exact(source, {"type": "json_pointer", "pointer": "/a"}, kind="json")


# --- contracts ----------------------------------------------------------------

NEW_SELECTORS: list[dict[str, Any]] = [
    {"type": "page", "start": 1, "end": 200},
    {"type": "paragraph", "start": 1, "end": 2000},
    {"type": "table", "index": 10000},
]


@pytest.mark.parametrize("selector", NEW_SELECTORS)
def test_retrieve_contract_accepts_new_selector_variants(selector: dict[str, Any]) -> None:
    validated = validate_tool_arguments(
        "contextshrink_retrieve", {"source_id": "src", "selector": selector}
    )
    assert validated["selector"] == selector


@pytest.mark.parametrize(
    "selector",
    [
        {"type": "page", "start": 1, "end": 201},
        {"type": "page", "start": 3, "end": 2},
        {"type": "page", "start": 0, "end": 1},
        {"type": "page", "start": True, "end": 1},
        {"type": "page", "start": 1, "end": 1, "index": 1},
        {"type": "paragraph", "start": 1, "end": 2001},
        {"type": "paragraph", "start": 1.0, "end": 1},
        {"type": "paragraph", "start": 1, "end": 1, "path": "secret"},
        {"type": "table", "index": 0},
        {"type": "table", "index": 10001},
        {"type": "table", "index": True},
        {"type": "table", "index": 1, "start": 1},
        {"type": "table"},
    ],
)
def test_retrieve_contract_rejects_invalid_new_selector_variants(selector: dict[str, Any]) -> None:
    with pytest.raises(ToolInputError) as caught:
        validate_tool_arguments(
            "contextshrink_retrieve", {"source_id": "src", "selector": selector}
        )
    assert caught.value.code == "invalid_arguments"
    assert "secret" not in str(caught.value)


def test_batch_contract_uses_the_same_selector_union() -> None:
    validated = validate_tool_arguments(
        "contextshrink_retrieve_batch",
        {"requests": [{"source_id": "src", "selector": {"type": "table", "index": 2}}]},
        "compute",
    )
    assert validated["requests"][0]["selector"] == {"type": "table", "index": 2}
    with pytest.raises(ToolInputError):
        validate_tool_arguments(
            "contextshrink_retrieve_batch",
            {"requests": [{"source_id": "src", "selector": {"type": "table", "index": True}}]},
            "compute",
        )


def _retrieve_spec(profile: str) -> dict[str, Any]:
    return next(spec for spec in tool_specs(profile) if spec["name"] == "contextshrink_retrieve")


def test_retrieve_schema_lists_new_variants_after_existing_ones() -> None:
    variants = _retrieve_spec("source")["inputSchema"]["properties"]["selector"]["oneOf"]
    consts = [variant["properties"]["type"].get("const") for variant in variants]
    assert consts == [
        "lines",
        "markdown_section",
        "json_pointer",
        "rows",
        "page",
        "paragraph",
        "table",
    ]
    description = _retrieve_spec("source")["inputSchema"]["properties"]["selector"]["description"]
    assert "page (PDF)" in description
    assert "paragraph (DOCX)" in description
    assert "table (DOCX)" in description


# A fixed commit on main from before the document selectors existed. Comparing
# against HEAD would break as soon as this change itself was committed.
BASELINE_COMMIT = "0f7fde89"


def _load_head_contracts(tmp_path: Path) -> Any:
    completed = subprocess.run(
        ["git", "show", f"{BASELINE_COMMIT}:horizon/desktop_mcp/contracts.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    snapshot = tmp_path / "head_contracts.py"
    snapshot.write_bytes(completed.stdout)
    spec = importlib.util.spec_from_file_location("head_contracts_snapshot", snapshot)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _without_descriptions(value: Any) -> Any:
    """Return a copy of a JSON-like value with every ``description`` key removed."""
    if isinstance(value, dict):
        return {
            key: _without_descriptions(item) for key, item in value.items() if key != "description"
        }
    if isinstance(value, list):
        return [_without_descriptions(item) for item in value]
    return value


def test_source_profile_schemas_match_baseline_except_retrieve_selector_extension(
    tmp_path: Path,
) -> None:
    try:
        head = _load_head_contracts(tmp_path)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git baseline snapshot unavailable")
    # The baseline predates tool profiles; its no-argument call is the source profile.
    head_specs = {spec["name"]: spec for spec in head.tool_specs()}
    current_specs = {spec["name"]: spec for spec in tool_specs("source")}
    assert list(current_specs) == list(head_specs)
    # Descriptions are guidance text, not schema structure; every other key is compared.
    for name, spec in head_specs.items():
        if name != "contextshrink_retrieve":
            assert _without_descriptions(current_specs[name]) == _without_descriptions(spec), name

    head_retrieve = head_specs["contextshrink_retrieve"]
    current_retrieve = current_specs["contextshrink_retrieve"]
    head_selector = head_retrieve["inputSchema"]["properties"]["selector"]
    current_selector = current_retrieve["inputSchema"]["properties"]["selector"]
    assert current_selector["oneOf"][: len(head_selector["oneOf"])] == head_selector["oneOf"]
    assert len(current_selector["oneOf"]) == len(head_selector["oneOf"]) + 3
    stripped_head = _without_descriptions(head_retrieve)
    stripped_current = _without_descriptions(current_retrieve)
    for item in (stripped_head, stripped_current):
        item["inputSchema"]["properties"]["selector"].pop("oneOf")
    assert stripped_current == stripped_head


# --- broker transport --------------------------------------------------------


@pytest.mark.parametrize(
    "selector",
    [
        {"type": "page", "start": 1, "end": 1},
        {"type": "page", "start": 1, "end": 200},
        {"type": "paragraph", "start": 5, "end": 2004},
        {"type": "table", "index": 1},
        {"type": "table", "index": 10000},
    ],
)
def test_transport_accepts_new_selector_shapes(selector: dict[str, Any]) -> None:
    _validate_selector(selector)


@pytest.mark.parametrize(
    "selector",
    [
        {"type": "page", "start": 1, "end": 201},
        {"type": "page", "start": 2, "end": 1},
        {"type": "page", "start": True, "end": 1},
        {"type": "page", "start": 1, "end": 1, "extra": 1},
        {"type": "page", "start": 0, "end": 0},
        {"type": "paragraph", "start": 1, "end": 2001},
        {"type": "paragraph", "start": 1.5, "end": 2},
        {"type": "paragraph", "start": 1},
        {"type": "table", "index": 0},
        {"type": "table", "index": 10001},
        {"type": "table", "index": False},
        {"type": "table", "index": "1"},
        {"type": "table", "index": 1, "start": 1},
    ],
)
def test_transport_rejects_invalid_new_selector_shapes(selector: dict[str, Any]) -> None:
    with pytest.raises(BrokerTransportError) as caught:
        _validate_selector(selector)
    assert caught.value.code == "invalid_arguments"


def test_transport_still_rejects_unknown_selector_types() -> None:
    with pytest.raises(BrokerTransportError):
        _validate_selector({"type": "sheet", "start": 1, "end": 1})
