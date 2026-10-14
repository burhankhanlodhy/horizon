from __future__ import annotations

import copy
import decimal
from decimal import Decimal

import pytest

from horizon.desktop_mcp import operations
from horizon.desktop_mcp.operations import (
    OPERATIONS_VERSION,
    OperationError,
    run_operation,
    validate_spec,
)
from horizon.desktop_mcp.tables import Row, Table, parse_table


def _table(columns: list[str], rows: list[list[str | None]]) -> Table:
    return Table(
        kind="csv",
        columns=tuple(columns),
        rows=tuple(
            Row(
                ref={"type": "rows", "start": i + 2, "end": i + 2},
                line_start=i + 2,
                line_end=i + 2,
                values=tuple(values),
            )
            for i, values in enumerate(rows)
        ),
    )


def _code(fn, *args) -> str:
    with pytest.raises(OperationError) as info:
        fn(*args)
    return info.value.code


def _sales() -> Table:
    return _table(
        ["region", "amount", "date", "note"],
        [
            ["north", "10.10", "2026-01-05", "alpha"],
            ["north", "2.20", "2026-02-01", "Beta"],
            ["south", "5", "2026-03-09", "gamma"],
            ["south", "", "2025-12-31", None],
            ["east", "7.5", "2026-01-20", "delta"],
        ],
    )


TYPES = {"amount": "decimal", "date": "date"}


def _lineage_adds_up(result) -> None:
    lin = result.lineage
    assert lin["scanned"] == lin["source_rows"]
    assert lin["matched"] + sum(lin["excluded"].values()) + lin["not_matched"] == lin["scanned"]


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "value", "expected_amounts"),
    [
        ("eq", "5", ["5"]),
        ("ne", "5", ["10.10", "2.20", "7.5"]),
        ("lt", "5", ["2.20"]),
        ("le", "5", ["2.20", "5"]),
        ("gt", "5", ["10.10", "7.5"]),
        ("ge", "5", ["10.10", "5", "7.5"]),
    ],
)
def test_comparison_predicates_on_decimal(op: str, value: str, expected_amounts: list[str]) -> None:
    spec = {
        "columns": TYPES,
        "where": {"op": op, "column": "amount", "value": value},
        "select": ["amount"],
    }
    result = run_operation(_sales(), spec)
    assert sorted(r[0] for r in result.table.rows) == sorted(expected_amounts)
    _lineage_adds_up(result)


def test_numeric_json_literal_matches_lexeme_value() -> None:
    spec = {
        "columns": TYPES,
        "where": {"op": "gt", "column": "amount", "value": 7},
        "select": ["amount"],
    }
    assert sorted(r[0] for r in run_operation(_sales(), spec).table.rows) == ["10.10", "7.5"]


def test_string_comparison_is_code_point_order() -> None:
    spec = {"where": {"op": "lt", "column": "region", "value": "north"}, "select": ["region"]}
    assert {r[0] for r in run_operation(_sales(), spec).table.rows} == {"east"}


def test_in_predicate_and_limit() -> None:
    spec = {
        "where": {"op": "in", "column": "region", "values": ["east", "south"]},
        "select": ["region", "note"],
    }
    assert len(run_operation(_sales(), spec).table.rows) == 3
    too_many = {"where": {"op": "in", "column": "region", "values": ["x"] * 1001}}
    assert _code(validate_spec, too_many) == "operation_invalid_spec"
    assert validate_spec({"where": {"op": "in", "column": "region", "values": ["x"] * 1000}})


def test_is_null_and_empty_string_null_semantics() -> None:
    # "" is null for typed columns, so eq/ne are false and is_null is true.
    null_amount = run_operation(
        _sales(),
        {"columns": TYPES, "where": {"op": "is_null", "column": "amount"}, "select": ["region"]},
    )
    assert null_amount.table.rows == (("south",),)
    eq_empty = run_operation(
        _sales(),
        {
            "columns": TYPES,
            "where": {"op": "ne", "column": "amount", "value": "0"},
            "select": ["region"],
        },
    )
    assert len(eq_empty.table.rows) == 4
    # "" is a real value for string columns.
    table = _table(["a"], [[""], [None]])
    assert run_operation(table, {"where": {"op": "is_null", "column": "a"}}).lineage["matched"] == 1
    assert (
        run_operation(table, {"where": {"op": "eq", "column": "a", "value": ""}}).lineage["matched"]
        == 1
    )


