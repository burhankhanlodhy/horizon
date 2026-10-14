"""Tests for bounded tabular parsing used by desktop typed operations."""

from __future__ import annotations

import pytest

from horizon.desktop_mcp.selectors import select_exact
from horizon.desktop_mcp.tables import (
    MAX_CELL_CHARS,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
    Row,
    Table,
    TableError,
    parse_table,
)


def _code(text: str, kind: str, **limits: int) -> str:
    with pytest.raises(TableError) as excinfo:
        parse_table(text, kind, **limits)
    return excinfo.value.code


# --- CSV / TSV ---------------------------------------------------------------


def test_csv_basic_rows_refs_and_spans() -> None:
    table = parse_table("a,b\n1,2\n3,4\n", "csv")
    assert isinstance(table, Table)
    assert table.columns == ("a", "b")
    assert table.rows == (
        Row({"type": "rows", "start": 2, "end": 2}, 2, 2, ("1", "2")),
        Row({"type": "rows", "start": 3, "end": 3}, 3, 3, ("3", "4")),
    )


def test_csv_header_empty_name_rejected() -> None:
    assert _code("a,,c\n1,2,3\n", "csv") == "table_header_invalid"


def test_csv_header_duplicate_name_rejected() -> None:
    assert _code("a,b,a\n1,2,3\n", "csv") == "table_header_invalid"


def test_csv_empty_text_has_no_header() -> None:
    assert _code("", "csv") == "table_header_invalid"


def test_csv_short_rows_padded_with_none() -> None:
    table = parse_table("a,b,c\n1\n", "csv")
    assert table.rows[0].values == ("1", None, None)


def test_csv_empty_string_is_distinct_from_missing() -> None:
    table = parse_table("a,b,c\n1,,\n2\n", "csv")
    assert table.rows[0].values == ("1", "", "")
    assert table.rows[1].values == ("2", None, None)


def test_csv_blank_lines_are_not_data_rows_but_keep_numbering() -> None:
    text = "id,name\n1,a\n\n2,b\n"
    table = parse_table(text, "csv")
    assert [row.values for row in table.rows] == [("1", "a"), ("2", "b")]
    assert table.rows[1].ref == {"type": "rows", "start": 4, "end": 4}
    assert select_exact(text, table.rows[1].ref, kind="csv").text == "2,b\n"


def test_csv_long_row_is_row_width_error() -> None:
    assert _code("a,b\n1,2,3\n", "csv") == "table_row_width"


def test_csv_quoted_comma_and_newline_keep_record_spans() -> None:
    text = 'a,b\n1,"x,y"\n2,"line1\nline2"\n3,z\n'
    table = parse_table(text, "csv")
    assert table.rows[0].values == ("1", "x,y")
    assert (table.rows[0].line_start, table.rows[0].line_end) == (2, 2)
    assert table.rows[1].values == ("2", "line1\nline2")
    assert (table.rows[1].line_start, table.rows[1].line_end) == (3, 4)
    assert table.rows[1].ref == {"type": "rows", "start": 3, "end": 3}
    assert (table.rows[2].line_start, table.rows[2].line_end) == (5, 5)
    assert table.rows[2].ref == {"type": "rows", "start": 4, "end": 4}


def test_csv_crlf_input() -> None:
    table = parse_table('a,b\r\n1,2\r\n3,"4\r\n5"\r\n', "csv")
    assert table.columns == ("a", "b")
    assert table.rows[0].values == ("1", "2")
    assert table.rows[1].values == ("3", "4\r\n5")
    assert (table.rows[1].line_start, table.rows[1].line_end) == (3, 4)


def test_csv_leading_zero_ids_kept() -> None:
    table = parse_table("id,n\n007,1.10\n", "csv")
    assert table.rows[0].values == ("007", "1.10")


def test_tsv_uses_tab_delimiter_and_keeps_commas() -> None:
    table = parse_table("a\tb\n1\t2,3\n", "tsv")
    assert table.columns == ("a", "b")
    assert table.rows[0].values == ("1", "2,3")


