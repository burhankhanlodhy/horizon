"""Lossless table encoding for parsed JSON values (TOON-style, indentation based).

``encode`` turns a JSON value into a compact text form and ``decode`` is its
exact inverse. Both are pure and deterministic; neither touches the network or
the clock. Compact callers verify ``decode(encode(value))`` against the source
before using the text (see ``compression.compact_json``).

Format summary (two spaces of indentation per nesting level):

- ``key: v`` is a field with a scalar value; ``key: {}`` is an empty object;
  ``key:`` opens an object whose fields sit one level deeper.
- ``key[N]: a,b,c`` is an array of N scalars; ``key[0]:`` is an empty array.
- ``key[N]:`` followed by ``- item`` lines one level deeper is a list. An item
  is a scalar, ``{}``, an object (its first field on the dash line) or a nested
  array header.
- ``key[N]{c1,c2}:`` is a table: N rows one level deeper, each a comma-separated
  row of cells in column order. A cell that holds an object or array is left
  empty in the row, and that value follows as a field named by its column, one
  level below the row.
- A top-level array uses an unnamed header such as ``[N]:``; a top-level object
  has no header.

Scalars: ``null``, ``true``, ``false``, numbers keep their source spelling, and
strings are bare when that is unambiguous. A string is quoted with JSON escapes
when it is empty, looks like a keyword or number, has leading or trailing
whitespace, contains a control character, a line break, a comma, a colon, a
quote, a backslash, a bracket or a brace. Object keys follow the same rule.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

__all__ = ["decode", "encode", "parse_lexical", "same_json"]

_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_DIGITS = re.compile(r"0|[1-9][0-9]*")
_KEYWORDS = frozenset({"null", "true", "false"})
_NUMBER_LIKE = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_BARE_FORBIDDEN = frozenset(',:"\\[]{}')
_LINE_BREAKS = frozenset("\u0085\u2028\u2029")


class _LexDecimal(Decimal):
    """A Decimal that remembers the source lexeme, so its spelling is kept."""


class _LexInt(int):
    """An int that remembers the source lexeme, so its spelling is kept."""


def _lex_float(text: str) -> _LexDecimal:
    value = _LexDecimal(text)
    value.lexeme = text  # type: ignore[attr-defined]
    return value


def _lex_int(text: str) -> _LexInt:
    value = _LexInt(text)
    value.lexeme = text  # type: ignore[attr-defined]
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def parse_lexical(text: str) -> Any:
    """Parse JSON like the compact path does, keeping each number's source spelling."""
    return json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_float=_lex_float,
        parse_int=_lex_int,
        parse_constant=_reject_constant,
    )


