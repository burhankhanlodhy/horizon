from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from horizon.desktop_mcp.broker import BrokerError
from horizon.desktop_mcp.contracts import tool_specs
from horizon.desktop_mcp.server import create_server
from horizon.desktop_mcp.settings import DesktopSettings

SOURCE_NAMES = [
    "contextshrink_search",
    "contextshrink_open",
    "contextshrink_retrieve",
    "contextshrink_stats",
]
COMPUTE_ONLY_NAMES = [
    "contextshrink_analyze",
    "contextshrink_render",
    "contextshrink_retrieve_batch",
]
COMPUTE_NAMES = [*SOURCE_NAMES, *COMPUTE_ONLY_NAMES]


class FakeComputeBackend:
    """Records dispatched calls with their exact arguments and returns a fixed payload."""

    def __init__(self, *, error: BaseException | None = None, payload: Any = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.error = error
        self.payload = payload

    def _answer(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        self.calls.append((name, args, kwargs))
        if self.error is not None:
            raise self.error
        if self.payload is not None:
            return self.payload
        return {"tool": name, "ok": True}

    def analyze(self, source_id, operation, *, result_token_budget=None):
        return self._answer(
            "analyze", (source_id, operation), {"result_token_budget": result_token_budget}
        )

    def render(self, source_id, *, fmt, title, operation=None, formula_policy="escape"):
        return self._answer(
            "render",
            (source_id,),
            {
                "fmt": fmt,
                "title": title,
                "operation": operation,
                "formula_policy": formula_policy,
            },
        )

    def retrieve_batch(self, requests, *, result_token_budget=None):
        return self._answer(
            "retrieve_batch", (requests,), {"result_token_budget": result_token_budget}
        )

    def search(self, query, limit=5):
        return {"hits": [], "hit_count": 0}

    def open(self, source, **kwargs):
        return None

    def retrieve(self, source_id, selector, **kwargs):
        return None

    def stats(self):
        return {}


def _compute_settings(**overrides: Any) -> DesktopSettings:
    return DesktopSettings(profile="local", tool_profile="compute", **overrides)


def _error_payload(result) -> dict[str, Any]:
    assert result.isError is True
    return json.loads(result.content[0].text)["error"]


def _analyze_args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "source_id": "cs1_example",
        "operation": {"group_by": ["region"], "aggregates": [{"op": "count", "as": "rows"}]},
    }
    args.update(overrides)
    return args


def _render_args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"source_id": "cs1_example", "format": "csv", "title": "Summary"}
    args.update(overrides)
    return args


def _batch_request(index: int = 0) -> dict[str, Any]:
    return {
        "source_id": f"cs1_example{index}",
        "selector": {"type": "rows", "start": 1, "end": 2},
    }


@pytest.mark.anyio
async def test_source_profile_lists_exactly_the_four_unchanged_tools() -> None:
    async with create_connected_server_and_client_session(
        create_server(DesktopSettings(profile="local"), FakeComputeBackend())
    ) as client:
        listed = await client.list_tools()

    assert [tool.name for tool in listed.tools] == SOURCE_NAMES
    assert [tool.model_dump(exclude_none=True) for tool in listed.tools] == tool_specs("source")


@pytest.mark.anyio
async def test_compute_profile_lists_seven_tools_in_order() -> None:
    async with create_connected_server_and_client_session(
        create_server(_compute_settings(), FakeComputeBackend())
    ) as client:
        listed = await client.list_tools()

    assert [tool.name for tool in listed.tools] == COMPUTE_NAMES
    assert [tool.model_dump(exclude_none=True) for tool in listed.tools] == tool_specs("compute")


@pytest.mark.anyio
async def test_compute_schemas_are_closed_objects_without_top_level_unions() -> None:
    async with create_connected_server_and_client_session(
        create_server(_compute_settings(), FakeComputeBackend())
    ) as client:
        listed = await client.list_tools()

    compute_tools = {tool.name: tool for tool in listed.tools if tool.name in COMPUTE_ONLY_NAMES}
    assert set(compute_tools) == set(COMPUTE_ONLY_NAMES)
    for tool in compute_tools.values():
        schema = tool.inputSchema
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert not {"oneOf", "anyOf", "allOf"} & set(schema)
        assert set(schema.get("required", [])) <= set(schema["properties"])

    analyze = compute_tools["contextshrink_analyze"].annotations
    assert analyze.readOnlyHint is True
    assert analyze.idempotentHint is True
    render = compute_tools["contextshrink_render"].annotations
    assert render.readOnlyHint is False
    assert render.destructiveHint is False
    assert render.idempotentHint is False
    assert render.openWorldHint is False
    assert compute_tools["contextshrink_retrieve_batch"].annotations.readOnlyHint is True
    operation_schema = compute_tools["contextshrink_analyze"].inputSchema["properties"]["operation"]
    assert operation_schema["type"] == "object"
    assert "JSON strings" in operation_schema["description"]


