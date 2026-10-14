# Phase G M14: suite hardening and crash recovery

This Stage 2 change closes the session-vault crash gap without changing the
0.1.1 extension broker, its tool roles, or the approved-root model. A companion
process crash can leave staged source text behind; the next companion launch
now removes only expired files whose ownership can be proved from authenticated
state. Recovery runs at launch and is bounded; it does not run in the
background. If the companion is never launched again, a crash orphan can stay
on disk after its disclosed TTL.

## Ownership and recovery rules

- Each new session directory has a kernel-released lease and two fixed
  authenticated manifest slots. The HMAC key is outside every approved root.
  Windows stores it under the current user's LocalAppData recovery-key folder,
  protects its bytes with current-user DPAPI, and applies a protected DACL for
  the owner and LocalSystem. POSIX tests use a mode-0600 key in a private
  temporary directory. A key path that overlaps an approved root fails closed.
- A stage import first creates an exclusive empty file and captures its file
  identity. A signed `writing` record containing that identity, the byte cap,
  expected digest, creation time, and expiry is durably committed before any
  source bytes are written. The write uses the retained file handle. A second
  signed generation marks the entry `committed` only after the broker verifies
  the staged digest.
- The manifests bind the session ID, approved-root identity, session-directory
  identity, lease identity, generation, and fixed schema. Entry names are
  generated basenames only. Recovery rejects duplicate keys or IDs, malformed
  data, invalid signatures, path-like names, hardlinks, reparse points, changed
  identities, oversized files, and ambiguous or corrupt slots.
- The two slots rotate by deleting only the authenticated older slot while the
  newest slot remains durable, then atomically promoting a unique metadata
  temp file into the empty slot. At every crash point, either the last signed
  generation remains or only a harmless metadata temp remains. Recovery never
  deletes unknown temp files or children.
- Recovery checks at most 4,096 root entries and 128 generated session
  directories per launch, and reads at most 256 child names per session. It
  skips active leases and leaves a session untouched if any item is unexpired,
  the lease identity changed, an entry identity is uncertain, a manifest is
  invalid, or cleanup cannot finish. It deletes only single-link regular files
  matching signed file IDs. A `writing` partial may be removed only when its
  actual size does not exceed the signed cap; a committed entry also needs an
  exact size and digest match.
- Existing pre-manifest session folders cannot be authenticated and are left
  alone. Files that were already absent after a partial cleanup are treated as
  completed deletions; the authenticated manifest remains available for retry.

## Automated checks

The focused Windows suite uses temporary approved roots and private temporary
key directories. It covers active-session protection, abrupt subprocess
termination and restart under the same Windows user, partial `writing` cleanup,
expiry, two-slot rotation, malformed and corrupt manifests, path escape,
excessive TTL, changed file identity, hardlinks, lease replacement, unknown
children, and cleanup retry. Existing staging tests continue to cover
exclusive collisions, Windows sharing conflicts, root containment, symlink and
reparse defenses, and broker digest pinning.

The Stage 2 implementation has not been tested on a clean machine or across
Windows SIDs. These are manual release checks and remain pending:

| Check | Required evidence | Status |
| --- | --- | --- |
| Fresh install and runtime copy | Install, launch, import, close, verify owned staging is removed | Pending |
| Normal restart and handover | Companion restart while a session is active; confirm lease protects the session, then confirm cleanup after process exit and expiry | Pending |
| Abrupt process termination | Kill the companion during a synthetic import; confirm partial or committed data survives until expiry and is removed on a later launch | Automated synthetic test; manual packaged check pending |
| Same-user profile reload | Reopen the DPAPI-protected key on the same Windows user profile and recover the signed manifest | Automated subprocess test; clean-machine check pending |
| Cross-SID boundary | A second Windows user cannot decrypt the key, acquire another user's active lease, or reap their files | Pending |
| Rollback and uninstall | Roll back/uninstall while a companion session is live; verify no unrelated approved-root content is removed | Pending |
| MCP roles and clipboard | Confirm existing fixed source/result roles and explicit-action clipboard behavior remain unchanged | Existing automated role/clipboard coverage; packaged smoke pending |

No external study participants, beta uptake, or economy is represented by this
suite report. Any M6/M13 measurements remain local harness evidence until
separately collected and reported under their approved contracts.

## Local frozen MCPB smoke (2026-10-14)

The final `.mcpb` was extracted to a fresh temporary directory and run only with
synthetic files and a temporary approved root. The packaged executable printed
CLI help; there is no `--version` CLI switch, while the MCPB manifest and runtime
metadata report version `0.1.1`. A frozen `serve --extension` stdio session
listed the four fixed source tools and found a synthetic fixture. The packaged
companion also imported synthetic text (HTTP 201), then was terminated abruptly.
After the signed fixture expiry was advanced with the temporary DPAPI key, a
second packaged launch reloaded that key, removed the expired orphan, and an
authenticated close removed the new session. All temporary processes and
synthetic roots were cleaned up. This local package smoke does not substitute
for the pending clean-machine Claude Desktop install/UI, Cross-SID, or rollback
checks above.

During an intermediate smoke-harness attempt, the parent harness called the
key loader without first overriding its own `LOCALAPPDATA`; it may have read or
created the default current-user recovery-key file at the sanitized location
`%LOCALAPPDATA%\ContextShrink\recovery-keys\protectedsecret.bin`. No key
bytes were printed or transmitted, and I did not inspect or delete that file.
The passing retry set both parent and frozen-child `LOCALAPPDATA` to a new
synthetic temporary profile before importing or calling the key loader.
