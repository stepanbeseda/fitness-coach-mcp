#!/usr/bin/env bash
# Create or rotate secrets in ./secrets (Docker secrets for the compose stack).
#
#   scripts/setup-secrets.sh                 create everything that is missing
#   scripts/setup-secrets.sh hevy-key        rotate the Hevy API key
#   scripts/setup-secrets.sh ts-authkey      set the Tailscale auth key (first start only)
#   scripts/setup-secrets.sh password        change the gateway login password
#   scripts/setup-secrets.sh totp            enrol a new TOTP authenticator
#   scripts/setup-secrets.sh backend-tokens  rotate the internal tokens (hevy, garmin, coach, garmin read-only)
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
    sudo chgrp "$APP_GID" "$file" || die "Could not set group $APP_GID on $file (needs sudo)."
  fi
}

write_secret() {
  local name="$1" value="$2" file="$SECRETS_DIR/$1"
  ensure_dir
  # Write in place (same inode) so running containers never see a half-written file.
  (umask 077 && printf '%s\n' "$value" > "$file")
  set_perms "$file"
  ok "Saved: secrets/$name"
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
    info "Building the gateway image (the first build takes a while)..."
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
      ok "Recreated: $svc"
    fi
  done
}

cmd_hevy_key() {
  local key code
  info "Hevy API key: https://hevy.com/settings?developer (Hevy web app, requires PRO)."
  key="$(read_hidden 'Hevy API key: ')"
  [[ -n "$key" ]] || die "Empty key."
  code="$(printf 'api-key: %s\n' "$key" | curl -sS -o /dev/null -w '%{http_code}' -H @- \
          https://api.hevyapp.com/v1/user/info || true)"
  case "$code" in
    200) ok "Hevy API key verified." ;;
    401|403) die "Hevy rejected the key (HTTP $code). Nothing was saved." ;;
    *) warn "Could not verify the key (HTTP ${code:-?}); saving it anyway." ;;
  esac
  write_secret hevy_api_key "$key"
  recreate hevy-mcp
}

cmd_ts_authkey() {
  local key
  info "Auth key: Tailscale admin → Settings → Keys → Generate auth key"
  info "  (Reusable: no, Ephemeral: no, Pre-approved: yes, Tags: ${TS_TAG:-tag:fitness-mcp})."
  key="$(read_hidden 'Tailscale auth key (tskey-auth-...): ')"
  [[ "$key" == tskey-* ]] || die "This does not look like a Tailscale auth key."
  write_secret ts_authkey "$key"
  info "The key is only needed for the node's first login; after that the state is kept in the ts-state volume."
}

cmd_password() {
  local pw pw2 hash
  ensure_gateway_image
  info "Password for the gateway login (at least 12 characters, ideally a passphrase of 4+ words)."
  pw="$(read_hidden 'New password: ')"
  pw2="$(read_hidden 'Repeat password: ')"
  [[ "$pw" == "$pw2" ]] || die "The passwords do not match."
  hash="$(printf '%s\n' "$pw" | docker run --rm -i --network none --entrypoint python "$GATEWAY_IMAGE" \
          -m gateway.cli hash-password --stdin)" || die "Hashing the password failed."
  unset pw pw2
  write_secret gateway_password_hash "$hash"
  recreate gateway
}

cmd_totp() {
  local secret code attempt
  ensure_gateway_image
  secret="$(docker run --rm -i --network none --entrypoint python "$GATEWAY_IMAGE" \
            -m gateway.cli new-totp --account "${OWNER_TAILSCALE_LOGIN:-owner}")" || die "Generating the TOTP secret failed."
  for attempt in 1 2 3; do
    IFS= read -rp "Enter the current 6-digit code from your authenticator: " code </dev/tty
    if printf '%s\n%s\n' "$secret" "$code" | docker run --rm -i --network none --entrypoint python \
         "$GATEWAY_IMAGE" -m gateway.cli verify-totp; then
      write_secret gateway_totp_secret "$secret"
      recreate gateway
      return 0
    fi
    warn "Wrong code (attempt $attempt/3). Check the clock on your phone."
  done
  die "TOTP not verified, nothing was saved."
}

