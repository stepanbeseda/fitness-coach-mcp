#!/usr/bin/env bash
# Offline test of the token steps in scripts/setup-secrets.sh. Never runs the script itself:
# exactly the named functions are extracted from it and run against a throwaway secrets directory, with
# recreate/sudo/prompts replaced by stubs that only record what they were asked to do.
# No Docker, no network, no access to ./secrets or .env.
#
#   scripts/test-setup-secrets.sh
set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")" && pwd)/setup-secrets.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILED=0

FUNCS=(ensure_dir write_secret has_secret new_backend_token cmd_backend_tokens
       ensure_backend_tokens ensure_garmin_readonly_token cmd_all)
for f in "${FUNCS[@]}"; do
  # One function per name: a one-line function ends on its own line, a block at the next "}" line.
  body="$(awk -v f="$f" 'p == 0 && index($0, f "() {") == 1 { p = 1; print; if ($0 ~ /}[ \t]*$/) p = 0; next }
                         p { print; if ($0 ~ /^}/) p = 0 }' "$SCRIPT")"
  [[ -n "$body" ]] || { echo "FAIL function $f not found in setup-secrets.sh"; exit 1; }
  printf '%s\n' "$body" >> "$WORK/funcs.sh"
done

check() {
  if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; FAILED=1; fi
}

# One case = a fresh secrets dir and a fresh log; the case body runs in a subshell.
run_case() {
  local name="$1" setup="$2" action="$3"
  CASE="$WORK/$name"
  mkdir -p "$CASE/secrets" "$CASE/bin"
  : > "$CASE/log"
  (
    set -euo pipefail
    export SECRETS_DIR="$CASE/secrets" APP_GID="$(id -g)" LOG="$CASE/log"
    # shellcheck disable=SC1091
    source "$WORK/funcs.sh"
    set_perms() { chmod 0640 "$1"; }
    recreate() { printf 'RECREATE %s\n' "$*" >> "$LOG"; }
    ok() { printf 'OK %s\n' "$*" >> "$LOG"; }
    info() { printf 'INFO %s\n' "$*" >> "$LOG"; }
    warn() { printf 'WARN %s\n' "$*" >> "$LOG"; }
    die() { printf 'DIE %s\n' "$*" >> "$LOG"; exit 3; }
    require_env() { :; }
    ensure_telegram_token() { :; }
    cmd_fix_perms() { :; }
    cmd_ts_authkey() { printf 'PROMPT ts\n' >> "$LOG"; }
    cmd_hevy_key() { printf 'PROMPT hevy\n' >> "$LOG"; }
    cmd_password() { printf 'PROMPT password\n' >> "$LOG"; }
    cmd_totp() { printf 'PROMPT totp\n' >> "$LOG"; }
    eval "$setup"
    eval "$action"
  ) && echo 0 > "$CASE/rc" || echo $? > "$CASE/rc"
}

