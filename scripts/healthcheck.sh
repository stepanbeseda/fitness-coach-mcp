#!/usr/bin/env bash
# Health of the whole stack. Exit code 0 = healthy, 1 = something needs attention (a problem, or with
# --notify a Telegram alert that could not be sent), 2 = bad arguments.
#
#   scripts/healthcheck.sh                     full report
#   scripts/healthcheck.sh --quiet             print only problems (for cron)
#   scripts/healthcheck.sh --quiet --notify    also send changes of the problem set to Telegram (for cron)
#
# Checks: container health, Tailscale node + Funnel, OAuth discovery over the tailnet and through the public
# Funnel path, the login page not being public, Garmin tokens, the coach memory database, the coach scheduler
# and history, the Hevy API key, drift of the pinned Claude client metadata and the age of the newest backup.
#
# --notify posts to the chat of COACH_TELEGRAM_CHAT_ID with secrets/telegram_bot_token (the morning briefing
# bot) when the set of failing checks changes, and once when all pass again. Every problem has a key (the
# failing check); a changed line under the same key is no change. The last accepted set is kept in
# ${XDG_STATE_HOME:-~/.local/state}/fitness-coach-mcp/healthcheck-notify.state. The token goes to curl on
# stdin, the chat id and the text in a temporary file: neither is ever on a command line or in the output.
source "$(dirname "$0")/lib.sh"

usage() {
  sed -n '2,7p' "$REPO_ROOT/scripts/healthcheck.sh" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

QUIET=0
NOTIFY=0
for arg in "$@"; do
  case "$arg" in
    --quiet) QUIET=1 ;;
    --notify) NOTIFY=1 ;;
    *) usage ;;
  esac
done

require_docker
require_env

problems=0
fail_keys=()
fail_texts=()
notify_failed=0
NOTIFY_TMP=""
report_ok()   { [[ $QUIET == 1 ]] || ok "$*"; }
report_warn() { warn "$*"; }
# report_fail KEY TEXT...: KEY names the failing check (no whitespace); the notify step compares keys, not texts.
report_fail() {
  local key="$1"
  [[ -n "$key" && "$key" != *[[:space:]]* ]] || die "healthcheck.sh: report_fail needs a problem key (bug)"
  shift
  fail "$*"
  problems=$((problems + 1))
  fail_keys+=("$key")
  fail_texts+=("$*")
}

# 1. containers
for svc in ts gateway hevy-mcp garmin-mcp coach-mcp; do
  cid="$(dc ps -q "$svc" 2>/dev/null || true)"
  if [[ -z "$cid" ]]; then
    report_fail "container-$svc" "$svc: not running"
    continue
  fi
  health="$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid")"
  if [[ "$health" == "running/healthy" ]]; then
    report_ok "$svc: $health"
  else
    report_fail "container-$svc" "$svc: $health"
  fi
done

# 2. tailscale node and funnel
ts_json="$(dc exec -T ts tailscale --socket /tmp/tailscaled.sock status --json 2>/dev/null || true)"
state="$(jq -r '.BackendState // empty' <<<"$ts_json" 2>/dev/null || true)"
dns="$(jq -r '.Self.DNSName // empty' <<<"$ts_json" 2>/dev/null || true)"
if [[ "$state" == "Running" && "${dns%.}" == "$PUBLIC_HOST" ]]; then
  report_ok "tailscale: node ${dns%.} online"
else
  report_fail tailscale-node "tailscale: state '${state:-?}', DNS '${dns:-?}' (expected $PUBLIC_HOST)"
fi
funnel="$(dc exec -T ts tailscale --socket /tmp/tailscaled.sock serve status --json 2>/dev/null || true)"
if jq -e --arg h "${PUBLIC_HOST}:443" '.AllowFunnel[$h] == true' <<<"$funnel" >/dev/null 2>&1; then
  report_ok "funnel: on for ${PUBLIC_HOST}:443"
else
  report_fail funnel-config "funnel: not on (check the funnel nodeAttrs for the tag in the tailnet policy)"
fi

# 3. discovery over the tailnet
meta="$(curl -fsS --max-time 10 "$PUBLIC_BASE_URL/.well-known/oauth-authorization-server" 2>/dev/null || true)"
if [[ "$(jq -r '.issuer // empty' <<<"$meta" 2>/dev/null)" == "$PUBLIC_BASE_URL" ]]; then
  report_ok "tailnet: OAuth metadata OK"
else
  report_fail tailnet-metadata "tailnet: OAuth metadata unreachable ($PUBLIC_BASE_URL)"
