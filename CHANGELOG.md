# Changelog

All notable changes to Horizon are documented here.

## [Unreleased]

## [0.42.0] — Keep-alive for every client, new integrations, Kompress fixes

Desktop app 0.5.1 and 0.5.2 bundle this client.

### Fixed
- OpenAI cache writes are read from the usage report. GPT-5.6 and later report
  `cache_write_tokens` and bill them at 1.25x input; Horizon inferred writes
  from the uncached tokens and priced them as plain input, so the cost of a
  cache write (and of a missed cache) read 25% low on those models. Earlier
  models, which report no writes, keep the inferred split.
- Flash credit is measured in the provider's own tokens and priced at the
  model's measured cache behaviour. Tokens: the request-body bytes a stub
  removes, times the provider's billed tokens per byte for the model, learned
  from requests flash did not touch (local tokenizers counted about half of
  what Gemini, GPT and Claude bill; replayed against a live Gemini session the
  estimate was within 1-3%). Price: the share of a conversation's repeated
  prefix the model's upstream serves from cache, from later turns no stub
  edited, mixing the cache-read rate with the input (or cache-write) rate.
  Until a model has that evidence the cache-read rate applies.
- The account ledger now credits Flash Observations, the fast-mode governor,
  the OpenAI Flex tier and model modernization. Their savings were missing
  from Est. savings (in cache mode, Claude flash recorded no savings at all).
  Each is priced from the request's own usage, added to `savings_usd` and
  listed per feature in `policy_usd`. A Flex request's cost is now its Flex
  price.
- Chat Completions no longer counts flashed outputs as compression at the
  fresh-input price; they are credited as flash, priced as cache reads.
- Chat Completions booked every earlier turn's compression again on every
  turn, at the fresh-input price (five turns: 134,745 tokens booked for
  44,915 removed). Its running total is now keyed per conversation, so each
  removal is booked once and its repeats as retained savings, priced as the
  cache reads they replace.
- Claude Messages had the same over-count in both proxy modes: cache mode
  replays the compressed prefix and token mode recompresses it, so every turn
  re-booked earlier removals (five turns: 44,915 tokens booked for 8,983
  removed in cache mode). Keyed the same way; the key ignores
  `cache_control`, which Claude Code moves every turn.
- Gemini `generateContent` (and the Code Assist stream) and Bedrock
  `InvokeModel` re-booked earlier removals every turn the same way; keyed
  likewise. Gemini `countTokens` booked its compression as savings although
  nothing is billed for a count; it now books none. A rejected Bedrock call
  was recorded as a success with savings; it now carries the upstream status.
- Flash credit is priced at the cache-read rate. Priced from the flashed
  request's own cache usage, it could exceed the measured saving severalfold
  on OpenAI-format upstreams, where a stub can break the cache. On an upstream
  that evidently does not cache, it prices at the input rate instead. Only
  later turns of conversations no stub edited count as that evidence: in a
  live native-Gemini run the flashed session's own misses (caused by its
  stubs) made a caching upstream look non-caching and overcredited flash.
- The account ledger's request cost no longer bills OpenAI's inferred cache
  writes on top of the same uncached tokens (it nearly doubled `cost_usd` on
  cache misses).
- Pricing resolves gateway spellings: bare `grok-*` names (xAI's catalog key
  is `xai/grok-*`), vendor-prefixed names such as `x-ai/grok-4.6` or
  `google/gemini-3.1-pro`, and GA names the catalog lists only as `-preview`
  (`gemini-3.1-pro`). Before, those models were unpriced: no savings credited
  and a $3/M fallback cost.

### Added
- Cache keep-alive for OpenAI GPT-5.6 and later (`HORIZON_CACHE_KEEPALIVE=1`).
  Their cache lives 30 minutes after its last use and a rewrite costs 1.25x
  input; an idle session is pinged two minutes before expiry. Responses: the
  exact last request with `prompt_cache_options.prewarm` (reads only, no
  output; also stored `previous_response_id` chains). Chat Completions, which
  rejects `prewarm`: the same request with a 16-token output limit. Measured
  on gpt-6.1-sol: warmed sessions still hit at 52 minutes, an untouched one
  was rewritten. Ping rows carry their provider.
