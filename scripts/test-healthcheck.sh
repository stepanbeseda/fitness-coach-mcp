#!/usr/bin/env bash
# Offline test of scripts/healthcheck.sh (problem keys, the backup age check and --notify). It builds a fake
# repo in a temporary directory with copies of scripts/lib.sh and scripts/healthcheck.sh and runs the script
# there under `env -i`, with stub `docker` and `curl` that answer from files and never call the real binaries,
# and argv-logging shims for the tools the script uses. No Docker, no network, no access to ./secrets or .env,
# no real Telegram call.
#
#   scripts/test-healthcheck.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILED=0
REPO="$WORK/repo"
ARGV_LOG="$WORK/argv.log"
TOKEN='987654321:AAharness-token-harness-token-0001'
CHAT=123456789

WARN=$'\xe2\x9a\xa0\xef\xb8\x8f'
CHECK=$'\xe2\x9c\x85'
NL=$'\n'
ARROW=$'\xe2\x86\x92'
ELLIPSIS=$'\xe2\x80\xa6'
T_NOTRUN='coach-mcp: not running'
T_DB="coach: database unavailable $ARROW docker compose logs coach-mcp"
T_SCHED="coach: morning briefing scheduler stopped $ARROW docker compose logs coach-mcp and docker compose restart coach-mcp"
T_HIST="coach: nightly history stopped $ARROW docker compose logs coach-mcp and docker compose restart coach-mcp"
T_INB="coach: two-way Telegram stopped $ARROW docker compose logs coach-mcp and docker compose restart coach-mcp"
TRY_AGAIN='The next run with --notify tries again.'
R1='COACH_TELEGRAM_CHAT_ID is not set in .env (scripts/setup-secrets.sh telegram-chat-id shows it)'
R2='COACH_TELEGRAM_CHAT_ID in .env is not a numeric Telegram chat id'
R3="secrets/telegram_bot_token is missing or not readable for $(id -un)"
R4='secrets/telegram_bot_token does not hold a bot token (scripts/setup-secrets.sh telegram-token)'
R5='another healthcheck.sh --notify run is in progress'

check() {
  if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; FAILED=1; fi
}

# --- tools: the real binaries are resolved first, the script sees argv-logging shims -----------------------
mkdir -p "$WORK/bin" "$WORK/home" "$WORK/tmp" "$WORK/cases" "$REPO/scripts" "$REPO/gateway" "$REPO/secrets"
for tool in python3 jq tr cat sed grep head tail cut sort find date mv cp rm mkdir chmod mktemp flock id; do
  real="$(command -v "$tool")" || { echo "FAIL tool $tool not found"; exit 1; }
  cat > "$WORK/bin/$tool" <<SHIM
#!/bin/bash
{ printf '%s' '$tool'; for a in "\$@"; do printf ' %s' "\$a"; done; printf '\\n'; } >> "\$ARGV_LOG"
exec $real "\$@"
SHIM
  chmod +x "$WORK/bin/$tool"
done

cat > "$WORK/bin/docker" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >> "$FAKE/docker.log"
unexpected() { printf 'UNEXPECTED docker %s\n' "$*" >> "$UNEXPECTED_LOG"; exit 99; }
is_down() { [[ -f "$FAKE/down" ]] && grep -qxF "$1" "$FAKE/down"; }
case "$1" in
  info) exit 0 ;;
  inspect)
    cid="${@: -1}"; svc="${cid#cid-}"
    if [[ -f "$FAKE/health-$svc" ]]; then cat "$FAKE/health-$svc"; else echo running/healthy; fi
    exit 0 ;;
  compose) ;;
  *) unexpected "$@" ;;
esac
shift
case "$1" in
  ps) is_down "$3" || echo "cid-$3"; exit 0 ;;
  exec) ;;
  *) unexpected "compose $*" ;;
esac
shift 2   # exec -T
svc="$1"; shift
if is_down "$svc"; then echo "service \"$svc\" is not running" >&2; exit 1; fi
case "$svc" in
  ts)
    if [[ "$*" == *"serve status --json"* ]]; then
      if [[ -f "$FAKE/funnel_off" ]]; then echo '{}'; else echo '{"AllowFunnel":{"fitness-mcp.example.ts.net:443":true}}'; fi
    elif [[ "$*" == *"status --json"* ]]; then
      if [[ -f "$FAKE/ts_json" ]]; then cat "$FAKE/ts_json"
      elif [[ -f "$FAKE/ts_bad" ]]; then echo '{"BackendState":"Stopped"}'
      else echo '{"BackendState":"Running","Self":{"DNSName":"fitness-mcp.example.ts.net.","TailscaleIPs":["100.64.0.1","fd7a:115c:a1e0::1"]}}'; fi
    else unexpected "compose exec ts $*"; fi
    exit 0 ;;
  garmin-mcp)
    [[ "$*" == "python -m garmin_coach_mcp.login --check" ]] || unexpected "compose exec garmin-mcp $*"
    [[ ! -f "$FAKE/garmin_bad" ]]; exit $? ;;
  coach-mcp)
    [[ "$1 $2" == "python -c" ]] || unexpected "compose exec coach-mcp $*"
    code="$3"
    if [[ "$code" == *"get('db')"* ]]; then [[ ! -f "$FAKE/coach_db_bad" ]]; exit $?
    elif [[ "$code" == *"'scheduler'"* ]]; then if [[ -f "$FAKE/scheduler" ]]; then cat "$FAKE/scheduler"; else echo on; fi; exit 0
    elif [[ "$code" == *"'history'"* ]]; then if [[ -f "$FAKE/history" ]]; then cat "$FAKE/history"; else echo on; fi; exit 0
    elif [[ "$code" == *"'inbound'"* ]]; then if [[ -f "$FAKE/inbound" ]]; then cat "$FAKE/inbound"; else echo on; fi; exit 0
    else unexpected "compose exec coach-mcp python -c <code>"; fi ;;
  *) unexpected "compose exec $svc $*" ;;
esac
STUB

cat > "$WORK/bin/curl" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >> "$FAKE/curl.log"
args=("$@")
out=""; data=""; kcfg=0; hstdin=0; url=""; resolve=0; post=0; dump=0
for ((i = 0; i < ${#args[@]}; i++)); do
  a="${args[$i]}"; n="${args[$((i + 1))]:-}"
  case "$a" in
    -o) out="$n" ;;
    --data-binary) data="${n#@}" ;;
    -K) [[ "$n" == "-" ]] && kcfg=1 ;;
    -H) [[ "$n" == "@-" ]] && hstdin=1 ;;
    --resolve) resolve=1 ;;
    -X) [[ "$n" == POST ]] && post=1 ;;
    -D) dump=1 ;;
    http*) url="$a" ;;
  esac
