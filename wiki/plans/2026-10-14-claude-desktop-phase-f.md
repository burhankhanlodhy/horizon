---
title: "ContextShrink Desktop MCP: Phase F documents and packaging"
date: 2026-10-14
status: f1-documents-in-progress
phase: F
milestones: [M4, M5]
---

# Phase F: digital documents, runtime packaging and installation

Phase F implements M4 (PDF/DOCX extraction) and M5 (distribution) from the
[build plan](2026-10-13-claude-desktop-enterprise-mcp-build-plan.md), sections 8 and 9.

Decisions (2026-10-14, by the owner):

- PDF uses `pypdf==6.20.1` (BSD, pure Python, no dependencies), pinned in the
  `desktop-documents` extra. DOCX is parsed with the standard library's
  `zipfile` and `xml.etree` (expat 2.8.3 includes entity-expansion
  protection), so `python-docx`/`lxml` are not added. `uv.lock` is not
  regenerated in this change: the local uv re-resolved the whole lock, which is
  unrelated churn, so adding `pypdf` to the lock is a follow-up.
- Packaging is a self-contained Windows build: PyInstaller bundles Python and
  the desktop MCP, wrapped as an MCPB extension with a binary server entry, so
  no Python or uv is needed on the target. Installers are never hosted on
  GitHub; distribution uses the private release bucket.

| Stage | Scope | Status |
|---|---|---|
| F1 | PDF/DOCX extraction, block anchors, page/paragraph/table selectors, unsupported detection | in progress |
| F2 | Frozen-aware entrypoint and worker spawn, broker lifecycle for an installed extension, PyInstaller build, MCPB manifest and pack, local smoke | pending |
| F3 | Clean-machine install/uninstall/rollback and signing (needs a second Windows machine and a code-signing certificate) | pending |

"Exact" for PDF/DOCX means exact text in the extracted representation with a
block reference. It is not rendered layout, OCR, or complete capture of
images, charts or text boxes. Scanned PDFs, handwriting and layout-dependent
analysis are out of scope. Unsupported or uncertain files return an
actionable error; there is never a silent empty-document success.

## F1 interfaces

### Shared types (`extractors/__init__.py`, done)

`ExtractedDocument(text, blocks, extractor_version, warnings)`,
`DocumentBlock(kind, index, line_start, line_end, attributes)`,
`ExtractionError(code)`, `extract_document(data, kind)`, and the
`MAX_EXTRACTED_CHARS` (16 MiB) and `MAX_PDF_PAGES` (2,000) limits.

### PDF (`extractors/pdf.py`)

`extract_pdf(data, *, max_pages=MAX_PDF_PAGES, max_chars=MAX_EXTRACTED_CHARS)`.

- Open with `pypdf.PdfReader(io.BytesIO(data))`. A parse failure raises
  `pdf_invalid`. pypdf's logger is held at ERROR during extraction, so no
  document text reaches logs.
- Encrypted: try `decrypt("")` (many PDFs are encrypted with an empty user
  password). If that fails, raise `pdf_encrypted`.
- More than `max_pages` pages: `pdf_too_many_pages`.
- One `page` block per page (index = page number) from `page.extract_text()`.
  Line endings are normalized to `\n`, and each non-empty page's text ends with
  `\n`. A page with no extractable text gets an empty block
  (`line_start > line_end`) and adds `pages_without_text` to the warnings,
  with the page numbers in `attributes["pages_without_text"]` on the first
  block. Pages whose resources include images add `images_present`.
- No extractable text on any page: `pdf_no_text` (likely scanned; OCR is out of
  scope).
- Total characters over `max_chars`: `pdf_text_too_large`.
- `extractor_version = f"pdf-pypdf-{pypdf.__version__}-v1"`.

### DOCX (`extractors/docx.py`)

`extract_docx(data, *, max_chars=MAX_EXTRACTED_CHARS)`.