- Codex WebSocket sessions are kept warm on their own connection. Their
  `store: false` chain lives only on the open upstream connection, so the
  pre-warm is sent there (chained to the last response, empty input,
  `prewarm`), its events never reach Codex, and Codex's next turn is chained to
  the pre-warm's response. Measured over 33 idle minutes on gpt-6.1-sol: one
  pre-warm ($0.0011) kept the cache, and the resumed turn read it all. Not for
  ChatGPT sign-in, or when proxy memory tools run on the connection.
- `wrap opencode` and `wrap codex` send a per-launch keep-alive id and end the
  session on exit, so a hosted proxy keeps their sessions warm only while the
  tool runs. OpenCode sends it as a provider header and through its transport
  plugin; Codex, whose built-in provider cannot add headers, in its base URL
  path (`/p/<project>/k/<id>/v1`, read by the proxy as the header).
- `horizon wrap bob` for the IBM Bob CLI (`BOB_GATEWAY_URL`; Bob keeps its own
  credential and settings). Bob's chat is compressed on a real
  `/inference/v1/chat/completions` route; its other gateway paths go to IBM's
  origin unchanged; `region_domain` is removed from its profile so Bob keeps
  using the proxy. Token mode by default; the wrap refuses to launch when Bob's
  saved gateway would bypass the proxy. Every wrap now warns when it reuses a
  proxy running a different mode, and `/health` reports the mode.
