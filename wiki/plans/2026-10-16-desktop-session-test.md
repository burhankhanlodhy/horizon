# Desktop long-session self test and extension 0.1.5

## Why

In the single-question test (2026-10-15), every token was paid once. That left
ContextShrink nothing to save: the fixed cost of a chat (about $0.10) was most
of the bill.

Real use is long chats. A long Claude Code session of the owner's
(2026-10-16) cost $47.24, and most of that was cache reads (57.6M tokens).
Every call re-reads the whole history, so a document that enters the chat
early is paid again on every later turn.

The test now asks many questions in one chat:

- **C (no ContextShrink):** the files are attached at the start and stay in the
  history.
- **B (ContextShrink):** only `find` excerpts enter the history.

## Design (owner's decisions)

- There are two chats in total: one with ContextShrink (B) and one without it
  (C_normal). Both ask the same 12 questions, in the same order, in the same
  chat.
- A seeded coin decides which chat runs first.
- The usage total is read before and after each chat. There is no per-question
  reading and no 90-second countdown.
- After the last answer, the wizard asks for the total and says: "type it
  once it has stopped changing (it updates within about a minute)". The wizard
  does not enforce a wait.
- Each answer is checked from its `ANSWER:` line, as before.
- Reconciliation is unchanged: per local date, Claude Desktop's line in the
  daily breakdown by product must match the span of that date's readings.
- The result is one paired comparison, with no confidence interval. The report
  says so.

## W1: Extension 0.1.5 (agent A)

- `contextshrink_open`: the `source` property description says it accepts a
  path or just the file name when one approved folder holds the file. This is
  already true in `source_access._locate`; only the text changes.
- The open description says "open a file by its name directly; no search is
  needed when you know the name" and "for each new question, call find again
  instead of re-reading the whole file". Keep it short.
- The `contextshrink_search` description stays as in 0.1.4.
- Optional, only if small and safe: with several approved roots, a bare file
  name that matches exactly one file directly under one root resolves to that
  file. When it matches several, return `source_unapproved` as today. Skip this
  if it touches more than `source_access._locate` and its tests.
- `VERSION = "0.1.5"`, and update the version tests, including the lifecycle
  comparison cases.
- Report the token change in the tools listing.

## W2: Session dataset (agent B)

`make_dataset.py --profile session` (default `single`, which stays
byte-identical to today) writes `documents/` and `pack.json`.

Documents (fictional, deterministic per seed, default seed 20261016):

| File | Size |
|---|---|
| `orders_export.json` | about 140 KB |
| `service_events.txt` | about 140 KB |
| `operations_handbook.md` | about 90 KB |
| `platform_config.json` | about 45 KB |

- The total stays at or under 430 KB, so all four files fit in one chat as
  attachments.
- Reuse the existing generators with size parameters. Keep the realism rules:
  every log line has its own timestamp, and there are no identical
  consecutive lines.
- **Questions:** 12, three per file, interleaved by file (orders, log,
  handbook, config, then repeat). Use the existing question kinds: an order
  field lookup, a nested config value, a first-ERROR timestamp, a count of
  events for one service, a handbook fact. Every answer is unique in its file
  and machine-checkable with the existing answer kinds.

Session pack format:

```json
{
  "schema_version": 1,
  "kind": "self_test_session_pack",
  "study_id": "st_session_v1",
  "model_label": "Claude Opus, Medium effort",
  "quality_tolerance": 1,
  "methods": ["B", "C_normal"],
  "documents": ["orders_export.json", "service_events.txt", "operations_handbook.md", "platform_config.json"],
  "tasks": [{"task_id": "...", "source_files": ["..."], "question": "...", "answer": {"kind": "...", "expected": "..."}}]
}
```

- `tasks` are in the order asked.
- `answer` uses the existing answer specs (`number`, `date`, `text_contains`).

## W3: Session wizard, report and kit (agent C)

New module `horizon/desktop_mcp/self_test_session.py`. It reuses
`self_test.py` helpers wherever possible: readings, money, the ledger hash
chain and its file format, answer checking, `answer_digest`, reconcile-by-date
logic, the state dir and the seed.

Pack validation: `methods` is exactly `["B", "C_normal"]`, and `tasks` has
4..40 entries.

Ledger records (hash-chained, same envelope as today):

- `study_started`: `pack_digest`, `seed_hex`, `order` (the two methods);
- `chat_started`: method, the before `total`, and `setup_confirmed`;
- `task_answer`: method, `task_id`, `correct`, `answer_sha256`;
- `chat_ended`: method, the after `total`;
- `chat_voided`: method and reason. The chat is redone from the start in a
  new chat, and its answers are discarded;
- `day_reconciled`: as today.

Messages to paste:

- **The first message of each chat** is a short preamble plus question 1.
  - C_normal: "I have attached <files>. I will ask you 12 questions about
    them, one at a time. Answer each from the files." Attach all four
    documents with this message.
  - B: "The files <files> are in my approved ContextShrink folder. I will
    ask you 12 questions about them, one at a time. Use the ContextShrink
    tools to answer each from the files." Attach nothing. (Not "read <files>":
    that wording invites loading every file whole before the first question.)
  - Do not tell Claude which ContextShrink mode to use.
- **Each question** ends with "End with one line that starts with "ANSWER:"
  and gives only the final answer." Questions 2..N are pasted in the same chat.

Setup for each chat, shown before its before-reading:

- B: Pilot method normal, connector ON. Fully quit and reopen Claude Desktop
  if the setting changed. Run the "show ContextShrink stats" check (evaluation
  arm `optimized`) before the before-reading, because the check chat's spend
  must fall outside the measured span.
- C_normal: connector OFF. Fully quit and reopen Claude Desktop if it
  changed.

Report (`kind: "self_test_session_report"`):

- per method: spend, correct of N, and cost per correct;
- `B_minus_C` and `B_vs_C_percent`;
- the date reconciliation status;
- `verdict`, one of:
  - `savings_observed`: B is cheaper, and its quality is not worse
    (correct_B ≥ correct_C − tolerance), and the dates passed;
  - `cost_increase_observed`;
  - `no_difference`: equal to the cent;
  - `none`, with reasons: `study_incomplete`, `reconciliation_pending`,
    `reconciliation_failed`, `quality_regression`;
- `scope`: "one paired session on this account, model setting and task set;
  no confidence interval".

CLI and launcher:

- `self_test_cli` `init`, `run`, `status`, `report` and `reconcile` dispatch on
  the pack's `kind`. Existing single-question packs keep working unchanged.
- The run flow: setup, the before reading, then question 1..N (copy with C,
  paste the ANSWER line, V to void the chat), then the after reading. There is
  no countdown anywhere in this flow.
- The kit (`build_kit.py`) builds the session kit by default: `--profile
  session` with the study `st_session_v1`.
- The kit's GUIDE.txt describes two long chats of about 12 questions, about
  1 to 1.5 hours in total, then the reconcile step about 4 hours later.

Tests:

- validation;
- the order seed is deterministic;
- a full simulated run to the report for each verdict;
- a void and redo;
- resume after quitting mid-chat;
- reconciliation pass and fail;
- CLI end to end with a fake console;
- existing packs unaffected.
