#!/usr/bin/env bash
# Offline test of scripts/check-updates.sh. It builds a fake repo in a temporary directory with copies of
# scripts/lib.sh, scripts/check-updates.sh, scripts/lock-deps.sh and the five Dockerfiles, plus small fake
# locks, and runs the script there under `env -i` with stub `curl` and `docker` that answer from files and never
# call the real binaries. No Docker, no network, no access to .env.
#
#   scripts/test-check-updates.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILED=0
REPO="$WORK/repo"
FAKE="$WORK/fake"
UNEXPECTED_LOG="$WORK/unexpected.log"

check() {
  if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; FAILED=1; fi
}

mkdir -p "$WORK/bin" "$WORK/home" "$REPO/scripts" "$REPO/.github/audit"
for svc in gateway garmin-mcp coach-mcp hevy-mcp tailscale; do
  mkdir -p "$REPO/$svc"; cp "$SRC/$svc/Dockerfile" "$REPO/$svc/"
done
cp "$SRC/scripts/lib.sh" "$SRC/scripts/check-updates.sh" "$SRC/scripts/lock-deps.sh" "$REPO/scripts/"
for lock in gateway/requirements.lock garmin-mcp/requirements.lock coach-mcp/requirements.lock; do
  printf 'httpx==0.28.1 \\\n    --hash=sha256:%064d\n    # via fake\n' 0 > "$REPO/$lock"
done
printf 'pip-audit==2.10.1 \\\n    --hash=sha256:%064d\n' 0 > "$REPO/.github/audit/requirements.lock"

# Pins as the tree has them.
PY_REF="$(sed -n 's|^FROM \(python:[^@ ]*@sha256:[0-9a-f]*\).*|\1|p' "$SRC/gateway/Dockerfile")"
PY_TAG="${PY_REF%%@*}"; PY_DIGEST="${PY_REF##*@}"
UV_REF="$(sed -n 's|^UV_IMAGE="\([^"]*\)"|\1|p' "$SRC/scripts/lock-deps.sh")"
UV_TAG="${UV_REF%%@*}"; UV_DIGEST="${UV_REF##*@}"
UV_PIN="$(sed -n 's|^ghcr.io/astral-sh/uv:\([0-9.]*\)-.*|\1|p' <<< "$UV_TAG")"
TS_PIN="$(sed -n 's|^FROM tailscale/tailscale:v\([0-9.]*\)@.*|\1|p' "$SRC/tailscale/Dockerfile")"
HEVY_PIN="$(sed -n 's|^FROM ghcr.io/chrisdoc/hevy-mcp:\([0-9.]*\)@.*|\1|p' "$SRC/hevy-mcp/Dockerfile")"
MOVED="sha256:$(printf 'f%.0s' {1..64})"

cat > "$WORK/bin/curl" <<'STUB'
#!/bin/bash
url="${*: -1}"
case "$url" in
  https://hub.docker.com/v2/repositories/tailscale/tailscale/tags*) cat "$FAKE/tailscale.json" ;;
  https://registry.npmjs.org/hevy-mcp/latest) cat "$FAKE/hevy.json" ;;
  https://pypi.org/pypi/*/json)
    name="${url#https://pypi.org/pypi/}"; name="${name%/json}"
    [[ -f "$FAKE/pypi-$name" ]] || { printf 'UNEXPECTED curl %s\n' "$*" >> "$UNEXPECTED_LOG"; exit 22; }
    printf '{"info":{"version":"%s"}}' "$(cat "$FAKE/pypi-$name")" ;;
  *) printf 'UNEXPECTED curl %s\n' "$*" >> "$UNEXPECTED_LOG"; exit 22 ;;
esac
STUB
cat > "$WORK/bin/docker" <<'STUB'
#!/bin/bash
case "$*" in
  info) [[ ! -f "$FAKE/no_docker" ]] ;;
  "buildx imagetools inspect "*)
    ref="${*: -1}"; key="$(printf '%s' "$ref" | tr '/:' '__')"
    [[ -f "$FAKE/digest-$key" ]] || { printf 'UNEXPECTED docker %s\n' "$*" >> "$UNEXPECTED_LOG"; exit 1; }
    printf 'Name:      %s\nMediaType: application/vnd.oci.image.index.v1+json\nDigest:    %s\n' "$ref" "$(cat "$FAKE/digest-$key")" ;;
  *) printf 'UNEXPECTED docker %s\n' "$*" >> "$UNEXPECTED_LOG"; exit 99 ;;
esac
STUB
chmod +x "$WORK/bin/curl" "$WORK/bin/docker"

key() { printf '%s' "$1" | tr '/:' '__'; }

