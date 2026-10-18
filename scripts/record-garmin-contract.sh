#!/usr/bin/env bash
# Records garmin-mcp's answers that coach-mcp reads and puts the same file into both services:
# garmin-mcp/tests/contract/garmin_answers.json and coach-mcp/tests/contract/garmin_answers.json.
# Run it after a deliberate change of such an answer, then adapt coach-mcp until scripts/check.sh is green.
# --tools-list also records garmin-mcp/tests/tools_list.json, after a deliberate change of the tool list.
# Offline (the canned payloads of garmin-mcp/tests, no Garmin account, no network); holds /tmp/fcm-docker.lock.
#
#   scripts/record-garmin-contract.sh [--tag TAG] [--tools-list]
#
# TAG (default $CHECK_TAG, else "local") names the image fcm-garmin-record-TAG, removed at the end.
# Exit 0 = recorded, 1 = a step failed, 2 = bad arguments.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
die() { printf 'Error: %s\n' "$1" >&2; exit "${2:-1}"; }

tag="${CHECK_TAG:-local}"
tests=(tests/test_contract.py)
record=(-e RECORD_GARMIN_CONTRACT=1)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag) [[ $# -ge 2 ]] || usage; tag="$2"; shift 2 ;;
    --tag=*) tag="${1#--tag=}"; shift ;;
    --tools-list) tests+=(tests/test_tools_list.py); record+=(-e RECORD_TOOLS_LIST=1); shift ;;
    -h|--help) usage ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage ;;
  esac
done
[[ "$tag" =~ ^[a-z0-9][a-z0-9_-]{0,39}$ ]] || die "Invalid tag '$tag' (lowercase letters, digits, - and _)." 2

image="fcm-garmin-record-$tag"
trap 'docker image rm "$image" >/dev/null 2>&1 || true' EXIT
flock /tmp/fcm-docker.lock docker build -q --target dev -t "$image" garmin-mcp >/dev/null
docker run --rm --network none --user "$(id -u):$(id -g)" -e HOME=/tmp "${record[@]}" \
  -v "$REPO_ROOT/garmin-mcp:/work" -w /work --entrypoint python "$image" \
  -m pytest -p no:cacheprovider "${tests[@]}"
cp garmin-mcp/tests/contract/garmin_answers.json coach-mcp/tests/contract/garmin_answers.json
echo "RECORDED garmin-mcp/tests/contract/garmin_answers.json, copied to coach-mcp/tests/contract/"
