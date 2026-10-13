"""Query-aware pruning of a pasted document (whole sentences, never words).

Kompress drops words evenly and cannot see the question; the result reads as
damaged and saves few tokens. This instead scores each sentence or list item
against the caller's question and the document's own centre of gravity, then
omits the lowest-scoring ones until a word budget is met. What stays is
verbatim, so the document still reads as written.

Headings, short lines, fenced code and tables are never removed.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

# A sentence ends at . ! or ? followed by space and an uppercase letter, digit,
# backtick, bracket or quote. Abbreviations like "e.g. the" do not split.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])[ \t]+(?=[A-Z0-9`(\[\"'*])")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6}\s|[=-]{3,}\s*$)")
_BULLET_RE = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)")
_SIGNAL_RE = re.compile(r"\d|`|\b[A-Z]{2,}\b")
MIN_UNIT_WORDS = 4

Embed = Callable[[list[str]], np.ndarray]  # rows L2-normalised


@dataclass
class _Unit:
    line: int
    text: str
    words: int
    lead: bool  # first sentence after a heading or paragraph start


def _split_line(line: str) -> tuple[str, list[str]]:
    """Return (bullet prefix, sentences) for one line of prose."""
    m = _BULLET_RE.match(line)
    prefix = m.group(1) if m else line[: len(line) - len(line.lstrip())]
    body = line[len(prefix) :]
    return prefix, [s for s in _SENTENCE_END_RE.split(body) if s.strip()]


def prune_pieces(
    pieces: Sequence[tuple[str, bool]],
    question: str,
    remove_fraction: float,
    embed: Embed,
    *,
    relevance_weight: float = 0.6,
) -> tuple[list[str], int, int]:
    """Prune compressible pieces; returns (pieces out, words before, words after).

    ``pieces`` are ``(text, compressible)`` as from ``_prompt_document_pieces``.
    Non-compressible pieces are returned unchanged.
    """
    units: list[_Unit] = []
    layout: list[list[tuple[int, str, list[int]] | str]] = []  # per piece: lines
    after_heading = True
    for text, compressible in pieces:
        lines_out: list[tuple[int, str, list[int]] | str] = []
        if not compressible:
            layout.append([text])
            after_heading = False
            continue
        for line in text.split("\n"):
            words = len(line.split())
            if not line.strip():
                lines_out.append(line)
                after_heading = True
                continue
            if _HEADING_RE.match(line) or words < MIN_UNIT_WORDS:
                lines_out.append(line)
                after_heading = bool(_HEADING_RE.match(line)) or after_heading
                continue
            prefix, sentences = _split_line(line)
            idxs = []
            for j, s in enumerate(sentences):
                idxs.append(len(units))
                units.append(_Unit(len(layout), s, len(s.split()), after_heading and j == 0))
            lines_out.append((len(layout), prefix, idxs))
            after_heading = False
        layout.append(lines_out)

    total = sum(u.words for u in units)
    keep = [True] * len(units)
    if units and remove_fraction > 0 and question.strip():
        vecs = embed([u.text for u in units])
        q = embed([question])[0]
        centre = vecs.mean(axis=0)
        centre = centre / (np.linalg.norm(centre) or 1.0)
        rel, cen = vecs @ q, vecs @ centre

        def z(a: np.ndarray) -> np.ndarray:
            return (a - a.mean()) / (a.std() or 1.0)

        score = relevance_weight * z(rel) + (1 - relevance_weight) * z(cen)
        score += np.array([0.5 if u.lead else 0.0 for u in units])
        score += np.array([0.25 if _SIGNAL_RE.search(u.text) else 0.0 for u in units])
        budget = total * (1 - remove_fraction)
        keep = [False] * len(units)
        kept = 0
        for i in np.argsort(-score):
            if kept >= budget:
                break
            keep[i] = True
            kept += units[i].words

    out: list[str] = []
    for lines_out in layout:
        rendered: list[str] = []
        for item in lines_out:
            if isinstance(item, str):
                rendered.append(item)
                continue
            _, prefix, idxs = item
            kept_text = " ".join(units[i].text.strip() for i in idxs if keep[i])
            if kept_text:
                rendered.append(prefix + kept_text)
        piece = "\n".join(rendered)
        out.append(re.sub(r"\n{3,}", "\n\n", piece))
    after = sum(u.words for u, k in zip(units, keep) if k)
    return out, total, after
