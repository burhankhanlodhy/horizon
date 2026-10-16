"""Byte-exact tests for the log_templates transform (timestamped log legends)."""

from __future__ import annotations

import importlib.util
import random
import re
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp.compression import (
    compact_text,
    compression_decoding_hint,
    infer_compaction_kind,
)
from horizon.desktop_mcp.log_templates import (
    encode_log_templates,
    expand_log_templates,
    looks_like_log,
)
from horizon.transforms.lossless_compaction import expand_runs, strip_ansi

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "desktop" / "mcp" / "self-test" / "make_dataset.py"
_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _load_generator() -> Any:
    spec = importlib.util.spec_from_file_location("make_dataset", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _restore(encoded: str) -> str:
    return expand_runs(expand_log_templates(encoded))


def _log_lines(count: int, rests: list[str], *, seed: int = 1) -> list[str]:
    rng = random.Random(seed)
    lines = []
    for index in range(count):
        stamp = f"2026-10-16T10:{index // 60 % 60:02d}:{index % 60:02d}Z"
        lines.append(f"{stamp} {rng.choice(rests)}")
    return lines


_SERVICE_RESTS = [
    "INFO edge-router heartbeat ok",
    "INFO billing-core heartbeat ok",
    "WARN queue-relay backlog depth 412",
]


@pytest.fixture(scope="module")
def service_events() -> str:
    module = _load_generator()
    files, _pack = module.build(20261016, "st_session_v1", None, "session")
    return files["service_events.txt"]


def test_session_service_events_is_templated_and_byte_exact(service_events: str) -> None:
    raw_bytes = len(service_events.encode("utf-8"))
    assert infer_compaction_kind("txt", service_events) == "log"

    outcome = compact_text(service_events, "log")

    assert outcome.applied
    assert outcome.transform == "log_templates"
    assert outcome.fidelity == "reversible_fold"
    assert _restore(outcome.text) == service_events
    assert len(outcome.text.encode("utf-8")) <= raw_bytes * 0.85
    hint = compression_decoding_hint(outcome.transform, outcome.fidelity)
    assert hint is not None and "`@k = <text>`" in hint


def test_session_service_events_keeps_every_timestamp_verbatim(service_events: str) -> None:
    outcome = compact_text(service_events, "log")
    assert outcome.transform == "log_templates"
    assert _TS.findall(_restore(outcome.text)) == _TS.findall(service_events)
    # The date-prefix form is chosen here: each time keeps its exact text, and the
    # date comes from the header line above it.
    headers = re.findall(r"^## date (\S+)$", outcome.text, re.MULTILINE)
    assert headers
    times_out = re.findall(r"^(\d{2}:\d{2}:\d{2}Z) ", outcome.text, re.MULTILINE)
    times_src = re.findall(r"T(\d{2}:\d{2}:\d{2}Z) ", service_events)
    assert times_out == times_src


def test_crlf_and_missing_final_newline_round_trip() -> None:
    lines = _log_lines(60, _SERVICE_RESTS, seed=2)
    crlf = "\r\n".join(lines)
    assert not crlf.endswith("\n")
    for source in (crlf, crlf + "\r\n", "\n".join(lines) + "\n"):
        outcome = compact_text(source, "log")
        assert outcome.applied, source[-20:]
        assert outcome.transform == "log_templates"
        assert _restore(outcome.text) == source


def test_legend_is_only_for_rests_seen_three_times() -> None:
    source = "\n".join(
        [
            "2026-10-16T10:00:00Z only once",
            "2026-10-16T10:00:01Z twice",
            "2026-10-16T10:00:02Z twice",
            "2026-10-16T10:00:03Z thrice",
            "2026-10-16T10:00:04Z thrice",
            "2026-10-16T10:00:05Z thrice",
        ]
    )
    encoded = encode_log_templates(source, date_prefix=False)
    assert encoded is not None
    assert encoded.split("\n")[0] == "@1 = thrice"
    assert encoded.split("\n")[1:] == [
        "2026-10-16T10:00:00Z only once",
        "2026-10-16T10:00:01Z twice",
        "2026-10-16T10:00:02Z twice",
        "2026-10-16T10:00:03Z @1",
        "2026-10-16T10:00:04Z @1",
        "2026-10-16T10:00:05Z @1",
    ]
    assert _restore(encoded) == source


@pytest.mark.parametrize(
    "bad_line",
    [
        "2026-10-16T10:00:09Z worker finished @1",
        "2026-10-16T10:00:09Z @1 = injected",
        "@1 = injected before the body",
        "@2 plain text at line start",
    ],
)
def test_source_lines_that_collide_with_syntax_are_not_encoded(bad_line: str) -> None:
    lines = _log_lines(40, _SERVICE_RESTS, seed=3) + [bad_line]
    source = "\n".join(lines) + "\n"

    assert encode_log_templates(source) is None
    outcome = compact_text(source, "log")
    if outcome.applied:
        assert outcome.transform != "log_templates"
        assert expand_runs(outcome.text) == source
    else:
        assert outcome.text == source
        assert outcome.fidelity == "byte_exact"


def test_mixed_lines_without_timestamps_pass_through_unchanged() -> None:
    stamped = _log_lines(45, _SERVICE_RESTS, seed=4)
    stack = [
        "java.lang.IllegalStateException: checksum",
        "\tat com.example.Store.write(Store.java:12)",
    ]
    lines: list[str] = []
    for index, line in enumerate(stamped):
        lines.append(line)
        if index % 9 == 0:
            lines.extend(stack)
        if index % 11 == 0:
            lines.append("")
    source = "\n".join(lines) + "\n"

    outcome = compact_text(source, "log")
    assert outcome.applied
    assert outcome.transform == "log_templates"
    assert _restore(outcome.text) == source
    assert "\tat com.example.Store.write(Store.java:12)" in outcome.text


def test_ansi_color_is_normalized_and_reported_as_such() -> None:
    lines = [f"\x1b[32m{line}\x1b[0m" for line in _log_lines(50, _SERVICE_RESTS, seed=5)]
    source = "\n".join(lines) + "\n"

    outcome = compact_text(source, "log")
    assert outcome.applied
    assert outcome.transform == "log_templates"
    assert outcome.fidelity == "normalized_log_ansi"
    assert _restore(outcome.text) == strip_ansi(source)


def test_templates_below_fifteen_percent_saving_are_not_applied() -> None:
    filler = [
        f"2026-10-16T11:{n // 60:02d}:{n % 60:02d}Z DEBUG unique-{n:04d} " + "x" * 120
        for n in range(60)
    ]
    repeated = ["2026-10-16T12:00:00Z ok", "2026-10-16T12:00:01Z ok", "2026-10-16T12:00:02Z ok"]
    source = "\n".join(filler + repeated) + "\n"

    outcome = compact_text(source, "log")
    assert outcome.transform != "log_templates"


def test_nginx_style_access_log_is_not_templated_and_stays_exact() -> None:
    rng = random.Random(6)
    lines = [
        f"203.0.113.{rng.randint(1, 9)} - - [16/Oct/2026:10:00:{n % 60:02d} +0000] "
        f'"GET /healthz HTTP/1.1" 200 2 "-" "probe/1.0"'
        for n in range(80)
    ]
    source = "\n".join(lines) + "\n"

    assert not looks_like_log(source)
    outcome = compact_text(source, "log")
    assert outcome.transform != "log_templates"
    if outcome.applied:
        assert expand_runs(outcome.text) == source
    else:
        assert outcome.text == source


def test_jsonl_app_log_keeps_existing_behaviour() -> None:
    lines = [
        '{"level":"info","msg":"heartbeat ok","service":"billing-core"}' for _ in range(30)
    ] + ['{"level":"error","msg":"queue lease expired"}']
    source = "\n".join(lines) + "\n"

    assert infer_compaction_kind("jsonl", source) == "log"
    outcome = compact_text(source, "log")
    assert outcome.transform in (None, "log_runs")
    if outcome.applied:
        assert expand_runs(outcome.text) == source
    else:
        assert outcome.text == source


def test_txt_kind_follows_the_eighty_percent_timestamp_rule() -> None:
    log_text = "\n".join(_log_lines(20, _SERVICE_RESTS, seed=7)) + "\n"
    prose = "Operations notes for the week.\nThe depot stays open on Sunday.\n"
    mostly_log = "\n".join(_log_lines(7, _SERVICE_RESTS, seed=8) + ["orphan frame"] * 3) + "\n"

    assert infer_compaction_kind("txt", log_text) == "log"
    assert infer_compaction_kind(".TXT", log_text) == "log"
    assert infer_compaction_kind("txt", prose) == "text"
    assert infer_compaction_kind("txt", mostly_log) == "text"
    assert infer_compaction_kind("md", log_text) == "text"
    assert infer_compaction_kind("text", log_text) == "text"


def test_prose_txt_keeps_text_transform_behaviour() -> None:
    source = "alpha beta gamma\n" * 30 + "end\n"
    kind = infer_compaction_kind("txt", source)
    assert kind == "text"
    outcome = compact_text(source, kind)
    assert outcome.transform == "repeated_lines"
    assert expand_runs(outcome.text) == source


def test_randomized_round_trips_are_exact() -> None:
    rng = random.Random(20261016)
    pool = [
        f"{level} {svc} {verb}"
        for level in ("INFO", "WARN")
        for svc in ("a", "b")
        for verb in ("ok", "retry 3", "")
    ]
    dates = ["2026-10-15", "2026-10-16", "2026-10-17", "2026-11-01"]
    encoded_count = 0
    dated_count = 0
    for case in range(500):
        count = rng.randint(1, 60)
        day = rng.choice(dates)
        lines: list[str] = []
        for index in range(count):
            if rng.random() < 0.15:  # date changes, sometimes backwards
                day = rng.choice(dates)
            roll = rng.random()
            clock = f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{index % 60:02d}"
            if roll < 0.08:
                lines.append("")
            elif roll < 0.15:
                lines.append(f"  continuation {rng.randint(0, 3)}")
            elif roll < 0.2:
                lines.append(f"{day} {clock} two spaces {rng.choice(pool)}")
            elif roll < 0.25:
                lines.append(f"Aug 16 {clock} host syslog {rng.choice(pool)}")
            elif roll < 0.3:
                lines.append(f"{day}T{clock}+02:00 {rng.choice(pool)}")
            else:
                zone = "Z" if rng.random() < 0.5 else ""
                lines.append(f"{day}T{clock}{zone} {rng.choice(pool)}")
        separator = "\r\n" if case % 4 == 0 else "\n"
        source = separator.join(lines)
        if case % 3:
            source += separator
        for date_prefix in (False, True):
            encoded = encode_log_templates(source, date_prefix=date_prefix)
            if encoded is None:
                continue
            encoded_count += 1
            assert expand_log_templates(encoded) == source, (case, date_prefix)
            if "## date " in encoded:
                dated_count += 1
    assert encoded_count > 100
    assert dated_count > 50


def test_randomized_round_trips_with_time_like_lines_are_exact() -> None:
    rng = random.Random(20261017)
    for case in range(200):
        lines = [
            f"2026-10-16T10:00:{index % 60:02d}Z {_SERVICE_RESTS[0]}"
            for index in range(rng.randint(3, 30))
        ]
        if case % 2:
            lines.insert(rng.randrange(len(lines)), f"{rng.randint(0, 23):02d}:00:00Z raw")
        source = "\n".join(lines) + "\n"
        encoded = encode_log_templates(source)
        assert encoded is not None
        assert expand_log_templates(encoded) == source, case
        if case % 2:
            assert "## date " not in encoded


def test_encode_refuses_empty_and_legendless_text() -> None:
    assert encode_log_templates("") is None
    assert encode_log_templates("no stamps here\nat all\n") is None
    two_lines = "2026-10-16T10:00:00Z a\n2026-10-16T10:00:01Z b\n"
    assert encode_log_templates(two_lines, date_prefix=False) is None
    # The date-prefix form still applies without a legend, and it is exact.
    dated = encode_log_templates(two_lines)
    assert dated == "## date 2026-10-16T\n10:00:00Z a\n10:00:01Z b\n"
    assert expand_log_templates(dated) == two_lines


def test_date_prefix_form_writes_a_header_per_date_change() -> None:
    lines = [
        "2026-10-16T09:00:00Z INFO a ok",
        "2026-10-16T09:00:01Z INFO a ok",
        "2026-10-17T00:00:00+02:00 INFO b rolled",
        "2026-10-17T00:00:01+02:00 INFO b rolled",
        "2026-10-17 00:00:02 spaced separator stays full",
        "2026-10-17T00:00:03+02:00 INFO b rolled",
    ]
    source = "\n".join(lines) + "\n"
    legend_only = encode_log_templates(source, date_prefix=False)
    assert legend_only is not None
    assert legend_only.split("\n")[0] == "@1 = INFO b rolled"
    dated = encode_log_templates(source)
    assert dated is not None
    assert dated.split("\n") == [
        "@1 = INFO b rolled",
        "## date 2026-10-16T",
        "09:00:00Z INFO a ok",
        "09:00:01Z INFO a ok",
        "## date 2026-10-17T",
        "00:00:00+02:00 @1",
        "00:00:01+02:00 @1",
        "2026-10-17 00:00:02 spaced separator stays full",
        "00:00:03+02:00 @1",
        "",
    ]
    assert expand_log_templates(dated) == source


def test_default_choice_is_never_larger_than_the_legend_form() -> None:
    # Dates alternate on every line, so the date-prefix form adds a header per
    # line; the smaller verified form must win.
    rng = random.Random(11)
    lines = []
    for index in range(60):
        day = 16 if index % 2 == 0 else 17
        lines.append(f"2026-10-{day}T10:00:{index % 60:02d}Z {rng.choice(_SERVICE_RESTS)}")
    source = "\n".join(lines) + "\n"
    plain = encode_log_templates(source, date_prefix=False)
    chosen = encode_log_templates(source)
    assert plain is not None and chosen is not None
    assert len(chosen.encode("utf-8")) <= len(plain.encode("utf-8"))
    assert expand_log_templates(chosen) == source


@pytest.mark.parametrize(
    "bad_line",
    [
        "## date 2026-10-16T",
        "## note this line starts with two hashes",
    ],
)
def test_header_like_source_lines_are_not_encoded(bad_line: str) -> None:
    lines = _log_lines(40, _SERVICE_RESTS, seed=9) + [bad_line]
    source = "\n".join(lines) + "\n"
    assert encode_log_templates(source) is None
    outcome = compact_text(source, "log")
    assert outcome.transform != "log_templates"
    if outcome.applied:
        assert expand_runs(outcome.text) == source
    else:
        assert outcome.text == source


def test_time_like_plain_line_disables_only_the_date_prefix_form() -> None:
    lines = _log_lines(40, _SERVICE_RESTS, seed=10) + ["12:00:00 bare time line"]
    source = "\n".join(lines) + "\n"
    legend_only = encode_log_templates(source, date_prefix=False)
    assert legend_only is not None
    assert encode_log_templates(source) == legend_only
    assert "## date" not in legend_only
    assert expand_log_templates(legend_only) == source


def test_identical_consecutive_lines_compose_with_templates_exactly() -> None:
    lines: list[str] = []
    for index in range(30):
        stamp = f"2026-10-16T13:00:{index:02d}Z"
        lines.append(f"{stamp} INFO scheduler tick")
        lines.extend([f"{stamp} WARN queue-relay backlog depth 412"] * 6)
    source = "\n".join(lines) + "\n"

    outcome = compact_text(source, "log")
    assert outcome.applied
    assert outcome.transform in ("log_runs", "log_templates")
    assert expand_runs(expand_log_templates(outcome.text)) == source


@pytest.mark.parametrize("date_prefix", [False, True])
def test_both_forms_round_trip_crlf_and_final_newline_variants(date_prefix: bool) -> None:
    lines = [
        f"2026-10-{16 + index // 20:02d}T10:{index % 60:02d}:00Z {_SERVICE_RESTS[index % 2]}"
        for index in range(50)
    ]
    for source in (
        "\r\n".join(lines),
        "\r\n".join(lines) + "\r\n",
        "\n".join(lines) + "\n",
        "\n".join(lines),
    ):
        encoded = encode_log_templates(source, date_prefix=date_prefix)
        assert encoded is not None
        assert expand_log_templates(encoded) == source
