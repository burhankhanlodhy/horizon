---
title: "ContextShrink Desktop MCP: Phase F documents and packaging"
date: 2026-10-14
status: f1-f2-complete-locally-f3-pending
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
| F1 | PDF/DOCX extraction, block anchors, page/paragraph/table selectors, unsupported detection | complete locally |
| F2 | Frozen-aware entrypoint and worker spawn, broker lifecycle for an installed extension, PyInstaller build, MCPB manifest and pack, local smoke | complete locally |
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

## F2 interfaces

Claude Desktop launches only the MCP server process. Until now the broker was
started by hand in a terminal, and it spawns its worker with
`sys.executable -I -c <bootstrap>`, which a PyInstaller executable cannot run.
F2 makes the runtime work as an installed extension without changing the
developer flow.

### Runtime (`__main__.py`, `broker.py`, `settings.py`, `extension.py`)

- **Frozen worker.** A `worker` subcommand runs `backend_worker.main` with the
  existing worker arguments. `BackendWorker._start` uses
  `[sys.executable, "worker", ...]` when `getattr(sys, "frozen", False)`, and
  keeps the current `-I -c` bootstrap otherwise.
- **Idle shutdown.** `broker_idle_seconds: int = 0` (0 means never; otherwise
  60-86400) is a config field. `BrokerService` records the time of the last
  handled request, and while serving, a watchdog thread stops the broker (the
  same path as Ctrl+C, so worker cleanup and capability revocation run) once
  it has been idle that long.
- **Extension mode.** `serve --extension` takes:
  - `--approved-roots DIR ...` (zero or more; MCPB expands a multi-folder
    setting into separate arguments);
  - `--tool-profile source|compute` (default `source`);
  - `--artifact-root DIR` (empty string means none);
  - `--broker-id` (default `desktop-ext`).

  It builds `DesktopSettings(profile="local", ..., broker_idle_seconds=1800)`.
  It writes them atomically (temp file plus `os.replace`), and only when
  changed, to `%LOCALAPPDATA%\ContextShrink\extension\<broker_id>.json`
  (POSIX: `~/.local/state/contextshrink/extension/`). Then it ensures a broker
  is running: if a `stats` call fails because no broker is reachable, it
  starts one detached (`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP |
  CREATE_NO_WINDOW`, stdio to `DEVNULL`), using `[sys.executable, "broker",
  "--config", path]` when frozen and `[sys.executable, "-m",
  "horizon.desktop_mcp", "broker", "--config", path]` otherwise. It waits up
  to 15 seconds for `stats` to succeed, then serves stdio as usual. Two chats
  starting at once may each try to start a broker. Only one can own the pipe
  and the other exits; the capability is issued only after pipe ownership.
  Any startup failure is reported as a sanitized code on stderr.
- `serve --config` (the developer and pilot path) is unchanged.

### Broker lifetime under the host's process job (decision)

The MCP Python SDK's stdio client starts each server in a kill-on-close
Windows Job Object that does not allow breakaway (`CREATE_BREAKAWAY_FROM_JOB`
is denied), so a broker started from a server process ends when the chat that
started it closes. Claude Desktop may behave the same way; that is unverified.
Escaping the host's job, for example through WMI or a scheduled task, would
defeat the host's process containment, so it is not done. Instead, extension
mode passes a recovery hook to the server. A call that fails with
`broker_unavailable` restarts the broker and is retried once.
`broker_unavailable` now means no request byte was sent; any failure after
sending starts is `broker_disconnected` and is never retried, so a
non-idempotent render cannot run twice. Handles and artifacts from the lost
broker are gone, and Claude needs a fresh search, the same as after a broker
restart. A real two-chat run confirmed that the second chat keeps working after
the first closes.

### Build (`desktop/mcp/extension/`)

- `entry.py`: the PyInstaller entry script, which calls
  `horizon.desktop_mcp.__main__.main`.
