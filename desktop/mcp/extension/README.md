# contextshrink-desktop extension (MCPB build)

This directory packages the desktop MCP server as a self-contained Windows
Claude Desktop extension (`.mcpb`, a zip with `manifest.json` at its root).
The frozen executable runs the same `serve --extension` runtime as
`python -m horizon.desktop_mcp`, and it starts its broker and worker by
re-entering its own `broker` and `worker` subcommands.

Files:

- `entry.py`: PyInstaller entry script. It calls
  `horizon.desktop_mcp.__main__.main` with `multiprocessing.freeze_support()`.
- `build_extension.py`: `build`, `validate`, `pack` and `all` subcommands.
- `manifest.template.json`: the MCPB manifest. `pack` writes its version.

Build output goes to `build/` and `dist/` in this directory. Both are
git-ignored. Nothing here uploads or publishes an artifact. Installers are not
hosted on GitHub.

## Commands

Run from the repository root with the project virtual environment:

```powershell
.venv\Scripts\python.exe desktop\mcp\extension\build_extension.py build
.venv\Scripts\python.exe desktop\mcp\extension\build_extension.py validate
.venv\Scripts\python.exe desktop\mcp\extension\build_extension.py pack --version 0.1.0
.venv\Scripts\python.exe desktop\mcp\extension\build_extension.py all --version 0.1.0
```

