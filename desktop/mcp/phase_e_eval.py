"""Run a synthetic-only A/B evaluation of Phase E compute tools through the MCP SDK.

The raw arm uses the source profile (search, exact open pages, retrieve). The
optimized arm adds the compute profile (analyze, render, retrieve_batch) and an
artifact root. Both arms share one approved temp root of sources generated
deterministically by this script.

This captures SDK tool schemas and ``CallToolResult`` objects, but not stdio
JSON-RPC framing, Desktop-added context, or provider-hidden prompt material.
No model, network service, API key, or user source is used. Token counts are
estimates from ``TokenEstimator``, not Claude usage counts.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from decimal import Decimal, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import anyio

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from horizon.desktop_mcp.artifacts import ArtifactError  # noqa: E402
from horizon.desktop_mcp.broker import BrokerError  # noqa: E402
from horizon.desktop_mcp.local_backend import (  # noqa: E402
    LocalBackend,
    LocalBackendError,
)
from horizon.desktop_mcp.measurement import TokenEstimator  # noqa: E402
from horizon.desktop_mcp.server import create_server  # noqa: E402
from horizon.desktop_mcp.settings import DesktopSettings  # noqa: E402
from horizon.desktop_mcp.source_access import SourceReader  # noqa: E402

SALES_ROWS = 3000
REGIONS = ("central", "east", "north", "south", "west")
SALES_SEED = 20261014
FILTER_REGION = "east"
FILTER_MIN_AMOUNT = "1600.00"
NOTE_LINES = 1000
RANGES = ((1, 12), (143, 150), (287, 301), (498, 500), (733, 740), (981, 1000))
TINY_ROWS = (
    ("1", "north", "10.00"),
    ("2", "south", "20.50"),
    ("3", "east", "35.25"),
    ("4", "west", "4.75"),
    ("5", "central", "99.10"),
)
TINY_TARGET = "3"
SALES_HEADER = ("id", "region", "amount")
SHORT_REPLY = "Saved the filtered table as a CSV artifact; the render manifest names the file."
GROUP_OPERATION: dict[str, Any] = {
    "columns": {"amount": "decimal"},
    "group_by": ["region"],
    "aggregates": [{"op": "sum", "column": "amount", "as": "total"}],
    "order_by": [{"column": "region", "direction": "asc"}],
}
FILTER_OPERATION: dict[str, Any] = {
    "columns": {"amount": "decimal"},
    "where": {
        "op": "and",
        "args": [
            {"op": "eq", "column": "region", "value": FILTER_REGION},
            {"op": "ge", "column": "amount", "value": FILTER_MIN_AMOUNT},
        ],
    },
    "select": ["id", "region", "amount"],
}
TINY_OPERATION: dict[str, Any] = {
    "columns": {"amount": "decimal"},
    "where": {"op": "eq", "column": "id", "value": TINY_TARGET},
    "select": ["amount"],
}
TASK_IDS = (
    "1_grouped_exact_total",
    "2_full_table_deliverable",
    "3_six_exact_ranges",
    "4_tiny_no_gain_case",
)
TASK_DESCRIPTIONS = {
    "1_grouped_exact_total": (
        "Grouped exact sum of amount by region over a 3,000-row CSV. Raw pages every "
        "exact read; optimized runs one analyze over the whole source."
    ),
    "2_full_table_deliverable": (
        "Filtered CSV deliverable (east region, amount >= 1600.00). Raw reads every "
        "page and counts the table it would emit as output; optimized renders one "
        "artifact and counts its manifest plus a fixed short reply."
    ),
    "3_six_exact_ranges": (
        "Six exact line ranges from a 1,000-line text source. Raw issues six retrieve "
        "calls; optimized issues one retrieve_batch call."
    ),
    "4_tiny_no_gain_case": (
        "One value from a 5-row CSV. Expected to favor the raw arm because the compute "
        "schemas are a fixed cost that a one-row answer cannot amortize."
    ),
}
_LCG_MASK = (1 << 64) - 1

_LIMITATIONS = [
    "Token counts are estimates from TokenEstimator (tiktoken o200k when available, else a UTF-8 byte heuristic), not Claude usage counts.",
    "Only SDK-serialized tool arguments and CallToolResult objects are counted; stdio JSON-RPC framing is not captured by the in-memory transport.",
    "Desktop-added context, system prompts, and provider-hidden prompt material are not visible and are not counted.",
    "estimated_model_output_tokens is a counterfactual for the table the model would have to write in the raw arm; no model was run.",
    "The final answer text is not counted in tasks 1, 3, and 4 for either arm; task 2 counts the table in the raw arm and a fixed short reply in the optimized arm.",
    "No USD, billing, or enterprise cost figures are reported.",
    "The raw arm reads exact pages at the default 2000-token page budget; a larger budget would reduce raw call counts.",
    "Source handles come from one contextshrink_search call per task in each arm; discovery is not free and is counted.",
    "Data is generated deterministically by this script; costs on real user sources will differ.",
    "Source handles, cursor tokens, and artifact ids are random per run, so token counts can shift by a few tokens between runs; answers do not.",
    "One run per arm with no timing or variance measurement; results are not a benchmark of model behavior.",
]


class _InProcessBrokerAdapter:
    """Map worker errors into the sanitized broker contract for this harness."""

    def __init__(self, backend: LocalBackend) -> None:
        self._backend = backend

    def __getattr__(self, name: str) -> Any:
        operation = getattr(self._backend, name)
        if not callable(operation):
            return operation

        def invoke(*args: Any, **kwargs: Any) -> Any:
            try:
                return operation(*args, **kwargs)
            except LocalBackendError as error:
                raise BrokerError(error.code, error.metadata) from None

        return invoke


def _lcg_values(seed: int, count: int) -> list[int]:
    state = seed
    values: list[int] = []
    for _ in range(count):
        state = (state * 6364136223846793005 + 1442695040888963407) & _LCG_MASK
        values.append(state >> 33)
    return values


def _cents_text(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _sales_rows() -> list[tuple[str, str, str]]:
    draws = _lcg_values(SALES_SEED, SALES_ROWS * 2)
    rows: list[tuple[str, str, str]] = []
    for index in range(SALES_ROWS):
        region = REGIONS[draws[2 * index] % len(REGIONS)]
        cents = 100 + draws[2 * index + 1] % 499900
        rows.append((f"{index + 1:05d}", region, _cents_text(cents)))
    return rows


def _csv_text(rows: list[tuple[str, ...]], header: tuple[str, ...]) -> str:
    # Synthetic values contain no commas or quotes; artifacts use CRLF terminators.
    lines = [",".join(header) + "\r\n"]
    lines.extend(",".join(row) + "\r\n" for row in rows)
    return "".join(lines)


def _group_totals(rows: list[tuple[str, str]]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    with localcontext() as context:
        context.prec = 60
        for region, amount in rows:
            totals[region] = totals.get(region, Decimal(0)) + Decimal(amount)
    return dict(sorted(totals.items()))


def _totals_match(answer: dict[str, Decimal], expected: dict[str, Decimal]) -> bool:
    return set(answer) == set(expected) and all(answer[key] == expected[key] for key in expected)


def _content_text(result: Any) -> str:
    return "\n".join(
        block.text for block in result.content if isinstance(getattr(block, "text", None), str)
    )


def _payload(result: Any) -> dict[str, Any]:
    try:
        payload = json.loads(_content_text(result))
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sdk_json(result: Any) -> str:
    # The SDK's typed result serialization, not its enclosing JSON-RPC frame.
    return _compact_json(result.model_dump(mode="json"))


class _Recorder:
    """Records every SDK call, grouped by task, with argument and result sizes."""

    def __init__(self, client: Any, estimator: TokenEstimator) -> None:
        self._client = client
        self._estimator = estimator
        self.calls: dict[str, list[dict[str, Any]]] = {task: [] for task in TASK_IDS}
        self.task = TASK_IDS[0]

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        result = await self._client.call_tool(tool, arguments)
        arg_json = _compact_json(arguments)
        sdk_json = _sdk_json(result)
        self.calls[self.task].append(
            {
                "tool": tool,
                "status": "error" if result.isError else "success",
                "is_error": bool(result.isError),
                "argument_utf8_bytes": len(arg_json.encode("utf-8")),
                "argument_tokens": self._estimator.count_text(arg_json),
                "result_utf8_bytes": len(sdk_json.encode("utf-8")),
                "result_tokens": self._estimator.count_text(sdk_json),
            }
        )
        return result


async def _search_handle(recorder: _Recorder, file_name: str) -> str:
    payload = _payload(
        await recorder.call("contextshrink_search", {"query": file_name, "limit": 5})
    )
    for hit in payload.get("hits", []):
        if isinstance(hit, dict) and hit.get("name") == file_name:
            source_id = hit.get("source_id")
            if isinstance(source_id, str):
                return source_id
    raise RuntimeError("eval_source_handle_missing")


async def _read_all_pages(recorder: _Recorder, source_id: str) -> str:
    """Page through every exact open result; returns the concatenated source text."""
    parts: list[str] = []
    payload = _payload(
        await recorder.call("contextshrink_open", {"source": source_id, "mode": "exact"})
    )
    for _ in range(1000):
        text = payload.get("text")
        if not isinstance(text, str):
            return ""
        parts.append(text)
        if payload.get("more_available") is not True:
            break
        payload = _payload(
            await recorder.call(
                "contextshrink_open",
                {"source": source_id, "mode": "exact", "cursor": payload["next_cursor"]},
            )
        )
    return "".join(parts)


def _parse_sales_text(text: str) -> list[tuple[str, str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    return [(row["id"], row["region"], row["amount"]) for row in reader]


def _task_summary(
    calls: list[dict[str, Any]], estimated_model_output_tokens: int
) -> dict[str, Any]:
    argument_tokens = sum(call["argument_tokens"] for call in calls)
    result_tokens = sum(call["result_tokens"] for call in calls)
    tools: dict[str, int] = {}
    for call in calls:
        tools[call["tool"]] = tools.get(call["tool"], 0) + 1
    return {
        "calls": len(calls),
        "calls_by_tool": dict(sorted(tools.items())),
        "error_call_count": sum(call["is_error"] for call in calls),
        "argument_tokens": argument_tokens,
        "result_tokens": result_tokens,
        "estimated_model_output_tokens": estimated_model_output_tokens,
        "total": argument_tokens + result_tokens + estimated_model_output_tokens,
    }


async def _run_arm(
    arm: str,
    sources_dir: Path,
    artifacts_dir: Path,
    expected: dict[str, Any],
    estimator: TokenEstimator,
) -> dict[str, Any]:
    common: dict[str, Any] = {
        "profile": "local",
        "approved_roots": (sources_dir,),
        "eval_arm": arm,
        "result_token_budget": 2000,
        "max_result_bytes": 65536,
    }
    if arm == "optimized":
        settings = DesktopSettings(tool_profile="compute", artifact_root=artifacts_dir, **common)
    else:
        settings = DesktopSettings(tool_profile="source", **common)
    backend = LocalBackend(
        SourceReader((sources_dir,)),
        result_token_budget=settings.result_token_budget,
        max_result_bytes=settings.max_result_bytes,
        counting_model=settings.counting_model,
        eval_arm=arm,
        min_compaction_gain_tokens=settings.min_compaction_gain_tokens,
        min_compaction_gain_percent=settings.min_compaction_gain_percent,
        artifact_root=artifacts_dir if arm == "optimized" else None,
    )
    server = create_server(settings, _InProcessBrokerAdapter(backend))
    options = server.create_initialization_options()
    instruction_text = options.instructions or ""
    answers: dict[str, bool] = {}
    per_task: dict[str, dict[str, Any]] = {}
    try:
        async with create_connected_server_and_client_session(server) as client:
            listed = await client.list_tools()
            schema_payload = [tool.model_dump(mode="json") for tool in listed.tools]
            schema_text = _compact_json(schema_payload)
            recorder = _Recorder(client, estimator)

            # Task 1: grouped exact total.
            recorder.task = TASK_IDS[0]
            sales_name = "sales.csv"
            if arm == "raw":
                sid = await _search_handle(recorder, sales_name)
                text = await _read_all_pages(recorder, sid)
                rows = _parse_sales_text(text)
                answer = _group_totals([(region, amount) for _, region, amount in rows])
            else:
                sid = await _search_handle(recorder, sales_name)
                payload = _payload(
                    await recorder.call(
                        "contextshrink_analyze",
                        {"source_id": sid, "operation": GROUP_OPERATION},
                    )
                )
                answer = {}
                for row in payload.get("rows", []):
                    answer[row[0]] = Decimal(row[1])
            answers[TASK_IDS[0]] = _totals_match(answer, expected["group_totals"])
            per_task[TASK_IDS[0]] = _task_summary(recorder.calls[TASK_IDS[0]], 0)

            # Task 2: filtered CSV deliverable.
            recorder.task = TASK_IDS[1]
            sid = await _search_handle(recorder, sales_name)
            if arm == "raw":
                text = await _read_all_pages(recorder, sid)
                filtered = [
                    list(row)
                    for row in _parse_sales_text(text)
                    if row[1] == FILTER_REGION and Decimal(row[2]) >= Decimal(FILTER_MIN_AMOUNT)
                ]
                answers[TASK_IDS[1]] = filtered == expected["filtered_rows"]
                output_tokens = estimator.count_text(expected["filtered_csv"])
            else:
                payload = _payload(
                    await recorder.call(
                        "contextshrink_render",
                        {
                            "source_id": sid,
                            "format": "csv",
                            "title": "East orders at least 1600.00",
                            "operation": FILTER_OPERATION,
                        },
                    )
                )
                artifact_id = payload.get("artifact", {}).get("artifact_id")
                matched = False
                store = backend.artifact_store
                if isinstance(artifact_id, str) and store is not None:
                    try:
                        data = store.read(artifact_id)
                        matched = data == expected["filtered_csv"].encode("utf-8")
                    except ArtifactError:
                        matched = False
                answers[TASK_IDS[1]] = matched
                output_tokens = estimator.count_text(SHORT_REPLY)
            per_task[TASK_IDS[1]] = _task_summary(recorder.calls[TASK_IDS[1]], output_tokens)

            # Task 3: six exact line ranges.
            recorder.task = TASK_IDS[2]
            notes_id = await _search_handle(recorder, "notes.txt")
            requests = [
                {"source_id": notes_id, "selector": {"type": "lines", "start": start, "end": end}}
                for start, end in RANGES
            ]
            if arm == "raw":
                texts: list[str] = []
                for request in requests:
                    payload = _payload(await recorder.call("contextshrink_retrieve", request))
                    texts.append(
                        payload.get("text") if isinstance(payload.get("text"), str) else ""
                    )
                answers[TASK_IDS[2]] = texts == expected["range_texts"]
            else:
                payload = _payload(
                    await recorder.call("contextshrink_retrieve_batch", {"requests": requests})
                )
                answers[TASK_IDS[2]] = _batch_texts_match(payload, expected["range_texts"])
            per_task[TASK_IDS[2]] = _task_summary(recorder.calls[TASK_IDS[2]], 0)

            # Task 4: one value from a tiny source.
            recorder.task = TASK_IDS[3]
            tiny_id = await _search_handle(recorder, "tiny.csv")
            if arm == "raw":
                text = await _read_all_pages(recorder, tiny_id)
                matches = [row for row in _parse_sales_text(text) if row[0] == TINY_TARGET]
                value = Decimal(matches[0][2]) if len(matches) == 1 else None
                answers[TASK_IDS[3]] = value == expected["tiny_value"]
            else:
                payload = _payload(
                    await recorder.call(
                        "contextshrink_analyze",
                        {"source_id": tiny_id, "operation": TINY_OPERATION},
                    )
                )
                rows = payload.get("rows", [])
                value = Decimal(rows[0][0]) if len(rows) == 1 else None
                answers[TASK_IDS[3]] = value == expected["tiny_value"]
            per_task[TASK_IDS[3]] = _task_summary(recorder.calls[TASK_IDS[3]], 0)
    finally:
        backend.close()

    return {
        "tool_names": [tool.name for tool in listed.tools],
        "tool_schema_utf8_bytes": len(_compact_json(schema_payload).encode("utf-8")),
        "tool_schema_token_estimate": estimator.count_text(schema_text),
        "server_instructions_utf8_bytes": len(instruction_text.encode("utf-8")),
        "server_instructions_token_estimate": estimator.count_text(instruction_text),
        "enabled_tool_count": len(listed.tools),
        "answers_match": answers,
        "tasks": per_task,
    }


def _batch_texts_match(payload: dict[str, Any], expected_texts: list[str]) -> bool:
    """Check each request's exact lines inside the fused blocks, in request order."""
    if payload.get("deferred") or payload.get("errors"):
        return False
    blocks = payload.get("blocks", [])
    found: list[str | None] = [None] * len(expected_texts)
    for block in blocks:
        block_start = block["line_ranges"][0]["start_line"]
        block_lines = str(block["text"]).splitlines(keepends=True)
        for scope in block["scopes"]:
            index = scope["index"]
            begin = scope["start_line"] - block_start
            end = scope["end_line"] - block_start + 1
            found[index] = "".join(block_lines[begin:end])
    return found == expected_texts


