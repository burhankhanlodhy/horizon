# Codex graph estimates in the existing account ledger

The requested destination is the existing **Est. savings** ledger. This change
adds no feature-specific charge, subscription, Stripe product, meter, or invoice
line. Existing savings-based billing rules remain unchanged.

## What is measured

The graph's MCP search result can contain a compact estimate receipt. The local
client counts a bounded literal repository-search alternative with a pinned
`o200k_base` tokenizer. This is a named counterfactual, not proof of what an
autonomous model would otherwise have done. Focused definition search can cost
less, as the paired retrieval benchmark shows.

The proxy accepts receipts only from paired graph-search tool outputs, including
Codex code-mode wrappers. User messages and unrelated tool outputs do not qualify.
The proxy recounts both the actual tool result (including receipt metadata) and
its invocation, then subtracts an additional 1,024 tokens per query for schemas
and project guidance:

`estimated tokens avoided = bounded alternative tokens - actual result tokens - invocation tokens - 1024`

Results are signed. Losses are preserved rather than clamped away. Positive
dollars use the cheapest known input/cache-read rate for the actual request's
model; losses use the ordinary input rate. Unknown prices produce zero dollars
and an explicit unpriced basis. This is an API-equivalent estimate, not a claim
that a flat ChatGPT subscription invoice has fallen by that amount.

Only successful, non-response-cached inference requests can consume a receipt.
Running a local graph CLI command or merely indexing a repository never adds
savings. Other graph operations have no defensible search-equivalent baseline
in this version and receive no estimated savings credit. Very small response
budgets omit metadata rather than displace useful graph results.

## Ledger and deduplication

Receipt IDs derive from the local project identity, source fingerprint, and
bounded search-output digest. Account-scoped UUIDs remain stable across keys,
proxy restarts, and conversation replays. The durable SQLite outbox and the
existing PostgreSQL `ON CONFLICT(event_id) DO NOTHING` ingestion make retries
idempotent. A replay is not a new credit. Identical searches in future chats
on the same unchanged snapshot are conservatively counted only once.

`kind=graph_estimate` rows land in the existing `metrics.proxy_events` ledger.
Their signed `savings_usd` enters **Est. savings** exactly once, including the
existing billing estimate and npx savings summary. The dedicated subtotal is
for explanation, not an additional amount to add to the total.

These rows contribute no inference requests, billed input/output tokens,
compression tokens, compression denominator, or additional actual model cost.
`graph_tokens_avoided` is a separate signed estimate, displayed in a local-graph
breakdown on the Usage page. Ordinary compression counters keep their existing
meaning. The source content avoided by the graph was never sent to the proxy,
so it cannot overlap proxy compression of content actually received.

Receipt metadata persists the baseline, actual token count, overhead allowance,
tokenizer, confidence, model, rate, and pricing basis. Repository contents,
query text, tool arguments, raw results, and user credentials are not copied
into the account ledger. This remains a client-measured estimate, not a signed
attestation or independently verified counterfactual.

## Rollout and validation

The control plane advertises graph-event support during account-key authorization.
The proxy defaults to disabling new graph rows when talking to an older API, so
an unsupported event cannot poison the analytics upload queue. Deploy the API
before the proxy; the dashboard can be deployed independently. Then use preview
0.2.1 and rerun `graph init` to update the owned MCP runtime, restart Codex, and
query from a chat in that project. Existing settings/instructions are preserved.

No production deployment or live ledger backfill is performed by this experiment.
See `experiments/codex-graph/results.json` and `ledger-result.json` for measured
retrieval and local cross-language ledger results. Automated checks cover signed
pricing, unavailable prices, failed requests, code-mode envelopes, invalid
receipts, ownership-independent replay IDs, durable outbox uniqueness, API schema
limits, old-API compatibility, runtime upgrade, and preservation of graph output
budgets. The end-to-end test caught and fixed Windows SQLite handles remaining
open after transactions; outbox connections now close deterministically.

Official accounting references:

- [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching):
  cached and uncached input have different cost implications.
- [OpenAI pricing](https://developers.openai.com/api/docs/pricing): model-specific
  input/cache/output prices; the implementation uses the existing pinned catalog.
- [OpenAI Responses](https://developers.openai.com/api/reference/responses): actual
  provider usage measures sent requests and cannot report hypothetical omitted input.
