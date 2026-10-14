---
title: "ContextShrink Desktop MCP: Phase E deterministic work and artifacts"
date: 2026-10-14
status: e2-wiring-complete-e3-pending
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
| E1 | Pure core: `tables`, `operations`, `batching`, `answer_contract`, `artifacts` with unit tests | complete locally |
| E2 | Backend/broker wiring; compute profile with `contextshrink_analyze`, `contextshrink_render` and `contextshrink_retrieve_batch`; contracts | complete locally |
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
  Blank lines are not data rows, so they never inflate counts, but later rows
  keep their logical record numbers. JSON rows span the whole document's lines.
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

## E2 interfaces

### Settings (done)

`tool_profile: "source" | "compute"` (default `source`; `compute` requires
`profile: "local"`) and `artifact_root: Path | None` (absolute; must not equal,
contain or sit inside any approved root, compared case-insensitively on
Windows). Both are config fields. The source profile's four tools are
unchanged. The ledger accepts the three new tool names and the modes
`computed`, `artifact` and `batch`.

### Tools (compute profile only)

The plan proposed two compute tools. Fused reads get a third tool rather than
an either/or `retrieve` schema, because hosts handle top-level `oneOf` poorly.

| Tool | Arguments | Result |
|---|---|---|
| `contextshrink_analyze` | `source_id`, `operation` (object, see `operations.py`), optional `result_token_budget` | computed table, lineage, coverage |
| `contextshrink_render` | `source_id`, `format` (`csv`/`json`/`markdown`/`text`), `title`, optional `operation`, optional `formula_policy` (`escape`/`reject`; `raw` is never model-facing) | artifact manifest only |
| `contextshrink_retrieve_batch` | `requests` (1-16 of `{source_id, selector}`), optional `result_token_budget` | `fuse_reads` payload |

Annotations: analyze and retrieve_batch are read-only and idempotent. Render
is not read-only (it creates a local file), is not destructive, and is not
idempotent; `openWorldHint` is false for all three.

### Backend (`LocalBackend`)

`LocalBackend(..., artifact_root: Path | None = None)` plus:

- `analyze(source_id, operation, *, result_token_budget=None) -> dict`
- `render(source_id, *, fmt, title, operation=None, formula_policy="escape") -> dict`
- `retrieve_batch(requests, *, result_token_budget=None) -> dict`
- `close() -> None` closes the artifact store (idempotent).

Every method takes the backend lock first and only then calls the artifact
store, so the lock order is always backend before store. The store's
`dependency_check` re-runs `_get_authorized_snapshot` for each dependency and
requires the same version and root key; a worker restart drops snapshots and
therefore revokes artifacts. The store is created lazily on the first render.
Parsed tables may be cached per `(source_id, version)` in a small LRU (at
most 4 entries).

Analyze payload: `{"tool": "analyze", "source_id", "version", "kind",
"operations_version", "columns", "rows", "row_refs", "values", "lineage",
"coverage": "computed over every parsed row of this source version",
"accounting"}`. When it exceeds `max_result_bytes` or the token budget it
raises `analysis_result_too_large` with metadata `{"source_id", "version",
"output_rows"}`, never a truncated table. Render payload: `{"tool": "render",
"artifact": <manifest>, "lineage": <lineage or {"source_rows": n}>,
"delivery": "stored_locally_not_in_result"}`, and `render_unavailable` when no
`artifact_root` is configured. Table, operation, artifact and batch errors map
to `LocalBackendError` with their own codes. Each call records one ledger event
(`computed`, `artifact` or `batch` mode) with no source text, spec or title.

### Broker wire operations

`analyze` args `{source_id, operation}` + optional `result_token_budget`;
`render` args `{source_id, format, title}` + optional `operation`,
`formula_policy`; `retrieve_batch` args `{requests}` + optional
`result_token_budget`. Validation is closed: `source_id` values start with
`cs1_`, `operation` is an object, `format` and `formula_policy` are from the
allowed sets, `title` is a string of 1-120 characters, `requests` has 1-16 items
of exactly `{source_id, selector}` with selectors checked by the existing
`_validate_selector`. Unknown keys fail. `BrokerClient` gains `analyze`,
`render` and `retrieve_batch` with the backend's signatures.

