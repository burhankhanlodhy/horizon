from __future__ import annotations

import pytest

from horizon.desktop_mcp.selectors import SelectorError, select_exact


def test_markdown_heading_occurrence_defaults_to_first_and_sections_respect_fences() -> None:
    source = "# Intro\nfirst\n```md\n## Not a heading\n```\n## Next\nsecond\n# End\n"
    selected = select_exact(source, {"type": "markdown_section", "heading": "Intro"}, kind="md")
    assert selected.text == "# Intro\nfirst\n```md\n## Not a heading\n```\n## Next\nsecond\n"
    assert selected.selector == {"type": "markdown_section", "heading": "Intro", "occurrence": 1}


def test_markdown_duplicate_occurrence_is_explicit() -> None:
    source = "# Part\none\n# Part\ntwo\n"
    selected = select_exact(
        source,
        {"type": "markdown_section", "heading": "Part", "occurrence": 2},
        kind="md",
    )
    assert selected.text == "# Part\ntwo\n"


def test_markdown_preserves_unspaced_hashes_and_requires_valid_fence_close() -> None:
    source = "# Title###\n```python\n## Hidden\n``` js\n## Still hidden\n````\n# End\n"
    selected = select_exact(
        source,
        {"type": "markdown_section", "heading": "Title###"},
        kind="md",
    )
    assert "## Hidden" in selected.text
    assert "## Still hidden" in selected.text
    assert "# End" not in selected.text


def test_json_pointer_returns_original_lexical_value_and_rejects_duplicates() -> None:
    source = '{ "number" : 1.2300e+02, "other": true }\n'
    selected = select_exact(source, {"type": "json_pointer", "pointer": "/number"}, kind="json")
    assert selected.text == "1.2300e+02"
    with pytest.raises(SelectorError, match="json_duplicate_key"):
        select_exact('{"a":1,"a":2}', {"type": "json_pointer", "pointer": "/a"}, kind="json")


def test_json_pointer_escape_and_invalid_escape() -> None:
    source = '{"a/b":{"~key":"original"}}'
    selected = select_exact(source, {"type": "json_pointer", "pointer": "/a~1b/~0key"}, kind="json")
    assert selected.text == '"original"'
    with pytest.raises(SelectorError, match="selector_invalid"):
        select_exact(source, {"type": "json_pointer", "pointer": "/a~2b"}, kind="json")


def test_json_pointer_physical_line_refs_match_cr_only_source_lines() -> None:
    source = '{\r"a": 1,\r"b": {"value": 2}\r}'
    selected = select_exact(
        source,
        {"type": "json_pointer", "pointer": "/b/value"},
        kind="json",
    )
    assert selected.text == "2"
    assert selected.selected_lines == {"start": 3, "end": 3}


def test_csv_rows_are_logical_records_and_preserve_quoted_newlines() -> None:
    source = 'name,note\nAda,"line one\nline two"\nLin,plain\n'
    selected = select_exact(source, {"type": "rows", "start": 2, "end": 2}, kind="csv")
    assert selected.text == 'Ada,"line one\nline two"\n'
    assert selected.selector == {"type": "rows", "start": 2, "end": 2}


def test_unicode_line_separator_inside_csv_field_is_not_a_physical_record_break() -> None:
    source = 'name,note\r\nAda,"first\u2028second"\r\nLin,plain\r\n'
    selected = select_exact(source, {"type": "rows", "start": 2, "end": 2}, kind="csv")
    assert selected.text == 'Ada,"first\u2028second"\r\n'
    assert selected.selected_lines == {"start": 2, "end": 2}


def test_line_selector_keeps_unicode_separator_in_the_same_physical_line() -> None:
    source = "first\u2028still-first\nsecond\n"
    selected = select_exact(source, {"type": "lines", "start": 1, "end": 1}, kind="txt")
    assert selected.text == "first\u2028still-first\n"
    assert selected.selected_lines == {"start": 1, "end": 1}


def test_rows_reject_non_csv_sources() -> None:
    with pytest.raises(SelectorError, match="selector_format_unsupported"):
        select_exact("a,b\n1,2\n", {"type": "rows", "start": 1, "end": 1}, kind="txt")


def test_json_pointer_array_units_concatenate_to_the_exact_selected_text() -> None:
    source = '{\r\n  "orders": [\r\n    {"id": 1},\r\n    {"id": 2, "tags": ["a", "b"]},\r\n    3\r\n  ],\r\n  "other": 0\r\n}\r\n'
    selected = select_exact(source, {"type": "json_pointer", "pointer": "/orders"}, kind="json")
    assert selected.paginable is True
    assert selected.scope_kind == "json_pointer"
    assert len(selected.units) == 3
    assert "".join(unit[2] for unit in selected.units) == selected.text
    assert selected.text.startswith("[") and selected.text.endswith("]")
    assert selected.units[0][0] == selected.line_start
    assert selected.units[-1][1] == selected.line_end
    assert selected.units[1][2].startswith('{"id": 2')


def test_json_pointer_object_units_are_members_in_document_order() -> None:
    source = '{"obj": {"b": 1, "a": [2]}}'
    selected = select_exact(source, {"type": "json_pointer", "pointer": "/obj"}, kind="json")
    assert selected.paginable is True
    assert [unit[2] for unit in selected.units] == ['{"b": 1, ', '"a": [2]}']
    assert "".join(unit[2] for unit in selected.units) == selected.text


def test_json_pointer_empty_container_and_scalar_stay_unpaginable() -> None:
    empty = select_exact('{"a": []}', {"type": "json_pointer", "pointer": "/a"}, kind="json")
    assert empty.text == "[]"
    assert empty.paginable is False
    scalar = select_exact('{"a": "text"}', {"type": "json_pointer", "pointer": "/a"}, kind="json")
    assert scalar.paginable is False
    assert scalar.units == ((1, 1, '"text"'),)