- `build` runs PyInstaller in `--onedir --console` mode and writes
  `build\dist\contextshrink-desktop\`. It adds `companion_ui/*` as data,
  collects `horizon.desktop_mcp`, and passes the hidden imports for pywin32,
  pypdf, the MCP SDK server modules and anyio. It excludes the proxy, ML and
  cloud stacks that the desktop path does not import.
- `validate` checks the required manifest fields, the `server.mcp_config`
  shape, that every `${user_config.X}` in `mcp_config` is declared and every
  declared key is used, `compatibility.platforms == ["win32"]`, and that the
  `tools` names equal `tool_specs("compute")`. When the build output exists, it
  also checks that the entry point file is present.
- `pack` refuses to run if validation fails. It writes
  `dist\contextshrink-desktop-<version>.mcpb` with `manifest.json` first and
  every onedir file under `server/`. Entries are sorted and carry the fixed
  timestamp 1980-01-01 with ZIP_DEFLATED, so the same inputs give the same
  bytes and sha256.

The default version is `VERSION = "0.1.0"` in `build_extension.py`.

## User configuration

- `approved_roots` (directory, multiple, required): folders the server may
  search and read. MCPB expands it into separate `--approved-roots` arguments.
- `tool_profile` (string, default `source`): `source` or `compute`.
- `artifact_root` (directory, optional): the folder that `render` writes to.

## Local smoke results

Run on 2026-10-14 on Windows 11 with PyInstaller 6.22.3, Python 3.12.14, mcp
1.30.0 and pypdf 6.20.1.

- Build: `build\dist\contextshrink-desktop\` is 59.1 MiB (61,925,660 bytes, 178
  files). The executable is `contextshrink-desktop.exe`, about 20 MB.
- Pack: `dist\contextshrink-desktop-0.1.0.mcpb` is 31,594,852 bytes, with 179
  entries (`manifest.json` plus 178 `server/` files). sha256
  `94551b8d1594b624057f8a0319d837f84c00fd4faf17b7eaa96789f30e3b9d31` (after
  the persistent usage stats and package-metadata changes).
  Determinism (same inputs, same bytes) is covered by a unit test on a fake
  onedir; the real build was packed once.
- Step 1, `contextshrink-desktop.exe --help`: exit 0, usage text printed.
- Step 2, frozen stdio session with the MCP Python SDK client: `serve
  --extension --broker-id smoke-<id> --approved-roots <tmp>`. The fixtures in
  the approved folder were `note.txt`, a 2-page PDF and a 1-paragraph DOCX.
  - `tools/list`: the four `source` tools (`search`, `open`, `retrieve`,
    `stats`).
  - `search` (empty query, limit 10): 3 hits (`note.txt`, `sample.pdf`,
    `sample.docx`), coverage `complete`, skipped 0.
  - `open` on `sample.pdf` (exact): no error, contains "Smoke page one alpha".
  - `retrieve` PDF `page 1..1`: no error, contains "Smoke page one alpha" and
    not "Smoke page two beta".
  - `retrieve` DOCX `paragraph 1..1`: no error, contains "Smoke docx paragraph
    gamma".
  - `open` on `note.txt`: no error.
  - The broker auto-started during the session (one `contextshrink-desktop.exe
    broker` process was seen).
- Step 3, broker cleanup: none left. The broker ended when the stdio session
  closed, which is the host's kill-on-close job behavior described in the
  runtime notes. The cleanup step only kills processes whose command line
  contains this run's broker id, and it found nothing to kill.
- Step 4, the unzipped `.mcpb` (`server\contextshrink-desktop.exe` run from the
  extraction directory) with `search` only: no error, the same 3 hits, and no
  broker left after the session.

The smoke harness is kept outside the repository. It uses the same MCP SDK
stdio client pattern as `tests/desktop_mcp/test_extension_runtime.py`.

- Compute profile on the frozen build (`--tool-profile compute
  --artifact-root <tmp>`): `tools/list` returned 7 tools. A grouped `analyze`
  returned exact decimals (`north` total `4.00` from `3.10 + 0.90`, mean
  `2.00`). `render` wrote a JSON artifact (139 bytes), and `retrieve_batch`
  returned one block. This confirms that the size exclusions below do not
  break the compute path.

Not covered by the smoke: the companion UI. It is bundled as data but was not
opened.

## Usage stats

Ask Claude "show ContextShrink stats" to see the counters. The stats result
has two usage blocks:

- `usage`: per-call observations kept in the running worker. They reset when
  the broker or worker restarts.
- `cumulative`: totals kept in a local file, so they survive restarts on this
  machine. It reads `{"persistence":"off"}` when the file is not in use.

The cumulative numbers are per-call estimates of the inner tool results:
call counts by tool, mode and status, error and fallback counts, and summed
token and UTF-8 byte estimates. Token counts are offline estimates, not
Claude's tokenizer. They are not task totals, measured savings or billing
data, and no USD figure is produced. The file holds counters only: no paths,
queries, source text or per-call timestamps.

Where the file lives: `stats\<broker-id>.json` beside the `extension` folder
under the ContextShrink base. By default that is
`%LOCALAPPDATA%\ContextShrink\stats\`. When `CONTEXTSHRINK_EXTENSION_HOME` is
set, the same sibling rule applies to its parent folder. If that stats folder
would sit inside an approved folder (for example, your whole profile), the
file is not written and `cumulative` reads `persistence: off`.

The file is a plain local file. Anyone with access to it can edit it, so it is
not evidence.

To reset, stop the running broker first: it ends when the chat that started
it closes, or after 30 minutes idle. Then delete the stats file. A running
broker keeps its totals in memory and rewrites the file on its next call.

## Known gaps

- The manifest lists the four default (`source`) tools and sets
  `tools_generated: true`, because choosing `tool_profile: compute` adds
  `analyze`, `render` and `retrieve_batch` at runtime. `source` stays the
  default: the compute schemas cost about 1,370 extra tokens in every chat
  (see the Phase E evaluation).
- Excluded for size: `botocore`, `boto3`, `s3transfer`, `hf_xet`, `tokenizers`,
  `huggingface_hub`, `fsspec`, `tree_sitter*`, `zstandard`, `safetensors`,
  `aiohttp` and `websockets`. Neither the source nor the compute path loads
  them; both smokes passed on the frozen build.
- `cryptography` is kept because pypdf uses it for encrypted PDFs.
- Signing (Authenticode and MCPB signing) and the clean-machine install check
  belong to F3.