## E2 review and end-to-end results

A scratch probe ran a real broker process, the real MCP stdio server and an
SDK client against a synthetic 5,000-row CSV with `tool_profile: "compute"`:

- The compute profile listed seven tools; the source profile's four schemas are
  identical to `main`.
- A grouped count/sum/mean over all 5,000 rows returned exact totals with an
  estimated 501 delivered tokens against a 44,400-token raw-equivalent payload
  for the same source. This is one synthetic per-call counterfactual, not a
  task-level or billing saving.
- Render produced Markdown and a 5,000-row CSV (88 KB) while returning only a
  manifest; no filesystem path appeared in any result.
- `retrieve_batch` merged two overlapping requests into one block and reported
  an unknown source per item.
- A 5,000-row projection returned `analysis_result_too_large` with
  `output_rows`, never a truncated table.

The probe found two defects, both fixed with tests:

1. The worker dropped error metadata, so `analysis_result_too_large` (and the
   existing exact-read `result_too_large`) reached clients without
   `source_id`/`version`/`output_rows`. The worker now forwards an allowlisted
   set of opaque identifiers and counts.
2. Artifact files survived broker shutdown. The broker hard-killed its worker
   even on a clean stop, Ctrl+Break was not handled, and a console interrupt
   also reached the worker. Now the broker closes the worker's input and waits
   up to three seconds before any kill, handles SIGBREAK like SIGINT, and the
   worker ignores console interrupts. As a crash backstop, a new artifact
   store removes generated artifact files older than the one-hour maximum
   lifetime (plus a margin) from earlier sessions, without following links
   or deleting recursively.

Verification: `pytest tests/desktop_mcp` plus the MCP dependency-contract and
CCR HTTP tests report 638 passed, 1 skipped; ruff check and format pass.

## E3 interfaces

### Role separation (`roles.py`)

Broker clients were not role-separated: any same-user client could send any
operation. E3 adds a broker-issued companion capability. It is a secret the
broker generates, not a role a client asserts, so the existing rule that
requests carry no `role` field still holds.

- `COMPANION_OPERATIONS = frozenset({"artifact_list", "artifact_read",
  "artifact_delete"})`.
- `capability_path(settings, *, base_dir=None) -> Path`: under
  `%LOCALAPPDATA%\ContextShrink\broker` (POSIX: `~/.local/state/contextshrink/broker`),
  named from a SHA-256 of the user SID and `broker_id`, so no identifier is
  readable from the name. `base_dir` exists for tests.
- `issue_capability(settings, *, base_dir=None) -> str`: 32 random bytes,
  URL-safe base64 (43 characters), written to a new file readable and writable
  only by the current user (Windows DACL with the user SID and LocalSystem,
  as the pipe uses; POSIX mode 0600 in a 0700 directory). A stale file from a
  crashed broker is replaced; only one broker can own the pipe name.
- `load_capability(settings, *, base_dir=None) -> str` raises
  `BrokerError("companion_capability_unavailable")`.
- `revoke_capability(settings, token, *, base_dir=None)` deletes the file
  only when its contents still match `token`.

The broker issues the capability before serving and revokes it in the same
`finally` that stops the worker. Request frames may carry an optional
`"capability"` key (a 43-character string). Companion operations without a
matching capability, compared with `hmac.compare_digest`, fail with
`operation_unauthorized`. A capability on any other operation is rejected
the same way, so clients cannot probe with it. The MCP adapter's
`BrokerClient` never loads or sends a capability; only the new
`CompanionBrokerClient` does.

Threat model: this keeps companion-only operations out of the MCP adapter,
which is the path Claude drives, and away from other users. It cannot stop
arbitrary code already running as the same user, which can read the user's
files anyway.

### Artifact operations

| Wire operation | Args | Worker -> `LocalBackend` |
|---|---|---|
| `artifact_list` | `{}` | `artifact_list() -> {"artifacts": [manifest, ...]}` (empty when no store exists) |
| `artifact_read` | `{artifact_id, offset, length}` | `artifact_read(artifact_id, offset, length) -> {"artifact_id", "offset", "data_base64", "total_bytes", "sha256", "eof"}` |
| `artifact_delete` | `{artifact_id}` | `artifact_delete(artifact_id) -> {"deleted": bool}` |

