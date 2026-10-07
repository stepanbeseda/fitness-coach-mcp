#!/usr/bin/env bash
# Create or rotate secrets in ./secrets (Docker secrets for the compose stack).
#
#   scripts/setup-secrets.sh                 create everything that is missing
#   scripts/setup-secrets.sh hevy-key        rotate the Hevy API key
#   scripts/setup-secrets.sh ts-authkey      set the Tailscale auth key (first start only)
#   scripts/setup-secrets.sh password        change the gateway login password
#   scripts/setup-secrets.sh totp            enrol a new TOTP authenticator
#   scripts/setup-secrets.sh backend-tokens  rotate the gateway <-> backend secrets (hevy, garmin, coach)
#   scripts/setup-secrets.sh telegram-token  set the Telegram bot token (morning briefing), show the chat id
#   scripts/setup-secrets.sh telegram-chat-id  show the chat id of your chat with the bot again
#   scripts/setup-secrets.sh fix-perms       re-apply ownership/permissions
#
# Secrets are typed with hidden input and written directly to files (mode 0640,
# group APP_GID so the unprivileged containers can read them). Nothing is echoed,
# logged or passed on a command line.
source "$(dirname "$0")/lib.sh"

GATEWAY_IMAGE="fitness-coach-mcp/gateway:0.1.0"

ensure_dir() {
  if [[ ! -d "$SECRETS_DIR" ]]; then
    (umask 077 && mkdir -p "$SECRETS_DIR")
  fi
  chmod 0700 "$SECRETS_DIR"
}

set_perms() {
  local file="$1"
  chmod 0640 "$file"
  if [[ "$(stat -c %g "$file")" != "$APP_GID" ]]; then
    sudo chgrp "$APP_GID" "$file" || die "Nepodařilo se nastavit skupinu $APP_GID na $file (potřebuje sudo)."
  fi
}

write_secret() {
  local name="$1" value="$2" file="$SECRETS_DIR/$1"
  ensure_dir
  # Write in place (same inode) so running containers never see a half-written file.
  (umask 077 && printf '%s\n' "$value" > "$file")
  set_perms "$file"
  ok "Uloženo: secrets/$name"
}

has_secret() { [[ -s "$SECRETS_DIR/$1" ]]; }

read_hidden() {
  local prompt="$1" value
  IFS= read -rsp "$prompt" value </dev/tty
  printf '\n' >&2
  printf '%s' "$value"
}

ensure_gateway_image() {
  if ! docker image inspect "$GATEWAY_IMAGE" >/dev/null 2>&1; then
    info "Builduju image gateway (poprvé to chvíli trvá)..."
    dc build gateway >&2
  fi
}

recreate() {
  # Secret files are bind-mounted; recreate running services so they pick up new values.
  local running
  running="$(dc ps --services --status running 2>/dev/null || true)"
  local svc
  for svc in "$@"; do
    if grep -qx "$svc" <<<"$running"; then
      dc up -d --force-recreate --no-deps "$svc" >&2
      ok "Restartováno: $svc"
    fi
  done
}

cmd_hevy_key() {
  local key code
  info "Hevy API key: https://hevy.com/settings?developer (web Hevy, vyžaduje PRO)."
  key="$(read_hidden 'Hevy API key: ')"
  [[ -n "$key" ]] || die "Prázdný klíč."
  code="$(printf 'api-key: %s\n' "$key" | curl -sS -o /dev/null -w '%{http_code}' -H @- \
          https://api.hevyapp.com/v1/user/info || true)"
  case "$code" in
    200) ok "Hevy API klíč ověřen." ;;
    401|403) die "Hevy klíč odmítnut (HTTP $code). Nic jsem neuložil." ;;
    *) warn "Klíč se nepodařilo ověřit (HTTP ${code:-?}); ukládám i tak." ;;
  esac
  write_secret hevy_api_key "$key"
  recreate hevy-mcp
}

cmd_ts_authkey() {
  local key
  info "Auth key: Tailscale admin → Settings → Keys → Generate auth key"
  info "  (Reusable: ne, Ephemeral: ne, Pre-approved: ano, Tags: ${TS_TAG:-tag:fitness-mcp})."
  key="$(read_hidden 'Tailscale auth key (tskey-auth-...): ')"
  [[ "$key" == tskey-* ]] || die "Tohle nevypadá jako Tailscale auth key."
  write_secret ts_authkey "$key"
  info "Klíč je potřeba jen pro první přihlášení uzlu; stav se pak drží ve volume ts-state."
}

