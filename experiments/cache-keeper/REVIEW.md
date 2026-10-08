# Cache keepalive review and Codex feasibility

Reviewed: 2026-10-07 (America/Chicago).
Code baseline: `c9be387c73ecece9c2f08aa464cfcf1d5bf46188`.
Author: Codex. Intended audience: project owner and Claude reviewing the next implementation.

Scope: assess this commit, its evidence, and a possible Codex adapter. This is a review, not an implementation or deployment. Other outstanding product/billing work is outside this review.

## Assessment

The underlying idea is useful: spend a small amount refreshing reusable context during a pause to avoid a larger input/cache-rebuild cost when the user returns. It preserves the model and conversation content. Its value depends on the pause length, the exact cache prefix surviving, and whether the user returns at all.

The commit is a working opt-in prototype with encouraging Anthropic evidence. It needs lifecycle, isolation, complete cost accounting, and scheduler corrections before enabling it broadly on a shared hosted proxy. The current tests pass but leave several consequential cases uncovered.

Codex is a feasible extension for supported OpenAI API models/endpoints. A separate provider adapter is required. Support on the actual ChatGPT-authenticated Codex endpoint remains unverified; public API documentation alone does not establish that support.

## What the commit implements

- `horizon/proxy/cache_keeper.py`: stores the latest eligible forwarded Anthropic request and schedules refreshes. Defaults: 20,000 minimum context tokens, 256 groups, 64 pending requests, 8-hour idle cap, and assumed return probability of 0.5.
- Five-minute caches are refreshed after 255 seconds; one-hour caches after 3,300 seconds. The loop runs every 15 seconds and sends due requests sequentially.
- Refreshes copy the original request, set `max_tokens: 0`, and disable streaming. Some incompatible output/thinking configurations are skipped.
- `horizon/proxy/handlers/anthropic.py`: captures the outgoing request; `outcome.py` supplies successful-response usage to select the target.
- `horizon/cli/wrap.py`: attaches a launch ID and attempts an exit notification after the subprocess returns normally.
- `horizon/proxy/server.py`: starts the background task when `HORIZON_CACHE_KEEPALIVE=1`; exposes a loopback-only end endpoint.
- `HORIZON_CACHE_TTL_UPGRADE=1h` is a separate option, also off by default.
- Results go to logs and optionally `cache_keeper.jsonl`; they are not integrated as a complete per-account net-cost ledger.

