#!/usr/bin/env bash
# End-to-end smoke test against the running stack, over the tailnet (MagicDNS):
# tools/list on all three endpoints, the last 3 Hevy workouts, today's Garmin
# recovery data, race predictions and heart-rate zones, running tolerance, the
# newest strength activity's sets and an activity's weather, the coach memory summary
# and today's briefing verdict. Also checks that the public (Funnel) path
# refuses tokens from non-Anthropic networks.
#
#   scripts/smoke-test.sh
source "$(dirname "$0")/lib.sh"
require_docker
require_env

mint() {
  dc exec -T gateway python -m gateway.cli mint-token --backend "$1" --ttl 300 2>/dev/null
}

HEVY_TOKEN="$(mint hevy)" || die "mint-token hevy failed (is the gateway running?)"
GARMIN_TOKEN="$(mint garmin)" || die "mint-token garmin failed"
COACH_TOKEN="$(mint coach)" || die "mint-token coach failed"

info "Test over the tailnet: $PUBLIC_BASE_URL (MagicDNS)"
status=0
python3 "$REPO_ROOT/scripts/smoke_test.py" "$PUBLIC_BASE_URL" "$HEVY_TOKEN" "$GARMIN_TOKEN" "$COACH_TOKEN" || status=1

echo
info "Test of the public path (Funnel) from the VPS: a token from a non-Anthropic IP must be refused"
public_ip="$(curl -fsS -H 'accept: application/dns-json' \
  "https://cloudflare-dns.com/dns-query?name=${PUBLIC_HOST}&type=A" | jq -r '[.Answer[]? | select(.type==1) | .data][0] // empty')"
if [[ -z "$public_ip" ]]; then
  warn "Public DNS returns no A record for $PUBLIC_HOST; is Funnel on?"
  status=1
else
  code="$(printf 'Authorization: Bearer %s\n' "$HEVY_TOKEN" | curl -sS -o /dev/null -w '%{http_code}' \
          --resolve "${PUBLIC_HOST}:443:${public_ip}" -H @- -H 'content-type: application/json' \
          -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' "$PUBLIC_BASE_URL/hevy/mcp")"
  if [[ "$code" == "403" ]]; then
    ok "Funnel ($public_ip): token from a foreign network refused (403)"
  else
    fail "Funnel ($public_ip): expected 403, got HTTP $code"
    status=1
  fi
fi
exit "$status"
