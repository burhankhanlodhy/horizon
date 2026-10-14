from __future__ import annotations

import pytest

from horizon.desktop_mcp.backend import FixtureBackend, FixtureRangeError


def test_backend_has_only_synthetic_fixtures_and_unknown_ids_are_inert() -> None:
    backend = FixtureBackend()
    assert {item.source_id for item in backend.list_fixtures()} == {
        "fixture:note",
        "fixture:log",
        "fixture:json",
    }
    assert backend.get_fixture("C:/Users/example/private.txt") is None
    assert backend.open("C:/Users/example/private.txt") is None


def test_open_repeats_exact_raw_text_and_retrieve_selects_versioned_lines() -> None:
    backend = FixtureBackend()
    first = backend.open("fixture:log")
    second = backend.open("fixture:log")
    assert first is not None and second is not None
    assert first["text"] == second["text"]
    assert first["mode"] == "exact"
    assert first["diagnostic_fixture"] is True
    assert first["token_count"] is None
    selected = backend.open("fixture:log", start_line=2, end_line=2)
    assert selected is not None
    assert selected["version"] == first["version"]
    assert selected["text"] == "2026-01-01T00:00:01Z WARN example event for search\n"
    assert selected["selected_lines"] == {"start": 2, "end": 2}


def test_search_and_retrieve_return_exact_fixture_matches_without_compression() -> None:
    backend = FixtureBackend()
    result = backend.search("example event")
    assert result["hit_count"] == 1
    assert result["hits"][0]["source_id"] == "fixture:log"
    retrieved = backend.retrieve("synthetic")
    assert retrieved["returned_text_is_raw"] is True
    assert retrieved["compression_ratio"] is None
    stats = backend.stats()
    assert stats["compression_applied"] is False
    assert stats["compression_ratio"] is None
    assert stats["token_count_status"] == "unknown; no tokenizer was used"


def test_empty_search_is_a_source_catalog_and_ranges_are_checked_before_slicing() -> None:
    backend = FixtureBackend()
    catalog = backend.search("")
    assert catalog["hit_count"] == 3
    assert {hit["source_id"] for hit in catalog["hits"]} == {
        "fixture:note",
        "fixture:log",
        "fixture:json",
    }
    for start, end in ((0, 1), (2, 1), (1, 99)):
        with pytest.raises(FixtureRangeError):
            backend.open("fixture:log", start_line=start, end_line=end)
