# Repeated fresh-chat experiment

Fixed before inspecting outcomes: eight paired independent chats, four tasks
repeated twice, using gpt-6.1-sol/high and the same eligible frozen source corpus
as the earlier 21-turn experiment. A has ordinary source retrieval; B has the
actual local-preview 0.2.3 graph integration and its unmodified routing policy.
Every chat begins without conversation history. No graph calls are prescribed.
The focused-control prompt explicitly names a file and should favor direct reads.
Order alternates A-first/B-first by case, with the same first arm per task in
both repetitions; task-specific order effects remain a limitation. The graph is
built once before model calls and kept between fresh
chats. Schema/guidance overhead is included in every B chat's observed usage.

The runner uses separate temporary homes and source copies for A and B, excludes
personal configuration, and never resumes chats. The homes persist only to reuse
the graph installation and sign-in; no earlier conversation is supplied. Prompts
forbid parent-folder reads, credentials, network, edits and delegation. Tools use
the installed Codex CLI engine and automatic approval review. This is not a
desktop UI experiment. Temporary sign-in copies are removed when the runner exits.

Primary outcomes: observed input tokens and main-model dollar equivalents using
actual provider cache hits. Also report uncached/cached input, output, wall time,
graph use/failures, source/tool output volume, per-task and per-pair outcomes.
No-cache dollars are a pricing scenario, not a separate measurement. Graph build
uses no model calls. Approval-review model usage is not exposed and cannot be
included. No production proxy or savings ledger participates.

Keep failed attempts and their known usage. Stop on a failed turn instead of
silently rerunning or discarding it. The first B savings-accounting chat hit an
account usage limit. After the user said Continue and the usage API confirmed
availability, that partial attempt was archived with its known usage and restarted
as a new fresh chat. Completed pairs remain untouched. Report the interruption's
usage separately and include it in the total experiment cost. The time gap is an
additional cache-state limitation for that pair. Do not select favorable cases for the overall
total. Two repetitions per task are exploratory, not a confidence guarantee.
Provider cache state is automatic, not experimentally held equal.

Quality review checklist (inspect actual claims against frozen source):

- Architecture: identifies API/control plane, proxy/provider path, transforms,
  client/desktop integration and dashboard; does not claim exhaustive coverage.
- Request flow: identifies entry point, authentication/routing, transformation,
  provider forwarding, usage reporting and a supported failure boundary.
- Savings accounting: distinguishes provider usage, compression savings and graph
  estimates; explains duplicate receipt handling and inference-count separation.
- Focused control: correctly explains discovery intent, direct reads for known
  locations, verification/coverage limitations and file-scoped estimate exclusion.

Citation file/line checks measure existence only. Manual checklist review does not
prove full semantic equivalence. A shorter or cheaper answer alone is not success.
Any recommendation for selective graph use drawn from outcomes is a hypothesis
requiring fresh validation, rather than an independently validated improvement.