def test_string_operators_case_sensitive_and_insensitive() -> None:
    table = _sales()
    sensitive = run_operation(
        table, {"where": {"op": "contains", "column": "note", "value": "BETA"}}
    )
    assert sensitive.lineage["matched"] == 0
    insensitive = run_operation(
        table,
        {"where": {"op": "contains", "column": "note", "value": "BETA", "case": "insensitive"}},
    )
    assert insensitive.lineage["matched"] == 1
    assert (
        run_operation(
            table,
            {
                "where": {
                    "op": "starts_with",
                    "column": "note",
                    "value": "AL",
                    "case": "insensitive",
                }
            },
        ).lineage["matched"]
        == 1
    )
    assert (
        run_operation(
            table,
            {"where": {"op": "ends_with", "column": "note", "value": "TA", "case": "insensitive"}},
        ).lineage["matched"]
        == 2
    )


def test_string_operator_on_typed_column_is_invalid_spec() -> None:
    assert (
        _code(
            validate_spec,
            {"columns": TYPES, "where": {"op": "contains", "column": "amount", "value": "1"}},
        )
        == "operation_invalid_spec"
    )


def test_case_option_only_for_string_operators() -> None:
    bad = {"where": {"op": "eq", "column": "region", "value": "x", "case": "insensitive"}}
    assert _code(validate_spec, bad) == "operation_invalid_spec"
    bad_value = {"where": {"op": "contains", "column": "note", "value": "x", "case": "upper"}}
    assert _code(validate_spec, bad_value) == "operation_invalid_spec"


def test_nested_and_or_not_predicates() -> None:
    where = {
        "op": "and",
        "args": [
            {
                "op": "or",
                "args": [
                    {"op": "eq", "column": "region", "value": "north"},
                    {"op": "eq", "column": "region", "value": "east"},
                ],
            },
            {"op": "not", "arg": {"op": "is_null", "column": "amount"}},
            {"op": "ge", "column": "date", "value": "2026-01-10"},
        ],
    }
    result = run_operation(
        _sales(), {"columns": TYPES, "where": where, "select": ["region", "amount"]}
    )
    assert result.table.rows == (("north", "2.20"), ("east", "7.5"))
    _lineage_adds_up(result)


def test_depth_limit_and_node_limit() -> None:
    leaf = {"op": "is_null", "column": "region"}
    deep = leaf
    for _ in range(7):
        deep = {"op": "not", "arg": deep}  # depth 8
    assert validate_spec({"where": deep})
    too_deep = {"op": "not", "arg": deep}  # depth 9
    assert _code(validate_spec, {"where": too_deep}) == "operation_invalid_spec"

    wide = {"op": "or", "args": [{"op": "or", "args": [leaf] * 32} for _ in range(8)]}
    # 1 + 8 + 256 = 265 nodes
    assert _code(validate_spec, {"where": wide}) == "operation_invalid_spec"
    narrow = {"op": "or", "args": [{"op": "or", "args": [leaf] * 32} for _ in range(7)]}
    assert validate_spec({"where": narrow})


def test_logic_arg_count_limits() -> None:
    assert _code(validate_spec, {"where": {"op": "and", "args": []}}) == "operation_invalid_spec"
    args = [{"op": "is_null", "column": "region"}] * 33
    assert _code(validate_spec, {"where": {"op": "or", "args": args}}) == "operation_invalid_spec"


def test_date_column_exact_iso_only() -> None:
    table = _table(
        ["d"], [["2026-02-28"], ["2026-1-01"], ["2026-02-30"], ["20260301"], ["2026-03-01"]]
    )
    result = run_operation(
        table,
        {
            "columns": {"d": "date"},
            "where": {"op": "ge", "column": "d", "value": "2026-02-01"},
            "select": ["d"],
        },
    )
    assert result.lineage["excluded"] == {"invalid_date:d": 3}
    assert [r[0] for r in result.table.rows] == ["2026-02-28", "2026-03-01"]


def test_boolean_is_case_sensitive() -> None:
    table = _table(["b"], [["true"], ["True"], ["false"], [""]])
    result = run_operation(
        table, {"columns": {"b": "boolean"}, "where": {"op": "eq", "column": "b", "value": "true"}}
    )
    assert result.lineage["matched"] == 1
    assert result.lineage["excluded"] == {"invalid_boolean:b": 1}


def test_invalid_values_exclude_by_default_and_error_on_request() -> None:
    table = _table(["amount"], [["1.5"], ["abc"], ["1,000"], ["2"]])
    result = run_operation(table, {"columns": {"amount": "decimal"}, "select": ["amount"]})
    assert result.table.rows == (("1.5",), ("2",))
    assert result.lineage["excluded"] == {"invalid_decimal:amount": 2}
    _lineage_adds_up(result)
    error = _code(run_operation, table, {"columns": {"amount": "decimal"}, "on_invalid": "error"})
    assert error == "operation_invalid_value"