cmd_password() {
  local pw pw2 hash
  ensure_gateway_image
  info "Heslo pro přihlášení do gateway (aspoň 12 znaků, ideálně passphrase ze 4+ slov)."
  pw="$(read_hidden 'Nové heslo: ')"
  pw2="$(read_hidden 'Heslo znovu: ')"
  [[ "$pw" == "$pw2" ]] || die "Hesla se neshodují."
  hash="$(printf '%s\n' "$pw" | docker run --rm -i --network none --entrypoint python "$GATEWAY_IMAGE" \
          -m gateway.cli hash-password --stdin)" || die "Hash hesla se nepodařil."
  unset pw pw2
  write_secret gateway_password_hash "$hash"
  recreate gateway
}

cmd_totp() {
  local secret code attempt
  ensure_gateway_image
  secret="$(docker run --rm -i --network none --entrypoint python "$GATEWAY_IMAGE" \
            -m gateway.cli new-totp --account "${OWNER_TAILSCALE_LOGIN:-owner}")" || die "Generování TOTP selhalo."
  for attempt in 1 2 3; do
    IFS= read -rp "Opiš aktuální 6místný kód z autentizátoru: " code </dev/tty
    if printf '%s\n%s\n' "$secret" "$code" | docker run --rm -i --network none --entrypoint python \
         "$GATEWAY_IMAGE" -m gateway.cli verify-totp; then
      write_secret gateway_totp_secret "$secret"
      recreate gateway
      return 0
    fi
    warn "Kód nesedí (pokus $attempt/3). Zkontroluj čas v telefonu."
  done
  die "TOTP neověřeno, nic jsem neuložil."
}

