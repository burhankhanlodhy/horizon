#!/usr/bin/env bash
# Build the ContextShrink desktop app for macOS on Apple Silicon: a .dmg.
#
# Run on an arm64 Mac (or a GitHub macos runner) with Rust (rustup), Node.js
# and uv installed. Outputs land in desktop/dist-macos/.
# The app is signed ad hoc, which Apple Silicon requires to run at all; it is
# not notarized, so macOS asks the user to approve it on first launch.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
CACHE_DIR=${CACHE_DIR:-$ROOT/.cache/macos-build}
OUT="$ROOT/desktop/dist-macos"
VENV="$CACHE_DIR/venv"
mkdir -p "$CACHE_DIR" "$OUT"

[ "$(uname -s)-$(uname -m)" = "Darwin-arm64" ] || { echo "Run on an Apple Silicon Mac." >&2; exit 1; }

echo "== Frozen Horizon client"
[ -x "$VENV/bin/python" ] || uv venv -q --python 3.12 "$VENV"
# Editable, like the other platforms: maturin puts the compiled horizon._core
# next to the source PyInstaller collects from. The heavy ML modules are
# installed but excluded from the frozen client (compression runs on the proxy).
uv pip install -q --python "$VENV/bin/python" -e "$ROOT[proxy,vault]" "pyinstaller>=6.10"
excludes=(torch transformers onnxruntime magika litellm tokenizers tiktoken scipy sklearn
          fastembed hnswlib sentence_transformers PIL matplotlib pandas tkinter IPython pytest)
client="$ROOT/desktop/client"
(
    cd "$ROOT"
    "$VENV/bin/python" -m PyInstaller "$client/horizon_client.py" \
        --name horizon \
        --distpath "$client/dist" --workpath "$client/build" --specpath "$client" \
        --noconfirm --clean --console --log-level WARN \
        --target-arch arm64 \
        --paths "$ROOT" \
        --collect-submodules horizon.cli \
        --collect-data horizon \
        --collect-submodules keyring \
        --copy-metadata keyring \
        --hidden-import keyring.backends.macOS \
        "${excludes[@]/#/--exclude-module=}"
)
"$client/dist/horizon/horizon" --version

echo "== Tauri app"
bundle="$ROOT/desktop/app/src-tauri/target/release/bundle"
rm -rf "$bundle"  # the bundler keeps earlier versions' packages
(
    cd "$ROOT/desktop/app"
    npm ci --no-audit --no-fund --loglevel=error
    npx tauri build --bundles app,dmg
)

rm -f "$OUT"/*
cp "$bundle"/dmg/*.dmg "$OUT/"
codesign --verify --deep --strict "$bundle/macos/ContextShrink.app" && echo "signature: ok (ad hoc)"
ls -l "$OUT"