def test_invalid_value_error_message_does_not_echo_source() -> None:
    table = _table(["amount"], [["secret-value-xyz"]])
    with pytest.raises(OperationError) as info:
        run_operation(table, {"columns": {"amount": "decimal"}, "on_invalid": "error"})
    assert "secret-value-xyz" not in str(info.value)
    assert info.value.code == "operation_invalid_value"


@pytest.mark.parametrize("bad", ["abc", "1e", "+1", ".5", "1.", "NaN", "Infinity", "1_000", ""])
def test_invalid_literal_for_decimal_column(bad: str) -> None:
    assert (
        _code(
            validate_spec,
            {"columns": TYPES, "where": {"op": "eq", "column": "amount", "value": bad}},
        )
        == "operation_invalid_literal"
    )


def test_integer_column_rejects_fraction_and_exponent() -> None:
    cols = {"n": "integer"}
    for bad in ["1.0", "1e3"]:
        assert (
            _code(
                validate_spec, {"columns": cols, "where": {"op": "eq", "column": "n", "value": bad}}
            )
            == "operation_invalid_literal"
        )
    table = _table(["n"], [["1.0"], ["-7"], ["1e3"]])
    result = run_operation(table, {"columns": cols, "select": ["n"]})
    assert result.table.rows == (("-7",),)


def test_literal_failure_is_raised_by_run_operation_even_when_table_empty() -> None:
    spec = {"columns": TYPES, "where": {"op": "eq", "column": "amount", "value": "bad"}}
    assert _code(run_operation, _table(["amount", "date"], []), spec) == "operation_invalid_literal"


def test_unknown_column_fails_before_scanning() -> None:
    assert (
        _code(run_operation, _sales(), {"where": {"op": "eq", "column": "missing", "value": "x"}})
        == "operation_unknown_column"
    )
    assert _code(run_operation, _sales(), {"select": ["missing"]}) == "operation_unknown_column"
    assert (
        _code(run_operation, _sales(), {"columns": {"missing": "decimal"}})
        == "operation_unknown_column"
    )


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------


def test_validate_spec_rejects_unknown_keys_at_every_level() -> None:
    assert _code(validate_spec, {"bogus": 1}) == "operation_invalid_spec"
    assert (
        _code(validate_spec, {"where": {"op": "is_null", "column": "a", "extra": 1}})
        == "operation_invalid_spec"
    )
    assert (
        _code(
            validate_spec, {"where": {"op": "not", "arg": {"op": "is_null", "column": "a"}, "x": 1}}
        )
        == "operation_invalid_spec"
    )
    assert (
        _code(validate_spec, {"aggregates": [{"op": "count", "as": "n", "extra": 1}]})
        == "operation_invalid_spec"
    )
    assert (
        _code(validate_spec, {"order_by": [{"column": "a", "direction": "asc", "nulls": "first"}]})
        == "operation_invalid_spec"
    )


def test_validate_spec_rejects_bools_as_ints_and_bad_limits() -> None:
    assert _code(validate_spec, {"limit": True}) == "operation_invalid_spec"
    assert _code(validate_spec, {"limit": 0}) == "operation_invalid_spec"
    assert _code(validate_spec, {"limit": 1001}) == "operation_invalid_spec"
    assert _code(validate_spec, {"limit": "10"}) == "operation_invalid_spec"
    assert validate_spec({"limit": 1000})["limit"] == 1000
    assert validate_spec({})["limit"] == 1000


def test_validate_spec_rejects_wrong_types() -> None:
    assert _code(validate_spec, []) == "operation_invalid_spec"
    assert _code(validate_spec, {"columns": {"a": "float"}}) == "operation_invalid_spec"
    assert _code(validate_spec, {"columns": {"a": True}}) == "operation_invalid_spec"
    assert _code(validate_spec, {"select": "region"}) == "operation_invalid_spec"
    assert _code(validate_spec, {"on_invalid": "skip"}) == "operation_invalid_spec"
    assert (
        _code(validate_spec, {"where": {"op": "eq", "column": "a", "value": True}})
        == "operation_invalid_spec"
    )
    assert (
        _code(validate_spec, {"where": {"op": "eq", "column": "a", "value": None}})
        == "operation_invalid_spec"
    )
    assert (
        _code(validate_spec, {"where": {"op": "matches", "column": "a", "value": "x"}})
        == "operation_invalid_spec"
    )


