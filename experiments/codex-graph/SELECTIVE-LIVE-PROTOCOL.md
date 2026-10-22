# 0.2.4 paired chat pilot

Fixed before live calls: one fresh conversation per arm, each with five identical
turns: architecture discovery, request flow, known-file pricing review,
known-file graph accounting review, then synthesis/reuse. Model gpt-6.1-sol,
high reasoning. A uses normal source retrieval; B has the actual 0.2.4 installed
project graph runtime and its unmodified routing policy. No graph call is forced.
Calls alternate AB/BA/AB/BA/AB. Each follow-up resumes its own arm's chat.

Source is the earlier eligible source-file list, reread from the current worktree,
plus the new api/savings_ledger.py. Benchmark files and the graph feature's
GRAPH.md/VALIDATION.md are excluded to avoid priming agents with benchmark
outcomes. Both arms get identical frozen files/hashes. Only B gets the owned
project AGENTS/MCP configuration. Its local graph is built once, without model
calls, and reused. The three schemas and routing guidance are part of B usage.

Isolated Codex homes contain temporary sign-in copies only while running.
Personal config, production proxy and savings ledger do not participate.
Agents may use local source/tools but may not edit, access the network, delegate,
or invoke external models. Temporary sign-in copies are removed on exit,
including failure. Failed attempts are retained and never silently retried.

Primary comparison is observed provider input, cached input, output and
main-model standard API-price equivalents ($2/M uncached input, $0.10/M cached
input, $10/M output). Turn usage is the delta of cumulative rollout usage;
cached input and reasoning are subsets, not counted twice. Prices verified
21 October 2026 in the preceding experiment. Subscription quota/fees and
auxiliary approval-review usage are not measured. Cache state is automatic.

Review coverage and claims, not only answer length. Architecture should cover
proxy/transforms, API/control plane, dashboard and desktop/client integration.
Flow should cover conditional auth, routing, transforms, transport, reporting
and a supported failure boundary. Pricing should distinguish usage/estimates
and cache/unknown-price behavior. Accounting should correctly explain zero
graph dollar credit, exclusion of historical rows and replay/count separation.
Synthesis should reuse supported findings and distinguish hypotheses.

One chat per arm is an exploratory pilot, not a replicated estimate or proof
of equal quality. Compare initial turn and cumulative five-turn totals, keep
all turns, verify original hashes/citation locations, report actual graph use
and errors. Do not compare against the old experiment as though the corpus,
prompt sequence or cache state were unchanged.

## Execution amendment: service capacity

B's first pricing attempt failed with "Selected model is at capacity" after
one completed source-read round. The interruption was announced, its raw evidence
and known usage archived, and one same-model/same-chat retry allowed. No model
switch or hidden retry. Report successful-turn totals and all observed usage
including the failure separately. Partial context from the failure remains in
B and may affect later turns, so subtracting its direct cost does not yield
an uncontaminated experiment. The first two paired turns predate the failure.