new_backend_token() {
  local value
  value="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
    || die "Generating the internal token $1 failed, nothing was saved."
  [[ ${#value} -ge 32 ]] || die "Generating the internal token $1 failed, nothing was saved."
  write_secret "$1" "$value"
}

cmd_backend_tokens() {
  new_backend_token hevy_backend_token
  new_backend_token garmin_backend_token
  new_backend_token coach_backend_token
  new_backend_token garmin_readonly_token
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

# The coach's token for garmin-mcp, which refuses it every tool not annotated read-only. Created
# only when missing or empty, and nothing is recreated: containers of an older version mount no
# such secret, so the token takes effect with the deploy of garmin-mcp and coach-mcp.
ensure_garmin_readonly_token() {
  if ! has_secret garmin_readonly_token; then
    new_backend_token garmin_readonly_token
    info "The new read-only token for coach-mcp takes effect after a deploy: docker compose up -d --build garmin-mcp coach-mcp"
  fi
  return 0
}

# Empty placeholder so docker compose can mount the secret while the morning briefing is off
# (coach-mcp sends nothing while COACH_TELEGRAM_CHAT_ID is empty).
ensure_telegram_token() {
  local file="$SECRETS_DIR/telegram_bot_token"
  if [[ ! -e "$file" ]]; then
    ensure_dir
    (umask 077 && : > "$file")
    set_perms "$file"
    ok "Created: secrets/telegram_bot_token (empty, morning briefing off)"
  fi
  return 0
}

wait_enter() { IFS= read -r _ </dev/tty; }

# Lists the private chats that recently wrote to the bot (getUpdates keeps them for 24 hours).
show_telegram_chat_ids() {
  local body code ids
  body="$(mktemp)"
  info "Now send the bot any message in Telegram (for example /start), then press Enter here."
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
    warn "No private chat with the bot found (HTTP $code). Message the bot and run scripts/setup-secrets.sh telegram-chat-id."
    info "With two-way Telegram on, coach-mcp reads your messages first: docker compose stop coach-mcp, then run this again."
    info "Afterwards: docker compose start coach-mcp."
    return 0
  fi
  ok "Private chats with the bot (chat id and name):"
  printf '%s\n' "$ids" | sed 's/^/  /'
  info "Add the line COACH_TELEGRAM_CHAT_ID= with your chat id to .env and run docker compose up -d coach-mcp."
}

cmd_telegram_token() {
  local token code
  info "Bot token from @BotFather in Telegram (/newbot), shaped like 123456789:AA..."
  token="$(read_hidden 'Telegram bot token: ')"
  [[ "$token" =~ $TELEGRAM_TOKEN_RE ]] \
    || die "This does not look like a Telegram bot token (digits, a colon, characters). Nothing was saved."
  code="$(telegram_api "$token" getMe /dev/null)"
  case "$code" in
    200) ok "Bot token verified." ;;
    401|404) die "Telegram rejected the token (HTTP $code). Nothing was saved." ;;
    *) warn "Could not verify the token (HTTP $code); saving it anyway." ;;
  esac
  write_secret telegram_bot_token "$token"
  show_telegram_chat_ids "$token"
  recreate coach-mcp
}

cmd_telegram_chat_id() {
  local token
  has_secret telegram_bot_token || die "Save the bot token first: scripts/setup-secrets.sh telegram-token"
  token="$(tr -d '\r\n' < "$SECRETS_DIR/telegram_bot_token")"
  [[ "$token" =~ $TELEGRAM_TOKEN_RE ]] \
    || die "The file secrets/telegram_bot_token does not hold a bot token. Run scripts/setup-secrets.sh telegram-token."
  show_telegram_chat_ids "$token"
}

cmd_fix_perms() {
  ensure_dir
  local f
  for f in "$SECRETS_DIR"/*; do
    [[ -f "$f" ]] && set_perms "$f"
  done
  ok "Permissions set (directory 0700, files 0640, group $APP_GID)."
}

cmd_all() {
  require_env
  ensure_dir
  ensure_backend_tokens
  ensure_garmin_readonly_token
  ensure_telegram_token
  has_secret ts_authkey || cmd_ts_authkey
  has_secret hevy_api_key || cmd_hevy_key
  has_secret gateway_password_hash || cmd_password
  has_secret gateway_totp_secret || cmd_totp
  cmd_fix_perms
  ok "All secrets are in place. Rotation: scripts/setup-secrets.sh <hevy-key|password|totp|backend-tokens|telegram-token>"
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