def test_validate_spec_rejects_non_finite_float_literal() -> None:
    assert (
        _code(
            validate_spec,
            {"columns": TYPES, "where": {"op": "eq", "column": "amount", "value": float("inf")}},
        )
        == "operation_invalid_spec"
    )


def test_group_by_requires_aggregates_and_select_excludes_aggregates() -> None:
    assert _code(validate_spec, {"group_by": ["region"]}) == "operation_invalid_spec"
    assert (
        _code(validate_spec, {"select": ["region"], "aggregates": [{"op": "count"}]})
        == "operation_invalid_spec"
    )
    assert (
        _code(validate_spec, {"order_by": [{"column": "x"}], "select": ["region"]})
        == "operation_invalid_spec"
    )


def test_aggregate_type_rules_are_checked_from_spec() -> None:
    assert (
        _code(validate_spec, {"aggregates": [{"op": "sum", "column": "region"}]})
        == "operation_invalid_spec"
    )
    assert (
        _code(
            validate_spec,
            {"aggregates": [{"op": "mean", "column": "date"}], "columns": {"date": "date"}},
        )
        == "operation_invalid_spec"
    )
    assert (
        _code(
            validate_spec,
            {"aggregates": [{"op": "min", "column": "flag"}], "columns": {"flag": "boolean"}},
        )
        == "operation_invalid_spec"
    )
    assert (
        _code(validate_spec, {"aggregates": [{"op": "count", "column": "region"}]})
        == "operation_invalid_spec"
    )
    assert validate_spec(
        {"aggregates": [{"op": "distinct_count", "column": "flag"}], "columns": {"flag": "boolean"}}
    )


def test_alias_defaults_and_duplicate_alias_error() -> None:
    spec = validate_spec(
        {
            "columns": TYPES,
            "aggregates": [
                {"op": "count"},
                {"op": "sum", "column": "amount"},
                {"op": "count_non_null", "column": "note"},
            ],
        }
    )
    assert [a["as"] for a in spec["aggregates"]] == ["count", "sum_amount", "count_non_null_note"]
    dup = {
        "columns": TYPES,
        "aggregates": [{"op": "sum", "column": "amount"}, {"op": "sum", "column": "amount"}],
    }
    assert _code(validate_spec, dup) == "operation_invalid_spec"
    alias_clash = {"group_by": ["region"], "aggregates": [{"op": "count", "as": "region"}]}
    assert _code(validate_spec, alias_clash) == "operation_invalid_spec"


def test_order_by_direction_and_group_order_columns() -> None:
    assert (
        _code(validate_spec, {"order_by": [{"column": "a", "direction": "up"}]})
        == "operation_invalid_spec"
    )
    spec = validate_spec({"order_by": [{"column": "a"}]})
    assert spec["order_by"] == [{"column": "a", "direction": "asc"}]
    grouped = {
        "group_by": ["region"],
        "aggregates": [{"op": "count"}],
        "order_by": [{"column": "nope"}],
    }
    assert _code(validate_spec, grouped) == "operation_invalid_spec"


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------


def _single(result) -> tuple:
    assert len(result.table.rows) == 1
    return result.table.rows[0]


def test_aggregates_on_decimal_column() -> None:
    spec = {
        "columns": TYPES,
        "aggregates": [
            {"op": "count", "as": "rows"},
            {"op": "count_non_null", "column": "amount", "as": "non_null"},
            {"op": "sum", "column": "amount", "as": "total"},
            {"op": "min", "column": "amount", "as": "lo"},
            {"op": "max", "column": "amount", "as": "hi"},
            {"op": "distinct_count", "column": "amount", "as": "distinct"},
        ],
    }
    result = run_operation(_sales(), spec)
    assert result.table.columns == ("rows", "non_null", "total", "lo", "hi", "distinct")
    assert _single(result) == ("5", "4", "24.80", "2.20", "10.10", "4")
    assert result.values == ()
    assert result.table.row_refs == ()
    _lineage_adds_up(result)


def test_aggregates_on_integer_column() -> None:
    table = _table(["n"], [["1"], ["2"], ["-5"], [""]])
    result = run_operation(
        table,
        {
            "columns": {"n": "integer"},
            "aggregates": [
                {"op": "sum", "column": "n"},
                {"op": "min", "column": "n"},
                {"op": "max", "column": "n"},
                {"op": "mean", "column": "n", "as": "avg"},
            ],
        },
    )
    assert _single(result)[:3] == ("-2", "-5", "2")
    assert _single(result)[3] == "-0.666666666667"
    assert result.values[0]["avg"] == {
        "sum": "-2",
        "count": 3,
        "display": "-0.666666666667",
        "exact": False,
    }


