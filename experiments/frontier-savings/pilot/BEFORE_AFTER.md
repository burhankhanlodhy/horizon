# Existing proxy versus automatic compact edits

October 9, 2026. Agreed policy: an always-active decision controller selects
profitable supported edits automatically, with no end-user feature toggle.

## Current implementation status

The repository contains request compression, source-read protection, cache-stable
prefix replay, tool-schema compaction, retrieval, output shaping and model/price
policies with their own activation conditions. Some are optional; their presence
in code does not prove activation on Pi 5.

Compact edits now have an isolated preview MCP experiment, an automatic proxy
framework and an implemented production adapter core in
`horizon/proxy/compact_edits/`. It supports complete-JSON Claude native Edit and
Codex function/custom patch translation, durable replay and qualified admission.
The controller is present in proxy startup/outcome/stats, without a user toggle.
The Claude cohort boundary now has local/native-client validation, including one
live pair with **18.1% lower API-equivalent cost** on a four-row fixture. Its
explicit script control was incomplete. **Economic/full-pipeline qualification
and deployment remain pending; additional measured/credited production savings
are zero.** See the [cohort review](../cohort/REPORT.md). The pilot's
40–61% figures are not the current proxy's overall saving. See
[adapter implementation notes](../../../docs/compact-edit-adapters.md).

There is no paired complete-workflow benchmark of the existing deployed proxy
versus an activated bridge for Claude Code, OpenCode or Codex. The current total
dollar savings percentage, future total and measured difference are unknown.
The new feature contributes zero credited production savings until integration
and evaluation establish otherwise. The follow-up made three bounded live
evaluation sessions; their costs and limitations are reported separately below.

## Capabilities before and after integration

| Area | Existing proxy | Automatic compact-edit bridge |
|---|---|---|
| Optimization | Eligible input compression, cache stability, separately gated output/price policies | Also reduces generated source-edit arguments for supported repetition |
| Model edit output | Generates native patch/search-replace text | Generates short receipt-bound operation, expanded deterministically in proxy |
| Coding client | Executes normal tools and permissions | Same native execution/permission path; no feature toggle |
| Selection | Existing compression/routing policies | Additional always-active admission/use controller; costly or unsupported edits use native tools |
| State | Compression/cache/history trackers | Also exact observed source and durable provider/client call journal |
| Streaming | Existing forwarding/buffering policies | Certified translated routes initially buffer affected responses; latency is an added tradeoff |
| Accounting | Existing input/cache/output and priced-policy measurements | Also track compact output, schema/receipt costs, recovery and counterfactual uncertainty |
| Billing | Approved savings components | Experimental output estimates stay outside the fee base until qualified |

The controller complements existing compression. It must not duplicate its
credits. Source correctness, native execution, streaming and replay are the
main engineering work needed to implement this design.

## Benefit by coding tool

| Tool | Relevant existing support | Potential compact-edit benefit | Evidence now |
|---|---|---|---|
| Claude Code | Anthropic request, source-read and prefix/cache policies | Repeated literal catalogs through a certified native Read/Edit adapter | Five native-client checks passed; one live pair saved 18.1%; full-pipeline/economic qualification pending |
| OpenCode | Provider-native paths, depending on actual model/route/tool contract | Similar repetitive edits after certifying its installed read/edit or patch adapter | No live compact-edit OpenCode test |
| Codex | OpenAI Responses/function/custom/WS paths with separate policies | Repetitive patches after grammar, transport and replay certification | No live compact-edit Codex test |

