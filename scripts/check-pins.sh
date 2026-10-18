#!/usr/bin/env bash
# Offline consistency check of the pinned images (CI, `scripts` job; no Docker, no network, does not read .env).
# Fails when
#   - a FROM line of a Dockerfile names an image that is not an earlier stage and is not pinned by digest
#     (an ARG in FROM hides the pin from Dependabot and from this script);
#   - the python base of gateway, garmin-mcp and coach-mcp is not the same tag and digest in all three;
#   - the image tag of tailscale or hevy-mcp in docker-compose.yml differs from the upstream version pinned in
#     its Dockerfile. Dependabot bumps only the Dockerfile; add the matching compose tag to its PR.
#
#   scripts/check-pins.sh
set -euo pipefail
cd "$(dirname "$0")/.."

problems=0
problem() { printf '✘ %s\n' "$*"; problems=$((problems + 1)); }

# 1. Every FROM is an earlier stage or an image pinned by digest.
for df in */Dockerfile; do
  stages=" "
  while read -r line; do
    read -r _ ref rest <<< "$line"
    if [[ "$stages" != *" $ref "* && ! "$ref" =~ @sha256:[0-9a-f]{64}$ ]]; then
      problem "$df: FROM $ref is neither an earlier stage nor pinned by digest"
    fi
    if [[ "$rest" =~ ^[Aa][Ss][[:space:]]+([^[:space:]]+) ]]; then stages+="${BASH_REMATCH[1]} "; fi
  done < <(grep -iE '^[[:space:]]*FROM[[:space:]]' "$df")
done

# 2. One python base for the three Python services.
py_pins=()
for svc in gateway garmin-mcp coach-mcp; do
  pin="$(sed -n 's|^FROM \(python:[^ @]*@sha256:[0-9a-f]\{64\}\).*|\1|p' "$svc/Dockerfile")"
  if [[ -z "$pin" ]]; then problem "$svc/Dockerfile: no 'FROM python:<tag>@sha256:<digest>' line"; continue; fi
  if [[ "$pin" == *$'\n'* ]]; then problem "$svc/Dockerfile: more than one python FROM line"; continue; fi
  py_pins+=("$svc=$pin")
done
if (( ${#py_pins[@]} == 3 )); then
  first="${py_pins[0]#*=}"
  for entry in "${py_pins[@]:1}"; do
    [[ "${entry#*=}" == "$first" ]] \
      || problem "python base differs: ${py_pins[0]%%=*} has ${first:0:40}…, ${entry%%=*} has ${entry#*=}"
  done
fi

# 3. The compose tag of tailscale and hevy-mcp is the upstream version of its Dockerfile.
compose_tag() {
  sed -n "s|^ *image: *fitness-coach-mcp/$1:\([^ ]*\) *$|\1|p" docker-compose.yml
}
check_tag() {  # name, upstream version from the Dockerfile
  local tag; tag="$(compose_tag "$1")"
  if [[ -z "$2" ]]; then problem "$1/Dockerfile: upstream version not found"; return; fi
  if [[ -z "$tag" ]]; then problem "docker-compose.yml: no 'image: fitness-coach-mcp/$1:<version>' line"; return; fi
  [[ "$tag" == "$2" ]] || problem "docker-compose.yml: fitness-coach-mcp/$1:$tag, but $1/Dockerfile pins $2"
}
check_tag tailscale "$(sed -n 's|^FROM tailscale/tailscale:v\([0-9.]*\)@sha256:.*|\1|p' tailscale/Dockerfile)"
check_tag hevy-mcp "$(sed -n 's|^FROM ghcr.io/chrisdoc/hevy-mcp:\([0-9.]*\)@sha256:.*|\1|p' hevy-mcp/Dockerfile)"

if (( problems )); then
  echo "PINS FAILED problems=$problems (docs/operations.md, \"Updates\")"
  exit 1
fi
py_digest="${first#*@sha256:}"
echo "PINS OK python=${py_digest:0:12} tailscale=$(compose_tag tailscale) hevy-mcp=$(compose_tag hevy-mcp)"
