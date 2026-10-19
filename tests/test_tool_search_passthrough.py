"""Evidence for ``ENABLE_TOOL_SEARCH`` behind the hosted proxy.

When Claude Code defers its own tools (``ENABLE_TOOL_SEARCH=true``) and talks to
the first-party Anthropic API, the proxy must leave the deferred shape alone:
the tools list, the ``defer_loading`` marks, and the tool-search beta token all
reach the upstream unchanged. These tests pin the helper decisions the proxy
makes on that path (``horizon/proxy/handlers/anthropic.py`` around the
``HORIZON_TOOL_SEARCH`` block).
"""

from __future__ import annotations

import copy

from horizon.proxy.helpers import (
    anthropic_first_party_tool_search_supported,
    inject_tool_search_deferral,
    request_already_defers_tools,
    strip_first_party_tool_search_tools_for_third_party_upstream,
)

FIRST_PARTY = "https://api.anthropic.com"


def _client_deferred_tools() -> list[dict]:
    return [
        {
            "type": "tool_search_tool_regex_20251119",
            "name": "tool_search_tool_regex",
        },
        {
            "name": "mcp__github__create_issue",
            "description": "Create an issue",
            "input_schema": {"type": "object", "properties": {}},
            "defer_loading": True,
        },
        {
            "name": "Read",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]


def test_first_party_upstream_supports_server_side_tool_search() -> None:
    assert anthropic_first_party_tool_search_supported(FIRST_PARTY)
    assert not anthropic_first_party_tool_search_supported("https://gateway.example.test")


def test_client_deferral_is_recognised_so_the_proxy_stands_down() -> None:
    assert request_already_defers_tools(_client_deferred_tools())


def test_client_deferred_tools_pass_the_proxy_unchanged_on_first_party() -> None:
    tools = _client_deferred_tools()
    original = copy.deepcopy(tools)

    # The proxy's own deferral injector stands down when the client already defers.
    assert inject_tool_search_deferral(tools) is tools
    assert tools == original

    # The third-party strip is a no-op on first-party Anthropic.
    stripped = strip_first_party_tool_search_tools_for_third_party_upstream(tools, FIRST_PARTY)
    assert stripped is tools
    assert tools == original


def test_third_party_upstream_would_strip_the_deferred_shape() -> None:
    """Why ``ENABLE_TOOL_SEARCH`` is only safe behind a first-party route."""
    tools = _client_deferred_tools()
    stripped = strip_first_party_tool_search_tools_for_third_party_upstream(
        tools, "https://gateway.example.test"
    )
    assert all("defer_loading" not in t for t in stripped)
    assert all(t.get("type") != "tool_search_tool_regex_20251119" for t in stripped)