- `horizon wrap antigravity` prints the custom OpenAI-compatible provider
  settings for Google's Antigravity IDE, and `horizon mcp install --agent
  antigravity` registers Horizon in its `mcp_config.json`.
- `horizon learn` can analyse with `agy` (Antigravity CLI) when
  `HORIZON_LEARN_ALLOW_UNSAFE_AGY=1` is set (agy has no tool-free mode).
- Context guard (`HORIZON_CONTEXT_GUARD=0` turns it off): a streamed Claude
  request near the model's real window reports usage at 95% of the window the
  client believes in, so Claude Code compacts before a `prompt is too long`
  error instead of compacting every turn. The real limit is learned from the
  first such error, and a `context-1m` session is no longer compressed as if it
  were five times over budget.
- Cache routing for Grok (`HORIZON_CACHE_ROUTING=1`, set by
  `HORIZON_SAVINGS=auto`): when a client sends no conversation id, Horizon adds
  a stable per-conversation, per-account `x-grok-conv-id` (Chat Completions) or
  `prompt_cache_key` (Responses), so xAI routes each turn to the server that
  holds its prompt cache. A client's own id always wins; content is unchanged.
- Price-cliff guard on the OpenAI-format paths: Chat Completions (any model
  served there: GPT, Gemini, Grok, ...) and Responses (HTTP and WebSocket).
  Near a whole-request price tier the catalog lists (GPT-5.4 / 6.x at 272k,
  Gemini 3.1 Pro at 200k) the request compresses harder, as on Claude. A
  Responses request chained with `previous_response_id` carries only its
  increment and is not guarded.
- Flash Observations for the native Gemini API (`HORIZON_FLASH_GEMINI=1`, set
  by `HORIZON_SAVINGS=auto`): `generateContent` and `streamGenerateContent`,
  including the plain streaming route Gemini CLI uses. Next-turn stubbing of
  large `functionResponse` outputs of shell, search and listing tools, with
  the same safety nets and ledger credit as the OpenAI form.
- Usage and Advanced Analytics show the savings beyond compression per
  feature.

### Fixed (Kompress)
- Kompress keeps each word on its source line and keeps runs of fixed-width
  table rows whole. It used to join words from unrelated lines (a `git log
  --stat` window became one line mixing commits) and crush `ls -l` rows
  unevenly. Measured on 125 public tool outputs: fact pairs intact 3,065 ->
  3,486 of 3,486; savings 14.5% -> 11.4% of offered tokens, most of it four
  READMEs whose badge HTML is no longer glued and elided
  (`experiments/kompress-line-integrity`).
- ML compression is bounded per request, not only per block: a tool result of
  many prose blocks could spend over a minute in Kompress. Blocks over the
  budget pass through unchanged (cache-safe).
- The Kompress tokenizer and encoder load at the snapshot the weights were
  exported against, not a floating ref.

## [0.41.0] — Savings profile and Flash safety nets

`HORIZON_SAVINGS` defaults to `off`, so nothing changes until it is set.

### Added
- `HORIZON_SAVINGS=off|auto|max`: one switch that fills in the cost features'
  defaults. `auto` enables what was measured to save money (Claude and
  OpenAI-format Flash Observations, Flex and fast-mode governors for headless
  traffic, the price-cliff guard and the cache-miss watch); `max` also enables
  model modernization. Explicit variables still win. The active profile is
  shown in `/stats`.
- Flash safety nets:
  - a flashed request the upstream rejects (400) is retried once unflashed,
    and flash is switched off for that host and model;
  - flash pauses in a conversation once the model re-fetches a stubbed output
    and gets the same data back.
- `/stats` → `flash`: outputs stubbed, tokens kept out, re-need pauses,
  automatic switch-offs and price-check skips, each with a reason.
- `HORIZON_FLASH_OPENAI_UPSTREAMS=*` allows any host.

### Changed
- `docker-compose.yml` leaves the cost-feature variables empty, so
  `HORIZON_SAVINGS` decides; an explicit value in `.env` still overrides it.

## [0.40.0] — Cost policies and Flash Observations

All new features are off by default. Evidence, run instructions and failure
analyses: `experiments/flash-observations/README.md`; design and cost model:
`wiki/plans/2026-10-08-implementation-guide.md`.

### Added
- **Flash Observations for Claude** (`HORIZON_FLASH_OBSERVATIONS=1`, official
  API only). A large output of an allow-listed local tool is shown for one
  turn through a turn-scoped system message (`clear_at`), then kept as a stub.
  Measured live on Opus 5.5: 22% cheaper with no loss of accuracy.
- **Flash Observations for OpenAI-format endpoints** (`HORIZON_FLASH_OPENAI=1`).
  Covers Responses over HTTP and WebSocket and Chat Completions. The newest
  output is shown in full, then stubbed from the next turn on. Only
  `api.openai.com` and `chatgpt.com` by default;
  `HORIZON_FLASH_OPENAI_UPSTREAMS` adds hosts that passed the preflight.
  Measured: GPT-6.1 Sol through ModelFlare 53% cheaper, Gemini 3.1 Pro
  client-side 80% cheaper.
- **Per-model check** (`HORIZON_FLASH_MIN_READ_RATIO`, default 0.04): the
  OpenAI path skips models whose cache reads are nearly free, such as
  DeepSeek, where stubbing saved nothing.
- `HORIZON_MODEL_MODERNIZE`: serve superseded model ids on their cheaper
  successor.
- `HORIZON_FAST_MODE_POLICY`: fast-mode governor.
- `HORIZON_OPENAI_FLEX_POLICY`: Flex tier for headless traffic.
- `HORIZON_PRICE_CLIFF_GUARD`: compress harder just below a whole-request
  price tier.
- `HORIZON_CACHE_MISS_WATCH`: predicted cache misses priced in `/stats`.
- Preflight scripts, live test harnesses and strict fake APIs under
  `experiments/flash-observations/`.

### Changed
- Accounting reads each model's long-context threshold (Haiku 5.5 at 100k,
  GPT-6.x at 272k) and its published long-tier write rate.
- The cache keep-alive ping budget uses the model's own read and write rates.

### Fixed
- Effort-only mid-conversation system messages are no longer relocated to the
  top-level system prompt on per-message-effort models.
- Model-family matching is boundary-aware (`claude-sonnet-5` does not match
  `claude-sonnet-5-5`).
- The signed-thinking guard no longer treats re-sent turn-scoped system
  messages as moved thinking blocks.

## [0.39.1] — Horizon rebrand baseline

Forked from Headroom 0.39.1 and rebranded to Horizon.

### Changed
- Full rebrand: package `headroom` -> `horizon` (`horizon-ai` on the
  packaging level), crates `headroom-core`/`headroom-py` ->
  `horizon-core`/`horizon-py`, extension module `headroom._core` ->
  `horizon._core`, CLI command `headroom` -> `horizon`, environment
  variables `HEADROOM_*` -> `HORIZON_*`, state directory `~/.headroom` ->
  `~/.horizon`, wire-level names (`x-headroom-*` headers ->
  `x-horizon-*`, `headroom_retrieve` -> `horizon_retrieve`).

### Removed
- `plugins/`, `sdk/`, `docs/` site, `benchmarks/`, `e2e/`, `examples/`,
  `sbom/`, `REALIGNMENT/`, `.github/` CI, beacon upload telemetry and the
  update-check phone-home, the `headroom-proxy`/`headroom-parity`/
  `headroom-simulators` Rust crates, and the `toin_publish` CLI (its TOML
  consumer was the removed Rust proxy).
- Local TOIN learning, the local telemetry collector and toggles are kept.
