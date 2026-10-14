from __future__ import annotations

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from horizon.desktop_mcp import __main__ as desktop_cli
from horizon.desktop_mcp.backend import FixtureBackend
from horizon.desktop_mcp.server import create_server
from horizon.desktop_mcp.settings import DesktopSettings


@pytest.mark.anyio
async def test_real_sdk_protocol_lists_and_calls_all_four_tools() -> None:
    server = create_server()
    async with create_connected_server_and_client_session(server) as client:
        listed = await client.list_tools()
        assert [tool.name for tool in listed.tools] == [
            "contextshrink_search",
            "contextshrink_open",
            "contextshrink_retrieve",
            "contextshrink_stats",
        ]
        opened = await client.call_tool("contextshrink_open", {"source": "fixture:note"})
        assert opened.isError is False
        assert opened.structuredContent is None
        assert len(opened.content) == 1
        opened_data = json.loads(opened.content[0].text)
        assert opened_data["diagnostic_fixture"] is True
        assert opened_data["text"].startswith("# Synthetic note")
        assert opened_data["token_count"] is None

        catalog = await client.call_tool("contextshrink_search", {"query": ""})
        assert catalog.isError is False
        catalog_data = json.loads(catalog.content[0].text)
        assert {hit["source_id"] for hit in catalog_data["hits"]} == {
            "fixture:note",
            "fixture:log",
            "fixture:json",
        }
        assert catalog_data["coverage"] == "complete"

        limited = await client.call_tool("contextshrink_search", {"query": "", "limit": 1})
        limited_data = json.loads(limited.content[0].text)
        assert limited_data["hit_count"] == 3
        assert limited_data["returned_hit_count"] == 1
        assert limited_data["coverage"] == "partial (limit)"

        search = await client.call_tool("contextshrink_search", {"query": "synthetic"})
        assert search.isError is False
        assert json.loads(search.content[0].text)["hit_count"] >= 1

        retrieved = await client.call_tool(
            "contextshrink_retrieve",
            {"source_id": "fixture:log", "selector": {"type": "lines", "start": 2, "end": 2}},
        )
        assert retrieved.isError is False
        assert retrieved.structuredContent is None
        assert "WARN example event" in json.loads(retrieved.content[0].text)["text"]

        stats = await client.call_tool("contextshrink_stats", {})
        assert stats.isError is False
        assert stats.structuredContent is None
        stats_data = json.loads(stats.content[0].text)
        assert stats_data["compression_ratio"] is None
        assert stats_data["compression_applied"] is False


@pytest.mark.anyio
async def test_protocol_errors_are_sanitized_and_unsupported_modes_are_explicit() -> None:
    async with create_connected_server_and_client_session(create_server()) as client:
        invalid = await client.call_tool(
            "contextshrink_open",
            {"source": "private-secret-path", "mode": "raw"},
        )
        assert invalid.isError is True
        invalid_text = " ".join(block.text for block in invalid.content if hasattr(block, "text"))
        assert "private-secret-path" not in invalid_text
        assert "feature_unavailable" not in invalid_text  # schema rejects before dispatch

        compact = await client.call_tool(
            "contextshrink_open", {"source": "fixture:note", "mode": "compact"}
        )
        assert compact.isError is True
        assert "feature_unavailable" in compact.content[0].text

        missing = await client.call_tool("contextshrink_open", {"source": "C:/private/secret.txt"})
        assert missing.isError is True
        missing_text = missing.content[0].text
        assert "C:/private/secret.txt" not in missing_text
        assert "source_unavailable" in missing_text

        invalid_range = await client.call_tool(
            "contextshrink_retrieve",
            {"source_id": "fixture:log", "selector": {"type": "lines", "start": 1, "end": 99}},
        )
        assert invalid_range.isError is True
        assert "selector_out_of_range" in invalid_range.content[0].text


@pytest.mark.anyio
async def test_results_obey_configured_byte_limit() -> None:
    class OversizedSyntheticBackend(FixtureBackend):
        def open(self, source_id: str, *, start_line=None, end_line=None):
            result = super().open(source_id, start_line=start_line, end_line=end_line)
            if result is not None:
                result["text"] += "synthetic padding " * 200
            return result

    settings = DesktopSettings(max_result_bytes=1024)
    async with create_connected_server_and_client_session(
        create_server(settings, OversizedSyntheticBackend())
    ) as client:
        response = await client.call_tool("contextshrink_open", {"source": "fixture:note"})
        assert response.isError is True
        assert json.loads(response.content[0].text)["error"]["code"] == "result_too_large"


