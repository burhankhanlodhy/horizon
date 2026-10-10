# Horizon

**The context-optimization proxy for LLM agents.** Horizon compresses tool
outputs, aligns provider cache prefixes, and manages context windows —
cutting LLM token costs by 50-90% without losing accuracy.

Horizon is a fork-lineage of the Headroom codebase, rebranded and slimmed to
a single deployment target: **a company VPS running the Python proxy**, with
employees connecting through per-user API keys.

## Architecture

- `horizon/` — the Python package: proxy server (FastAPI), compression
  pipeline, CLI, dashboard, CCR (compressed-context retrieval), memory.
- `crates/horizon-core` — the Rust compression engine (SmartCrusher, code /
  log / diff / search compressors, tokenizer).
- `crates/horizon-py` — PyO3 bindings exposing the engine as `horizon._core`.
  Hard dependency: there is no pure-Python fallback for compression.

## Install (from source)

```bash
pip install -e ".[proxy]"
```

Building the Rust extension requires a Rust toolchain (the pinned version in
`rust-toolchain.toml` is installed automatically by rustup) and the MSVC
Build Tools on Windows.

## Use the hosted ContextShrink service from the CLI

```bash
pip install "contextshrink[hosted]"
horizon login              # email and password; creates a key for this device
horizon wrap claude        # any tool: runs through the hosted proxy
horizon account            # plan, savings this cycle, fee estimate
horizon logout             # revokes this device's key
```

Usage and savings appear on the same dashboard as the desktop app's.
`horizon wrap --local <tool>` uses a local proxy instead.

## Run the proxy

```bash
horizon proxy --host 127.0.0.1
```

The proxy requires a bearer token in network mode:

```bash
HORIZON_PROXY_TOKEN=$(openssl rand -hex 32) horizon proxy --host 0.0.0.0
```

## VPS deployment

```bash
cp .env.example .env    # set HORIZON_PROXY_TOKEN
docker compose up -d
```

The compose file publishes the proxy on **host loopback only** — put a TLS
reverse proxy in front for network access (see `deploy/vps/`, added by the
Phase B workstream).

## Wrap Claude Code

Point Claude Code through a local Horizon proxy:

```bash
horizon wrap claude --no-proxy --port 18787
```

Undo it when done (do NOT stop the remote proxy when unwrapping against a
tunnelled VPS):

```bash
horizon unwrap claude --no-stop-proxy
```

## CLI

```
horizon --help          # command tree
horizon proxy --help    # proxy server options
horizon wrap --help     # agent wrappers (claude, codex, gemini, ...)
horizon keys --help     # per-user API keys (Phase B)
```

## Verification

```bash
make ci-precheck        # cargo fmt/clippy/test + compressor test subset
pytest tests/test_transforms tests/test_ccr.py -q
```

Baseline numbers and the one pre-existing environment-dependent test failure
are recorded in `BASELINE.md`.

## License

Apache-2.0 (see `LICENSE`, `NOTICE`).