def _expected_answers(
    sales: list[tuple[str, str, str]], notes_text: str, tiny: tuple[tuple[str, str, str], ...]
) -> dict[str, Any]:
    note_lines = notes_text.splitlines(keepends=True)
    filtered = [
        [row_id, region, amount]
        for row_id, region, amount in sales
        if region == FILTER_REGION and Decimal(amount) >= Decimal(FILTER_MIN_AMOUNT)
    ]
    tiny_value = next(Decimal(amount) for row_id, _, amount in tiny if row_id == TINY_TARGET)
    return {
        "group_totals": _group_totals([(region, amount) for _, region, amount in sales]),
        "filtered_rows": filtered,
        "filtered_csv": _csv_text([tuple(row) for row in filtered], SALES_HEADER),
        "range_texts": ["".join(note_lines[start - 1 : end]) for start, end in RANGES],
        "tiny_value": tiny_value,
    }


def _write_sources(sources_dir: Path, sales: list[tuple[str, str, str]]) -> str:
    sources_dir.mkdir(parents=True, exist_ok=True)
    (sources_dir / "sales.csv").write_text(
        _csv_text(sales, SALES_HEADER), encoding="utf-8", newline=""
    )
    tiny_text = _csv_text(list(TINY_ROWS), SALES_HEADER)
    (sources_dir / "tiny.csv").write_text(tiny_text, encoding="utf-8", newline="")
    notes = "".join(
        f"note {number:04d}: synthetic line {(number * 37) % 997:03d} for range checks\n"
        for number in range(1, NOTE_LINES + 1)
    )
    (sources_dir / "notes.txt").write_text(notes, encoding="utf-8", newline="")
    return notes


