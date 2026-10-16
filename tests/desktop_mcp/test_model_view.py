"""Model-facing projection of presentation results (0.1.6)."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from horizon.desktop_mcp.local_backend import LocalBackend
from horizon.desktop_mcp.model_view import DROPPED, KEPT, model_view
from horizon.desktop_mcp.policies import CursorSigner, present_selected_scope, present_snapshot
from horizon.desktop_mcp.selectors import select_exact
from horizon.desktop_mcp.server import create_server
from horizon.desktop_mcp.settings import DesktopSettings
from horizon.desktop_mcp.snapshots import SnapshotStore
from horizon.desktop_mcp.source_access import SourceReader

ROOT = "approved-root"


class _FakeEstimator:
    """One token per four characters; the provenance keys finalize_accounting reads."""

    def count_text(self, text: str) -> int:
        return len(text) // 4

    def provenance(self) -> dict[str, Any]:
        return {
            "fallback": False,
            "counter_version": "fake",
            "count_status": "fake_len_div_4",
            "tokenizer_name": "fake_len_div_4",
            "counting_model_assumption": None,
        }


def _snapshot(text: str, kind: str = "txt"):
    raw = text.encode("utf-8")
    return SnapshotStore().put(
        root_key=ROOT,
        relative_path=f"fixture.{kind}",
        source=f"C:/approved/fixture.{kind}",
        kind=kind,
        text=text,
        digest=hashlib.sha256(raw).hexdigest(),
        byte_count=len(raw),
        identity="volume:file-id",
    )


def _full_payload() -> dict[str, Any]:
    """A payload that uses every kept field, plus every dropped field."""
    payload: dict[str, Any] = dict.fromkeys(DROPPED, "x")
    payload.update(
        {
            "source_id": "cs1_example",
            "mode": "exact",
            "requested_mode": "compact",
            "coverage": "partial (limit)",
            "fidelity": "normalized_json_whitespace",
            "compression_applied": True,
            "compression_transform": "json_whitespace",
            "compression_encoding_version": "contextshrink-compact-v1",
            "compression_decoding_hint": "hint",
            "line_count": 3,
            "selected_lines": {"start": 1, "end": 3},
            "match_count": 2,
            "headings": [{"line": 1, "heading": "Title"}],
            "omitted_ranges": [{"start_line": 2, "end_line": 2, "reason": "x"}],
            "extraction": {"warnings": ["ocr skipped"], "block_counts": {"page": 1}},
            "text": "body",
            "more_available": True,
            "next_cursor": "cursor-token",
        }
    )
    return payload


def test_projection_keeps_listed_fields_in_order() -> None:
    view = model_view(_full_payload())

    assert list(view) == [
        "source_id",
        "mode",
        "requested_mode",
        "coverage",
        "fidelity",
        "compression_applied",
        "compression_transform",
        "compression_encoding_version",
        "compression_decoding_hint",
        "line_count",
        "selected_lines",
        "match_count",
        "headings",
        "omitted_ranges",
        "warnings",
        "text",
        "more_available",
        "next_cursor",
    ]
    assert view["warnings"] == ["ocr skipped"]


def test_bookkeeping_fields_are_dropped() -> None:
    view = model_view(_full_payload())

    for key in DROPPED:
        assert key not in view
    assert set(KEPT).isdisjoint(DROPPED)


def test_unknown_key_is_dropped_by_default() -> None:
    view = model_view({"source_id": "cs1", "mode": "exact", "text": "t", "future_field": {"a": 1}})

    assert view == {"source_id": "cs1", "mode": "exact", "text": "t"}


def test_conditional_fields_appear_only_when_informative() -> None:
    quiet = {
        "source_id": "cs1",
        "mode": "exact",
        "requested_mode": "exact",
        "fidelity": "byte_exact",
        "compression_applied": False,
        "compression_transform": None,
        "line_count": 2,
        "selected_lines": None,
        "omitted_ranges": [],
        "extraction": {"warnings": []},
        "text": "t",
        "more_available": False,
        "next_cursor": None,
    }

    assert model_view(quiet) == {"source_id": "cs1", "mode": "exact", "line_count": 2, "text": "t"}

    informative = {**quiet, "requested_mode": "compact", "fidelity": "partial_exact_excerpt"}
    informative["more_available"] = True
    informative["next_cursor"] = "c"
    view = model_view(informative)
    assert view["requested_mode"] == "compact"
    assert view["fidelity"] == "partial_exact_excerpt"
    assert view["next_cursor"] == "c"


def test_paging_fields_are_absent_when_no_more_is_available() -> None:
    view = model_view({"source_id": "cs1", "mode": "exact", "text": "t", "next_cursor": "stale"})

    assert "next_cursor" not in view
    assert "more_available" not in view


@pytest.mark.parametrize("value", [None, "text", 3, [1, 2], ["x"]])
def test_non_dict_values_are_returned_unchanged(value: Any) -> None:
    assert model_view(value) is value


def test_error_payload_is_returned_unchanged() -> None:
    error = {"error": {"code": "result_too_large", "message": "m"}}

    assert model_view(error) is error


def test_odd_payload_fields_do_not_raise_and_input_is_not_mutated() -> None:
    odd = {
        "source_id": "cs1",
        "mode": "exact",
        "text": None,
        "extraction": "not-a-dict",
        "more_available": "yes",
        "omitted_ranges": "unexpected",
        "selected_lines": None,
    }
    before = copy.deepcopy(odd)

    view = model_view(odd)

    assert odd == before
    assert view == {
        "source_id": "cs1",
        "mode": "exact",
        "text": None,
        "omitted_ranges": "unexpected",
    }


def _log_text() -> str:
    return "".join(f"unique line {n:04d} with stable detail\n" for n in range(300)) + (
        "status ok\n" * 60
    )


def _presentation_payloads() -> list[dict[str, Any]]:
    """Payloads from every presentation path in present_snapshot and present_selected_scope."""
    estimator = _FakeEstimator()
    signer = CursorSigner()
    long_text = "".join(f"ordinary entry {n:04d} with routine detail\n" for n in range(1500))
    long_text = long_text.replace("ordinary entry 0700", "ZZMARK the one line that matters")
    handbook = "# Title\n\nIntro line.\n\n## Renewal\n\nRenewal ZZFIND here.\n\n## Other\n\nmore\n"
    rows = "id,name\n" + "".join(f"{n},name {n}\n" for n in range(1, 401))
    config = json.dumps({"items": [{"id": n, "note": "x" * 40} for n in range(120)]})
    common: dict[str, Any] = {
        "estimator": estimator,
        "cursor_signer": signer,
        "max_result_bytes": 262_144,
    }

    # A cursor is bound to its snapshot, so continuations reuse one snapshot.
    long_snap = _snapshot(long_text)
    handbook_snap = _snapshot(handbook, "md")
    payloads: list[dict[str, Any]] = []
    payloads.append(
        present_snapshot(
            _snapshot("alpha\nbeta\n"), mode="exact", result_token_budget=6000, **common
        )
    )
    first = present_snapshot(long_snap, mode="exact", result_token_budget=600, **common)
    payloads.append(first)
    payloads.append(
        present_snapshot(
            long_snap,
            mode="exact",
            result_token_budget=600,
            cursor=first["next_cursor"],
            **common,
        )
    )
    payloads.append(
        present_snapshot(
            _snapshot(_log_text(), "log"),
            mode="compact",
            result_token_budget=8000,
            min_compaction_gain_tokens=1,
            min_compaction_gain_percent=0,
            **common,
        )
    )
    payloads.append(
        present_snapshot(
            _snapshot("alpha\nbeta\n", "log"), mode="compact", result_token_budget=6000, **common
        )
    )
    payloads.append(
        present_snapshot(
            long_snap, mode="focused", query="ZZMARK", result_token_budget=6000, **common
        )
    )
    payloads.append(
        present_snapshot(
            long_snap,
            mode="focused",
            query="nothingmatchesthis",
            result_token_budget=6000,
            **common,
        )
    )
    find_page = present_snapshot(
        long_snap,
        mode="find",
        query="routine detail",
        result_token_budget=600,
        context_lines=2,
        **common,
    )
    payloads.append(find_page)
    payloads.append(
        present_snapshot(
            long_snap,
            mode="find",
            query="routine detail",
            result_token_budget=600,
            context_lines=2,
            cursor=find_page["next_cursor"],
            **common,
        )
    )
    payloads.append(
        present_snapshot(handbook_snap, mode="overview", result_token_budget=6000, **common)
    )
    payloads.append(
        present_snapshot(handbook_snap, mode="overview", result_token_budget=128, **common)
    )
    for text, kind, selector in (
        (long_text, "txt", {"type": "lines", "start": 1, "end": 1400}),
        (rows, "csv", {"type": "rows", "start": 1, "end": 401}),
        (config, "json", {"type": "json_pointer", "pointer": "/items"}),
        (handbook, "md", {"type": "markdown_section", "heading": "Renewal"}),
    ):
        selected = select_exact(text, selector, kind=kind)
        payloads.append(
            present_selected_scope(
                _snapshot(text, kind),
                selected,
                result_token_budget=600,
                max_result_bytes=262_144,
                estimator=estimator,
                cursor=None,
                cursor_signer=signer,
            )
        )
    return payloads


def test_every_presentation_key_is_kept_or_explicitly_dropped() -> None:
    payloads = _presentation_payloads()
    modes = {payload.get("mode") for payload in payloads}
    seen: set[str] = set()
    for payload in payloads:
        seen.update(payload)

    assert {"exact", "compact", "focused", "find", "overview"} <= modes
    assert seen - set(KEPT) - DROPPED == set()
    for payload in payloads:
        assert set(model_view(payload)) <= set(KEPT)


@pytest.mark.anyio
async def test_mcp_open_result_has_no_accounting(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_bytes(b"alpha\nbeta\n")
    backend = LocalBackend(SourceReader((tmp_path,), max_source_bytes=1_000_000))
    settings = DesktopSettings(profile="local", tool_profile="source")

    async with create_connected_server_and_client_session(
        create_server(settings, backend)
    ) as client:
        result = await client.call_tool(
            "contextshrink_open", {"source": str(source), "mode": "exact"}
        )

    body = json.loads(result.content[0].text)
    assert result.isError is False
    assert "accounting" not in body
    assert body["text"] == "alpha\nbeta\n"
    assert set(body) <= set(KEPT)


@pytest.mark.anyio
async def test_mcp_paging_and_retrieve_work_through_the_projection(tmp_path: Path) -> None:
    lines = [f"row {n:04d} value steady text\n" for n in range(1, 301)]
    source = tmp_path / "long.txt"
    source.write_bytes("".join(lines).encode("utf-8"))
    backend = LocalBackend(SourceReader((tmp_path,), max_source_bytes=1_000_000))
    settings = DesktopSettings(profile="local", tool_profile="source")

    async with create_connected_server_and_client_session(
        create_server(settings, backend)
    ) as client:
        first = await client.call_tool(
            "contextshrink_open",
            {"source": str(source), "mode": "exact", "result_token_budget": 600},
        )
        body = json.loads(first.content[0].text)
        source_id = body["source_id"]
        pieces = [body["text"]]
        cursor = body.get("next_cursor")
        while cursor is not None:
            page = await client.call_tool(
                "contextshrink_open",
                {
                    "source": source_id,
                    "mode": "exact",
                    "result_token_budget": 600,
                    "cursor": cursor,
                },
            )
            page_body = json.loads(page.content[0].text)
            pieces.append(page_body["text"])
            cursor = page_body.get("next_cursor")
        assert len(pieces) >= 3
        assert "".join(pieces) == "".join(lines)

        retrieved = await client.call_tool(
            "contextshrink_retrieve",
            {"source_id": source_id, "selector": {"type": "lines", "start": 1, "end": 2}},
        )

    assert retrieved.isError is False
    assert json.loads(retrieved.content[0].text)["text"] == "".join(lines[:2])


def test_exact_single_read_shows_no_saving(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("short note\n", encoding="utf-8")
    backend = LocalBackend(SourceReader((tmp_path,), max_source_bytes=1_000_000))

    opened = backend.open(str(source), result_token_budget=2000)

    assert opened is not None
    result = opened["accounting"]["result"]
    assert result["byte_delta"] == 0
    assert result["token_delta_estimate"] == 0


@pytest.mark.anyio
async def test_stats_still_counts_projected_open_calls(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("short note\n", encoding="utf-8")
    backend = LocalBackend(
        SourceReader((tmp_path,), max_source_bytes=1_000_000),
        usage_stats_path=tmp_path / "usage.json",
    )
    settings = DesktopSettings(profile="local", tool_profile="source")

    async with create_connected_server_and_client_session(
        create_server(settings, backend)
    ) as client:
        await client.call_tool("contextshrink_open", {"source": str(source), "mode": "exact"})
        stats = await client.call_tool("contextshrink_stats", {})

    usage = json.loads(stats.content[0].text)["usage"]
    assert usage["by_mode"]["exact"] == 1
    assert usage["event_count"] == 1
