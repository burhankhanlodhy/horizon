"""Honest per-page accounting for exact reads, plus stale-handle error hints (0.1.1)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from horizon.desktop_mcp.broker import BrokerError
from horizon.desktop_mcp.local_backend import LocalBackend
from horizon.desktop_mcp.policies import CursorSigner, present_snapshot
from horizon.desktop_mcp.server import SERVER_VERSION, create_server
from horizon.desktop_mcp.settings import DesktopSettings
from horizon.desktop_mcp.snapshots import SnapshotStore
from horizon.desktop_mcp.source_access import OpenedSource, SourceReader
from horizon.desktop_mcp.version import VERSION

ROOT = "approved-root"
SOURCE = "C:/approved/fixture.txt"
# Structural overhead allowed per page beyond its own text: keys, the accounting
# block, the cursor, and one JSON line-range entry per returned range.
PAGE_OVERHEAD_BYTES = 2000
PAGE_RANGE_BYTES = 80


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


def _long_text(lines: int = 300) -> str:
    return "".join(
        f"row {number:04d} value {(number * 37) % 1000:03d} steady text\n"
        for number in range(1, lines + 1)
    )


def _local_settings() -> DesktopSettings:
    return DesktopSettings(profile="local", tool_profile="source")


def _exact_pages(text: str, budget: int = 600) -> list[dict[str, Any]]:
    """Read a snapshot exactly page by page, following every cursor."""
    snapshot = _snapshot(text)
    signer = CursorSigner()
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = present_snapshot(
            snapshot,
            mode="exact",
            estimator=_FakeEstimator(),  # type: ignore[arg-type]
            result_token_budget=budget,
            max_result_bytes=262_144,
            cursor=cursor,
            cursor_signer=signer,
        )
        pages.append(page)
        cursor = page.get("next_cursor")
        if cursor is None:
            return pages


def _assert_exact_page_is_honest(page: dict[str, Any]) -> None:
    accounting = page["accounting"]
    result = accounting["result"]
    source = accounting["source"]
    # Raw text is the page's own text, so source-level reduction is zero.
    assert source["raw_utf8_bytes"] == len(page["text"].encode("utf-8"))
    assert source["rendered_utf8_bytes"] == source["raw_utf8_bytes"]
    assert source["token_delta_estimate"] == 0
    # Delivered is the same content plus metadata, so nothing is "avoided".
    assert result["delivered_utf8_bytes"] >= result["raw_equivalent_utf8_bytes"]
    assert result["delivered_token_estimate"] >= result["raw_equivalent_token_estimate"]
    assert result["token_delta_estimate"] <= 0
    assert result["byte_delta"] <= 0
    # The raw-equivalent carries this page's text, not the rest of the file.
    ranges = len(page.get("line_ranges", []))
    assert result["raw_equivalent_utf8_bytes"] <= (
        len(page["text"].encode("utf-8")) + PAGE_OVERHEAD_BYTES + PAGE_RANGE_BYTES * ranges
    )


def test_exact_open_of_multi_page_file_reports_each_page_once() -> None:
    text = _long_text()
    file_bytes = len(text.encode("utf-8"))
    pages = _exact_pages(text)

    assert len(pages) >= 3
    assert "".join(page["text"] for page in pages) == text
    for page in pages:
        _assert_exact_page_is_honest(page)

    # Each page's raw-equivalent is its own content, so the sources sum to the
    # file once, never N times the file (the old behaviour).
    source_raw = sum(page["accounting"]["source"]["raw_utf8_bytes"] for page in pages)
    assert source_raw == file_bytes


def test_exact_single_page_file_is_not_counted_against_remaining_scope() -> None:
    text = "short note\n"
    [page] = _exact_pages(text, budget=2000)

    assert page["more_available"] is False
    _assert_exact_page_is_honest(page)


def test_exact_selected_line_page_compares_only_its_own_lines() -> None:
    text = _long_text(150)
    snapshot = _snapshot(text)
    page = present_snapshot(
        snapshot,
        mode="exact",
        estimator=_FakeEstimator(),  # type: ignore[arg-type]
        result_token_budget=6000,
        max_result_bytes=262_144,
        start_line=1,
        end_line=89,
    )

    assert page["selected_lines"] == {"start": 1, "end": 89}
    assert page["text"] == "".join(text.splitlines(keepends=True)[:89])
    _assert_exact_page_is_honest(page)


def test_compact_fallback_to_exact_pages_reports_no_savings() -> None:
    # A short log that does not clear the gate falls back to exact pages.
    text = "alpha\nbeta\ngamma\n"
    page = present_snapshot(
        _snapshot(text, "log"),
        mode="compact",
        estimator=_FakeEstimator(),  # type: ignore[arg-type]
        result_token_budget=6000,
        max_result_bytes=262_144,
    )

    assert page["compression_applied"] is False
    assert page["mode"] == "exact"
    _assert_exact_page_is_honest(page)


def test_compact_that_applies_a_fold_still_reports_positive_savings() -> None:
    text = "".join(f"unique line {number:04d} with stable detail\n" for number in range(300))
    text += "status ok\n" * 60
    result = present_snapshot(
        _snapshot(text, "log"),
        mode="compact",
        estimator=_FakeEstimator(),  # type: ignore[arg-type]
        result_token_budget=8000,
        max_result_bytes=262_144,
        min_compaction_gain_tokens=1,
        min_compaction_gain_percent=0,
    )

    assert result["compression_applied"] is True
    accounting = result["accounting"]
    assert accounting["result"]["token_delta_estimate"] > 0
    assert accounting["source"]["raw_utf8_bytes"] == len(text.encode("utf-8"))


def test_focused_still_compares_against_the_full_document() -> None:
    lines = [f"ordinary entry {number:04d} with routine detail" for number in range(1500)]
    lines[700] = "ZZFOCUSMARK the one line that matters"
    text = "\n".join(lines) + "\n"
    result = present_snapshot(
        _snapshot(text),
        mode="focused",
        query="ZZFOCUSMARK",
        estimator=_FakeEstimator(),  # type: ignore[arg-type]
        result_token_budget=6000,
        max_result_bytes=262_144,
    )

    assert result["mode"] == "focused"
    accounting = result["accounting"]
    assert accounting["source"]["raw_utf8_bytes"] == len(text.encode("utf-8"))
    assert (
        accounting["result"]["raw_equivalent_token_estimate"]
        > (accounting["result"]["delivered_token_estimate"])
    )
    assert accounting["result"]["token_delta_estimate"] > 0


@dataclass
class _Record:
    source: str
    text: str

    def opened(self) -> OpenedSource:
        return OpenedSource(
            root_key=ROOT,
            relative_path=self.source.rsplit("/", 1)[-1],
            text=self.text,
            digest=hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
            byte_count=len(self.text.encode("utf-8")),
            identity="volume:file-id",
        )


class _Reader:
    catalog_truncated = False

    def __init__(self, record: _Record) -> None:
        self.record = record

    def read(self, source: str) -> OpenedSource:
        return self.record.opened()

    def check_access(self, source: str) -> str:
        return ROOT


def test_retrieve_line_pages_share_one_raw_cost() -> None:
    text = _long_text(300)
    backend = LocalBackend(_Reader(_Record(SOURCE, text)))  # type: ignore[arg-type]
    opened = backend.open(SOURCE)
    assert opened is not None
    source_id = opened["source_id"]

    selector = {"type": "lines", "start": 1, "end": 300}
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = backend.retrieve(source_id, selector, result_token_budget=600, cursor=cursor)
        pages.append(page)
        cursor = page.get("next_cursor")
        if cursor is None:
            break

    assert len(pages) >= 3
    assert "".join(page["text"] for page in pages) == text
    for page in pages:
        _assert_exact_page_is_honest(page)
    file_bytes = len(text.encode("utf-8"))
    source_raw = sum(page["accounting"]["source"]["raw_utf8_bytes"] for page in pages)
    assert source_raw == file_bytes


def test_retrieve_rows_pages_share_one_raw_cost() -> None:
    rows = "".join(f"{number},name {number},{number * 3}\n" for number in range(1, 301))
    csv_source = "C:/approved/people.csv"
    backend = LocalBackend(_Reader(_Record(csv_source, "id,name,score\n" + rows)))  # type: ignore[arg-type]
    opened = backend.open(csv_source)
    assert opened is not None
    source_id = opened["source_id"]

    selector = {"type": "rows", "start": 1, "end": 301}
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = backend.retrieve(source_id, selector, result_token_budget=600, cursor=cursor)
        pages.append(page)
        cursor = page.get("next_cursor")
        if cursor is None:
            break

    assert len(pages) >= 2
    for page in pages:
        _assert_exact_page_is_honest(page)
    source_raw = sum(page["accounting"]["source"]["raw_utf8_bytes"] for page in pages)
    assert source_raw == len(("id,name,score\n" + rows).encode("utf-8"))


def test_exact_only_session_has_no_estimated_tokens_avoided(tmp_path: Path) -> None:
    text = _long_text(300)
    source = tmp_path / "notes.txt"
    source.write_text(text, encoding="utf-8")
    reader = SourceReader((tmp_path,), max_source_bytes=1_000_000)
    usage = tmp_path / "usage.json"
    backend = LocalBackend(reader, usage_stats_path=usage)

    opened = backend.open(str(source), result_token_budget=600)
    assert opened is not None
    cursor = opened.get("next_cursor")
    while cursor is not None:
        opened = backend.open(opened["source_id"], result_token_budget=600, cursor=cursor)
        assert opened is not None
        cursor = opened.get("next_cursor")
    backend.open(str(source))  # a full exact read after the paged one

    token_estimates = backend.stats()["cumulative"]["token_estimates"]
    assert token_estimates
    for entry in token_estimates.values():
        assert entry["estimated_tokens_avoided"] <= 0
    assert json.loads(usage.read_text(encoding="utf-8"))["token_estimates"]


class _ErrorBackend:
    """Local-profile backend whose every operation raises the given broker error."""

    def __init__(self, code: str) -> None:
        self.code = code

    def _raise(self, *args: Any, **kwargs: Any) -> Any:
        raise BrokerError(self.code)

    search = open = retrieve = stats = _raise


@pytest.mark.anyio
@pytest.mark.parametrize(
    "code",
    ["snapshot_unknown", "snapshot_expired", "cursor_invalid", "cursor_mismatch"],
)
async def test_stale_handle_codes_return_actionable_hint(code: str) -> None:
    async with create_connected_server_and_client_session(
        create_server(_local_settings(), _ErrorBackend(code))
    ) as client:
        result = await client.call_tool("contextshrink_open", {"source": "C:/approved/a.txt"})

    assert result.isError is True
    error = json.loads(result.content[0].text)["error"]
    assert error["code"] == code
    assert error["message"] == (
        "This source handle or cursor is no longer available (it may have expired, "
        "or the local service restarted). Search again or open the file by its path "
        "to get a fresh handle."
    )


@pytest.mark.anyio
async def test_other_broker_codes_keep_the_generic_message() -> None:
    async with create_connected_server_and_client_session(
        create_server(_local_settings(), _ErrorBackend("source_unavailable"))
    ) as client:
        result = await client.call_tool("contextshrink_open", {"source": "C:/approved/a.txt"})

    error = json.loads(result.content[0].text)["error"]
    assert error["code"] == "source_unavailable"
    assert error["message"] == "The broker could not complete the source operation."


def test_server_version_comes_from_the_version_module() -> None:
    assert VERSION == "0.1.2"
    assert SERVER_VERSION == VERSION
    # The MCP initialize response reports the server's version through its
    # initialization options.
    server = create_server(_local_settings(), _ErrorBackend("source_unavailable"))
    assert server.create_initialization_options().server_version == VERSION