new_backend_token() {
  local value
  value="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
    || die "Generování interního tokenu $1 selhalo, nic jsem neuložil."
  [[ ${#value} -ge 32 ]] || die "Generování interního tokenu $1 selhalo, nic jsem neuložil."
  write_secret "$1" "$value"
}

cmd_backend_tokens() {
  new_backend_token hevy_backend_token
  new_backend_token garmin_backend_token
  new_backend_token coach_backend_token
  recreate hevy-mcp garmin-mcp coach-mcp gateway
}

# Create only the backend tokens that are missing (an existing install gains the coach
# token without rotating the others) and recreate just those backends plus the gateway.
ensure_backend_tokens() {
  local b created=()
  for b in hevy garmin coach; do
    if ! has_secret "${b}_backend_token"; then
      new_backend_token "${b}_backend_token"
      created+=("${b}-mcp")
    fi
  done
  if (( ${#created[@]} > 0 )); then
    recreate "${created[@]}" gateway
  fi
  return 0
}

# Bot tokens look like 123456789:AA... (digits, a colon, 30+ characters); coach-mcp checks the same shape.
TELEGRAM_TOKEN_RE='^[0-9]{1,20}:[A-Za-z0-9_-]{30,64}$'

# Empty placeholder so docker compose can mount the secret while the morning briefing is off
# (coach-mcp sends nothing while COACH_TELEGRAM_CHAT_ID is empty).
ensure_telegram_token() {
  local file="$SECRETS_DIR/telegram_bot_token"
  if [[ ! -e "$file" ]]; then
    ensure_dir
    (umask 077 && : > "$file")
    set_perms "$file"
    ok "Vytvořeno: secrets/telegram_bot_token (prázdný, ranní briefing vypnutý)"
  fi
  return 0
}

# Calls a Bot API method and prints the HTTP status (000 = no answer). The token reaches curl
# through a config file on stdin, so it is never on a command line; the reply body goes to $3.
telegram_api() {
  printf 'url = "https://api.telegram.org/bot%s/%s"\n' "$1" "$2" \
    | curl -s --max-time 15 -o "$3" -w '%{http_code}' -K - || true
}

wait_enter() { IFS= read -r _ </dev/tty; }

# Lists the private chats that recently wrote to the bot (getUpdates keeps them for 24 hours).
show_telegram_chat_ids() {
  local body code ids
  body="$(mktemp)"
  info "Pošli teď botovi v Telegramu libovolnou zprávu (třeba /start) a pak tady stiskni Enter."
  wait_enter
  code="$(telegram_api "$1" getUpdates "$body")"
  ids=""
  if [[ "$code" == 200 ]]; then
    ids="$(python3 - "$body" <<'PY' || true
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        updates = json.load(fh).get("result") or []
except (OSError, ValueError, AttributeError):
    updates = []
seen = {}
for update in updates if isinstance(updates, list) else []:
    for key in ("message", "edited_message", "my_chat_member"):
        chat = (update.get(key) or {}).get("chat") or {} if isinstance(update, dict) else {}
        if chat.get("type") == "private" and isinstance(chat.get("id"), int):
            name = str(chat.get("first_name") or "")
            # Anyone can write to the bot: keep only printable characters (no terminal escapes), at most 40.
            seen.setdefault(chat["id"], "".join(c for c in name if c.isprintable())[:40])
for chat_id, name in seen.items():
    print(f"{chat_id} ({name})" if name else chat_id)
PY
)"
  fi
  rm -f "$body"
  if [[ -z "$ids" ]]; then
    warn "Žádný soukromý chat s botem jsem nenašel (HTTP $code). Napiš botovi a spusť scripts/setup-secrets.sh telegram-chat-id."
    return 0
  fi
  ok "Soukromé chaty s botem (chat id a jméno):"
  printf '%s\n' "$ids" | sed 's/^/  /'
  info "Do .env přidej řádek COACH_TELEGRAM_CHAT_ID= s číslem svého chatu a spusť docker compose up -d coach-mcp."
}

cmd_telegram_token() {
  local token code
  info "Token bota od @BotFather v Telegramu (/newbot), tvar 123456789:AA..."
  token="$(read_hidden 'Telegram bot token: ')"
  [[ "$token" =~ $TELEGRAM_TOKEN_RE ]] \
    || die "Tohle nevypadá jako token Telegram bota (číslo, dvojtečka, znaky). Nic jsem neuložil."
  code="$(telegram_api "$token" getMe /dev/null)"
  case "$code" in
    200) ok "Token bota ověřen." ;;
    401|404) die "Telegram token odmítl (HTTP $code). Nic jsem neuložil." ;;
    *) warn "Token se nepodařilo ověřit (HTTP $code); ukládám i tak." ;;
  esac
  write_secret telegram_bot_token "$token"
  show_telegram_chat_ids "$token"
  recreate coach-mcp
}

cmd_telegram_chat_id() {
  local token
  has_secret telegram_bot_token || die "Nejdřív ulož token bota: scripts/setup-secrets.sh telegram-token"
  token="$(tr -d '\r\n' < "$SECRETS_DIR/telegram_bot_token")"
  [[ "$token" =~ $TELEGRAM_TOKEN_RE ]] \
    || die "Soubor secrets/telegram_bot_token nemá tvar tokenu bota. Spusť scripts/setup-secrets.sh telegram-token."
  show_telegram_chat_ids "$token"
}

cmd_fix_perms() {
  ensure_dir
  local f
  for f in "$SECRETS_DIR"/*; do
    [[ -f "$f" ]] && set_perms "$f"
  done
  ok "Oprávnění nastavena (adresář 0700, soubory 0640, skupina $APP_GID)."
}

cmd_all() {
  require_env
  ensure_dir
  ensure_backend_tokens
  ensure_telegram_token
  has_secret ts_authkey || cmd_ts_authkey
  has_secret hevy_api_key || cmd_hevy_key
  has_secret gateway_password_hash || cmd_password
  has_secret gateway_totp_secret || cmd_totp
  cmd_fix_perms
  ok "Všechny secrets jsou připravené. Rotace: scripts/setup-secrets.sh <hevy-key|password|totp|backend-tokens|telegram-token>"
}

require_docker
case "${1:-all}" in
  all) cmd_all ;;
  hevy-key) cmd_hevy_key ;;
  ts-authkey) cmd_ts_authkey ;;
  password) cmd_password ;;
  totp) cmd_totp ;;
  backend-tokens) cmd_backend_tokens ;;
  telegram-token) cmd_telegram_token ;;
  telegram-chat-id) cmd_telegram_chat_id ;;
  fix-perms) cmd_fix_perms ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