done
if (( kcfg )); then
  cat >> "$FAKE/tg_config.log"
  n=$(( $(ls "$FAKE"/sent.*.json 2>/dev/null | wc -l) + 1 ))
  [[ -z "$data" ]] || cp "$data" "$FAKE/sent.$n.json"
  code=200; [[ ! -f "$FAKE/tg_code" ]] || code="$(cat "$FAKE/tg_code")"
  if [[ "$code" == 000 ]]; then printf 000; exit 7; fi
  if [[ -f "$FAKE/tg_reply" ]]; then cp "$FAKE/tg_reply" "$out"; else printf '{"ok":true,"result":{"message_id":1}}' > "$out"; fi
  printf '%s' "$code"; exit 0
fi
if (( hstdin )); then
  cat > /dev/null
  [[ "$url" == *api.hevyapp.com* ]] || { printf 'UNEXPECTED curl %s\n' "$*" >> "$UNEXPECTED_LOG"; exit 99; }
  if [[ -f "$FAKE/hevy_bad" ]]; then printf 401; else printf 200; fi
  exit 0
fi
if (( resolve )); then
  case "$url" in
    *oauth-protected-resource*) if [[ -f "$FAKE/disc_bad" ]]; then printf 500; else printf 200; fi; exit 0 ;;
    */authorize) if [[ -f "$FAKE/authz_bad" ]]; then printf 200; else printf 403; fi; exit 0 ;;
  esac
fi
if [[ "$url" =~ ^http://100\.64\.0\.1:(8080|9002)/$ ]]; then
  mode=refused
  [[ ! -f "$FAKE/direct_${BASH_REMATCH[1]}" ]] || mode="$(cat "$FAKE/direct_${BASH_REMATCH[1]}")"
  case "$mode" in
    refused) printf '0.000000 7'; exit 7 ;;
    blocked) printf '0.000000 28'; exit 28 ;;
    accept) printf '0.000150 0'; exit 0 ;;
    silent) printf '0.000150 28'; exit 28 ;;
    odd) printf '0.000000 6'; exit 6 ;;
  esac
fi
case "$url" in
  */.well-known/oauth-authorization-server)
    if [[ -f "$FAKE/meta_bad" ]]; then echo '{}'; else echo '{"issuer":"https://fitness-mcp.example.ts.net"}'; fi; exit 0 ;;
  */mcp)
    if (( post && dump )); then
      if [[ -f "$FAKE/challenge_bad" ]]; then printf 'HTTP/1.1 200\r\n'
      else printf 'HTTP/1.1 401\r\nwww-authenticate: Bearer resource_metadata="x"\r\n'; fi
      exit 0
    fi ;;
  *cloudflare-dns.com*)
    if [[ -f "$FAKE/dns_bad" ]]; then echo '{}'; else echo '{"Answer":[{"type":1,"data":"203.0.113.7"}]}'; fi; exit 0 ;;
esac
printf 'UNEXPECTED curl %s\n' "$*" >> "$UNEXPECTED_LOG"
exit 99
STUB
chmod +x "$WORK/bin/docker" "$WORK/bin/curl"

# --- fake repo --------------------------------------------------------------------------------------------
cp "$SRC/scripts/lib.sh" "$SRC/scripts/healthcheck.sh" "$REPO/scripts/"
printf '[[clients]]\nclient_id = "local-test"\nredirect_uris = []\n' > "$REPO/gateway/config.toml"
printf 'k\n' > "$REPO/secrets/hevy_api_key"

CHAT_LINE="COACH_TELEGRAM_CHAT_ID=$CHAT"
write_env() {
  { echo 'PUBLIC_BASE_URL=https://fitness-mcp.example.ts.net'
    [[ -z "$CHAT_LINE" ]] || echo "$CHAT_LINE"
    for line in "$@"; do echo "$line"; done; } > "$REPO/.env"
}
write_token() { printf '%s' "$1" > "$REPO/secrets/telegram_bot_token"; }

# begin NAME: a fresh case with its own fake answers and state directory, default .env and token.
begin() {
  CASE="$WORK/cases/$1"
  FAKE="$CASE/fake"
  STATE="$CASE/state"
  mkdir -p "$FAKE" "$STATE"
  SF="$STATE/fitness-coach-mcp/healthcheck-notify.state"
  CHAT_LINE="COACH_TELEGRAM_CHAT_ID=$CHAT"
  write_env
  write_token "$TOKEN"$'\n'
}
fake_reset() {
  local f
  for f in down ts_bad funnel_off garmin_bad coach_db_bad scheduler history inbound meta_bad challenge_bad dns_bad \
           disc_bad authz_bad hevy_bad tg_code tg_reply health-ts health-gateway health-hevy-mcp \
           health-garmin-mcp health-coach-mcp ts_json direct_8080 direct_9002; do
    rm -f "$FAKE/$f"
  done
}
fake() { printf '%s\n' "${2:-x}" > "$FAKE/$1"; }
reqs() { find "$FAKE" -name 'sent.*.json' | wc -l; }

# run_hc ARGS...: one run of the script under a clean environment; sets RC, NREQ, LAST_JSON and the files
# $WORK/out and $WORK/err.
run_hc() {
  local before
  before="$(reqs)"
  RC=0
  env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$WORK/home" XDG_STATE_HOME="$STATE" LANG=C.UTF-8 TMPDIR="${RUN_TMPDIR:-$WORK/tmp}" \
    FAKE="$FAKE" ARGV_LOG="$ARGV_LOG" UNEXPECTED_LOG="$WORK/unexpected.log" \
    bash "$REPO/scripts/healthcheck.sh" "$@" > "$WORK/out" 2> "$WORK/err" < /dev/null || RC=$?
  NREQ=$(( $(reqs) - before ))
  cat "$WORK/out" >> "$WORK/all.out"
  cat "$WORK/err" >> "$WORK/all.err"
  LAST_JSON="$FAKE/sent.$(reqs).json"
}
sent_text() { jq -j .text "$LAST_JSON"; }
state_keys() { cut -f1 "$SF" | paste -sd, -; }
state_text() { grep -F "$1"$'\t' "$SF" | cut -f2; }
out_has() { grep -qxF -- "$1" "$WORK/out"; }
err_has() { grep -qxF -- "$1" "$WORK/err"; }
no_temp_left() { [[ -z "$(ls -A "$WORK/tmp")" ]]; }
lines_of() { wc -l < "$1" | tr -d ' '; }
nofail_msg() { [[ "$(grep -c 'Notify failed' "$WORK/err" || true)" == 0 ]]; }

