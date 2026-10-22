# Codex connector acceptance record

## Selective defaults and diagnostic accounting (0.2.4)

Updated 21 October 2026. Package 0.2.4 is a local preview, not published to npm.

- Node suite: 61 passed, one optional live Codex-migration test skipped. The
  native-engine integration ran, including five-result defaults, explicit
  larger limits, missing-symbol fallback, working class traces, freshness,
  runtime migration and old-chat maintenance/query call compatibility.
- Proxy/API/billing/key-summary/keepalive checks: 91 passed. The real shared
  SQL aggregate and fee query execute against SQLite's compatible JSON and
  FILTER support, with legacy positive/negative graph rows, ordinary requests,
  keepalive costs, other accounts and period boundaries. No Stripe requests
  or production account changes occur.
- Dashboard production build passes. Graph diagnostics are labeled as
  retrieval differences and zero savings credit, not whole-session savings.
- Three advertised graph tools replace six; response budget defaults to
  3,000 bytes and search limit to five. Serialized tool/guidance/initialization
  overhead measures 544 versus 706 local o200k tokens (22.95% lower), using
  the preserved 0.2.3 fixture. This is a static measurement, not provider
  usage or a demonstrated whole-chat cost reduction. See
  `experiments/codex-graph/selective-overhead-results.json`.
- Signed retrieval diagnostics keep the existing receipt/replay/outbox path.
  They receive zero dollars. API ingestion neutralizes old proxy credits;
  analytics, key summaries, free-plan caps and new fee calculations exclude
  historical graph dollars. Stored historical rows and issued invoices are
  not rewritten.
- Rerun `graph init` from this preview and restart Codex to upgrade an owned
  project integration. API, proxy and dashboard changes require deployment;
  no deployment or npm publication was performed for this update.

## Previous acceptance notes


Updated 21 October 2026 (Chicago). Package 0.2.3 is a local preview, not published to npm.

## Compact discovery (0.2.3)

- All 61 Node tests pass with no native skips, including compact result order,
  preservation of citations/extra metadata, untouched input data, native source
  verification, lookup receipt exclusion and owned runtime upgrades.
- On identical saved 0.2.2 results, projection alone reduces output tokens from
  664 to 421, 680 to 426, and 674 to 421. Receipt metadata is retained and the
  ordered symbol/file/line citations match exactly. Recorded in
  `experiments/codex-graph/search-compaction-results.json`.
- The ten-step workflow accounts for the actual schemas, project guidance and
  compact MCP safety notes. Its report remains a retrieval experiment, not a
  live-model or quality-equivalence measurement.
- The per-query ledger allowance remains 1,024 tokens. No separate billing
  mechanism or credit for direct focused searches has been added.
- The package remains a local preview. Rerun `graph init` with 0.2.3 and restart
  Codex to load the owned runtime; no production deployment has occurred.

## Selective graph discovery (0.2.2)

- All 60 Node tests pass, with no skipped native engine tests. They cover
  owned 0.2.1 integration upgrades, native indexing/MCP, lookup/file-scope receipt
  exclusion, automatic freshness, and retention of unrelated project content.
- The signed ledger proof still passes through Node tokenization, Python
  proxy recounting, SQLite replay deduplication and API validation. No production
  account was modified. Lookup calls receive no receipt; eligible discovery
  estimates remain in **Est. savings**, with no separate charge.
- The scripted ten-step comparison uses three graph discoveries, six focused
  source operations and one context reuse step. It includes schemas plus both
  project and MCP guidance, keeps the focused-search comparison, and verifies
  current source citations. See `experiments/codex-graph/session-results.json`.
- This does not establish live agent routing, answer quality, cached model
  cost, or total-session savings. No live-model paired session has been run.
- Rerun `graph init` with 0.2.2 and restart Codex to load the new owned runtime
  and guidance. No production deployment or npm publication has occurred.

## Graph estimate ledger (0.2.1)

- 59 Node tests pass, including pinned native graph indexing/MCP, runtime upgrade,
  bounded receipts, and tokenizer/exclusion behavior. No native tests skipped.
- The proxy/API tests cover signed dollar estimates, unpriced models, failed
  requests, direct/code-mode MCP results, input/output recounting, stable receipt
  IDs, old-API capability gating, and durable queue deduplication. Existing billing
  rules, proxy compression tests, and key-summary behavior are regression-tested.
- The dashboard production build passes. Its graph breakdown is already part of
  Est. savings and is separate from inference/compression counters.
- `experiments/codex-graph/benchmark.cjs` compares three real discovery tasks with
  broad and focused search, including metadata and overhead. Its report does not
  establish autonomous full-chat savings. The cross-language ledger proof validates
  real Node receipts through proxy tokenization, durable SQLite queue, and API
  schema after repeated history replay. No production ledger is modified.
