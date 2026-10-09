# OneProvider Gemini compact-edit compatibility check

October 9, 2026. Follow-up to [Claude cohort qualification](../cohort/REPORT.md).

## Result

The supplied key authenticated successfully. `GET /v1/models` listed
`gemini-3.8-flash`, `gemini-3.1-pro` and `gemini-3.1-pro-preview`.
**Flash returned a valid receipt-bound compact call**, and the deterministic
expansion passed an independently constructed AST and comment-preservation check.

**No savings percentage or production qualification was established.** The
script comparison was interrupted with unknown usage, native Edit was not run,
and the two Pro IDs were only checked in the model catalog. Further paid calls
were stopped after a returned completion counter exceeded the requested output
cap. Production code, client configuration, Pi 5 and billing remain unchanged.

## Documented route

OneProvider's [model catalog](https://oneprovider.dev/docs/api/models) lists
Gemini under the OpenAI-compatible API. Its
[Messages reference](https://oneprovider.dev/docs/api/messages) documents a
Claude allowlist on the Anthropic endpoint. Therefore this experiment used
`https://api.oneprovider.dev/v1/chat/completions`, with OpenAI function tools.
It did not point Claude Code at an unsupported Gemini Messages model or install
a new production Chat Completions adapter.

The [published pricing](https://oneprovider.dev/pricing), checked October 9:

| ID | Input / million | Output / million |
|---|---:|---:|
| gemini-3.8-flash | $0.75 | $3.75 |
| gemini-3.1-pro | $2.00 | $12.00 |
| gemini-3.1-pro-preview | $2.00 | $12.00 |

Prices below are computed from these rates and returned usage. They are not a
verified cabinet debit, an invoice, or independently authenticated underlying
model identity. The response identified itself as the requested Flash ID.

## Observations

| Request | Outcome | Returned usage | Catalog-priced cost |
|---|---|---|---:|
| Flash tool smoke | `ping` function call, HTTP 200 | 52 prompt + 53 completion | $0.00023775 |
| Flash four-row Python-script control | Tool execution window ended before settlement; no response captured | Unknown | Unknown |
| Flash four-row compact operation | Valid bound compact tool call, HTTP 200 | 1,630 prompt + 9,796 completion | $0.03795750 |

The completed compact request took **141.411 seconds**. It requested
`max_tokens=2048` but reported **9,796 completion tokens**. Its returned assistant
content had 2,333 characters and its compact arguments 228 characters. The usage
object provided only prompt/completion/total counters, without a reasoning
breakdown. This evidence cannot determine whether the difference arose from
reasoning accounting, gateway normalization, ignored limits or reporting.

The important budget finding is precise: **`max_tokens` was not demonstrated to
bound the reported billable completion counter.** Do not use that field alone
to establish a safe dollar escrow or a production cost certificate on this route.
Compact payload length alone is also not the complete output bill.

The first script request used a 4,096-token cap and a 60-second execution/network
window. The Node execution timed out and reset its kernel before any response or
usage was saved. That is an incomplete observation, not proof of a model failure
or a zero charge. Its original $0.037605 reservation was retained. An explicitly
recorded continuation tested a different compact request with a 2,048-token cap
and a longer execution window; the interrupted script request was never resent.
After the cap mismatch became visible, the earlier reservation was marked as
**not a verified upper bound**, and further paid calls were stopped.

OneProvider's [documentation](https://oneprovider.dev/docs/llms.txt) allows
multi-minute non-streaming requests. The first timeout consequently does not
establish a service outage. Future testing must distinguish the tool execution
deadline from the HTTP/body deadline and the provider's generation lifetime.

## Budget accounting

- New requests with returned usage: **$0.03819525** at published rates.
- Earlier completed Claude research: **$1.20013880** API-equivalent.
- Combined known-usage estimate: **$1.23833405**, excluding the interrupted call.
- Interrupted call: unknown actual charge; reservation retained, never zeroed.
- Bookkeeping subtotal of prior conservative ledger, twice returned Gemini usage
  and initial interrupted reservation: **$2.09108990**.
- The authorized shared ceiling remains **$3**. The bookkeeping subtotal is
  **not a verified upper bound** because the cap assumption was invalidated.

No claim is made that the unknown interrupted charge was reconciled or that a
provider-enforced cash cap was installed. Before further paid comparisons, obtain
that request's usage/charge from the provider and establish an enforced key spend
limit or a verified limit on all billed reasoning and output. Do not reset this
ledger or delete failed attempts to obtain fresh headroom.

## Experimental scope

`fixtures.py` prepares isolated copies of the existing pricing source and tools
for native-style exact Edit, a Python-only Bash script, and the compact compiler.
Both four- and twelve-row specs exist, but only the four-row requests above ran.
The fixture supplies an authored Read history containing complete source; this
is a controlled model-payload experiment, not an installed client's Read result
or signed-history test. Any later native qualification must use genuine provider
calls/results and retain their opaque metadata.

The compact arm retains native tools in its catalog. Arguments must match the
exact source receipt/table/template/ordered keys; paths cannot be model-selected.
The expansion is checked against an independently constructed expected module
AST and the original comment sequence. No source module is imported/executed.
Native Edit uses unique exact matching within the isolated source. Generated
scripts are never automatically executed: they require manual code review and
a matching review receipt before grading an actual edited file.

No script was received or executed in this live check. No native coding client,
full Horizon compression pipeline, account outbox, replay/resume or local guard
was certified by it. No experimental amount entered account savings or fees.
Claude qualification and Codex's local guard remain separate requirements.

## Local validation and guard refinements

Seven new Python tests passed: independent AST construction for four/twelve
rows, correct/incorrect receipts, native exact matching, foreign-path rejection,
and mandatory review without script execution. Three JavaScript budget checks
passed using mocked HTTP and disposable ledgers, with zero external calls:

1. A reported completion counter above the requested cap stops further calls.
2. An interrupted reservation stops automatic continuation and remains counted.
3. An exhausted shared budget starts no request.

The final runner persists escrow before HTTP, rejects duplicate experiment IDs,
uses the fixed authorized endpoint with redirects disabled, records known usage,
retains ambiguous reservations and refuses continuation after invalidated billing
bounds. These changes prevent further calls after discovering the mismatch;
they do not retroactively turn `max_tokens` into a provider-enforced billed cap.

The credential was held only in the tool runtime's memory and was cleared after
testing. It was not written to settings, environment files, fixtures, logs or
committed reports. Raw model responses and source specs remain in ignored
`runs/`; public results contain model labels, counters and grading outcomes.

## Next useful test

After usage reconciliation and a reliable spend ceiling, run balanced repeated
Edit/compact/script comparisons for each selected model, including all read,
schema, reasoning, replay and repair cost. Use genuine native clients after the
payload study. Measure task completion and latency alongside dollar cost. A
working script can be the cheaper baseline; the current data cannot determine
that or support an overall savings percentage.

Evidence: [results.json](results.json), [fixture grader](fixtures.py),
[budgeted runner](run.mjs), [zero-cost budget checks](budget.test.mjs).