notify_failed_with() {   # notify_failed_with REASON: stderr carries exactly that failure line
  err_has "Notify failed: $1. $TRY_AGAIN"
}

down_coach() { fake down coach-mcp; }

# --- H1: healthy first run sends nothing, but leaves an empty state -----------------------------------------
echo "[H1: healthy first run]"
begin h1
run_hc --quiet --notify
check "exit 0, stdout and stderr empty" '[[ $RC == 0 && ! -s "$WORK/out" && ! -s "$WORK/err" ]]'
check "no request" '[[ $NREQ == 0 ]]'
check "state file exists and is empty, mode 600" '[[ -f "$SF" && ! -s "$SF" && "$(stat -c %a "$SF")" == 600 ]]'
check "state directory mode 700" '[[ "$(stat -c %a "$STATE/fitness-coach-mcp")" == 700 ]]'
check "no temporary directory left" 'no_temp_left'

# --- H2..H8: one sequence on one state ----------------------------------------------------------------------
echo "[H2: a new problem set sends one message]"
begin seq
down_coach
run_hc --quiet --notify
check "exit 1, one request" '[[ $RC == 1 && $NREQ == 1 ]]'
check "text" '[[ "$(sent_text)" == "$WARN fitness-coach-mcp: 2 problems${NL}New:${NL}- $T_NOTRUN${NL}- $T_DB" ]]'
check "chat id is a JSON integer" 'grep -q "\"chat_id\": $CHAT, " "$LAST_JSON"'
check "stdout has both problem lines and the count" 'out_has "✘ $T_NOTRUN" && out_has "✘ $T_DB" && out_has "✘ Problems: 2"'
check "nothing else on stderr" '[[ ! -s "$WORK/err" ]]'
check "state keys" '[[ "$(state_keys)" == "container-coach-mcp,coach-db" ]]'

echo "[H3: the same set sends nothing]"
run_hc --quiet --notify
check "exit 1, no request" '[[ $RC == 1 && $NREQ == 0 ]]'

echo "[H4: another line under the same keys sends nothing]"
fake_reset
fake health-coach-mcp running/unhealthy
fake coach_db_bad
run_hc --quiet --notify
check "exit 1, no request" '[[ $RC == 1 && $NREQ == 0 ]]'
check "the stored text is the current one" '[[ "$(state_text container-coach-mcp)" == "coach-mcp: running/unhealthy" ]]'
check "keys unchanged" '[[ "$(state_keys)" == "container-coach-mcp,coach-db" ]]'

echo "[H5: a stopped scheduler is a problem of its own]"
fake_reset
fake scheduler stopped
run_hc --quiet --notify
check "exit 1, one request" '[[ $RC == 1 && $NREQ == 1 ]]'
check "text with New and Resolved, singular header" '[[ "$(sent_text)" == "$WARN fitness-coach-mcp: 1 problem${NL}New:${NL}- $T_SCHED${NL}Resolved:${NL}- coach-mcp: running/unhealthy${NL}- $T_DB" ]]'
check "state key" '[[ "$(state_keys)" == "coach-briefing" ]]'

echo "[H6: a stopped history is added]"
fake history stopped
run_hc --quiet --notify
check "exit 1, one request" '[[ $RC == 1 && $NREQ == 1 ]]'
check "text with New and Still" '[[ "$(sent_text)" == "$WARN fitness-coach-mcp: 2 problems${NL}New:${NL}- $T_HIST${NL}Still:${NL}- $T_SCHED" ]]'
check "state keys" '[[ "$(state_keys)" == "coach-briefing,coach-history" ]]'

echo "[H7: all checks pass again, once]"
fake_reset
run_hc --quiet --notify
check "exit 0, one request" '[[ $RC == 0 && $NREQ == 1 ]]'
check "recovery text" '[[ "$(sent_text)" == "$CHECK fitness-coach-mcp: all checks pass again${NL}Resolved:${NL}- $T_SCHED${NL}- $T_HIST" ]]'
check "state is empty" '[[ -f "$SF" && ! -s "$SF" ]]'

echo "[H8: healthy again sends nothing]"
run_hc --quiet --notify
check "exit 0, no request" '[[ $RC == 0 && $NREQ == 0 ]]'

# --- H9, H10: a message Telegram did not accept ---------------------------------------------------------------
echo "[H9: a rejected message leaves the state, the next run sends again]"
begin h9
down_coach
fake tg_code 400
printf '{"ok":false,"error_code":400}' > "$FAKE/tg_reply"
run_hc --quiet --notify
check "exit 1, one request" '[[ $RC == 1 && $NREQ == 1 ]]'
check "stderr: failure, problems" 'notify_failed_with "Telegram did not accept the message (HTTP 400)" && err_has "Current problems:" && err_has "  - $T_NOTRUN" && err_has "  - $T_DB"'
check "stdout still has the problems" 'out_has "✘ $T_NOTRUN"'
check "no state and no .new file" '[[ ! -e "$SF" && ! -e "$SF.new" ]]'
fake_reset
down_coach
run_hc --quiet --notify
check "the retry sends the same text" '[[ $RC == 1 && $NREQ == 1 && "$(sent_text)" == "$WARN fitness-coach-mcp: 2 problems${NL}New:${NL}- $T_NOTRUN${NL}- $T_DB" ]]'
check "state written" '[[ "$(state_keys)" == "container-coach-mcp,coach-db" ]]'
run_hc --quiet --notify
check "then nothing" '[[ $NREQ == 0 ]]'

echo "[H10: only HTTP 200 with ok true counts as sent]"
rows=("200|{\"ok\":false}|200" "200|{\"ok\":\"yes\"}|200" "200|not json|200" "000||000" "400|{\"ok\":true}|400")
i=0
for row in "${rows[@]}"; do
  i=$((i + 1))
  IFS='|' read -r code reply shown <<<"$row"
  begin "h10-$i"
  down_coach
  fake tg_code "$code"
  [[ -z "$reply" ]] || printf '%s' "$reply" > "$FAKE/tg_reply"
  run_hc --quiet --notify
  check "row $i ($code, ${reply:-no answer}): R6 with the code, exit 1, state untouched" \
    '[[ $RC == 1 && $NREQ == 1 ]] && notify_failed_with "Telegram did not accept the message (HTTP $shown)" && [[ ! -e "$SF" && ! -e "$SF.new" ]]'
