#!/usr/bin/env bash
# Health of the whole stack. Exit code 0 = healthy, 1 = something needs attention.
#
#   scripts/healthcheck.sh           full report
#   scripts/healthcheck.sh --quiet   print only problems (for cron)
#
# Checks: container health, Tailscale node + Funnel, OAuth discovery over the
# tailnet and through the public Funnel path, the login page not being public,
# Garmin tokens, the coach memory database, the Hevy API key and drift of the
# pinned Claude client metadata.
source "$(dirname "$0")/lib.sh"
require_docker
require_env

QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1
problems=0
report_ok()   { [[ $QUIET == 1 ]] || ok "$*"; }
report_warn() { warn "$*"; }
report_fail() { fail "$*"; problems=$((problems + 1)); }

# 1. containers
for svc in ts gateway hevy-mcp garmin-mcp coach-mcp; do
  cid="$(dc ps -q "$svc" 2>/dev/null || true)"
  if [[ -z "$cid" ]]; then
    report_fail "$svc: neběží"
    continue
  fi
  health="$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid")"
  if [[ "$health" == "running/healthy" ]]; then
    report_ok "$svc: $health"
  else
    report_fail "$svc: $health"
  fi
done

# 2. tailscale node and funnel
ts_json="$(dc exec -T ts tailscale --socket /tmp/tailscaled.sock status --json 2>/dev/null || true)"
state="$(jq -r '.BackendState // empty' <<<"$ts_json" 2>/dev/null || true)"
dns="$(jq -r '.Self.DNSName // empty' <<<"$ts_json" 2>/dev/null || true)"
if [[ "$state" == "Running" && "${dns%.}" == "$PUBLIC_HOST" ]]; then
  report_ok "tailscale: uzel ${dns%.} online"
else
  report_fail "tailscale: stav '${state:-?}', DNS '${dns:-?}' (čekám $PUBLIC_HOST)"
fi
funnel="$(dc exec -T ts tailscale --socket /tmp/tailscaled.sock serve status --json 2>/dev/null || true)"
if jq -e --arg h "${PUBLIC_HOST}:443" '.AllowFunnel[$h] == true' <<<"$funnel" >/dev/null 2>&1; then
  report_ok "funnel: zapnutý pro ${PUBLIC_HOST}:443"
else
  report_fail "funnel: není zapnutý (zkontroluj nodeAttrs funnel pro tag v tailnet policy)"
fi

# 3. discovery over the tailnet
meta="$(curl -fsS --max-time 10 "$PUBLIC_BASE_URL/.well-known/oauth-authorization-server" 2>/dev/null || true)"
if [[ "$(jq -r '.issuer // empty' <<<"$meta" 2>/dev/null)" == "$PUBLIC_BASE_URL" ]]; then
  report_ok "tailnet: OAuth metadata OK"
else
  report_fail "tailnet: OAuth metadata nedostupná ($PUBLIC_BASE_URL)"
fi
for b in hevy garmin coach; do
  challenge="$(curl -sS --max-time 10 -o /dev/null -D - -X POST "$PUBLIC_BASE_URL/$b/mcp" 2>/dev/null | tr -d '\r' || true)"
  if grep -q '^HTTP/[0-9.]* 401' <<<"$challenge" && grep -qi '^www-authenticate: Bearer resource_metadata=' <<<"$challenge"; then
    report_ok "tailnet: /$b/mcp bez tokenu vrací 401 + resource_metadata"
  else
    report_fail "tailnet: /$b/mcp nevrací očekávanou 401 výzvu"
  fi
done

# 4. public path (Funnel) resolved via public DNS
public_ip="$(curl -fsS --max-time 10 -H 'accept: application/dns-json' \
  "https://cloudflare-dns.com/dns-query?name=${PUBLIC_HOST}&type=A" 2>/dev/null \
  | jq -r '[.Answer[]? | select(.type==1) | .data][0] // empty' 2>/dev/null || true)"
if [[ -z "$public_ip" ]]; then
  report_fail "funnel: veřejné DNS nevrací A záznam pro $PUBLIC_HOST"
else
  pub() { curl -sS --max-time 15 -o /dev/null -w '%{http_code}' --resolve "${PUBLIC_HOST}:443:${public_ip}" "$@" 2>/dev/null || echo 000; }
  prm="$(pub "$PUBLIC_BASE_URL/.well-known/oauth-protected-resource/garmin/mcp")"
  authz="$(pub "$PUBLIC_BASE_URL/authorize")"
  [[ "$prm" == 200 ]] && report_ok "funnel ($public_ip): discovery veřejně dostupná" \
                      || report_fail "funnel ($public_ip): discovery vrací HTTP $prm"
  [[ "$authz" == 403 ]] && report_ok "funnel: přihlašovací stránka veřejně nedostupná (403)" \
                        || report_fail "funnel: /authorize vrací HTTP $authz (čekám 403)"
