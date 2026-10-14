from __future__ import annotations

import hashlib

import pytest

from horizon.desktop_mcp.answer_contract import INSTRUCTION_PACK_VERSIONS, instruction_pack


def test_v1_pack_shape_length_and_digest() -> None:
    pack = instruction_pack("v1")
    assert set(pack) == {"version", "text", "sha256", "characters"}
    assert pack["version"] == "v1"
    assert isinstance(pack["text"], str) and pack["text"]
    assert pack["characters"] == len(pack["text"]) <= 600
    assert pack["sha256"] == hashlib.sha256(pack["text"].encode("utf-8")).hexdigest()


def test_default_version_is_v1_and_digest_is_stable() -> None:
    assert instruction_pack() == instruction_pack("v1")
    assert instruction_pack()["sha256"] == instruction_pack("v1")["sha256"]
    assert INSTRUCTION_PACK_VERSIONS == ("v1",)


def test_v1_requires_completeness_and_citation_language() -> None:
    text = instruction_pack("v1")["text"].lower()
    assert "complete" in text
    assert "citation" in text
    assert "direct answer" in text


@pytest.mark.parametrize(
    "phrase",
    ["skip", "don't think", "do not think", "omit the", "shorten", "truncate"],
)
def test_v1_never_tells_the_model_to_skip_reasoning_or_truncate(phrase: str) -> None:
    assert phrase not in instruction_pack("v1")["text"].lower()


@pytest.mark.parametrize("version", ["v2", "", "V1", None, 1])
def test_unknown_version_is_rejected(version) -> None:
    with pytest.raises(ValueError, match="instruction_pack_unknown"):
        instruction_pack(version)