done
begin h10-ok
down_coach
printf '{"ok":true,"result":{"message_id":9}}' > "$FAKE/tg_reply"
run_hc --quiet --notify
check "200 with ok true and more fields is accepted" '[[ $RC == 1 && $NREQ == 1 && -s "$SF" ]] && nofail_msg'

# --- H11..H16: configuration and failure paths -----------------------------------------------------------------
echo "[H11: validation runs although nothing would be sent]"
begin h11
CHAT_LINE=""; write_env
run_hc --quiet --notify
check "healthy, no chat id: exit 1, no request, R1 only" '[[ $RC == 1 && $NREQ == 0 && ! -s "$WORK/out" ]] && notify_failed_with "$R1" && [[ "$(lines_of "$WORK/err")" == 1 ]]'
check "no state directory created" '[[ ! -e "$STATE/fitness-coach-mcp" ]]'

echo "[H12: problems are repeated on stderr]"
begin h12
CHAT_LINE=""; write_env
down_coach
run_hc --quiet --notify
check "exit 1, no request" '[[ $RC == 1 && $NREQ == 0 ]]'
check "stdout has the problems" 'out_has "✘ $T_NOTRUN" && out_has "✘ $T_DB"'
check "stderr: R1, Current problems, both lines" '[[ "$(cat "$WORK/err")" == "Notify failed: $R1. $TRY_AGAIN${NL}Current problems:${NL}  - $T_NOTRUN${NL}  - $T_DB" ]]'

echo "[H13: chat id shapes, boundaries and order]"
chat_row() {   # chat_row NAME CHAT -> runs with coach down on a fresh state
  begin "$1"; CHAT_LINE="COACH_TELEGRAM_CHAT_ID=$2"; write_env; down_coach; run_hc --quiet --notify
}
chat_row h13-a 12ab
check "12ab: R2, no request" '[[ $RC == 1 && $NREQ == 0 ]] && notify_failed_with "$R2"'
chat_row h13-b 0123
check "0123: R2" '[[ $RC == 1 && $NREQ == 0 ]] && notify_failed_with "$R2"'
chat_row h13-c -0
check "-0: R2" '[[ $RC == 1 && $NREQ == 0 ]] && notify_failed_with "$R2"'
chat_row h13-d -1001234567890
check "a negative group id is sent as a JSON integer" '[[ $RC == 1 && $NREQ == 1 ]] && grep -q "\"chat_id\": -1001234567890, " "$LAST_JSON"'
chat_row h13-e 12345678901234567890
check "20 digits are accepted" '[[ $NREQ == 1 ]] && grep -q "\"chat_id\": 12345678901234567890, " "$LAST_JSON"'
chat_row h13-f -12345678901234567890
check "minus and 20 digits are accepted" '[[ $NREQ == 1 ]] && grep -q "\"chat_id\": -12345678901234567890, " "$LAST_JSON"'
chat_row h13-g 123456789012345678901
check "21 digits are rejected" '[[ $RC == 1 && $NREQ == 0 ]] && notify_failed_with "$R2"'
begin h13-h; CHAT_LINE="COACH_TELEGRAM_CHAT_ID=12ab"; write_env; rm -f "$REPO/secrets/telegram_bot_token"; down_coach
run_hc --quiet --notify
check "chat id is checked before the token file (R2, not R3)" '[[ $NREQ == 0 ]] && notify_failed_with "$R2"'

echo "[H14: token shapes, boundaries and order]"
token_row() {   # token_row NAME CONTENT
  begin "$1"; write_token "$2"; down_coach; run_hc --quiet --notify
}
begin h14-a; rm -f "$REPO/secrets/telegram_bot_token"; down_coach; run_hc --quiet --notify
check "missing file: R3" '[[ $RC == 1 && $NREQ == 0 ]] && notify_failed_with "$R3"'
if [[ "$(id -u)" == 0 ]]; then
  echo "  skip unreadable token file: running as root"
else
  begin h14-unreadable; down_coach; chmod 000 "$REPO/secrets/telegram_bot_token"
  run_hc --quiet --notify
  chmod 600 "$REPO/secrets/telegram_bot_token"
  check "unreadable token file: R3, the problems on stderr, no request, no raw shell error" \
    '[[ $RC == 1 && $NREQ == 0 ]] && notify_failed_with "$R3" && err_has "Current problems:" && err_has "  - $T_NOTRUN" && err_has "  - $T_DB" && ! grep -q "Permission denied" "$WORK/err" && out_has "✘ $T_NOTRUN"'
fi
token_row h14-b ''
check "empty file: R4" '[[ $RC == 1 && $NREQ == 0 ]] && notify_failed_with "$R4"'
token_row h14-c 'garbage-token'
check "garbage: R4" '[[ $RC == 1 && $NREQ == 0 ]] && notify_failed_with "$R4"'
token_row h14-d '123:short'
check "123:short: R4" '[[ $RC == 1 && $NREQ == 0 ]] && notify_failed_with "$R4"'
check "the token never appears in the output" '! grep -rqF -e garbage-token -e 123:short "$WORK/out" "$WORK/err"'
token_row h14-e "1:$(printf 'H%.0s' $(seq 1 30))"
check "30 characters after the colon are accepted" '[[ $NREQ == 1 ]] && nofail_msg'
token_row h14-f "1:$(printf 'H%.0s' $(seq 1 29))"
check "29 characters are rejected" '[[ $NREQ == 0 ]] && notify_failed_with "$R4"'
token_row h14-g "12345678901234567890:$(printf 'H%.0s' $(seq 1 64))"
check "20 digits and 64 characters are accepted" '[[ $NREQ == 1 ]] && nofail_msg'
token_row h14-h "12345678901234567890:$(printf 'H%.0s' $(seq 1 65))"
check "65 characters are rejected" '[[ $NREQ == 0 ]] && notify_failed_with "$R4"'
token_row h14-i "123456789012345678901:$(printf 'H%.0s' $(seq 1 40))"
check "21 digits are rejected" '[[ $NREQ == 0 ]] && notify_failed_with "$R4"'
begin h14-j; write_token "$TOKEN"$'\r\n'; down_coach; run_hc --quiet --notify
check "a CRLF line end is stripped" '[[ $NREQ == 1 ]] && nofail_msg'
begin h14-k; write_token 'garbage-token'; down_coach
mkdir -p "$STATE/fitness-coach-mcp"
exec 9>> "$STATE/fitness-coach-mcp/healthcheck-notify.lock"
flock -n 9
run_hc --quiet --notify
exec 9>&-
check "token is checked before the lock (R4, not R5)" '[[ $NREQ == 0 ]] && notify_failed_with "$R4"'