def _build_report(
    arms: dict[str, Any], estimator: TokenEstimator, fixture_count: dict[str, int]
) -> dict[str, Any]:
    for arm in arms.values():
        surface_tokens = (
            arm["tool_schema_token_estimate"] + arm["server_instructions_token_estimate"]
        )
        arm["one_time_surface_token_estimate"] = surface_tokens
        arm["task_total_token_estimate"] = sum(task["total"] for task in arm["tasks"].values())
        arm["overall_total_token_estimate"] = surface_tokens + arm["task_total_token_estimate"]

    tasks: dict[str, Any] = {}
    for task_id in TASK_IDS:
        raw = arms["raw"]["tasks"][task_id]
        optimized = arms["optimized"]["tasks"][task_id]
        surface_difference = (
            arms["optimized"]["one_time_surface_token_estimate"]
            - arms["raw"]["one_time_surface_token_estimate"]
        )
        difference = optimized["total"] - raw["total"]
        tasks[task_id] = {
            "description": TASK_DESCRIPTIONS[task_id],
            "answers_match": {
                "raw": arms["raw"]["answers_match"][task_id],
                "optimized": arms["optimized"]["answers_match"][task_id],
            },
            "arms": {"raw": raw, "optimized": optimized},
            "optimized_minus_raw_total": difference,
            "larger_total_arm": _larger_arm(optimized["total"], raw["total"]),
            "optimized_minus_raw_including_one_time_surface": difference + surface_difference,
            "larger_total_arm_including_one_time_surface": _larger_arm(
                optimized["total"] + arms["optimized"]["one_time_surface_token_estimate"],
                raw["total"] + arms["raw"]["one_time_surface_token_estimate"],
            ),
        }

    answers_all = all(
        task["answers_match"]["raw"] and task["answers_match"]["optimized"]
        for task in tasks.values()
    )
    raw_overall = arms["raw"]["overall_total_token_estimate"]
    optimized_overall = arms["optimized"]["overall_total_token_estimate"]
    return {
        "schema_version": 1,
        "phase": "E",
        "check": "synthetic_mcp_sdk_ab_eval_compute",
        "status": "passed" if answers_all else "failed",
        "all_answers_match": answers_all,
        "fixture_provenance": "synthetic_generated_in_script",
        "fixture_counts": fixture_count,
        "execution": {
            "model_api_calls": 0,
            "network_calls": 0,
            "user_sources_read": False,
            "transport": "in_memory_mcp_python_sdk_session",
            "counting": estimator.provenance(),
        },
        "measurement_scope": {
            "arguments": "serialized tool arguments counted for every call",
            "sdk_result": "serialized typed CallToolResult objects counted for every call, including errors",
            "schemas_and_instructions": "captured once per arm and added once to that arm's overall total",
            "estimated_model_output_tokens": "counterfactual output the model would emit in the raw arm; fixed short reply in the optimized arm; zero where the final answer is excluded",
            "json_rpc_framing": "not captured by the in-memory transport",
            "stdio_protocol_bytes": "not measured",
            "host_added_context": "unknown",
            "provider_hidden_prompt_material": "unknown",
        },
        "cost_attribution": {
            "enterprise_billing": "not_measured",
            "usd_savings": "not_reported",
            "token_counts": "estimates with declared counter provenance; not Claude usage counts",
        },
        "arms": {
            arm_name: {
                "enabled_tool_count": arm["enabled_tool_count"],
                "tool_names": arm["tool_names"],
                "tool_schema_utf8_bytes": arm["tool_schema_utf8_bytes"],
                "tool_schema_token_estimate": arm["tool_schema_token_estimate"],
                "server_instructions_utf8_bytes": arm["server_instructions_utf8_bytes"],
                "server_instructions_token_estimate": arm["server_instructions_token_estimate"],
                "one_time_surface_token_estimate": arm["one_time_surface_token_estimate"],
                "task_total_token_estimate": arm["task_total_token_estimate"],
                "overall_total_token_estimate": arm["overall_total_token_estimate"],
            }
            for arm_name, arm in arms.items()
        },
        "overall": {
            "raw_overall_total_token_estimate": raw_overall,
            "optimized_overall_total_token_estimate": optimized_overall,
            "optimized_minus_raw": optimized_overall - raw_overall,
            "larger_overall_arm": _larger_arm(optimized_overall, raw_overall),
        },
        "tasks": tasks,
        "limitations": list(_LIMITATIONS),
    }