# reset: every pin is current, Docker is available.
reset() {
  rm -rf "$FAKE"; mkdir -p "$FAKE"; : > "$UNEXPECTED_LOG"
  printf '{"results":[{"name":"v%s"}]}' "$TS_PIN" > "$FAKE/tailscale.json"
  printf '{"version":"%s"}' "$HEVY_PIN" > "$FAKE/hevy.json"
  printf '0.28.1' > "$FAKE/pypi-httpx"
  printf '2.10.1' > "$FAKE/pypi-pip-audit"
  printf '%s' "$UV_PIN" > "$FAKE/pypi-uv"
  printf '%s' "$PY_DIGEST" > "$FAKE/digest-$(key "$PY_TAG")"
  printf '%s' "$UV_DIGEST" > "$FAKE/digest-$(key "$UV_TAG")"
  for svc in gateway garmin-mcp coach-mcp; do cp "$SRC/$svc/Dockerfile" "$REPO/$svc/"; done
}

run() {
  RC=0
  OUT="$(env -i HOME="$WORK/home" PATH="$WORK/bin:/usr/bin:/bin" FAKE="$FAKE" UNEXPECTED_LOG="$UNEXPECTED_LOG" \
    "$REPO/scripts/check-updates.sh" 2>&1)" || RC=$?
}

# line PATTERN: the output line that starts with PATTERN (the COMPONENT column).
line() { grep -E "^$1 " <<< "$OUT" || true; }

echo "check-updates.sh"

reset; run
check "exit 0 with everything current" '[[ $RC == 0 ]]'
check "no unexpected call" '[[ ! -s "$UNEXPECTED_LOG" ]]'
for svc in gateway garmin-mcp coach-mcp; do
  check "$svc python digest row is current" '[[ "$(line "$svc: $PY_TAG")" == *" ${PY_DIGEST:7:12} "*" current" ]]'
done
check "uv version row is current" '[[ "$(line "uv \\(lock-deps.sh\\)")" == *" $UV_PIN "*" current" ]]'
check "uv digest row is current" '[[ "$(line "uv image \\(digest\\)")" == *" ${UV_DIGEST:7:12} "*" current" ]]'
check "no mismatch warning" '[[ "$OUT" != *"pins of gateway, garmin-mcp and coach-mcp differ"* ]]'
check "tailscale and hevy-mcp rows still there" \
  '[[ "$(line tailscale/tailscale)" == *current && "$(line hevy-mcp)" == *current ]]'

reset; printf '%s' "$MOVED" > "$FAKE/digest-$(key "$PY_TAG")"; run
for svc in gateway garmin-mcp coach-mcp; do
  check "$svc python digest moved upstream: UPDATE" '[[ "$(line "$svc: $PY_TAG")" == *" ffffffffffff "*UPDATE* ]]'
done

reset
sed -i "s|^\(FROM python:[^@]*\)@sha256:[0-9a-f]*|\1@$MOVED|" "$REPO/coach-mcp/Dockerfile"
run
check "differing python pins: warning" '[[ "$OUT" == *"The python base pins of gateway, garmin-mcp and coach-mcp differ"* ]]'
check "differing python pins: coach row shows its own pin" '[[ "$(line "coach-mcp: $PY_TAG")" == *" ffffffffffff "*UPDATE* ]]'
check "differing python pins: gateway row stays current" '[[ "$(line "gateway: $PY_TAG")" == *current ]]'

reset
python3 - "$REPO/garmin-mcp/Dockerfile" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
text = re.sub(r"^FROM (python:\S+) AS deps$", r"ARG BASE_IMAGE=\1\n\nFROM ${BASE_IMAGE} AS deps", text, flags=re.M)
open(path, "w", encoding="utf-8").write(text)
PY
run
check "an ARG pin is reported, not skipped" '[[ "$(line "garmin-mcp: python")" == *"no literal FROM python:<tag>@sha256 line"* ]]'

reset; printf '99.0.0' > "$FAKE/pypi-uv"; run
check "newer uv release: UPDATE" '[[ "$(line "uv \\(lock-deps.sh\\)")" == *" $UV_PIN "*" 99.0.0 "*UPDATE* ]]'

reset; printf '%s' "$MOVED" > "$FAKE/digest-$(key "$UV_TAG")"; run
check "uv digest moved: UPDATE" '[[ "$(line "uv image \\(digest\\)")" == *" ffffffffffff "*UPDATE* ]]'

reset; : > "$FAKE/no_docker"; run
check "without Docker: exit 0" '[[ $RC == 0 ]]'
check "without Docker: no digest rows" '[[ -z "$(line "gateway: $PY_TAG")" && -z "$(line "uv image \\(digest\\)")" ]]'
check "without Docker: uv version row stays" '[[ "$(line "uv \\(lock-deps.sh\\)")" == *current ]]'
check "without Docker: no docker inspect call" '[[ ! -s "$UNEXPECTED_LOG" ]]'

reset; printf '2.11.0' > "$FAKE/pypi-pip-audit"; run
check "the audit tool lock is read" '[[ "$(line ".github/audit: pip-audit")" == *" 2.10.1 "*" 2.11.0 "*UPDATE* ]]'

if (( FAILED )); then echo "check-updates.sh tests FAILED"; exit 1; fi
echo "check-updates.sh tests OK"
