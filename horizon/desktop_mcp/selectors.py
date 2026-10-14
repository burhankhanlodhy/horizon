"""Strict exact selectors for text, Markdown, JSON, CSV, and TSV snapshots."""

from __future__ import annotations

import csv
import io
import json
import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .text_lines import physical_line_number, split_physical_lines

if TYPE_CHECKING:
    from .extractors import DocumentBlock

_MAX_PAGE_SPAN = 200
_MAX_PARAGRAPH_SPAN = 2000
_MAX_CSV_FIELD_CHARS = 1 * 1024 * 1024
_MAX_CSV_ROWS = 100_000
_MAX_CSV_COLUMNS = 10_000
_CSV_FIELD_LIMIT_LOCK = threading.Lock()


def _check_csv_column_count(text: str, delimiter: str) -> None:
    """Reject very wide records before csv.reader allocates their field list."""

    columns = 1
    at_field_start = True
    in_quotes = False
    index = 0
    while index < len(text):
        character = text[index]
        if in_quotes:
            if character == '"':
                if index + 1 < len(text) and text[index + 1] == '"':
                    index += 2
                    continue
                in_quotes = False
        elif character == '"' and at_field_start:
            in_quotes = True
            at_field_start = False
        elif character == delimiter:
            columns += 1
            if columns > _MAX_CSV_COLUMNS:
                raise SelectorError("csv_column_limit")
            at_field_start = True
        elif character in "\r\n":
            if character == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
                index += 1
            columns = 1
            at_field_start = True
        else:
            at_field_start = False
        index += 1


class SelectorError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SelectedScope:
    text: str
    selector: dict[str, Any]
    coverage: str
    selected_lines: dict[str, int] | None
    scope_kind: Literal["lines", "section", "json_pointer", "rows", "pages", "paragraphs", "table"]
    start_offset: int
    end_offset: int
    line_start: int
    line_end: int
    paginable: bool
    units: tuple[tuple[int, int, str], ...] = ()


_HEADING = re.compile(r"^ {0,3}(?P<marks>#{1,6})[ \t]+(?P<title>.*?)(?:[ \t]+#+[ \t]*)?$")
_FENCE = re.compile(r"^ {0,3}(?P<mark>`{3,}|~{3,})(?P<tail>.*)$")
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 100_000