`artifact_id` is 32 lowercase hex characters, `offset` is an int from 0, and
`length` is from 1 to 393,216 (384 KiB, about 512 KiB as base64) so a chunk
fits the 1 MiB frame. Every call goes through the store, so dependency
revocation, expiry and the hash check apply. Revoked or expired artifacts
return `artifact_revoked`/`artifact_unknown` and drop out of the list.
`CompanionBrokerClient(settings)` offers `list_artifacts() -> list[dict]`,
`read_artifact(artifact_id) -> bytes` (reads every chunk, then checks the
total length and SHA-256 against the first chunk's values, raising
`BrokerError("artifact_unavailable")` on mismatch) and
`delete_artifact(artifact_id) -> bool`.

### Companion artifact browser

New authenticated JSON routes on the existing companion, with the same Host,
Origin, capability-header and framing checks as the other routes:

- `GET /api/artifacts`: manifests.
- `GET /api/artifacts/<id>/download`: the verified bytes with
  `Content-Disposition: attachment` and a generated file name (title reduced
  to `[A-Za-z0-9 ._-]`, at most 80 characters, plus the format's extension),
  a format-specific `Content-Type` (`text/csv`, `application/json`,
  `text/markdown`, `text/plain`, all `charset=utf-8`), `nosniff`, and
  `no-store`.
- `DELETE /api/artifacts/<id>`.

Without a broker config these routes return `artifacts_unavailable`. The UI
lists the title, format, rows, size and expiry. It previews `preview_rows` as
a text-only table (`textContent`, never `innerHTML`), and a Download button
fetches with the session header and saves through a temporary object URL.
The user's browser chooses the destination, and no server code writes to a
path it was given.

### Evaluation (`desktop/mcp/phase_e_eval.py`)

A synthetic in-memory SDK A/B evaluation in the style of `phase_c_eval.py`.
Raw arm: source profile with exact reads only. Optimized arm: compute
profile. Each arm counts its own enabled schemas and instructions once (the
compute schemas are larger) plus every argument and result. Tasks:

1. An exact grouped total over a several-thousand-row CSV. Raw arm: page
   through the whole file with exact reads. Optimized: one analyze call.
2. A full-table deliverable. Raw arm: the table the model would have to
   generate, estimated as output tokens and labeled as output. Optimized:
   one render call plus its manifest.
3. Six exact ranges. Raw arm: six retrieves. Optimized: one retrieve_batch
   call.
4. A tiny no-gain case, where a small source and a one-row answer are
   expected to cost more with compute schemas.

The report includes per-task and total estimated tokens per arm, call counts,
the fact that every answer matches a deterministic expected value, and the
same limits as Phase C: estimates rather than Claude counts, no JSON-RPC
framing, no hidden host context, and no USD.

## E1 review decisions

- Only columns an operation actually uses are type-checked. Predicate columns
  are checked first, so a row the filter rejects is `not_matched` rather than
  `excluded` for an invalid value it never needed, and `on_invalid: "error"`
  never fires for it. Lineage still satisfies
  `matched + sum(excluded) + not_matched == scanned == source_rows`.
- Comparison literals sent as JSON numbers pass through a binary float; send
  high-precision literals as strings. Distinct counts and group keys compare
  typed values, so `1.0` and `1.00` are one value shown in first-seen form.
- `fuse_reads` gives back the most recently admitted blocks when late
  deferral entries would exceed the budget, instead of failing the call.
- `render_json` rejects duplicate column names (`artifact_columns_invalid`)
  rather than dropping values. `ArtifactStore.read` hashes what it opened and
  returns nothing unless it matches the recorded SHA-256.
- E1 verification: `pytest tests/desktop_mcp -q` reports 527 passed, 1 skipped;
  ruff check and format pass. 200,000 CSV rows parse and aggregate (grouped
  count/sum/mean) in about 0.5 seconds locally.

## Verification

Each E1 module ships its own `tests/desktop_mcp/test_<module>.py`. The
desktop suite, ruff check and ruff format must pass before E2 starts.
