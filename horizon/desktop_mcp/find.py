"""Literal line matching and context blocks for the find presentation mode.

Pure functions only. A query is split into terms on whitespace; a double-quoted
phrase is one term. A line matches when it contains every term, compared
case-insensitively. There is no regex. Line numbers are one-based physical
lines, as in split_physical_lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .text_lines import split_physical_lines

# A quoted phrase, or a run of non-space characters. An unmatched quote is an
# ordinary character, so it is kept as part of a plain term.
_TERM = re.compile(r'"([^"]*)"|(\S+)')


@dataclass(frozen=True)
class Block:
    """Exact source lines around one or more matching lines."""

    start_line: int
    end_line: int
    match_lines: tuple[int, ...]

    def reference(self) -> dict[str, object]:
        # The block text is carried once, in the result's top-level text.
        return {
            "start_line": self.start_line,
            "end_line": self.end_line,
            "match_lines": list(self.match_lines),
        }


def parse_terms(query: str) -> list[str]:
    """Split a query into search terms; blank quoted phrases are dropped."""
    terms: list[str] = []
    for match in _TERM.finditer(query or ""):
        if match.group(1) is not None:
            term = match.group(1)
        else:
            term = match.group(2)
        if term.strip():
            terms.append(term)
    return terms


def match_lines(text: str, terms: list[str]) -> list[int]:
    """Return one-based numbers of lines that contain every term, case-insensitively."""
    folded = [term.casefold() for term in terms]
    if not folded:
        return []
    matches: list[int] = []
    for number, line in enumerate(split_physical_lines(text), start=1):
        haystack = line.rstrip("\r\n").casefold()
        if all(term in haystack for term in folded):
            matches.append(number)
    return matches


def build_blocks(
    lines: list[str], match_line_numbers: list[int], context_lines: int
) -> list[Block]:
    """Expand each match by context_lines and merge ranges that overlap or touch."""
    if context_lines < 0:
        raise ValueError("context_lines must not be negative")
    total = len(lines)
    blocks: list[Block] = []
    for number in sorted(set(match_line_numbers)):
        start = max(1, number - context_lines)
        end = min(total, number + context_lines)
        if blocks and start <= blocks[-1].end_line + 1:
            previous = blocks[-1]
            blocks[-1] = Block(
                previous.start_line,
                max(previous.end_line, end),
                (*previous.match_lines, number),
            )
        else:
            blocks.append(Block(start, end, (number,)))
    return blocks


def block_text(lines: list[str], block: Block) -> str:
    """Exact source text of a block, line terminators included."""
    return "".join(lines[block.start_line - 1 : block.end_line])


__all__ = ["Block", "block_text", "build_blocks", "match_lines", "parse_terms"]
