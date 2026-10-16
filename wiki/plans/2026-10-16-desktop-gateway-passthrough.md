# Desktop gateway, step 1: pure pass-through and exact metering

This is the first step of `2026-10-16-desktop-gateway-proposal.md`. The
`horizon proxy` runs on 127.0.0.1 as Claude Desktop's 3P gateway
(`inferenceProvider: gateway`, auth scheme `x-api-key` carrying the user's
Anthropic key). It forwards every request to api.anthropic.com unchanged and
records exact usage for each request and each chat.

## Findings from the research pass (2026-10-16)

There is no single pass-through switch, and `--no-optimize` is not enough.
These run regardless of it:

| Transform | Location | How to turn it off today |
|---|---|---|
| CCR tool injection | `handlers/anthropic.py` ~2757 | `--no-ccr` |
| Tool-search deferral (first-party upstream, 12 or more tools) | `anthropic.py` ~3397, `helpers.py` ~3955 | `HORIZON_TOOL_SEARCH=0` |
| Context guard (rewrites the client-visible `usage.input_tokens`) | `context_guard.py`, `streaming.py` ~1087, `anthropic.py` ~5450 | `HORIZON_CONTEXT_GUARD=0` |
| Message `cache_control` normalizer (unconditional) | `anthropic.py` ~2548, `cache/prefix_tracker.py` | `HORIZON_STABLE_BOUNDARY_BREAKPOINT=0`; there is no full off switch |
| TTL ordering guard | `helpers.py` ~519 and ~654 | `HORIZON_CACHE_CONTROL_TTL_GUARD=0` |
| Sticky `anthropic-beta` merge | `anthropic.py` ~1893 | `HORIZON_BETA_HEADER_STICKY=disabled` |
| Response cache (non-stream) | `server.py` ~1098 | `--no-cache` |
| Rate limiter (60 RPM) | `server.py` ~1107 | `--no-rate-limit` |
| Image optimizer (`ProxyConfig.image_optimize`, not wired to a flag) | `anthropic.py` ~1950 | off only because the mode is cache |
| Code-aware and read lifecycle | `cli/proxy.py` | `--no-code-aware`, `--no-read-lifecycle` |
| Subscription tracking | | `--no-subscription-tracking` |

With all of those off, a live probe against a fake upstream showed:

- `/v1/messages`, streaming and non-streaming, reached upstream byte-identical;
- `/v1/models` and `/v1/messages/count_tokens` were forwarded;
- `x-api-key`, `anthropic-version`, `anthropic-beta`, `user-agent` and
  `x-claude-code-session-id` were unchanged;
- SSE was relayed incrementally.

The byte-identity mechanism is `body_forwarding.select_outbound_body`: the
original bytes are sent unless a transform marks the body as mutated.

**Metering today** is split across three sinks, and none of them is complete:

- `savings_events.jsonl` has the 5m/1h cache-write split but no output
  tokens, request id or session, and it skips turns that are cache reads only.
- `--log-file` RequestLog has output tokens and `turn_id`, but no 5m/1h split,
  and it skips failed requests.
- The PERF log lines have neither.

The proxy also derives no client session for Anthropic requests:
`x-claude-code-session-id` is forwarded but never read.

**Prerequisite:** the native `horizon/_core.pyd` (git-ignored) must be present.
Copy it from a built checkout.

## W1: one switch (agent A)

Add `--passthrough` to `horizon proxy`, also reachable as the env var
`HORIZON_PASSTHROUGH=1`. It sets every row of the table to off, plus anything
else that rewrites the request body, headers or usage. In particular:

- the normalizer and the TTL guard are skipped entirely (not just neutralised);
- `image_optimize` is false;
- the compression pipeline, tool sort and tool schema compaction are off;
- memory and model routing or modernizing are off;
- output shaping and flash observations are off;
- keep-alive pings and the TTL upgrade are off.

Two exceptions are allowed:

- `sanitize_anthropic_model_id`, which strips a `[1m]` suffix and ANSI codes,
  is needed for correctness. Keep it, and record it as the reason when it
  changes the body.
- Header changes are limited to hop-by-hop headers and the proxy's own
  `x-horizon-*` headers.

