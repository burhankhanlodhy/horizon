# Supported scope of the desktop MCP

This page states what the desktop MCP profile reads, how it presents results,
what limits apply, what it does not do, and what each tool profile costs in
tokens. Every bound is taken from the code cited in its row. Token figures are
estimates; see [Token counts](#token-counts).

## Source formats

Only files under an approved root can be read. Search lists approved source
filenames only. A suffix not in the list below is refused as
`source_unsupported`. Files are opened only as regular files; symlinks,
junctions and other reparse points are refused. Files that are online-only
OneDrive placeholders are refused as `source_unavailable`, so a file must be
downloaded locally before it can be read.

| Suffix | How it is read | Presentation and selectors | Notes |
| --- | --- | --- | --- |
| `.txt` `.md` `.markdown` `.log` `.diff` `.patch` `.paths` `.search` `.conf` `.config` `.ini` `.toml` `.yaml` `.yml` | UTF-8 text, strict decode; a file containing a NUL byte is refused (`source_invalid_text`) | All four modes; `lines` and `markdown_section` selectors | Other encodings are not supported |
| `.json` | UTF-8 text | As above, plus `json_pointer`; `json` compact kind | Parsed into a table for `analyze` and `render` (JSON arrays) |
| `.jsonl` `.ndjson` | UTF-8 text | As above; compacted with the `log` kind | Parsed into a table for `analyze` and `render`, one row per line |
| `.csv` `.tsv` | UTF-8 text | As above, plus `rows` over logical CSV or TSV records | Parsed into a table for `analyze` and `render` |
| `.pdf` | Text extractor, page by page (`pypdf`) | `page` selector (PDF only); `exact` text with a page reference | No OCR. Encrypted PDFs are refused (`pdf_encrypted`). A PDF with no extractable text is refused (`pdf_no_text`). Warnings: `pages_without_text`, `images_present` |
| `.docx` | Standard-library XML extractor for `word/document.xml`, with tables | `paragraph` and `table` selectors (DOCX only) | Documents with tracked changes (`ins`, `del`, `moveFrom`, `moveTo`) are refused (`docx_tracked_changes`). Comments are not extracted (warning `comments_omitted`). Macros, external links and embedded objects are never run or opened |

Documents are extracted once at snapshot time; the snapshot keeps the extracted
text and the block references. Exact text in the extracted representation is
what the tools return. Layout, text boxes and images are not reproduced.

## Selectors

| Selector | Applies to | Bounds |
| --- | --- | --- |
| `lines` (start and end, 1-based, inclusive) | Any text format | Span of at most 2,000 lines |
| `markdown_section` (heading, optional occurrence) | Text formats, matched on Markdown headings | Heading at most 256 characters |
| `json_pointer` | JSON | Document depth at most 64 and at most 100,000 nodes |
| `rows` | CSV and TSV logical records | Span of at most 2,000 records; at most 100,000 records; at most 10,000 columns; a field is at most 1 MiB |
| `page` | PDF only | Span of at most 200 pages |
| `paragraph` | DOCX only | Span of at most 2,000 paragraphs |
| `table` (index) | DOCX only | Exactly one table index |

A line range (`start_line` and `end_line`) is accepted only with `exact` mode.
Any other mode with a line range returns `selector_invalid` (`policies.py`).

## Presentation modes

Each mode works on an immutable snapshot version. Every result states its
`coverage` and, where relevant, its `fidelity`.

| Mode | What it returns | Guarantee | Labels | Limits |
| --- | --- | --- | --- | --- |
| `exact` (default) | A slice of the snapshot text, paged by cursor when it does not fit the result budget | Byte-exact slice of the selected lines at that version (`fidelity: byte_exact`). For PDF and DOCX, the exact extracted text with a block reference | `coverage: complete source snapshot`, or `exact selected lines at immutable snapshot version`; `partial` when paged | Result budget and byte limits below |
| `compact` | A reversible or normalized rewrite for the kinds listed below | Each candidate is checked before it is returned, and the exact snapshot remains available. If the candidate does not save at least max(64 tokens, 5 percent of the raw estimate), the result falls back to exact pages | `fidelity`: `byte_exact` (no change), `reversible_fold` (repeated-line runs or blocks folded, with decoding hints), `normalized_json_whitespace` (JSON whitespace between tokens removed; not byte recovery), `normalized_log_ansi` (ANSI colour removed), `normalized_diff_metadata` (`index` lines removed; not reconstructable) | Kinds: JSON, log (`.log`, `.jsonl`, `.ndjson`), search and paths, text, config (YAML, TOML, INI, conf), diff and patch. A source that already contains a repeat-marker line is returned byte-exact |
| `focused` | Exact passages ranked by lexical relevance to a non-empty `query` | Passages are copied verbatim with line references. Lexical only, not semantic. Coverage is always partial | `coverage`: `partial (selected exact passages; omitted document content)` and similar; `fidelity: partial_exact_excerpt` | Up to 100 candidate passages per query. A query is required (`query_required`). Oversized passages are omitted and their ranges listed |
| `overview` | An inventory of Markdown headings, or of PDF and DOCX headings | Inventory only. It is not a summary and does not interpret the text | `fidelity: partial_inventory`; coverage `partial (heading inventory only; not a document summary)` | No summarization |

## Limits

Defaults are used unless a setting is supplied. Settings are validated at load
time (`settings.py`).

| Limit | Default | Bound | Where |
| --- | --- | --- | --- |
| Approved roots | none | At most 32, absolute | `settings.py` |
| Source file size | 8 MiB | Hard ceiling 25 MiB (`max_source_bytes`) | `source_access.py`, `settings.py` |
| Snapshot bytes per source | 16 MiB | At least the source limit | `settings.py`, `snapshots.py` |
| Snapshot store total | 128 MiB | At least the per-snapshot limit | `settings.py`, `snapshots.py` |
| Snapshots held | 64 | 1 to 4,096 | `settings.py` |
| Snapshot TTL | 3,600 s | 60 to 86,400 s | `settings.py` |
| Result token budget | 2,000 | 128 to 8,000 (estimate) | `settings.py` |
| Result size in bytes | 65,536 | 1 KiB to 1 MiB | `settings.py` |
| Input size | 32,768 | 1 KiB to 1 MiB | `settings.py` |
| Extracted characters per document | 16 MiB | Fixed | `extractors/__init__.py` |
| PDF pages | 2,000 | Fixed; more is refused | `extractors/pdf.py` |
| DOCX archive | 2,000 entries, 64 MiB declared, 32 MiB of document XML, compression ratio 200 | Fixed | `extractors/docx.py` |
| Table for `analyze` and `render` | 200,000 rows, 256 columns, 1,000,000 characters per cell | Fixed | `tables.py` |
| Batch retrieve | 16 requests | Fixed; each request is a `source_id` plus selector | `batching.py` |
| `analyze` | Output rows: default 1,000, maximum 1,000; groups: maximum 1,000; group-by keys: maximum 4; decimal precision: maximum 10,000; predicates: depth 8 and 256 nodes; 1,000 values in `in` | Fixed | `operations.py` |
| Artifacts (`render`) | 8 MiB per artifact; 128 MiB total; 64 artifacts; TTL at most 3,600 s; title at most 120 characters | Fixed; rendering needs an `artifact_root` | `artifacts.py`, `local_backend.py` |
| Artifact formats | `csv`, `json`, `markdown`, `text` | CSV formula cells escaped by default (`escape`; `reject` and `raw` are also available) | `artifacts.py` |
| Catalog | 1,000 entries visited; 100 results per page | Fixed | `source_access.py` |
| Companion staging | 8 MiB per import; 32 items; 64 MiB total per session; request body 20 MiB; TTL at most 3,595 s | Fixed | `companion.py` |
| Pilot records | 64 KiB per record input; 10,000 records; 8 MiB store; 16 MiB export; 1 to 90 days of retention | Fixed | `pilot_metadata.py` |
| Usage stats file | 64 KiB | Larger files are reset | `measurement.py` |
| Broker idle stop (extension) | 120 s after heartbeats stop | Fixed | `extension.py` |

Snapshot and artifact limits are per process and per session. Crossing a limit
returns a stable error code (for example `source_too_large`, `analysis_result_too_large`).

## Non-goals

- No OCR and no scanned or image-only PDFs. A PDF with no text fails with
  `pdf_no_text`.
- No tracked-change DOCX. A document with tracked changes fails with
  `docx_tracked_changes`. Accepting changes is not done.
- No rendered layout fidelity. Extracted text is not a faithful copy of how a
  document looks, and the extractor does not promise that every text box or
  image was captured.
- No hosted service. The package runs local processes only: a named pipe for
  the broker and a companion HTTP server bound to `127.0.0.1`. The package has
  no outbound network code.
- No USD, billing or savings claim. Usage counters record `billing_attribution:
  not_measured` and `usd_savings: not_reported`. Counts are per-call estimates,
  not task totals.
- No semantic search. `focused` ranks by lexical relevance only.
- No non-UTF-8 text and no NUL bytes in text sources.

## Tool profiles

| Profile | Tools | Set by |
| --- | --- | --- |
| `source` (default) | `contextshrink_search`, `contextshrink_open`, `contextshrink_retrieve`, `contextshrink_stats` (4 tools) | Default in `settings.py` and `extension.py` |
| `compute` | The four above, plus `contextshrink_analyze`, `contextshrink_render`, `contextshrink_retrieve_batch` (7 tools) | `tool_profile: compute`; requires the local profile and, for `render`, an `artifact_root` |

Token overhead of the compute profile, from `desktop/mcp/phase-e-eval-report.json`:

| Measure | Source (4 tools) | Compute (7 tools) | Difference |
| --- | --- | --- | --- |
| One-time surface (schemas plus server instructions) | 851 | 2,219 | +1,368 |
| Tool schemas | 795 | 2,138 | +1,343 |
| Server instructions | 56 | 81 | +25 |

The report labels the two arms `raw` (4 tools) and `optimized` (7 tools). The
one-time surface is counted once per chat. The estimate is about 1,370 tokens
per chat, as stated in `extension/README.md`. The default stays `source`.

## Token counts

Token counts are estimates made with `o200k_base` through tiktoken. They are
not Claude's tokenizer and are not Claude usage counts. The report's
`cost_attribution` block says the same (`count_status: estimate_not_claude_count`).
The verified o200k vocabulary cache is read only, and a missing or bad cache
disables the counter; see `DATA-STORES.md`.
