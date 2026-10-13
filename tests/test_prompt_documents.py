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


@pytest.fixture(autouse=True)
def _model_ready(monkeypatch):
    monkeypatch.setattr(ContentRouter, "_force_kompress_ready", lambda self: True)


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


def test_trailing_system_messages_leave_the_user_turn_mutable():
    from horizon.proxy.handlers.anthropic import AnthropicHandlerMixin as A

    user, asst, system = {"role": "user"}, {"role": "assistant"}, {"role": "system"}
    assert A._strict_previous_turn_frozen_count([user, system], 0) == 0
    assert A._strict_previous_turn_frozen_count([user, asst, user, system, system], 0) == 2
    # An assistant prefill (or nothing but system) still freezes everything.
    assert A._strict_previous_turn_frozen_count([user, asst], 0) == 2
    assert A._strict_previous_turn_frozen_count([system], 0) == 1


def test_claude_code_shape_compresses_the_paste_and_keeps_the_question(router, monkeypatch):
    """Claude Code sends [..., user(question + paste, cache-marked), system(effort)]."""
    monkeypatch.setenv("HORIZON_PROMPT_DOCUMENTS", "1")
    text = _doc() + "\n\n" + QUESTION
    messages = [
        {"role": "user", "content": [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]},
        {"role": "system", "content": [{"type": "text", "text": "Effort: medium."}]},
    ]
    result = router.apply(messages, _Tokenizer(), prefix_replay_guaranteed=True)
    out = result.messages[0]["content"][0]["text"]
    assert "[doc]" in out and out.endswith(QUESTION)
    assert result.messages[1] == messages[1]


def test_a_later_typed_turn_before_a_trailing_system_message_stays_verbatim(router, monkeypatch):
    monkeypatch.delenv("HORIZON_PROMPT_DOCUMENTS", raising=False)
    typed = "Please now also add retries. " * 120
    messages = [
        {"role": "user", "content": "Build it."},
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": typed},
        {"role": "system", "content": "Effort: medium."},
    ]
    result = router.apply(
        messages, _Tokenizer(), prefix_replay_guaranteed=True, compress_user_messages=True
    )
    assert result.messages[2]["content"] == typed


def test_windows_line_endings_are_paragraph_breaks_too(router, monkeypatch):
    """Pastes on Windows arrive with \r\n; the document must still be found."""
    monkeypatch.setenv("HORIZON_PROMPT_DOCUMENTS", "1")
    text = (QUESTION + "\n\n" + _doc() + "\n\n" + QUESTION).replace("\n", "\r\n")
    span = cr._prompt_document_span(text, cr.PROMPT_DOC_EDGE_CHARS)
    assert span is not None and QUESTION not in text[span[0] : span[1]]
    result = router.apply(
        [{"role": "user", "content": text}], _Tokenizer(), prefix_replay_guaranteed=True
    )
    out = result.messages[0]["content"]
    assert "[doc]" in out and out.endswith(QUESTION)