Safety net: in pass-through, if any transform marks the body mutated (apart
from the model-id sanitize), forward the client's original bytes anyway and log
`passthrough_violation=<reasons>` once per request. The guarantee is "the bytes
the client sent, or the documented sanitize", never a silent rewrite.

The startup log prints one line, `passthrough: on (all transforms off)`.

Tests use a fake upstream (follow the patterns in `tests/gateway/`, for example
`fake_provider.py`):

- a Desktop-like request with streaming, tool use, `cache_control` on system,
  tools and messages, a 1h TTL entry, a thinking block, 15 or more tools, an
  image block and a large tool result arrives byte-identical upstream;
- non-streaming does too;
- `/v1/models` and `count_tokens` are forwarded;
- `usage` in the streamed and non-streamed responses reaches the client
  unchanged;
- with the context guard otherwise triggered (a large usage), the numbers are
  still unchanged;
- the violation fallback, using a forced mutation, sends the original bytes;
- defaults without `--passthrough` behave as before.

## W2: exact gateway metering (agent B)

Add one per-request sink, `gateway_usage.jsonl`, in the workspace
(`HORIZON_WORKSPACE_DIR` or `~/.horizon`). It is written for **every**
`/v1/messages` request, including errors and turns that are cache reads only,
in pass-through and in normal mode.

Each line holds:

- `v` (1), `ts`, `request_id`, `status`, `stream`, `model`
- `client` (the user-agent class), `session` (see below), `turn_id` (the
  existing content-derived id)
- `usage`: `input` (uncached), `cache_read`, `cache_write_5m`,
  `cache_write_1h`, `output`, all from the provider's real `usage` fields
- `cost_usd`, priced with `horizon/pricing` (Anthropic list prices, 5m and 1h
  write rates kept separate)
- `pricing_basis`
- `passthrough` (bool), and `transforms` (an empty list in pass-through)

`session` is the first of these that is present:

1. the `x-claude-code-session-id` request header;
2. a session id parsed from `body.metadata.user_id`. This is defensive: accept
   `..._session_<uuid>` and JSON forms, and never fail on them;
3. otherwise `null`.

Never log message content.

Report CLI: `horizon gateway report [--workspace DIR] [--since ISO]
[--session ID] [--json]`. It prints, per session:

- the first and last time;
- the request count;
- the token totals per class;
- the cache hit rate, cache_read / (input + cache_read + cache_write);
- the cost in USD;
- the cost split by class (input, cache reads, cache writes, output).

It ends with the grand totals. `--json` writes a machine-readable report.

Tests cover:

- a fake-upstream round trip writes one line per request, with exact fields
  for stream and non-stream;
- an error status is still recorded;
- a turn that is cache reads only is recorded;
- session from the header, from `metadata.user_id`, and missing;
- the pricing arithmetic, including the 5m and 1h rates;
- report grouping and totals;
- no content in the sink. Fuzz this with a sentinel string in the prompt, and
  assert it is absent from the file.

## W3: kit and live-check guide (after W1 and W2)

`desktop/gateway/`:

- **`Start Gateway.cmd`.** It starts the proxy with `--passthrough` on
  127.0.0.1:8787, with the dedicated workspace
  `%USERPROFILE%\ContextShrinkGateway`.
- **`Gateway Report.cmd`.**
- **A Desktop 3P configuration template.** It goes in
  `%LOCALAPPDATA%\Claude-3p\configLibrary\` (`_meta.json` plus `<id>.json`).
  Desktop's own in-app editor is also fine: Help → Troubleshooting → Enable
  Developer Mode, then Developer → Configure third-party inference. The
  template sets:
  - `inferenceProvider: gateway`
  - `inferenceGatewayBaseUrl: http://127.0.0.1:8787`
  - `inferenceGatewayAuthScheme: x-api-key`
  - `inferenceGatewayApiKey: <PASTE YOUR ANTHROPIC API KEY>` (the user fills
    this in)
  - `chatTabEnabled: true`
  - full model IDs in `inferenceModels`
- **`GUIDE.md`.** It covers:
  - what 3P mode changes: billing goes to the API key, chats stay on the
    device, and there is no claude.ai web or mobile;
  - that switching modes affects the claude.ai sign-in on that machine, so use
    a test machine or profile;
  - how to verify caching: the report's cache hit rate should be high after
    the first turn;
  - what to send back.

The user enters the API key. The agent never handles a real key.
