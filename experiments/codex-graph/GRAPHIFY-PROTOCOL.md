# Actual Graphify versus ordinary retrieval

Primary comparison: eight paired fresh Codex chats, four tasks repeated twice.
Tasks match the earlier fresh-chat experiment, including the known-file control.
Each case uses gpt-6.1-sol/high and the same frozen 2,659-file source corpus.
New A baseline chats are measured contemporaneously rather than borrowing old A
costs. A-first/B-first alternates within each repetition, and each task swaps its
first arm in the second repetition. Provider cache hits are automatic and observed, not
forced equal. Prompts forbid source edits, delegation, new dependencies and
external model/network calls by the agent. All conversations begin fresh.

B is published `graphifyy[mcp,sql]==0.9.93`, installed in an isolated Python environment.
Its final code-only AST graph is built before model calls using `graphify extract --code-only`, with
clustering and report generation via `cluster-only --no-viz --no-label`. Paid
semantic extraction of documentation/media and model-generated community labels
are excluded. This tests local open-source Graphify, not Graphify Cloud. Original
documentation remains available to ordinary source retrieval. Report actual graph
coverage and quality limitations rather than assuming code-only indexing contains
the same semantic information as every source/documentation file.

The unmodified bundled Codex skill and native routing are installed only in B's
temporary project. Its Python runtime is on B's PATH. B prompts explicitly invoke
`$graphify`; the underlying task is identical to A. The skill's own query workflow
governs vocabulary expansion, traversal, source-location evidence, saved results
and reflections. Graphify-generated vocabulary, saved answers and reflections
under `graphify-out` may persist across B chats. Original source may not be edited.
The index is not deliberately rebuilt between chats. Record whether work-memory
steps actually execute; do not invent useful/correct outcomes to seed memory.

The skill instructs answers to rely on graph evidence and admit missing coverage.
Therefore lower token usage alone does not establish equivalent task completion.
Review each answer against the earlier coverage rubric, especially the ability to
explain behavior, pricing, deduplication and deployment conditions. Distinguish
explicit graph facts, inferred claims, unsupported details and honest omissions.

Count observed provider input, cached input and output, main-model dollar
equivalents, latency, tool outputs, Graphify query/reflect/save-result calls and
failures. Graph construction/install wall time is separate and uses no model
tokens in this mode. Approval-review model usage is unavailable. Cached input is
a subset of input, and reasoning output a subset of output. Subscription allowance
and API-price equivalents are not interchangeable.

Keep failed attempts and known costs. Stop rather than silently retrying. On an
inspected transient failure and explicit continuation, archive logs/usage before
retrying a new fresh chat. Include interruptions separately in experiment costs.
Check every original source hash and citation file/line existence. Temporary
sign-in copies must be removed on runner exit. No production proxy/ledger or
personal Codex installation is changed by the experiment.

With two repetitions per task and one repository, results are exploratory. Do not
generalize cost savings from incomplete answers, or from this local code-only
mode to Cloud, semantic enrichment, other repositories or subscription quotas.
