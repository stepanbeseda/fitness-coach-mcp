#!/usr/bin/env bash
# Re-generate the hash-locked requirements files from pyproject.toml using a
# pinned uv image (linux/aarch64, CPython 3.13).
#
#   scripts/lock-deps.sh [--upgrade]
source "$(dirname "$0")/lib.sh"
require_docker

UV_IMAGE="ghcr.io/astral-sh/uv:0.12.21-python3.13-trixie-slim@sha256:ca9f1a036df3ab14905132452f7c01ec17119fe8c75af450e5daa5acc35a30ec"
extra_args=()
[[ "${1:-}" == "--upgrade" ]] && extra_args+=(--upgrade)

lock() {
  local dir="$1" out="$2"; shift 2
  docker run --rm --user "$(id -u):$(id -g)" --tmpfs /tmp:mode=1777 -e UV_CACHE_DIR=/tmp/uv-cache \
    -v "$REPO_ROOT/$dir:/work" -w /work "$UV_IMAGE" \
    uv pip compile pyproject.toml "$@" "${extra_args[@]}" --generate-hashes --python-version 3.13 \
    --python-platform aarch64-unknown-linux-gnu --no-header -o "$out"
  ok "$dir/$out"
}

for dir in gateway garmin-mcp; do
  [[ -f "$dir/pyproject.toml" ]] || continue
  lock "$dir" requirements.lock
  if grep -q '^dev = \[' "$dir/pyproject.toml"; then
    lock "$dir" requirements-dev.lock --extra dev
  fi
done
