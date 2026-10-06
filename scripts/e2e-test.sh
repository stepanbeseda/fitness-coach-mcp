#!/usr/bin/env bash
# Full-stack test without real secrets and without touching the live deployment:
# builds the images, starts them as a separate compose project with the
# compose.test.yml overlay (no Tailscale login, no internet), runs the OAuth +
# MCP flow from scripts/e2e_flow.py and tears everything down again.
#
#   scripts/e2e-test.sh
source "$(dirname "$0")/lib.sh"
require_docker

PROJECT="fcm-e2e"
TMP_SECRETS="$(mktemp -d)"
export SECRETS_DIR="$TMP_SECRETS"
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://fitness-mcp.example.ts.net}"
export OWNER_TAILSCALE_LOGIN="e2e-owner@test"
E2E_PASSWORD="e2e-$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
compose=(docker compose -p "$PROJECT" -f docker-compose.yml -f compose.test.yml)

BUILD_LOG="$(mktemp)"

cleanup() {
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$TMP_SECRETS" "$BUILD_LOG"
}
trap cleanup EXIT

info "Build images..."
# --progress=plain: with stdout redirected, Compose's default (tty) progress renderer fails with
# "failed to get console: provided file is not a console". The plain log goes to a file and is
# shown only when the build fails.
"${compose[@]}" build --progress=plain >/dev/null 2>"$BUILD_LOG" \
  || { cat "$BUILD_LOG" >&2; die "Build selhal (log výše)."; }

info "Throwaway secrets..."
put() { printf '%s\n' "$2" > "$TMP_SECRETS/$1"; chmod 0644 "$TMP_SECRETS/$1"; }
put gateway_password_hash "$(printf '%s\n' "$E2E_PASSWORD" | docker run --rm -i --network none \
  --entrypoint python fitness-coach-mcp/gateway:0.1.0 -m gateway.cli hash-password --stdin)"
put gateway_totp_secret "$(python3 -c 'import base64, os; print(base64.b32encode(os.urandom(20)).decode())')"
put hevy_backend_token "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
put garmin_backend_token "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
put coach_backend_token "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
put hevy_api_key "00000000-0000-0000-0000-000000000000"
put ts_authkey "tskey-auth-unused-in-e2e"

info "Start stack ($PROJECT)..."
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
[[ $unhealthy == 0 ]] || { "${compose[@]}" logs --tail 30; die "Služby nejsou healthy."; }

status=0
"${compose[@]}" exec -T -e E2E_PASSWORD="$E2E_PASSWORD" gateway python - < scripts/e2e_flow.py || status=1

echo
info "Audit log (posledních 12 záznamů):"
"${compose[@]}" exec -T gateway tail -n 12 /data/audit.jsonl
exit "$status"
