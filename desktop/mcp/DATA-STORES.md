# Local data stores of the desktop MCP, extension and companion

This page lists every place the desktop MCP profile, the packaged extension and
the local companion write to the Windows user profile, or keep in memory, and
what each one holds. Each fact is taken from the code cited in its row. Where a
fact could not be checked from code, it is marked in [Not verified](#not-verified).

Paths use Windows syntax. `%LOCALAPPDATA%` is the current user's local
application-data folder. If `LOCALAPPDATA` is unset, the code falls back to
`%USERPROFILE%\AppData\Local` (`extension.py`, `roles.py`, `pilot_metadata.py`).
The recovery key directory has no fallback: it fails when `LOCALAPPDATA` is
unset (`session_recovery.py`).

## Summary

| Store | Location (default, and env or argument overrides) | What it is | What it contains | What it never contains | Owner and ACL | Retention and deletion | Uninstall leaves it | Module |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Source snapshots | Memory only; no file | Immutable text versions of approved sources, held for the broker worker | Extracted or read UTF-8 text, digest, byte count, provenance | Written to disk by this module, ever | Owning process (the Windows user running Claude Desktop) | Freed when the process exits; TTL default 3600 s (60 to 86400); quotas 128 MiB and 64 snapshots by default | Nothing on disk | `snapshots.py` |
| Per-call usage ledger | Memory only; no file | Bounded HMAC event log for measurement integrity, 2000 events | Allowlisted per-call metadata counters | Paths, queries, source text | Owning process; a per-instance random key | Lost when the worker exits | Nothing on disk | `measurement.py` (`LocalUsageLedger`) |
| Cumulative usage counters | `%LOCALAPPDATA%\ContextShrink\stats\desktop-ext.json` (`CONTEXTSHRINK_EXTENSION_HOME` changes the parent; the stats folder is its sibling). Path field `usage_stats_path` | Aggregate counters across broker restarts | Call counts by tool, mode and status; error and fallback counts; summed token and UTF-8 byte estimates | Paths, queries, source text, hashes, error codes, per-call timestamps | Inherits the `LOCALAPPDATA` folder ACL (no explicit DACL in code); written via a temp file and `os.replace` | Kept until deleted. Max 64 KiB; an invalid file restarts from zero and is replaced on the next successful write | Yes. The file stays | `measurement.py` (`CumulativeUsageStats`), `extension.py` (`extension_stats_path`) |
| Extension settings | `%LOCALAPPDATA%\ContextShrink\extension\desktop-ext.json` (`broker_id` sets the file name; `CONTEXTSHRINK_EXTENSION_HOME` overrides the folder) | Settings the extension writes before it ensures the broker is running | All `DesktopSettings` fields, including `approved_roots`, `artifact_root`, `usage_stats_path` (paths) | Source text, capability tokens, pilot data | Inherits the `LOCALAPPDATA` folder ACL; temp file then replace | Rewritten on each extension start only when content changed; never expires | Yes | `extension.py` (`extension_config_path`, `write_extension_settings`) |
| Runtime copies | `%LOCALAPPDATA%\ContextShrink\runtime\<version>-<12 hex>\` (sibling of `extension`; overridden by `CONTEXTSHRINK_EXTENSION_HOME`) | Copy of the one-folder frozen build the broker runs from; each folder has a `.complete` marker | Program files only (executable, DLLs, data) | User data, source text, tokens | Inherits the `LOCALAPPDATA` folder ACL | A broker started from a frozen build removes other build folders and stale `*.tmp-*` copies older than 1 hour; the running build is kept; a folder whose DLLs are loaded cannot be renamed and stays | Yes. Uninstall does not remove it | `extension.py` (`ensure_runtime_copy`, `cleanup_runtimes`), `broker.py` (`_clean_runtimes_for_this_build`) |
| Broker capability | `%LOCALAPPDATA%\ContextShrink\broker\<sha256 of SID + broker id>.cap` | Random bearer token the companion reads to call artifact operations | One `secrets.token_urlsafe(32)` string | Anything else | Protected DACL granting GENERIC_ALL to the user SID and LocalSystem only (`roles.py`); folder has no explicit DACL | Created when the broker starts serving; revoked on clean stop only if contents still match; a crash leaves it, and the next issue replaces it | Only if a crash left it | `roles.py`, `broker.py` |
| Artifact session folders | `<artifact root>\.contextshrink-artifacts-<32 hex>\artifact-<32 hex>.{csv,json,md,txt}`. The artifact root is supplied by the operator (`--artifact-root` or setting `artifact_root`); there is no default, and rendering is disabled without it. It must not overlap an approved root | Rendered output of a table result, created by `render` in the compute profile | Rendered rows and columns derived from approved sources | Source paths or titles in file names (names are generated); the session folder name is generated | Created through `PinnedStagingDirectory`; no explicit DACL, so it inherits from the artifact root | Each file lives at most 3600 s (`ttl_seconds` capped at 3600); a new store in the same root removes generated files older than 3900 s; a clean close removes every owned file and the folder; a file is also dropped when its source dependency changes | Possibly, if the broker crashed before close | `artifacts.py`, `local_backend.py`, `staging_fs.py` |
| Staged text (companion session folders) | `<approved root>\.contextshrink-session-<32 hex>\context-<32 hex>.txt`. The approved root is supplied by the operator (`--staging-root`) and must be one of the approved roots | Text the user imports into the companion, held until the TTL or the session ends | Plaintext source text, one file per import, max 8 MiB each, 32 items and 64 MiB per session | Source paths in names (names are generated) | No explicit DACL (`NtCreateFile` passes no security descriptor), so it inherits from the approved root; POSIX folders are created 0700 | Expires after `snapshot_ttl_seconds` capped at 3600 minus 5 s; an expiry loop checks every 2 s; a clean close deletes files and folder; a crash leaves them until a later companion launch in the same root removes expired, authenticated files | Possibly, after a crash. Source text stays until its TTL and a later launch | `companion.py`, `staging_fs.py` |
| Recovery manifests and lease | Inside each session folder above: `recovery-a.json`, `recovery-b.json` (two slots) and `session.lease` (0 bytes, kernel-locked) | Signed ownership records that let a later launch prove which expired files it may delete | Session UUID; root, folder and lease identities; generation; entries (generated name, state, file ID, size, SHA-256 of the staged text, created and expiry epochs); HMAC-SHA-256 | Source text; source paths; the key | Same as the session folder | Removed with the session on clean close; kept after a crash so the next launch can recover; a crash-orphan manifest that does not verify leaves its folder untouched | Possibly, after a crash | `session_recovery.py`, `companion.py`, `staging_fs.py` |
| DPAPI recovery key | `%LOCALAPPDATA%\ContextShrink\recovery-keys\protectedsecret.bin` in a `recovery-keys` folder. Tests can pass another directory; the key directory must not overlap an approved root | 32-byte HMAC key that authenticates the manifests, wrapped with DPAPI (CryptProtectData, user scope, no entropy) behind magic `CSRK1\0` | The wrapped key bytes | The plaintext key on Windows (it is DPAPI-protected); source text | Directory and file both carry a protected DACL granting Owner Rights and LocalSystem only (`D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)`) | Created on the first companion start; never expires. Deleting it makes every existing manifest fail verification, so recovery leaves those folders alone | Yes | `session_recovery.py` (`load_hmac_key`, `default_key_directory`) |
| Pilot metadata store | `%LOCALAPPDATA%\ContextShrink\pilot\metadata.json` (argparse `--store` exists but is hidden) | Opt-in pilot consent and per-task records | Consent record; allowlisted record fields: pseudonymous participant code, task and pair IDs, arm (`A`, `B`, `C_normal`, `C_tuned`), versions, model, effort, tool profile, counters, reason codes, timestamps | Prompts, source contents, paths, credentials, clipboard contents, arbitrary tool arguments (rejected by `validate_record`) | Protected DACL granting GENERIC_ALL to the user SID and LocalSystem; POSIX 0600 | Consent lasts 1 to 90 days (default 30). The store deletes itself on read after expiry. `pilot_metadata.py delete --confirm` removes records and consent now; max 8 MiB | Yes | `pilot_metadata.py` (`default_store_path`, `PilotMetadataStore`) |
| Pilot exports | User-chosen path (`pilot_metadata.py export --output`). Refused if it equals or lies inside the store path; refused if it exists unless `--overwrite` | Copy of the store: consent and records with an `evidence_class` header | Same as the store | Same as the store | Written with the same protected DACL as the store (`_atomic_write` calls `_protect_windows_file`) | Never removed by the tool. Delete by hand; max 16 MiB | Not applicable (outside the profile) | `pilot_metadata.py` (`export`, `_atomic_write`) |
| Operator settings file | Chosen on the command line (`--config`). Not created or searched by the loader | JSON settings for the local profile, max 64 KiB | Settings values | Anything created by the loader; environment searches | Owned by the operator | Not managed by the code | Not applicable | `settings.py` (`load_settings`) |
| Token-count vocabulary cache | Read-only: `%TEMP%\data-gym-cache\<sha1 of the o200k URL>`, or `TIKTOKEN_CACHE_DIR` | Cached `o200k_base` vocabulary used for token estimates | The vocabulary file | Anything written by this code | Not owned by this code | This code only reads it, after a SHA-256 check; a missing or bad file disables the counter (`verified_vocab_cache_missing` / `verified_vocab_cache_invalid`). It does not download | Not applicable | `measurement.py` (`_load_cached_o200k`) |

Named pipe: the broker serves on a Windows named pipe, which is not a file and
leaves nothing on disk (`broker_transport.py`).

## Stores in more detail

### Source snapshots and the usage ledger (memory only)

`SnapshotStore` keeps source text in worker memory, bounded by TTL, byte quota
and count. Quotas count UTF-8 bytes of the text, not Python overhead. The
ledger (`LocalUsageLedger`) keeps 2000 allowlisted events with a random 32-byte
key created per instance. Its docstring says events disappear with the worker.
Cursor states are also in memory (`CursorSigner`, max 1024 states). Nothing in
these three is written to disk by the code.

### Cumulative usage counters

The file is rewritten after each recorded call. Its docstring lists exactly
what it holds (counters only). The file is a plain local file that anyone with
access to it can edit, so it is not evidence. Reset by deleting it after the
broker has stopped. A running broker keeps totals in memory and rewrites the
file on its next call.

### Extension settings, runtime copies and capability

On a frozen build, `serve --extension` writes the settings file, then ensures a
broker is running from a runtime copy. The broker's capability is written
beside the broker state. Its token is loaded by the companion on each request.
The capability is revoked only when the broker's serve loop ends normally
(`broker.py`, `finally` block).

### Artifact session folders

`ArtifactStore.__init__` calls `sweep_stale_sessions(base_dir)` before creating
a fresh session folder. The sweep touches only generated names, follows no
links or reparse points, and deletes nothing recursively. `close()` removes
every owned file and then the folder.

### Staged text and recovery

Each companion start first runs `recover_expired_sessions` for its root. That
function reads at most 4,096 root entries and 128 session folders per launch.
It removes an orphan only when: the lease is free, both manifests verify, the
lease identity matches, every file identity matches, all entries have expired,
and no unknown children block the final `rmdir`. Anything else is left alone.

The live companion writes manifests after each generation, so a crash can leave
at most one of these: the last signed generation, or an unowned temp file.
Staged text is never recursively deleted.

### DPAPI key

On Windows the key bytes are wrapped with DPAPI in the user's profile. Any
process running as that Windows user can unwrap them, so the key does not
protect against same-user code. The key directory must not overlap an approved
root; `_check_key_location` refuses that.

### Pilot store

`PilotMetadataStore` deletes the file when consent has expired and it is read.
It also removes stale `.metadata.json.*.tmp` files older than an hour. Records
are validated again on read and export, so a hand-edited file cannot widen what
an export contains. Collection is not connected to ordinary extension calls
(`desktop/mcp/README.md`).

## Environment and argument overrides

| Override | Affects | Notes |
| --- | --- | --- |
| `CONTEXTSHRINK_EXTENSION_HOME` (absolute path only) | Extension settings folder; runtime copies and stats folder as siblings | Does not move the capability, pilot store or recovery key |
| `LOCALAPPDATA` | Every `%LOCALAPPDATA%` default above | Unset falls back to `%USERPROFILE%\AppData\Local`; the recovery key fails instead |
| `--artifact-root` / setting `artifact_root` | Artifact session folders | No default |
| `--staging-root` | Staged text and recovery folders | Must be an approved root |
| `usage_stats_path` (extension computes it) | Cumulative counters file | Set to none when the stats folder overlaps an approved root |
| `TIKTOKEN_CACHE_DIR` | Read-only o200k cache | |
| Test-only parameters (`base_dir`, `key_directory`, `recovery_key_directory`, `--store`) | Capability, recovery key, companion key, pilot store | Hidden or documented as test injection points |

## Delete everything

Do these steps in order. Stopping processes abruptly skips the normal cleanup,
so do not skip the wait.

1. Quit Claude Desktop completely, including the tray icon.
2. Stop the broker and any companion. The broker stops about 120 seconds after
   the last chat closes, because heartbeats stop (`extension.py`, `_EXTENSION_IDLE_SECONDS`).
   To stop it now, check first and then end the processes:

   ```powershell
   Get-Process -Name contextshrink-desktop -ErrorAction SilentlyContinue
   Stop-Process -Name contextshrink-desktop -Force   # only if the list above is not empty and you have confirmed nothing else needs it
   ```

   Confirm nothing is left: `Get-Process -Name contextshrink-desktop` returns
   nothing. Stopping a broker abruptly skips capability revocation and worker
   cleanup; the leftovers are handled in step 4.
3. Delete the per-user folders. Check that they hold nothing else you need
   first:

   ```powershell
   Remove-Item -Recurse -Force "$env:LOCALAPPDATA\ContextShrink\runtime"
   Remove-Item -Recurse -Force "$env:LOCALAPPDATA\ContextShrink\extension"
   Remove-Item -Recurse -Force "$env:LOCALAPPDATA\ContextShrink\stats"
   Remove-Item -Recurse -Force "$env:LOCALAPPDATA\ContextShrink\broker"
   ```

   For the pilot store, run `desktop\mcp\pilot_metadata.py delete --confirm`
   first. Copy any export you still need before deleting, because exports are
   separate files.
4. Delete the staged and artifact session folders, which are inside the
   folders you approved and the artifact root you chose. Do this only when no
   companion or broker is running. They hold source text until their TTL, and
   folder names start with a dot. Check each approved root and artifact root for
   `.contextshrink-session-*` and `.contextshrink-artifacts-*`.
5. Delete the recovery key only after step 4. Deleting it first makes the
   existing manifests unverifiable, so recovery will leave those folders
   behind for good:

   ```powershell
   Remove-Item -Recurse -Force "$env:LOCALAPPDATA\ContextShrink\recovery-keys"
   ```

6. If `%LOCALAPPDATA%\ContextShrink` is empty now, you may remove it. If it
   holds other content, keep that content.

Microsoft Store builds of Claude Desktop may redirect `%LOCALAPPDATA%` writes
into the app's package folder, so some of these paths may appear there
instead. This was observed not to happen for the stats file on one machine. If
a folder above is missing, look under the package's `LocalCache` folder
inside `%LOCALAPPDATA%\Packages`.

## What uninstalling the extension leaves behind

The repository contains no uninstall code. Per `desktop/mcp/extension/README.md`
the uninstall removes the extension's server folder, but not the runtime copy.
Based on the code, nothing else is removed either, so the following remain
after uninstall until the steps above are run: the runtime copy, the settings
file, the stats file, the pilot store and exports, the recovery key, and any
staged or artifact session folders left by a crash. Capability files remain
only after a crash.

## Not verified

- How Claude Desktop installs and removes an extension, and whether its server
  folder is the same as any path above. Only the extension README describes this.
- The Microsoft Store redirection of `%LOCALAPPDATA%`. The note above is based
  on one observed machine.
- The process name `contextshrink-desktop.exe` as the running broker's name is
  taken from the build script (`EXE_NAME`), not from a live run.
- Whether tiktoken, the library, writes its cache to `data-gym-cache` itself.
  This code only reads it.
- Windows DACLs on the capability file and pilot store were not read back at
  runtime on this machine. The DACL claims come from the code that sets them.
  The test suite checks the recovery key DACL on Windows
  (`tests/desktop_mcp/test_recovery_hardening.py`).