def test_aggregates_on_date_and_string_columns() -> None:
    result = run_operation(
        _sales(),
        {
            "columns": TYPES,
            "aggregates": [
                {"op": "min", "column": "date", "as": "first"},
                {"op": "max", "column": "date", "as": "last"},
                {"op": "min", "column": "note", "as": "min_note"},
                {"op": "max", "column": "note", "as": "max_note"},
            ],
        },
    )
    assert _single(result) == ("2025-12-31", "2026-03-09", "Beta", "gamma")


def test_sum_and_mean_over_zero_non_null_values_are_null() -> None:
    table = _table(["amount"], [[""], [None]])
    result = run_operation(
        table,
        {
            "columns": {"amount": "decimal"},
            "aggregates": [
                {"op": "sum", "column": "amount", "as": "s"},
                {"op": "mean", "column": "amount", "as": "m"},
                {"op": "min", "column": "amount", "as": "lo"},
                {"op": "count", "as": "n"},
            ],
        },
    )
    assert _single(result) == (None, None, None, "2")
    assert result.values == ({"m": None},)


def test_global_aggregate_on_empty_input_returns_one_row() -> None:
    result = run_operation(
        _table(["amount"], []),
        {
            "columns": {"amount": "decimal"},
            "aggregates": [{"op": "count"}, {"op": "sum", "column": "amount"}],
        },
    )
    assert _single(result) == ("0", None)
    assert result.lineage["output_rows"] == 1


def test_group_by_multiple_columns() -> None:
    table = _table(
        ["region", "kind", "amount"],
        [["n", "a", "1"], ["n", "b", "2"], ["n", "a", "3"], ["s", "a", "4"], ["n", "a", ""]],
    )
    result = run_operation(
        table,
        {
            "columns": {"amount": "decimal"},
            "group_by": ["region", "kind"],
            "aggregates": [
                {"op": "sum", "column": "amount", "as": "total"},
                {"op": "count", "as": "rows"},
            ],
            "order_by": [{"column": "total", "direction": "desc"}],
        },
    )
    assert result.table.columns == ("region", "kind", "total", "rows")
    assert result.table.rows == (("n", "a", "4", "3"), ("s", "a", "4", "1"), ("n", "b", "2", "1"))
    assert result.table.row_refs == ()
    _lineage_adds_up(result)


def test_group_by_null_is_its_own_group() -> None:
    table = _table(["region", "amount"], [["n", "1"], [None, "2"], ["n", "3"]])
    result = run_operation(
        table, {"group_by": ["region"], "aggregates": [{"op": "count", "as": "c"}]}
    )
    assert sorted(result.table.rows, key=lambda r: r[0] or "") == [(None, "1"), ("n", "2")]


def test_group_by_limits() -> None:
    assert (
        _code(
            validate_spec, {"group_by": ["a", "b", "c", "d", "e"], "aggregates": [{"op": "count"}]}
        )
        == "operation_invalid_spec"
    )
    rows = [[str(i)] for i in range(1001)]
    table = _table(["k"], rows)
    assert (
        _code(run_operation, table, {"group_by": ["k"], "aggregates": [{"op": "count"}]})
        == "operation_too_many_groups"
    )
    ok = run_operation(
        _table(["k"], rows[:1000]),
        {"group_by": ["k"], "aggregates": [{"op": "count"}], "limit": 1000},
    )
    assert ok.lineage["output_rows"] == 1000


# ---------------------------------------------------------------------------
# Mean and exact decimal arithmetic
# ---------------------------------------------------------------------------


def test_mean_exact_when_quotient_terminates() -> None:
    table = _table(["x"], [["1"], ["2"], ["3"]])
    result = run_operation(
        table,
        {"columns": {"x": "integer"}, "aggregates": [{"op": "mean", "column": "x", "as": "avg"}]},
    )
    assert _single(result) == ("2",)
    assert result.values == ({"avg": {"sum": "6", "count": 3, "display": "2", "exact": True}},)


def test_mean_non_terminating_is_rounded_half_even_to_twelve_digits() -> None:
    table = _table(["x"], [["0"], ["0"], ["1"]])
    result = run_operation(
        table,
        {"columns": {"x": "integer"}, "aggregates": [{"op": "mean", "column": "x", "as": "avg"}]},
    )
    assert _single(result) == ("0.333333333333",)
    assert result.values[0]["avg"]["exact"] is False
    assert result.values[0]["avg"]["display"] == "0.333333333333"

    two_thirds = run_operation(
        _table(["x"], [["0"], ["1"], ["1"]]),
        {"columns": {"x": "integer"}, "aggregates": [{"op": "mean", "column": "x", "as": "avg"}]},
    )
    assert two_thirds.values[0]["avg"]["display"] == "0.666666666667"


