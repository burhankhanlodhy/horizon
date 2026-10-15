---
title: "ContextShrink Desktop MCP: single-account self test"
date: 2026-10-15
status: ready-for-owner-testing
phase: G
---

# Single-account self test

The owner has one Claude Enterprise account, billed on API usage. There is no
admin-level or per-user export. Claude Desktop's settings show:

- a **running total** in dollars to the cent (for example `$20.23 of $500`),
  which updates within about a minute;
- a **daily and weekly breakdown by product and by model**, which appears
  about 4 hours later and lists Claude Desktop separately from Claude Code.

The owner runs every test personally. The multi-participant Phase G tools stay
as they are for later use. This self test is a separate, simpler path whose
claims apply only to this account, this model setting and this task set.

## Design

- **Methods.**
  - **A:** ContextShrink with compression off (extension *Pilot method* =
    `exact_only`).
  - **B:** ContextShrink normal.
  - **C_normal:** ContextShrink connector off, documents attached natively.
  - **C_tuned:** like C_normal, with the v1 answer instruction pack pasted
    first.

  The same model and effort are used every day, as named in the pack.
- **Unit of measurement.** At $0.01 resolution, single tasks are mostly
  rounding noise. The 16 tasks are split into 8 fixed groups of 2. A group's
  cost under a method is the running total read after the group minus the
  total read before it. Rounding error is about ±$0.01 per difference, roughly
  symmetric, and averages out across units.
- **Schedule.**
  - There are 2 rounds. In each round every method gets one calendar day
    (local time), so there are 8 measurement days.
  - The method order in each round comes from a seeded balanced design, and
    round 2 uses a different order.
  - Within a day, the groups run in seeded random order, and the tasks within
    a group in seeded order.
  - Every task runs in a fresh chat with a fixed prompt.
- **Daily reconciliation.** About 4 hours after a day ends, the owner enters
  that day's Claude Desktop line from the by-product breakdown. That day's
  group deltas must sum to it within $0.01 × (number of groups + 1).
  Otherwise the day is unreconciled and left out of claims.
- **Other contamination signals.** Any spend between one group's "after"
  reading and the next group's "before" reading flags the day. So does a
  total that goes backwards, or a block the owner reports as interrupted.
- **Quality.** Every task has an objectively checkable answer. Claude is asked
  to end with a line starting `ANSWER:`, and the tool checks that line against
  the key. Nobody grades by hand.

## Interfaces

### Pack (`self_test_pack`, schema 1)

```json
{
  "schema_version": 1,
  "kind": "self_test_pack",
  "study_id": "st_my_docs",
  "model_label": "Claude <model> at <effort>",
  "minimums": {"paired_units": 12, "groups": 6},
  "quality_tolerance": 1,
  "groups": [{"group_id": "g1", "tasks": ["t_contract_term", "t_q3_total"]}],
  "tasks": [{
    "task_id": "t_contract_term",
    "source_files": ["contract.pdf"],
    "question": "What is the renewal notice period?",
    "answer": {"kind": "text_contains", "expected": "90 days"}
  }]
}
```

- `study_id` matches `^st_[a-z0-9_]{1,40}$`, and `task_id` matches
  `^t_[a-z0-9_]{1,40}$`.
- `model_label` is at most 80 printable characters. It is shown to the owner
  every day.
- There are 4 to 16 groups (8 recommended), each with 1 to 4 tasks. Every task
  is in exactly one group.
- `source_files` lists 1 to 4 safe base names with supported suffixes (txt,
  md, json, csv, pdf, docx).
- `question` is at most 2,000 characters.
- `answer.kind` is one of:
  - `number`: `expected` is a decimal string, with an optional `tolerance`
    decimal string (default `0`);
  - `text_contains`: matching is case-folded and whitespace-normalised
    substring;
  - `date`: `expected` is `YYYY-MM-DD`, and common answer formats are
    accepted;
  - `choice`: `expected` is a list of 1 to 10 accepted strings, matched
    exactly after case-folding.
- `quality_tolerance` is an integer from 0 to 4. Method B may have at most
  that many fewer correct answers than C_normal over the study before a
  savings claim is withheld for `quality_regression`.