def _larger_arm(optimized: int, raw: int) -> str:
    if optimized == raw:
        return "equal"
    return "optimized" if optimized > raw else "raw"


async def _evaluate(
    sales: list[tuple[str, str, str]],
    notes_text: str,
    sources_dir: Path,
    artifacts_dir: Path,
    estimator: TokenEstimator,
) -> dict[str, Any]:
    expected = _expected_answers(sales, notes_text, TINY_ROWS)
    arms: dict[str, Any] = {}
    for arm in ("raw", "optimized"):
        arms[arm] = await _run_arm(arm, sources_dir, artifacts_dir, expected, estimator)
    return _build_report(
        arms,
        estimator,
        {
            "sales_rows": SALES_ROWS,
            "filtered_rows": len(expected["filtered_rows"]),
            "note_lines": NOTE_LINES,
            "tiny_rows": len(TINY_ROWS),
        },
    )


def run_phase_e_eval(output: Path | None = None) -> dict[str, Any]:
    """Evaluate synthetic sources through raw and optimized SDK sessions."""
    sales = _sales_rows()
    estimator = TokenEstimator()
    with TemporaryDirectory(prefix="contextshrink-phase-e-") as temp_dir:
        base = Path(temp_dir)
        sources_dir = base / "sources"
        artifacts_dir = base / "artifacts"
        artifacts_dir.mkdir()
        notes_text = _write_sources(sources_dir, sales)
        report = anyio.run(_evaluate, sales, notes_text, sources_dir, artifacts_dir, estimator)
    if output is not None:
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_phase_e_eval(args.output)
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["status"] == "passed" else 1)