def test_mean_display_rounding_half_even_tie() -> None:
    # 1000000000000 + 1 over 2 -> 500000000000.5 (13 digits); ties round to even.
    table = _table(["x"], [["1000000000000"], ["1"]])
    result = run_operation(
        table,
        {"columns": {"x": "integer"}, "aggregates": [{"op": "mean", "column": "x", "as": "m"}]},
    )
    struct = result.values[0]["m"]
    assert struct["display"] == "500000000000"
    assert struct["exact"] is False


def test_mean_exact_for_small_binary_fraction() -> None:
    table = _table(["x"], [["0.5"], ["0.25"]])
    result = run_operation(
        table,
        {"columns": {"x": "decimal"}, "aggregates": [{"op": "mean", "column": "x", "as": "m"}]},
    )
    assert _single(result) == ("0.375",)
    assert result.values[0]["m"]["exact"] is True


def test_mean_with_grouping_has_one_structured_value_per_row() -> None:
    result = run_operation(
        _sales(),
        {
            "columns": TYPES,
            "group_by": ["region"],
            "aggregates": [{"op": "mean", "column": "amount", "as": "avg"}],
            "order_by": [{"column": "region"}],
        },
    )
    assert [r[0] for r in result.table.rows] == ["east", "north", "south"]
    assert len(result.values) == 3
    assert result.values[1]["avg"]["display"] == "6.15"
    assert result.values[2]["avg"]["display"] == "5"


def test_zero_point_one_plus_point_two_is_exact() -> None:
    table = _table(["x"], [["0.1"], ["0.2"]])
    result = run_operation(
        table,
        {"columns": {"x": "decimal"}, "aggregates": [{"op": "sum", "column": "x", "as": "s"}]},
    )
    assert _single(result) == ("0.3",)


def test_sum_keeps_input_scale_and_plain_notation() -> None:
    assert _single(
        run_operation(
            _table(["x"], [["1.10"], ["2.20"]]),
            {"columns": {"x": "decimal"}, "aggregates": [{"op": "sum", "column": "x", "as": "s"}]},
        )
    ) == ("3.30",)
    assert _single(
        run_operation(
            _table(["x"], [["1e3"], ["1"]]),
            {"columns": {"x": "decimal"}, "aggregates": [{"op": "sum", "column": "x", "as": "s"}]},
        )
    ) == ("1001",)
    assert _single(
        run_operation(
            _table(["x"], [["1e3"]]),
            {"columns": {"x": "decimal"}, "aggregates": [{"op": "sum", "column": "x", "as": "s"}]},
        )
    ) == ("1000",)
    assert _single(
        run_operation(
            _table(["x"], [["-1"], ["1"]]),
            {"columns": {"x": "decimal"}, "aggregates": [{"op": "sum", "column": "x", "as": "s"}]},
        )
    ) == ("0",)


def test_many_twenty_digit_values_sum_exactly() -> None:
    values = [f"{i + 10}" + "1234567890123456789" for i in range(50)]  # no leading zeros
    with decimal.localcontext(decimal.Context(prec=500)):
        expected = sum(Decimal(v) for v in values)
    table = _table(["x"], [[v] for v in values])
    result = run_operation(
        table,
        {"columns": {"x": "integer"}, "aggregates": [{"op": "sum", "column": "x", "as": "s"}]},
    )
    assert _single(result) == (format(expected, "f"),)


def test_fractional_twenty_digit_values_sum_exactly() -> None:
    values = ["12345678901234567890.12345678"] * 37 + ["0.00000001"]
    with decimal.localcontext(decimal.Context(prec=500)):
        expected = sum(Decimal(v) for v in values)
    table = _table(["x"], [[v] for v in values])
    result = run_operation(
        table,
        {"columns": {"x": "decimal"}, "aggregates": [{"op": "sum", "column": "x", "as": "s"}]},
    )
    assert _single(result) == (format(expected, "f"),)


def test_precision_exceeded_for_extreme_exponent() -> None:
    table = _table(["x"], [["1e999999"], ["1"]])
    assert (
        _code(
            run_operation,
            table,
            {"columns": {"x": "decimal"}, "aggregates": [{"op": "sum", "column": "x"}]},
        )
        == "operation_precision_exceeded"
    )