echo "[H15: a held lock]"
begin h15
down_coach
mkdir -p "$STATE/fitness-coach-mcp"
exec 9>> "$STATE/fitness-coach-mcp/healthcheck-notify.lock"
flock -n 9
run_hc --quiet --notify
check "busy: exit 1, R5, no request, state untouched" '[[ $RC == 1 && $NREQ == 0 && ! -e "$SF" ]] && notify_failed_with "$R5"'
exec 9>&-
run_hc --quiet --notify
check "free again: one request" '[[ $NREQ == 1 ]] && nofail_msg'

echo "[H16: the state directory cannot be written]"
if [[ "$(id -u)" == 0 ]]; then
  echo "  skip running as root"
else
  begin h16a
  down_coach
  chmod 500 "$STATE"
  run_hc --quiet --notify
  chmod 700 "$STATE"
  check "parent not writable: R7, no request, nothing else on the failure line" \
    '[[ $RC == 1 && $NREQ == 0 ]] && notify_failed_with "cannot write the state file in $STATE/fitness-coach-mcp" && ! grep -q "Permission denied" "$WORK/err"'
  begin h16b
  down_coach
  mkdir -p "$STATE/fitness-coach-mcp"
  chmod 500 "$STATE/fitness-coach-mcp"
  run_hc --quiet --notify
  chmod 700 "$STATE/fitness-coach-mcp"
  check "lock file not creatable: R7, no request, stderr only the failure and the problems" \
    '[[ $RC == 1 && $NREQ == 0 ]] && notify_failed_with "cannot write the state file in $STATE/fitness-coach-mcp" && ! grep -q "Permission denied" "$WORK/err" && [[ "$(lines_of "$WORK/err")" == 4 ]]'
fi

# --- H17, H18: without --notify nothing changes ---------------------------------------------------------------
echo "[H17: no --notify, no Telegram, no state]"
begin h17
down_coach
run_hc --quiet --notify
cp "$SF" "$WORK/h17.state"
rm -f "$FAKE/tg_config.log"
run_hc --quiet
check "problems: exit 1, no request, state byte-identical" '[[ $RC == 1 && $NREQ == 0 ]] && cmp -s "$SF" "$WORK/h17.state"'
check "no Telegram call, nothing about notify in the output" '[[ ! -e "$FAKE/tg_config.log" ]] && ! grep -qi notify "$WORK/out" "$WORK/err"'
begin h17b
CHAT_LINE=""; write_env
run_hc --quiet
check "healthy without a chat id and without --notify: exit 0, silent, no state directory" \
  '[[ $RC == 0 && ! -s "$WORK/out" && ! -s "$WORK/err" && ! -e "$STATE/fitness-coach-mcp" ]]'
run_hc
check "full report mentions no notify" '[[ $RC == 0 ]] && out_has "✔ All good." && ! grep -qi notify "$WORK/out"'

echo "[H18: bad arguments]"
begin h18
for args in "--bogus" "--quiet --bogus" "-h" "--notify extra"; do
  # shellcheck disable=SC2086
  run_hc $args
  check "'$args': exit 2, usage on stderr, no docker call" \
    '[[ $RC == 2 && $NREQ == 0 && ! -s "$WORK/out" && ! -s "$FAKE/docker.log" ]] && grep -q "scripts/healthcheck.sh --quiet --notify" "$WORK/err" && [[ "$(lines_of "$WORK/err")" == 6 ]]'
done

# --- H19: backup age -------------------------------------------------------------------------------------------
echo "[H19: backup age]"
bname() { printf 'fitness-coach-backup-%s.tgz' "$(date -u -d "$1" +%Y%m%dT%H%M%SZ)"; }
mk() { : > "$BDIR/$(bname "$1")"; }

begin h19-r1; run_hc --quiet
check "r1 no BACKUP_DIR: exit 0, no backup problem" '[[ $RC == 0 ]] && ! grep -q "backup" "$WORK/out"'
run_hc
check "r1 the full report says it is not checked" 'out_has "✔ backup: BACKUP_DIR not set, backup age not checked"'

begin h19-r2; write_env BACKUP_DIR=backups; run_hc --quiet
check "r2 relative BACKUP_DIR" '[[ $RC == 1 ]] && out_has "✘ backup: BACKUP_DIR is not an absolute path to a readable directory → docs/operations.md, Backups"'
begin h19-r3; write_env "BACKUP_DIR=$WORK/no-such-dir"; run_hc --quiet
check "r3 missing directory" '[[ $RC == 1 ]] && out_has "✘ backup: BACKUP_DIR is not an absolute path to a readable directory → docs/operations.md, Backups"'
begin h19-r3b; : > "$CASE/a-file"; write_env "BACKUP_DIR=$CASE/a-file"; run_hc --quiet
check "r3b BACKUP_DIR is a file" '[[ $RC == 1 ]] && out_has "✘ backup: BACKUP_DIR is not an absolute path to a readable directory → docs/operations.md, Backups"'

# Each guard of the directory test decides alone: an existing relative directory (the script runs in the repo
# root), an existing directory without read permission, and one without search permission.
begin h19-r3c; mkdir -p "$REPO/backups"; : > "$REPO/backups/$(date -u +fitness-coach-backup-%Y%m%dT%H%M%SZ.tgz)"
write_env BACKUP_DIR=backups; run_hc --quiet
rm -rf "$REPO/backups"
check "r3c an existing relative directory is refused" '[[ $RC == 1 ]] && out_has "✘ backup: BACKUP_DIR is not an absolute path to a readable directory → docs/operations.md, Backups"'
if [[ "$(id -u)" == 0 ]]; then
  echo "  skip r3d/r3e directory modes: running as root"
else
  for mode in 300 400; do
    begin "h19-mode-$mode"; BDIR="$CASE/backups"; mkdir -p "$BDIR"; mk '-3 hours'; chmod "$mode" "$BDIR"
    write_env "BACKUP_DIR=$BDIR"; run_hc --quiet
    chmod 700 "$BDIR"
    if [[ $mode == 300 ]]; then
      check "r3d directory without read permission (mode 300) is refused" '[[ $RC == 1 ]] && out_has "✘ backup: BACKUP_DIR is not an absolute path to a readable directory → docs/operations.md, Backups"'
    else
      check "r3e directory without search permission (mode 400) is refused" '[[ $RC == 1 ]] && out_has "✘ backup: BACKUP_DIR is not an absolute path to a readable directory → docs/operations.md, Backups"'
    fi
  done