- `build_extension.py`:
  - **Build:** PyInstaller `--onedir` with the console bootloader, named
    `contextshrink-desktop`.
    - It bundles `companion_ui/*` as data and collects pywin32, pydantic,
      the MCP SDK, anyio and pypdf.
    - It explicitly excludes the proxy and ML stacks (`torch`,
      `transformers`, `onnxruntime`, `numpy`, `litellm`, `fastapi` and
      similar), plus `horizon.proxy`, `horizon.providers` and other
      non-desktop packages.
    - Build output goes under `desktop/mcp/extension/build/` (git-ignored).
  - **Pack:** write `manifest.json` from `manifest.template.json` with the
    version filled in. Zip it with `server/<onedir contents>` into
    `dist/contextshrink-desktop-<version>.mcpb`, using deterministic order
    and timestamps.
  - **Validate:** before packing, check the required fields, that the entry
    point exists inside the archive, that the tools list matches
    `tool_specs`, and that every argument variable references a declared
    `user_config` key.
  - It never uploads. Publishing goes to the private release bucket in a
    separate, explicit step.
- `manifest.template.json`:
  - `manifest_version "0.3"`, `server.type "binary"`, and `entry_point
    "server/contextshrink-desktop.exe"`.
  - `mcp_config.command "${__dirname}/server/contextshrink-desktop.exe"` with
    `args ["serve", "--extension", "--tool-profile",
    "${user_config.tool_profile}", "--artifact-root",
    "${user_config.artifact_root}", "--approved-roots",
    "${user_config.approved_roots}"]` (the multi-folder value last).
  - `user_config`: `approved_roots` (directory, multiple, required),
    `tool_profile` (string, default `source`) and `artifact_root` (directory,
    optional).
  - `compatibility.platforms ["win32"]`, and `tools` with the seven tool
    names and descriptions.
- Local smoke: run the built executable in extension mode through the MCP SDK
  stdio client against a temporary approved folder, check that it auto-starts
  a broker, then search, open a PDF page, and stop.

Signing (Authenticode for the executable, plus MCPB signing) and the
clean-machine install/uninstall/rollback check are F3 and need the owner's
certificate and a second machine.

## F1/F2 results

- Extraction:
  - PDF gives one block per page. Encrypted files try an empty password, then
    fail with `pdf_encrypted`, and text-free files fail with `pdf_no_text`.
    Pages without text and pages with images are reported.
  - DOCX gives paragraphs, headings with levels, and tables. Zip size and
    ratio limits are checked before decompressing, DOCTYPE and ENTITY are
    rejected, tracked changes are refused, and omitted parts are reported.
  - Page, paragraph and table selectors always return whole blocks. Results
    carry the extractor version and name omitted content in their coverage.
- Known extraction gaps: images nested in PDF form XObjects are not detected,
  so `images_present` can be missed. Only code review covers a DOCX entry that
  lies about its size; the read is bounded and Python's `zipfile` also
  rejects a size mismatch.
- Build: a 59.1 MiB one-folder bundle (178 files) and a 31.6 MB `.mcpb`. The
  build excludes the proxy and ML stacks; `starlette`/`uvicorn` stay because
  the MCP SDK imports them.
- Smoke on the frozen build:
  - **Source profile:** search, PDF open and page retrieve, and DOCX
    paragraph retrieve all worked. The broker and worker were started by the
    executable itself, and the same calls worked from the unpacked `.mcpb`.
  - **Compute profile:** seven tools, exact grouped decimals, a rendered
    artifact and a batch. Results are in `desktop/mcp/extension/README.md`.
- The manifest lists the four default tools with `tools_generated: true`.
  `source` remains the default profile because the compute schemas cost
  about 1,370 extra tokens per chat.
- Verification: `pytest tests/desktop_mcp` plus the MCP dependency-contract and
  CCR HTTP tests report 914 passed, 2 skipped; ruff check and format pass.

## F3 (needs the owner)

- Install the `.mcpb` in Claude Desktop on a clean Windows machine with no
  developer Python, choose an approved folder, and run search, open and a PDF
  page retrieve in two chats. Then uninstall, and confirm that no broker,
  capability file or artifact folder is left behind, and that the native
  workflow is unaffected.
- This also answers the open question of whether Claude Desktop's process job
  behaves like the SDK's.
- Sign the executable (Authenticode) and the extension with the owner's
  certificate.
- Publish only to the private release bucket, as a separate approved step.
- Decide where the companion is launched from for extension users. Today it is
  `contextshrink-desktop.exe companion --config <extension settings path>`.
