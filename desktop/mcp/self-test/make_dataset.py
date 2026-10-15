"""Deterministic generator for the portable self-test document set.

Writes ``DIR/documents/*`` and ``DIR/pack.json`` for a clearly fictional company.
Every answer is computed from the generated data structures. Output depends only
on ``--seed`` and this file: no clock, no locale, LF line endings.

Usage: python make_dataset.py --out DIR [--seed 20261015]

Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_SEED = 20261015
COMPANY = "Quorvale Freight Partners"
NOTICE = (
    f"Fictional test data: {COMPANY} is an invented company, so nothing in these files is real."
)
TARGET_BYTES = {
    "operations_handbook.md": 60 * 1024,
    "orders_export.json": 120 * 1024,
    "service_events.txt": 100 * 1024,
    "platform_config.json": 40 * 1024,
    "contract_summary.md": 3 * 1024,
    "team_contacts.csv": 3 * 1024,
}
_SKU_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_REGIONS = [
    "Northern Plains",
    "Coastal South",
    "Lake Ridge",
    "Harbor East",
    "Summit West",
    "River Delta",
]
_STATUSES = ["pending", "shipped", "delivered", "cancelled", "returned"]
_MONTHS = [7, 8, 9]


class DatasetError(RuntimeError):
    """Raised when the output directory cannot be used."""


# ----------------------------------------------------------------- shared helpers


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _date_2026(month: int, day: int) -> str:
    return f"2026-{month:02d}-{day:02d}"


def _money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


# ------------------------------------------------------------ operations handbook

_HANDBOOK_TITLES = [
    "Purpose and scope",
    "Roles and approvals",
    "Inbound receiving",
    "Dock scheduling",
    "Trailer seal checks",
    "Temperature logging",
    "Dock reconciliation",
    "Pallet labelling",
    "Weight and dimension checks",
    "Damage claims",
    "Carrier onboarding",
    "Hazardous goods handling",
    "Cycle counting",
    "Stock adjustments",
    "Returns intake",
    "Customer order cut-off",
    "Picking accuracy",
    "Packing standards",
    "Quarantine handling",
    "Outbound loading",
    "Driver check-in",
    "Yard movements",
    "Incident reporting",
    "Near-miss review",
    "Safety training records",
    "Fire drill schedule",
    "Equipment inspections",
    "Forklift certification",
    "Vendor invoice matching",
    "Expense approvals",
    "Payroll cut-off",
    "Month-end close",
    "Annual audit calendar",
    "Data retention",
    "Access control",
    "Password rotation",
    "Change freezes",
    "Vendor risk reviews",
    "Customer complaints",
    "Continuous improvement",
]
_ROLES = [
    "shift lead",
    "dock supervisor",
    "compliance analyst",
    "warehouse controller",
    "carrier manager",
    "inventory auditor",
    "regional director",
    "safety officer",
    "billing clerk",
    "night operations manager",
    "returns coordinator",
    "yard marshal",
]
_VERBS = ["checks", "records", "confirms", "reviews", "signs off on", "logs"]
_OBJECTS = [
    "the seal number on every trailer",
    "the temperature reading at intake",
    "each pallet count",
    "the signed bill of lading",
    "the exception ticket",
    "the handover checklist",
    "the weight manifest",
    "the photo evidence for damage",
    "the labelled tote count",
    "the driver declaration",
]
_EVENTS = ["arrival", "unloading", "dispatch", "the shift handover", "the cycle count"]
_METRICS = [
    "on-time departures",
    "dock utilisation",
    "count accuracy",
    "claim turnaround",
    "first-pass acceptance",
]
_PERIODS = ["shift", "week", "batch"]
_SENTENCES = [
    "The {role} {verb} {obj} within {mins} minutes of {event}.",
    "A {role2} who skips {obj} is recorded as a control failure under rule {code}.",
    "Exceptions above {pct} percent of the batch go to the {role2} before release.",
    "Records of {obj} are kept for {days} days and are read-only after the first review.",
    "If {count} or more items fail in one {period}, the {role} opens a review with the {role2}.",
    "Targets for {metric} are {pct} percent and are reviewed every {days} days.",
    "Work that starts before {hour}:00 needs written sign-off from the {role2}.",
    "Any change to {obj} needs the {role2} to approve it in the system of record.",
]
_FACT_SECTIONS = {7: "dock", 19: "quarantine", 33: "audit"}


def _sentence(rng: random.Random) -> str:
    values = {
        "role": rng.choice(_ROLES),
        "role2": rng.choice(_ROLES),
        "verb": rng.choice(_VERBS),
        "obj": rng.choice(_OBJECTS),
        "event": rng.choice(_EVENTS),
        "mins": rng.randint(5, 180),
        "code": f"R-{rng.randint(10, 99)}.{rng.randint(1, 9)}",
        "pct": rng.randint(2, 40),
        "days": rng.randint(7, 365),
        "count": rng.randint(3, 25),
        "period": rng.choice(_PERIODS),
        "metric": rng.choice(_METRICS),
        "hour": rng.randint(4, 9),
    }
    return rng.choice(_SENTENCES).format(**values)


def _paragraph(rng: random.Random) -> str:
    if rng.random() < 0.25:
        items = [f"- {_sentence(rng)}" for _ in range(rng.randint(3, 5))]
        return "\n".join(items)
    return " ".join(_sentence(rng) for _ in range(rng.randint(4, 6)))


def _handbook(rng: random.Random) -> tuple[str, dict[str, dict[str, Any]]]:
    """Return the Markdown text and the three fact records (values drawn here)."""
    hours = rng.randint(12, 96)
    weight = rng.choice(range(600, 1600, 50))
    bay_code = f"BAY-{rng.choice(_SKU_LETTERS)}{rng.randint(100, 999)}"
    audit_day = rng.randint(2, 27)
    facts = {
        "dock": {
            "sentence": f"The dock reconciliation hold lasts {hours} hours after the last trailer is unloaded.",
            "question": "In operations_handbook.md, how many hours does the dock reconciliation hold last? Answer with the number of hours only.",
            "answer": {"kind": "number", "expected": str(hours), "tolerance": "0"},
        },
        "quarantine": {
            "sentence": f"The quarantine bay code for pallets over {weight} kg is {bay_code}, and the bay stays sealed until a supervisor clears it.",
            "question": f"In operations_handbook.md, what is the quarantine bay code for pallets over {weight} kg? Answer with the code only.",
            "answer": {"kind": "text_contains", "expected": bay_code},
        },
        "audit": {
            "sentence": f"The annual audit freeze begins on {_date_2026(11, audit_day)}, and no ledger entries may be edited after that day.",
            "question": "In operations_handbook.md, on what date does the annual audit freeze begin? Answer as YYYY-MM-DD.",
            "answer": {"kind": "date", "expected": _date_2026(11, audit_day)},
        },
    }
    per_section = TARGET_BYTES["operations_handbook.md"] * 4 // (5 * len(_HANDBOOK_TITLES))
    parts = [f"> {NOTICE}", "", f"# Operations Handbook: {COMPANY}", ""]
    for number, title in enumerate(_HANDBOOK_TITLES, 1):
        paragraphs: list[str] = []
        size = 0
        goal = per_section + rng.randint(-250, 250)
        while size < goal:
            text = _paragraph(rng)
            paragraphs.append(text)
            size += len(text) + 2
        fact_key = _FACT_SECTIONS.get(number)
        if fact_key is not None:
            paragraphs.insert(rng.randrange(len(paragraphs) + 1), facts[fact_key]["sentence"])
        parts.append(f"## {number}. {title}")
        parts.append("")
        parts.append("\n\n".join(paragraphs))
        parts.append("")
    text = "\n".join(parts)
    return text, facts


# ------------------------------------------------------------------- order export


def _orders(rng: random.Random) -> tuple[str, list[dict[str, Any]]]:
    """Return the JSON text and the three lookup tasks, computed from the orders."""
    ids = rng.sample(range(10000, 20000), 350)
    orders: list[dict[str, Any]] = []
    for number in ids:
        skus: list[str] = []
        items: list[dict[str, Any]] = []
        for _ in range(rng.choice([1, 1, 1, 2, 2, 3])):
            sku = f"SKU-{rng.choice(_SKU_LETTERS)}{rng.randint(1000, 9999)}"
            while sku in skus:
                sku = f"SKU-{rng.choice(_SKU_LETTERS)}{rng.randint(1000, 9999)}"
            skus.append(sku)
            items.append(
                {
                    "sku": sku,
                    "qty": rng.randint(1, 9),
                    "unit_price": _money(rng.randint(199, 49999)),
                }
            )
        month = rng.choice(_MONTHS)
        orders.append(
            {
                "order_id": f"NW-{number}",
                "customer_id": f"CU-{rng.randint(10000, 99999)}",
                "region": rng.choice(_REGIONS),
                "status": rng.choice(_STATUSES),
                "created": _date_2026(month, rng.randint(1, 28)),
                "items": items,
            }
        )

    def _count_order(order_id: str) -> int:
        return sum(1 for order in orders if order["order_id"] == order_id)

    tasks: dict[str, dict[str, Any]] = {}

    # Price lookup: choose an order item whose SKU appears once in that order.
    order = orders[rng.randrange(len(orders))]
    item = order["items"][rng.randrange(len(order["items"]))]
    assert _count_order(order["order_id"]) == 1
    assert sum(1 for entry in order["items"] if entry["sku"] == item["sku"]) == 1
    tasks["price"] = {
        "question": (
            f"In orders_export.json, what is the unit_price of SKU {item['sku']} "
            f"in order {order['order_id']}? Answer with the number only."
        ),
        "answer": {
            "kind": "number",
            "expected": item["unit_price"],
            "tolerance": "0",
        },
    }

    order = orders[rng.randrange(len(orders))]
    assert _count_order(order["order_id"]) == 1
    tasks["region"] = {
        "question": (
            f"In orders_export.json, which region is order {order['order_id']} in? "
            "Answer with the region name."
        ),
        "answer": {"kind": "text_contains", "expected": order["region"]},
    }

    order = orders[rng.randrange(len(orders))]
    assert _count_order(order["order_id"]) == 1
    tasks["created"] = {
        "question": (
            f"In orders_export.json, on what date was order {order['order_id']} created? "
            "Answer as YYYY-MM-DD."
        ),
        "answer": {"kind": "date", "expected": order["created"]},
    }

    text = _json_text({"_notice": NOTICE, "orders": orders})
    return text, tasks


# ----------------------------------------------------------------- service events

_FILLER_SERVICES = [
    "auth-proxy",
    "report-builder",
    "edge-router",
    "billing-core",
    "search-indexer",
    "mail-dispatch",
    "metrics-agent",
]
_ERROR_SERVICE = "ledger-sync"
_WARN_SERVICE = "cache-gateway"
_RESTART_SERVICE = "queue-relay"
_LOG_START = datetime(2026, 8, 1, 6, 0, 0)
_LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) (\w+) (\S+) (.*)$")


def _entry(
    rng: random.Random,
    level: str,
    service: str,
    message: str,
    count: int = 1,
    gap: int | None = None,
) -> dict[str, Any]:
    return {
        "gap": rng.randint(1, 4) if gap is None else gap,
        "level": level,
        "service": service,
        "message": message,
        "count": count,
    }


def _raw(lines: list[str]) -> dict[str, Any]:
    """Untimestamped lines (stack-trace frames) emitted exactly as given."""
    return {"raw": lines}


def _entry_bytes(entry: dict[str, Any]) -> int:
    if "raw" in entry:
        return sum(len(line) + 1 for line in entry["raw"])
    body = f"{entry['level']} {entry['service']} {entry['message']}"
    return entry["count"] * (len("2026-08-01T06:00:00Z ") + len(body) + 1)


def _render_events(entries: list[dict[str, Any]]) -> list[str]:
    clock = _LOG_START
    lines: list[str] = []
    for entry in entries:
        if "raw" in entry:
            lines.extend(entry["raw"])
            continue
        clock += timedelta(seconds=entry["gap"])
        stamp = clock.strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"{stamp} {entry['level']} {entry['service']} {entry['message']}"
        lines.extend([line] * entry["count"])
    return lines


_TRACE_REASONS = [
    "ledger batch state mismatch",
    "commit offset ahead of log",
    "record checksum did not match",
    "queue lease expired mid-write",
]
_TRACE_CLASSES = [
    "com.quorvale.ledger.BatchWorker",
    "com.quorvale.ledger.CommitQueue",
    "com.quorvale.io.RecordStore",
    "com.quorvale.util.RetryPolicy",
]
_TRACE_METHODS = ["run", "flush", "apply", "retry", "commit", "write"]


def _trace_frames(rng: random.Random) -> list[str]:
    """One stack trace: an exception line plus 5-9 untimestamped frame lines."""
    frames = [f"java.lang.IllegalStateException: {rng.choice(_TRACE_REASONS)}"]
    while len(frames) < rng.randint(6, 10):
        cls = rng.choice(_TRACE_CLASSES)
        method = rng.choice(_TRACE_METHODS)
        frame = f"\tat {cls}.{method}({cls.rsplit('.', 1)[1]}.java:{rng.randint(40, 480)})"
        if frame != frames[-1]:
            frames.append(frame)
    return frames


def _service_events(rng: random.Random) -> tuple[str, dict[str, str], int]:
    """Return the log text, the task answers and the restart count."""
    target = TARGET_BYTES["service_events.txt"]

    # Stack traces: each trace's frames repeat identically after a timestamped
    # ERROR header, 3-8 times, at seeded positions in the log.
    occurrences: list[list[dict[str, Any]]] = []
    for _ in range(3):
        frames = _trace_frames(rng)
        service = rng.choice(_FILLER_SERVICES)
        for _ in range(rng.randint(3, 8)):
            occurrences.append(
                [
                    _entry(rng, "ERROR", service, "worker task failed"),
                    _raw(frames),
                ]
            )
    trace_bytes = sum(_entry_bytes(entry) for pair in occurrences for entry in pair)

    entries: list[dict[str, Any]] = []
    size = 0
    while size < target - trace_bytes:
        service = rng.choice(_FILLER_SERVICES)
        roll = rng.random()
        new: list[dict[str, Any]]
        if roll < 0.5:
            # Heartbeats: one line per entry, each with its own timestamp.
            new = [
                _entry(rng, "INFO", service, "heartbeat ok", gap=rng.randint(1, 5))
                for _ in range(rng.randint(12, 60))
            ]
        elif roll < 0.6:
            new = [
                _entry(
                    rng,
                    "INFO",
                    service,
                    f"batch {rng.randint(1000, 9999)} closed rows={rng.randint(10, 900)}",
                )
            ]
        elif roll < 0.7:
            new = [_entry(rng, "DEBUG", service, f"cache warm pass {rng.randint(1, 99)} ok")]
        elif roll < 0.8:
            code = f"W-{rng.randint(1000, 4999)}"
            new = [_entry(rng, "WARN", service, f"retry budget low code={code}")]
        elif roll < 0.9:
            new = [
                _entry(
                    rng,
                    "ERROR",
                    service,
                    f"upstream timeout after {rng.randint(5, 60)}s attempt={rng.randint(1, 4)}",
                )
            ]
        else:
            new = [_entry(rng, "INFO", service, "RESTART worker pool recycled")]
        for entry in new:
            entries.append(entry)
            size += _entry_bytes(entry)

    for pair in occurrences:
        position = rng.randrange(len(entries) + 1)
        entries[position:position] = pair

    # Planted events for the three tasks, placed at seeded positions.
    restart_count = rng.randint(2, 6)
    planted = [
        _entry(rng, "ERROR", _ERROR_SERVICE, "commit failed, retrying"),
        _entry(rng, "ERROR", _ERROR_SERVICE, "commit failed, giving up"),
        _entry(rng, "WARN", _WARN_SERVICE, "pool saturated code=W-6042"),
        _entry(rng, "WARN", _WARN_SERVICE, "backlog reached 5000 entries code=W-5183"),
    ]
    planted += [
        _entry(rng, "INFO", _RESTART_SERVICE, "RESTART process exited with status 1")
        for _ in range(restart_count)
    ]
    for entry in planted:
        entries.insert(rng.randrange(len(entries) + 1), entry)

    lines = _render_events(entries)
    # Timestamped lines are parsed; untimestamped stack-trace frames are skipped.
    parsed = [_LOG_LINE.match(line) for line in lines]

    first_error = next(
        match.group(1)
        for match in parsed
        if match and match.group(2) == "ERROR" and match.group(3) == _ERROR_SERVICE
    )
    backlog = [
        match
        for match in parsed
        if match
        and match.group(2) == "WARN"
        and match.group(3) == _WARN_SERVICE
        and "backlog" in match.group(4)
    ]
    assert len(backlog) == 1
    code = re.search(r"code=(W-\d+)", backlog[0].group(4))
    assert code is not None
    restarts = sum(
        1
        for match in parsed
        if match and match.group(3) == _RESTART_SERVICE and match.group(4).startswith("RESTART")
    )
    assert restarts == restart_count

    text = "\n".join([f"# {NOTICE}", *lines]) + "\n"
    answers = {
        "error": first_error,
        "warn": code.group(1),
    }
    return text, answers, restarts


# ------------------------------------------------------------------ platform config

_SERVICE_NAMES = [
    "auth-proxy",
    "billing-core",
    "cache-gateway",
    "edge-router",
    "ledger-sync",
    "mail-dispatch",
    "metrics-agent",
    "queue-relay",
    "report-builder",
    "search-indexer",
    "session-store",
    "upload-scanner",
    "webhook-fanout",
    "export-worker",
    "profile-api",
    "rate-guard",
    "audit-sink",
    "notify-hub",
]
_TEAMS = ["platform", "finance-systems", "data", "security", "operations"]
_FLAGS = [
    "strict_schema",
    "gzip_responses",
    "shadow_writes",
    "trace_sampling",
    "legacy_paths",
    "batch_commit",
    "warm_start",
    "dual_read",
    "rate_headers",
    "canary_routing",
]


def _service_config(rng: random.Random, name: str) -> dict[str, Any]:
    changelog = []
    for _ in range(rng.randint(4, 6)):
        changelog.append(
            {
                "version": f"{rng.randint(1, 4)}.{rng.randint(0, 19)}.{rng.randint(0, 99)}",
                "date": _date_2026(rng.randint(1, 8), rng.randint(1, 28)),
                "note": _sentence(rng),
            }
        )
    return {
        "owner_team": rng.choice(_TEAMS),
        "enabled": rng.random() < 0.9,
        "description": f"{name} service for {COMPANY} test operations. " + _sentence(rng),
        "listen": {"bind": "127.0.0.1", "port": rng.randint(7000, 9999), "tls": True},
        "limits": {
            "checkout": {
                "max_batch_items": rng.randint(150, 900),
                "timeout_ms": rng.choice([2500, 5000, 7500, 10000]),
            },
            "search": {"max_results": rng.randint(50, 500), "max_query_chars": 512},
            "upload": {"max_mb": rng.randint(5, 80), "concurrency": rng.randint(1, 8)},
        },
        "retry": {"attempts": rng.randint(1, 6), "backoff_ms": [100, 400, rng.randint(800, 3000)]},
        "features": {flag: rng.random() < 0.5 for flag in _FLAGS},
        "changelog": changelog,
    }


def _platform(rng: random.Random) -> tuple[str, int]:
    services = {name: _service_config(rng, name) for name in _SERVICE_NAMES}
    doc = {
        "_notice": NOTICE,
        "environment": "test-sandbox",
        "schema": 3,
        "services": services,
    }
    value = services["billing-core"]["limits"]["checkout"]["max_batch_items"]
    return _json_text(doc), value


# ------------------------------------------------------------------------ contract

_CLAUSES = [
    "Fees are invoiced monthly in arrears at a base rate of {fee} per consignment.",
    "Service credits of {pct} percent apply when delivery windows are missed in one {period}.",
    "Each party keeps records of shipments for {days} days after the invoice date.",
    "Claims for loss or damage must be lodged within {days} days of delivery.",
    "Insurance cover of {cover} is maintained for all loads in transit.",
    "Subcontracting needs prior written consent from the account manager.",
    "Confidential information is limited to named staff and is returned on request.",
]


def _contract(rng: random.Random) -> tuple[str, dict[str, Any]]:
    notice_days = rng.randint(30, 90)
    answer = {"kind": "number", "expected": str(notice_days), "tolerance": "0"}
    lines = [
        f"> {NOTICE}",
        "",
        f"# Contract Summary: {COMPANY} and Brackenmoor Pier Services",
        "",
        "## Parties",
        "",
        f"{COMPANY} (the client) and Brackenmoor Pier Services (the provider), both invented for testing.",
        "",
        "## Term and renewal",
        "",
        f"The agreement starts on 2026-0{rng.randint(1, 4)}-01 and renews automatically for successive "
        f"terms of {rng.choice([6, 12, 24])} months unless ended under the termination clause.",
        "",
        "## Termination",
        "",
        f"Written notice of termination must be given at least {notice_days} days before the end of the current term.",
        "",
        "## Service and fees",
        "",
    ]
    size = sum(len(line) + 1 for line in lines)
    goal = TARGET_BYTES["contract_summary.md"]
    while size < goal:
        clause = rng.choice(_CLAUSES).format(
            fee=f"{rng.randint(4, 40)}.{rng.randint(0, 99):02d} EUR",
            pct=rng.randint(2, 15),
            period=rng.choice(_PERIODS),
            days=rng.randint(7, 120),
            cover=f"{rng.randint(1, 9)} million EUR",
        )
        lines.append(clause)
        lines.append("")
        size += len(clause) + 2
    return "\n".join(lines) + "\n", answer


# ------------------------------------------------------------------- team contacts

_FIRST = [
    "Aiko", "Bram", "Carmen", "Dariusz", "Elif", "Farah", "Gideon", "Hana", "Ivo", "Jolene",
    "Kofi", "Lena", "Mateo", "Nadia", "Oskar", "Pilar", "Quinn", "Rafael", "Sana", "Tobias",
    "Uma", "Viktor", "Wren", "Xavier", "Yara", "Zeke",
]  # fmt: skip
_LAST = [
    "Abernathy", "Brandt", "Castellano", "Dvorak", "Eriksen", "Fujita", "Galloway", "Hadley",
    "Ishii", "Jansen", "Kowalczyk", "Lindqvist", "Moreau", "Nakamura", "Oduya", "Petrov",
    "Quintero", "Rasmussen", "Stroud", "Tanaka", "Underhill", "Vance", "Whitford", "Yilmaz",
    "Zapata",
]  # fmt: skip
_CONTACT_TEAMS = ["Operations", "Finance", "Safety", "Dispatch", "Data", "Carrier relations"]
_CONTACT_ROLES = [
    "Analyst",
    "Team lead",
    "Dispatcher",
    "Accounts clerk",
    "Planner",
    "Safety officer",
]
_OFFICES = ["Harbor Site", "Ridge Site", "Lake Site", "Delta Site", "Summit Site"]


def _contacts(rng: random.Random) -> tuple[str, int, str]:
    """Return the CSV text, the extension answer and the target's name."""
    names = rng.sample([(first, last) for first in _FIRST for last in _LAST], 40)
    extensions = rng.sample(range(4000, 8000), 40)
    rows = []
    for (first, last), extension in zip(names, extensions, strict=True):
        rows.append(
            [
                f"{first} {last}",
                rng.choice(_CONTACT_TEAMS),
                rng.choice(_CONTACT_ROLES),
                rng.choice(_OFFICES),
                str(extension),
                f"{first[0].lower()}.{last.lower()}@quorvale.example",
            ]
        )
    target = rng.randrange(len(rows))
    buffer = io.StringIO()
    buffer.write(f"# {NOTICE}\n")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["name", "team", "role", "office", "extension", "email"])
    writer.writerows(rows)
    return buffer.getvalue(), int(rows[target][4]), rows[target][0]


