"""Bounded tabular views of approved CSV/TSV/JSON snapshots for typed operations.

Cells keep their source lexemes. Typing happens only in ``operations`` when a
specification declares a column type, so parsing never coerces IDs, numbers,
or dates. Every row carries an exact selector for same-version recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    raise NotImplementedError


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
