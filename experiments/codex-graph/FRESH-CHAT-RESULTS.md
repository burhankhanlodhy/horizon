# Reusing the graph across fresh chats

Eight paired independent chats: four tasks, two repetitions, same frozen source and gpt-6.1-sol/high. A uses ordinary source retrieval; B has unmodified 0.2.3 graph integration. Every B chat includes setup overhead; the local index is reused.

| Task (two chats per arm) | A input | B input | A USD | B USD | Dollar change with graph |
|---|---:|---:|---:|---:|---:|
| architecture | 603,774 | 913,846 | $0.3049 | $0.3877 | 27.16% |
| request-flow | 757,379 | 1,000,047 | $0.3426 | $0.3555 | 3.77% |
| savings-accounting | 553,458 | 703,603 | $0.2555 | $0.3464 | 35.58% |
| focused-control | 55,391 | 61,054 | $0.0498 | $0.0888 | 78.37% |
| **Total** | **1,970,002** | **2,678,550** | **$0.9528** | **$1.1785** | **23.68%** |

Overall graph input reduction: -35.97%. Dollar reduction: -23.68%. Positive reductions mean savings; negative values mean extra usage/cost. 1 of 8 pairs cost less with the graph.

Costs use observed cached input at $0.10/M, uncached input at $2/M and output at $10/M, verified against official pricing on 2026-10-21. These are main-model API-price equivalents, not subscription invoices. Auxiliary approval-review usage is not available. No-cache amounts in JSON are pricing scenarios, not additional runs.

The local graph built in 7.86 seconds and used zero model tokens. B made 12 graph tool calls. All 2659 source hashes were preserved in both arms. All 16 conversation IDs are distinct; sign-in copies were removed.

Citation checks: A 118 citations (0 invalid file/line locations); B 113 (0 invalid). File/line existence does not establish claim correctness.

Manual source-grounded coverage review is recorded in fresh-chat-quality.json. It is a limited rubric review, not an independent blind assessment or proof of semantic equivalence.

This is an exploratory sample, with automatic provider cache state and only two repetitions per task. First-arm order alternates by case but remains the same per task across repetitions. Do not generalize a favorable task or pair into a product-wide savings promise. See FRESH-CHAT-PROTOCOL.md and fresh-chat-results.json for prompts, per-pair outcomes, outputs, failures and limitations.

Interrupted attempts: 1, with $0.0809 of known additional main-model usage. These attempts are preserved separately rather than substituted for completed answers. Total experiment usage including interruptions is $2.2122. A usage-limit interruption separated the two arms of the first savings-accounting pair; cache state/timing may therefore differ further for that pair.

Post-hoc sensitivity check excluding both arms of the quota-interrupted pair: graph input reduction -31.90%, dollar reduction -18.70%. This is a sensitivity check, not a substitute for the prespecified overall result.

Retrieval diagnostics: A made 90 shell calls and returned 255,798 one-pass tool-output tokens; B made 73 shell calls plus 12 graph calls and returned 264,397 tokens. These output sizes use local o200k_base tokenization and are not total provider input or separate charges. Tool calls include source searches, reads and directory listings.

Interpretation: this implementation reuses structural relationships, not the conclusions of earlier source reviews. In this sample it did not reliably replace enough source investigation to reduce whole-chat usage. There were two failed class traces; the adapter advertises general symbols while the native trace expects an exact function/method name. The first architecture search also returned test symbols ahead of application entry points. Failures and their costs remain included. Individual lookup estimates must not be described as measured whole-chat savings.

Recommended next work: clarify callable-only tracing and missing-symbol errors; improve production-source relevance; combine graph locations with compact current-source evidence so one retrieval can replace multiple reads; and investigate source-hash-validated persistent review summaries. Validate changes with fresh held-out questions and multiple repositories before claiming a repeatable savings benefit. Existing cost results do not establish savings from any of these proposed changes.
