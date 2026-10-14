---
title: "ContextShrink Desktop MCP: Phase E deterministic work and artifacts"
date: 2026-10-14
status: e1-core-in-progress
phase: E
milestones: [M11, M12]
---

# Phase E: deterministic computation, fused reads, and artifacts

Phase E implements M11 (batched deterministic work) and M12 (artifact
delivery) from the [build plan](2026-10-13-claude-desktop-enterprise-mcp-build-plan.md),
sections 5, 7 and 18, plus the short output instruction pack. Its value is that
exact counts, sums, filters and repeated rows are computed or assembled
locally, so Claude neither reads every raw row nor regenerates a large table.
It does not lower Desktop's internal reasoning budget, and a file destination
alone is not a savings claim.

Nothing here evaluates model-supplied code, SQL, shell, or formulas. Every
operation is a validated, bounded, typed specification.

## Delivery stages

| Stage | Scope | Status |
|---|---|---|
| E1 | Pure core: `tables`, `operations`, `batching`, `answer_contract`, `artifacts` with unit tests | in progress |
| E2 | Backend/broker wiring; compute profile with `contextshrink_analyze` and `contextshrink_render`; contracts | pending |
| E3 | Companion artifact browser, user-operated export, revocation, SDK evaluation | pending |

E1 modules have no MCP, broker, network, or model dependencies.

## E1 interfaces

### `tables.py`

`parse_table(text, kind, *, max_rows, max_columns, max_cell_chars) -> Table`.

- `csv`/`tsv`: the first record is the header. Header names must be non-empty
  and unique, otherwise `TableError("table_header_invalid")`. Records use the
  same logical numbering as the `rows` selector (header is record 1, so the
  first data row has `ref {"type": "rows", "start": 2, "end": 2}`). Short records
  pad with `None`; a record longer than the header is `table_row_width`.
- `json`: a top-level array of flat objects. Columns are the union of keys in
  first-seen order. `ref` is `{"type": "json_pointer", "pointer": "/<index>"}`.
  Nested arrays/objects are `table_nested_value`; duplicate keys are
  `table_duplicate_key`; NaN/Infinity are rejected. Number lexemes are kept
  verbatim (`1.10` stays `1.10`).
- `jsonl`/`ndjson`: one flat object per non-blank physical line; `ref` is a
  `lines` selector for that line.
- Limits raise `table_too_many_rows`, `table_too_many_columns`,
  `table_cell_too_large`. Unsupported kinds raise `table_kind_unsupported`.
  Malformed input raises `table_invalid`. Errors never contain source text.

`ResultTable(columns, rows, row_refs)` is the shared output of operations and
the input of renderers.

### `operations.py`

`run_operation(table: Table, spec: dict) -> OperationResult` and
`validate_spec(spec) -> dict` (returns a normalized copy or raises
`OperationError(code)`). Spec, all keys optional except as noted:

```json
{
  "columns": {"amount": "decimal", "date": "date", "region": "string"},
  "where": {"op": "and", "args": [
    {"op": "ge", "column": "date", "value": "2026-01-01"},
    {"op": "not", "arg": {"op": "is_null", "column": "amount"}}
  ]},
  "group_by": ["region"],
  "aggregates": [
    {"op": "count", "as": "rows"},
    {"op": "sum", "column": "amount", "as": "total"},
    {"op": "mean", "column": "amount", "as": "average"}
  ],
  "select": ["region", "amount"],
  "order_by": [{"column": "total", "direction": "desc"}],
  "limit": 100,
  "on_invalid": "exclude"
}
```

- Column types: `string` (default), `decimal`, `integer`, `date` (ISO
  `YYYY-MM-DD` only), `boolean` (`true`/`false`, case-sensitive). Decimal
  lexemes match `-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?`, and `integer`
  forbids fraction/exponent. No thousands separators, currency symbols, or
  locale parsing. Unknown column names are `operation_unknown_column`.
- Null: `None` and `""` are null for typed columns. For `string` columns only
  `None` is null. Comparisons involving null are false; `is_null` tests it.
- Invalid typed values: with `on_invalid: "exclude"` (default) the row is
  excluded and counted in `lineage.excluded["invalid_<type>:<column>"]`; with
  `"error"` the operation fails with `operation_invalid_value` (no value echoed).
- Predicates: `and`/`or` (`args`, 1-32 items), `not` (`arg`), `eq ne lt le gt
  ge` (`column`, `value`), `in` (`column`, `values` up to 1,000), `is_null`,
  `contains starts_with ends_with` (string columns, `value`, optional
  `"case": "insensitive"` using `casefold`). Depth at most 8, at most 256
  nodes. Comparison values are given as strings or JSON numbers and are parsed
  with the column's type; a value that fails to parse is
  `operation_invalid_literal`.
- Aggregates: `count` (rows), `count_non_null`, `sum`, `min`, `max`, `mean`
  (decimal/integer), `distinct_count` (any type), `min`/`max` also for date and
  string. Aliases default to `<op>_<column>` and must be unique.
