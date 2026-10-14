from __future__ import annotations

from horizon.desktop_mcp.retrieval import rank_passages


def test_rank_passages_returns_verbatim_source_with_deterministic_references() -> None:
    source = "# Intro\nhello world\n\n# Storage\nThe database uses SQLite for local snapshots.\n\nOther text.\n"
    first = rank_passages(source, "SQLite snapshots", limit=5)
    second = rank_passages(source, "SQLite snapshots", limit=5)
    assert first == second
    assert first[0].text == "# Storage\nThe database uses SQLite for local snapshots.\n"
    assert first[0].heading == "Storage"
    assert first[0].reference() == {"start_line": 4, "end_line": 5, "heading": "Storage"}


def test_no_lexical_match_returns_no_passages() -> None:
    assert rank_passages("alpha beta\n", "unrelated") == []
