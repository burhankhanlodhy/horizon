"""Bounded tabular views of approved CSV/TSV/JSON snapshots for typed operations.

Cells keep their source lexemes. Typing happens only in ``operations`` when a
specification declares a column type, so parsing never coerces IDs, numbers,
or dates. Every row carries an exact selector for same-version recovery.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from .selectors import _CSV_FIELD_LIMIT_LOCK
from .text_lines import split_physical_lines

MAX_TABLE_ROWS = 200_000
MAX_TABLE_COLUMNS = 256
MAX_CELL_CHARS = 1_000_000


class TableError(ValueError):
    """Sanitized table failure with a stable code and no source text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Row:
    # Exact retrieve selector for this record, e.g. {"type": "rows", "start": 2,
    # "end": 2} for CSV/TSV or {"type": "json_pointer", "pointer": "/0"} for JSON.
    ref: dict[str, Any]
    line_start: int
    line_end: int
    # One entry per Table.columns item. None means the cell or key is absent;
    # an empty string stays "". JSON null is None, true/false are "true"/"false",
    # numbers keep their exact source lexeme, and strings are decoded text.
    values: tuple[str | None, ...]


@dataclass(frozen=True)
class Table:
    kind: str
    columns: tuple[str, ...]
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class ResultTable:
    """Computed output shared by operations and artifact renderers."""

    columns: tuple[str, ...]
    rows: tuple[tuple[str | None, ...], ...]
    # Source selectors per output row when rows map 1:1 to source records;
    # empty for grouped/aggregate output.
    row_refs: tuple[dict[str, Any], ...] = ()


def parse_table(
    text: str,
    kind: str,
    *,
    max_rows: int = MAX_TABLE_ROWS,
    max_columns: int = MAX_TABLE_COLUMNS,
    max_cell_chars: int = MAX_CELL_CHARS,
) -> Table:
    """Parse an approved CSV/TSV/JSON/JSONL snapshot into a bounded table.

    CSV/TSV row refs and line spans use the same logical record and physical
    line numbering as ``selectors._select_rows``. JSON rows use a json_pointer
    ref to their index in the top-level array; their line span is the whole
    document's span because per-element spans are not computed here. JSONL rows
    use a ``lines`` ref and span their single physical line.
    """
    if not isinstance(kind, str):
        raise TableError("table_kind_unsupported")
    if not isinstance(text, str):
        raise TableError("table_invalid")
    normalized = kind.lower().lstrip(".")
    if normalized in {"csv", "tsv"}:
        return _delimited_table(
            text,
            "\t" if normalized == "tsv" else ",",
            normalized,
            max_rows=max_rows,
            max_columns=max_columns,
            max_cell_chars=max_cell_chars,
        )
    if normalized == "json":
        return _json_array_table(
            text,
            max_rows=max_rows,
            max_columns=max_columns,
            max_cell_chars=max_cell_chars,
        )
    if normalized in {"jsonl", "ndjson"}:
        return _jsonl_table(
            text,
            normalized,
            max_rows=max_rows,
            max_columns=max_columns,
            max_cell_chars=max_cell_chars,
        )
    raise TableError("table_kind_unsupported")


class _JsonNumber:
    """A JSON number kept as its exact source lexeme."""

    __slots__ = ("lexeme",)

    def __init__(self, lexeme: str) -> None:
        self.lexeme = lexeme


class _JsonObject:
    """A decoded JSON object as ordered pairs; values may be nested."""

    __slots__ = ("pairs",)

    def __init__(self, pairs: tuple[tuple[str, Any], ...]) -> None:
        self.pairs = pairs


def _reject_constant(_name: str) -> Any:
    raise TableError("table_invalid")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> _JsonObject:
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise TableError("table_duplicate_key")
        seen.add(key)
    return _JsonObject(tuple(pairs))


def _json_loads(text: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_JsonNumber,
            parse_int=_JsonNumber,
            parse_constant=_reject_constant,
        )
    except TableError:
        raise
    except (ValueError, RecursionError):
        raise TableError("table_invalid") from None


def _flat_pairs(document: Any) -> tuple[tuple[str, Any], ...]:
    if not isinstance(document, _JsonObject):
        raise TableError("table_invalid")
    for key, value in document.pairs:
        if key == "":
            raise TableError("table_header_invalid")
        if isinstance(value, (_JsonObject, list)):
            raise TableError("table_nested_value")
    return document.pairs


def _json_cell(value: Any, max_cell_chars: int) -> str | None:
    if value is None:
        return None
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, _JsonNumber):
        cell = value.lexeme
    elif isinstance(value, str):
        cell = value
    else:
        raise TableError("table_invalid")
    if len(cell) > max_cell_chars:
        raise TableError("table_cell_too_large")
    return cell