- `minimums.paired_units` is at least 4 and `minimums.groups` at least 3.

### Prompts (fixed text)

- **A and B:** `Use the ContextShrink tools to read {files} from my approved
  folders, then answer this: {question}` + `\n\nEnd with one line that starts
  with "ANSWER:" and gives only the final answer.`
- **C_normal:** `I have attached {files}. Answer this: {question}` plus the
  same ending.
- **C_tuned:** the `answer_contract` v1 text, then two newlines, then the
  C_normal prompt.
- `{files}` is the file names joined with `, `.

### Ledger

- **Location and format.** `<state_dir>/ledger.jsonl`, append-only. Each line
  is `{"seq", "prev_sha256", "type", "utc", ...}`, where `prev_sha256` is the
  SHA-256 of the previous line's exact bytes (64 zeros for the first). Loading
  checks the whole chain, and any break is `ledger_tampered`.
- **Record types:**
  - `study_started` (pack digest, seed, schedule);
  - `day_started` (round, method, local date, UTC offset, confirmed setup);
  - `block_before` / `block_after` (group, total as a 2-decimal string);
  - `task_answer` (task, correct, answer SHA-256; the answer text is not
    stored);
  - `block_voided` (group, reason);
  - `day_finished`;
  - `day_reconciled` (local date, Claude Desktop daily total).
- **Readings.** A reading is a dollar amount with exactly 2 decimals, between
  0 and 100,000. "After" must be at least "before", and a block delta above
  $20 is rejected as a likely typo.
- **The day.** A day must finish on the local date it started. Only one
  measurement day is allowed per local date.
- **Default state directory.** `%LOCALAPPDATA%\ContextShrink\self-test\<study_id>`.
  Readings and answers never leave it.

### Analysis

- **Group cost.** `cost(round, group, method)` is after minus before for the
  non-voided block.
- **Comparisons.** The primary comparison is B vs C_normal. The secondary
  ones are B vs A, B vs C_tuned, and C_tuned vs C_normal. For each, one
  `PairedUnit(cluster_id=group_id, difference=cost_candidate −
  cost_reference)` is built per round and group, where both days are
  reconciled and neither block is flagged.
- **Interval and decision.** Each comparison gets `paired_difference_interval`
  with a fixed reporting seed, then `savings_decision` with:
  - `evidence_class` and `eligible_classes` = `self_metered`;
  - `provenance_verified` true only when every included day is reconciled;
  - the pack minimums;
  - a `quality_regression` override on the primary comparison.
- **Report contents.**
  - per-method totals;
  - cost per correct answer;
  - per-task correctness;
  - flagged and unreconciled days with reasons;
  - each comparison's interval and decision;
  - scope labels: `single_account_self_test`, meter resolution $0.01, and
    "results apply to this account, model setting and task set only".

## Pieces

| Piece | Owner files |
|---|---|
| Extension 0.1.2 *Pilot method* | `extension.py`, `manifest.template.json`, `version.py`, extension README, tests |
| Self-test core | `horizon/desktop_mcp/self_test.py`, pack template, tests |
| Terminal wizard | `desktop/mcp/self_test.py`, tests |
| Plain-language guide | `desktop/mcp/SELF-TEST-GUIDE.md` (coordinator) |

## Status

All four pieces are built, plus read-mode guidance in the tool
descriptions, so method B uses the modes that actually save tokens. It
costs about 191 estimated tokens per chat; CSV is excluded because
compact has no CSV fold.

Verification: `pytest tests/desktop_mcp` plus the MCP dependency-contract
and CCR HTTP tests report 1,522 passed, 2 skipped, and lint and format
pass. The frozen 0.1.2 build was smoke-tested:

- the manifest carries `pilot_method`;
- the server reports 0.1.2;
- `exact_only` reports `evaluation_arm: raw` and serves a focused request
  as exact text;
- `normal` reports `optimized` and focuses.

The wizard's init/status/run screens were checked by hand. It refuses the
example pack and a placeholder model label.