S() { cat "$CASE/secrets/$1" 2>/dev/null || true; }
put() { printf '%s\n' "$2" > "$CASE/secrets/$1"; }
len_ok() { local v; v="$(S "$1")"; [[ ${#v} -ge 32 ]]; }
mode_is() { [[ "$(stat -c %a "$CASE/secrets/$1")" == "$2" ]]; }
logged() { grep -qxF "$1" "$CASE/log"; }
log_count() { grep -c "^$1" "$CASE/log" || true; }
OLD3='put hevy_backend_token hevy-old-0123456789abcdef0123456789;
      put garmin_backend_token garmin-old-0123456789abcdef012345678;
      put coach_backend_token coach-old-0123456789abcdef0123456789'
REST='put ts_authkey tskey-x; put hevy_api_key k; put gateway_password_hash h; put gateway_totp_secret t'
HINT='INFO Nový read-only token pro coach-mcp platí po nasazení: docker compose up -d --build garmin-mcp coach-mcp'

echo "[fresh install: all four tokens, one recreate as before]"
run_case fresh ":" "ensure_backend_tokens; ensure_garmin_readonly_token"
check "exit 0" '[[ $(cat "$CASE/rc") == 0 ]]'
for t in hevy_backend_token garmin_backend_token coach_backend_token garmin_readonly_token; do
  check "$t created (>= 32 chars, mode 640)" "len_ok $t && mode_is $t 640"
done
check "read-only token differs from the gateway token" '[[ "$(S garmin_readonly_token)" != "$(S garmin_backend_token)" ]]'
check "recreate only from ensure_backend_tokens" '[[ $(log_count RECREATE) == 1 ]] && logged "RECREATE hevy-mcp garmin-mcp coach-mcp gateway"'

echo "[existing install before PR 12: only the read-only token is added, nothing recreated]"
run_case existing "$OLD3" "ensure_backend_tokens; ensure_garmin_readonly_token"
check "read-only token created" 'len_ok garmin_readonly_token && mode_is garmin_readonly_token 640'
check "gateway tokens untouched" '[[ "$(S garmin_backend_token)" == garmin-old-0123456789abcdef012345678 && "$(S hevy_backend_token)" == hevy-old-0123456789abcdef0123456789 && "$(S coach_backend_token)" == coach-old-0123456789abcdef0123456789 ]]'
check "no recreate" '[[ $(log_count RECREATE) == 0 ]]'
check "deploy hint printed" 'logged "$HINT"'

echo "[complete install: second run changes nothing]"
run_case complete "$OLD3; put garmin_readonly_token ro-old-0123456789abcdef0123456789ab" \
  "ensure_backend_tokens; ensure_garmin_readonly_token"
check "read-only token kept" '[[ "$(S garmin_readonly_token)" == ro-old-0123456789abcdef0123456789ab ]]'
check "nothing written, recreated or hinted" '[[ ! -s "$CASE/log" ]]'

echo "[empty read-only file counts as missing]"
run_case empty "$OLD3; : > \"\$SECRETS_DIR/garmin_readonly_token\"" "ensure_garmin_readonly_token"
check "empty file replaced by a token" 'len_ok garmin_readonly_token'

echo "[generator failure: nothing stored]"
printf '#!/bin/sh\necho short\n' > "$WORK/python3"; chmod +x "$WORK/python3"
run_case genfail "$OLD3; PATH=\"$WORK:\$PATH\"" "ensure_garmin_readonly_token"
check "dies with the token name" '[[ $(cat "$CASE/rc") == 3 ]] && grep -q "^DIE .*garmin_readonly_token" "$CASE/log"'
check "no read-only file" '[[ ! -e "$CASE/secrets/garmin_readonly_token" ]]'

echo "[rotation: backend-tokens rotates the read-only token too]"
run_case rotate "$OLD3; put garmin_readonly_token ro-old-0123456789abcdef0123456789ab" "cmd_backend_tokens"
check "read-only token rotated" '[[ "$(S garmin_readonly_token)" != ro-old-0123456789abcdef0123456789ab ]] && len_ok garmin_readonly_token'
check "gateway garmin token rotated" '[[ "$(S garmin_backend_token)" != garmin-old-0123456789abcdef012345678 ]]'
check "one recreate of all backends and the gateway" '[[ $(log_count RECREATE) == 1 ]] && logged "RECREATE hevy-mcp garmin-mcp coach-mcp gateway"'

echo "[setup-secrets.sh without arguments (cmd_all) on an existing install]"
run_case all "$OLD3; $REST" "cmd_all"
check "read-only token created by cmd_all" 'len_ok garmin_readonly_token'
check "no prompts, no recreate" '[[ $(log_count PROMPT) == 0 && $(log_count RECREATE) == 0 ]]'

echo
if [[ $FAILED == 0 ]]; then echo "SETUP-SECRETS OK"; else echo "SETUP-SECRETS FAILED"; exit 1; fi
