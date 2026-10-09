#!/usr/bin/env bash
# The project's check, one command, safe to run next to another check: the test stage of
# garmin-mcp, gateway and coach-mcp (forced to run, never a cached result) and the e2e test
# under a private compose project and private image tags. Every docker build and the e2e run
# hold /tmp/fcm-docker.lock (CHECK_LOCK=0 turns the lock off). Does not read .env.
#
#   scripts/check.sh [--tag TAG] [--only garmin|gateway|coach|e2e]... [--keep-images]
#
# TAG (default $CHECK_TAG, else "local") names the images fcm-<svc>-check-TAG, the e2e images
# fcm-e2e/<svc>:TAG and the e2e project fcm-e2e-TAG; use the PR's own tag (pr07). Logs go to a
# temp dir printed at the start. The last line of a full green run is
#   CHECK OK tree=<12 chars of HEAD^{tree}>[-dirty] garmin=<passed> gateway=<passed> coach=<passed> e2e=OK
# ("-dirty" = uncommitted changes were checked). A run with --only ends with "CHECK PARTIAL ..."
# and "skip" for the steps it left out. The script removes the images it built unless
# --keep-images. Exit 0 = green, 1 = a step failed, 2 = bad arguments.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOCK_FILE="/tmp/fcm-docker.lock"
E2E_SERVICES=(tailscale gateway hevy-mcp garmin-mcp coach-mcp)

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
info() { printf '%s\n' "$*" >&2; }
die() { printf 'Chyba: %s\n' "$1" >&2; exit "${2:-1}"; }

tag="${CHECK_TAG:-local}"
keep_images=0
only=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag) [[ $# -ge 2 ]] || usage; tag="$2"; shift 2 ;;
    --tag=*) tag="${1#--tag=}"; shift ;;
    --only) [[ $# -ge 2 ]] || usage; only+=("$2"); shift 2 ;;
    --only=*) only+=("${1#--only=}"); shift ;;
    --keep-images) keep_images=1; shift ;;
    -h|--help) usage ;;
    *) info "Neznámý argument: $1"; usage ;;
  esac
done
# The tag is part of image names and of a compose project name: lowercase, digits, - and _.
[[ "$tag" =~ ^[a-z0-9][a-z0-9_-]{0,39}$ ]] || die "Neplatný tag '$tag' (malá písmena, číslice, - a _)." 2
[[ "$tag" != test ]] || die "Tag 'test' patří výchozímu scripts/e2e-test.sh; zvol vlastní (pr<NN>)." 2
for step in "${only[@]}"; do
  [[ "$step" =~ ^(garmin|gateway|coach|e2e)$ ]] || die "Neznámý krok pro --only: $step" 2
done
wanted() { [[ ${#only[@]} -eq 0 ]] || [[ " ${only[*]} " == *" $1 "* ]]; }

docker info >/dev/null 2>&1 || die "Docker není dostupný pro uživatele $(id -un)."
command -v flock >/dev/null || [[ "${CHECK_LOCK:-1}" == 0 ]] || die "Chybí flock (nebo CHECK_LOCK=0)."

# locked CMD...: run CMD under the machine-wide docker lock (2 CPUs: one build at a time).
locked() {
  if [[ "${CHECK_LOCK:-1}" == 0 ]]; then "$@"; else flock "$LOCK_FILE" "$@"; fi
}

tree_id() {
  local tree
  tree="$(git rev-parse --short=12 'HEAD^{tree}')"
  [[ -z "$(git status --porcelain)" ]] || tree="$tree-dirty"
  printf '%s' "$tree"
}

LOG_DIR="$(mktemp -d "/tmp/fcm-check-$tag.XXXXXX")"
built=()
cleanup() {
  [[ $keep_images == 1 || ${#built[@]} -eq 0 ]] || docker image rm "${built[@]}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# fail STEP LOG: name the failing step, show the end of its log and exit 1.
fail() {
  info ""
  info "CHECK FAILED step=$1 (log: $2), posledních 40 řádků:"
  tail -n 40 "$2" >&2 || true
  exit 1
}

tree="$(tree_id)"
info "Check tree=$tree tag=$tag, logy: $LOG_DIR"
declare -A result=([garmin]=skip [gateway]=skip [coach]=skip [e2e]=skip)

# Test stage of one service. --no-cache-filter test re-runs the suite even for an unchanged tree.
for pair in garmin:garmin-mcp gateway:gateway coach:coach-mcp; do
  name="${pair%%:*}" dir="${pair#*:}"
  wanted "$name" || continue
  image="fcm-$name-check-$tag" log="$LOG_DIR/$name.log"
  info "[$name] docker build --target test $dir -> $image ..."
  built+=("$image")
  locked docker build --progress=plain --target test --no-cache-filter test -t "$image" "$dir" \
    >"$log" 2>&1 || fail "$name" "$log"
  passed="$(grep -Eo '[0-9]+ passed' "$log" | tail -n 1 | cut -d' ' -f1 || true)"
  [[ -n "$passed" ]] || fail "$name (v logu chybí souhrn pytestu)" "$log"
  result[$name]="$passed"
  info "[$name] $passed passed"
done

if wanted e2e; then
  log="$LOG_DIR/e2e.log"
  info "[e2e] scripts/e2e-test.sh, projekt fcm-e2e-$tag, image fcm-e2e/*:$tag ..."
  for svc in "${E2E_SERVICES[@]}"; do built+=("fcm-e2e/$svc:$tag"); done
  locked env E2E_PROJECT="fcm-e2e-$tag" E2E_TAG="$tag" scripts/e2e-test.sh >"$log" 2>&1 || fail e2e "$log"
  grep -q '^E2E OK$' "$log" || fail "e2e (chybí řádek E2E OK)" "$log"
  if grep -q 'E2E backup OK' scripts/e2e_backup.sh 2>/dev/null; then
    grep -q 'E2E backup OK' "$log" || fail "e2e (chybí řádek E2E backup OK)" "$log"
  fi
  result[e2e]=OK
  info "[e2e] OK"
fi

[[ "$(tree_id)" == "$tree" ]] || die "Strom se během checku změnil ($tree -> $(tree_id)); spusť check znovu."
verdict=OK
[[ ${#only[@]} -eq 0 ]] || verdict=PARTIAL
echo "CHECK $verdict tree=$tree garmin=${result[garmin]} gateway=${result[gateway]} coach=${result[coach]}" \
  "e2e=${result[e2e]}"
