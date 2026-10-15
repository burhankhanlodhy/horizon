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
