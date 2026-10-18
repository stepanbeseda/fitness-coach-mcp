#!/usr/bin/env bash
# Offline test of scripts/check-pins.sh. Each case copies the script, the five Dockerfiles and
# docker-compose.yml into a temporary repo, changes one pin the way an update (or a mistake) would, and checks
# the exit code and the message. No Docker, no network.
#
#   scripts/test-check-pins.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILED=0
SERVICES=(gateway garmin-mcp coach-mcp hevy-mcp tailscale)

check() {
  if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; FAILED=1; fi
}

# fresh NAME: a copy of the tree's pins in $WORK/NAME; prints the path.
fresh() {
  local repo="$WORK/$1"
  mkdir -p "$repo/scripts"
  cp "$SRC/scripts/check-pins.sh" "$repo/scripts/"
  cp "$SRC/docker-compose.yml" "$repo/"
  for svc in "${SERVICES[@]}"; do mkdir -p "$repo/$svc"; cp "$SRC/$svc/Dockerfile" "$repo/$svc/"; done
  printf '%s' "$repo"
}

# run REPO: runs check-pins.sh there; sets RC and OUT.
run() {
  RC=0
  OUT="$("$1/scripts/check-pins.sh" 2>&1)" || RC=$?
}

OTHER_DIGEST="sha256:$(printf '0%.0s' {1..63})1"

echo "check-pins.sh"

repo="$(fresh tree)"; run "$repo"
check "the tree passes" '[[ $RC == 0 && "$OUT" == "PINS OK python="* ]]'

repo="$(fresh py-digest)"
sed -i "s|^\(FROM python:[^@]*\)@sha256:[0-9a-f]*|\1@$OTHER_DIGEST|" "$repo/coach-mcp/Dockerfile"
run "$repo"
check "a different python digest in one Dockerfile fails" \
  '[[ $RC == 1 && "$OUT" == *"python base differs"*"coach-mcp has python:"*"$OTHER_DIGEST"* ]]'

repo="$(fresh py-tag)"
sed -i 's|^FROM python:3\.13-slim@|FROM python:3.14-slim@|' "$repo/garmin-mcp/Dockerfile"
run "$repo"
check "a different python tag in one Dockerfile fails" \
  '[[ $RC == 1 && "$OUT" == *"python base differs"*"garmin-mcp has python:3.14-slim@"* ]]'

repo="$(fresh py-all)"
for svc in gateway garmin-mcp coach-mcp; do
  sed -i "s|^\(FROM python:[^@]*\)@sha256:[0-9a-f]*|\1@$OTHER_DIGEST|" "$repo/$svc/Dockerfile"
done
run "$repo"
check "the same new python digest in all three passes" '[[ $RC == 0 && "$OUT" == "PINS OK python=000000000000 "* ]]'

repo="$(fresh py-arg)"
python3 - "$repo/garmin-mcp/Dockerfile" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
text = re.sub(r"^FROM (python:\S+) AS deps$", r"ARG BASE_IMAGE=\1\n\nFROM ${BASE_IMAGE} AS deps", text, flags=re.M)
open(path, "w", encoding="utf-8").write(text)
PY
run "$repo"
check "an ARG in FROM fails" '[[ $RC == 1 && "$OUT" == *'\''garmin-mcp/Dockerfile: FROM ${BASE_IMAGE} is neither'\''* ]]'
check "an ARG in FROM also hides the python pin" \
  '[[ "$OUT" == *"garmin-mcp/Dockerfile: no '\''FROM python:<tag>@sha256:<digest>'\'' line"* ]]'

repo="$(fresh unpinned)"
sed -i 's|^\(FROM ghcr.io/chrisdoc/hevy-mcp:[0-9.]*\)@sha256:[0-9a-f]*|\1|' "$repo/hevy-mcp/Dockerfile"
run "$repo"
check "an image without a digest fails" \
  '[[ $RC == 1 && "$OUT" == *"hevy-mcp/Dockerfile: FROM ghcr.io/chrisdoc/hevy-mcp:"*"neither an earlier stage nor pinned by digest"* ]]'

repo="$(fresh ts-compose)"
sed -i 's|image: fitness-coach-mcp/tailscale:[0-9.]*|image: fitness-coach-mcp/tailscale:9.9.9|' "$repo/docker-compose.yml"
run "$repo"
check "a different tailscale compose tag fails" \
  '[[ $RC == 1 && "$OUT" == *"fitness-coach-mcp/tailscale:9.9.9, but tailscale/Dockerfile pins "* ]]'

repo="$(fresh hevy-compose)"
sed -i 's|image: fitness-coach-mcp/hevy-mcp:[0-9.]*|image: fitness-coach-mcp/hevy-mcp:9.9.9|' "$repo/docker-compose.yml"
run "$repo"
check "a different hevy-mcp compose tag fails" \
  '[[ $RC == 1 && "$OUT" == *"fitness-coach-mcp/hevy-mcp:9.9.9, but hevy-mcp/Dockerfile pins "* ]]'

repo="$(fresh ts-bump)"
sed -i "s|^FROM tailscale/tailscale:v[0-9.]*@sha256:[0-9a-f]*|FROM tailscale/tailscale:v9.9.9@$OTHER_DIGEST|" \
  "$repo/tailscale/Dockerfile"
run "$repo"
check "a Dockerfile bump without the compose tag fails" \
  '[[ $RC == 1 && "$OUT" == *"but tailscale/Dockerfile pins 9.9.9"* ]]'
sed -i 's|image: fitness-coach-mcp/tailscale:[0-9.]*|image: fitness-coach-mcp/tailscale:9.9.9|' "$repo/docker-compose.yml"
run "$repo"
check "the same bump with the compose tag passes" '[[ $RC == 0 && "$OUT" == *" tailscale=9.9.9 "* ]]'

repo="$(fresh no-compose-line)"
sed -i '/image: fitness-coach-mcp\/hevy-mcp:/d' "$repo/docker-compose.yml"
run "$repo"
check "a missing compose image line fails" \
  '[[ $RC == 1 && "$OUT" == *"docker-compose.yml: no '\''image: fitness-coach-mcp/hevy-mcp:<version>'\'' line"* ]]'

if (( FAILED )); then echo "check-pins.sh tests FAILED"; exit 1; fi
echo "check-pins.sh tests OK"
