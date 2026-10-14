"""Run a synthetic-only A/B evaluation through the MCP Python SDK.

This captures SDK tool schemas and ``CallToolResult`` objects, but not stdio
JSON-RPC framing, Desktop-added context, or provider-hidden prompt material.
No model, network service, API key, or user source is used.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import anyio

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from horizon.desktop_mcp.broker import BrokerError  # noqa: E402
from horizon.desktop_mcp.local_backend import (  # noqa: E402
    LocalBackend,  # noqa: E402
    LocalBackendError,  # noqa: E402
)  # noqa: E402
from horizon.desktop_mcp.measurement import TokenEstimator  # noqa: E402
from horizon.desktop_mcp.server import create_server  # noqa: E402
from horizon.desktop_mcp.settings import DesktopSettings  # noqa: E402
from horizon.desktop_mcp.source_access import SourceReader  # noqa: E402

_FIXTURES = Path(__file__).with_name("phase_c_fixtures.json")


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


def _json_bytes(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )


def _sdk_json(result: Any) -> str:
    # This is the SDK's typed result object serialization, not its enclosing
    # JSON-RPC response frame (which the in-memory transport does not expose).
    return json.dumps(
        result.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _content_text(result: Any) -> str:
    return "\n".join(
        block.text for block in result.content if isinstance(getattr(block, "text", None), str)
    )


def _parse_payload(result: Any) -> dict[str, Any]:
    try:
        payload = json.loads(_content_text(result))
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


async def _run_arm(
    arm: str, cases: list[dict[str, Any]], root: Path, estimator: TokenEstimator
) -> dict[str, Any]:
    settings = DesktopSettings(
        profile="local",
        approved_roots=(root,),
        eval_arm=arm,
        result_token_budget=2000,
        max_result_bytes=65536,
    )
    backend = LocalBackend(
        SourceReader((root,)),
        result_token_budget=settings.result_token_budget,
        max_result_bytes=settings.max_result_bytes,
        counting_model=settings.counting_model,
        eval_arm=arm,
        min_compaction_gain_tokens=settings.min_compaction_gain_tokens,
        min_compaction_gain_percent=settings.min_compaction_gain_percent,
    )
    server = create_server(settings, _InProcessBrokerAdapter(backend))
    options = server.create_initialization_options()
    instruction_text = options.instructions or ""
    calls: list[dict[str, Any]] = []
    quality_rows: list[dict[str, Any]] = []
    async with create_connected_server_and_client_session(server) as client:
        listed = await client.list_tools()
        tool_models = list(listed.tools)
        schema_payload = [tool.model_dump(mode="json") for tool in tool_models]
        schema_bytes = _json_bytes(schema_payload)
        schema_text = json.dumps(
            schema_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        schema_token_estimate = estimator.count_text(schema_text)
        instructions_bytes = len(instruction_text.encode("utf-8"))
        instructions_token_estimate = estimator.count_text(instruction_text)

        async def call(tool: str, arguments: dict[str, Any]) -> Any:
            result = await client.call_tool(tool, arguments)
            arg_json = json.dumps(
                arguments, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
            sdk_json = _sdk_json(result)
            inner_text = _content_text(result)
            calls.append(
                {
                    "tool": tool,
                    "status": "error" if result.isError else "success",
                    "argument_utf8_bytes": len(arg_json.encode("utf-8")),
                    "argument_token_estimate": estimator.count_text(arg_json),
                    "sdk_call_tool_result_utf8_bytes": len(sdk_json.encode("utf-8")),
                    "sdk_call_tool_result_token_estimate": estimator.count_text(sdk_json),
                    "inner_result_text_utf8_bytes": len(inner_text.encode("utf-8")),
                    "inner_result_text_token_estimate": estimator.count_text(inner_text),
                    "is_error": bool(result.isError),
                }
            )
            return result

        # One discovery call and one intentionally sanitized missing-source
        # error are part of each arm's call total.
        await call("contextshrink_search", {"query": "synthetic", "limit": 20})
        await call(
            "contextshrink_open", {"source": str(root / "outside-root-secret.txt"), "mode": "exact"}
        )

        for case in cases:
            filename = case["filename"]
            source_path = root / filename
            source_text = case["text"]
            requested = case.get("requested_mode", "focused")
            query = case.get("query")
            args: dict[str, Any] = {"source": str(source_path), "mode": requested}
            if requested == "focused":
                args["query"] = query
            opened = await call("contextshrink_open", args)
            opened_payload = _parse_payload(opened)
            displayed = (
                opened_payload.get("text")
                if isinstance(opened_payload.get("text"), str)
                else _content_text(opened)
            )
            required = case["required_literals"]
            recovery_targets = case.get("recovery_literals", [])
            expected_rendered = list(required)
            if arm == "optimized" and opened_payload.get("compression_applied") is True:
                expected_rendered.extend(case.get("optimized_literals", []))
            missing_open = [literal for literal in expected_rendered if literal not in displayed]
            recovered: list[str] = []
            recovery_targets_found: list[str] = [
                literal for literal in recovery_targets if literal in displayed
            ]

            # Exact recovery check: request the tight line span containing all
            # required literals. This validates focused-result omissions can
            # be recovered by immutable line selectors without claiming that
            # the focused view itself was lossless.
            if requested == "focused" and opened_payload.get("source_id"):
                lines = source_text.splitlines()
                missing_targets = [
                    literal for literal in expected_rendered if literal not in displayed
                ]
                missing_targets.extend(
                    literal for literal in recovery_targets if literal not in displayed
                )
                matching_lines = [
                    index + 1
                    for index, line in enumerate(lines)
                    if any(lit in line for lit in missing_targets)
                ]
                if matching_lines:
                    retrieved = await call(
                        "contextshrink_retrieve",
                        {
                            "source_id": opened_payload["source_id"],
                            "selector": {
                                "type": "lines",
                                "start": min(matching_lines),
                                "end": max(matching_lines),
                            },
                        },
                    )
                    retrieved_text = _parse_payload(retrieved).get("text", "")
                    if isinstance(retrieved_text, str):
                        recovered = [
                            literal for literal in missing_open if literal in retrieved_text
                        ]
                        recovery_targets_found = [
                            literal
                            for literal in recovery_targets_found
                            if literal in displayed or literal in retrieved_text
                        ] + [
                            literal
                            for literal in recovery_targets
                            if literal in retrieved_text and literal not in recovery_targets_found
                        ]

            missing_final = [
                literal
                for literal in expected_rendered
                if literal not in displayed and literal not in recovered
            ]
            accounting = opened_payload.get("accounting", {})
            quality_rows.append(
                {
                    "case_id": case["id"],
                    "requested_mode": requested,
                    "effective_mode": opened_payload.get("mode", "unknown"),
                    "fidelity": opened_payload.get("fidelity", "unknown"),
                    "coverage": opened_payload.get("coverage", "unknown"),
                    "required_literal_count": len(expected_rendered),
                    "missing_from_open_count": len(missing_open),
                    "exactly_recovered_literal_count": len(recovered),
                    "missing_after_recovery_count": len(missing_final),
                    "recovery_target_count": len(recovery_targets),
                    "recovery_targets_found_count": len(recovery_targets_found),
                    "compression_applied": bool(opened_payload.get("compression_applied", False)),
                    "full_inner_result_accounting": accounting.get("result")
                    if isinstance(accounting, dict)
                    else None,
                }
            )

    return {
        "arm": arm,
        "tool_schemas": schema_payload,
        "tool_schema_utf8_bytes": schema_bytes,
        "tool_schema_token_estimate": schema_token_estimate,
        "server_instructions_utf8_bytes": instructions_bytes,
        "server_instructions_token_estimate": instructions_token_estimate,
        "enabled_tool_count": len(tool_models),
        "calls": calls,
        "quality": quality_rows,
        "totals": {
            "call_count": len(calls),
            "argument_utf8_bytes": sum(call["argument_utf8_bytes"] for call in calls),
            "argument_token_estimate": sum(call["argument_token_estimate"] for call in calls),
            "sdk_call_tool_result_utf8_bytes": sum(
                call["sdk_call_tool_result_utf8_bytes"] for call in calls
            ),
            "sdk_call_tool_result_token_estimate": sum(
                call["sdk_call_tool_result_token_estimate"] for call in calls
            ),
            "inner_result_text_utf8_bytes": sum(
                call["inner_result_text_utf8_bytes"] for call in calls
            ),
            "inner_result_text_token_estimate": sum(
                call["inner_result_text_token_estimate"] for call in calls
            ),
            "error_call_count": sum(call["is_error"] for call in calls),
            "observed_surface_token_estimate_without_inner_duplicate": (
                schema_token_estimate
                + instructions_token_estimate
                + sum(call["argument_token_estimate"] for call in calls)
                + sum(call["sdk_call_tool_result_token_estimate"] for call in calls)
            ),
            "observed_surface_utf8_bytes_without_inner_duplicate": (
                schema_bytes
                + instructions_bytes
                + sum(call["argument_utf8_bytes"] for call in calls)
                + sum(call["sdk_call_tool_result_utf8_bytes"] for call in calls)
            ),
            "counterfactual_per_call_raw_equivalent_inner_tokens": sum(
                (row.get("full_inner_result_accounting") or {}).get(
                    "raw_equivalent_token_estimate", 0
                )
                for row in quality_rows
            ),
        },
    }


async def _evaluate(
    cases: list[dict[str, Any]], root: Path, estimator: TokenEstimator
) -> dict[str, Any]:
    arms: dict[str, Any] = {}
    for arm in ("raw", "optimized"):
        arms[arm] = await _run_arm(arm, cases, root, estimator)
    # Both arms use the same schema list and instructions; count them once per
    # arm in task estimates. Their UTF-8 sizes are separately visible.
    for arm in arms.values():
        arm["totals"]["one_time_schema_and_instructions_utf8_bytes"] = (
            arm["tool_schema_utf8_bytes"] + arm["server_instructions_utf8_bytes"]
        )
        arm["totals"]["one_time_schema_and_instructions_token_estimate"] = (
            arm["tool_schema_token_estimate"] + arm["server_instructions_token_estimate"]
        )

    expected = all(
        row["missing_after_recovery_count"] == 0
        and row["recovery_targets_found_count"] == row["recovery_target_count"]
        for data in arms.values()
        for row in data["quality"]
    )
    return {
        "schema_version": 2,
        "phase": "C",
        "check": "synthetic_mcp_sdk_ab_eval",
        "status": "passed" if expected else "failed",
        "fixture_provenance": "synthetic_fixture_only",
        "execution": {
            "model_api_calls": 0,
            "network_calls": 0,
            "user_sources_read": False,
            "transport": "in_memory_mcp_python_sdk_session",
            "counting": estimator.provenance(),
        },
        "measurement_scope": {
            "sdk_result": "serialized typed CallToolResult objects counted for every call, including errors and exact recovery calls",
            "arguments": "serialized tool arguments counted for every call",
            "schemas_and_instructions": "captured once per arm and included as fixed one-time estimate",
            "inner_result": "TextContent.text counted separately; per-result source/full-result accounting is nested in opened results",
            "json_rpc_framing": "not captured by the in-memory transport",
            "stdio_protocol_bytes": "not measured",
            "host_added_context": "unknown",
            "provider_hidden_prompt_material": "unknown",
        },
        "cost_attribution": {
            "enterprise_billing": "not_measured",
            "usd_savings": "not_reported",
            "token_counts": "estimates with declared counter provenance; not Claude usage counts",
            "raw_equivalent": "per-call counterfactual baseline, not task totals or measured saved tokens",
        },
        "claims": {
            "universal_positive_savings": False,
            "quality_gate": "required literals are present in the open result or recoverable by an exact line selector; focused/overview coverage may remain partial",
            "quality_interpretation": "deterministic fixture coverage checks, not model-answer faithfulness or a blind score",
        },
        "arms": arms,
    }


def run_phase_c_eval(output: Path | None = None) -> dict[str, Any]:
    """Evaluate synthetic fixtures through raw and optimized SDK sessions."""
    fixture_data = json.loads(_FIXTURES.read_text(encoding="utf-8"))
    cases = fixture_data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise RuntimeError("phase_c_fixtures_invalid")
    estimator = TokenEstimator()
    with TemporaryDirectory(prefix="contextshrink-phase-c-") as temp_dir:
        root = Path(temp_dir)
        for case in cases:
            filename = case.get("filename")
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise RuntimeError("phase_c_fixture_case_invalid")
            (root / filename).write_text(case["text"], encoding="utf-8", newline="")
        report = anyio.run(_evaluate, cases, root, estimator)
    report["fixture_case_count"] = len(cases)
    report["fixture_file_provenance"] = fixture_data.get("provenance", "unknown")
    if output is not None:
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(json.dumps(run_phase_c_eval(args.output), indent=2, sort_keys=True))