def test_documents_carry_no_retrieval_marker(monkeypatch):
    """With a marker the model fetched the whole original straight away."""
    monkeypatch.setenv("HORIZON_PROMPT_DOCUMENTS", "1")
    r = ContentRouter()

    def with_marker(self, content, content_key, *a, **k):
        return (
            content[: len(content) // 2]
            + "\n[120 words compressed to 60 (from 9 source lines). Retrieve more: hash=ab12cd34ef56]",
            True,
        )

    monkeypatch.setattr(ContentRouter, "_compress_block_content", with_marker)
    text = QUESTION + "\n\n" + _doc() + "\n\n" + QUESTION
    out = r.apply([{"role": "user", "content": text}], _Tokenizer(), prefix_replay_guaranteed=True)
    body = out.messages[0]["content"]
    assert "Retrieve more" not in body and len(body) < len(text)


def test_markdown_tables_stay_exact_and_specs_count_as_prose():
    table = "| Id | Status | Owner |\n|---|---|---|\n| AC-1 | PASS | ops |\n| AC-2 | FAIL | dev |\n"
    middle = _doc(10) + "\n\n" + table + "\n" + _doc(10)
    pieces = cr._prompt_document_pieces(middle)
    assert "".join(p for p, _ in pieces) == middle
    assert (table, False) in pieces
    assert [ok for _, ok in pieces].count(True) == 2
    # Status words and IDs made the detector call specs build logs; sentences decide.
    spec = "\n".join(
        f"- AC-{i}: The quota service must reject a request when the tenant budget is spent, status FAIL."
        for i in range(40)
    )
    assert cr._looks_like_prose(spec)
    assert not cr._looks_like_prose("\n".join(f"ERROR {i} at 0x{i:04x}" for i in range(40)))


def test_keep_setting_applies_to_documents_only(monkeypatch):
    monkeypatch.setenv("HORIZON_PROMPT_DOCUMENTS", "1")
    monkeypatch.setenv("HORIZON_PROMPT_DOCUMENTS_KEEP", "0.9")
    r = ContentRouter()
    seen: list = []

    def record(self, content, content_key, *a, **k):
        seen.append(getattr(self, "_runtime_target_ratio", None))
        return content[: len(content) // 2], True

    after: list = []
    doc = ContentRouter._compress_prompt_document

    def wrapped(self, *a, **k):
        out = doc(self, *a, **k)
        after.append(getattr(self, "_runtime_target_ratio", None))
        return out

    monkeypatch.setattr(ContentRouter, "_compress_block_content", record)
    monkeypatch.setattr(ContentRouter, "_compress_prompt_document", wrapped)
    text = QUESTION + "\n\n" + _doc() + "\n\n" + QUESTION
    r.apply(
        [{"role": "user", "content": text}], _Tokenizer(), prefix_replay_guaranteed=True, target_ratio=0.5
    )
    assert seen and set(seen) == {0.9}
    # The caller's own ratio is back for everything after the document.
    assert after == [0.5]
    for bad in ("", "abc", "1.5", "0"):
        monkeypatch.setenv("HORIZON_PROMPT_DOCUMENTS_KEEP", bad)
        assert cr._prompt_document_keep() is None


def test_documents_use_kompress_and_stay_verbatim_without_it(router, monkeypatch):
    """Specs read as logs to the detector; the log compressor would drop lines."""
    monkeypatch.setenv("HORIZON_PROMPT_DOCUMENTS", "1")
    forced: list = []
    original = ContentRouter._compress_block_content

    def record(self, *a, **k):
        forced.append(getattr(self, "_runtime_force_kompress", False))
        return original(self, *a, **k)

    monkeypatch.setattr(ContentRouter, "_compress_block_content", record)
    text = QUESTION + "\n\n" + _doc() + "\n\n" + QUESTION
    router.apply([{"role": "user", "content": text}], _Tokenizer(), prefix_replay_guaranteed=True)
    assert forced and all(forced)
    monkeypatch.setattr(ContentRouter, "_force_kompress_ready", lambda self: False)
    result = router.apply([{"role": "user", "content": text}], _Tokenizer(), prefix_replay_guaranteed=True)
    assert result.messages[0]["content"] == text


# ── Query-aware pruning (HORIZON_PROMPT_DOCUMENTS_QUERY) ──────────────────

def _sectioned_doc() -> str:
    topics = {
        "Risk check": "The risk check rejects an order when exposure exceeds the limit. "
        "Exposure is measured against account equity before every order.",
        "Lunch": "Lunch is served at noon on Fridays and the weather is pleasant in spring.",
        "Office": "The office has plants and a kitchen; coffee is free and the chairs are new.",
        "Holidays": "Holidays follow the calendar and the team takes two weeks in summer off.",
    }
    parts = ["# Trading service"]
    for title, body in topics.items():
        parts.append(f"## {title}\n\n" + "\n\n".join(f"{body} Note {i}." for i in range(30)))
    return "\n\n".join(parts)


class _FakeEmbedder:
    def __init__(self, ready=True):
        self.ready = ready
        self.warmed = 0

    def warm(self):
        self.warmed += 1

    def embed(self, texts, deadline=None):
        import numpy as np

        vocab = ["risk", "order", "exposure", "limit", "lunch", "office", "holidays"]
        rows = []
        for t in texts:
            low = t.lower()
            v = np.array([low.count(w) for w in vocab], dtype=float) + 0.01
            rows.append(v / np.linalg.norm(v))
        return np.vstack(rows)


@pytest.fixture
def query_embedder(monkeypatch):
    from horizon.transforms import query_prune

    fake = _FakeEmbedder()
    monkeypatch.setattr(query_prune, "shared_embedder", lambda: fake)
    monkeypatch.setattr(cr, "_PROMPT_DOC_QUERY_CACHE", cr.OrderedDict())
    monkeypatch.setenv("HORIZON_PROMPT_DOCUMENTS", "1")
    monkeypatch.setenv("HORIZON_PROMPT_DOCUMENTS_QUERY", "1")
    return fake


NARROW_Q = "How does the risk check decide when to reject an order over the exposure limit?"


def test_a_narrow_question_keeps_its_section_whole(router, query_embedder):
    text = NARROW_Q + "\n\n" + _sectioned_doc()
    result = router.apply([{"role": "user", "content": text}], _Tokenizer(), prefix_replay_guaranteed=True)
    out = result.messages[0]["content"]
    assert out.startswith(NARROW_Q)
    assert cr.PROMPT_DOC_QUERY_NOTE in out
    assert "The risk check rejects an order when exposure exceeds the limit." in out
    # The last 1500 chars are edge (kept as typed), so one of three eligible sections goes.
    assert out.count("[Section omitted") >= 1 and len(out) < len(text)
    assert "router:prompt_document:query" in result.transforms_applied
    assert router.calls == []  # no Kompress on top


def test_a_whole_document_request_is_not_query_pruned(router, query_embedder):
    text = "Review this document and list every risk.\n\n" + _sectioned_doc()
    result = router.apply([{"role": "user", "content": text}], _Tokenizer(), prefix_replay_guaranteed=True)
    assert "router:prompt_document:query" not in result.transforms_applied
    assert cr.PROMPT_DOC_QUERY_NOTE not in result.messages[0]["content"]


def test_a_document_sent_whole_while_loading_stays_whole(router, query_embedder):
    query_embedder.ready = False
    text = NARROW_Q + "\n\n" + _sectioned_doc()
    first = router.apply([{"role": "user", "content": text}], _Tokenizer(), prefix_replay_guaranteed=True)
    assert query_embedder.warmed == 1
    query_embedder.ready = True
    again = router.apply([{"role": "user", "content": text}], _Tokenizer(), prefix_replay_guaranteed=True)
    assert again.messages[0]["content"] == first.messages[0]["content"]
    assert cr.PROMPT_DOC_QUERY_NOTE not in again.messages[0]["content"]


def test_the_question_can_come_from_its_own_block(router, query_embedder):
    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": NARROW_Q},
            {"type": "text", "text": _sectioned_doc(), "cache_control": {"type": "ephemeral"}},
        ],
    }
    out = router._process_content_blocks(
        msg, msg["content"], "", [], set(), set(),
        route_counts={}, messages_from_end=1, prefix_replay_guaranteed=True,
        protect_prompt_text=True,
    )["content"]
    assert out[0]["text"] == NARROW_Q
    assert out[1]["text"].count("[Section omitted") >= 2
