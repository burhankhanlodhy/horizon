"""Query-aware pruning keeps whole sentences verbatim and the structure intact."""

from __future__ import annotations

import re

import numpy as np

from horizon.transforms.query_prune import prune_pieces

QUESTION = "How does the risk check stop orders?"
DOC = """# Trading service

The risk check runs before any order is placed and stops orders over the limit. The weather in the office was pleasant that week. Operators pause trading from a dashboard.

## Notes

- The risk check rejects an order when exposure exceeds 2% of equity.
- Lunch is served at noon on Fridays for everyone in the team.
- Every decision is logged with its order id for later review.
Short line here.
"""


def _embed(texts: list[str]) -> np.ndarray:
    """Bag of words over a tiny vocabulary: risk/order words point one way."""
    vocab = ["risk", "order", "orders", "check", "limit", "exposure", "logged", "lunch", "weather"]
    rows = []
    for t in texts:
        words = re.findall(r"[a-z]+", t.lower())
        v = np.array([words.count(w) for w in vocab], dtype=float) + 0.01
        rows.append(v / np.linalg.norm(v))
    return np.vstack(rows)


def _sentences(text: str) -> set[str]:
    return {s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", text) if s.strip()}


def test_kept_text_is_verbatim_and_structure_survives():
    out, before, after = prune_pieces([(DOC, True)], QUESTION, 0.4, _embed)
    text = "".join(out)
    assert after < before
    assert _sentences(text) <= _sentences(DOC)
    for line in ("# Trading service", "## Notes", "Short line here."):
        assert line in text
    assert "weather" not in text and "Lunch" not in text
    assert "rejects an order when exposure exceeds 2%" in text


def test_non_compressible_pieces_and_no_question_are_untouched():
    table = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    out, before, after = prune_pieces([(DOC, True), (table, False)], QUESTION, 0.4, _embed)
    assert out[1] == table
    out, before, after = prune_pieces([(DOC, True)], "  ", 0.4, _embed)
    assert out == [DOC] and before == after


def test_budget_is_met_but_not_overshot():
    out, before, after = prune_pieces([(DOC, True)], QUESTION, 0.3, _embed)
    assert before * 0.7 <= after < before
