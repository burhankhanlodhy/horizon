# Codex savings review — 20 October 2026

## Live findings

Initial read-only inspection of the Pi proxy and account telemetry was followed
by deployment at the user's explicit request. The npx connector routes
requests and displays telemetry; compression and model pricing run on the proxy.

The proxy has LiteLLM 1.106.0 and a functioning GPT price catalog. Its
gpt-6.1-sol standard prices agree with the fetched official pricing page:
$2/M input, $0.10/M cached input, $10/M output. The catalog also carries
long-context and service-tier rates. These are API-equivalent estimates, not
measured reductions to a flat ChatGPT subscription charge. codex-auto-review
is not a priced catalog model; alias-specific dollar estimates need separate
verification rather than assuming it is a particular GPT model.

Source: https://developers.openai.com/api/docs/pricing

An account-level snapshot over the preceding 24 hours, across keys:

| Agent | Input tokens before compression | Tokens saved | Reduction |
| --- | ---: | ---: | ---: |
| claude-code | 94,051,844 | 965,424 | 1.03% |
| codex | 4,124,465 | 37,692 | 0.91% |

These are cumulative tokens across requests, including re-sent history, not
unique conversation tokens or a matched workload comparison. Claude processed
about 22.8 times as much input in this snapshot. Absolute saved-token totals
therefore exaggerate the difference between the two compression rates.

The active savings profile is already max, including OpenAI flash observations.
Normal Codex compression is visible in telemetry (schema compaction,
smart_crusher, mixed, code_aware). File-read protection and frozen prefixes are
intentional correctness and cache protections and remain enabled.

## Implemented locally

The current Codex desktop harness sends shell work inside
custom_tool_call(name="exec", input=<JavaScript>). Flash observations previously
recognized direct exec_command/shell calls but missed this wrapper.

The fix allows this wrapper to inherit exec_command's policy only when every
tools reference names exec_command. Mixed, computed, and other tool references
remain untouched. Existing text-only, size, horizon, rerun, host, model-price,
and reject-and-retry gates still apply. No tool call, call ID, reasoning item,
or earlier-than-horizon output is rewritten. Default policy does not enable
arbitrary exec output.

Validation: 45 targeted tests pass across transform, HTTP cache/token modes,
and WebSocket paths; Ruff and git diff --check pass. Offline replay at 40
post-migration tool-output boundaries from the user's current chat, keeping
pre-migration history frozen, reduced serialized input bytes from 136,167,767
to 132,894,207 (2.40% additional reduction). This is a local payload-size
measurement, not a token, quality, latency, or dollar-savings guarantee.

## Deployment

Deployed 2026-10-21 03:31:43 UTC (20 October, 10:31:43 PM America/Chicago) to
the Pi 5 horizon-proxy service. The installed module and checkout matched the
tested baseline before cutover. A new image layers only the changed module on
the existing running image; dependencies and pricing catalog remain pinned.
The checkout also contains the fix for subsequent normal builds.

- Module SHA-256: f85166f80f06b304e14dc6e480f19957d881a78a582920afb2f652335a771cff
- Running image: sha256:f7804eff458ce821b961782b603d15253fdaea61eff6d83a8bd44e0fbd6bdeed
- Rollback image: horizon-proxy:before-codex-exec-flash.srLXnJ
- Backup directory: /home/raspberrypi5/deployments/codex-exec-flash.srLXnJ

The image passed an isolated no-network smoke test before restart. After
restart, health and readyz returned 200, Docker health was healthy, profile
remained max, and unauthenticated public routes were rejected by the gateway
with 403. Seven subsequent requests under the user's Codex key all completed
with status 200. Those requests reported 27,707 total tokens saved across all
transforms; that total is not an attribution to this fix alone. No synthetic
model generation was made. A guarded rollback.sh is saved beside the original
module and image reference; it has not been executed.

## Follow-up measurements

A matched before/after coding comparison should record model/tier, forwarded
and cached tokens, compression
latency, cache misses, retry/re-read frequency, and successful edits/tests.
Frozen history imported before enablement must remain unchanged. No npx update
is needed for this proxy-side fix. A higher percentage alone is insufficient
if it increases cache misses, breaks edits, or forces the model to re-read data.