fi
for b in hevy garmin coach; do
  challenge="$(curl -sS --max-time 10 -o /dev/null -D - -X POST "$PUBLIC_BASE_URL/$b/mcp" 2>/dev/null | tr -d '\r' || true)"
  if grep -q '^HTTP/[0-9.]* 401' <<<"$challenge" && grep -qi '^www-authenticate: Bearer resource_metadata=' <<<"$challenge"; then
    report_ok "tailnet: /$b/mcp without a token returns 401 + resource_metadata"
  else
    report_fail "tailnet-mcp-$b" "tailnet: /$b/mcp does not return the expected 401 challenge"
  fi
done

# 4. public path (Funnel) resolved via public DNS
public_ip="$(curl -fsS --max-time 10 -H 'accept: application/dns-json' \
  "https://cloudflare-dns.com/dns-query?name=${PUBLIC_HOST}&type=A" 2>/dev/null \
  | jq -r '[.Answer[]? | select(.type==1) | .data][0] // empty' 2>/dev/null || true)"
if [[ -z "$public_ip" ]]; then
  report_fail funnel-dns "funnel: public DNS returns no A record for $PUBLIC_HOST"
else
  pub() { curl -sS --max-time 15 -o /dev/null -w '%{http_code}' --resolve "${PUBLIC_HOST}:443:${public_ip}" "$@" 2>/dev/null || echo 000; }
  prm="$(pub "$PUBLIC_BASE_URL/.well-known/oauth-protected-resource/garmin/mcp")"
  authz="$(pub "$PUBLIC_BASE_URL/authorize")"
  [[ "$prm" == 200 ]] && report_ok "funnel ($public_ip): discovery publicly reachable" \
                      || report_fail funnel-discovery "funnel ($public_ip): discovery returns HTTP $prm"
  [[ "$authz" == 403 ]] && report_ok "funnel: login page not publicly reachable (403)" \
                        || report_fail funnel-login "funnel: /authorize returns HTTP $authz (expected 403)"
fi

# 5. Garmin tokens
if dc exec -T garmin-mcp python -m garmin_coach_mcp.login --check >/dev/null 2>&1; then
  report_ok "garmin: tokens valid"
else
  report_fail garmin-tokens "garmin: tokens missing or expired → scripts/garmin-login.sh"
fi

# 5b. coach memory database
if dc exec -T coach-mcp python -c "import json,sys,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5)); sys.exit(0 if d.get('db') == 'ok' else 1)" >/dev/null 2>&1; then
  report_ok "coach: database OK"
else
  report_fail coach-db "coach: database unavailable → docker compose logs coach-mcp"
fi

# 5c. morning briefing scheduler (informational)
sched="$(dc exec -T coach-mcp python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5)).get('scheduler', '?'))" 2>/dev/null || echo '?')"
case "$sched" in
  on) report_ok "coach: morning briefing on" ;;
  off) report_ok "coach: morning briefing off (COACH_TELEGRAM_CHAT_ID in .env)" ;;
  stopped) report_fail coach-briefing "coach: morning briefing scheduler stopped → docker compose logs coach-mcp and docker compose restart coach-mcp" ;;
  *) report_warn "coach: morning briefing state unknown" ;;
esac

# 5d. nightly history (informational)
hist="$(dc exec -T coach-mcp python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5)).get('history', '?'))" 2>/dev/null || echo '?')"
case "$hist" in
  on) report_ok "coach: nightly history on" ;;
  off) report_ok "coach: nightly history off (COACH_HISTORY_ENABLED in .env)" ;;
  stopped) report_fail coach-history "coach: nightly history stopped → docker compose logs coach-mcp and docker compose restart coach-mcp" ;;
  *) report_warn "coach: nightly history state unknown" ;;
esac

# 6. Hevy API key
if [[ -r "$SECRETS_DIR/hevy_api_key" ]]; then
  code="$(printf 'api-key: %s\n' "$(tr -d '\r\n' < "$SECRETS_DIR/hevy_api_key")" | curl -sS --max-time 15 \
          -o /dev/null -w '%{http_code}' -H @- https://api.hevyapp.com/v1/user/info 2>/dev/null || echo 000)"
  case "$code" in
    200) report_ok "hevy: API key valid" ;;
    401|403) report_fail hevy-key "hevy: API key rejected → scripts/setup-secrets.sh hevy-key" ;;
    *) report_warn "hevy: API not responding (HTTP $code)" ;;
  esac
else
  report_warn "hevy: secrets/hevy_api_key not readable for $(id -un), skipping"
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
        issues.append(f"WARN {cid}: cannot download ({exc})")
        continue
    live = {u.split("?")[0] for u in doc.get("redirect_uris", [])}
    pinned = set(client["redirect_uris"])
    missing = sorted(live - pinned)
    if missing:
        issues.append(f"FAIL {cid}: new redirect_uris {missing} → add them to gateway/config.toml")
