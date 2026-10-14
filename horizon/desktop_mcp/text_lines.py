"""Physical text line boundaries for source byte/offset coordinates."""

from __future__ import annotations

import re

_LINE_ENDING = re.compile(r"\r\n|\r|\n")


def split_physical_lines(text: str) -> list[str]:
    """Split on CRLF, LF, or CR only, retaining each original terminator."""
    if not text:
        return []
    result: list[str] = []
    start = 0
    for match in _LINE_ENDING.finditer(text):
        result.append(text[start : match.end()])
        start = match.end()
    if start < len(text):
        result.append(text[start:])
    return result


def physical_line_number(text: str, offset: int) -> int:
    """Return the one-based physical line containing the character at offset."""
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= len(text):
        raise ValueError("offset is outside source text")
    return sum(1 for _match in _LINE_ENDING.finditer(text, 0, offset)) + 1


__all__ = ["physical_line_number", "split_physical_lines"]
