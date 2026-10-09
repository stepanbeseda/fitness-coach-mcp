# Shared helpers for the scripts in this directory (source, don't execute).
# shellcheck shell=bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

APP_UID="${APP_UID:-10001}"
APP_GID="${APP_GID:-10001}"
TS_HOSTNAME="${TS_HOSTNAME:-fitness-mcp}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-}"
SECRETS_DIR="${SECRETS_DIR:-$REPO_ROOT/secrets}"
PUBLIC_HOST="${PUBLIC_BASE_URL#https://}"
PUBLIC_HOST="${PUBLIC_HOST%%/*}"

if [[ -t 1 ]]; then
  C_OK=$'\e[32m' C_WARN=$'\e[33m' C_ERR=$'\e[31m' C_DIM=$'\e[2m' C_OFF=$'\e[0m'
else
  C_OK="" C_WARN="" C_ERR="" C_DIM="" C_OFF=""
fi

info() { printf '%s\n' "$*" >&2; }
ok()   { printf '%s✔%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '%s!%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
fail() { printf '%s✘%s %s\n' "$C_ERR" "$C_OFF" "$*"; }
die()  { printf '%sError:%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }

require_docker() {
  if ! docker info >/dev/null 2>&1; then
    die "Docker is not available to user $(id -un). Log in again (docker group) or run: sg docker -c \"$0 $*\""
  fi
}

dc() { docker compose "$@"; }

require_env() {
  [[ -n "$PUBLIC_BASE_URL" ]] || die "Missing .env with PUBLIC_BASE_URL (cp .env.example .env && chmod 600 .env)."
}
