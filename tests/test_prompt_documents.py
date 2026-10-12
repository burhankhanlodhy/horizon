"""Documents inside the caller's prompt (HORIZON_PROMPT_DOCUMENTS).

The newest user turn stays verbatim except the prose middle of a large paste:
the typed words at either end, fenced code and harness reminders never change.
"""

from __future__ import annotations

import pytest

from horizon.transforms import content_router as cr
from horizon.transforms.content_router import ContentRouter

QUESTION = "Review this document and generate a plan for me on how to build it."
PARA = (
    "The service ingests market data every minute, stores candles in a local "
    "database and runs a risk check before any order is placed. Operators can "
    "pause trading from a dashboard, and every decision is logged for review."
)


SENTENCES = PARA.split(". ") + [
    "Backtests replay recorded fills so that slippage is modelled honestly",
    "A paper account runs for several weeks before any capital is committed",
    "If the exchange connection drops, open orders are cancelled within seconds",
    "Model weights are versioned, and a rollback restores the previous set",
    "Alerts reach the operator by email and by a push notification",
]


def _doc(paragraphs: int = 60) -> str:
    """Varied prose (identical lines read as a fixed-width table to the detector)."""
    out = []
    for i in range(paragraphs):
        n = 2 + i % 4
        out.append(". ".join(SENTENCES[(i + j * 3) % len(SENTENCES)] for j in range(n)) + f". Note {i}.")
    return "\n\n".join(out)


class _Tokenizer:
    def count_text(self, content: str) -> int:
        return len(content.split())


@pytest.fixture
def router(monkeypatch):
    r = ContentRouter()
    calls: list[str] = []

    def fake_compress_block_content(self, content, content_key, *a, **k):
        calls.append(content)
        return content[: len(content) // 2] + " [doc]", True

    monkeypatch.setattr(ContentRouter, "_compress_block_content", fake_compress_block_content)
    r.calls = calls
    return r


def test_span_keeps_the_typed_words_at_both_ends():
    text = QUESTION + "\n\n" + _doc() + "\n\nThanks!"
    start, end = cr._prompt_document_span(text, cr.PROMPT_DOC_EDGE_CHARS)
    assert start >= cr.PROMPT_DOC_EDGE_CHARS and text[start - 2 : start] == "\n\n"
    assert len(text) - end >= cr.PROMPT_DOC_EDGE_CHARS
    assert QUESTION not in text[start:end] and "Thanks!" not in text[start:end]


def test_short_prompts_reminders_and_code_pastes_are_left_alone():
    assert cr._prompt_document_span(QUESTION, cr.PROMPT_DOC_EDGE_CHARS) is None
    reminded = _doc(30) + "\n\n<system-reminder>\nDo not guess.\n</system-reminder>\n\n" + _doc(30)
    assert cr._prompt_document_span(reminded, cr.PROMPT_DOC_EDGE_CHARS) is None
    code = "\n\n".join("```python\n" + "x = 1\n" * 40 + "```" for _ in range(20))
    assert cr._prompt_document_span(QUESTION + "\n\n" + code + "\n\nok", 100) is None


def test_fenced_code_inside_a_document_is_never_compressible():
    middle = _doc(10) + "\n\n```bash\nrm -rf build && make\n```\n\n" + _doc(10)
    pieces = cr._prompt_document_pieces(middle)
    assert [ok for _, ok in pieces] == [True, False, True]
    assert "".join(p for p, _ in pieces) == middle


def test_flag_off_keeps_the_prompt_verbatim(router, monkeypatch):
    monkeypatch.delenv("HORIZON_PROMPT_DOCUMENTS", raising=False)
    text = QUESTION + "\n\n" + _doc()
    result = router.apply(
        [{"role": "user", "content": text}], _Tokenizer(), prefix_replay_guaranteed=True
    )
    assert result.messages[0]["content"] == text
    assert router.calls == []


def test_string_prompt_compresses_only_the_document(router, monkeypatch):
    monkeypatch.setenv("HORIZON_PROMPT_DOCUMENTS", "1")
    text = QUESTION + "\n\n" + _doc() + "\n\n" + QUESTION
    result = router.apply(
        [{"role": "user", "content": text}], _Tokenizer(), prefix_replay_guaranteed=True
    )
    out = result.messages[0]["content"]
    assert len(out) < len(text) and "[doc]" in out
    assert out.startswith(text[: cr.PROMPT_DOC_EDGE_CHARS])
    assert out.endswith(text[-cr.PROMPT_DOC_EDGE_CHARS :])
    assert "router:prompt_document" in result.transforms_applied


def test_fresh_marked_text_block_and_text_document_block(router, monkeypatch):
    monkeypatch.setenv("HORIZON_PROMPT_DOCUMENTS", "1")
    text = _doc() + "\n\n" + QUESTION
    msg = {
        "role": "user",
        "content": [
            {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": _doc()}},
            {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}},
        ],
    }
    out = router._process_content_blocks(
        msg, msg["content"], "", [], set(), set(),
        route_counts={}, messages_from_end=1, prefix_replay_guaranteed=True,
        protect_prompt_text=True,
    )["content"]
    assert "[doc]" in out[0]["source"]["data"]
    assert out[1]["text"].endswith(QUESTION) and "[doc]" in out[1]["text"]
    # The marker rides on the compressed block, which is what gets cached.
    assert out[1]["cache_control"] == {"type": "ephemeral"}


def test_a_cached_earlier_turn_is_never_touched(router, monkeypatch):
    monkeypatch.setenv("HORIZON_PROMPT_DOCUMENTS", "1")
    block = {"type": "text", "text": QUESTION + "\n\n" + _doc() + "\n\nok", "cache_control": {"type": "ephemeral"}}
    msg = {"role": "user", "content": [block]}
    out = router._process_content_blocks(
        msg, msg["content"], "", [], set(), set(),
        route_counts={}, messages_from_end=3, prefix_replay_guaranteed=True,
        protect_prompt_text=True,
    )
    assert out["content"][0] == block
    assert router.calls == []
