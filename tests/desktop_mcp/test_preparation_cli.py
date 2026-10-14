"""Tests for the explicit local preparation command-line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp import preparation_cli
from horizon.desktop_mcp.__main__ import main as desktop_main


def _input_files(tmp_path: Path) -> tuple[Path, Path]:
    request = tmp_path / "request.txt"
    request.write_bytes(b"Please compare the supplied notes.\n")
    context = tmp_path / "context.md"
    context.write_bytes(b"One exact supporting detail.\n")
    return request, context


def _inline_args(request: Path, context: Path) -> list[str]:
    return [
        "--request-file",
        str(request),
        "--mode",
        "passthrough",
        "--context-file",
        str(context),
    ]


def test_inline_preview_uses_no_broker_and_does_not_print_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    request, context = _input_files(tmp_path)

    def broker_must_not_be_used(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("inline preparation must not contact the broker")

    monkeypatch.setattr(preparation_cli, "_reference_from_broker", broker_must_not_be_used)
    result = desktop_main(["prepare", *_inline_args(request, context)])

    assert result == 0
    output = capsys.readouterr().out
    preview = json.loads(output)
    assert preview["status"] == "preview_ready"
    assert preview["packet_written"] is False
    assert preview["copy_instructions"].endswith("manually.")
    assert "Please compare" not in output
    assert "One exact supporting detail" not in output

    assert preparation_cli.main([*_inline_args(request, context), "--show-packet"]) == 0
    visible_preview = json.loads(capsys.readouterr().out)
    packet = json.loads(visible_preview["packet_json"])
    assert packet["request"] == "Please compare the supplied notes.\n"
    assert packet["supporting_context"]["text"] == "One exact supporting detail.\n"


def test_export_requires_last_confirmation_and_does_not_overwrite_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request, context = _input_files(tmp_path)
    output = tmp_path / "prepared.json"
    args = _inline_args(request, context) + ["--output", str(output)]

    with pytest.raises(SystemExit) as missing_confirmation:
        preparation_cli.main(args)
    assert missing_confirmation.value.code == 2
    assert not output.exists()
    capsys.readouterr()

    assert preparation_cli.main([*args, "--confirm"]) == 0
    exported = json.loads(output.read_text(encoding="utf-8"))
    assert exported["request"] == "Please compare the supplied notes.\n"
    assert exported["supporting_context"]["text"] == "One exact supporting detail.\n"
    capsys.readouterr()

    assert preparation_cli.main([*args, "--confirm"]) == 2
    assert json.loads(output.read_text(encoding="utf-8")) == exported
    assert json.loads(capsys.readouterr().err)["error"] == "output_exists"


def test_overwrite_requires_explicit_flag_and_confirmation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request, context = _input_files(tmp_path)
    output = tmp_path / "prepared.json"
    args = _inline_args(request, context) + ["--output", str(output), "--overwrite"]

    with pytest.raises(SystemExit) as missing_confirmation:
        preparation_cli.main(args)
    assert missing_confirmation.value.code == 2
    assert not output.exists()
    capsys.readouterr()

    assert preparation_cli.main([*args, "--confirm"]) == 0
    assert output.exists()
    assert json.loads(capsys.readouterr().out)["packet_written"] is True


def test_confirm_must_be_final_argument(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    request, context = _input_files(tmp_path)
    args = ["--confirm", *_inline_args(request, context)]
    with pytest.raises(SystemExit) as not_last:
        preparation_cli.main(args)
    assert not_last.value.code == 2
    assert "--confirm must be the last" in capsys.readouterr().err


def test_export_cannot_overwrite_request_or_context_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request, context = _input_files(tmp_path)
    original_request = request.read_bytes()
    result = preparation_cli.main(
        [*_inline_args(request, context), "--output", str(request), "--overwrite", "--confirm"]
    )
    assert result == 2
    assert request.read_bytes() == original_request
    assert json.loads(capsys.readouterr().err)["error"] == "output_conflicts_with_input"


def test_failed_output_write_cleans_partial_files_and_preserves_old_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "prepared.json"
    destination.write_text("old", encoding="utf-8")

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("synthetic write failure")

    monkeypatch.setattr(preparation_cli.os, "fsync", fail_fsync)
    with pytest.raises(preparation_cli.PreparationCliError) as overwrite_error:
        preparation_cli._write_packet(destination, '{"new":true}', overwrite=True)
    assert overwrite_error.value.code == "output_write_failed"
    assert destination.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".prepared.json.*.tmp")) == []

    created = tmp_path / "new.json"
    with pytest.raises(preparation_cli.PreparationCliError) as create_error:
        preparation_cli._write_packet(created, '{"new":true}', overwrite=False)
    assert create_error.value.code == "output_write_failed"
    assert not created.exists()


def test_reference_mode_reads_only_existing_exact_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from horizon.desktop_mcp import broker

    request = tmp_path / "request.txt"
    request.write_bytes(b"Explain the source.\n")
    config = tmp_path / "local.json"
    config.write_text('{"profile":"local"}', encoding="utf-8")
    source_id = "cs1_" + "a" * 32
    version = "sha256:" + "b" * 64 + ":text-v1"
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeBrokerClient:
        def __init__(self, settings: Any) -> None:
            assert settings.profile == "local"

        def open(self, handle: str, **kwargs: Any) -> dict[str, Any]:
            calls.append((handle, kwargs))
            return {
                "source_id": source_id,
                "version": version,
                "kind": "md",
                "coverage": "exact selected lines at immutable snapshot version",
                "selected_lines": {"start": 1, "end": 1},
                "text": "First exact page only.\n",
                "more_available": True,
                "next_cursor": "cursor-remains-local",
            }

    monkeypatch.setattr(broker, "BrokerClient", FakeBrokerClient)
    result = preparation_cli.main(
        [
            "--request-file",
            str(request),
            "--mode",
            "reference",
            "--source-id",
            source_id,
            "--config",
            str(config),
            "--show-packet",
        ]
    )

    assert result == 0
    assert len(calls) == 1
    assert calls[0][0] == source_id
    assert calls[0][1]["mode"] == "exact"
    output = json.loads(capsys.readouterr().out)
    packet = json.loads(output["packet_json"])
    context = packet["supporting_context"]
    assert context["source"]["version"] == version
    assert context["evidence"] == "First exact page only.\n"
    assert "first bounded exact page only" in context["coverage"]
    assert context["exact_recovery"]["source_id"] == source_id

    assert (
        preparation_cli.main(
            [
                "--request-file",
                str(request),
                "--mode",
                "focused",
                "--source-id",
                source_id,
                "--config",
                str(config),
                "--query",
                "First",
                "--show-packet",
            ]
        )
        == 0
    )
    focused_preview = json.loads(capsys.readouterr().out)
    focused_packet = json.loads(focused_preview["packet_json"])
    focused_context = focused_packet["supporting_context"]
    assert "first bounded exact page only" in focused_context["source"]["input_evidence_coverage"]
    assert focused_context["ranking_scope"] == "provided_bounded_evidence_only"
    assert focused_context["coverage"].startswith("partial")


def test_reference_mode_rejects_path_like_source_id_before_broker_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from horizon.desktop_mcp import broker

    request = tmp_path / "request.txt"
    request.write_bytes(b"Explain the source.\n")
    config = tmp_path / "local.json"
    config.write_text('{"profile":"local"}', encoding="utf-8")
    constructed = False

    class BrokerMustNotBeConstructed:
        def __init__(self, _settings: Any) -> None:
            nonlocal constructed
            constructed = True
            raise AssertionError("invalid source handles must be rejected first")

    monkeypatch.setattr(broker, "BrokerClient", BrokerMustNotBeConstructed)
    result = preparation_cli.main(
        [
            "--request-file",
            str(request),
            "--mode",
            "reference",
            "--source-id",
            str(tmp_path / "approved.md"),
            "--config",
            str(config),
        ]
    )
    assert result == 2
    assert not constructed
    assert json.loads(capsys.readouterr().err)["error"] == "invalid_source_id"