def _object_table(
    kind: str,
    records: Iterable[tuple[dict[str, Any], int, int, tuple[tuple[str, Any], ...]]],
    *,
    max_rows: int,
    max_columns: int,
    max_cell_chars: int,
) -> Table:
    """Build a table from flat JSON objects, columns in first-seen key order."""
    columns: dict[str, None] = {}
    staged: list[tuple[dict[str, Any], int, int, dict[str, str | None]]] = []
    for ref, line_start, line_end, pairs in records:
        if len(staged) >= max_rows:
            raise TableError("table_too_many_rows")
        cells: dict[str, str | None] = {}
        for key, value in pairs:
            if len(key) > max_cell_chars:
                raise TableError("table_cell_too_large")
            if key not in columns:
                columns[key] = None
                if len(columns) > max_columns:
                    raise TableError("table_too_many_columns")
            cells[key] = _json_cell(value, max_cell_chars)
        staged.append((ref, line_start, line_end, cells))
    names = tuple(columns)
    rows = tuple(
        Row(ref, line_start, line_end, tuple(cells.get(name) for name in names))
        for ref, line_start, line_end, cells in staged
    )
    return Table(kind=kind, columns=names, rows=rows)


def _json_array_table(text: str, *, max_rows: int, max_columns: int, max_cell_chars: int) -> Table:
    document = _json_loads(text)
    if not isinstance(document, list):
        raise TableError("table_invalid")
    # Spans cover the whole document; see parse_table's docstring.
    document_lines = len(split_physical_lines(text)) or 1

    def records() -> Iterator[tuple[dict[str, Any], int, int, tuple[tuple[str, Any], ...]]]:
        for index, element in enumerate(document):
            yield (
                {"type": "json_pointer", "pointer": f"/{index}"},
                1,
                document_lines,
                _flat_pairs(element),
            )

    return _object_table(
        "json",
        records(),
        max_rows=max_rows,
        max_columns=max_columns,
        max_cell_chars=max_cell_chars,
    )


def _jsonl_table(
    text: str, kind: str, *, max_rows: int, max_columns: int, max_cell_chars: int
) -> Table:
    def records() -> Iterator[tuple[dict[str, Any], int, int, tuple[tuple[str, Any], ...]]]:
        for number, line in enumerate(split_physical_lines(text), 1):
            if line.strip(" \t\r\n") == "":
                continue
            yield (
                {"type": "lines", "start": number, "end": number},
                number,
                number,
                _flat_pairs(_json_loads(line)),
            )

    return _object_table(
        kind,
        records(),
        max_rows=max_rows,
        max_columns=max_columns,
        max_cell_chars=max_cell_chars,
    )


def _csv_header(record: list[str], *, max_columns: int, max_cell_chars: int) -> tuple[str, ...]:
    if not record:
        raise TableError("table_header_invalid")
    if len(record) > max_columns:
        raise TableError("table_too_many_columns")
    seen: set[str] = set()
    for name in record:
        if name == "" or name in seen:
            raise TableError("table_header_invalid")
        if len(name) > max_cell_chars:
            raise TableError("table_cell_too_large")
        seen.add(name)
    return tuple(record)


def _delimited_table(
    text: str,
    delimiter: str,
    kind: str,
    *,
    max_rows: int,
    max_columns: int,
    max_cell_chars: int,
) -> Table:
    header: tuple[str, ...] | None = None
    rows: list[Row] = []
    # csv.field_size_limit is process-global: raise it for this parse only,
    # under the lock shared with selectors, and always restore it.
    with _CSV_FIELD_LIMIT_LOCK:
        previous_limit = csv.field_size_limit(max(len(text), 1))
        try:
            reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
            number = 0
            physical_start = 1
            for record in reader:
                number += 1
                physical_end = max(physical_start, reader.line_num)
                line_start, line_end = physical_start, physical_end
                physical_start = physical_end + 1
                if header is None:
                    header = _csv_header(
                        record, max_columns=max_columns, max_cell_chars=max_cell_chars
                    )
                    continue
                # A blank line is a record for selector numbering, but not a
                # data row: counting it would inflate aggregates. Later refs
                # keep their logical numbers.
                if not record:
                    continue
                if len(rows) >= max_rows:
                    raise TableError("table_too_many_rows")
                if len(record) > len(header):
                    raise TableError("table_row_width")
                for cell in record:
                    if len(cell) > max_cell_chars:
                        raise TableError("table_cell_too_large")
                values = tuple(record) + (None,) * (len(header) - len(record))
                rows.append(
                    Row(
                        {"type": "rows", "start": number, "end": number},
                        line_start,
                        line_end,
                        values,
                    )
                )
        except csv.Error:
            raise TableError("table_invalid") from None
        finally:
            csv.field_size_limit(previous_limit)
    if header is None:
        raise TableError("table_header_invalid")
    return Table(kind=kind, columns=header, rows=tuple(rows))


__all__ = [
    "MAX_CELL_CHARS",
    "MAX_TABLE_COLUMNS",
    "MAX_TABLE_ROWS",
    "ResultTable",
    "Row",
    "Table",
    "TableError",
    "parse_table",
]