- Arithmetic uses `decimal.localcontext` with `prec` large enough that sums of
  bounded inputs are exact, with `Inexact` trapped. An `Inexact` signal is
  `operation_precision_exceeded`, never a silently rounded total. `sum` over
  zero non-null values is null. `mean` is reported as a structured value
  `{"sum": "...", "count": n, "display": "...", "exact": bool}` where `display`
  is rounded half-even to 12 significant digits and `exact` says whether the
  division terminates within that display.
- Output values are strings (Decimals via `str` keeping input scale, e.g. a sum
  of `1.10` and `2.20` is `3.30`) or `None`. Means appear in `ResultTable`
  cells as their `display` string, with the structured form in
  `OperationResult.values`.
- `select` (projection) is used when there are no aggregates; with aggregates
  the output columns are `group_by` columns plus aggregate aliases. `group_by`
  allows up to 4 columns and up to 1,000 groups (`operation_too_many_groups`).
- `order_by` sorts by typed value with nulls last, stable; columns must be
  output columns. `limit` 1-1,000 (default 1,000) applies to output rows;
  `lineage.output_truncated` reports when it cut rows.
- `OperationResult` has `table: ResultTable`, `values` (per-row dict of
  structured mean values, may be empty), and `lineage`:
  `{"source_rows", "scanned", "matched", "excluded": {reason: count},
  "output_rows", "output_truncated", "spec_digest"}`. `spec_digest` is the
  SHA-256 of the canonical JSON of the normalized spec. No sampling: every
  source row is scanned.

### `batching.py`

`fuse_reads(snapshots, requests, *, max_result_bytes, token_budget, estimator=None) -> dict`.

- `snapshots`: mapping of source_id to `SourceSnapshot`. `requests`: 1-16
  `{"source_id", "selector"}` items using the existing exact selectors
  (`select_exact`). Unknown sources are reported per item as
  `source_unknown`, not as a whole-call failure.
- Line selections that overlap or touch within the same source and version are
  merged into one block. Each block lists the request indices it serves.
  Section, JSON-pointer and row selections are merged only when their resolved
  line spans overlap.
- One global budget in request order: a block that fits is returned with exact
  text and `line_ranges`; the rest are listed under `deferred` with their
  original selectors and reason `budget` so the caller can retrieve them
  individually. A block that could never fit alone gets reason `too_large`.
- Output: `{"mode": "batch", "blocks": [...], "deferred": [...], "errors": [...],
  "coverage": "exact selected scopes", "request_count": n}`. Text is never
  clipped.

### `answer_contract.py`

`instruction_pack(version: str = "v1") -> dict` returning `{"version",
"text", "sha256", "characters"}`. The v1 text is a ContextShrink-authored
default of at most 600 characters that leads with the result, keeps required
facts/citations/deliverables complete, cites source references instead of
repeating them, and expands when correctness or the user requires it. It must
never tell the model to skip reasoning or shorten a requested full document.
Unknown versions raise `ValueError("instruction_pack_unknown")`.

### `artifacts.py`

- Renderers (pure): `render_csv(table, *, formula_policy="escape")`,
  `render_json(table)`, `render_markdown(table, *, title=None)`,
  `render_text(table)`. CSV uses RFC 4180 quoting and `\r\n`. Formula policy:
  cells starting with `=`, `+`, `-`, `@`, tab or carriage return are prefixed
  with `'` under `escape`, raise `ArtifactError("artifact_formula_cell")` under
  `reject`, or are left unchanged under `raw`. A leading `-` or `+` followed only
  by a valid decimal lexeme is a number and is not escaped. JSON is an array of
  objects in column order. Markdown escapes `|`, `\` and leading markup, and
  replaces newlines with `<br>`; Markdown and text are display formats, not
  lossless exports.
- `ArtifactStore(base_dir, *, dependency_check, max_artifact_bytes=8 MiB,
  max_total_bytes=128 MiB, max_items=64, ttl_seconds=3600, clock=time.time)`
  owns one session directory created under `base_dir` through
  `staging_fs.PinnedStagingDirectory`, using generated IDs and file names only.
  The model never supplies a path or file name.
- `create(fmt, table, *, title, dependencies, formula_policy="escape") ->
  dict` renders, enforces limits (`artifact_too_large`, `artifact_store_full`),
  writes exclusively and returns a manifest: `{"artifact_id", "format",
  "title", "utf8_bytes", "sha256", "row_count", "columns", "preview_rows"
  (at most 5), "dependencies": [{"source_id", "version"}], "created_at",
  "expires_at"}`. No filesystem path appears in the manifest.
- `read(artifact_id) -> bytes` and `manifest(artifact_id)` call
  `dependency_check(dependencies)` first and raise `artifact_revoked` when it
  returns False, invalidating the artifact. `list()`, `delete(id)`,
  `invalidate_where(predicate)`, `expire()`, and `close()` (removes owned
  files) complete the lifecycle. Expired or deleted IDs raise
  `artifact_unknown`. `export_path(artifact_id)` is for the companion's
  user-operated Save As only and is never model-facing.
- `Dependency(source_id, version, root_key)` is the dependency record.
  Titles are at most 120 characters of printable text.

## Verification

Each E1 module ships its own `tests/desktop_mcp/test_<module>.py`. The
desktop suite, ruff check and ruff format must pass before E2 starts.
