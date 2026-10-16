import pytest

from horizon.desktop_mcp.contracts import (
    ToolInputError,
    json_text,
    tool_specs,
    validate_tool_arguments,
)


def test_tool_specs_are_four_stable_read_only_contracts() -> None:
    specs = tool_specs()
    assert [spec["name"] for spec in specs] == [
        "contextshrink_search",
        "contextshrink_open",
        "contextshrink_retrieve",
        "contextshrink_stats",
    ]
    assert all(spec["annotations"]["readOnlyHint"] is True for spec in specs)
    assert all(spec["inputSchema"]["additionalProperties"] is False for spec in specs)
    specs[0]["name"] = "mutated"
    assert tool_specs()[0]["name"] == "contextshrink_search"


def test_validate_search_defaults_and_strict_bounds() -> None:
    assert validate_tool_arguments("contextshrink_search", {}) == {"query": "", "limit": 5}
    assert validate_tool_arguments("contextshrink_search", {"query": "docs", "limit": 20}) == {
        "query": "docs",
        "limit": 20,
    }
    for args in ({"limit": True}, {"limit": 0}, {"unexpected": 1}, {"query": "x" * 4097}):
        with pytest.raises(ToolInputError) as caught:
            validate_tool_arguments("contextshrink_search", args)
        assert caught.value.code == "invalid_arguments"
        assert "x" * 20 not in str(caught.value)


def test_open_rejects_wrong_mode_budget_and_overlong_source() -> None:
    assert validate_tool_arguments("contextshrink_open", {"source": "doc"}) == {
        "source": "doc",
        "mode": "exact",
        "result_token_budget": None,
        "query": None,
        "cursor": None,
        "context_lines": None,
    }
    for args in (
        {"source": "doc", "mode": "raw"},
        {"source": "doc", "result_token_budget": 127},
        {"source": "doc", "result_token_budget": True},
        {"source": "doc", "cursor": "c" * 257},
        {"source": "s" * 4097},
    ):
        with pytest.raises(ToolInputError):
            validate_tool_arguments("contextshrink_open", args)


def test_retrieve_requires_valid_tagged_ordered_lines() -> None:
    base = {"source_id": "src", "selector": {"type": "lines", "start": 2, "end": 8}}
    assert validate_tool_arguments("contextshrink_retrieve", base)["selector"] == base["selector"]
    for selector in (
        {"type": "lines", "start": 0, "end": 2},
        {"type": "lines", "start": 3, "end": 2},
        {"type": "other", "start": 1, "end": 2},
        {"type": "lines", "start": True, "end": 2},
        {"type": "lines", "start": 1, "end": 2, "path": "secret"},
    ):
        with pytest.raises(ToolInputError) as caught:
            validate_tool_arguments(
                "contextshrink_retrieve", {"source_id": "src", "selector": selector}
            )
        assert caught.value.code == "invalid_arguments"
        assert "secret" not in str(caught.value)


def test_retrieve_selector_union_is_strict_and_bounded() -> None:
    cases = [
        (
            {"type": "markdown_section", "heading": "Renewal"},
            {"type": "markdown_section", "heading": "Renewal", "occurrence": 1},
        ),
        (
            {"type": "json_pointer", "pointer": "/items/0/name"},
            {"type": "json_pointer", "pointer": "/items/0/name"},
        ),
        (
            {"type": "rows", "start": 2, "end": 8},
            {"type": "rows", "start": 2, "end": 8},
        ),
        (
            {"type": "json_pointer", "pointer": ""},
            {"type": "json_pointer", "pointer": ""},
        ),
    ]
    for selector, expected in cases:
        got = validate_tool_arguments(
            "contextshrink_retrieve", {"source_id": "cs1_example", "selector": selector}
        )
        assert got["selector"] == expected

    for selector in (
        {"type": "markdown_section", "heading": " "},
        {"type": "markdown_section", "heading": "x", "occurrence": 101},
        {"type": "json_pointer", "pointer": "items/0"},
        {"type": "json_pointer", "pointer": "/bad~2escape"},
        {"type": "rows", "start": 1, "end": 200002},
        {"type": "rows", "start": 1, "end": 2, "delimiter": "tsv"},
    ):
        with pytest.raises(ToolInputError):
            validate_tool_arguments(
                "contextshrink_retrieve", {"source_id": "cs1_example", "selector": selector}
            )


def test_stats_accepts_only_empty_object_and_unknown_tool_is_sanitized() -> None:
    assert validate_tool_arguments("contextshrink_stats", {}) == {}
    with pytest.raises(ToolInputError):
        validate_tool_arguments("contextshrink_stats", {"token": "private"})
    with pytest.raises(ToolInputError) as caught:
        validate_tool_arguments("contextshrink_private", {"value": "private"})
    assert caught.value.code == "unknown_tool"
    assert "private" not in str(caught.value)


def test_unknown_argument_key_is_not_reflected_in_error_fields() -> None:
    with pytest.raises(ToolInputError) as caught:
        validate_tool_arguments("contextshrink_search", {"SECRET_ARGUMENT_KEY": "private"})
    assert caught.value.fields == ("unknown_field",)
    assert "SECRET_ARGUMENT_KEY" not in caught.value.fields
    assert "SECRET_ARGUMENT_KEY" not in str(caught.value)


def test_json_text_is_compact_and_deterministic() -> None:
    assert json_text({"z": 1, "a": "é"}) == '{"a":"é","z":1}'


def test_open_result_token_budget_accepts_64000_and_rejects_64001() -> None:
    accepted = validate_tool_arguments(
        "contextshrink_open", {"source": "C:/approved/a.txt", "result_token_budget": 64000}
    )
    assert accepted["result_token_budget"] == 64000
    with pytest.raises(ToolInputError):
        validate_tool_arguments(
            "contextshrink_open", {"source": "C:/approved/a.txt", "result_token_budget": 64001}
        )