# --------------------------------------------------------------------------- pack


def _task(task_id: str, source: str, question: str, answer: dict[str, Any]) -> dict[str, Any]:
    return {"task_id": task_id, "source_files": [source], "question": question, "answer": answer}


def _pack(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "self_test_pack",
        "study_id": "st_generated_v1",
        "model_label": "Claude Opus, Medium effort",
        "minimums": {"paired_units": 10, "groups": 8},
        "quality_tolerance": 1,
        "groups": [
            {"group_id": f"g{index:02d}", "tasks": [task["task_id"]]}
            for index, task in enumerate(tasks, 1)
        ],
        "tasks": tasks,
    }


# ------------------------------------------------------------------------- driver


def build(seed: int) -> tuple[dict[str, str], dict[str, Any]]:
    """Return ``{file_name: text}`` and the pack, all derived from ``seed``."""
    rng = random.Random(seed)
    files: dict[str, str] = {}

    handbook_text, facts = _handbook(rng)
    files["operations_handbook.md"] = handbook_text

    orders_text, order_tasks = _orders(rng)
    files["orders_export.json"] = orders_text

    events_text, event_answers, restarts = _service_events(rng)
    files["service_events.txt"] = events_text

    config_text, config_value = _platform(rng)
    files["platform_config.json"] = config_text

    contract_text, contract_answer = _contract(rng)
    files["contract_summary.md"] = contract_text

    contacts_text, extension, person = _contacts(rng)
    files["team_contacts.csv"] = contacts_text

    tasks = [
        _task(
            "t_hb_dock_hold",
            "operations_handbook.md",
            facts["dock"]["question"],
            facts["dock"]["answer"],
        ),
        _task(
            "t_hb_quarantine_code",
            "operations_handbook.md",
            facts["quarantine"]["question"],
            facts["quarantine"]["answer"],
        ),
        _task(
            "t_hb_audit_freeze",
            "operations_handbook.md",
            facts["audit"]["question"],
            facts["audit"]["answer"],
        ),
        _task(
            "t_ord_unit_price",
            "orders_export.json",
            order_tasks["price"]["question"],
            order_tasks["price"]["answer"],
        ),
        _task(
            "t_ord_region",
            "orders_export.json",
            order_tasks["region"]["question"],
            order_tasks["region"]["answer"],
        ),
        _task(
            "t_ord_created",
            "orders_export.json",
            order_tasks["created"]["question"],
            order_tasks["created"]["answer"],
        ),
        _task(
            "t_log_first_error",
            "service_events.txt",
            f"In service_events.txt, what is the timestamp of the first ERROR line for service {_ERROR_SERVICE}? Answer with the timestamp exactly as written in the file.",
            {"kind": "text_contains", "expected": event_answers["error"]},
        ),
        _task(
            "t_log_warn_code",
            "service_events.txt",
            f"In service_events.txt, what code is on the WARN line for service {_WARN_SERVICE} that reports a backlog? Answer with the code only.",
            {"kind": "text_contains", "expected": event_answers["warn"]},
        ),
        _task(
            "t_log_restart_count",
            "service_events.txt",
            f"In service_events.txt, how many RESTART events are recorded for service {_RESTART_SERVICE}? Answer with the number only.",
            {"kind": "number", "expected": str(restarts), "tolerance": "0"},
        ),
        _task(
            "t_cfg_nested",
            "platform_config.json",
            "In platform_config.json, what is the value of services -> billing-core -> limits -> checkout -> max_batch_items? Answer with the number only.",
            {"kind": "number", "expected": str(config_value), "tolerance": "0"},
        ),
        _task(
            "t_contract_notice",
            "contract_summary.md",
            "In contract_summary.md, how many days before the end of the current term must written notice of termination be given? Answer with the number of days only.",
            contract_answer,
        ),
        _task(
            "t_contact_extension",
            "team_contacts.csv",
            f"In team_contacts.csv, what is the extension of {person}? Answer with the extension number only.",
            {"kind": "number", "expected": str(extension), "tolerance": "0"},
        ),
    ]
    return files, _pack(tasks)


def generate(out: Path, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Write the dataset into ``out`` and return the pack. Refuses a non-empty ``out``."""
    out = Path(out)
    if out.exists() and not out.is_dir():
        raise DatasetError("output path exists and is not a directory")
    if out.is_dir() and any(out.iterdir()):
        raise DatasetError("refusing to overwrite a non-empty directory")
    files, pack = build(seed)
    documents = out / "documents"
    documents.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (documents / name).write_bytes(text.encode("utf-8"))
    (out / "pack.json").write_bytes(_json_text(pack).encode("utf-8"))
    return pack


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", required=True, type=Path, help="output directory (must be empty or absent)"
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="integer seed")
    args = parser.parse_args(argv)
    try:
        generate(args.out, args.seed)
    except DatasetError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"wrote {args.out / 'documents'} and {args.out / 'pack.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
