#!/usr/bin/env bash
# Garmin Connect login for the garmin-mcp container (tokens live in the
# garmin-tokens volume, files 0600; the password is never stored).
#
#   scripts/garmin-login.sh                interactive login (e-mail, password, MFA code)
#   scripts/garmin-login.sh --check        are the stored tokens still valid?
#   scripts/garmin-login.sh --import FILE  import garmin_tokens.json created elsewhere
#                                          (fallback when Garmin blocks logins from the VPS)
source "$(dirname "$0")/lib.sh"
require_docker

login() {
  dc run --rm --no-deps --entrypoint python "$@"
}

case "${1:-}" in
  --check)
    login -T garmin-mcp -m garmin_coach_mcp.login --check
    ;;
  --import)
    file="${2:-}"
    [[ -f "$file" ]] || die "Give the garmin_tokens.json file (created by a login on a Mac)."
    login -T garmin-mcp -m garmin_coach_mcp.login --import-file - < "$file"
    ok "Tokens imported (0600). Delete the source file: shred -u $file"
    ;;
  "")
    login -it garmin-mcp -m garmin_coach_mcp.login
    info "The running garmin-mcp picks up the new tokens by itself on the next call."
    ;;
  *)
    sed -n '2,8p' "$0"
    exit 2
    ;;
esac
