# Desktop extension: edit-in-place workspace

Status: spec (2026-10-16). It is not built. Building it waits on the 0.1.7
session-test result.

## Why

In a coding chat, the expensive tokens are Claude's own:

- **Output.** Writing or rewriting code is billed at the output rate, about 5×
  input. A 300-line file is about 4k tokens. Ten "rewrite the file with this
  change" turns cost about 40k output tokens.
- **History.** Every printed version stays in the chat, and each later turn
  re-reads it.

An MCP extension cannot change how Claude writes prose. It can give Claude a
place to keep the code and a way to change it **by edits**. Then:

- each change costs a few hundred output tokens instead of the whole file;
- the chat history holds small edit calls and small confirmations, not
  repeated copies of the file;
- Claude reads back only what it needs, with `find`, `retrieve` or `compact`.

This is an estimate until it is measured (see Measurement).

## Baselines we must beat (be honest about them)

1. **Plain Desktop chat:** Claude prints code in replies.
2. **Desktop artifacts:** Desktop can already update an artifact in place
   (partial updates), so code kept in artifacts already avoids some full
   rewrites.
3. **Anthropic's Filesystem extension**, the reference MCP server, which already
   offers `write_file` and `edit_file`.

Our edge has to come from what the others lack:

- results that stay small (the `model_view` projection, with no file content
  echoed back);
- reads that keep history light (`find`, `compact` and exact ranges, shared
  with the read tools);
- version checks that prevent silent clobbering;
- local version history with restore;
- one coherent read and write tool set.

If the measurement shows no gain over baseline 2 or 3, we do not ship it as a
cost feature.

## Scope

- A new optional setting, `workspace_root`. It is one directory that the
  extension may **read and write**, and it is added to the read roots.
- When `workspace_root` is unset, nothing changes: the extension stays
  read-only, there are no new tools, and the tool listing does not grow.
- Text files only (UTF-8). There is no delete, no rename, no shell and no
  execution.

## Settings and manifest

- `user_config.workspace_root`: type `directory`, optional, title "Workspace
  folder (read and write)". Its description warns that Claude can create and
  change files there.
- Pass it as `--workspace-root`. When it is present, the server adds the write
  tools. The tool list is fixed for a given configuration and never changes
  within a chat.
