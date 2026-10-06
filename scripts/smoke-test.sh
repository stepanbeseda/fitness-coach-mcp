#!/usr/bin/env bash
# End-to-end smoke test against the running stack, over the tailnet (MagicDNS):
# tools/list on all three endpoints, the last 3 Hevy workouts, today's Garmin
# recovery data, race predictions and heart-rate zones, the coach memory summary
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

HEVY_TOKEN="$(mint hevy)" || die "mint-token hevy selhal (běží gateway?)"
GARMIN_TOKEN="$(mint garmin)" || die "mint-token garmin selhal"
COACH_TOKEN="$(mint coach)" || die "mint-token coach selhal"

info "Test přes tailnet: $PUBLIC_BASE_URL (MagicDNS)"
status=0
python3 "$REPO_ROOT/scripts/smoke_test.py" "$PUBLIC_BASE_URL" "$HEVY_TOKEN" "$GARMIN_TOKEN" "$COACH_TOKEN" || status=1

echo
info "Test veřejné cesty (Funnel) z VPS: token z ne-Anthropic IP musí být odmítnut"
public_ip="$(curl -fsS -H 'accept: application/dns-json' \
  "https://cloudflare-dns.com/dns-query?name=${PUBLIC_HOST}&type=A" | jq -r '[.Answer[]? | select(.type==1) | .data][0] // empty')"
if [[ -z "$public_ip" ]]; then
  warn "Veřejné DNS pro $PUBLIC_HOST nevrací A záznam – je zapnutý Funnel?"
  status=1
else
  code="$(printf 'Authorization: Bearer %s\n' "$HEVY_TOKEN" | curl -sS -o /dev/null -w '%{http_code}' \
          --resolve "${PUBLIC_HOST}:443:${public_ip}" -H @- -H 'content-type: application/json' \
          -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' "$PUBLIC_BASE_URL/hevy/mcp")"
  if [[ "$code" == "403" ]]; then
    ok "Funnel ($public_ip): token z cizí sítě odmítnut (403)"
  else
    fail "Funnel ($public_ip): čekal jsem 403, dostal HTTP $code"
    status=1
  fi
fi
exit "$status"
