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

Budget fitting in `policies.py` still measures the full payload. Pages may
now be slightly under-filled, which is conservative and acceptable. Do not
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