def test_precision_exceeded_when_output_would_be_huge() -> None:
    table = _table(["x"], [["1e999999"]])
    assert (
        _code(
            run_operation,
            table,
            {"columns": {"x": "decimal"}, "aggregates": [{"op": "max", "column": "x"}]},
        )
        == "operation_precision_exceeded"
    )


def test_precision_exceeded_with_lowered_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(operations, "MAX_PRECISION", 30)
    table = _table(["x"], [["999999999999999999999999999999"], ["1"]])
    assert (
        _code(
            run_operation,
            table,
            {"columns": {"x": "decimal"}, "aggregates": [{"op": "sum", "column": "x"}]},
        )
        == "operation_precision_exceeded"
    )
    fits = _table(["x"], [["12345678901234567890"], ["1"]])
    assert _single(
        run_operation(
            fits,
            {"columns": {"x": "decimal"}, "aggregates": [{"op": "sum", "column": "x", "as": "s"}]},
        )
    ) == ("12345678901234567891",)


def test_huge_exponent_literal_is_invalid_value_not_crash() -> None:
    table = _table(["x"], [["1e99999999999999999999999"], ["2"]])
    result = run_operation(table, {"columns": {"x": "decimal"}, "select": ["x"]})
    assert result.table.rows == (("2",),)
    assert result.lineage["excluded"] == {"invalid_decimal:x": 1}


# ---------------------------------------------------------------------------
# Projection, ordering, limits, lineage
# ---------------------------------------------------------------------------


def test_projection_order_limit_and_truncation() -> None:
    result = run_operation(
        _sales(),
        {
            "columns": TYPES,
            "select": ["region", "amount"],
            "order_by": [{"column": "amount", "direction": "desc"}],
            "limit": 3,
        },
    )
    assert result.table.rows == (("north", "10.10"), ("east", "7.5"), ("south", "5"))
    assert result.lineage["output_truncated"] is True
    assert result.lineage["output_rows"] == 3
    assert result.lineage["excluded"] == {}
    assert result.lineage["matched"] == 5


def test_nulls_last_in_both_directions_and_stable_sort() -> None:
    table = _table(["k", "tag"], [["2", "a"], [None, "b"], ["1", "c"], ["2", "d"], [None, "e"]])
    base = {"columns": {"k": "integer"}, "select": ["k", "tag"]}
    asc = run_operation(table, {**base, "order_by": [{"column": "k"}]})
    assert asc.table.rows == (("1", "c"), ("2", "a"), ("2", "d"), (None, "b"), (None, "e"))
    desc = run_operation(table, {**base, "order_by": [{"column": "k", "direction": "desc"}]})
    assert desc.table.rows == (("2", "a"), ("2", "d"), ("1", "c"), (None, "b"), (None, "e"))


def test_multi_key_order() -> None:
    table = _table(["a", "b"], [["1", "x"], ["2", "a"], ["1", "a"]])
    result = run_operation(
        table,
        {"select": ["a", "b"], "order_by": [{"column": "a"}, {"column": "b", "direction": "desc"}]},
    )
    assert result.table.rows == (("1", "x"), ("1", "a"), ("2", "a"))


def test_projection_order_by_non_output_column_is_invalid() -> None:
    spec = {"select": ["region"], "order_by": [{"column": "amount"}]}
    assert _code(run_operation, _sales(), spec) == "operation_invalid_spec"


def test_projection_passes_row_refs_and_keeps_cell_text() -> None:
    result = run_operation(
        _sales(),
        {
            "columns": TYPES,
            "select": ["amount"],
            "where": {"op": "gt", "column": "amount", "value": "5"},
        },
    )
    assert result.table.rows == (("10.10",), ("7.5",))
    assert result.table.row_refs == (
        {"type": "rows", "start": 2, "end": 2},
        {"type": "rows", "start": 6, "end": 6},
    )
    assert result.values == ()


def test_limit_one_and_default_limit_cover_all_rows() -> None:
    assert run_operation(_sales(), {"limit": 1}).lineage["output_rows"] == 1
    default = run_operation(_sales(), {})
    assert default.lineage["output_truncated"] is False
    assert len(default.table.rows) == 5
    assert default.table.columns == ("region", "amount", "date", "note")


def test_lineage_counts_add_up_and_version_is_recorded() -> None:
    result = run_operation(
        _sales(),
        {
            "columns": TYPES,
            "where": {"op": "gt", "column": "amount", "value": "3"},
            "select": ["region"],
        },
    )
    lin = result.lineage
    assert lin["source_rows"] == 5
    assert lin["scanned"] == 5
    assert lin["matched"] == 3
    assert lin["not_matched"] == 2  # "2.20" fails the predicate; "" is null and compares false
    assert lin["excluded"] == {}
    assert lin["operations_version"] == OPERATIONS_VERSION == "desktop-operations-v1"
    _lineage_adds_up(result)


