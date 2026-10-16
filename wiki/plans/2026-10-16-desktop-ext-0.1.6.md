# Desktop extension 0.1.6: model-facing results without bookkeeping

## Why

Measured on the session-test documents (2026-10-16), most of each `find`
result is bookkeeping rather than file text:

| Result | Total | File text |
|---|---|---|
| Order lookup | 2,989 B | 43% |
| RESTART count | 2,216 B | 21% |
| Config lookup | 2,517 B | 36% |

Each result carries about 24 fields. The largest is a roughly 700-byte
`accounting` block that only our own stats use. In a long chat every tool
result stays in the history and is re-read on every later turn, so this
overhead is paid again and again.

## Change

### A model-facing projection

Add `model_view(payload) -> dict` in one place (a new
`horizon/desktop_mcp/model_view.py`). It turns a full open or retrieve
payload into what Claude sees.

**Kept**, in this order:

- `source_id`
- `mode`
- `requested_mode`, only when it differs from `mode` (it signals a fallback)
- `coverage`
- `fidelity`, only when it is not `byte_exact`
- `compression_applied` and `compression_transform`, only when compression was
  applied, together with `compression_encoding_version` and
  `compression_decoding_hint`
- `line_count`
- `selected_lines`, only when it is not null
- `match_count` (find)
- `headings` (overview)
- `omitted_ranges`, only when it is non-empty
- `warnings`, from `extraction.warnings`, only when non-empty
- `text`
- `more_available` and `next_cursor`, only when `more_available` is true

**Dropped:** `accounting`, `approved_local_files`, `byte_count`, `digest`,
`extractor_version`, `policy_version`, `version`, `kind`,
`result_token_budget_applied`, `context_lines`, `match_count_complete`,
`matches`, `line_ranges` (the `[[lines a-b]]` headers in `text` already show
them), `selector`, `tool`, `returned_text_is_raw`, `eval_arm`, and `extraction`
(its warnings are kept).

Other rules:

- Error payloads are unchanged.
- A field not listed above is dropped by default, so new fields never reach
  the model by accident. Add a test that fails when a payload key is neither
  kept nor explicitly dropped.

### Where it applies

- The MCP server serializes `model_view(payload)` for `contextshrink_open` and
  `contextshrink_retrieve` results (`server.py` `_result`).
- Search, stats and the compute tools are unchanged in 0.1.6.
- The result-size check (`max_result_bytes`) runs on what is actually sent.
- The broker and worker still carry the full payload. The stats observation
  (`local_backend._observe`) still reads the full payload's accounting.

### Honest accounting

The accounting must describe what the model receives.
`measurement.finalize_accounting` measures `model_view(payload)` as delivered,
and `model_view(raw_payload)` as the raw equivalent. In both cases the
accounting block itself is excluded, because the model never sees it.

This keeps the existing rule: an exact read never shows tokens avoided, since
both sides project the same way. Removing bookkeeping is not counted as a
saving; it simply shrinks both sides.

- Check `tests/desktop_mcp/test_honest_accounting.py` and the measurement
  tests, and update the expected numbers deliberately.
- Report before and after delivered sizes for the three cases in the table.

### Paging

Budget fitting in `policies.py` is unchanged, but the bytes it checks are the
accounting's delivered size, which is now the projected size (`model_view`).
Pages are therefore filled against what the model receives, and the worker
checks the same projected bytes, so a page that fits is never rejected later.
Fitting still reserves room for the full accounting block, so pages can be
under-filled by about a kilobyte. That is conservative and acceptable. Do not
change the fitting logic in 0.1.6.

### Version

`VERSION = "0.1.6"`. Update the version tests: in the lifecycle cases 0.1.5 is
older (True), and 0.1.6 and 0.1.6.0 are not (False).

### Tests

- the projection keeps and drops what is listed;
- an unknown key is dropped;
- an end-to-end MCP open result has no `accounting`;
- cursor paging still works through the projection, and so does retrieve
  with `source_id` from the projection;
- an exact read shows no saving;
- the stats file still counts the calls.

## Review findings

An independent review of 0.1.6 (commit 42e4feb9) found the following. Each
item was fixed and has a test.

- **Analyze accounting (high).** Analyze results are not model-projected, but
  `finalize_accounting` measured the projection and so reported a saving of
  about 99% that did not exist. `finalize_accounting` now takes
  `project: bool = True`; analyze passes `project=False` and is measured whole,
  as before 0.1.6. Test: `test_analyze_accounting_measures_the_full_result_as_before_0_1_6`.
- **Worker size check (high).** The worker rejected open and retrieve results
  whose full JSON exceeded `max_result_bytes`, but policy fitting measures the
  projected size, so pages up to about a kilobyte over the limit were rejected.
  The worker now checks the projected bytes; the full frame is still bounded by
  `MAX_FRAME_BYTES`. The worker and the policy measure the same bytes, so
  `_observe` cannot record a success for a result the worker then rejects.
  Tests: `test_worker_accepts_every_projected_exact_page` (3000 lines, limits
  65536 / 32000 and 16384 / 8000) and
  `test_worker_size_check_uses_the_projection_not_the_full_payload`.
- **Structured retrieve boundaries (medium).** Retrieve results lost their unit
  boundaries. `selector` and `line_ranges` are now kept for retrieve results
  only; find and focused still carry `[[lines]]` headers instead. Test:
  `test_structured_retrieve_keeps_unit_boundaries` (PDF pages, DOCX table, CSV rows).
- **Block counts (medium-low).** `extraction.block_counts` is lifted to a
  top-level `block_counts` when non-empty, so the model knows DOCX table counts.
  Test: `test_document_block_counts_reach_the_model_view`.
- **Cursor continuation (low).** `kind` is always kept. `context_lines` is kept
  when a find used context, so the model can repeat it with `next_cursor`. The
  open schema is unchanged: a description sentence pushed the tool-spec token
  growth to the 200-token limit in `test_mode_guidance.py`, so it was not added.
  Tests:
  `test_kind_is_always_kept` and
  `test_context_lines_are_kept_only_when_find_used_context`.

Known small accounting asymmetries, left as they are:

- Exact paged reads show a small cost. The raw side excludes `next_cursor`, as
  it did before 0.1.6, so a paged read is never reported as a saving.
- Compact shows about 26 bytes of label effect, because `requested_mode` is
  dropped on the delivered side when it equals `mode`, while the raw side keeps it.
