from __future__ import annotations

from horizon.desktop_mcp.compression import (
    COMPACT_ENCODING_VERSION,
    compact_json,
    compact_text,
    compression_decoding_hint,
)
from horizon.transforms.lossless_compaction import (
    expand_runs,
    unfold_repeated_blocks,
)


def test_json_compaction_preserves_lexemes_and_labels_whitespace_normalization() -> None:
    outcome = compact_json('{ "n": 1.2300e+02, "s": " a  b " }\n')
    assert outcome.applied
    assert outcome.text == '{"n":1.2300e+02,"s":" a  b "}'
    assert outcome.fidelity == "normalized_json_whitespace"


def test_json_duplicate_keys_fall_back_exact() -> None:
    source = '{ "a": 1, "a": 2 }'
    outcome = compact_json(source)
    assert not outcome.applied
    assert outcome.text == source
    assert outcome.fidelity == "byte_exact"


def test_compaction_only_claims_reversible_when_inverse_matches() -> None:
    repeated = "alpha " * 24
    source = f"{repeated}\n{repeated}\n{repeated}\n{repeated}\n"
    outcome = compact_text(source, "text")
    assert outcome.applied
    assert outcome.fidelity == "reversible_fold"
    assert expand_runs(outcome.text) == source
    hint = compression_decoding_hint(outcome.transform, outcome.fidelity)
    assert COMPACT_ENCODING_VERSION == "contextshrink-compact-v1"
    assert hint is not None and "N total copies" in hint
    assert "N includes the visible line" in hint


def test_decoder_hints_explain_transform_order_and_normalized_fidelity() -> None:
    config_hint = compression_decoding_hint("config_blocks", "reversible_fold")
    assert config_hint is not None
    assert config_hint.startswith("First expand each `... (repeats K lines")
    assert "block-expanded stream" in config_hint
    assert "Then expand each `... (repeated N times)`" in config_hint

    json_hint = compression_decoding_hint("json_whitespace", "normalized_json_whitespace")
    assert json_hint is not None
    assert "not byte recovery" in json_hint
    assert compression_decoding_hint("unknown", "reversible_fold") is None


def test_config_hint_matches_mixed_block_and_run_marker_inverse_order() -> None:
    block_lines = ["alpha: 1", "beta: x", "beta: x", "beta: x", "gamma: 3"]
    block = "\n".join(block_lines) + "\n"
    source = block * 4
    outcome = compact_text(source, "config")

    assert outcome.applied
    assert outcome.transform == "config_blocks"
    assert "... (repeats " in outcome.text
    assert "... (repeated " in outcome.text
    assert expand_runs(unfold_repeated_blocks(outcome.text)) == source


def test_untrusted_inverse_marker_falls_back_without_expanding() -> None:
    source = "value\n... (repeated 999999999999999999 times)\n"
    outcome = compact_text(source, "text")
    assert not outcome.applied
    assert outcome.text == source
    assert outcome.fidelity == "byte_exact"


def test_ansi_stripping_cannot_hide_an_untrusted_log_marker() -> None:
    source = "\x1b[31mvalue\x1b[0m\n\x1b[31m... (repeated 999999999999999999 times)\x1b[0m\n"
    outcome = compact_text(source, "log")
    assert not outcome.applied
    assert outcome.text == source


def test_diff_metadata_normalization_is_derived_from_original() -> None:
    source = "diff --git a/a b/a\nindex 1234..5678 100644\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\n"
    outcome = compact_text(source, "diff")
    assert outcome.applied
    assert outcome.text == "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\n"
    assert outcome.fidelity == "normalized_diff_metadata"
