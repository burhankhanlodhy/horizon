"""Deterministic, extractive retrieval over one immutable text snapshot."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .text_lines import split_physical_lines

_WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+[ \t]*)?$")
_FENCE_RE = re.compile(r"^ {0,3}(?P<mark>`{3,}|~{3,})(?P<tail>.*)$")


@dataclass(frozen=True)
class Passage:
    start_line: int
    end_line: int
    heading: str | None
    text: str
    score: float

    def reference(self) -> dict[str, int | str]:
        ref: dict[str, int | str] = {"start_line": self.start_line, "end_line": self.end_line}
        if self.heading:
            ref["heading"] = self.heading
        return ref


def _tokens(text: str) -> list[str]:
    return [match.group(0).casefold() for match in _WORD_RE.finditer(text)]


def _paragraphs(text: str) -> list[tuple[int, int, str, str | None]]:
    """Split into exact paragraph spans, carrying the nearest Markdown heading."""
    lines = split_physical_lines(text)
    if not lines and text:
        lines = [text]
    spans: list[tuple[int, int, str, str | None]] = []
    heading: str | None = None
    fence_char: str | None = None
    fence_length = 0
    start: int | None = None
    body: list[str] = []
    for number, line in enumerate(lines, 1):
        bare = line.rstrip("\r\n")
        fence = _FENCE_RE.match(bare)
        if fence:
            marker = fence.group("mark")
            if fence_char is None:
                if marker.startswith("`") and "`" in fence.group("tail"):
                    if line.strip():
                        if start is None:
                            start = number
                        body.append(line)
                    continue
                fence_char, fence_length = marker[0], len(marker)
            elif (
                marker[0] == fence_char
                and len(marker) >= fence_length
                and not fence.group("tail").strip(" \t")
            ):
                fence_char, fence_length = None, 0
            if line.strip():
                if start is None:
                    start = number
                body.append(line)
            continue
        match = _HEADING_RE.match(bare) if fence_char is None else None
        if match:
            if start is not None:
                spans.append((start, number - 1, "".join(body), heading))
                start, body = None, []
            heading = match.group(1)
        if line.rstrip("\r\n").strip(" \t"):
            if start is None:
                start = number
            body.append(line)
        elif start is not None:
            spans.append((start, number - 1, "".join(body), heading))
            start, body = None, []
    if start is not None:
        spans.append((start, len(lines), "".join(body), heading))
    return spans


def rank_passages(text: str, query: str, *, limit: int = 20) -> list[Passage]:
    """Rank source paragraphs by deterministic BM25-style lexical relevance.

    Returned passage text is copied verbatim from the snapshot.  This function
    neither summarizes nor interprets the text; callers must label omissions as
    partial coverage and may expose the original line references for recovery.
    """
    terms = _tokens(query)
    if not terms or not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return []
    spans = _paragraphs(text)
    if not spans:
        return []

    token_rows = [_tokens(body) for _, _, body, _ in spans]
    document_frequency: dict[str, int] = {}
    for term in set(terms):
        document_frequency[term] = sum(term in row for row in token_rows)
    average_length = sum(map(len, token_rows)) / max(1, len(token_rows))
    scores: list[Passage] = []
    for (start, end, body, heading), row in zip(spans, token_rows):
        frequencies: dict[str, int] = {}
        for token in row:
            frequencies[token] = frequencies.get(token, 0) + 1
        length = len(row)
        score = 0.0
        for term in set(terms):
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            df = document_frequency[term]
            inverse = math.log(1.0 + (len(spans) - df + 0.5) / (df + 0.5))
            denominator = frequency + 1.2 * (1.0 - 0.75 + 0.75 * length / max(1, average_length))
            score += inverse * frequency * 2.2 / denominator
        if heading:
            heading_tokens = _tokens(heading)
            score += 0.2 * sum(1 for term in set(terms) if term in heading_tokens)
        if score > 0:
            scores.append(Passage(start, end, heading, body, score))

    scores.sort(key=lambda item: (-item.score, item.start_line, item.end_line))
    return scores[:limit]


__all__ = ["Passage", "rank_passages"]