def test_kind_is_case_and_dot_insensitive() -> None:
    assert parse_table("a\n1\n", ".CSV").kind == "csv"


def test_unsupported_kind_rejected() -> None:
    assert _code("<a/>", "xml") == "table_kind_unsupported"


def test_csv_malformed_quote_is_table_invalid() -> None:
    assert _code('a,b\n1,"x"y\n', "csv") == "table_invalid"


# --- CSV/TSV limits -----------------------------------------------------------


def test_csv_row_limit() -> None:
    assert _code("a\n1\n2\n", "csv", max_rows=1) == "table_too_many_rows"
    assert len(parse_table("a\n1\n2\n", "csv", max_rows=2).rows) == 2


def test_csv_column_limit() -> None:
    assert _code("a,b,c\n1,2,3\n", "csv", max_columns=2) == "table_too_many_columns"


def test_csv_cell_limit_applies_to_header_and_data() -> None:
    assert _code("abcd\n1\n", "csv", max_cell_chars=3) == "table_cell_too_large"
    assert _code("ab\nabcd\n", "csv", max_cell_chars=3) == "table_cell_too_large"


def test_limit_defaults_are_exposed() -> None:
    assert MAX_TABLE_ROWS > 0
    assert MAX_TABLE_COLUMNS > 0
    assert MAX_CELL_CHARS > 0


# --- JSON array ----------------------------------------------------------------


def test_json_union_of_keys_in_first_seen_order() -> None:
    table = parse_table('[{"b": "1", "a": "2"}, {"c": "3", "a": "4"}]', "json")
    assert table.columns == ("b", "a", "c")
    assert table.rows[0].values == ("1", "2", None)
    assert table.rows[1].values == (None, "4", "3")
    assert table.rows[0].ref == {"type": "json_pointer", "pointer": "/0"}
    assert table.rows[1].ref == {"type": "json_pointer", "pointer": "/1"}


def test_json_scalars_null_booleans_and_strings() -> None:
    table = parse_table('[{"n": null, "t": true, "f": false, "s": "true", "e": ""}]', "json")
    assert table.rows[0].values == (None, "true", "false", "true", "")


def test_json_number_lexemes_kept_verbatim() -> None:
    text = '[{"a": 1.10, "b": 1e3, "c": 123456789012345678901234567890, "d": -0.0, "e": 2E+2}]'
    table = parse_table(text, "json")
    assert table.rows[0].values == (
        "1.10",
        "1e3",
        "123456789012345678901234567890",
        "-0.0",
        "2E+2",
    )


def test_json_unicode_and_escapes_decoded() -> None:
    table = parse_table('[{"k": "caf\\u00e9 \\"q\\"\\n", "u": "naïve ☃"}]', "json")
    assert table.rows[0].values == ('café "q"\n', "naïve ☃")


def test_json_nested_value_rejected() -> None:
    assert _code('[{"a": {"b": 1}}]', "json") == "table_nested_value"
    assert _code('[{"a": [1]}]', "json") == "table_nested_value"


def test_json_duplicate_key_rejected_at_any_depth() -> None:
    assert _code('[{"a": 1, "a": 2}]', "json") == "table_duplicate_key"
    assert _code('[{"a": {"x": 1, "x": 2}}]', "json") == "table_duplicate_key"


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_json_nan_and_infinity_rejected(literal: str) -> None:
    assert _code(f'[{{"a": {literal}}}]', "json") == "table_invalid"


def test_json_top_level_object_rejected() -> None:
    assert _code('{"a": 1}', "json") == "table_invalid"


def test_json_non_object_element_rejected() -> None:
    assert _code("[1, 2]", "json") == "table_invalid"
    assert _code("[null]", "json") == "table_invalid"


def test_json_malformed_is_table_invalid() -> None:
    assert _code('[{"a": 1', "json") == "table_invalid"