def test_lineage_with_exclusions_adds_up() -> None:
    table = _table(["amount", "region"], [["1", "a"], ["bad", "a"], ["2", "b"], ["x", "b"]])
    result = run_operation(
        table,
        {
            "columns": {"amount": "decimal"},
            "where": {"op": "eq", "column": "region", "value": "b"},
            "select": ["amount"],
        },
    )
    # "bad" sits in region a, which the predicate rejects: not_matched, not excluded.
    assert result.lineage["excluded"] == {"invalid_decimal:amount": 1}
    assert result.lineage["not_matched"] == 2
    assert result.lineage["matched"] == 1
    _lineage_adds_up(result)


def test_spec_digest_is_stable_across_key_order_and_defaults() -> None:
    a = {
        "columns": TYPES,
        "select": ["region"],
        "limit": 10,
        "where": {"op": "eq", "column": "region", "value": "north"},
    }
    b = {
        "where": {"value": "north", "column": "region", "op": "eq"},
        "limit": 10,
        "select": ["region"],
        "columns": {"date": "date", "amount": "decimal"},
    }
    c = copy.deepcopy(a)
    c["on_invalid"] = "exclude"
    ra = run_operation(_sales(), a).lineage["spec_digest"]
    rb = run_operation(_sales(), b).lineage["spec_digest"]
    rc = run_operation(_sales(), c).lineage["spec_digest"]
    assert ra == rb == rc
    assert len(ra) == 64
    different = run_operation(_sales(), {**a, "limit": 11}).lineage["spec_digest"]
    assert different != ra


def test_run_operation_is_deterministic_and_does_not_mutate_spec() -> None:
    spec = {
        "columns": TYPES,
        "group_by": ["region"],
        "aggregates": [{"op": "sum", "column": "amount"}],
    }
    snapshot = copy.deepcopy(spec)
    first = run_operation(_sales(), spec)
    second = run_operation(_sales(), spec)
    assert first == second
    assert spec == snapshot


def test_operation_error_carries_code_only() -> None:
    err = OperationError("operation_invalid_value")
    assert err.code == "operation_invalid_value"
    assert str(err) == "operation_invalid_value"
    assert isinstance(err, ValueError)


def test_order_by_mean_alias_uses_exact_quotient() -> None:
    table = _table(["g", "x"], [["a", "1"], ["a", "2"], ["b", "2"], ["b", "2"]])
    result = run_operation(
        table,
        {
            "columns": {"x": "integer"},
            "group_by": ["g"],
            "aggregates": [{"op": "mean", "column": "x", "as": "avg"}],
            "order_by": [{"column": "avg", "direction": "desc"}],
        },
    )
    assert [r[0] for r in result.table.rows] == ["b", "a"]
    assert [r[1] for r in result.table.rows] == ["2", "1.5"]


def test_rows_rejected_by_the_predicate_are_not_matched_not_excluded() -> None:
    table = parse_table("id,v\n007,x\n007,\n008,abc\n", "csv")
    spec = {
        "columns": {"v": "decimal"},
        "where": {"op": "eq", "column": "id", "value": "007"},
        "aggregates": [{"op": "count"}, {"op": "count_non_null", "column": "v"}],
    }
    result = run_operation(table, spec)
    # 007,x is excluded; 007 with an empty v is a null, so it matches.
    assert result.lineage["matched"] == 1
    assert result.lineage["not_matched"] == 1
    assert result.lineage["excluded"] == {"invalid_decimal:v": 1}
    # An invalid value in a row the filter already rejects cannot fail the call.
    strict = run_operation(
        parse_table("id,v\n007,1\n008,abc\n", "csv"), {**spec, "on_invalid": "error"}
    )
    assert strict.table.rows == (("1", "1"),)


def test_invalid_predicate_column_still_excludes_the_row() -> None:
    table = parse_table("v\n5\nabc\n1\n", "csv")
    result = run_operation(
        table,
        {
            "columns": {"v": "decimal"},
            "where": {"op": "gt", "column": "v", "value": "2"},
            "aggregates": [{"op": "count"}],
        },
    )
    assert result.lineage["matched"] == 1
    assert result.lineage["not_matched"] == 1
    assert result.lineage["excluded"] == {"invalid_decimal:v": 1}
