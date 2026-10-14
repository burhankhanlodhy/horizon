"""Cumulative usage-stats file: persistence, recovery, limits and wiring."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from horizon.desktop_mcp import __main__ as cli
from horizon.desktop_mcp import backend_worker, extension
from horizon.desktop_mcp import local_backend as local_backend_module
from horizon.desktop_mcp.local_backend import LocalBackend
from horizon.desktop_mcp.measurement import CumulativeUsageStats, LocalUsageLedger
from horizon.desktop_mcp.settings import ConfigurationError, DesktopSettings, load_settings
from horizon.desktop_mcp.source_access import SourceReader

CLOCK_START = dt.datetime(2026, 10, 14, 9, 30, tzinfo=dt.timezone.utc).timestamp()
STAMP = "2026-10-14T09:30:00Z"
TOP_LEVEL_KEYS = {
    "schema_version",
    "since",
    "last_updated",
    "calls",
    "by_tool",
    "by_mode",
    "by_status",
    "error_count",
    "fallback_count",
    "compact_result_count",
    "token_estimates",
    "byte_totals",
}


def _event(
    *,
    tool: str = "contextshrink_open",
    mode: str = "compact",
    status: str = "success",
    compression: bool = True,
    tokenizer: str = "o200k_base",
    counter: str = "counter_v1",
    fallback: bool = False,
    raw_tokens: int | None = None,
    delivered_tokens: int | None = None,
    raw_bytes: int | None = None,
    delivered_bytes: int | None = None,
    source_version: str | None = "source-version-secret",
    error_code: str | None = None,
) -> dict[str, Any]:
    """Build a signed event through the real ledger so sanitization applies."""
    result: dict[str, Any] = {}
    if raw_tokens is not None:
        result["raw_equivalent_token_estimate"] = raw_tokens
    if delivered_tokens is not None:
        result["delivered_token_estimate"] = delivered_tokens
    if raw_bytes is not None:
        result["raw_equivalent_utf8_bytes"] = raw_bytes
    if delivered_bytes is not None:
        result["delivered_utf8_bytes"] = delivered_bytes
    accounting = {
        "count_basis": "estimate",
        "counter_version": counter,
        "tokenizer_name": tokenizer,
        "fallback": fallback,
        "result": result,
    }
    return LocalUsageLedger().record(
        tool=tool,
        mode=mode,
        source_version=source_version,
        status=status,
        accounting=accounting,
        compression_applied=compression,
        error_code=error_code,
    )


def _root_with_note(tmp_path: Path, name: str = "approved") -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "note.txt").write_text("alpha beta gamma\n", encoding="utf-8")
    return root


def _reader(root: Path) -> SourceReader:
    return SourceReader((root,), max_source_bytes=4096)


VALID_DOCUMENT: dict[str, Any] = {
    "schema_version": 1,
    "since": STAMP,
    "last_updated": STAMP,
    "calls": 1,
    "by_tool": {"contextshrink_search": 1},
    "by_mode": {"exact": 1},
    "by_status": {"success": 1},
    "error_count": 0,
    "fallback_count": 0,
    "compact_result_count": 0,
    "token_estimates": {},
    "byte_totals": {"raw_equivalent_sum": 0, "delivered_sum": 0},
}


def test_fresh_file_records_mixed_events(tmp_path: Path) -> None:
    path = tmp_path / "stats" / "usage.json"
    stats = CumulativeUsageStats(path, clock=lambda: CLOCK_START)
    stats.add(
        _event(
            tool="contextshrink_open",
            mode="compact",
            raw_tokens=100,
            delivered_tokens=40,
            raw_bytes=400,
            delivered_bytes=160,
        )
    )
    stats.add(
        _event(
            tool="contextshrink_search",
            mode="exact",
            fallback=True,
            tokenizer="utf8_bytes_div_3_heuristic",
            counter="byte_v1",
            raw_tokens=30,
            delivered_tokens=30,
            raw_bytes=90,
            delivered_bytes=90,
        )
    )
    stats.add(
        _event(
            tool="contextshrink_retrieve",
            mode="focused",
            status="error",
            compression=False,
            error_code="source_unavailable",
        )
    )

    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == TOP_LEVEL_KEYS
    assert data["schema_version"] == 1
    assert data["since"] == STAMP
    assert data["last_updated"] == STAMP
    assert data["calls"] == 3
    assert data["by_tool"] == {
        "contextshrink_open": 1,
        "contextshrink_search": 1,
        "contextshrink_retrieve": 1,
    }
    assert data["by_mode"] == {"compact": 1, "exact": 1, "focused": 1}
    assert data["by_status"] == {"success": 2, "error": 1}
    assert data["error_count"] == 1
    assert data["fallback_count"] == 1
    assert data["compact_result_count"] == 1
    assert data["token_estimates"] == {
        "o200k_base:counter_v1:exact": {
            "calls": 1,
            "raw_equivalent_sum": 100,
            "delivered_sum": 40,
        },
        "utf8_bytes_div_3_heuristic:byte_v1:fallback": {
            "calls": 1,
            "raw_equivalent_sum": 30,
            "delivered_sum": 30,
        },
    }
    assert data["byte_totals"] == {"raw_equivalent_sum": 490, "delivered_sum": 250}


def test_totals_survive_a_new_cumulative_instance(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    first = CumulativeUsageStats(path, clock=lambda: CLOCK_START)
    first.add(_event(raw_tokens=10, delivered_tokens=4, raw_bytes=40, delivered_bytes=16))
    first.add(_event(tool="contextshrink_search", mode="exact", compression=False))

    second = CumulativeUsageStats(path, clock=lambda: CLOCK_START + 60)
    assert second.snapshot()["calls"] == 2
    second.add(_event(raw_tokens=10, delivered_tokens=4, raw_bytes=40, delivered_bytes=16))

    snapshot = second.snapshot()
    assert snapshot["calls"] == 3
    assert snapshot["since"] == STAMP
    assert snapshot["last_updated"] == "2026-10-14T09:31:00Z"
    assert snapshot["token_estimates"]["o200k_base:counter_v1:exact"] == {
        "calls": 2,
        "raw_equivalent_sum": 20,
        "delivered_sum": 8,
        "estimated_tokens_avoided": 12,
    }
    assert snapshot["byte_totals"] == {"raw_equivalent_sum": 80, "delivered_sum": 32}


def test_totals_survive_a_new_backend_on_the_same_path(tmp_path: Path) -> None:
    root = _root_with_note(tmp_path)
    usage = tmp_path / "state" / "usage.json"
    first = LocalBackend(_reader(root), usage_stats_path=usage)
    first.search("note")

    second = LocalBackend(_reader(root), usage_stats_path=usage)
    cumulative = second.stats()["cumulative"]
    assert cumulative["calls"] == 1
    assert cumulative["by_tool"] == {"contextshrink_search": 1}
    second.search("note")
    assert json.loads(usage.read_text(encoding="utf-8"))["calls"] == 2


def test_stats_reports_persistence_off_when_unset(tmp_path: Path) -> None:
    backend = LocalBackend(_reader(_root_with_note(tmp_path)))
    assert backend.stats()["cumulative"] == {"persistence": "off"}


def test_snapshot_public_fields_and_no_path(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    stats = CumulativeUsageStats(path, clock=lambda: CLOCK_START)
    stats.add(_event())
    snapshot = stats.snapshot()
    assert snapshot["persistence"] == "local_file"
    assert snapshot["billing_attribution"] == "not_measured"
    assert "reset_reason" not in snapshot
    assert "write_error" not in snapshot
    assert "scope" in snapshot and "integrity" in snapshot and "reset" in snapshot
    assert str(path) not in json.dumps(snapshot)
    assert str(tmp_path) not in json.dumps(snapshot)


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b"[1, 2]",
        json.dumps({**VALID_DOCUMENT, "schema_version": 2}).encode(),
        json.dumps({**VALID_DOCUMENT, "schema_version": True}).encode(),
        json.dumps({**VALID_DOCUMENT, "calls": "7"}).encode(),
        json.dumps({**VALID_DOCUMENT, "calls": -1}).encode(),
        json.dumps({**VALID_DOCUMENT, "by_tool": {"contextshrink_search": "one"}}).encode(),
        # Valid JSON, but larger than the 64 KiB read cap.
        json.dumps(VALID_DOCUMENT).encode() + b" " * (64 * 1024),
    ],
    ids=[
        "bad_json",
        "not_object",
        "wrong_schema_version",
        "bool_schema_version",
        "string_count",
        "negative_count",
        "bad_counter_map",
        "oversized",
    ],
)
def test_invalid_file_starts_fresh_and_is_kept_until_a_write(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "usage.json"
    path.write_bytes(content)

    stats = CumulativeUsageStats(path, clock=lambda: CLOCK_START)
    assert stats.reset_reason == "invalid"
    assert stats.snapshot()["calls"] == 0
    assert stats.snapshot()["reset_reason"] == "invalid"
    assert path.read_bytes() == content

    stats.add(_event())
    assert json.loads(path.read_text(encoding="utf-8"))["calls"] == 1
    assert CumulativeUsageStats(path).snapshot()["calls"] == 1


def test_directory_at_path_is_unreadable_and_does_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    path.mkdir()

    stats = CumulativeUsageStats(path, clock=lambda: CLOCK_START)
    assert stats.reset_reason == "unreadable"
    stats.add(_event())
    assert stats.write_error is True
    snapshot = stats.snapshot()
    assert snapshot["calls"] == 1
    assert snapshot["write_error"] is True
    assert snapshot["reset_reason"] == "unreadable"
    assert path.is_dir()


def test_unwritable_path_keeps_tool_calls_working(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    backend = LocalBackend(_reader(_root_with_note(tmp_path)), usage_stats_path=blocker / "u.json")

    result = backend.search("note")
    assert isinstance(result, dict)
    cumulative = backend.stats()["cumulative"]
    assert cumulative["calls"] == 1
    assert cumulative["write_error"] is True
    assert blocker.read_text(encoding="utf-8") == "not a directory"


def test_atomic_write_leaves_no_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "usage.json"
    stats = CumulativeUsageStats(path, clock=lambda: CLOCK_START)
    for _ in range(3):
        stats.add(_event())
    assert sorted(item.name for item in tmp_path.iterdir()) == ["usage.json"]
    before = path.read_bytes()

    def failing_replace(source: Any, destination: Any) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", failing_replace)
    stats.add(_event())
    monkeypatch.undo()

    assert stats.write_error is True
    assert sorted(item.name for item in tmp_path.iterdir()) == ["usage.json"]
    assert path.read_bytes() == before

    stats.add(_event())
    assert stats.write_error is False
    assert json.loads(path.read_text(encoding="utf-8"))["calls"] == 5
    assert sorted(item.name for item in tmp_path.iterdir()) == ["usage.json"]


def test_key_caps_collapse_extra_basis_keys_into_other(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    stats = CumulativeUsageStats(path, clock=lambda: CLOCK_START)
    for index in range(70):
        stats.add(_event(tokenizer=f"tok{index}", raw_tokens=2, delivered_tokens=1))

    tokens = stats.snapshot()["token_estimates"]
    assert len(tokens) == 65
    assert tokens["tok0:counter_v1:exact"]["calls"] == 1
    assert tokens["other"]["calls"] == 6
    assert tokens["other"]["raw_equivalent_sum"] == 12
    assert sum(entry["calls"] for entry in tokens.values()) == 70
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert len(stored["token_estimates"]) == 65
    assert stored["calls"] == 70


def test_file_never_holds_text_query_paths_or_per_call_fields(tmp_path: Path) -> None:
    usage_dir = tmp_path / "ZZPATHMARK"
    usage = usage_dir / "usage.json"
    root = tmp_path / "approved"
    root.mkdir()
    (root / "ZZSOURCEMARK.txt").write_text("ZZTEXTMARK body\n", encoding="utf-8")
    backend = LocalBackend(_reader(root), usage_stats_path=usage)
    backend.search("ZZQUERYMARK")
    backend._observe(
        "contextshrink_open",
        {"version": "ZZVERSIONMARK", "requested_mode": "compact"},
        status="error",
        error_code="ZZERRMARK",
    )

    raw = usage.read_bytes()
    for marker in (
        b"ZZPATHMARK",
        b"ZZSOURCEMARK",
        b"ZZTEXTMARK",
        b"ZZQUERYMARK",
        b"ZZVERSIONMARK",
        b"ZZERRMARK",
        str(root).encode("utf-8"),
    ):
        assert marker not in raw
    data = json.loads(raw.decode("utf-8"))
    assert set(data) == TOP_LEVEL_KEYS
    assert data["calls"] == 2
    assert data["error_count"] == 1
    assert set(data["by_tool"]) <= {"contextshrink_search", "contextshrink_open"}


def test_negative_estimated_tokens_avoided_is_kept(tmp_path: Path) -> None:
    backend = LocalBackend(_reader(tmp_path), usage_stats_path=tmp_path / "usage.json")
    backend._observe(
        "contextshrink_open",
        {
            "requested_mode": "compact",
            "compression_applied": True,
            "accounting": {
                "counter_version": "counter_v1",
                "tokenizer_name": "o200k_base",
                "fallback": False,
                "result": {
                    "raw_equivalent_token_estimate": 10,
                    "delivered_token_estimate": 25,
                    "raw_equivalent_utf8_bytes": 40,
                    "delivered_utf8_bytes": 100,
                },
            },
        },
    )
    entry = backend.stats()["cumulative"]["token_estimates"]["o200k_base:counter_v1:exact"]
    assert entry["estimated_tokens_avoided"] == -15
    stored = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert (
        "estimated_tokens_avoided" not in stored["token_estimates"]["o200k_base:counter_v1:exact"]
    )


def test_settings_usage_stats_path_validation(tmp_path: Path) -> None:
    assert DesktopSettings().usage_stats_path is None
    with pytest.raises(ValidationError):
        DesktopSettings(profile="local", usage_stats_path="relative/usage.json")

    root = tmp_path / "root"
    with pytest.raises(ValidationError):
        DesktopSettings(
            profile="local", approved_roots=[root], usage_stats_path=root / "usage.json"
        )
    with pytest.raises(ValidationError):
        DesktopSettings(
            profile="local", approved_roots=[tmp_path], usage_stats_path=tmp_path / "u.json"
        )
    with pytest.raises(ValidationError):
        DesktopSettings(profile="local", approved_roots=[root], usage_stats_path=root)

    accepted = DesktopSettings(
        profile="local",
        approved_roots=[root],
        usage_stats_path=str(tmp_path / "elsewhere" / "usage.json"),
    )
    assert accepted.usage_stats_path == tmp_path / "elsewhere" / "usage.json"


def test_config_file_reports_usage_stats_path_field_only(tmp_path: Path) -> None:
    config = tmp_path / "settings.json"
    config.write_text(
        json.dumps({"profile": "local", "usage_stats_path": "relative/usage.json"}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError) as caught:
        load_settings(config)
    assert caught.value.fields == ("usage_stats_path",)


def _write_config(path: Path, **updates: Any) -> None:
    data: dict[str, Any] = {"profile": "local"}
    data.update(updates)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_make_backend_passes_the_configured_usage_stats_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, Any]] = []

    class CapturingBackend:
        def __init__(self, reader: Any, **kwargs: Any) -> None:
            captured.append(kwargs)

    monkeypatch.setattr(local_backend_module, "LocalBackend", CapturingBackend)
    usage = tmp_path / "stats" / "usage.json"
    _write_config(tmp_path / "off.json")
    _write_config(tmp_path / "on.json", usage_stats_path=str(usage))
    backend_worker._make_backend(load_settings(tmp_path / "off.json"))
    backend_worker._make_backend(load_settings(tmp_path / "on.json"))
    assert captured[0]["usage_stats_path"] is None
    assert captured[1]["usage_stats_path"] == usage


def test_worker_rebuilds_backend_when_usage_stats_path_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[Path | None] = []

    class FakeBackend:
        pass

    def fake_make(settings: DesktopSettings) -> FakeBackend:
        built.append(settings.usage_stats_path)
        return FakeBackend()

    monkeypatch.setattr(backend_worker, "_make_backend", fake_make)
    config = tmp_path / "settings.json"
    _write_config(config)
    runtime = backend_worker.Runtime(
        config, expected_broker_id="desktop", expected_max_clients=8, expected_timeout=10
    )
    assert built == [None]
    runtime._reload()
    assert built == [None]

    usage = tmp_path / "stats" / "usage.json"
    _write_config(config, usage_stats_path=str(usage))
    runtime._reload()
    assert built == [None, usage]


def test_extension_stats_path_is_a_sibling_of_the_extension_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "ContextShrink" / "extension"
    monkeypatch.setenv("CONTEXTSHRINK_EXTENSION_HOME", str(home))
    assert (
        extension.extension_stats_path("unit") == tmp_path / "ContextShrink" / "stats" / "unit.json"
    )
    assert (
        extension.extension_stats_path("unit", base_dir=tmp_path / "x" / "extension")
        == tmp_path / "x" / "stats" / "unit.json"
    )


def test_extension_settings_set_usage_stats_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "CONTEXTSHRINK_EXTENSION_HOME", str(tmp_path / "ContextShrink" / "extension")
    )
    documents = tmp_path / "documents"
    documents.mkdir()
    args = cli._parser().parse_args(
        ["serve", "--extension", "--approved-roots", str(documents), "--broker-id", "unit"]
    )
    settings = extension._extension_settings(args)
    assert settings.usage_stats_path == tmp_path / "ContextShrink" / "stats" / "unit.json"


def test_extension_settings_skip_stats_inside_an_approved_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "CONTEXTSHRINK_EXTENSION_HOME", str(tmp_path / "ContextShrink" / "extension")
    )
    args = cli._parser().parse_args(
        ["serve", "--extension", "--approved-roots", str(tmp_path), "--broker-id", "unit"]
    )
    settings = extension._extension_settings(args)
    assert settings.usage_stats_path is None


def test_cumulative_basis_keeps_versioned_counter_labels(tmp_path: Path) -> None:
    from horizon.desktop_mcp.measurement import CumulativeUsageStats

    stats = CumulativeUsageStats(tmp_path / "stats.json")
    stats.add(
        {
            "tool": "contextshrink_open",
            "mode": "exact",
            "status": "success",
            "tokenizer_name": "o200k_base",
            "counter_version": "tiktoken-0.14.0",
            "fallback": False,
            "result_raw_equivalent_token_estimate": 10,
            "result_delivered_token_estimate": 4,
        }
    )
    assert list(stats.snapshot()["token_estimates"]) == ["o200k_base:tiktoken-0.14.0:exact"]
