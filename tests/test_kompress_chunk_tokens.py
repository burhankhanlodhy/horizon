"""Kompress chunks must fit the model's token window, or their tail is lost.

Words past the 512-token truncation got no score and were dropped from the
output: 10-35% of a technical document vanished at any keep ratio.
"""

from __future__ import annotations

from horizon.transforms.kompress_compressor import _token_bounded_chunks


class _Encoding:
    def __init__(self, word_ids):
        self._ids = word_ids

    def word_ids(self):
        return self._ids


class _TwoTokensPerWord:
    """[CLS] w0 w0 w1 w1 ... [SEP], truncated like a fast tokenizer."""

    def __call__(self, words, *, is_split_into_words, truncation, max_length):
        ids = [None] + [i for i in range(len(words)) for _ in range(2)]
        return _Encoding(ids[: max_length - 1] + [None])


def test_chunks_cover_every_word_once_within_the_token_window():
    words = [f"w{i}" for i in range(1000)]
    spans = _token_bounded_chunks(_TwoTokensPerWord(), words, max_words=350, max_tokens=512)
    assert spans[0][0] == 0 and spans[-1][1] == len(words)
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    # 350 words need 702 tokens; each chunk must fit 510 (two specials).
    assert all(2 * (end - start) + 2 <= 512 for start, end in spans)


def test_short_chunks_are_left_at_the_word_limit():
    words = [f"w{i}" for i in range(500)]
    spans = _token_bounded_chunks(_TwoTokensPerWord(), words, max_words=100, max_tokens=512)
    assert spans == [(0, 100), (100, 200), (200, 300), (300, 400), (400, 500)]


def test_a_tokenizer_without_word_alignment_keeps_fixed_word_chunks():
    def plain(words, **_):
        return {"input_ids": [0] * len(words)}

    assert _token_bounded_chunks(plain, ["w"] * 700, max_words=350) == [(0, 350), (350, 700)]
