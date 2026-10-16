"""Find mode: term parsing, literal matching, context blocks, paging and wiring."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from horizon.desktop_mcp.broker_transport import BrokerTransportError, _validate_operation_args
from horizon.desktop_mcp.contracts import ToolInputError, tool_specs, validate_tool_arguments
from horizon.desktop_mcp.find import build_blocks, match_lines, parse_terms
from horizon.desktop_mcp.local_backend import LocalBackend, LocalBackendError
from horizon.desktop_mcp.policies import PolicyError, present_snapshot
from horizon.desktop_mcp.server import create_server
from horizon.desktop_mcp.settings import DesktopSettings
from horizon.desktop_mcp.snapshots import SourceSnapshot
from horizon.desktop_mcp.source_access import OpenedSource, SourceAccessError
from horizon.desktop_mcp.text_lines import split_physical_lines

QUERY = "queue-relay RESTART"


@dataclass
class _Record:
    source: str
    text: str
    root_key: str = "approved-root"
    identity: str = "volume:file-id"
    digest: str = "digest-v1"

    def opened(self) -> OpenedSource:
        return OpenedSource(
            root_key=self.root_key,
            relative_path="log.txt",
            text=self.text,
            digest=self.digest,
            byte_count=len(self.text.encode("utf-8")),
            identity=self.identity,
        )


class _Reader:
    def __init__(self, records: list[_Record]) -> None:
        self.records = {record.source: record for record in records}
        self.catalog_truncated = False
        self.denied: set[str] = set()

    def catalog(self, query: str = "", limit: int = 5) -> list[dict[str, str]]:
        values = list(self.records.values())
        return [
            {"source": item.source, "name": item.source.rsplit("/", 1)[-1], "kind": "txt"}
            for item in values[:limit]
        ]

    def read(self, source: str) -> OpenedSource:
        try:
            return self.records[source].opened()
        except KeyError:
            raise SourceAccessError("source_unapproved") from None

    def check_access(self, source: str) -> str:
        if source not in self.records:
            raise SourceAccessError("source_unavailable")
        return self.records[source].root_key


def _backend(*records: _Record, eval_arm: str = "optimized", budget: int = 512) -> LocalBackend:
    reader = _Reader(list(records))
    return LocalBackend(
        reader, result_token_budget=budget, max_result_bytes=4096, eval_arm=eval_arm
    )


def _log(match_numbers: set[int], total: int = 400, filler: int = 40) -> str:
    lines = []
    for number in range(1, total + 1):
        tail = " queue-relay RESTART" if number in match_numbers else ""
        lines.append(f"event {number:04d} " + "x" * filler + tail + "\n")
    return "".join(lines)


def _snapshot(text: str) -> SourceSnapshot:
    backend = _backend(_Record("C:/approved/find.txt", text))
    snapshot = backend._import_source("C:/approved/find.txt")
    return snapshot


def _matched_lines(page: dict) -> list[int]:
    return [number for match in page["matches"] for number in match["match_lines"]]


# --- Term parsing and matching ------------------------------------------------


def test_parse_terms_splits_on_whitespace_and_keeps_quoted_phrases_whole() -> None:
    assert parse_terms("RESTART  queue-relay") == ["RESTART", "queue-relay"]
    assert parse_terms('"queue relay" RESTART') == ["queue relay", "RESTART"]
    assert parse_terms('a "b c" d') == ["a", "b c", "d"]


def test_parse_terms_drops_blank_input_and_blank_phrases() -> None:
    assert parse_terms("") == []
    assert parse_terms("   ") == []
    assert parse_terms('""') == []


def test_match_lines_requires_every_term_case_insensitively() -> None:
    text = "Queue-Relay restart\nqueue-relay only\nrestart only\nQUEUE-RELAY RESTART done\n"
    assert match_lines(text, ["queue-relay", "restart"]) == [1, 4]
    assert match_lines(text, parse_terms('"queue-relay restart"')) == [1, 4]
    assert match_lines(text, parse_terms('"relay restart"')) == [1, 4]


def test_match_lines_uses_physical_lines_for_crlf_and_is_regex_free() -> None:
    text = "alpha beta\r\ngamma\rdelta (x)\n"
    assert match_lines(text, ["ALPHA"]) == [1]
    assert match_lines(text, ["gamma"]) == [2]
    assert match_lines(text, ["(x)"]) == [3]
    assert match_lines(text, ["x.*"]) == []


def test_match_lines_with_no_terms_matches_nothing() -> None:
    assert match_lines("anything\n", []) == []


# --- Context blocks -----------------------------------------------------------


def _lines(count: int) -> list[str]:
    return [f"line {number}\n" for number in range(1, count + 1)]


def test_context_merges_overlapping_ranges_into_one_block() -> None:
    blocks = build_blocks(_lines(10), [3, 5], context_lines=1)
    assert [(b.start_line, b.end_line, b.match_lines) for b in blocks] == [(2, 6, (3, 5))]


def test_context_merges_touching_ranges_and_keeps_distant_ones_apart() -> None:
    touching = build_blocks(_lines(10), [3, 4], context_lines=0)
    assert [(b.start_line, b.end_line) for b in touching] == [(3, 4)]
    apart = build_blocks(_lines(10), [1, 10], context_lines=2)
    assert [(b.start_line, b.end_line) for b in apart] == [(1, 3), (8, 10)]


def test_context_is_clipped_at_document_edges() -> None:
    blocks = build_blocks(_lines(4), [1, 4], context_lines=50)
    assert [(b.start_line, b.end_line, b.match_lines) for b in blocks] == [(1, 4, (1, 4))]


# --- Policy: result shape, paging, cursors ------------------------------------


def test_zero_matches_is_a_normal_result_with_an_exact_count() -> None:
    backend = _backend(_Record("C:/approved/log.txt", _log(set(), total=20)))
    page = backend.open("C:/approved/log.txt", mode="find", query=QUERY)
    assert page is not None
    assert page["mode"] == "find"
    assert page["match_count"] == 0
    assert page["match_count_complete"] is True
    assert page["matches"] == []
    assert page["more_available"] is False
    assert page["next_cursor"] is None


def test_find_result_reports_exact_matching_blocks_and_partial_coverage() -> None:
    backend = _backend(_Record("C:/approved/log.txt", _log({7}, total=20)))
    page = backend.open("C:/approved/log.txt", mode="find", query=QUERY, context_lines=1)
    assert page is not None
    assert page["fidelity"] == "partial_exact_excerpt"
    assert page["coverage"].startswith("partial (matching lines with context")
    assert page["match_count"] == 1
    assert page["matches"][0]["start_line"] == 6
    assert page["matches"][0]["end_line"] == 8
    assert page["matches"][0]["match_lines"] == [7]
    assert page["text"].startswith("[[lines 6-8]]\n")
    assert page["context_lines"] == 1


def test_match_count_stays_complete_while_blocks_page() -> None:
    matches = set(range(5, 400, 3))
    backend = _backend(_Record("C:/approved/log.txt", _log(matches)))
    first = backend.open("C:/approved/log.txt", mode="find", query=QUERY)
    assert first is not None
    assert first["match_count"] == len(matches)
    assert first["more_available"] is True
    assert len(_matched_lines(first)) < len(matches)

    seen = _matched_lines(first)
    cursor = first["next_cursor"]
    while cursor is not None:
        page = backend.open(first["source_id"], mode="find", query=QUERY, cursor=cursor)
        assert page is not None
        assert page["match_count"] == len(matches)
        seen.extend(_matched_lines(page))
        cursor = page["next_cursor"]
    assert seen == sorted(matches)


def test_cursor_is_rejected_for_another_query_or_context_width() -> None:
    # Matches every fifth line keep a line between context blocks, so pages exist.
    backend = _backend(_Record("C:/approved/log.txt", _log(set(range(5, 400, 5)))))
    first = backend.open("C:/approved/log.txt", mode="find", query=QUERY, context_lines=1)
    assert first is not None
    cursor = first["next_cursor"]
    with pytest.raises(LocalBackendError) as other_query:
        backend.open(
            first["source_id"], mode="find", query="RESTART", context_lines=1, cursor=cursor
        )
    assert other_query.value.code == "cursor_mismatch"
    with pytest.raises(LocalBackendError) as other_context:
        backend.open(first["source_id"], mode="find", query=QUERY, context_lines=2, cursor=cursor)
    assert other_context.value.code == "cursor_mismatch"


def test_find_cursor_is_rejected_by_focused_mode() -> None:
    backend = _backend(_Record("C:/approved/log.txt", _log(set(range(5, 400, 3)))))
    first = backend.open("C:/approved/log.txt", mode="find", query=QUERY)
    assert first is not None
    with pytest.raises(LocalBackendError) as caught:
        backend.open(first["source_id"], mode="focused", query=QUERY, cursor=first["next_cursor"])
    assert caught.value.code == "cursor_mismatch"


def test_blank_find_query_is_query_required() -> None:
    backend = _backend(_Record("C:/approved/log.txt", _log({3})))
    for query in (None, "", "   ", '""'):
        with pytest.raises(LocalBackendError) as caught:
            backend.open("C:/approved/log.txt", mode="find", query=query)
        assert caught.value.code == "query_required"


def test_context_lines_without_find_is_rejected_at_every_layer() -> None:
    backend = _backend(_Record("C:/approved/log.txt", _log({3})))
    with pytest.raises(LocalBackendError) as caught:
        backend.open("C:/approved/log.txt", mode="focused", query="event", context_lines=2)
    assert caught.value.code == "invalid_arguments"
    with pytest.raises(LocalBackendError) as exact:
        backend.open("C:/approved/log.txt", context_lines=0)
    assert exact.value.code == "invalid_arguments"
    with pytest.raises(ToolInputError):
        validate_tool_arguments(
            "contextshrink_open", {"source": "C:/a.txt", "context_lines": 1}, "source"
        )
    with pytest.raises(BrokerTransportError):
        _validate_operation_args(
            "open",
            {
                "source": "C:/a.txt",
                "start_line": None,
                "end_line": None,
                "mode": "exact",
                "context_lines": 1,
            },
        )


@pytest.mark.parametrize("bad", [-1, 51, True, "2"])
def test_context_lines_out_of_range_or_wrong_type_is_rejected(bad: object) -> None:
    with pytest.raises(ToolInputError):
        validate_tool_arguments(
            "contextshrink_open",
            {"source": "C:/a.txt", "mode": "find", "query": "x", "context_lines": bad},
            "source",
        )


def test_context_lines_accepts_the_documented_range() -> None:
    for value in (0, 50):
        args = validate_tool_arguments(
            "contextshrink_open",
            {"source": "C:/a.txt", "mode": "find", "query": "x", "context_lines": value},
            "source",
        )
        assert args["context_lines"] == value


def test_oversized_block_is_listed_by_range_and_later_matches_still_return() -> None:
    lines = [
        "queue-relay RESTART first\n",
        "plain filler\n",
        "queue-relay RESTART " + ("y" * 12000) + "\n",
        "plain filler\n",
        "queue-relay RESTART fifth\n",
        "queue-relay RESTART sixth\n",
    ]
    backend = _backend(_Record("C:/approved/big.txt", "".join(lines)))
    page = backend.open("C:/approved/big.txt", mode="find", query=QUERY)
    assert page is not None
    assert page["match_count"] == 4
    assert _matched_lines(page) == [1]
    assert page["omitted_ranges"] == []
    assert page["more_available"] is True

    rest = backend.open(page["source_id"], mode="find", query=QUERY, cursor=page["next_cursor"])
    assert rest is not None
    assert rest["omitted_ranges"] == [
        {"start_line": 3, "end_line": 3, "reason": "passage_exceeds_result_budget"}
    ]
    assert _matched_lines(rest) == [5, 6]
    assert rest["more_available"] is False
    assert rest["next_cursor"] is None


def test_raw_eval_arm_serves_find_as_exact() -> None:
    backend = _backend(_Record("C:/approved/log.txt", _log({3})), eval_arm="raw")
    page = backend.open("C:/approved/log.txt", mode="find", query=QUERY)
    assert page is not None
    assert page["mode"] == "exact"
    assert page["eval_arm"] == "raw"
    assert "matches" not in page


def test_stats_and_supported_modes_list_find() -> None:
    backend = _backend(_Record("C:/approved/log.txt", _log({3})))
    assert "find" in backend.stats()["supported_modes"]
    backend.open("C:/approved/log.txt", mode="find", query=QUERY)
    assert backend.stats()["usage"]["by_mode"] == {"find": 1}


def test_present_snapshot_rejects_find_without_terms() -> None:
    snapshot = _snapshot(_log({3}, total=10))
    with pytest.raises(PolicyError) as caught:
        present_snapshot(snapshot, mode="find", query="  ")
    assert caught.value.code == "query_required"


def test_split_helper_agrees_with_block_text_for_last_line_without_newline() -> None:
    text = "a RESTART\nb"
    assert split_physical_lines(text) == ["a RESTART\n", "b"]
    snapshot = _snapshot(text)
    page = present_snapshot(snapshot, mode="find", query="restart")
    assert page["matches"][0] == {"start_line": 1, "end_line": 1, "match_lines": [1]}
    assert page["text"] == "[[lines 1-1]]\na RESTART\n"


# --- Tool descriptions and schema ---------------------------------------------


def _spec(name: str) -> dict:
    return next(spec for spec in tool_specs("source") if spec["name"] == name)


def test_open_schema_exposes_find_and_context_lines() -> None:
    schema = _spec("contextshrink_open")["inputSchema"]
    assert schema["properties"]["mode"]["enum"] == [
        "exact",
        "compact",
        "focused",
        "overview",
        "find",
    ]
    assert schema["properties"]["context_lines"]["minimum"] == 0
    assert schema["properties"]["context_lines"]["maximum"] == 50


def test_open_description_leads_with_find_guidance() -> None:
    description = _spec("contextshrink_open")["description"]
    assert description.index("find: with a query") < description.index("compact:")
    assert "set context_lines to see nearby lines" in description


def test_search_description_points_content_lookup_to_find() -> None:
    assert _spec("contextshrink_search")["description"] == (
        "Find approved source files by name. It does not search inside files; use "
        "contextshrink_open with mode find for that."
    )


# --- End to end through the in-process server ---------------------------------


@pytest.mark.anyio
async def test_find_through_the_in_process_server_returns_the_count_and_blocks() -> None:
    backend = _backend(_Record("C:/approved/log.txt", _log({9, 10, 300}, total=320)), budget=2000)
    async with create_connected_server_and_client_session(
        create_server(DesktopSettings(profile="local"), backend)
    ) as client:
        response = await client.call_tool(
            "contextshrink_open",
            {
                "source": "C:/approved/log.txt",
                "mode": "find",
                "query": QUERY,
                "context_lines": 1,
            },
        )
        assert response.isError is False
        payload = json.loads(response.content[0].text)
        assert payload["mode"] == "find"
        assert payload["match_count"] == 3
        # The model view drops the matches list; the block headers in text carry the lines.
        first_start, first_end = (
            int(value)
            for value in re.search(r"\[\[lines (\d+)-(\d+)\]\]", payload["text"]).groups()
        )
        assert first_start <= 9 and first_end >= 10

        empty = await client.call_tool(
            "contextshrink_open",
            {"source": "C:/approved/log.txt", "mode": "find", "query": "absent-term"},
        )
        assert empty.isError is False
        assert json.loads(empty.content[0].text)["match_count"] == 0

        invalid = await client.call_tool(
            "contextshrink_open",
            {"source": "C:/approved/log.txt", "mode": "exact", "context_lines": 2},
        )
        assert "invalid_arguments" in invalid.content[0].text