fi

begin h19-r4; BDIR="$CASE/backups"; mkdir -p "$BDIR"; write_env "BACKUP_DIR=$BDIR"; run_hc --quiet
check "r4 empty directory" '[[ $RC == 1 ]] && out_has "✘ backup: no archive in BACKUP_DIR yet → scripts/backup.sh"'

begin h19-r5; BDIR="$CASE/backups"; mkdir -p "$BDIR"; mk '-3 hours'; write_env "BACKUP_DIR=$BDIR"; run_hc
check "r5 archive 3 h old: exit 0, ok line" '[[ $RC == 0 ]] && out_has "✔ backup: newest archive 3 h old"'

begin h19-r6; BDIR="$CASE/backups"; mkdir -p "$BDIR"; mk '-27 hours'; write_env "BACKUP_DIR=$BDIR"; run_hc --quiet
check "r6 27 h old" '[[ $RC == 1 ]] && out_has "✘ backup: newest archive is 27 h old (limit 26 h) → scripts/backup.sh, docs/operations.md Troubleshooting"'
begin h19-r7; BDIR="$CASE/backups"; mkdir -p "$BDIR"; mk '-25 hours -58 minutes'; write_env "BACKUP_DIR=$BDIR"; run_hc --quiet
check "r7 25 h 58 min is inside the limit" '[[ $RC == 0 ]]'
begin h19-r8; BDIR="$CASE/backups"; mkdir -p "$BDIR"; mk '-26 hours -30 minutes'; write_env "BACKUP_DIR=$BDIR"; run_hc --quiet
check "r8 26 h 30 min is over the limit (seconds, not whole hours)" '[[ $RC == 1 ]] && out_has "✘ backup: newest archive is 26 h old (limit 26 h) → scripts/backup.sh, docs/operations.md Troubleshooting"'
begin h19-r9; BDIR="$CASE/backups"; mkdir -p "$BDIR"; mk '-3 hours'; write_env "BACKUP_DIR=$BDIR" BACKUP_MAX_AGE_HOURS=2; run_hc --quiet
check "r9 BACKUP_MAX_AGE_HOURS=2" '[[ $RC == 1 ]] && out_has "✘ backup: newest archive is 3 h old (limit 2 h) → scripts/backup.sh, docs/operations.md Troubleshooting"'
begin h19-r9b; BDIR="$CASE/backups"; mkdir -p "$BDIR"; mk '-3 hours'; write_env "BACKUP_DIR=$BDIR" BACKUP_MAX_AGE_HOURS=1; run_hc --quiet
check "r9b BACKUP_MAX_AGE_HOURS=1 is accepted" '[[ $RC == 1 ]] && out_has "✘ backup: newest archive is 3 h old (limit 1 h) → scripts/backup.sh, docs/operations.md Troubleshooting"'
begin h19-r10; BDIR="$CASE/backups"; mkdir -p "$BDIR"; mk '-50 hours'; mk '-1 hour'; write_env "BACKUP_DIR=$BDIR"; run_hc --quiet
check "r10 the newest of two archives counts" '[[ $RC == 0 ]]'
begin h19-r11; BDIR="$CASE/backups"; mkdir -p "$BDIR"; mk '-50 hours'
: > "$BDIR/fitness-coach-backup-latest.tgz"; mkdir "$BDIR/.backup-tmp.x"; : > "$BDIR/notes.txt"
mkdir "$BDIR/$(bname '-1 hour')"
write_env "BACKUP_DIR=$BDIR"; run_hc --quiet
check "r11 foreign names and a directory with an archive name are ignored" '[[ $RC == 1 ]] && out_has "✘ backup: newest archive is 50 h old (limit 26 h) → scripts/backup.sh, docs/operations.md Troubleshooting"'
begin h19-r12; BDIR="$CASE/backups"; mkdir -p "$BDIR"; : > "$BDIR/fitness-coach-backup-20261399T000000Z.tgz"; write_env "BACKUP_DIR=$BDIR"; run_hc --quiet
check "r12 impossible date in the name" '[[ $RC == 1 ]] && out_has "✘ backup: cannot read the time of the newest archive fitness-coach-backup-20261399T000000Z.tgz"'
begin h19-r13; BDIR="$CASE/backups"; mkdir -p "$BDIR"; mk '+3 hours'; write_env "BACKUP_DIR=$BDIR"; run_hc --quiet
check "r13 archive 3 h in the future" '[[ $RC == 1 ]] && grep -q "^✘ backup: newest archive fitness-coach-backup-.*\.tgz lies in the future → check the clock and BACKUP_DIR$" "$WORK/out"'
begin h19-r13b; BDIR="$CASE/backups"; mkdir -p "$BDIR"; mk '+90 minutes'; write_env "BACKUP_DIR=$BDIR"; run_hc --quiet
check "r13b archive 90 min ahead is already in the future" '[[ $RC == 1 ]] && grep -q "lies in the future" "$WORK/out"'
begin h19-r14b; BDIR="$CASE/backups"; mkdir -p "$BDIR"; mk '+59 minutes'; write_env "BACKUP_DIR=$BDIR"; run_hc --quiet
check "r14b archive 59 min ahead is fine" '[[ $RC == 0 ]]'
begin h19-r14; BDIR="$CASE/backups"; mkdir -p "$BDIR"; mk '+30 minutes'; write_env "BACKUP_DIR=$BDIR"; run_hc --quiet
check "r14 archive 30 min ahead (clock skew) is fine" '[[ $RC == 0 ]]'
SETTING='✘ backup: BACKUP_MAX_AGE_HOURS must be a whole number of hours from 1 to 999'
for v in 0 abc 1000 -5 05; do
  begin "h19-r15-$v"; BDIR="$CASE/backups"; mkdir -p "$BDIR"; mk '-3 hours'; write_env "BACKUP_DIR=$BDIR" "BACKUP_MAX_AGE_HOURS=$v"; run_hc --quiet
  check "r15 BACKUP_MAX_AGE_HOURS=$v is refused" '[[ $RC == 1 ]] && out_has "$SETTING"'
done
begin h19-r15-999; BDIR="$CASE/backups"; mkdir -p "$BDIR"; mk '-3 hours'; write_env "BACKUP_DIR=$BDIR" BACKUP_MAX_AGE_HOURS=999; run_hc --quiet
check "r15 999 is accepted" '[[ $RC == 0 ]]'
begin h19-r16; write_env BACKUP_DIR=backups BACKUP_MAX_AGE_HOURS=abc; run_hc --quiet
check "r16 the setting is checked before the directory" '[[ $RC == 1 ]] && out_has "$SETTING" && ! grep -q "not an absolute path" "$WORK/out"'