- Server deployment is required before live graph estimates appear. The API must
  advertise support before the proxy emits the new rows. The package remains a
  local preview; this change adds no separate feature charge.

## Local graph verification (0.2.0)

- Real checksum-pinned codebase-memory-mcp v0.8.1 engine indexed this worktree:
  about 49,000 nodes and 313,000 edges across over 2,600 eligible files. Counts
  change as local source is edited. Indexing and queries make no model request.
- Real searches for `flash_openai`, `pricing`, and `migrate` returned about
  1.7–1.8 KB of compact JSON each at a five-result limit. The compression search
  located the expected handler at line 2120 and its transform helpers; a bounded
  caller/callee trace used 2.9 KB. The matched full compression files totalled
  about 608 KB, but this is an illustrative discovery comparison: normal targeted
  search also avoids full reads, and edits still require relevant source. It is
  not a measured end-to-end token or dollar savings percentage.
- Automated coverage includes native symbol search, callers/callees, Cypher,
  architecture, dirty/untracked changes, deleted symbols, freshness, output byte
  budgets, secret-name/ignore/symlink filters, locks, binary tamper refusal,
  preserving/removing project integration, and a real stdio MCP conversation.
- Offline map sampling uses connected endpoints. The real-worktree export had
  200 nodes and 185 relationships. Safe encoding, script syntax, and native export
  are checked; visual browser acceptance remains pending because the browser tool
  disallows local file URLs.
- The MCP transport was exercised with the actual installed Codex engine on an
  isolated fixture for connector regression, and with real stdio requests for the
  graph tools. Live desktop graph-tool discovery in a restarted trusted project
  is a separate acceptance check. No graph savings percentage is claimed.
- To enable native graph integration tests, set `CONTEXTSHRINK_TEST_CBM_BIN` to
  the pinned v0.8.1 binary before running the suite. Both native environment
  variables below are needed for a run with no skipped native tests.

See [GRAPH.md](GRAPH.md) for installation and known coverage limits.

0.1.2 visibility fix: reproduced a running 0.1.1 companion whose form was hidden
by the launcher's STARTUPINFO SW_HIDE. Explicitly show the form on its Shown
event. A real Windows PowerShell launch using windowsHide=true and sample totals
was verified visible through IsWindowVisible and closed normally with exit 0.
The user's existing companion was also made visible without reconnecting.

The user's current chat migrated successfully: its SQLite and rollout providers
are both contextshrink_npx, its undo record is present, and the chat resumed.
The user subsequently confirmed the overall setup is working, including the
migrated chat and companion. Full manual restore and window-control checks
below remain separate from that functional confirmation.

- [x] **New local desktop chats:** user confirmed that requests appeared on the
  ContextShrink dashboard. Their 0.1.0 `savings` output reported 15 requests,
  3% trimmed and $0.04 estimated net savings for that key today (UTC).
- [ ] **Existing chats, live desktop acceptance:** implementation is in 0.1.2.
  Close Codex, preview and apply migration to one old `openai` chat. Reopen the
  same chat, confirm its history/title/project/pin, and make a request. Verify
  dashboard routing, then restore and confirm that later turns are retained.
- [ ] **Savings companion, live desktop acceptance:** implementation is in 0.1.2.
  Run `savings-window`, verify totals against `savings`, test Keep on top and
  moving the window, disconnect/reconnect, then close the companion. This is
  the companion-window alternative selected by the user, not a native footer.

## Developer verification

- All 59 Node tests passed, including both opt-in native engine regressions.
- Offline migration tests cover unchanged dry runs, provider-only SQLite
  updates, byte-for-byte conversation preservation, restoration after appended
  turns, archived moves, interrupted writes, edited metadata refusal, path
  containment, process guard, and automatic restoration on disconnect.
- The installed `codex-cli 0.163.0-alpha.10.2` resumed the same isolated fixture
  ID using the migrated provider, routed its next request to a local mock
  gateway, included its earlier history, and restored the original provider
  while preserving later turns. Fake credentials and loopback endpoints only;
  no real account, ChatGPT quota, or existing user chat was used.
- The Windows WinForms companion was rendered and visually checked using
  sample totals matching the user's report. Window lifecycle tests cover
  cleanup, refresh, numeric-only snapshots, error states, and hidden launcher.
- This does not yet establish real desktop acceptance for the two unchecked
  items above. No app binaries or live Codex history were modified.

To run the full suite including the installed engine (adjust its path):

```powershell
$env:CONTEXTSHRINK_TEST_CODEX_BIN = 'C:\path\to\codex.exe'
node --test test/*.test.js
```

The engine regression is skipped when that variable is not set. Chat migration
tests require Node 22.13+. Standard connector commands support Node 18+.