OpenCode documents native read/edit/patch and bash capabilities, plus edit
permissions. Native scripted transformations therefore belong in the baseline,
and normal client permissions must survive translation. Documentation alone
does not prove compatibility for an installed version.
See [OpenCode tools](https://opencode.ai/docs/tools/).

The actual protocol/tool contract matters more than the application name. Unknown
versions, wrappers, missing source and unsupported transports use native
passthrough without asking users to make an optimization decision.

For metered API customers, validated lower billable generation can reduce dollar
spend. For a fixed-price coding subscription, it may reduce quota consumption
without reducing the subscription invoice. The pilot used Claude Pro and reports
API-equivalent usage cost, not invoice savings.

## New native-client evidence

Claude Code 2.1.295, existing configured model `claude-sonnet-5-5`: one native
four-row workflow cost $0.0377830; compact cost $0.0309524. Both passed, a
$0.0068306 / 18.1% difference. The separate script workflow failed after a native
Bash denial and harness errors; its $0.0175930 partial cost is not a successful
baseline. Both successful arms had Bash in their catalogs, but that alone does
not prove script execution was permitted. Fixed arm order, one pair and no full
stock-proxy benchmark prevent fleet claims. Shared completed usage is $1.2001388
API-equivalent, with prior usage plus the new conservative bound at $1.9770944,
within the unchanged $3 research ceiling. See [the review](../cohort/REPORT.md).

## Earlier MCP per-task costs

Mean over three repetitions, CLI API-equivalent USD. Valid insertion positions
were adjudicated separately from the original strict AST grades.

| Task | Ordinary edit | Compact edit | Compact change |
|---|---:|---:|---:|
| Add four catalog rows | $0.025313 | $0.015126 | **40.2% cheaper** |
| Add twelve rows, adequate output limit | $0.046395 | $0.018222 | **60.7% cheaper** |
| Five repeated keywords | $0.014302 | $0.014551 | 1.7% more expensive |
| One function literal | $0.011087 | $0.011315 | 2.1% more expensive |
| New logic through fallback | $0.022062 | $0.024295 | 10.1% more expensive |
| One module constant | $0.016826 | $0.022893 | 36.1% more expensive |

The four non-clone types collectively cost 13.7% more. Avoiding their codec
overhead is a controller objective, not a measured result of an installed
controller. The successful repeated-edit comparisons used exact search/replace
as control; scripts were disabled, so the best native workflow may be cheaper.

Do not use the primary suite's 48.3% aggregate reduction as a proxy headline:
its largest native task failed from a low output cap. Excluding that task leaves
only 1.6% aggregate reduction in the remaining suite. The successful higher-limit
twelve-row comparison is reported above as a separate sensitivity run.

## Correct before/after calculation

Let `B` be comparable cost without ContextShrink, `C` cost with the existing
proxy, and `G` additional **net** avoided cost from compact edits, including
non-use schema overhead and failed recovery.

```text
old saving against no proxy = (B - C) / B
new cost = C - G
new saving against no proxy = (B - C + G) / B
additional reduction of current bill = G / C
```

A 40% saving on one edit cannot be added as forty percentage points to an
account's previous savings. Different denominators cannot be mixed.

For illustration, `p` is the eligible tasks' share of the **current dollar bill**
and `r` their net cost reduction. If all other costs stay equal, `G/C = p*r`.
`p` is not the share of tool calls or output tokens. Production/session overhead
not included in `r` must be subtracted separately.

| Eligible share of current bill | Illustrative reduction using pilot 40.2–60.7% | Overall saving if the old proxy saved 30% |
|---|---:|---:|
| 0% | 0% before overhead; negative if admitted unnecessarily | 30% before overhead |
| 10% | 4.0–6.1% | 32.8–34.3% |
| 25% | 10.1–15.2% | 37.0–40.6% |
| 50% | 20.1–30.4% | 44.1–51.3% |

**30% old savings and these workload shares are hypothetical.** They are not
measurements of this project. Scripts, different models, real caches and
recovery may shrink or erase the gain.

At pilot cost, 1,000 comparable twelve-row tasks imply about $28.17 avoided
API-equivalent usage cost. This is a scenario, not a customer forecast or an
amount that may be added to the account ledger.

## Required evidence for actual percentages

1. Same workspaces, tasks, model, price/service tier and correctness requirements.
2. Existing deployed proxy as control, bridge as treatment, with normal native
   script/replace-all options available.
3. Complete-workflow usage, real edit outcomes, cache categories, reasoning,
   all internal calls, non-use/admission costs and latency.
4. Representative independent sessions per client/adapter/model. Three repeated
   authored tasks are not a production workload sample.
5. A separate no-proxy control if reporting total savings versus no proxy.
   Historical aggregate ledger numbers do not supply that counterfactual.
6. Signed cost deltas including losses and an explicit uncertainty basis.

These measurements can establish a client/model-specific uplift, not one
universal percentage for all Claude Code, OpenCode and Codex accounts.

See [PROXY_AUTOMATION_FRAMEWORK.md](PROXY_AUTOMATION_FRAMEWORK.md) for the
automatic admission/skip rules, and [RESULTS.md](RESULTS.md) for live observations.