# --- H20: the size guard, and a hand-edited state --------------------------------------------------------------
echo "[H20: size guard]"
begin h20
mkdir -p "$STATE/fitness-coach-mcp"
python3 -c "
import sys
sys.stdout.write(''.join('k%d\t%s\n' % (i, 'x' * 90) for i in range(80)))" > "$SF"
run_hc --quiet --notify
check "healthy: one request, first line is the recovery header" '[[ $RC == 0 && $NREQ == 1 ]] && [[ "$(sent_text | head -n1)" == "$CHECK fitness-coach-mcp: all checks pass again" ]]'
check "at most 4000 UTF-8 bytes" '[[ "$(sent_text | wc -c | tr -d " ")" -le 4000 ]]'
check "last line counts the dropped lines" '[[ "$(sent_text | tail -n1)" =~ ^"$ELLIPSIS "[0-9]+" more lines"$ && "$(sent_text | tail -n1)" != "$ELLIPSIS 0 more lines" ]]'
check "the exact cut" '[[ "$(sent_text | tail -n1)" == "$ELLIPSIS 38 more lines" ]]'
check "state is empty" '[[ ! -s "$SF" ]]'

echo "[H20b: a hand-edited state]"
begin h20b
mkdir -p "$STATE/fitness-coach-mcp"
printf 'badline\n\tnokey\nA\tfirst A\nA\tsecond A\nB\tbee\n' > "$SF"
run_hc --quiet --notify
check "lines without a key are ignored, the first of a key wins" \
  '[[ "$(sent_text)" == "$CHECK fitness-coach-mcp: all checks pass again${NL}Resolved:${NL}- first A${NL}- bee" ]]'

echo "[H20c: a state file that cannot be read]"
begin h20c
mkdir -p "$STATE/fitness-coach-mcp"
printf 'K\tcaf\xe9 broken\n' > "$SF"
run_hc --quiet --notify
check "invalid UTF-8 in the state is tolerated: the old problem is resolved" \
  '[[ $RC == 0 && $NREQ == 1 ]] && [[ "$(sent_text | head -n1)" == "$CHECK fitness-coach-mcp: all checks pass again" ]] && [[ ! -s "$SF" ]]'
begin h20d
down_coach
mkdir -p "$SF"
run_hc --quiet --notify
check "a composer failure is R8, with the problems on stderr and nothing sent" \
  '[[ $RC == 1 && $NREQ == 0 && -d "$SF" && ! -e "$SF.new" ]] && notify_failed_with "the message could not be composed (python3 failed)" && err_has "  - $T_NOTRUN"'

echo "[H20e: no temporary directory]"
begin h20e
down_coach
RUN_TMPDIR="$WORK/no-such-tmp" run_hc --quiet --notify
check "mktemp fails: failure line, the problems on stderr, no request, no raw error" \
  '[[ $RC == 1 && $NREQ == 0 ]] && notify_failed_with "cannot create a temporary directory" && err_has "  - $T_NOTRUN" && err_has "  - $T_DB" && [[ "$(lines_of "$WORK/err")" == 4 ]]'

# --- H24: a tab or a newline in a problem line cannot break the state file -------------------------------------
echo "[H24: tabs and newlines in a problem line]"
begin h24
printf '{"BackendState":"a\\tb\\nc"}\n' > "$FAKE/ts_json"
run_hc --quiet --notify
check "one state line with the whitespace replaced by spaces" \
  '[[ $RC == 1 && $NREQ == 1 && "$(lines_of "$SF")" == 1 && "$(state_keys)" == "tailscale-node" ]] && [[ "$(state_text tailscale-node)" == "tailscale: state '"'"'a b c'"'"', DNS '"'"'?'"'"' (expected fitness-mcp.example.ts.net)" ]]'
run_hc --quiet --notify
check "and the next run sees the same set" '[[ $NREQ == 0 ]]'

echo "[H25: the notify lines of a full report]"
begin h25
down_coach
run_hc --notify
check "a sent message is announced" '[[ $RC == 1 && $NREQ == 1 ]] && out_has "✔ notify: Telegram message sent"'
run_hc --notify
check "an unchanged set is announced" '[[ $RC == 1 && $NREQ == 0 ]] && out_has "✔ notify: no change since the last run, nothing sent"'

echo "[H26: a kept key shows its current text, blocks in the order New, Still, Resolved]"
begin h26
fake health-coach-mcp running/unhealthy
fake coach_db_bad
run_hc --quiet --notify
fake_reset
fake health-coach-mcp running/starting
fake scheduler stopped
run_hc --quiet --notify
check "text" '[[ $RC == 1 && $NREQ == 1 && "$(sent_text)" == "$WARN fitness-coach-mcp: 2 problems${NL}New:${NL}- $T_SCHED${NL}Still:${NL}- coach-mcp: running/starting${NL}Resolved:${NL}- $T_DB" ]]'

echo "[H27: two-way Telegram: stopped is a problem, on, off and an older image are not]"
begin h27
fake inbound stopped
run_hc --notify
check "stopped: exit 1, one request, the line and the key" '[[ $RC == 1 && $NREQ == 1 ]] && out_has "✘ $T_INB" && [[ "$(state_keys)" == "coach-inbound" ]]'
check "stopped: the message names it" '[[ "$(sent_text)" == "$WARN fitness-coach-mcp: 1 problem${NL}New:${NL}- $T_INB" ]]'
fake inbound on
run_hc --notify
check "on: exit 0, the recovery message, the ok line" '[[ $RC == 0 && $NREQ == 1 ]] && out_has "✔ coach: two-way Telegram on"'
fake inbound off
run_hc
check "off: exit 0 and its own ok line" '[[ $RC == 0 ]] && out_has "✔ coach: two-way Telegram off (COACH_TELEGRAM_INBOUND, or no private chat id in .env)"'
fake inbound '?'
run_hc
check "an image without the field: exit 0 and a warning" '[[ $RC == 0 ]] && out_has "! coach: two-way Telegram state unknown"'

