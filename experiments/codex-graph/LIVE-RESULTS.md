# Live Horizon source-review comparison

gpt-6.1-sol, high reasoning; initial review plus 20 follow-ups per arm.

| Phase | A input | B input | A cached input | B cached input | A output | B output | A USD | B USD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| initialReview | 289718 | 376280 | 243200 | 300160 | 2676 | 3409 | $0.1441 | $0.2163 |
| next20 | 15467094 | 15116879 | 15141888 | 14818560 | 28513 | 28683 | $2.4497 | $2.3653 |
| combined | 15756812 | 15493159 | 15385088 | 15118720 | 31189 | 32092 | $2.5938 | $2.5817 |

Graph B used 263,653 fewer input tokens (1.67%) and cost $0.0122 less (0.47%) in this single pair.

Costs use observed cache hits at $2/M input, $0.10/M cached input and $10/M output. Cached tokens are part of input; reasoning tokens are part of output. Total input includes history carried into repeated model requests. These are API-price equivalents, not subscription invoices.

If all of the same input usage were priced uncached, the arithmetic would be $31.8255 for A and $31.3072 for B. That is a pricing scenario, not another measured run.

B made 3 graph calls during initial discovery; one class trace failed and its cost is retained. Later steps used source reads and existing context. The blocked pilot is excluded from the comparison and records $0.1613 in additional main-model API-price-equivalent usage.

Both arms completed 21 turns. All 2659 source hashes remain unchanged; temporary sign-in copies were removed. A has 234 file/line citations across 61 files and B has 235 across 59; no referenced file/line was missing. This verifies citation existence, not semantic correctness.

Equal prompts and snapshots do not prove equal review quality. One stochastic pair with a small gain does not establish reliable future session savings. The test used the installed CLI engine with fresh profiles and isolated source snapshots, rather than automated desktop UI chats. Only main-model usage is available; auxiliary approval-review usage is not exposed. See live-results.json for quality checks, cache limitations, pilot costs and no-cache estimates.
