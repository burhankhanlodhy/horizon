# Changelog

All notable changes to Horizon are documented here.

## [Unreleased]

### Fixed
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