# --- H23: every call site has its own key ----------------------------------------------------------------------
echo "[H23: keys]"
begin h23a
BDIR="$CASE/backups"; mkdir -p "$BDIR"; write_env "BACKUP_DIR=$BDIR"
fake ts_bad; fake funnel_off; fake meta_bad; fake challenge_bad; fake dns_bad; fake garmin_bad; fake coach_db_bad
fake scheduler stopped; fake history stopped; fake inbound stopped; fake hevy_bad; fake down gateway
fake health-garmin-mcp running/unhealthy
run_hc --quiet --notify
nprob="$(grep '^✘ ' "$WORK/out" | grep -vc '^✘ Problems:' || true)"
check "everything broken: exit 1, Problems: 16" '[[ $RC == 1 ]] && out_has "✘ Problems: 16"'
check "problem lines = state lines = distinct keys" '[[ "$nprob" == 16 && "$(lines_of "$SF")" == 16 && "$(cut -f1 "$SF" | sort -u | wc -l | tr -d " ")" == 16 ]]'
check "no key error" '! grep -q "needs a problem key" "$WORK/err" "$WORK/out"'
check "the backup key is among them and was sent" 'grep -q "^backup	" "$SF" && grep -q "backup: no archive in BACKUP_DIR yet" "$LAST_JSON"'
check "all keys" '[[ "$(state_keys)" == "container-gateway,container-garmin-mcp,tailscale-node,funnel-config,tailnet-metadata,tailnet-mcp-hevy,tailnet-mcp-garmin,tailnet-mcp-coach,funnel-dns,garmin-tokens,coach-db,coach-briefing,coach-history,coach-inbound,hevy-key,backup" ]]'
begin h23b
fake disc_bad; fake authz_bad
run_hc --quiet --notify
check "public path keys" '[[ $RC == 1 && "$(state_keys)" == "funnel-discovery,funnel-login" ]]'
check "exactly one CIMD call site (static)" '[[ "$(grep -c "report_fail \"cimd:" "$SRC/scripts/healthcheck.sh")" == 1 ]]'
check "no call passes its text as the key (static)" '! grep -qE "report_fail \"[^\"]* " "$SRC/scripts/healthcheck.sh"'

# --- H28: ports of the node outside tailscale serve ----------------------------------------------------------
echo "[H28: ports of the node outside tailscale serve]"
begin h28a
run_hc
check "(a) refused on both ports: exit 0 and both ok lines" '[[ $RC == 0 ]] && out_has "✔ tailnet: port 8080 of the node does not accept connections (refused)" && out_has "✔ tailnet: port 9002 of the node does not accept connections (refused)"'
check "(a) the probes went to the node address on both ports" 'grep -qF "http://100.64.0.1:8080/" "$FAKE/curl.log" && grep -qF "http://100.64.0.1:9002/" "$FAKE/curl.log"'
begin h28b
fake direct_9002 blocked
run_hc
check "(b) a blocked probe (policy drop) is ok, with its own wording" '[[ $RC == 0 ]] && out_has "✔ tailnet: port 9002 of the node does not accept connections (no answer)" && out_has "✔ tailnet: port 8080 of the node does not accept connections (refused)"'
begin h28c
fake direct_8080 accept
run_hc --quiet --notify
check "(c) an accepting port 8080 is a failure with its own key" '[[ $RC == 1 && "$(state_keys)" == "tailnet-direct-8080" ]] && out_has "✘ tailnet: port 8080 of the node accepts connections that bypass tailscale serve → scripts/check-ingress.py; README, Security model"'
check "(c) the node address is not in the output or the message" '! grep -q "100\.64\.0\.1" "$WORK/out" "$WORK/err" && [[ "$(sent_text)" != *100.64.0.1* ]]'
begin h28d
fake direct_9002 silent
run_hc --quiet --notify
check "(d) a connect that never answers is a failure, port 9002 key" '[[ $RC == 1 && "$(state_keys)" == "tailnet-direct-9002" ]]'
begin h28e
fake direct_8080 odd
run_hc
check "(e) any other curl exit is a warning, not a failure" '[[ $RC == 0 ]] && out_has "! tailnet: probe of port 8080 inconclusive (curl exit 6)"'
begin h28f
printf '%s\n' '{"BackendState":"Running","Self":{"DNSName":"fitness-mcp.example.ts.net.","TailscaleIPs":["fd7a:115c:a1e0::1"]}}' > "$FAKE/ts_json"
run_hc
check "(f) no IPv4 address: a warning and no probe" '[[ $RC == 0 ]] && out_has "! tailnet: no IPv4 address of the node, port probe skipped" && ! grep -q ":8080/" "$FAKE/curl.log"'
begin h28g
printf '%s\n' '{"BackendState":"Stopped","Self":{"DNSName":"fitness-mcp.example.ts.net.","TailscaleIPs":["100.64.0.1"]}}' > "$FAKE/ts_json"
run_hc
check "(g) a node that is not running is not probed" '! grep -q ":8080/" "$FAKE/curl.log" && ! grep -q ":9002/" "$FAKE/curl.log" && ! grep -q "no IPv4 address" "$WORK/out"'

# --- H21, H22: secrets in no output, no unexpected call -----------------------------------------------------
echo "[H21: no token and no chat id on any command line or in any output]"
logs=("$WORK/all.out" "$WORK/all.err" "$ARGV_LOG")
while IFS= read -r f; do logs+=("$f"); done < <(find "$WORK/cases" \( -name docker.log -o -name curl.log \))
leak=0
for needle in "$TOKEN" AAharness-token garbage-token HHHHHHHHHHHHHHHH "$CHAT" 1001234567890 12345678901234567890; do
  if grep -qF -- "$needle" "${logs[@]}"; then echo "  leak: $needle"; leak=1; fi
done
check "token and chat ids absent from stdout, stderr, docker.log, curl.log and the argv log" '[[ $leak == 0 ]]'
check "positive: python3 and flock were run (argv shims work)" 'grep -q "^python3 " "$ARGV_LOG" && grep -q "^flock " "$ARGV_LOG"'
check "positive: curl was called with --data-binary" 'grep -rq --include=curl.log -e "--data-binary" "$WORK/cases"'
check "positive: the token reached curl on stdin only" 'grep -rqF --include=tg_config.log -e "$TOKEN" "$WORK/cases"'
check "positive: the chat id is in the request body" 'grep -q "\"chat_id\": $CHAT, " "$WORK/cases/seq/fake/sent.1.json"'
check "no temporary directory left by any run" 'no_temp_left'

echo "[H22: no unexpected docker or curl call]"
check "unexpected.log absent" '[[ ! -e "$WORK/unexpected.log" ]]'

echo
if [[ $FAILED == 0 ]]; then echo "HEALTHCHECK OK"; else echo "HEALTHCHECK FAILED"; exit 1; fi