def _json_pointer_tokens(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise SelectorError("selector_invalid")
    result: list[str] = []
    for token in pointer[1:].split("/"):
        decoded: list[str] = []
        index = 0
        while index < len(token):
            if token[index] != "~":
                decoded.append(token[index])
                index += 1
                continue
            if index + 1 >= len(token) or token[index + 1] not in "01":
                raise SelectorError("selector_invalid")
            decoded.append("~" if token[index + 1] == "0" else "/")
            index += 2
        result.append("".join(decoded))
    return result


class _JsonSpanParser:
    """Validate JSON and locate an exact value span without re-serializing it."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0
        self.nodes = 0

    def parse_pointer(self, pointer: list[str]) -> tuple[int, int]:
        self._space()
        found = self._value(pointer, 0, select=True)
        self._space()
        if self.i != len(self.text):
            raise SelectorError("json_invalid")
        if found is None:
            raise SelectorError("selector_not_found")
        return found

    def _space(self) -> None:
        while self.i < len(self.text) and self.text[self.i] in " \t\r\n":
            self.i += 1

    def _string(self) -> str:
        if self.i >= len(self.text) or self.text[self.i] != '"':
            raise SelectorError("json_invalid")
        try:
            value, end = json.JSONDecoder().raw_decode(self.text, self.i)
        except (json.JSONDecodeError, RecursionError):
            raise SelectorError("json_invalid") from None
        if not isinstance(value, str):
            raise SelectorError("json_invalid")
        self.i = end
        return value

    def _value(self, path: list[str], depth: int, *, select: bool) -> tuple[int, int] | None:
        if depth > _MAX_JSON_DEPTH:
            raise SelectorError("json_too_complex")
        self.nodes += 1
        if self.nodes > _MAX_JSON_NODES:
            raise SelectorError("json_too_complex")
        self._space()
        start = self.i
        if self.i >= len(self.text):
            raise SelectorError("json_invalid")
        char = self.text[self.i]
        found: tuple[int, int] | None = None
        if char == "{":
            self.i += 1
            self._space()
            seen: set[str] = set()
            if self.i < len(self.text) and self.text[self.i] == "}":
                self.i += 1
            else:
                while True:
                    self._space()
                    key = self._string()
                    if key in seen:
                        raise SelectorError("json_duplicate_key")
                    seen.add(key)
                    self._space()
                    if self.i >= len(self.text) or self.text[self.i] != ":":
                        raise SelectorError("json_invalid")
                    self.i += 1
                    matches = select and bool(path) and key == path[0]
                    child_path = path[1:] if matches else []
                    child = self._value(child_path, depth + 1, select=matches)
                    if matches and child is not None:
                        found = child
                    self._space()
                    if self.i < len(self.text) and self.text[self.i] == ",":
                        self.i += 1
                        continue
                    if self.i < len(self.text) and self.text[self.i] == "}":
                        self.i += 1
                        break
                    raise SelectorError("json_invalid")
        elif char == "[":
            self.i += 1
            self._space()
            index = 0
            if self.i < len(self.text) and self.text[self.i] == "]":
                self.i += 1
            else:
                while True:
                    matches = select and bool(path) and path[0] == str(index)
                    child_path = path[1:] if matches else []
                    child = self._value(child_path, depth + 1, select=matches)
                    if matches and child is not None:
                        found = child
                    index += 1
                    self._space()
                    if self.i < len(self.text) and self.text[self.i] == ",":
                        self.i += 1
                        continue
                    if self.i < len(self.text) and self.text[self.i] == "]":
                        self.i += 1
                        break
                    raise SelectorError("json_invalid")
        elif char == '"':
            self._string()
        else:
            while self.i < len(self.text) and self.text[self.i] not in ",]} \t\r\n":
                self.i += 1
            if self.i == start:
                raise SelectorError("json_invalid")
            raw = self.text[start : self.i]
            try:
                json.loads(raw, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
            except (json.JSONDecodeError, ValueError, RecursionError):
                raise SelectorError("json_invalid") from None
        end = self.i
        if select and path == [] and found is None:
            found = (start, end)
        return found


def _select_lines(text: str, selector: dict[str, Any]) -> SelectedScope:
    start, end = selector["start"], selector["end"]
    lines = split_physical_lines(text) or [""]
    if end > len(lines):
        raise SelectorError("selector_out_of_range")
    start_offset = sum(len(line) for line in lines[: start - 1])
    end_offset = start_offset + sum(len(line) for line in lines[start - 1 : end])
    units: list[tuple[int, int, str]] = []
    offset = start_offset
    for number, line in enumerate(lines[start - 1 : end], start):
        units.append((number, number, line))
        offset += len(line)
    return SelectedScope(
        text[start_offset:end_offset],
        {"type": "lines", "start": start, "end": end},
        "exact selected lines at immutable snapshot version",
        {"start": start, "end": end},
        "lines",
        start_offset,
        end_offset,
        start,
        end,
        True,
        tuple(units),
    )


def markdown_headings(text: str) -> list[tuple[int, int, str]]:
    """Return ATX headings outside fenced code blocks, with source line spans.

    Setext headings are intentionally outside this selector's supported
    grammar so a bounded section start is never guessed from surrounding text.
    """
    lines = split_physical_lines(text) or [""]
    headings: list[tuple[int, int, str]] = []
    fence_char: str | None = None
    fence_length = 0
    for number, line in enumerate(lines, 1):
        bare = line.rstrip("\r\n")
        fence = _FENCE.match(bare)
        if fence:
            marker = fence.group("mark")
            if fence_char is None:
                if marker.startswith("`") and "`" in fence.group("tail"):
                    continue
                fence_char, fence_length = marker[0], len(marker)
            elif (
                marker[0] == fence_char
                and len(marker) >= fence_length
                and not fence.group("tail").strip(" \t")
            ):
                fence_char, fence_length = None, 0
            continue
        if fence_char is not None:
            continue
        match = _HEADING.match(bare)
        if match:
            headings.append((number, len(match.group("marks")), match.group("title").strip()))
    return headings


def _select_markdown_section(text: str, selector: dict[str, Any]) -> SelectedScope:
    heading = selector["heading"].strip()
    occurrence = selector.get("occurrence", 1)
    lines = split_physical_lines(text) or [""]
    headings = markdown_headings(text)
    matching = [item for item in headings if item[2].casefold() == heading.casefold()]
    if occurrence < 1 or occurrence > len(matching):
        raise SelectorError("selector_not_found")
    start, level, _title = matching[occurrence - 1]
    end = len(lines)
    for number, candidate_level, _candidate_title in headings:
        if number > start and candidate_level <= level:
            end = number - 1
            break
    scope = _select_lines(text, {"type": "lines", "start": start, "end": max(start, end)})
    return SelectedScope(
        scope.text,
        {"type": "markdown_section", "heading": heading, "occurrence": occurrence},
        "exact Markdown section at immutable snapshot version",
        {"start": scope.line_start, "end": scope.line_end},
        "section",
        scope.start_offset,
        scope.end_offset,
        scope.line_start,
        scope.line_end,
        True,
        scope.units,
    )


def _select_json_pointer(text: str, selector: dict[str, Any]) -> SelectedScope:
    pointer = selector["pointer"]
    if len(pointer) > 1024:
        raise SelectorError("selector_invalid")
    tokens = _json_pointer_tokens(pointer)
    start, end = _JsonSpanParser(text).parse_pointer(tokens)
    selected = text[start:end]
    start_line = physical_line_number(text, start)
    end_line = physical_line_number(text, end)
    return SelectedScope(
        selected,
        {"type": "json_pointer", "pointer": pointer},
        "exact JSON pointer value; scalar spelling preserved",
        {"start": start_line, "end": end_line},
        "json_pointer",
        start,
        end,
        start_line,
        end_line,
        False,
        ((start_line, end_line, selected),),
    )


def _select_rows(text: str, selector: dict[str, Any], kind: str) -> SelectedScope:
    if kind.lower().lstrip(".") not in {"csv", "tsv"}:
        raise SelectorError("selector_format_unsupported")
    start, end = selector["start"], selector["end"]
    delimiter = "\t" if kind.lower().lstrip(".") == "tsv" else ","
    _check_csv_column_count(text, delimiter)
    # csv.field_size_limit is process-global. Scope the larger limit to this
    # parse and restore it even when malformed or oversized input raises.
    # The source byte cap is not enough to bound per-row Python object growth.
    stream = io.StringIO(text, newline="")
    records: list[tuple[int, int]] = []
    physical_start = 1
    with _CSV_FIELD_LIMIT_LOCK:
        previous_field_limit = csv.field_size_limit(_MAX_CSV_FIELD_CHARS)
        try:
            reader = csv.reader(stream, delimiter=delimiter, strict=True)
            try:
                for _row in reader:
                    if len(records) >= _MAX_CSV_ROWS:
                        raise SelectorError("csv_row_limit")
                    if len(_row) > _MAX_CSV_COLUMNS:
                        raise SelectorError("csv_column_limit")
                    physical_end = max(physical_start, reader.line_num)
                    records.append((physical_start, physical_end))
                    physical_start = physical_end + 1
            except csv.Error as exc:
                code = (
                    "csv_field_too_large"
                    if "field larger than field limit" in str(exc).lower()
                    else "csv_invalid"
                )
                raise SelectorError(code) from None
        finally:
            csv.field_size_limit(previous_field_limit)
    if end > len(records):
        raise SelectorError("selector_out_of_range")
    physical_first = records[start - 1][0]
    physical_last = records[end - 1][1]
    lines = split_physical_lines(text) or [""]
    selected = "".join(lines[physical_first - 1 : physical_last])
    start_offset = sum(len(line) for line in lines[: physical_first - 1])
    end_offset = start_offset + len(selected)
    units = tuple(
        (record_first, record_last, "".join(lines[record_first - 1 : record_last]))
        for record_first, record_last in records[start - 1 : end]
    )
    return SelectedScope(
        selected,
        {"type": "rows", "start": start, "end": end},
        "exact selected logical records (row 1 includes the header when present)",
        {"start": physical_first, "end": physical_last},
        "rows",
        start_offset,
        end_offset,
        physical_first,
        physical_last,
        True,
        units,
    )


def _select_document_blocks(
    text: str,
    selector: dict[str, Any],
    *,
    kind: str,
    blocks: tuple[DocumentBlock, ...],
) -> SelectedScope:
    """Select whole PDF pages or DOCX paragraphs/tables by their block spans.

    Empty blocks (a PDF page with no extractable text) are never units: they
    have no physical lines to paginate, so they appear only in the coverage
    note and the selection's zero-length line/offset position. Pagination
    therefore never splits a page or paragraph.
    """
    selector_type = selector["type"]
    normalized = kind.lower().lstrip(".")
    required_kind = "pdf" if selector_type == "page" else "docx"
    if normalized != required_kind or not blocks:
        raise SelectorError("selector_format_unsupported")
    wanted_kinds = {
        "page": {"page"},
        "paragraph": {"paragraph", "heading"},
        "table": {"table"},
    }[selector_type]
    candidates = [block for block in blocks if block.kind in wanted_kinds]
    if selector_type == "table":
        selected_blocks = [block for block in candidates if block.index == selector["index"]]
        if not candidates or not selected_blocks:
            raise SelectorError("selector_out_of_range")
    else:
        start, end = selector["start"], selector["end"]
        highest = max((block.index for block in candidates), default=0)
        if end > highest:
            raise SelectorError("selector_out_of_range")
        selected_blocks = sorted(
            (block for block in candidates if start <= block.index <= end),
            key=lambda block: block.index,
        )
        if not selected_blocks:
            raise SelectorError("selector_out_of_range")

    lines = split_physical_lines(text) or [""]
    units: list[tuple[int, int, str]] = []
    empty_pages: list[int] = []
    for block in selected_blocks:
        if block.line_start > block.line_end:
            if selector_type == "page":
                empty_pages.append(block.index)
            continue
        if block.line_start < 1 or block.line_end > len(lines):
            raise SelectorError("selector_format_unsupported")
        units.append(
            (
                block.line_start,
                block.line_end,
                "".join(lines[block.line_start - 1 : block.line_end]),
            )
        )

    if units:
        first_line, last_line = units[0][0], units[-1][1]
        start_offset = sum(len(line) for line in lines[: first_line - 1])
        selected_text = "".join(unit[2] for unit in units)
        end_offset = start_offset + len(selected_text)
    else:
        # Only empty pages: anchor the zero-length position at the first
        # selected page's start so offsets and line refs stay meaningful.
        anchor = min(
            max(1, selected_blocks[0].line_start),
            len(lines),
        )
        first_line = last_line = anchor
        start_offset = end_offset = sum(len(line) for line in lines[: anchor - 1])
        selected_text = ""

    if selector_type == "table":
        coverage = "exact extracted DOCX table at immutable snapshot version"
        scope_kind: Literal["pages", "paragraphs", "table"] = "table"
        normalized_selector: dict[str, Any] = {"type": "table", "index": selector["index"]}
    elif selector_type == "page":
        coverage = "exact extracted PDF text for selected pages at immutable snapshot version"
        scope_kind = "pages"
        normalized_selector = {"type": "page", "start": selector["start"], "end": selector["end"]}
    else:
        coverage = "exact extracted DOCX paragraphs and headings at immutable snapshot version"
        scope_kind = "paragraphs"
        normalized_selector = {
            "type": "paragraph",
            "start": selector["start"],
            "end": selector["end"],
        }
    if empty_pages:
        coverage += "; pages without extractable text: " + ", ".join(
            str(number) for number in empty_pages
        )
    return SelectedScope(
        selected_text,
        normalized_selector,
        coverage,
        {"start": first_line, "end": last_line},
        scope_kind,
        start_offset,
        end_offset,
        first_line,
        last_line,
        True,
        tuple(units) if units else (),
    )


def select_exact(
    text: str,
    selector: dict[str, Any],
    *,
    kind: str,
    blocks: tuple[DocumentBlock, ...] = (),
) -> SelectedScope:
    """Validate and select an exact source span; never reserialize source data."""
    if not isinstance(selector, dict) or not isinstance(selector.get("type"), str):
        raise SelectorError("selector_invalid")
    selector_type = selector["type"]
    expected_fields = {
        "lines": {"type", "start", "end"},
        "markdown_section": {"type", "heading"},
        "json_pointer": {"type", "pointer"},
        "rows": {"type", "start", "end"},
        "page": {"type", "start", "end"},
        "paragraph": {"type", "start", "end"},
        "table": {"type", "index"},
    }
    fields = expected_fields.get(selector_type)
    if fields is None:
        raise SelectorError("selector_invalid")
    if selector_type == "markdown_section":
        if set(selector) not in (fields, fields | {"occurrence"}):
            raise SelectorError("selector_invalid")
        selector = {**selector, "occurrence": selector.get("occurrence", 1)}
    elif set(selector) != fields:
        raise SelectorError("selector_invalid")
    if selector_type in {"lines", "rows"}:
        start, end = selector["start"], selector["end"]
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 1
            or end < start
            or end - start > 2000
        ):
            raise SelectorError("selector_out_of_range")
    if selector_type == "lines":
        return _select_lines(text, selector)
    if selector_type == "markdown_section":
        heading = selector["heading"]
        occurrence = selector["occurrence"]
        if (
            not isinstance(heading, str)
            or not heading.strip()
            or len(heading) > 256
            or isinstance(occurrence, bool)
            or not isinstance(occurrence, int)
            or not 1 <= occurrence <= 100
        ):
            raise SelectorError("selector_invalid")
        return _select_markdown_section(text, selector)
    if selector_type == "json_pointer":
        pointer = selector["pointer"]
        if not isinstance(pointer, str) or len(pointer) > 1024:
            raise SelectorError("selector_invalid")
        return _select_json_pointer(text, selector)
    if selector_type in {"page", "paragraph", "table"}:
        if selector_type == "table":
            index = selector["index"]
            if isinstance(index, bool) or not isinstance(index, int) or index < 1:
                raise SelectorError("selector_invalid")
        else:
            start, end = selector["start"], selector["end"]
            span_cap = _MAX_PAGE_SPAN if selector_type == "page" else _MAX_PARAGRAPH_SPAN
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, int)
                or not isinstance(end, int)
                or start < 1
                or end < start
                or end - start + 1 > span_cap
            ):
                raise SelectorError("selector_invalid")
        return _select_document_blocks(text, selector, kind=kind, blocks=tuple(blocks))
    return _select_rows(text, selector, kind)


__all__ = ["SelectedScope", "SelectorError", "markdown_headings", "select_exact"]