- Zip safety: at most 2,000 entries, at most 64 MiB declared uncompressed in
  total, `word/document.xml` at most 32 MiB, and no entry with a compression
  ratio above 200:1 unless it is under 1 MiB. Violations are `docx_too_large`;
  a corrupt or non-DOCX zip is `docx_invalid`. Paths are read only by exact
  name. Relationships and external targets are never followed.
- XML: any `<!DOCTYPE` or `<!ENTITY` is rejected as `docx_invalid` before
  parsing; parse with `xml.etree.ElementTree`.
- Body walk in document order:
  - `w:p` becomes a `paragraph` block, or a `heading` block when its
    `w:pStyle` is `Title` or `Heading<N>`, with `attributes
    {"heading_level": N}` (Title is level 0). Paragraphs and headings share
    one numbering.
  - Text comes from `w:t`; `w:tab` becomes `\t`; `w:br` and `w:cr` become
    `\n`; `w:instrText` (field codes) is excluded while field results are kept.
    `w:sdt` content is descended into.
  - Empty paragraphs are skipped and not numbered.
  - `w:tbl` becomes a `table` block (separate numbering, `attributes {"rows",
    "columns"}`). Each row is one line with cells joined by `\t`; a cell's
    paragraphs are joined with a space, and tabs or newlines inside a cell
    become spaces. Nested tables are flattened into their cell and add
    `nested_tables_flattened`.
- Tracked changes (`w:ins`, `w:del`, `w:moveFrom`, `w:moveTo` in the body)
  raise `docx_tracked_changes`, because choosing a view silently would
  misstate the document.
- Warnings:
  - `textbox_content_omitted` for `w:txbxContent`, which is not extracted;
  - `images_present` for `w:drawing` or `w:pict`;
  - `comments_omitted`, `footnotes_omitted`, `endnotes_omitted` and
    `headers_footers_omitted` when those parts exist in the package.
- No paragraphs or tables with text: `docx_no_text`. Over `max_chars`:
  `docx_text_too_large`. No page numbers are invented.
- `extractor_version = "docx-stdlib-v1"`.

### Selectors (`selectors.py`, contracts, transport)

`select_exact(text, selector, *, kind, blocks=())` gains:

- `{"type": "page", "start", "end"}` for `pdf`, with at most 200 pages per
  selector;
- `{"type": "paragraph", "start", "end"}` for `docx`, numbering paragraphs and
  headings together, with at most 2,000 per selector;
- `{"type": "table", "index"}` for `docx`.

These map to the line spans of the matching blocks and paginate by block, so
a page or paragraph is never split. A selection made only of empty pages
returns empty text with a coverage note naming those pages. Out of range is
`selector_out_of_range`; the wrong kind, or missing blocks, is
`selector_format_unsupported`. The MCP `contextshrink_retrieve` and
`contextshrink_retrieve_batch` schemas, and the broker's `_validate_selector`,
accept the three new variants. This intentionally extends the source
profile's retrieve schema.

### Integration (`source_access`, `snapshots`, `local_backend`, `batching`, `policies`)

- `.pdf` and `.docx` join the supported suffixes. `SourceReader.read` keeps
  every existing identity, size and change check, then calls
  `extract_document(raw, kind)` instead of the UTF-8 decode for those kinds.
  `ExtractionError` becomes `SourceAccessError(code)`. `OpenedSource` gains
  `extractor_version` (default `"text-v1"`), `blocks` and `warnings`. The
  digest stays the SHA-256 of the raw file.
- Snapshots carry `blocks` and `warnings`, and the version embeds the
  extractor version.
- Retrieve and fused reads pass the snapshot's blocks to `select_exact`.
  Open and retrieve results for documents add `"extraction":
  {"extractor_version", "warnings", "block_counts"}`, and the coverage text
  says "extracted text" and names omissions when there are warnings.
- Overview for PDF lists pages with their line spans and text/no-text state.
  For DOCX it lists headings. Compact mode falls back to exact for document
  kinds.
- Table operations are not supported on PDF/DOCX
  (`table_kind_unsupported`).
