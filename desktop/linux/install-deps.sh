#!/usr/bin/env bash
# Install what the Linux desktop build needs on Ubuntu 22.04: Tauri's system
# libraries, Node.js 22, rustup and uv (which supplies Python 3.12).
# Used by the build container (Containerfile) and, later, GitHub Actions.
set -euo pipefail

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"
export DEBIAN_FRONTEND=noninteractive

$SUDO apt-get update -qq
$SUDO apt-get install -y -qq --no-install-recommends \
    build-essential pkg-config cmake curl wget file ca-certificates xz-utils git \
    libwebkit2gtk-4.1-dev libxdo-dev libssl-dev libayatana-appindicator3-dev \
    librsvg2-dev patchelf rpm fuse libfuse2 >/dev/null

# Node.js 22 LTS from nodejs.org, checksum-verified.
if ! command -v node >/dev/null; then
    base=https://nodejs.org/dist/latest-v22.x
    tmp=$(mktemp -d)
    curl -fsSL "$base/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt"
    file=$(grep -oE 'node-v[0-9.]+-linux-x64\.tar\.xz' "$tmp/SHASUMS256.txt" | head -1)
    curl -fsSL "$base/$file" -o "$tmp/$file"
    (cd "$tmp" && grep " $file\$" SHASUMS256.txt | sha256sum -c - >/dev/null)
    $SUDO tar -xJf "$tmp/$file" -C /usr/local --strip-components=1
    rm -rf "$tmp"
fi

# rustup; the repo's rust-toolchain.toml picks the exact compiler at build time.
if ! command -v rustup >/dev/null && [ ! -x "$HOME/.cargo/bin/rustup" ]; then
    curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain none
fi

# uv (pinned like the proxy Dockerfile), then Python 3.12 through it.
if ! command -v uv >/dev/null; then
    curl -fsSL https://astral.sh/uv/0.11.18/install.sh | $SUDO env UV_INSTALL_DIR=/usr/local/bin sh
fi
uv python install 3.12

echo "node $(node --version), uv $(uv --version | cut -d' ' -f2)"