@pytest.mark.anyio
async def test_compute_tools_are_unknown_in_source_profile() -> None:
    backend = FakeComputeBackend()
    async with create_connected_server_and_client_session(
        create_server(DesktopSettings(profile="local"), backend)
    ) as client:
        responses = [
            await client.call_tool("contextshrink_analyze", _analyze_args()),
            await client.call_tool("contextshrink_render", _render_args()),
            await client.call_tool(
                "contextshrink_retrieve_batch", {"requests": [_batch_request()]}
            ),
        ]

    for response in responses:
        assert _error_payload(response)["code"] == "unknown_tool"
    assert backend.calls == []


@pytest.mark.anyio
async def test_compute_tools_dispatch_with_exact_kwargs() -> None:
    backend = FakeComputeBackend(payload={"tool": "fake", "text": "payload"})
    operation = {"group_by": ["region"], "aggregates": [{"op": "count", "as": "rows"}]}
    async with create_connected_server_and_client_session(
        create_server(_compute_settings(), backend)
    ) as client:
        analyze = await client.call_tool(
            "contextshrink_analyze", _analyze_args(result_token_budget=512)
        )
        render = await client.call_tool(
            "contextshrink_render",
            _render_args(operation={"select": ["region"]}, formula_policy="reject"),
        )
        batch = await client.call_tool(
            "contextshrink_retrieve_batch",
            {"requests": [_batch_request(0), _batch_request(1)], "result_token_budget": 256},
        )

    for response in (analyze, render, batch):
        assert response.isError is False
        assert json.loads(response.content[0].text) == {"tool": "fake", "text": "payload"}

    selector = {"type": "rows", "start": 1, "end": 2}
    assert backend.calls == [
        ("analyze", ("cs1_example", operation), {"result_token_budget": 512}),
        (
            "render",
            ("cs1_example",),
            {
                "fmt": "csv",
                "title": "Summary",
                "operation": {"select": ["region"]},
                "formula_policy": "reject",
            },
        ),
        (
            "retrieve_batch",
            (
                [
                    {"source_id": "cs1_example0", "selector": selector},
                    {"source_id": "cs1_example1", "selector": selector},
                ],
            ),
            {"result_token_budget": 256},
        ),
    ]


@pytest.mark.anyio
async def test_render_defaults_are_passed_explicitly() -> None:
    backend = FakeComputeBackend()
    async with create_connected_server_and_client_session(
        create_server(_compute_settings(), backend)
    ) as client:
        response = await client.call_tool("contextshrink_render", _render_args(format="json"))

    assert response.isError is False
    assert backend.calls == [
        (
            "render",
            ("cs1_example",),
            {"fmt": "json", "title": "Summary", "operation": None, "formula_policy": "escape"},
        )
    ]


@pytest.mark.anyio
async def test_broker_errors_map_to_error_results_with_code() -> None:
    backend = FakeComputeBackend(error=BrokerError("render_unavailable", {"detail": "x"}))
    async with create_connected_server_and_client_session(
        create_server(_compute_settings(), backend)
    ) as client:
        response = await client.call_tool("contextshrink_render", _render_args())

    assert _error_payload(response)["code"] == "render_unavailable"


@pytest.mark.anyio
async def test_invalid_compute_arguments_report_field_names_only() -> None:
    backend = FakeComputeBackend()
    cases = [
        ("contextshrink_render", _render_args(format="pdf"), "format"),
        ("contextshrink_render", _render_args(formula_policy="raw"), "formula_policy"),
        ("contextshrink_render", _render_args(title=""), "title"),
        ("contextshrink_render", _render_args(operation="select region"), "operation"),
        ("contextshrink_render", _render_args(SECRET_KEY="private"), "unknown_field"),
        (
            "contextshrink_retrieve_batch",
            {"requests": [_batch_request(i) for i in range(17)]},
            "requests",
        ),
        ("contextshrink_retrieve_batch", {"requests": []}, "requests"),
        ("contextshrink_analyze", _analyze_args(operation=["not", "object"]), "operation"),
        ("contextshrink_analyze", _analyze_args(result_token_budget=64), "result_token_budget"),
    ]
    async with create_connected_server_and_client_session(
        create_server(_compute_settings(), backend)
    ) as client:
        for name, args, field in cases:
            response = await client.call_tool(name, args)
            error = _error_payload(response)
            assert error["code"] == "invalid_arguments", name
            assert field in error["fields"], (name, error)
            assert "private" not in response.content[0].text
    assert backend.calls == []


@pytest.mark.anyio
async def test_oversized_compute_result_is_refused() -> None:
    backend = FakeComputeBackend(payload={"tool": "analyze", "text": "x" * 4096})
    async with create_connected_server_and_client_session(
        create_server(_compute_settings(max_result_bytes=1024), backend)
    ) as client:
        response = await client.call_tool("contextshrink_analyze", _analyze_args())

    assert _error_payload(response)["code"] == "result_too_large"


def test_instructions_mention_compute_only_when_enabled() -> None:
    source_text = create_server(DesktopSettings(profile="local"), FakeComputeBackend()).instructions
    compute_text = create_server(_compute_settings(), FakeComputeBackend()).instructions

    assert "manifest" not in source_text
    assert "manifest" in compute_text
    assert "outside the chat" in compute_text
    assert compute_text.startswith(source_text)
