#!/usr/bin/env bash
# Build the Linux packages inside an Ubuntu 22.04 container (podman or docker).
# Run from WSL or any Linux machine:  bash desktop/linux/build-in-container.sh
#
# The source is copied into a cache on the Linux filesystem (fast, and keeps
# the Windows checkout untouched); caches survive between runs. Packages are
# copied back to desktop/dist-linux/ in the checkout.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/../.." && pwd)
CACHE=${CS_BUILD_CACHE:-$HOME/.cache/contextshrink-linux}
ENGINE=$(command -v podman || command -v docker || true)
[ -n "$ENGINE" ] || { echo "Install podman or docker first." >&2; exit 1; }
IMAGE=contextshrink-linux-build:22.04

# Rebuild the image when the dependency script changes.
tag=$(sha256sum "$REPO/desktop/linux/install-deps.sh" "$REPO/desktop/linux/Containerfile" | sha256sum | cut -c1-12)
if [ "$("$ENGINE" image inspect -f '{{index .Config.Labels "deps"}}' "$IMAGE" 2>/dev/null)" != "$tag" ]; then
    "$ENGINE" build -q --label "deps=$tag" -t "$IMAGE" "$REPO/desktop/linux"
fi

# Copy the files git would see (tracked + untracked, not ignored), or the list
# in CS_FILE_LIST when the caller made it (e.g. with Windows git, which is
# faster on a Windows checkout).
mkdir -p "$CACHE"/{cargo-registry,rustup,npm,uv}
rm -rf "$CACHE/src" && mkdir -p "$CACHE/src"
list=${CS_FILE_LIST:-}
if [ -z "$list" ]; then
    list=$(mktemp)
    git -c safe.directory="$REPO" -C "$REPO" ls-files -co --exclude-standard > "$list"
fi
tar -C "$REPO" -cf - -T "$list" | tar -C "$CACHE/src" -xf -
# A Windows checkout may have CRLF line endings; shell scripts need LF.
find "$CACHE/src" -name '*.sh' -exec sed -i 's/\r$//' {} +

"$ENGINE" run --rm \
    -v "$CACHE/src:/build" \
    -v "$CACHE:/cache" \
    -v "$CACHE/cargo-registry:/root/.cargo/registry" \
    -v "$CACHE/rustup:/root/.rustup" \
    -v "$CACHE/npm:/root/.npm" \
    -v "$CACHE/uv:/root/.cache/uv" \
    -e CACHE_DIR=/cache \
    "$IMAGE" bash /build/desktop/linux/build.sh

mkdir -p "$REPO/desktop/dist-linux"
rm -f "$REPO/desktop/dist-linux/"*
cp "$CACHE/src/desktop/dist-linux/"* "$REPO/desktop/dist-linux/"
ls -l "$REPO/desktop/dist-linux"
