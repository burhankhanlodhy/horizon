# Actual Graphify: fresh-chat comparison

Published Graphify 0.9.93, using its bundled Codex skill/native routing, against ordinary source retrieval. Same frozen repository and gpt-6.1-sol/high; four tasks repeated twice, sixteen fresh conversations. Code-only index, clustering without paid semantic labels, generated query/reflection memory allowed between B chats. This does not test Graphify Cloud or semantic document/media enrichment.

| Task (two chats per arm) | Ordinary input | Graphify input | Ordinary USD | Graphify USD | Cost reduction with Graphify |
|---|---:|---:|---:|---:|---:|
| architecture | 625,302 | 1,508,849 | $0.3362 | $0.5191 | -54.41% |
| request-flow | 747,606 | 1,234,451 | $0.3995 | $0.4328 | -8.33% |
| savings-accounting | 565,508 | 1,114,593 | $0.3121 | $0.4219 | -35.18% |
| focused-control | 60,517 | 283,608 | $0.0640 | $0.1376 | -114.86% |
| **Total** | **1,998,933** | **4,141,501** | **$1.1118** | **$1.5113** | **-35.94%** |

Input reduction: -107.19%. Dollar reduction: -35.94%. Positive means savings; negative means higher usage/cost. 1 of eight pairs cost less with Graphify.

Dollar figures use observed cache hits and standard rates of $2/M uncached input, $0.10/M cached input and $10/M output. These are main-model API-price equivalents, not subscription charges. Cached input is already included in input; reasoning is already included in output. Auxiliary automatic-approval usage is unavailable.

Final graph: 59,124 nodes, 142,827 edges. Its final build took 113.67 seconds and zero model tokens. An earlier local build revealed the missing optional SQL parser; that parser was installed and the graph rebuilt before any model calls. Total local setup computation across both builds: 202.49 seconds, zero model tokens. Structural graph hash unchanged across chats: true.

All 2659 original source hashes remained unchanged in both arms; all 16 conversation IDs are distinct; temporary sign-in copies were removed. Original-source citation location checks: A 115 citations / 0 invalid; B 115 / 0 invalid. Graph-only/generated-state citations may need separate interpretation.

Observed Graphify command counts: {"query":14,"reflect":6,"path":8,"save-result":6,"explain":7}. Generated saved-answer and reflection files are listed in graphify-results.json.

Manual coverage/claim review is recorded in graphify-quality.json; consult it before interpreting lower cost as equal task completion. This is not an independent blind quality assessment.

The package skill, native AGENTS routing and prompts are part of this treatment. The index excludes semantic enrichment; some implementation details may be unavailable from structural evidence. Cache state was not equalized. Two repetitions and one repository provide exploratory results, not a universal savings guarantee. See GRAPHIFY-PROTOCOL.md for the fixed design and graphify-results.json for per-pair data, failures and limitations.

## Interpretation

This workflow did not save whole-chat tokens or dollars on this repository. Graphify cost more in seven of eight pairs; its one cheaper request-flow answer also omitted authentication conditions covered by its baseline. Both Graphify architecture answers omitted desktop integration, and the second largely omitted the separate control-plane architecture. Other answers broadly met the limited coverage rubric. This does not establish equal answer quality.

One-pass tool-output tokens fell from 344,631 to 270,214 (21.59% less), while shell calls rose from 84 to 94. These output counts are locally tokenized retrieval diagnostics, not provider usage. Total model input includes instructions and conversation context on successive model calls, so smaller retrieval alone did not overcome the complete workflow's overhead. This experiment cannot isolate each overhead source without an ablation.

Cached input was 1,608,704 for A and 3,688,960 for B; uncached input was 390,229 versus 452,541, and output was 17,047 versus 23,737. Observed caching is already included in the dollar comparison. Reusing a local graph does not remove subsequent query, verification, or conversation costs.

Six saved-answer files and a reflection file were retained between Graphify chats. Their existence proves persistence, not a causal savings benefit. The structural graph was unchanged. Native Codex memory databases had zero stage-one outputs and zero jobs in both arms at completion; initialization rows alone do not indicate generated-memory reuse.

The summary's legacy graphCalls field counts MCP-tool events; this native Graphify skill uses shell commands. Actual Graphify use is recorded under memory.invocations and per-chat retrieval.graphifyCalls. Nonzero command exits are retained in quality.toolFailures; some are normal unmatched searches, not failed conversations. No conversation was interrupted or retried.

Practical implication: retain direct source retrieval for known files/symbols, and test selective compact graph discovery or impact analysis before enabling broader graph-first routing. Estimated savings should not treat reduced retrieval output as demonstrated whole-session savings. This benchmark changed no production code or ledger.