def same_json(left: Any, right: Any) -> bool:
    """Compare two parsed JSON values by type, key order, and exact number value."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if left is None or right is None:
        return left is right
    if isinstance(left, str):
        return isinstance(right, str) and left == right
    if isinstance(left, int):
        return isinstance(right, int) and left == right
    if isinstance(left, Decimal):
        return isinstance(right, Decimal) and left.as_tuple() == right.as_tuple()
    if isinstance(left, list):
        return (
            isinstance(right, list)
            and len(left) == len(right)
            and all(same_json(a, b) for a, b in zip(left, right, strict=True))
        )
    if isinstance(left, dict):
        return (
            isinstance(right, dict)
            and list(left) == list(right)
            and all(same_json(left[key], right[key]) for key in left)
        )
    return False


# ---------------------------------------------------------------- encoding


def _needs_quote(text: str) -> bool:
    if not text or text in _KEYWORDS or text[0] == "-" or _NUMBER_LIKE.fullmatch(text):
        return True
    if text != text.strip():
        return True
    for char in text:
        if char in _BARE_FORBIDDEN or char < " " or char == "\x7f" or char in _LINE_BREAKS:
            return True
    return False


def _text(value: str) -> str:
    return json.dumps(value, ensure_ascii=False) if _needs_quote(value) else value


def _key(key: Any) -> str:
    if not isinstance(key, str):
        raise ValueError("JSON object keys must be strings")
    return _text(key)


def _token(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, int):
        return getattr(value, "lexeme", None) or str(int(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite number")
        return getattr(value, "lexeme", None) or str(value)
    raise ValueError(f"unsupported JSON value type: {type(value).__name__}")


def _table_columns(items: list[Any]) -> tuple[str, ...] | None:
    first = items[0]
    if not isinstance(first, dict) or not first:
        return None
    columns = tuple(first)
    for item in items:
        if not isinstance(item, dict) or tuple(item) != columns:
            return None
    return columns


def _entry(head: str, value: Any, level: int) -> list[tuple[int, str]]:
    """Lines for one keyed value; ``head`` is its encoded key."""
    if isinstance(value, dict):
        if not value:
            return [(level, f"{head}: {{}}")]
        lines = [(level, f"{head}:")]
        for key, item in value.items():
            lines.extend(_entry(_key(key), item, level + 1))
        return lines
    if isinstance(value, list):
        return _array_lines(head, value, level)
    return [(level, f"{head}: {_token(value)}")]


def _array_lines(head: str, items: list[Any], level: int) -> list[tuple[int, str]]:
    count = len(items)
    if count == 0:
        return [(level, f"{head}[0]:")]
    if not any(isinstance(item, (dict, list)) for item in items):
        cells = ",".join(_token(item) for item in items)
        return [(level, f"{head}[{count}]: {cells}")]
    columns = _table_columns(items)
    if columns is not None:
        return _table_lines(head, items, columns, level)
    lines = [(level, f"{head}[{count}]:")]
    for item in items:
        lines.extend(_list_item_lines(item, level + 1))
    return lines


def _table_lines(
    head: str, rows: list[dict[str, Any]], columns: tuple[str, ...], level: int
) -> list[tuple[int, str]]:
    header = ",".join(_key(column) for column in columns)
    lines = [(level, f"{head}[{len(rows)}]{{{header}}}:")]
    cell_level = level + 1
    for row in rows:
        cells: list[str] = []
        nested: list[tuple[str, Any]] = []
        for column in columns:
            value = row[column]
            if isinstance(value, (dict, list)):
                cells.append("")
                nested.append((column, value))
            else:
                cells.append(_token(value))
        lines.append((cell_level, ",".join(cells)))
        for column, value in nested:
            lines.extend(_entry(_key(column), value, cell_level + 1))
    return lines


def _list_item_lines(item: Any, dash_level: int) -> list[tuple[int, str]]:
    if isinstance(item, dict):
        if not item:
            return [(dash_level, "- {}")]
        fields: list[tuple[int, str]] = []
        for key, value in item.items():
            fields.extend(_entry(_key(key), value, dash_level + 1))
    elif isinstance(item, list):
        fields = _array_lines("", item, dash_level + 1)
    else:
        return [(dash_level, f"- {_token(item)}")]
    _, first_text = fields[0]
    fields[0] = (dash_level, f"- {first_text}")
    return fields


def encode(value: Any) -> str:
    """Return the table text for a parsed JSON value.

    Raises ValueError for values JSON cannot represent exactly (floats,
    non-finite numbers, lone surrogates).
    """
    if isinstance(value, dict):
        if not value:
            text = "{}"
        else:
            lines: list[tuple[int, str]] = []
            for key, item in value.items():
                lines.extend(_entry(_key(key), item, 0))
            text = _render(lines)
    elif isinstance(value, list):
        text = _render(_array_lines("", value, 0))
    else:
        text = _token(value)
    text.encode("utf-8")
    return text


def _render(lines: list[tuple[int, str]]) -> str:
    return "\n".join("  " * level + text for level, text in lines)


# ---------------------------------------------------------------- decoding


def _scan_quoted(text: str, pos: int) -> tuple[str, int]:
    index = pos + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return text[pos : index + 1], index + 1
        index += 1
    raise ValueError("unterminated quoted text")


def _scan_head(text: str, pos: int = 0, stops: str = ":[") -> tuple[str, int]:
    if pos < len(text) and text[pos] == '"':
        return _scan_quoted(text, pos)
    end = pos
    while end < len(text) and text[end] not in stops:
        end += 1
    return text[pos:end], end


def _decode_key(raw: str) -> str:
    if raw.startswith('"'):
        value = json.loads(raw)
        if not isinstance(value, str):
            raise ValueError("malformed quoted key")
        return value
    if _needs_quote(raw):
        raise ValueError("unquoted key that needs quotes")
    return raw


def _parse_token(raw: str) -> Any:
    if raw == "":
        raise ValueError("empty value")
    if raw.startswith('"'):
        value = json.loads(raw)
        if not isinstance(value, str):
            raise ValueError("malformed quoted value")
        return value
    if raw == "null":
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw[0] == "-" or _NUMBER_LIKE.fullmatch(raw):
        if not _NUMBER.fullmatch(raw):
            raise ValueError("malformed number")
        if "." in raw or "e" in raw or "E" in raw:
            return Decimal(raw)
        return int(raw)
    if _needs_quote(raw):
        raise ValueError("unquoted value that needs quotes")
    return raw


def _split_cells(text: str) -> list[str]:
    cells: list[str] = []
    pos = 0
    while True:
        if pos < len(text) and text[pos] == '"':
            raw, pos = _scan_quoted(text, pos)
        else:
            end = text.find(",", pos)
            if end == -1:
                end = len(text)
            raw, pos = text[pos:end], end
        cells.append(raw)
        if pos == len(text):
            return cells
        if text[pos] != ",":
            raise ValueError("malformed cell separator")
        pos += 1


def _read_columns(text: str, pos: int) -> tuple[list[str], int]:
    columns: list[str] = []
    while True:
        raw, pos = _scan_head(text, pos, stops=",}")
        columns.append(_decode_key(raw))
        if pos >= len(text):
            raise ValueError("unterminated table header")
        if text[pos] == "}":
            if len(set(columns)) != len(columns):
                raise ValueError("duplicate table column")
            return columns, pos + 1
        if text[pos] != ",":
            raise ValueError("malformed table header")
        pos += 1


def _parse_fields(lines: list[tuple[int, str]], pos: int, level: int) -> tuple[dict[str, Any], int]:
    fields: dict[str, Any] = {}
    while pos < len(lines) and lines[pos][0] == level:
        key, value, pos = _parse_entry(lines[pos][1], lines, pos + 1, level + 1)
        if key in fields:
            raise ValueError("duplicate object key")
        fields[key] = value
    return fields, pos


def _parse_entry(
    text: str, lines: list[tuple[int, str]], pos: int, child_level: int
) -> tuple[str, Any, int]:
    raw, cursor = _scan_head(text)
    key = _decode_key(raw)
    value, pos = _parse_value(text[cursor:], lines, pos, child_level)
    return key, value, pos


def _parse_value(
    rest: str, lines: list[tuple[int, str]], pos: int, child_level: int
) -> tuple[Any, int]:
    if rest.startswith(": "):
        token = rest[2:]
        if token == "{}":
            return {}, pos
        return _parse_token(token), pos
    if rest == ":":
        fields, pos = _parse_fields(lines, pos, child_level)
        if not fields:
            raise ValueError("missing nested block")
        return fields, pos
    if rest.startswith("["):
        return _parse_array(rest, lines, pos, child_level)
    raise ValueError("malformed entry")


def _parse_array(
    text: str, lines: list[tuple[int, str]], pos: int, child_level: int
) -> tuple[list[Any], int]:
    close = text.find("]")
    if not text.startswith("[") or close < 0 or not _DIGITS.fullmatch(text[1:close]):
        raise ValueError("malformed array header")
    count = int(text[1:close])
    cursor = close + 1
    columns: list[str] | None = None
    if cursor < len(text) and text[cursor] == "{":
        columns, cursor = _read_columns(text, cursor + 1)
    tail = text[cursor:]
    if tail == ":":
        if count == 0:
            if columns is not None:
                raise ValueError("empty table")
            return [], pos
        if columns is not None:
            return _parse_rows(columns, count, lines, pos, child_level)
        return _parse_items(count, lines, pos, child_level)
    if tail.startswith(": ") and columns is None and count > 0:
        cells = _split_cells(tail[2:])
        if len(cells) != count:
            raise ValueError("inline array count mismatch")
        return [_parse_token(cell) for cell in cells], pos
    raise ValueError("malformed array header")


def _parse_rows(
    columns: list[str],
    count: int,
    lines: list[tuple[int, str]],
    pos: int,
    child_level: int,
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    for _ in range(count):
        if pos >= len(lines) or lines[pos][0] != child_level:
            raise ValueError("missing table row")
        cells = _split_cells(lines[pos][1])
        pos += 1
        if len(cells) != len(columns):
            raise ValueError("table row width mismatch")
        slots: list[Any] = [None] * len(columns)
        pending: list[int] = []
        for index, cell in enumerate(cells):
            if cell == "":
                pending.append(index)
            else:
                slots[index] = _parse_token(cell)
        for index in pending:
            if pos >= len(lines) or lines[pos][0] != child_level + 1:
                raise ValueError("missing nested table cell")
            key, value, pos = _parse_entry(lines[pos][1], lines, pos + 1, child_level + 2)
            if key != columns[index] or not isinstance(value, (dict, list)):
                raise ValueError("misplaced nested table cell")
            slots[index] = value
        rows.append(dict(zip(columns, slots, strict=True)))
    return rows, pos


def _parse_items(
    count: int, lines: list[tuple[int, str]], pos: int, child_level: int
) -> tuple[list[Any], int]:
    items: list[Any] = []
    for _ in range(count):
        if pos >= len(lines) or lines[pos][0] != child_level:
            raise ValueError("missing list item")
        text = lines[pos][1]
        pos += 1
        if not text.startswith("- "):
            raise ValueError("malformed list item")
        content = text[2:]
        if content == "{}":
            items.append({})
            continue
        if content.startswith("["):
            value, pos = _parse_array(content, lines, pos, child_level + 2)
            items.append(value)
            continue
        raw, cursor = _scan_head(content)
        if cursor < len(content) and content[cursor] in ":[":
            key = _decode_key(raw)
            value, pos = _parse_value(content[cursor:], lines, pos, child_level + 2)
            rest, pos = _parse_fields(lines, pos, child_level + 1)
            if key in rest:
                raise ValueError("duplicate object key")
            items.append({key: value, **rest})
        else:
            items.append(_parse_token(content))
    return items, pos


def decode(text: str) -> Any:
    """Return the JSON value encoded by ``encode``. Raises ValueError when malformed."""
    lines: list[tuple[int, str]] = []
    for raw in text.split("\n"):
        stripped = raw.lstrip(" ")
        spaces = len(raw) - len(stripped)
        if spaces % 2:
            raise ValueError("odd indentation")
        lines.append((spaces // 2, stripped))
    if not lines or lines[0][0] != 0:
        raise ValueError("document must start at the top level")
    first = lines[0][1]
    if first.startswith("["):
        value, pos = _parse_array(first, lines, 1, 1)
    elif first == "{}":
        value, pos = {}, 1
    else:
        _, cursor = _scan_head(first)
        if cursor < len(first) and first[cursor] in ":[":
            value, pos = _parse_fields(lines, 0, 0)
        else:
            if len(lines) != 1:
                raise ValueError("trailing content after a scalar")
            value, pos = _parse_token(first), 1
    if pos != len(lines):
        raise ValueError("unexpected content after the document")
    return value