The owner runs it next: install 0.1.2, write the 16-task pack, then follow
`desktop/mcp/SELF-TEST-GUIDE.md`.

## Revision: single-day run on a separate laptop (2026-10-15)

The owner will run the test in one day, on a different laptop that has the
Enterprise account and no Python or repository. The changes below replace
the 8-day schedule.

### Design

- **Bigger generated documents.** Each task then costs about $0.10 or more
  with Opus, which a cent-resolution meter reads cleanly, so each task can be
  measured on its own.
  - The pack has **12 groups of 1 task**, and the tasks use generated
    documents with computed answers.
  - Pack minimums are `paired_units: 10` and `groups: 8`, so one voided block
    does not end the study.
- **Phases.** The 12 groups are split, by seeded order, into halves H1 and
  H2. A method order O is chosen by seed from the 4-method orders in which A
  and B are adjacent, so the Pilot method changes once per half.
  - H1 runs every method in order O.
  - H2 runs them in `reverse(O)`.

  That gives 8 phases. Each phase is one method over its half's groups, in
  seeded order, so every task runs once under every method (48 chats). The
  mirrored order balances position effects such as short-lived prompt
  caching.
- **Readings.** Each block is one task, with a reading before and after it.
  The wizard waits at least 90 seconds after the answer before the "after"
  reading, because the running total updates within about a minute.
- **Phase boundaries.** Each phase starts with its setup checklist. For A or
  B, that includes the restart and stats check, done in its own chat before
  the phase's first "before" reading. Spend between blocks is therefore
  expected at phase boundaries, and only recorded as a warning inside a
  phase.
- **Reconciliation by date.** For every local date with blocks, the owner
  enters that date's Claude Desktop line. It must equal the date's last
  reading minus its first reading, within $0.01 × (blocks that date + 1). A
  date that fails excludes all of its blocks.
  - Using the span includes the setup check chats, which are Desktop spend,
    so they reconcile.
  - Any non-Desktop spend in the span, such as Claude Code, makes the date
    fail, so other products must stay idle all day.
  - A run that crosses midnight, or resumes the next day, reconciles each
    date separately. One day is recommended, not required.
- **Exclusions.** A voided block is excluded and redone straight away with a
  fresh "before" reading, and does not exclude anything else. A block whose
  delta is out of range is rejected when entered.
  `provenance_verified` holds only when every date with included blocks is
  reconciled.
- **Units.** One unit per group (task), for each method pair, with cluster =
  group. Comparisons and decisions are otherwise unchanged.

### Portable kit (no Python on the target laptop)

- The self-test CLI moves into the package as
  `horizon/desktop_mcp/self_test_cli.py`, and the frozen app gains a
  `self-test` subcommand. So `contextshrink-desktop.exe self-test run ...`
  works from the bundle. `desktop/mcp/self_test.py` stays as a thin wrapper
  for development.
- `desktop/mcp/self-test/make_dataset.py --out DIR [--seed N]` generates
  `documents/` and `pack.json` deterministically. Every answer is computed
  from the generated data, and a test re-derives each answer by parsing the
  files. The documents are a fictional company's material:
  - a long operations handbook (Markdown, about 60 KB), with 3 tasks;
  - an order export (JSON, about 120 KB), with 3 lookup tasks;
  - service event logs with repetitive runs (`.log` is not a pack suffix,
    so `.txt`, about 100 KB), with 3 tasks;
  - a platform configuration (JSON, about 40 KB), with 1 task;
  - a short contract summary (Markdown, about 3 KB), with 1 control task;
  - a contacts list (CSV, about 3 KB), with 1 control task.

  Answers are lookups with one location each, not sums over large files.
- **The kit zip** contains:
  - `Start Self Test.cmd`, which uses `%~dp0` and runs the bundled exe;
  - `runtime\`, the frozen one-folder app;
  - `contextshrink-desktop-<version>.mcpb`;
  - `documents\`, the folder to approve in the extension;
  - `pack.json`;
  - `GUIDE.txt`.

  The state lives in `%LOCALAPPDATA%\ContextShrink\self-test\<study_id>`, and
  the report is written next to the launcher. The extension version is
  0.1.3.