- Validation, following the `artifact_root` rules in `settings.py`:
  - the path must be absolute and must be an existing directory;
  - it must not equal, contain or sit inside the artifact root;
  - it must not contain the extension's own data folders (stats, runtime,
    history);
  - it may overlap the approved read roots, since the user may want to work on
    a project folder that is also readable;
  - a drive root (`C:\`) or the user's profile root is refused, because that is
    too broad.

## Tools (two new, both `readOnlyHint: false`)

### `contextshrink_write`

Creates a file, or replaces its whole content.

Arguments:

- `path`: relative to the workspace.
- `content`: the full UTF-8 text.
- `expected_version`:
  - required when the file exists: the `version` from the last open or write
    result;
  - omitted to create a new file.
- `restore_version` (optional, used **instead of** `content`): restores a
  version from the local history. It still needs `expected_version` for the
  current file.

Errors:

- the file exists and no `expected_version` was given: `file_exists`;
- the version does not match: `file_changed`, which includes the current
  `version` and `line_count` but no content;
- other errors use the same codes as edit.

### `contextshrink_edit`

Changes an existing file by exact text replacement.

Arguments:

- `path`, and `expected_version` (required).
- `edits`: 1 to 32 items `{old, new, occurrences?}`.
  - `old` must appear exactly `occurrences` times (default 1) in the text
    produced by the earlier edits of the same call.
  - Edits are applied in order. The whole call succeeds or nothing is written.
- Errors:
  - `edit_not_found`, with the edit index;
  - `edit_not_unique`, with the edit index and up to 10 matching line numbers,
    but no content;
  - `file_changed`;
  - `result_too_large`, when the new file would exceed the size limit.

### Result of both tools (model view, small by design)

- `path`, the new `version`, `line_count`, `byte_count`.
- `changed`: up to 16 `{start_line, end_line}` ranges in the new file.
- `preview`: the changed lines plus 2 lines of context around each range, as
  `[[lines a-b]]` blocks. It is capped at about 1,500 tokens in total.
  - Past the cap, it says `preview_truncated: true`, and Claude uses `retrieve`
    to read more.
  - The whole file is never echoed.
- `history_id` of the previous version, so it can be restored.

## Reading the workspace

The existing tools read the workspace like any approved root: `search`, `open`
(exact, compact, focused, overview, find) and `retrieve`.

Every open or retrieve result for a workspace file carries `version`, the short
sha256 of the bytes. Keep it in `model_view` for workspace files only, so the
model has the value to pass as `expected_version`.

A write or edit makes the snapshot store's copies of that path stale. The next
open snapshots the new bytes. Cursors bound to the old version fail with
`cursor_mismatch`, as today.

## Safety

- **Path confinement.** Reuse `source_access` path checks on every component:
  - no absolute paths, no `..` and no drive letters;
  - no alternate data streams (`:`);
  - no reserved Windows names, and no trailing dots or spaces;
  - no reparse points or junctions anywhere on the path, checked
    before the write and on the final path. The open uses no-follow semantics
    where the platform allows.
  - Creating new directories is allowed, up to 8 levels deep, inside the
    workspace only.
- **Atomic writes.** Write to a temporary file in the same directory, fsync
  it, then `os.replace` it.
  - On Windows a target held open by an editor can fail. Retry 3 times over
    300 ms, then return `file_busy`.
  - Never leave a partial file behind.
- **Limits:**
  - at most 2 MiB per file;
  - at most 500 files changed per workspace per day (`limit_reached`);
  - text must be valid UTF-8 with no NUL bytes;
  - keep the original line-ending style when editing (CRLF stays CRLF);
  - keep or omit the final newline exactly as `new` says.
- **History.** Before any overwrite, copy the previous bytes to
  `%LOCALAPPDATA%\ContextShrink\workspace-history\<workspace-key>\`. Keep the
  last 50 versions per file and at most 200 MiB per workspace, pruning the
  oldest first. The history folder is never inside the workspace or a read
  root.
- **Permissions.** The write tools set `readOnlyHint: false`, `destructiveHint:
  false` (every change is restorable) and `idempotentHint: false`. Claude
  Desktop asks the user before calls, according to the user's tool-permission
  setting.
- **Audit.** Append metadata only to the stats file: the tool, the result
  status, bytes before and after. Never paths, content or queries, as with the
  read stats.

## Guidance (tool descriptions)

The descriptions should be short and cost-aware. They must not grow the
listing by more than about 300 tokens when the workspace is set.

- **Write:** "Create a file in the workspace, or replace it. Use for new
  files. To change an existing file, use contextshrink_edit."
- **Edit:** "Change a workspace file by exact replacements. Send only the
  changed text, not the whole file, and use expected_version from the last
  open or write. After an edit, rely on the result preview; read more with
  find or retrieve only when needed."
- **Server instructions** (sent once): "When the user is working on files in
  the workspace, keep the code in files and describe changes briefly; do not
  print whole files in chat unless the user asks."

## Measurement (before claiming savings)

A self-test pack, `kind: self_test_coding_pack`, run by the same wizard and
kit, with two chats on the same task:

- **C:** connector off. Claude works in chat or artifacts as it normally would.
- **B:** connector on, with the workspace set to the kit's empty `workspace`
  folder.

The task script has a fixed start ("Write a Python module `inventory.py` that
…") and 8 fixed change requests pasted one at a time: add a function, rename a
field, fix a seeded bug, add validation, and so on.

The verdict is correctness, then cost:

- the final code must pass a hidden test suite;
- the kit's frozen app runs the tests through `self-test check-code
  <file>`, in a subprocess with a timeout. The code runs on the user's own
  machine and is Claude's output from the user's own chat;
- in C, the user saves Claude's final code to `final\inventory.py` (the wizard
  says how);
- in B, the final file is `workspace\inventory.py`.

Report:

- both totals, and the correct flag for each chat;
- `B_minus_C`, and the verdict as in the session test;
- also how many write and edit calls B made, and the bytes it sent, from the
  stats file.

Success bar: B is at least 30% cheaper than C, and both pass the hidden tests.
Optionally, a third chat uses Anthropic's Filesystem extension (baseline 3).

## Work items

| Item | What |
|---|---|
| W1 | `workspace_root` setting, manifest entry, validation, path confinement for writes, atomic write, history store, limits. Unit tests, including the junction and reparse cases and the Windows file-busy retry. |
| W2 | The two tools: contracts, server, broker and transport plumbing, worker operations, backend, model view (the `version` key for workspace reads, compact write and edit results), stats. End-to-end tests through the MCP server, the in-process broker, and the frozen build. |
| W3 | Descriptions and server instructions with the token-budget test (listing growth at most 300 tokens with the workspace, 0 without). |
| W4 | The coding pack, the hidden tests, `self-test check-code`, wizard support for a coding pack, and the kit (`workspace\` and `final\` folders, guide text). |
| W5 | An independent review (security and accounting) before release. |

## Open questions for the owner

1. Is one workspace folder enough, or are several needed?
2. Are restores through `contextshrink_write(restore_version=…)` enough, or
   should the companion app show the history?
3. Should a third chat with Anthropic's Filesystem extension be part of the
   test?
