# Changelog

All notable changes to Horizon are documented here.

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
