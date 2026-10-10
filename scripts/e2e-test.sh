#!/usr/bin/env bash
# Full-stack test without real secrets and without touching the live deployment:
# builds the images under their own fcm-e2e/*:$E2E_TAG tags (the live tags stay
# untouched), starts them as a separate compose project ($E2E_PROJECT) with the
# compose.test.yml overlay (no Tailscale login, no internet), runs the OAuth +
# MCP flow from scripts/e2e_flow.py and tears everything down again.
# Before the build it runs the offline tests of setup-secrets.sh and healthcheck.sh; once the stack is healthy
# it checks coach-mcp's read-only Garmin token from inside coach-mcp (scripts/e2e_coach_garmin.py).
#
#   scripts/e2e-test.sh
#   E2E_PROJECT=fcm-e2e-pr07 E2E_TAG=pr07 scripts/e2e-test.sh   parallel-safe run (scripts/check.sh)
#
# E2E_PROJECT: compose project, "fcm-e2e" or "fcm-e2e-<suffix>" (default fcm-e2e).
# E2E_TAG: tag of the fcm-e2e/<svc> images (default test). Two runs with different values of both
# share nothing: compose prefixes networks and volumes with the project and no service publishes a port.
source "$(dirname "$0")/lib.sh"
require_docker

PROJECT="${E2E_PROJECT:-fcm-e2e}"
export E2E_TAG="${E2E_TAG:-test}"
[[ "$PROJECT" =~ ^fcm-e2e(-[a-z0-9][a-z0-9_-]*)?$ ]] \
  || die "E2E_PROJECT must be fcm-e2e or fcm-e2e-<suffix> (lowercase letters, digits, - and _), not: $PROJECT"
[[ "$E2E_TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || die "Invalid E2E_TAG: $E2E_TAG"
TMP_SECRETS="$(mktemp -d)"
export SECRETS_DIR="$TMP_SECRETS"
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://fitness-mcp.example.ts.net}"
export OWNER_TAILSCALE_LOGIN="e2e-owner@test"
# The morning briefing push stays off in the e2e stack, whatever the caller's .env says.
export COACH_TELEGRAM_CHAT_ID=""
export COACH_WEEKLY_PUSH=""
# The nightly history job stays off too: no Garmin login and no internet in the e2e stack.
export COACH_HISTORY_ENABLED="false"
# No home location in the e2e stack either: get_weather_forecast must answer with its "no location" error.
export COACH_HOME_LATITUDE=""
export COACH_HOME_LONGITUDE=""
E2E_PASSWORD="e2e-$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
compose=(docker compose -p "$PROJECT" -f docker-compose.yml -f compose.test.yml)

BUILD_LOG="$(mktemp)"

cleanup() {
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$TMP_SECRETS" "$BUILD_LOG"
}
trap cleanup EXIT

info "Offline test of scripts/setup-secrets.sh..."
bash scripts/test-setup-secrets.sh >&2 || die "Offline test of setup-secrets.sh failed (output above)."

info "Offline test of scripts/healthcheck.sh..."
bash scripts/test-healthcheck.sh >&2 || die "Offline test of healthcheck.sh failed (output above)."

info "Build images..."
# --progress=plain: with stdout redirected, Compose's default (tty) progress renderer fails with
# "failed to get console: provided file is not a console". The plain log goes to a file and is
# shown only when the build fails.
"${compose[@]}" build --progress=plain >/dev/null 2>"$BUILD_LOG" \
  || { cat "$BUILD_LOG" >&2; die "Build failed (log above)."; }

info "Throwaway secrets..."
put() { printf '%s\n' "$2" > "$TMP_SECRETS/$1"; chmod 0644 "$TMP_SECRETS/$1"; }
put gateway_password_hash "$(printf '%s\n' "$E2E_PASSWORD" | docker run --rm -i --network none \
  --entrypoint python "fcm-e2e/gateway:$E2E_TAG" -m gateway.cli hash-password --stdin)"
put gateway_totp_secret "$(python3 -c 'import base64, os; print(base64.b32encode(os.urandom(20)).decode())')"
put hevy_backend_token "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
put garmin_backend_token "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
put garmin_readonly_token "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
put coach_backend_token "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
put telegram_bot_token ""
put hevy_api_key "00000000-0000-0000-0000-000000000000"
put ts_authkey "tskey-auth-unused-in-e2e"

info "Start stack ($PROJECT, images fcm-e2e/*:$E2E_TAG)..."
"${compose[@]}" up -d >/dev/null
for _ in $(seq 1 60); do
  unhealthy=0
  for svc in gateway hevy-mcp garmin-mcp coach-mcp; do
    cid="$("${compose[@]}" ps -q "$svc")"
    [[ -n "$cid" && "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid")" == healthy ]] \
      || unhealthy=1
  done
  [[ $unhealthy == 0 ]] && break
  sleep 2
done
"${compose[@]}" ps --format 'table {{.Service}}\t{{.Status}}'
[[ $unhealthy == 0 ]] || { "${compose[@]}" logs --tail 30; die "Services are not healthy."; }

info "coach-mcp -> garmin-mcp (read-only token)..."
"${compose[@]}" exec -T coach-mcp python - < scripts/e2e_coach_garmin.py \
  || { "${compose[@]}" logs --tail 30 garmin-mcp coach-mcp; die "Read-only token checks in coach-mcp failed (output above)."; }

status=0
"${compose[@]}" exec -T coach-mcp python - < scripts/e2e_hevy_client.py || status=1
"${compose[@]}" exec -T -e E2E_PASSWORD="$E2E_PASSWORD" gateway python - < scripts/e2e_flow.py || status=1
COMPOSE_PROJECT_NAME="$PROJECT" COMPOSE_FILE="docker-compose.yml:compose.test.yml" scripts/e2e_backup.sh || status=1

echo
info "Audit log (last 12 records):"
"${compose[@]}" exec -T gateway tail -n 12 /data/audit.jsonl
exit "$status"
