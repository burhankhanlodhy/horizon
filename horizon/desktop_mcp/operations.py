"""Validated, bounded, typed operations over parsed tables.

A specification is data, never code: no eval, no model-supplied SQL or
formulas, and no regular expressions taken from the specification. Every
source row is scanned. Lineage counts always add up:

    matched + sum(excluded) + not_matched == scanned == source_rows

Typing rules
- Column types are ``string`` (default), ``decimal``, ``integer``, ``date``
  (exact ``YYYY-MM-DD``), and ``boolean`` (exact ``true``/``false``).
- For typed columns ``None`` and ``""`` are null. For ``string`` only ``None``
  is null.
- A typed cell that does not parse is an invalid value. With
  ``on_invalid: "exclude"`` (default) the row is excluded and counted under
  ``excluded["invalid_<type>:<column>"]``; the first invalid referenced column
  in table order names the reason. With ``"error"`` the operation fails.
  Predicate columns are checked first: a row the predicate rejects is
  ``not_matched`` even if another column it does not need is invalid, and
  ``"error"`` never fires for such a row.

Exactness
- Decimal and integer arithmetic runs in a context with ``MAX_PRECISION``
  significant digits and ``Inexact`` trapped. Any sum that would need more
  digits raises ``operation_precision_exceeded``; nothing is rounded silently.
- Results are formatted with ``format(d, "f")``: no exponent notation, scale
  preserved (``1.10 + 2.20`` is ``"3.30"``, ``1e3`` alone is ``"1000"``).
  Zero is printed without a sign.
- ``mean`` is ``sum / count``. ``display`` is that quotient rounded
  ROUND_HALF_EVEN to 12 significant digits. ``exact`` is true when the quotient
  terminates within those 12 significant digits (no rounding happened).
"""

from __future__ import annotations

import datetime as _dt
import decimal
import hashlib
import json
import math
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, NamedTuple

from horizon.desktop_mcp.tables import ResultTable, Table

OPERATIONS_VERSION = "desktop-operations-v1"

# Module constants below are read at call time so tests can lower them.
MAX_PRECISION = 10_000
MAX_MEAN_DIGITS = 12
MAX_GROUPS = 1_000
MAX_GROUP_BY = 4
MAX_LIMIT = 1_000
DEFAULT_LIMIT = 1_000
MAX_PREDICATE_DEPTH = 8
MAX_PREDICATE_NODES = 256
MAX_LOGIC_ARGS = 32
MAX_IN_VALUES = 1_000

COLUMN_TYPES = ("string", "decimal", "integer", "date", "boolean")
AGGREGATE_OPS = (
    "count",
    "count_non_null",
    "sum",
    "min",
    "max",
    "mean",
    "distinct_count",
)
_NUMERIC_TYPES = ("decimal", "integer")
_ORDERABLE_TYPES = ("decimal", "integer", "date", "string")
_CMP_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
}
_STRING_OPS: dict[str, Callable[[str, str], bool]] = {
    "contains": lambda value, lit: lit in value,
    "starts_with": lambda value, lit: value.startswith(lit),
    "ends_with": lambda value, lit: value.endswith(lit),
}
_TOP_KEYS = frozenset(
    {"columns", "where", "group_by", "aggregates", "select", "order_by", "limit", "on_invalid"}
)