fi

# 5. Garmin tokens
if dc exec -T garmin-mcp python -m garmin_coach_mcp.login --check >/dev/null 2>&1; then
  report_ok "garmin: tokeny platné"
else
  report_fail "garmin: tokeny chybí nebo expirovaly → scripts/garmin-login.sh"
fi

# 5b. coach memory database
if dc exec -T coach-mcp python -c "import json,sys,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5)); sys.exit(0 if d.get('db') == 'ok' else 1)" >/dev/null 2>&1; then
  report_ok "coach: databáze OK"
else
  report_fail "coach: databáze nedostupná → docker compose logs coach-mcp"
fi

# 5c. morning briefing scheduler (informational)
sched="$(dc exec -T coach-mcp python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5)).get('scheduler', '?'))" 2>/dev/null || echo '?')"
case "$sched" in
  on) report_ok "coach: ranní briefing zapnutý" ;;
  off) report_ok "coach: ranní briefing vypnutý (COACH_TELEGRAM_CHAT_ID v .env)" ;;
  stopped) report_fail "coach: plánovač ranního briefingu skončil → docker compose logs coach-mcp a docker compose restart coach-mcp" ;;
  *) report_warn "coach: stav ranního briefingu nezjištěn" ;;
esac

# 5d. nightly history (informational)
hist="$(dc exec -T coach-mcp python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5)).get('history', '?'))" 2>/dev/null || echo '?')"
case "$hist" in
  on) report_ok "coach: noční historie zapnutá" ;;
  off) report_ok "coach: noční historie vypnutá (COACH_HISTORY_ENABLED v .env)" ;;
  stopped) report_fail "coach: noční historie skončila → docker compose logs coach-mcp a docker compose restart coach-mcp" ;;
  *) report_warn "coach: stav noční historie nezjištěn" ;;
esac

# 6. Hevy API key
if [[ -r "$SECRETS_DIR/hevy_api_key" ]]; then
  code="$(printf 'api-key: %s\n' "$(tr -d '\r\n' < "$SECRETS_DIR/hevy_api_key")" | curl -sS --max-time 15 \
          -o /dev/null -w '%{http_code}' -H @- https://api.hevyapp.com/v1/user/info 2>/dev/null || echo 000)"
  case "$code" in
    200) report_ok "hevy: API klíč platný" ;;
    401|403) report_fail "hevy: API klíč odmítnut → scripts/setup-secrets.sh hevy-key" ;;
    *) report_warn "hevy: API neodpovídá (HTTP $code)" ;;
  esac
else
  report_warn "hevy: secrets/hevy_api_key nečitelný pro $(id -un), přeskakuji"
fi

# 7. drift of pinned Claude client metadata documents (CIMD)
drift="$(python3 - "$REPO_ROOT/gateway/config.toml" <<'PY' 2>&1
import json, sys, tomllib, urllib.request
config = tomllib.load(open(sys.argv[1], "rb"))
issues = []
for client in config["clients"]:
    cid = client["client_id"]
    if not cid.startswith("https://"):
        continue
    try:
        req = urllib.request.Request(cid, headers={"Accept": "application/json", "User-Agent": "fitness-coach-mcp-healthcheck"})
        doc = json.load(urllib.request.urlopen(req, timeout=10))
    except Exception as exc:
        issues.append(f"WARN {cid}: nelze stáhnout ({exc})")
        continue
    live = {u.split("?")[0] for u in doc.get("redirect_uris", [])}
    pinned = set(client["redirect_uris"])
    missing = sorted(live - pinned)
    if missing:
        issues.append(f"FAIL {cid}: nové redirect_uris {missing} → doplň gateway/config.toml")
print("\n".join(issues))
PY
)"
if [[ -z "$drift" ]]; then
  report_ok "claude CIMD: připnutá metadata odpovídají živým dokumentům"
else
  while IFS= read -r line; do
    case "$line" in
      FAIL*) report_fail "claude CIMD: ${line#FAIL }" ;;
      *) report_warn "claude CIMD: ${line#WARN }" ;;
    esac
  done <<<"$drift"
fi

if [[ $problems -eq 0 ]]; then
  [[ $QUIET == 1 ]] || ok "Vše v pořádku."
  exit 0
fi
fail "Problémů: $problems"
exit 1
