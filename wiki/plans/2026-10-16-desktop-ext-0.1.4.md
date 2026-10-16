# Desktop extension 0.1.4: fewer tool turns per task

## Why

On 2026-10-15 the one-day self test (`st_generated_v1`, Claude Opus, Medium
effort, 48 chats, date reconciled) measured these totals:

| Method | Spend | Correct |
|---|---|---|
| C_normal (files attached) | $1.58 | 12/12 |
| B (ContextShrink, normal) | $8.11 | 12/12 |
| A (ContextShrink, exact only) | $9.52 | 11/12 |

Verdict: `cost_increase_supported` (B − C_normal = +$0.54 per task, 95% CI
0.20 to 0.91). Four lookups in large files cost $6.22 of B's total:

- three order lookups in `orders_export.json`, 11–14× the attached cost;
- one RESTART count in the log, 8.6×.

The stats file showed 504 tool calls, about 21 per chat. Every call is a new
model turn that resends the conversation.

Causes found in the code:

1. **Results are capped at about 2,000 tokens.** `result_token_budget`
   defaults to 2000 (maximum 8000) and `max_result_bytes` to 65536. Large
   files are therefore read in many pages.
2. **Compact mode never compresses large files.** A compact candidate that
   does not fit the budget falls back to exact pages (`policies.py`), so
   `compact_result_count` was 0 after 184 compact requests.
3. **Line and row selectors are capped at 2,000 lines.**
   `{"type":"lines","start":173,"end":6140}` failed with `invalid_arguments`.
4. **A large JSON pointer value errors instead of paging.**
   `/orders` failed with `result_too_large`, because a JSON pointer scope is
   not paginable.
5. **There is no in-file lookup.** `contextshrink_search` finds file names
   only, so finding an ID or counting events means paging through the file.

## Changes

### W1: Bigger results and paging that works (agent A)

- `settings.result_token_budget`: default 32000, allowed 128..64000 (24000 was too small: the extension counts tokens as UTF-8 bytes ÷ 3, so the compacted 74 KB orders file is about 28k estimated tokens).
- `settings.max_result_bytes`: default 262144, allowed 1024..1048576 (unchanged).
- `ResultLimits` (`policies.py`) accepts budgets of 128..64000.
- Every `result_token_budget` field in `contracts.py` (the pydantic models
  and the JSON schemas) allows a maximum of 64000.
- The `LinesSelector` and `RowsSelector` span limit rises from 2000 to 200000.
  Ordering is still checked, and exact paging still applies the budget.
- **JSON pointer paging.** When the pointer selects an array or object, the
  scope is paginable. Each unit is one element (or one member), with its
  exact source bytes and line range, in document order. The result and its
  `next_cursor` follow the existing `present_selected_scope` unit paging. A
  scalar value, or a container small enough to fit, keeps the current
  behaviour. If a single element is larger than the budget, the result is
  still `result_too_large`.
- Keep the companion's `min(8000, ...)` clamps.
- Check that `MAX_FRAME_BYTES` (1 MiB) still holds a full 262144-byte result
  after JSON escaping and envelope overhead. If it does not, lower the default
  `max_result_bytes` to the largest value that fits.

### W2: `find` mode on `contextshrink_open` (agent B)

`mode: "find"` requires a non-blank `query`.

Matching:

- The query is split into terms on whitespace. A double-quoted phrase is one
  term.
- A line matches when it contains every term, compared case-insensitively.
- There is no regex.

Optional argument `context_lines`: an integer from 0 to 50, default 0. It is
valid only with `find`; any other mode returns `invalid_arguments`.

The result:

- `mode: "find"`, `fidelity: "partial_exact_excerpt"`, and coverage
  `"partial (matching lines with context; full document not represented)"`;
- `match_count`: the total number of matching lines in the whole snapshot,
  always complete (`match_count_complete: true`);
- `matches`: blocks `{start_line, end_line, match_lines: [n...], text}`. Each
  block holds exact source lines and is merged when context ranges overlap
  or touch. Blocks are in document order;
- `text`: the blocks rendered as `[[lines a-b]]\n<exact text>`, like focused
  mode;
- paging: blocks that do not fit the budget continue on a signed cursor. The
  cursor carries `tool="open"`, `mode="find"`, the query digest and a block
  offset. `more_available` and `next_cursor` work as in focused mode. A block
  larger than the budget is listed in `omitted_ranges` with its range;
- when nothing matches: `match_count: 0`, empty `matches`, and no error.

Other rules:

- Accounting uses the whole snapshot as the raw equivalent, like focused mode
  (`_finalize_full`).
- With `eval_arm == "raw"`, find is served as exact, like the other modes.
- The usage stats record the mode as `find`. Add `find` to the allowed modes
  wherever modes are listed (measurement, cumulative stats, the server and the
  broker transport).

### W3: Tool descriptions (agent B)

- `contextshrink_open`: lead with when to use `find`. Suggested text: "find:
  with a query, to look up a specific value, ID, name or date, or to count
  lines (returns every matching line with line numbers and the total count;
  set context_lines to see nearby lines). One open normally answers the
  question; results hold large files whole, so do not page through a file to
  look for something." Keep the compact, focused, overview and exact guidance
  short. The mode schema enum gains `find`.
- `contextshrink_search`: "Find approved source files by name. It does not
  search inside files; use contextshrink_open with mode find for that."
- Update the manifest `tools[].description` lines if they mention modes.
- Keep the added tokens small and report the size change of the tools
  listing.

### W4: Self-test method subset (agent C)

- The pack can carry `"methods": ["B", "C_normal"]`. It must be a subset of
  `METHODS` that contains both `B` and `C_normal`. When it is absent, all four
  methods are used, so existing packs and ledgers are unchanged.
- The schedule, report and comparisons use only the pack's methods: phases =
  2 × the number of methods, and only comparisons whose methods are both
  present. Quality regression still compares B with C_normal.
- `make_dataset.py` gains `--study-id` and `--methods`. The kit builder
  passes `--study-id st_generated_v2 --methods B,C_normal`, with the same seed
  and documents.

### Version

`VERSION = "0.1.4"`. Update the version tests.

## Check before release

- Run the full `tests/desktop_mcp` suite and ruff.
- Run a local frozen-build smoke on the generated documents:
  - find `NW-13664` returns the order in one call;
  - find `queue-relay RESTART` returns `match_count` 3;
  - a compact open of `orders_export.json` applies compression in one result;
  - retrieving `/orders` pages instead of failing.
- Re-run the test on the Enterprise laptop: B vs C_normal, 24 chats.