_DECIMAL_RE = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_INTEGER_RE = re.compile(r"-?(0|[1-9][0-9]*)")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class OperationError(ValueError):
    """Sanitized operation failure with a stable code and no source values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OperationResult:
    table: ResultTable
    # One dict per output row when a mean aggregate is present, else empty.
    # Each dict maps a mean alias to {"sum", "count", "display", "exact"}
    # (or None for a group with no non-null values).
    values: tuple[dict[str, Any], ...]
    lineage: dict[str, Any]


def _invalid(code: str = "operation_invalid_spec") -> OperationError:
    return OperationError(code)


# ---------------------------------------------------------------------------
# Typed parsing
# ---------------------------------------------------------------------------


def _parse_typed(text: str, typ: str) -> Any:
    """Parse a non-null lexeme for ``typ``; raises ValueError on mismatch."""
    if typ == "string":
        return text
    if typ == "decimal" or typ == "integer":
        pattern = _DECIMAL_RE if typ == "decimal" else _INTEGER_RE
        if not pattern.fullmatch(text):
            raise ValueError(typ)
        try:
            value = Decimal(text)
        except ArithmeticError as exc:
            raise ValueError(typ) from exc
        if not value.is_finite():
            raise ValueError(typ)
        return value
    if typ == "date":
        if not _DATE_RE.fullmatch(text):
            raise ValueError(typ)
        return _dt.date.fromisoformat(text)
    if typ == "boolean":
        if text == "true":
            return True
        if text == "false":
            return False
        raise ValueError(typ)
    raise ValueError(typ)


def _typed_cell(cell: str | None, typ: str) -> tuple[bool, Any]:
    """Return (ok, value). ``None`` is null; invalid cells return (False, None)."""
    if cell is None:
        return True, None
    if typ == "string":
        return True, cell
    if cell == "":
        return True, None
    try:
        return True, _parse_typed(cell, typ)
    except ValueError:
        return False, None


def _literal(value: Any, typ: str) -> str:
    """Validate a predicate literal and return its canonical string form."""
    if typ == "string":
        if not isinstance(value, str):
            raise _invalid()
        return value
    if isinstance(value, str):
        text = value
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid()
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise _invalid()
        text = repr(value)
    else:
        try:
            text = str(value)
        except ValueError as exc:
            raise OperationError("operation_invalid_literal") from exc
    try:
        _parse_typed(text, typ)
    except ValueError as exc:
        raise OperationError("operation_invalid_literal") from exc
    return text


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_decimal(value: Decimal) -> str:
    if value.is_zero():
        exponent = value.as_tuple().exponent
        if isinstance(exponent, int) and exponent < 0 and -exponent <= MAX_PRECISION:
            return format(Decimal(0).scaleb(exponent), "f")
        return "0"
    exponent = value.as_tuple().exponent
    adjusted = value.adjusted()
    if max(adjusted, 0) + 1 + max(-exponent, 0) > MAX_PRECISION:
        raise OperationError("operation_precision_exceeded")
    return format(value, "f")


def _format_value(value: Any, typ: str) -> str | None:
    if value is None:
        return None
    if typ in _NUMERIC_TYPES:
        return _format_decimal(value)
    if typ == "date":
        return value.isoformat()
    if typ == "boolean":
        return "true" if value else "false"
    return str(value)


def _mean(total: Decimal, count: int) -> tuple[Decimal, bool]:
    ctx = decimal.Context(
        prec=MAX_MEAN_DIGITS,
        rounding=decimal.ROUND_HALF_EVEN,
        Emax=decimal.MAX_EMAX,
        Emin=decimal.MIN_EMIN,
        traps=[],
    )
    quotient = ctx.divide(total, Decimal(count))
    return quotient, not ctx.flags[decimal.Inexact]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _reject_unknown(node: Any, allowed: frozenset[str] | set[str]) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise _invalid()
    for key in node:
        if not isinstance(key, str) or key not in allowed:
            raise _invalid()
    return node


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid()
    return value


def _names(raw: Any, *, min_items: int, max_items: int | None) -> list[str]:
    if not isinstance(raw, list):
        raise _invalid()
    if len(raw) < min_items or (max_items is not None and len(raw) > max_items):
        raise _invalid()
    names = [_name(item) for item in raw]
    if len(set(names)) != len(names):
        raise _invalid()
    return names


def _validate_columns(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise _invalid()
    columns: dict[str, str] = {}
    for name, typ in raw.items():
        _name(name)
        if typ not in COLUMN_TYPES:
            raise _invalid()
        columns[name] = typ
    return columns


def _predicate(node: Any, types: dict[str, str], depth: int, counter: list[int]) -> dict[str, Any]:
    if depth > MAX_PREDICATE_DEPTH:
        raise _invalid()
    counter[0] += 1
    if counter[0] > MAX_PREDICATE_NODES:
        raise _invalid()
    if not isinstance(node, dict):
        raise _invalid()
    op = node.get("op")
    if op in ("and", "or"):
        _reject_unknown(node, {"op", "args"})
        args = node.get("args")
        if not isinstance(args, list) or not 1 <= len(args) <= MAX_LOGIC_ARGS:
            raise _invalid()
        return {"op": op, "args": [_predicate(a, types, depth + 1, counter) for a in args]}
    if op == "not":
        _reject_unknown(node, {"op", "arg"})
        return {"op": "not", "arg": _predicate(node.get("arg"), types, depth + 1, counter)}
    if op in _CMP_OPS:
        _reject_unknown(node, {"op", "column", "value"})
        column = _name(node.get("column"))
        value = _literal(node.get("value"), types.get(column, "string"))
        return {"op": op, "column": column, "value": value}
    if op == "in":
        _reject_unknown(node, {"op", "column", "values"})
        column = _name(node.get("column"))
        raw_values = node.get("values")
        if not isinstance(raw_values, list) or not 1 <= len(raw_values) <= MAX_IN_VALUES:
            raise _invalid()
        typ = types.get(column, "string")
        return {
            "op": "in",
            "column": column,
            "values": [_literal(v, typ) for v in raw_values],
        }
    if op == "is_null":
        _reject_unknown(node, {"op", "column"})
        return {"op": "is_null", "column": _name(node.get("column"))}
    if op in _STRING_OPS:
        _reject_unknown(node, {"op", "column", "value", "case"})
        column = _name(node.get("column"))
        if types.get(column, "string") != "string":
            raise _invalid()
        value = node.get("value")
        if not isinstance(value, str):
            raise _invalid()
        result: dict[str, Any] = {"op": op, "column": column, "value": value}
        if "case" in node:
            if node["case"] != "insensitive":
                raise _invalid()
            result["case"] = "insensitive"
        return result
    raise _invalid()


def _aggregates(raw: Any, types: dict[str, str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise _invalid()
    result: list[dict[str, Any]] = []
    for item in raw:
        _reject_unknown(item, {"op", "column", "as"})
        op = item.get("op")
        if op not in AGGREGATE_OPS:
            raise _invalid()
        entry: dict[str, Any]
        if op == "count":
            if "column" in item:
                raise _invalid()
            default_alias = "count"
            entry = {"op": op}
        else:
            column = _name(item.get("column"))
            typ = types.get(column, "string")
            if op in ("sum", "mean") and typ not in _NUMERIC_TYPES:
                raise _invalid()
            if op in ("min", "max") and typ not in _ORDERABLE_TYPES:
                raise _invalid()
            default_alias = f"{op}_{column}"
            entry = {"op": op, "column": column}
        alias = item.get("as", default_alias)
        entry["as"] = _name(alias)
        result.append(entry)
    return result


def _order(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        raise _invalid()
    result = []
    for item in raw:
        _reject_unknown(item, {"column", "direction"})
        direction = item.get("direction", "asc")
        if direction not in ("asc", "desc"):
            raise _invalid()
        result.append({"column": _name(item.get("column")), "direction": direction})
    return result


def validate_spec(spec: Any) -> dict[str, Any]:
    """Return a normalized copy of ``spec`` or raise ``OperationError``.

    Normalization fills defaults (``limit``, ``on_invalid``, aggregate aliases,
    order direction), drops ``"string"`` column entries, and removes absent
    optional keys, so equivalent specs share one ``spec_digest``.
    """
    if not isinstance(spec, dict):
        raise _invalid()
    _reject_unknown(spec, _TOP_KEYS)
    types = _validate_columns(spec.get("columns", {}))

    limit = spec.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise _invalid()

    on_invalid = spec.get("on_invalid", "exclude")
    if on_invalid not in ("exclude", "error"):
        raise _invalid()

    where = None
    if spec.get("where") is not None:
        where = _predicate(spec["where"], types, 1, [0])

    group_by: list[str] = []
    if spec.get("group_by") is not None:
        group_by = _names(spec["group_by"], min_items=0, max_items=MAX_GROUP_BY)

    aggregates = []
    if spec.get("aggregates") is not None:
        aggregates = _aggregates(spec["aggregates"], types)

    select = None
    if spec.get("select") is not None:
        select = _names(spec["select"], min_items=1, max_items=None)

    order = []
    if spec.get("order_by") is not None:
        order = _order(spec["order_by"])

    if aggregates:
        if select is not None:
            raise _invalid()
        outputs = group_by + [a["as"] for a in aggregates]
        if len(set(outputs)) != len(outputs):
            raise _invalid()
        if any(o["column"] not in outputs for o in order):
            raise _invalid()
    else:
        if group_by:
            raise _invalid()
        if select is not None and any(o["column"] not in select for o in order):
            raise _invalid()

    normalized: dict[str, Any] = {
        "columns": {k: v for k, v in types.items() if v != "string"},
        "group_by": group_by,
        "aggregates": aggregates,
        "order_by": order,
        "limit": limit,
        "on_invalid": on_invalid,
    }
    if where is not None:
        normalized["where"] = where
    if select is not None:
        normalized["select"] = select
    return normalized


def _predicate_columns(node: dict[str, Any], out: list[str]) -> None:
    op = node["op"]
    if op in ("and", "or"):
        for arg in node["args"]:
            _predicate_columns(arg, out)
    elif op == "not":
        _predicate_columns(node["arg"], out)
    else:
        out.append(node["column"])


def _digest(normalized: dict[str, Any]) -> str:
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


# ---------------------------------------------------------------------------
# Predicate compilation
# ---------------------------------------------------------------------------


def _compile(
    node: dict[str, Any], index: dict[str, int], types: list[str]
) -> Callable[[dict[int, Any]], bool]:
    op = node["op"]
    if op in ("and", "or"):
        parts = [_compile(arg, index, types) for arg in node["args"]]
        if op == "and":
            return lambda typed: all(part(typed) for part in parts)
        return lambda typed: any(part(typed) for part in parts)
    if op == "not":
        inner = _compile(node["arg"], index, types)
        return lambda typed: not inner(typed)

    idx = index[node["column"]]
    typ = types[idx]
    if op in _CMP_OPS:
        compare = _CMP_OPS[op]
        literal = _parse_typed(node["value"], typ)
        return lambda typed: typed[idx] is not None and compare(typed[idx], literal)
    if op == "in":
        members = {_parse_typed(v, typ) for v in node["values"]}
        return lambda typed: typed[idx] is not None and typed[idx] in members
    if op == "is_null":
        return lambda typed: typed[idx] is None
    # String operators: case-sensitive by default.
    insensitive = node.get("case") == "insensitive"
    literal_text = node["value"].casefold() if insensitive else node["value"]
    test = _STRING_OPS[op]

    def string_match(typed: dict[int, Any]) -> bool:
        value = typed[idx]
        if value is None:
            return False
        if insensitive:
            value = value.casefold()
        return test(value, literal_text)

    return string_match


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class _Entry(NamedTuple):
    cells: tuple[str | None, ...]
    sort: dict[str, Any]
    ref: dict[str, Any] | None
    values: dict[str, Any]


def _new_accumulators(specs: list[tuple[str, int | None, str, str]]) -> list[Any]:
    accs: list[Any] = []
    for op, _idx, _typ, _alias in specs:
        if op == "count_non_null":
            accs.append([0])
        elif op == "distinct_count":
            accs.append([set()])
        elif op in ("sum", "mean"):
            accs.append([None, 0])
        elif op in ("min", "max"):
            accs.append([None])
        else:
            accs.append(None)
    return accs


def _update(
    group: list[Any], specs: list[tuple[str, int | None, str, str]], typed: dict[int, Any]
) -> None:
    group[0] += 1
    for i, (op, idx, _typ, _alias) in enumerate(specs):
        value = typed[idx] if idx is not None else None
        acc = group[1][i]
        if op == "count" or value is None:
            continue
        if op == "count_non_null":
            acc[0] += 1
        elif op == "distinct_count":
            acc[0].add(value)
        elif op in ("sum", "mean"):
            acc[0] = value if acc[0] is None else acc[0] + value
            acc[1] += 1
        elif op == "min":
            if acc[0] is None or value < acc[0]:
                acc[0] = value
        elif op == "max":
            if acc[0] is None or value > acc[0]:
                acc[0] = value


def _finalize_agg(
    op: str, acc: Any, rows: int, typ: str
) -> tuple[str | None, Any, dict[str, Any] | None]:
    """Return (display cell, typed sort value, structured mean or None)."""
    if op == "count":
        return str(rows), rows, None
    if op == "count_non_null":
        return str(acc[0]), acc[0], None
    if op == "distinct_count":
        return str(len(acc[0])), len(acc[0]), None
    if op == "sum":
        total = acc[0]
        if total is None:
            return None, None, None
        return _format_decimal(total), total, None
    if op == "mean":
        total, count = acc
        if total is None:
            return None, None, None
        quotient, exact = _mean(total, count)
        display = _format_decimal(quotient)
        struct = {
            "sum": _format_decimal(total),
            "count": count,
            "display": display,
            "exact": exact,
        }
        return display, Fraction(total) / count, struct
    # min / max
    if acc[0] is None:
        return None, None, None
    return _format_value(acc[0], typ), acc[0], None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _type_cells(
    values: tuple[str | None, ...],
    indices: list[int],
    types: list[str],
    columns: tuple[str, ...],
    typed: dict[int, Any],
    reject_on_invalid: bool,
) -> str | None:
    """Type the given cells into ``typed``; return the first invalid reason."""
    invalid_reason: str | None = None
    for idx in indices:
        ok, value = _typed_cell(values[idx], types[idx])
        if not ok:
            if reject_on_invalid:
                raise OperationError("operation_invalid_value")
            if invalid_reason is None:
                invalid_reason = f"invalid_{types[idx]}:{columns[idx]}"
        typed[idx] = value
    return invalid_reason


def run_operation(table: Table, spec: dict[str, Any]) -> OperationResult:
    norm = validate_spec(spec)
    digest = _digest(norm)

    index: dict[str, int] = {}
    for position, name in enumerate(table.columns):
        index.setdefault(name, position)
    types = [norm["columns"].get(name, "string") for name in table.columns]

    aggregates = norm["aggregates"]
    aggregate_mode = bool(aggregates)
    group_names = norm["group_by"]
    order = norm["order_by"]

    where_names: list[str] = []
    if "where" in norm:
        _predicate_columns(norm["where"], where_names)

    # Referenced table columns: unknown names fail before any row is read.
    referenced: list[str] = list(norm["columns"]) + where_names
    referenced.extend(group_names)
    referenced.extend(a["column"] for a in aggregates if "column" in a)
    referenced.extend(norm.get("select", []))
    if not aggregate_mode:
        referenced.extend(o["column"] for o in order)
    if any(name not in index for name in referenced):
        raise OperationError("operation_unknown_column")

    if aggregate_mode:
        out_names = group_names + [a["as"] for a in aggregates]
    elif "select" in norm:
        out_names = list(norm["select"])
    else:
        out_names = list(table.columns)
    if any(o["column"] not in out_names for o in order):
        raise _invalid()

    group_idx = [index[name] for name in group_names]
    agg_specs = [
        (
            a["op"],
            index[a["column"]] if "column" in a else None,
            types[index[a["column"]]] if "column" in a else "integer",
            a["as"],
        )
        for a in aggregates
    ]

    needed: set[int] = set(group_idx)
    needed.update(spec_idx for _op, spec_idx, _t, _a in agg_specs if spec_idx is not None)
    needed.update(index[name] for name in where_names)
    if "select" in norm:
        needed.update(index[name] for name in norm["select"])
    if not aggregate_mode:
        needed.update(index[o["column"]] for o in order)
        if "select" not in norm:
            # Whole-row projection outputs every column, so every typed column
            # is validated before its row is emitted.
            needed.update(range(len(table.columns)))
    # Predicate columns are typed first so a row the filter rejects is counted
    # as not_matched, never as excluded for an invalid value it does not use.
    where_sorted = sorted({index[name] for name in where_names})
    rest_sorted = sorted(needed.difference(where_sorted))

    predicate = _compile(norm["where"], index, types) if "where" in norm else None
    reject_on_invalid = norm["on_invalid"] == "error"

    scanned = 0
    matched = 0
    not_matched = 0
    excluded: dict[str, int] = {}
    groups: dict[tuple[Any, ...], list[Any]] = {}
    projected: list[tuple[Any, dict[int, Any]]] = []
    if aggregate_mode and not group_idx:
        groups[()] = [0, _new_accumulators(agg_specs)]

    ctx = decimal.Context(
        prec=MAX_PRECISION,
        rounding=decimal.ROUND_HALF_EVEN,
        Emax=decimal.MAX_EMAX,
        Emin=decimal.MIN_EMIN,
        traps=[decimal.Inexact],
    )
    try:
        with decimal.localcontext(ctx):
            for row in table.rows:
                scanned += 1
                typed: dict[int, Any] = {}
                invalid_reason = _type_cells(
                    row.values, where_sorted, types, table.columns, typed, reject_on_invalid
                )
                if invalid_reason is None and predicate is not None and not predicate(typed):
                    not_matched += 1
                    continue
                if invalid_reason is None:
                    invalid_reason = _type_cells(
                        row.values, rest_sorted, types, table.columns, typed, reject_on_invalid
                    )
                if invalid_reason is not None:
                    excluded[invalid_reason] = excluded.get(invalid_reason, 0) + 1
                    continue
                matched += 1
                if aggregate_mode:
                    key = tuple(typed[i] for i in group_idx)
                    group = groups.get(key)
                    if group is None:
                        if len(groups) >= MAX_GROUPS:
                            raise OperationError("operation_too_many_groups")
                        group = groups[key] = [0, _new_accumulators(agg_specs)]
                    _update(group, agg_specs, typed)
                else:
                    projected.append((row, typed))

            entries = _build_entries(
                aggregate_mode,
                out_names,
                index,
                types,
                group_idx,
                agg_specs,
                groups,
                projected,
                order,
            )
    except decimal.Inexact as exc:
        raise OperationError("operation_precision_exceeded") from exc

    for item in reversed(order):
        name = item["column"]
        desc = item["direction"] == "desc"
        present = [e for e in entries if e.sort[name] is not None]
        missing = [e for e in entries if e.sort[name] is None]
        present.sort(key=lambda e, n=name: e.sort[n], reverse=desc)
        entries = present + missing

    truncated = len(entries) > norm["limit"]
    entries = entries[: norm["limit"]]

    has_mean = any(a["op"] == "mean" for a in aggregates)
    result_table = ResultTable(
        columns=tuple(out_names),
        rows=tuple(e.cells for e in entries),
        row_refs=() if aggregate_mode else tuple(e.ref for e in entries if e.ref is not None),
    )
    values = tuple(e.values for e in entries) if has_mean else ()
    lineage = {
        "operations_version": OPERATIONS_VERSION,
        "source_rows": len(table.rows),
        "scanned": scanned,
        "matched": matched,
        "not_matched": not_matched,
        "excluded": dict(sorted(excluded.items())),
        "output_rows": len(entries),
        "output_truncated": truncated,
        "spec_digest": digest,
    }
    return OperationResult(table=result_table, values=values, lineage=lineage)


def _build_entries(
    aggregate_mode: bool,
    out_names: list[str],
    index: dict[str, int],
    types: list[str],
    group_idx: list[int],
    agg_specs: list[tuple[str, int | None, str, str]],
    groups: dict[tuple[Any, ...], list[Any]],
    projected: list[tuple[Any, dict[int, Any]]],
    order: list[dict[str, str]],
) -> list[_Entry]:
    entries: list[_Entry] = []
    if aggregate_mode:
        for key, group in groups.items():
            cells: list[str | None] = []
            sorts: list[Any] = []
            values: dict[str, Any] = {}
            for value, idx in zip(key, group_idx):
                cells.append(_format_value(value, types[idx]))
                sorts.append(value)
            for (op, _idx, typ, alias), acc in zip(agg_specs, group[1]):
                cell, sort_value, struct = _finalize_agg(op, acc, group[0], typ)
                cells.append(cell)
                sorts.append(sort_value)
                if op == "mean":
                    values[alias] = struct
            entries.append(_Entry(tuple(cells), dict(zip(out_names, sorts)), None, values))
        return entries

    select_idx = [index[name] for name in out_names]
    order_names = [o["column"] for o in order]
    for row, typed in projected:
        cells_raw = tuple(row.values[i] for i in select_idx)
        sort_map = {name: typed[index[name]] for name in order_names}
        entries.append(_Entry(cells_raw, sort_map, row.ref, {}))
    return entries


__all__ = [
    "AGGREGATE_OPS",
    "COLUMN_TYPES",
    "OPERATIONS_VERSION",
    "OperationError",
    "OperationResult",
    "run_operation",
    "validate_spec",
]
