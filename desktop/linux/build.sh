#!/usr/bin/env bash
# Build the ContextShrink desktop app for Linux x86_64: .deb, .rpm and AppImage.
#
# Run from a checkout on Ubuntu 22.04 with install-deps.sh already applied (the
# build container, or a CI runner). Outputs land in desktop/dist-linux/.
#   CACHE_DIR  where the Python venv and Rust target dir live (default .cache)
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
CACHE_DIR=${CACHE_DIR:-$ROOT/.cache/linux-build}
OUT="$ROOT/desktop/dist-linux"
VENV="$CACHE_DIR/venv"
export CARGO_TARGET_DIR="$CACHE_DIR/target"
export PATH="$HOME/.cargo/bin:$PATH"
mkdir -p "$CACHE_DIR" "$OUT"

echo "== Frozen Horizon client"
[ -x "$VENV/bin/python" ] || uv venv -q --python 3.12 "$VENV"
# Editable, like the Windows .venv: maturin puts the compiled horizon._core next
# to the source PyInstaller collects from. The heavy ML modules are installed
# but excluded from the frozen client below (compression runs on the proxy).
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
        --paths "$ROOT" \
        --collect-submodules horizon.cli \
        --collect-data horizon \
        --collect-submodules keyring \
        --copy-metadata keyring \
        --hidden-import keyring.backends.SecretService \
        --collect-submodules secretstorage \
        --collect-submodules jeepney \
        "${excludes[@]/#/--exclude-module=}"
)
"$client/dist/horizon/horizon" --version

echo "== Tauri app"
bundle="$CARGO_TARGET_DIR/release/bundle"
rm -rf "$bundle"  # the bundler keeps earlier versions' packages
(
    cd "$ROOT/desktop/app"
    npm ci --no-audit --no-fund --loglevel=error
    # linuxdeploy (AppImage) is itself an AppImage: no FUSE in containers.
    # Its bundled strip cannot read newer ELF sections, so skip stripping.
    APPIMAGE_EXTRACT_AND_RUN=1 NO_STRIP=true npx tauri build
)

rm -f "$OUT"/*
cp "$bundle"/deb/*.deb "$bundle"/rpm/*.rpm "$bundle"/appimage/*.AppImage "$OUT/"
ls -l "$OUT"