print("\n".join(issues))
PY
)"
if [[ -z "$drift" ]]; then
  report_ok "claude CIMD: pinned metadata matches the live documents"
else
  while IFS= read -r line; do
    case "$line" in
      FAIL*)
        rest="${line#FAIL }"
        cid="${rest%%: *}"
        report_fail "cimd:$cid" "claude CIMD: $rest"
        ;;
      *) report_warn "claude CIMD: ${line#WARN }" ;;
    esac
  done <<<"$drift"
fi

# 8. age of the newest backup (scripts/backup.sh names its archives by UTC time)
backup_dir="${BACKUP_DIR:-}"
backup_max="${BACKUP_MAX_AGE_HOURS:-26}"
if [[ -z "$backup_dir" ]]; then
  report_ok "backup: BACKUP_DIR not set, backup age not checked"
elif [[ ! "$backup_max" =~ ^[1-9][0-9]{0,2}$ ]]; then
  report_fail backup "backup: BACKUP_MAX_AGE_HOURS must be a whole number of hours from 1 to 999"
elif [[ "$backup_dir" != /* || ! -d "$backup_dir" || ! -r "$backup_dir" || ! -x "$backup_dir" ]]; then
  report_fail backup "backup: BACKUP_DIR is not an absolute path to a readable directory → README, Backups"
else
  # `|| true`: under pipefail a grep without a match would end the script.
  newest="$(find "$backup_dir" -mindepth 1 -maxdepth 1 -type f -printf '%f\n' 2>/dev/null \
    | grep -E '^fitness-coach-backup-[0-9]{8}T[0-9]{6}Z\.tgz$' | LC_ALL=C sort | tail -n1 || true)"
  if [[ -z "$newest" ]]; then
    report_fail backup "backup: no archive in BACKUP_DIR yet → scripts/backup.sh"
  else
    stamp="${newest#fitness-coach-backup-}"
    stamp="${stamp%.tgz}"
    then_ts="$(date -u -d "${stamp:0:4}-${stamp:4:2}-${stamp:6:2} ${stamp:9:2}:${stamp:11:2}:${stamp:13:2}" +%s 2>/dev/null)" \
      || then_ts=""
    if [[ -z "$then_ts" ]]; then
      report_fail backup "backup: cannot read the time of the newest archive $newest"
    else
      age=$(( $(date +%s) - then_ts ))
      if (( age < -3600 )); then
        report_fail backup "backup: newest archive $newest lies in the future → check the clock and BACKUP_DIR"
      elif (( age > backup_max * 3600 )); then
        report_fail backup "backup: newest archive is $(( age / 3600 )) h old (limit $backup_max h) → scripts/backup.sh, README Troubleshooting"
      else
        report_ok "backup: newest archive $(( age / 3600 )) h old"
      fi
    fi
  fi
fi

# notify_fail REASON: a message that did not go out. The problems are repeated on stderr, because the
# recommended cron line drops stdout and cron mail must still carry them.
notify_fail() {
  notify_failed=1
  printf 'Notify failed: %s. The next run with --notify tries again.\n' "$1" >&2
  if (( problems > 0 )); then
    printf 'Current problems:\n' >&2
    local text
    for text in "${fail_texts[@]}"; do
      printf '  - %s\n' "$text" >&2
    done
  fi
}

# Sends the change of the problem set to Telegram (see the header). Every failure calls notify_fail and returns.
notify_step() {
  local chat="${COACH_TELEGRAM_CHAT_ID:-}" token_file="$SECRETS_DIR/telegram_bot_token" token
  if [[ -z "$chat" ]]; then
    notify_fail "COACH_TELEGRAM_CHAT_ID is not set in .env (scripts/setup-secrets.sh telegram-chat-id shows it)"
    return 0
  fi
  if [[ ! "$chat" =~ ^-?[1-9][0-9]{0,19}$ ]]; then
    notify_fail "COACH_TELEGRAM_CHAT_ID in .env is not a numeric Telegram chat id"
    return 0
  fi
  if [[ ! -f "$token_file" || ! -r "$token_file" ]]; then
    notify_fail "secrets/telegram_bot_token is missing or not readable for $(id -un)"
    return 0
  fi
  token="$(tr -d '\r\n' < "$token_file")"
  if [[ ! "$token" =~ $TELEGRAM_TOKEN_RE ]]; then
    notify_fail "secrets/telegram_bot_token does not hold a bot token (scripts/setup-secrets.sh telegram-token)"
    return 0
  fi

  local dir="${XDG_STATE_HOME:-${HOME:-}/.local/state}/fitness-coach-mcp"
  local state="$dir/healthcheck-notify.state"
  umask 077
  if ! mkdir -p "$dir" 2>/dev/null || ! { exec 8>>"$dir/healthcheck-notify.lock"; } 2>/dev/null; then
    notify_fail "cannot write the state file in $dir"
    return 0
  fi
  if ! flock -n 8; then
    notify_fail "another healthcheck.sh --notify run is in progress"
    return 0
  fi

  NOTIFY_TMP="$(mktemp -d 2>/dev/null)" || NOTIFY_TMP=""
  if [[ -z "$NOTIFY_TMP" ]]; then
    notify_fail "cannot create a temporary directory"
    return 0
  fi
  local i text key
  : > "$NOTIFY_TMP/current"
  for i in "${!fail_keys[@]}"; do
    key="${fail_keys[$i]}"
    text="${fail_texts[$i]}"
    text="${text//$'\t'/ }"
    text="${text//$'\n'/ }"
    printf '%s\t%s\n' "$key" "$text" >> "$NOTIFY_TMP/current"
  done

  local verdict
  verdict="$(NOTIFY_CHAT_ID="$chat" python3 - "$state" "$NOTIFY_TMP/current" "$NOTIFY_TMP/body.json" "$state.new" 2>/dev/null <<'PY'
import json
import os
import sys

LIMIT = 4000


def read_pairs(path):
    pairs = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh.read().split("\n"):
                key, sep, text = line.partition("\t")
                if sep and key and key not in pairs:
                    pairs[key] = text
    except FileNotFoundError:
        pass
    return pairs


state, current, body, newstate = sys.argv[1:5]
prev = read_pairs(state)
cur = read_pairs(current)
with open(newstate, "w", encoding="utf-8") as fh:
    fh.write("".join(f"{k}\t{t}\n" for k, t in cur.items()))
if set(cur) == set(prev):
    print("none")
    sys.exit(0)
new = [t for k, t in cur.items() if k not in prev]
still = [t for k, t in cur.items() if k in prev]
resolved = [t for k, t in prev.items() if k not in cur]
if cur:
    n = len(cur)
    lines = [f"⚠️ fitness-coach-mcp: {n} problem{'s' if n != 1 else ''}"]
else:
    lines = ["✅ fitness-coach-mcp: all checks pass again"]
for title, items in (("New:", new), ("Still:", still), ("Resolved:", resolved)):
    if items:
        lines.append(title)
        lines.extend(f"- {t}" for t in items)
cut = 0
while True:
    tail = f"\n… {cut} more line{'s' if cut != 1 else ''}" if cut else ""
    text = "\n".join(lines) + tail
    if len(text.encode("utf-8")) <= LIMIT or len(lines) == 1:
        break
    lines.pop()
    cut += 1
with open(body, "w", encoding="utf-8") as fh:
    json.dump({"chat_id": int(os.environ["NOTIFY_CHAT_ID"]), "text": text}, fh)
print("send")
PY
)" || verdict=""
  if [[ "$verdict" != "none" && "$verdict" != "send" ]]; then
    rm -f "$state.new"
    notify_fail "the message could not be composed (python3 failed)"
    return 0
  fi

  if [[ "$verdict" == "send" ]]; then
    local code
    code="$(telegram_api "$token" sendMessage "$NOTIFY_TMP/reply" "$NOTIFY_TMP/body.json")"
    if [[ "$code" != 200 ]] \
      || ! python3 -c 'import json, sys; sys.exit(0 if json.load(open(sys.argv[1])).get("ok") is True else 1)' \
        "$NOTIFY_TMP/reply" 2>/dev/null; then
      rm -f "$state.new"
      notify_fail "Telegram did not accept the message (HTTP $code)"
      return 0
    fi
  fi
  if ! mv -f "$state.new" "$state"; then
    notify_fail "cannot write the state file in $dir"
    return 0
  fi
  if [[ "$verdict" == "send" ]]; then
    report_ok "notify: Telegram message sent"
  else
    report_ok "notify: no change since the last run, nothing sent"
  fi
}

cleanup() { [[ -z "$NOTIFY_TMP" ]] || rm -rf "$NOTIFY_TMP"; }
trap cleanup EXIT
if [[ $NOTIFY == 1 ]]; then
  notify_step
fi

if [[ $problems -eq 0 ]]; then
  [[ $notify_failed == 0 ]] || exit 1
  [[ $QUIET == 1 ]] || ok "All good."
  exit 0
fi
fail "Problems: $problems"
exit 1