Anthropic documents zero-output prewarming, cache reuse refreshing lifetime, and mixed five-minute/one-hour cache regions. The basic mechanism is supported. Mixed regions must be scheduled according to the region actually being protected. [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

## Experiment evidence and limits

The existing [experiment README](README.md) reports:

| Scenario | Control, both runs | Keepalive, including listed pings | Reduction |
|---|---:|---:|---:|
| Five-minute lane, 12-minute pause | $0.802 | $0.685 | About 15% |
| One-hour lane, 70-minute pause | $1.136 | $0.999 | About 12% |

The second experiment's approximately 24% reduction is for the resumed turn, not both runs or a monthly bill. Resumed cache reads increased from roughly 8.8k to 67.8k tokens. This is useful evidence that refreshes preserved reusable context. The one-hour TTL-upgrade arm in the first experiment cost about 16% more than control; retain its default-off status.

Evidence inspected in this review:

- `live_test.py`, `start.ps1`, the experiment README, and the existing local ledger under `%LOCALAPPDATA%/ContextShrinkExperiment/cache-keeper/keepalive/`.
- That ledger contains four ping events and two resume events. Its resume net values are $0.144210 and $0.255080, and both lack `kept_tokens`.
- Those are the older accounting values described in the README. They predate the final baseline/tool-prefix deduction present in the reviewed commit. Do not cite the stored ledger as a live validation of the final corrected calculation.

Limitations:

1. These are a small number of scripted resume cases, not a randomized population of pauses or users. Never-returned sessions are missing from the main comparison.
2. The arms ran concurrently under the same account with similar context. Shared cache prefixes can contaminate the control; the README already identifies the shared tool-prefix effect. Different keeper IDs do not isolate provider caches.
3. The harness invokes Claude directly with a fixed experiment ID and later resumes it. It deliberately preserves protection between launches. This does not test the normal wrapper's end-on-exit policy, which would stop protection when the first process exits.
4. Answers differed in length, so total-cost differences include output variation. Evaluate input/cache cost separately and report total cost alongside it.
5. The 23% cache-rewrite share in the motivating long-session audit is an opportunity estimate from that session, not a guaranteed avoidable fraction for all users.
6. API-equivalent dollars do not establish an equal reduction in a flat subscription payment or its quota consumption. Those need separate measurement and wording.

## Findings in the implementation

Priorities: P1 = address before a shared hosted pilot; P2 = address before broader operation or using the results as a reliable product metric. These are code findings; no production incident is claimed.

### F1 — P1: refresh spending is incomplete

Location: `cache_keeper.py:241` (`tick`) and `:302` (`_resume_event`).

The stop condition is `write > max(read, 1)`, not “any write occurred.” A ping with 100,000 read tokens, 50,000 write tokens and 10,000 uncached tokens is accepted, but only 100,000 read tokens are accumulated. Write/uncached costs are omitted. The accepted-ping log also omits those counters. A failed/rebuilding ping can incur costs and then stop without those costs entering the resume calculation.

The default 5/9-ping limit is a count budget based on fixed multipliers, not an actual dollar cap. Network failures retry without increasing that count; a timeout can leave the outcome unknown after the provider has processed the request.

Recommendation: record each attempt and all reported usage before deciding whether to continue. Apply model-specific read/write/uncached rates. Give partial writes an explicit policy; initially stop after unexpected writes. Treat unobserved usage after a timeout as uncertain, not proven free. Use actual spend plus a bounded reserve for an in-flight attempt.

### F2 — P1: group identity does not include the authenticated tenant

Location: `cache_keeper.py:103` (`group_of`) and `:186` (`record_usage`).

A supplied liveness ID completely replaces credential identity and is truncated to 100 characters. Two different credentials with the same ID address the same group. Without an ID, every conversation using one provider credential shares a group. Tools-bearing subagent calls can replace the main conversation's target.

This can mix session state and cost attribution or warm the wrong conversation. It is not by itself evidence of response-content disclosure, since pings use the stored request/header pair and do not return generated output to another user.

Recommendation: bind state to authenticated ContextShrink user/key identity, provider endpoint, model, and an explicit conversation/launch identity. Keep credentials out of identifiers and logs. Scope stop operations to the same authenticated owner. A group ID must not be accepted as authorization.

### F3 — P1: ended sessions can be revived by pending completions

Location: `cache_keeper.py:180`, `:190`, `:218`.

`record_request()` checks `_ended`, but `record_usage()` does not. Sequence: record request, end session, receive its usage. The completion recreates the group and it becomes eligible for future pings. `end()` also leaves pending entries behind.

Recommendation: cancel/remove pending entries on end, and check lifecycle generation/state again on every completion. A closed generation must never become active again.

### F4 — P1: mutable state races with requests and pings

Location: `cache_keeper.py:186` and `:241`.

An older request completing after a newer one replaces the newer body/timestamp. During an awaited ping, a real request can replace the same group and reset its counters. When the old ping returns, it then updates the new group's counters and timestamp. The local simulation reproduced `last_request_at=3400` with `last_touch_at=3300` and one old ping attributed after the new request.

Recommendation: snapshot immutable target generations; record ping expenditure against the generation sent. Apply completion state changes only when the generation is still current. Track real requests in flight and explicitly handle out-of-order completion, exit, and cancellation. Group state needs coordination even in a single asyncio event loop because awaits allow interleaving.

### F5 — P1: the hosted lifecycle path is incomplete

Locations: `server.py:5697`, `account_analytics.py:275`, `deploy/pi5-webtier/Caddyfile:15`, `wrap.py:5639`.

The gateway does not allow the end endpoint. Account middleware rejects the unmatched path, and the endpoint additionally requires loopback. The wrapper's notification has no ContextShrink account authentication. Merely exposing the route would not complete the flow.

The wrapper calls end after normal `subprocess.run()` return, rather than in the existing `finally` block. Exceptions/interruption can skip it. Hard process termination also needs a lease/heartbeat expiry, since a finalizer cannot be relied on in that case.

Recommendation: an authenticated, tenant-scoped lifecycle API plus a short renewable lease and explicit opt-in/spend settings. Decide whether closing a tool means stop immediately or protect for a user-selected grace period. Preserve route restrictions until ownership checks are implemented.

### F6 — P2: mixed TTLs and expired deadlines are mishandled

Location: `cache_keeper.py:59` (`ttl_of`) and `:229` (`due`).

Any nested one-hour marker makes the whole group one-hour. With one-hour tools and five-minute messages, the first refresh is scheduled after the message cache has expired. Top-level automatic `cache_control` is not inspected. No marker is also treated as a five-minute cache rather than unverified cache eligibility.

`due()` has no upper freshness bound. A scheduler resuming after a deadline can send a ping to an already expired cache and pay to rebuild it. A 200 response with zero read/write counters is treated as a successful refresh too.

Recommendation: track protected cache regions and observed coverage, handle automatic caching explicitly, and skip refreshes after the eligibility window is missed unless deliberate cache rebuilding is a separate approved behavior. Require meaningful cache usage before considering a ping successful.

### F7 — P2: abandoned-session costs and causal uncertainty are absent from the net metric

Location: `cache_keeper.py:196`, `:211`, `:302`.

Net savings are emitted only for a qualifying resume. A session that never returns has expenditure but no negative net outcome. Pings can also be reset on the next request without a qualifying resume event. Summing `cache_keeper_resume.net_usd` therefore cannot measure the feature's portfolio-wide return.

A warm resume does not prove our ping caused the hit: another request can keep a shared prefix alive. Subtracting the first warm prefix/tool-size estimate helps, but does not reconstruct the control outcome. Group replacement by a different conversation/model makes attribution weaker still.

Recommendation: maintain an append-only cost ledger for every attempt and a separately labeled estimate of avoided cost. Include terminal outcomes for abandonment, expiry and end. Use a controlled holdout to estimate aggregate benefit. Do not conflate a measured cache read with a measured causal dollar saving.

### F8 — P2: scheduling and memory limits need operational bounds

Location: `cache_keeper.py:154`, `:158`, `:229`, `:241`; `server.py:1999`, `:3184`.

- Each ping can await the HTTP client for 120 seconds. Due groups are processed sequentially, so one slow upstream can consume another group's 45-second margin.
- Every worker starts its own keeper with no shared ownership/lease. A deployment with multiple workers can retain stale copies and duplicate refreshes across workers.
- Idle/budget/stopped groups are skipped, but their bodies and credential headers are not deleted at those boundaries. They remain until eviction, explicit end or shutdown. `_ended` grows without a bound. Group count limits do not bound memory bytes for large prompts.

Recommendation: bounded concurrency with deadline-aware scheduling, a per-session owner lease, byte limits, expired-state cleanup and bounded tombstones. No payload or credential persistence is needed for this feature.

### F9 — P2: the economic assumptions need model-specific validation

Location: `cache_keeper.py:53`, `:142`, `:226`, `:337`.

The scheduler uses a fixed 0.1 read multiplier and a 0.5 return probability. Price resolution uses `long_context=False` and silently falls back to a generic $3/M base when resolution fails. These may be reasonable prototype assumptions but cannot support a precise model-independent savings claim or dollar cap.

Recommendation: store the actual model, pricing basis and usage classes on each attempt. Skip dollar-based decisions when the price is unknown, or clearly use an explicit conservative user-approved budget policy. Calibrate return probability from completed and abandoned sessions rather than assuming it is universal.

## Validation performed for this review

Existing suite: `python -m pytest tests/test_proxy/test_cache_keeper.py -q -p no:cacheprovider` — **22 passed**, Python 3.12, 6.28 seconds. Offline metadata settings were used. The `.venv` interpreter worked in this elevated execution environment.

Additional isolated simulations loaded only `cache_keeper.py` with Python's standard library. They used synthetic credentials, a fake clock, a fake sender and fixed fake pricing. No provider inference requests were made. The observed output was:

```json
{
  "mixed_ttl_seconds": 3600,
  "same_liveness_id_different_credentials_collide": true,
  "partial_write_ping": {
    "stopped": false,
    "pings": 1,
    "recorded_read_tokens": 100000,
    "write_cost_accumulator_exists": false
  },
  "pending_completion_resurrects_ended_group": true,
  "out_of_order_completion_last_request_at": 0,
  "already_expired_group_still_due": true,
  "idle_expiry_retains_body_and_headers": true,
  "inflight_ping_races_new_request": {
    "last_request_at": 3400,
    "last_touch_at": 3300,
    "pings_attributed_after_new_request": 1
  }
}
```

Reproduction recipes, suitable for regression tests:

1. Body: one-hour marker in tools, five-minute marker in messages; call `ttl_of`.
2. Call `group_of` with two different `x-api-key` values and the same liveness ID.
3. Adopt a 150k-token target at t=0; at t=3300 return a fake ping with read=100k, write=50k, uncached=10k; inspect the group.
4. `record_request('r')`, `end('L')`, then `record_usage('r', ...)`; inspect `_groups` and `due()`.
5. Start old at t=0, new at t=100; complete new then old; inspect the adopted timestamp/body.
6. Adopt a one-hour target at t=0, jump to t=4000, call `due()`.
7. Set idle cap=10 seconds; adopt at t=0, call `due()` at t=20; inspect retained body/headers.
8. Block a fake ping started at t=3300 on an asyncio Event; complete a real request at t=3400; release the ping; inspect timestamps/counters.

These simulations establish deterministic code behavior, not real provider cache performance. The existing suite's end test starts another request after end; it does not cover a request already pending when end occurs.

## Can this work with Codex?

### Documented capabilities

OpenAI's current prompt-caching guide documents zero-output prewarming for GPT-5.6 and later through `prompt_cache_options.prewarm=true`. These models use a minimum cache lifetime of 30 minutes after write/reuse. Earlier supported models have `in_memory`/`24h` retention policies; “24h” is not a guaranteed 24-hour hit. Prewarming may incur write cost on a miss. [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)

The Responses request reference exposes `prompt_cache_options` with `prewarm` and `ttl`; returned usage includes separate `cached_tokens` and `cache_write_tokens`. Capability must be selected by the actual endpoint and model. [Responses reference](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)

WebSocket `response.create` also supports `generate:false` for preparing request state and returns a response ID. That documentation does not establish equivalence to a charged prompt-KV refresh or prove cache-retention extension. Connection-local response state and prompt caching are distinct. Connections have a 60-minute limit; response lineage and reconnect handling matter. [WebSocket mode](https://developers.openai.com/api/docs/guides/websocket-mode)

The documented Sign in with ChatGPT preview has different constraints, including required HTTP `store:false`/`stream:true` and unsupported `prompt_cache_retention`. It is evidence that authentication routes differ; it does not certify prewarm support on the exact Codex backend used here. [ChatGPT plan-usage limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)

### Feasibility by path

| Path | Assessment | Next step |
|---|---|---|
| Codex using OpenAI API credentials and a model with documented prewarm | Feasible design; not yet implemented or live-tested here | Build a capability-gated provider adapter after shared keeper fixes |
| Earlier model with retention options but no documented zero-output prewarm | Optimize supported retention and stable prefixes first | Do not emulate refresh with arbitrary generated turns |
| Codex through ChatGPT subscription authentication | Unverified | Probe the exact endpoint/model with explicit experiment scope; verify usage and client behavior |
| WebSocket `generate:false` by itself | Request-state warmup is documented; KV refresh economics unresolved | Treat as a separate experiment, not an assumed cache keeper |

The answer is therefore **yes for supported API configurations; conditional for the user's actual Codex subscription path**. It is not a drop-in copy of the Anthropic implementation.

### Repository-specific work required

1. There is currently no OpenAI keeper hook/adapter. The keeper is fed by the Anthropic handler and `outcome.provider == 'anthropic'` branch only.
2. Codex WebSocket turns often carry incremental input plus `previous_response_id`. Replaying the last frame alone does not necessarily reproduce the context. Resolve the exact previously forwarded history or use a documented continuation/prewarm form on a compatible connection. Preserve tools, instructions, reasoning settings, cache key and endpoint identity.
3. Keeper responses must not advance the user's conversation, leak into the client stream, run tools, or alter the saved compression lineage. Current WS accounting treats completed responses as ordinary turns around `openai.py:8790`; auxiliary refreshes need explicit classification and event ownership.
4. `_extract_responses_usage` (`openai.py:1745`) reads `cached_tokens` and infers all remaining input as writes/uncached. It does not consume explicit `cache_write_tokens`. Before evaluating newer-model refresh costs, parse actual usage categories and preserve the distinction between measured writes and legacy inferred counters. The historical “OpenAI has no write premium” assumption must not be blindly applied to every new model.
5. Retention settings must be capability-specific. Do not inject one retention field into all API and subscription requests. Unsupported combinations should disable the feature cleanly.
6. Prefer a shared controller with provider adapters for request construction, cache-region identity, lifetime, usage normalization and pricing. Keep model/endpoint capability data separate from scheduling policy.

## Proposed implementation approach

### Phase 1: make the Anthropic controller reliable

Address F1-F6 first: owned sessions, lifecycle generations, completed/in-flight request coordination, exact attempt cost recording, explicit cache eligibility and deadline handling. Add byte limits, cleanup and worker ownership before a hosted pilot. Preserve the default-off switches.

Use an account-scoped setting with a short protection period and explicit spend cap. A lease should represent user intent to protect the session. Define closing the tool, a crash, sleep, network loss and a normal pause as separate lifecycle events.

### Phase 2: measure expected value

For each protected prefix and proposed refresh, estimate:

```text
expected benefit = P(return before protection ends)
                   * incremental resume cost avoided by retaining this prefix
expected net = expected benefit - expected total refresh expenditure
```

Use the portion that would otherwise expire, excluding independently warm shared context. A refresh can be useful only if it changes the eventual resume outcome. Include all read, write and uncached input charges, and any provider-reported output/other charges. Losses from abandoned sessions remain in the aggregate.

Do not credit the same avoided rewrite on every ping. Attribute it once to a qualifying resume, with a stated counterfactual and uncertainty. Never add a full uncompressed cache benefit on top of a compression benefit if the same tokens have already been removed; evaluate the keeper on the actually forwarded prefix.

### Phase 3: an OpenAI API experiment

First verify zero-output prewarm on one explicitly supported API model with synthetic context and a small predetermined budget. Record the full usage breakdown. Compare no-refresh and refresh groups across pauses below and above the model's documented lifetime. Isolate reusable prefixes between arms to avoid a treatment refreshing its own control.

Measure cache reads/writes, total input cost, time to first token on resume, no-output/no-tool behavior, and the full cost of sessions that never resume. Repeat enough cases to quantify variation. The previous agent-path A/B problem is reduced by freezing the same prompt and continuation; do not use divergent coding trajectories to infer a small cache effect.

Only after the API experiment succeeds should a separate experiment assess the actual ChatGPT-authenticated Codex route. API success is not evidence of subscription endpoint compatibility or proportional quota savings.

### Phase 4: opt-in hosted pilot

Exercise simultaneous sessions, same-account subagents, duplicate/untrusted IDs, mixed TTLs, 429s, timeouts, revoked access, slow upstreams, client disconnects, restarts, multiple workers and unknown prices. Compare the complete portfolio cost with a holdout before choosing default settings.

The UI could show protection status, refresh spend and estimated avoided rebuild cost. The refresh spend is measured; the avoided cost is an estimate. A user should be able to stop protection immediately.

## Questions for Claude's follow-up review

- Can each reproduced case be covered by a focused regression test and fixed without changing normal user requests?
- Should protection survive normal tool exit for a short grace period, and how will that intent reach Pi 5 securely?
- Can the scheduler identify the protected conversation reliably when subagents share a launch ID?
- Which precise Codex model, authentication mode and endpoint should the first compatibility experiment target?
- Does that endpoint report actual write usage and preserve the same prefix through prewarming?
- How will negative outcomes and uncertain provider charges appear in aggregate metrics?
- Can the live experiment be repeated against the final accounting code with properly isolated controls?

## Review boundary

This review created this document, ran the existing 22-test keeper suite and synthetic local checks, and read existing artifacts plus official documentation. No new paid provider run, source-code fix, deployment, billing update, Git commit or push was performed. References describe documentation retrieved during this review and should be rechecked when implementing an adapter.
