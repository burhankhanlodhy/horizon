from __future__ import annotations

import csv

import pytest

from horizon.desktop_mcp.selectors import (
    _MAX_CSV_COLUMNS,
    _MAX_CSV_FIELD_CHARS,
    _MAX_CSV_ROWS,
    SelectorError,
    select_exact,
)


def test_csv_row_limit_accepts_boundary_and_counts_blank_rows() -> None:
    source = "\n" * _MAX_CSV_ROWS

    selected = select_exact(source, {"type": "rows", "start": 1, "end": 1}, kind="csv")

    assert selected.text == "\n"
    assert selected.units == ((1, 1, "\n"),)


def test_csv_row_limit_rejects_the_next_blank_row() -> None:
    source = "\n" * (_MAX_CSV_ROWS + 1)

    with pytest.raises(SelectorError) as error:
        select_exact(source, {"type": "rows", "start": 1, "end": 1}, kind="csv")

    assert error.value.code == "csv_row_limit"


def test_csv_column_limit_rejects_a_wide_record_before_parsing() -> None:
    source = "," * _MAX_CSV_COLUMNS + "\n"

    with pytest.raises(SelectorError) as error:
        select_exact(source, {"type": "rows", "start": 1, "end": 1}, kind="csv")

    assert error.value.code == "csv_column_limit"


def test_csv_column_count_ignores_quoted_delimiters_and_line_breaks() -> None:
    source = 'id,note\n1,"a,b ""quoted""\nnext line"\n3,a"b,c\n'

    selected = select_exact(source, {"type": "rows", "start": 2, "end": 3}, kind="csv")

    assert selected.text == '1,"a,b ""quoted""\nnext line"\n3,a"b,c\n'


def test_csv_field_limit_allows_large_existing_cells_and_restores_global_limit() -> None:
    previous_limit = csv.field_size_limit()
    source = 'id,blob\n1,"' + ("x" * 200_000) + '"\n'

    selected = select_exact(source, {"type": "rows", "start": 2, "end": 2}, kind="csv")

    assert len(selected.text) == 200_005
    assert csv.field_size_limit() == previous_limit


def test_csv_oversized_field_has_a_safe_error_and_restores_global_limit() -> None:
    previous_limit = csv.field_size_limit()
    source = "x" * (_MAX_CSV_FIELD_CHARS + 1) + "\n"

    with pytest.raises(SelectorError) as error:
        select_exact(source, {"type": "rows", "start": 1, "end": 1}, kind="csv")

    assert error.value.code == "csv_field_too_large"
    assert csv.field_size_limit() == previous_limit


def test_csv_parse_error_restores_global_field_limit() -> None:
    previous_limit = csv.field_size_limit()

    with pytest.raises(SelectorError) as error:
        select_exact('"unterminated\n', {"type": "rows", "start": 1, "end": 1}, kind="csv")

    assert error.value.code == "csv_invalid"
    assert csv.field_size_limit() == previous_limit


def test_lines_selector_accepts_a_ten_thousand_line_span() -> None:
    source = "x\n" * 10001

    selected = select_exact(source, {"type": "lines", "start": 1, "end": 10001}, kind="txt")

    assert len(selected.units) == 10001
    assert selected.text == source


def test_lines_selector_rejects_a_span_over_the_200000_line_cap() -> None:
    with pytest.raises(SelectorError, match="selector_out_of_range"):
        select_exact("x\n", {"type": "lines", "start": 1, "end": 200002}, kind="txt")