def test_json_limits() -> None:
    assert _code('[{"a": 1}, {"a": 2}]', "json", max_rows=1) == "table_too_many_rows"
    assert _code('[{"a": 1, "b": 2}]', "json", max_columns=1) == "table_too_many_columns"
    assert _code('[{"a": "abcd"}]', "json", max_cell_chars=3) == "table_cell_too_large"


def test_json_empty_array_is_empty_table() -> None:
    table = parse_table("[]", "json")
    assert table.columns == ()
    assert table.rows == ()


# --- JSONL / NDJSON ------------------------------------------------------------


def test_jsonl_blank_lines_skipped_and_lines_refs() -> None:
    text = '{"a": 1}\n\n   \n{"a": 2, "b": "x"}\r\n'
    table = parse_table(text, "jsonl")
    assert table.columns == ("a", "b")
    assert table.rows[0] == Row({"type": "lines", "start": 1, "end": 1}, 1, 1, ("1", None))
    assert table.rows[1] == Row({"type": "lines", "start": 4, "end": 4}, 4, 4, ("2", "x"))


def test_ndjson_alias_and_limits() -> None:
    assert parse_table('{"a": 1}\n', "ndjson").kind == "ndjson"
    assert _code('{"a": 1}\n{"a": 2}\n', "jsonl", max_rows=1) == "table_too_many_rows"


def test_jsonl_nested_and_non_object_lines_rejected() -> None:
    assert _code('{"a": {"b": 1}}\n', "jsonl") == "table_nested_value"
    assert _code("[1]\n", "jsonl") == "table_invalid"
    assert _code('{"a": 1, "a": 1}\n', "jsonl") == "table_duplicate_key"


# --- Error hygiene -----------------------------------------------------------


def test_errors_never_contain_source_text() -> None:
    secret = "TOPSECRETVALUE"
    for text, kind in [
        (f'[{{"a": "{secret}", "a": 1}}]', "json"),
        (f'[{{"a": {{"{secret}": 1}}}}]', "json"),
        (f"a,a\n{secret},1\n", "csv"),
        (f"a\n{secret}\n{secret},{secret}\n", "csv"),
        (f"[{secret}", "json"),
    ]:
        with pytest.raises(TableError) as excinfo:
            parse_table(text, kind)
        assert secret not in str(excinfo.value)
        assert secret not in excinfo.value.code


# --- Cross-check with exact selectors -------------------------------------------


def test_csv_row_ref_selects_exact_record_text() -> None:
    text = 'a,b\r\n1,2\r\n3,"line1\nline2"\r\n5,6'
    table = parse_table(text, "csv")
    expected = {
        0: "1,2\r\n",
        1: '3,"line1\nline2"\r\n',
        2: "5,6",
    }
    for index, row in enumerate(table.rows):
        scope = select_exact(text, row.ref, kind="csv")
        assert scope.text == expected[index]
        assert scope.line_start == row.line_start
        assert scope.line_end == row.line_end


def test_tsv_row_ref_selects_exact_record_text() -> None:
    text = 'a\tb\n1\t"x\ny"\n2\t3\n'
    table = parse_table(text, "tsv")
    scope = select_exact(text, table.rows[0].ref, kind="tsv")
    assert scope.text == '1\t"x\ny"\n'
    assert (scope.line_start, scope.line_end) == (2, 3)


def test_json_pointer_ref_selects_element() -> None:
    text = '[{"a": 1.10}, {"a": 2}]'
    table = parse_table(text, "json")
    scope = select_exact(text, table.rows[1].ref, kind="json")
    assert scope.text == '{"a": 2}'


def test_jsonl_lines_ref_selects_line() -> None:
    text = '{"a": 1}\n\n{"a": 2}\n'
    table = parse_table(text, "jsonl")
    scope = select_exact(text, table.rows[1].ref, kind="jsonl")
    assert scope.text == '{"a": 2}\n'
