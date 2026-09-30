#!/usr/bin/env bash
# Garmin Connect login for the garmin-mcp container (tokens live in the
# garmin-tokens volume, files 0600; the password is never stored).
#
#   scripts/garmin-login.sh                interactive login (e-mail, password, MFA code)
#   scripts/garmin-login.sh --check        are the stored tokens still valid?
#   scripts/garmin-login.sh --import DIR   import a token directory created elsewhere
#                                          (fallback when Garmin blocks logins from the VPS)
source "$(dirname "$0")/lib.sh"
require_docker

run_login() {
  dc run --rm --no-deps --entrypoint python "$@"
}

case "${1:-}" in
  --check)
    dc exec -T garmin-mcp python -m garmin_coach_mcp.login --check
    ;;
  --import)
    src="${2:-}"
    [[ -d "$src" ]] || die "Zadej adresář s tokeny (např. ~/.garminconnect zkopírovaný z Macu)."
    # Stream the files in as a tar archive; the container unpacks them as the
    # unprivileged user and tightens permissions.
    tar -C "$src" -cf - . | dc run --rm -T --no-deps --entrypoint sh garmin-mcp -c \
      'umask 077 && tar -C "$GARMINTOKENS" -xf - && find "$GARMINTOKENS" -type f -exec chmod 0600 {} + && ls -l "$GARMINTOKENS"'
    dc restart garmin-mcp >/dev/null
    dc exec -T garmin-mcp python -m garmin_coach_mcp.login --check
    ;;
  "")
    run_login -it garmin-mcp -m garmin_coach_mcp.login
    dc restart garmin-mcp >/dev/null && ok "garmin-mcp restartován s novými tokeny."
    ;;
  *)
    sed -n '2,8p' "$0"
    exit 2
    ;;
esac
