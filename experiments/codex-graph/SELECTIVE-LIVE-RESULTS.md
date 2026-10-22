# 0.2.4: paired five-turn chat pilot

Model gpt-6.1-sol, high reasoning; 2502 identical frozen source files. Two independent conversations, five turns each.

| Phase | A input | B input | A cached | B cached | A output | B output | A USD | B USD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| initialReview | 281,684 | 423,708 | 224,512 | 363,520 | 3,257 | 3,795 | $0.1694 | $0.1947 |
| uninterruptedFirstTwo | 742,150 | 771,560 | 659,712 | 677,888 | 5,471 | 5,546 | $0.2856 | $0.3106 |
| followUps | 1,596,092 | 1,558,516 | 1,526,016 | 1,339,520 | 5,877 | 5,817 | $0.3515 | $0.6301 |
| combined | 1,877,776 | 1,982,224 | 1,750,528 | 1,703,040 | 9,134 | 9,612 | $0.5209 | $0.8248 |
| observedIncludingInterruptions | 1,877,776 | 2,059,130 | 1,750,528 | 1,777,152 | 9,134 | 9,769 | $0.5209 | $0.8394 |

Input reduction (positive means less for B): -5.56%. Dollar reduction: -58.34% ($-0.3039).

B made 1 graph calls; A made none. Graph indexing used no model tokens. Both conversations completed five turns; source hashes are unchanged and temporary sign-in copies removed.

Costs include observed cache hits at $2/M uncached input, $0.10/M cached input and $10/M output. These are main-model API-price equivalents, not subscription charges or extra-turn estimates. Total input includes history repeatedly supplied to the model.

## Quality and limits

Citation locations: A 59 citations across 23 files, 0 invalid; B 63 across 23, 0 invalid. Existence checks do not establish correctness. Nonzero search exits are recorded and can mean no matches.

Primary reviewer read all ten successful answer files and inspected relevant source implementations and tests. Limited coverage review, not independent blind scoring or proof of equal semantic quality. Citation existence is checked separately.

Both arms broadly completed the requested review with supported findings. A's request-flow answer narrowed to hosted-account mode and omitted the separately configured operator token gate; B covered that gate. No major unsupported claim was identified in this limited review. A smaller or cheaper answer is not automatically equally useful.

Turn 0 (architecture): A met; B met. Both cover Python/Rust proxy/transforms, account/control plane, dashboard and desktop clients, request/analytics data flow and supported risks. B additionally identifies documentation drift. Neither claims an exhaustive security audit.

Turn 1 (Responses request flow): A partial scope; B met. Both cover account authorization, routing, compression, transport, buffered usage/outbox and supported failure boundaries. A explicitly scopes to hosted mode and omits the separate configured non-loopback operator gate. B includes both authentication conditions. Streaming usage finalization is not comprehensively traced.

Turn 2 (cache-aware savings pricing): A met; B met. Both explain live/prefix regions, cache-read/write prices, no-mix list fallback, unknown-price fallback versus unpriced, free-model zero pricing and usage versus estimates. B also flags the source comment calling list price an unconditional upper bound; cache-write premium tests support that concern. B completed this turn after a preserved capacity interruption.

Turn 3 (graph ledger and fee accounting): A met; B met. Both correctly explain zero graph dollar credit, exclusion of signed historical graph rows from new monetary totals, zero inference counts, API normalization, retained already-recorded fees, account-scoped receipt identity and durable duplicate suppression. Their brief fee-threshold wording should be read with the cited billing formula, which charges 5% of full-cycle eligible savings once the $20 threshold is exceeded.

Turn 4 (reuse/synthesis): A met; B met. Both use supported earlier findings and explicitly distinguish facts from efficiency hypotheses. Neither makes a new tool call. Recommendations do not claim proven runtime savings.

One capacity-interrupted B attempt is retained with known cost; retrying the same conversation leaves extra partial context. Subsequent cache mix differed substantially. This prevents treating the full-session dollar difference as an isolated causal graph effect.

Both arms had two nonzero shell exits during request-flow inspection; those requests and outputs are retained and charged in totals, rather than silently removed.

Only B's first turn used the graph: one overview. All focused follow-ups used source, and both final syntheses reused context without tools. The selective policy worked in this pair, without delivering lower total usage or cost.

Native Codex memory database stage1_outputs and jobs were zero in both profiles at completion; no native generated-memory reuse was observed. The graph index timestamp remained unchanged.

This is one pilot pair, with alternating arm order and automatic cache state. It cannot establish repeatable savings. It is not comparable to earlier experiments as an isolated version effect: prompts, corpus and cache state differ. Production ledgers, personal config and deployed services were unchanged. See SELECTIVE-LIVE-PROTOCOL.md and selective-live-results.json for evidence and limits.

## Capacity interruption

One B pricing attempt failed with selected model at capacity. Its evidence and known usage are retained separately and included in observedIncludingInterruptions. combined sums the five successful turns only. The same chat resumed after the failure, so the extra partial context can affect later calls; excluding the failed attempt's direct cost does not remove that confound. The uninterruptedFirstTwo comparison is unaffected by this failure. No model was switched.

## Interpretation

Savings did not hold in this pilot. Across successful turns, B used 5.56% more total input and cost 58.34% more. Including known interruption usage, cost was $0.8394 versus $0.5209. The uninterrupted first two turns already cost 8.77% more for B; their input was 3.96% higher.

The extra uncached input in the interrupted pricing phase dominates the full-session dollar gap. It is not an isolated causal estimate of graph overhead. Locally tokenized one-pass tool outputs were 82,799 for A and 71,128 for B (14.10% smaller), while successful-turn tool calls were 32 versus 24. Smaller retrieval/fewer tools did not translate into lower whole-chat usage. These local diagnostic counts differ from provider usage.

The policy routed selectively: one graph overview, then source-only focused work and tool-free synthesis. Both architectures covered the expected components; the limited manual review found generally useful answers, with A's operator-auth omission. This pilot does not validate a graph savings claim or a dollar credit. Keep graph use optional and diagnostics uncredited until replicated comparable sessions show a net gain.

This evaluates the graph feature alone. Neither arm used production proxy compression, so it does not measure ContextShrink's separate compression savings.