@pytest.mark.anyio
async def test_local_presentation_controls_are_forwarded_and_accounting_is_not_mutated() -> None:
    class LocalContractBackend:
        def __init__(self) -> None:
            self.open_calls = []
            self.retrieve_calls = []

        def open(self, source, *, mode="exact", query=None, result_token_budget=None, cursor=None):
            self.open_calls.append((source, mode, query, result_token_budget, cursor))
            return {
                "source_id": "cs1_example",
                "version": "v1",
                "mode": mode,
                "text": "bounded result",
                "accounting": {"delivered_payload_est_tokens": 42, "count_basis": "test"},
                "more_available": cursor is None,
                "next_cursor": "next-page" if cursor is None else None,
            }

        def retrieve(self, source_id, selector, *, result_token_budget=None, cursor=None):
            self.retrieve_calls.append((source_id, selector, result_token_budget, cursor))
            return {
                "source_id": source_id,
                "version": "v1",
                "selector": selector,
                "text": "exact excerpt",
                "accounting": {"delivered_payload_est_tokens": 24, "count_basis": "test"},
                "more_available": cursor is None,
            }

        def stats(self):
            return {}

        def search(self, query, limit=5):
            return {"hits": [], "hit_count": 0}

    backend = LocalContractBackend()
    settings = DesktopSettings(profile="local")
    async with create_connected_server_and_client_session(
        create_server(settings, backend)
    ) as client:
        for mode in ("exact", "compact", "focused", "overview"):
            response = await client.call_tool(
                "contextshrink_open",
                {
                    "source": "cs1_example",
                    "mode": mode,
                    "query": "renewal" if mode == "focused" else None,
                    "result_token_budget": 512,
                    "cursor": "cursor-token",
                },
            )
            assert response.isError is False
            assert response.structuredContent is None
            assert json.loads(response.content[0].text)["accounting"] == {
                "delivered_payload_est_tokens": 42,
                "count_basis": "test",
            }

        selectors = [
            {"type": "lines", "start": 1, "end": 2},
            {"type": "markdown_section", "heading": "Renewal"},
            {"type": "json_pointer", "pointer": "/items/0"},
            {"type": "rows", "start": 2, "end": 4},
        ]
        for selector in selectors:
            response = await client.call_tool(
                "contextshrink_retrieve",
                {
                    "source_id": "cs1_example",
                    "selector": selector,
                    "result_token_budget": 256,
                    "cursor": "retrieve-cursor",
                },
            )
            assert response.isError is False
            assert json.loads(response.content[0].text)["selector"]["type"] == selector["type"]

    assert len(backend.open_calls) == 4
    assert backend.open_calls[2] == ("cs1_example", "focused", "renewal", 512, "cursor-token")
    assert len(backend.retrieve_calls) == 4
    assert backend.retrieve_calls[1][1]["type"] == "markdown_section"
    assert backend.retrieve_calls[1][2:] == (256, "retrieve-cursor")


def test_cli_propagates_failed_doctor_status(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "horizon.desktop_mcp.capabilities.run_doctor",
        lambda _output=None: {"check": "doctor", "status": "missing_sdk"},
    )

    exit_code = desktop_cli.main(["doctor"])

    assert exit_code != 0
    assert json.loads(capsys.readouterr().out)["status"] == "missing_sdk"


def test_cli_sanitizes_report_write_failures(monkeypatch, capsys, tmp_path) -> None:
    def fail_write(_output=None):
        raise OSError("private path and OS detail")

    monkeypatch.setattr("horizon.desktop_mcp.capabilities.run_doctor", fail_write)
    private_path = tmp_path / "private" / "report.json"

    exit_code = desktop_cli.main(["doctor", "--output", str(private_path)])
    captured = capsys.readouterr()

    assert exit_code != 0
    assert captured.out == ""
    assert "report_write_failed" in captured.err
    assert str(private_path) not in captured.err
    assert "OS detail" not in captured.err


def test_probe_help_states_desktop_compatibility_is_not_verified(capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        desktop_cli.main(["probe", "--help"])

    assert caught.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "local MCP SDK stdio path" in help_text
    assert "does not verify Claude Desktop compatibility" in help_text
