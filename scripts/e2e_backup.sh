#!/usr/bin/env bash
# E2E step for scripts/backup.sh, called by scripts/e2e-test.sh against the running e2e stack
# (COMPOSE_PROJECT_NAME=fcm-e2e or fcm-e2e-<suffix>, see E2E_PROJECT): a missing part fails with a
# clear message and writes nothing, then a complete run writes one archive with the three parts,
# rotates only our own archives and never prints the data. Ends with "E2E backup OK".
#
#   COMPOSE_PROJECT_NAME=fcm-e2e COMPOSE_FILE=docker-compose.yml:compose.test.yml scripts/e2e_backup.sh
source "$(dirname "$0")/lib.sh"
[[ "${COMPOSE_PROJECT_NAME:-}" =~ ^fcm-e2e(-[a-z0-9][a-z0-9_-]*)?$ ]] \
  || die "e2e_backup.sh runs only against the e2e stack (COMPOSE_PROJECT_NAME=fcm-e2e or fcm-e2e-<suffix>)."

w="$(mktemp -d)"
trap 'rm -rf "$w"' EXIT
mkdir "$w/a"

# stop STEP [FILE]: show the captured output (not for the privacy step) and fail.
stop() {
  [[ -z "${2:-}" ]] || cat -- "$2" >&2
  die "E2E backup: $1"
}

# 1. a missing part: no archive, the problem line names the part
if scripts/backup.sh --dir "$w/a" --keep 2 >"$w/out1" 2>&1; then
  stop "step 1: a backup without Garmin tokens should have failed" "$w/out1"
fi
grep -q garmin_tokens.json "$w/out1" || stop "step 1: the output does not name garmin_tokens.json" "$w/out1"
grep -q 'Garmin tokens missing' "$w/out1" || stop "step 1: the output does not say that the Garmin tokens are missing" "$w/out1"
if compgen -G "$w/a/fitness-coach-backup-*" >/dev/null || compgen -G "$w/a/.backup-tmp.*" >/dev/null; then
  stop "step 1: an archive or a staging directory was left after the failure" "$w/out1"
fi

# 1b. a token file without di_token: the host check refuses it, nothing is written
printf '%s' '{"x":1}' | dc exec -T garmin-mcp sh -c 'cat > /data/garmin/garmin_tokens.json' \
  || stop "step 1b: writing the invalid token file failed"
if scripts/backup.sh --dir "$w/a" --keep 2 >"$w/out1b" 2>&1; then
  stop "step 1b: a backup with an invalid token file should have failed" "$w/out1b"
fi
grep -q 'not a valid token file' "$w/out1b" || stop "step 1b: the output does not report the invalid token file" "$w/out1b"
if compgen -G "$w/a/fitness-coach-backup-*" >/dev/null; then
  stop "step 1b: an archive was left after the failure" "$w/out1b"
fi
# A staging directory left by a killed run: the next run removes it.
mkdir "$w/a/.backup-tmp.stale"
: >"$w/a/.backup-tmp.stale/coach.db"

# 2. a throwaway token file (the command the README restore recipe uses)
printf '%s' '{"di_token":"E2E-FAKE-TOKEN-MARKER","di_refresh_token":"E2E-FAKE-REFRESH-MARKER","di_client_id":"e2e"}' \
  | dc exec -T garmin-mcp python -m garmin_coach_mcp.login --tokens-dir /data/garmin --import-file - --no-verify \
  >/dev/null || stop "step 2: importing the test tokens failed"

# 3. old archives, two archives named from the future (a clock that was ahead), a foreign archive-like file
#    and a foreign file that rotation must not touch
for day in 20200101 20200102 20200103 20991230 20991231; do : >"$w/a/fitness-coach-backup-${day}T000000Z.tgz"; done
: >"$w/a/notes.txt"
: >"$w/a/other-backup.tgz"

# 4. a complete run
scripts/backup.sh --dir "$w/a" --keep 2 >"$w/out2" 2>&1 || stop "step 4: the backup failed" "$w/out2"

# 5. rotation: the new archive is never removed; of the others only the newest keep-1 stay; foreign files untouched
[[ ! -e "$w/a/.backup-tmp.stale" ]] || stop "step 5: the leftover of an interrupted backup (.backup-tmp.*) is still there" "$w/out2"
mapfile -t archives < <(cd "$w/a" && ls -1 fitness-coach-backup-*.tgz)
[[ ${#archives[@]} -eq 2 ]] || stop "step 5: expected 2 archives, found ${#archives[@]}" "$w/out2"
new=""
for file in "${archives[@]}"; do
  case "$file" in
    fitness-coach-backup-20991231T000000Z.tgz) ;;
    *) new="$file" ;;
  esac
done
# Match the date part only: a time of day like T202050Z must not look like an old archive.
[[ -n "$new" && "$new" != fitness-coach-backup-202001* && "$new" != fitness-coach-backup-20991230* ]] \
  || stop "step 5: rotation kept the wrong archives (${archives[*]})" "$w/out2"
for gone in 20200101 20200102 20200103 20991230; do
  [[ ! -e "$w/a/fitness-coach-backup-${gone}T000000Z.tgz" ]] || stop "step 5: rotation did not remove the old archive $gone" "$w/out2"
done
[[ -f "$w/a/notes.txt" && -f "$w/a/other-backup.tgz" ]] || stop "step 5: rotation removed a foreign file" "$w/out2"
grep -q 20991230T000000Z "$w/out2" && grep -q 20991231T000000Z "$w/out2" \
  || stop "step 5: missing warning about archives with a newer time" "$w/out2"
archive="$w/a/$new"
[[ "$(stat -c %a "$archive")" == 600 ]] || stop "step 5: the archive does not have mode 600" "$w/out2"

# 5b. a backup directory that is itself named like a staging directory is not removed by the clean-up
mkdir "$w/.backup-tmp.self"
scripts/backup.sh --dir "$w/.backup-tmp.self" --keep 1 >"$w/out3" 2>&1 || stop "step 5b: a backup into a .backup-tmp.* directory failed" "$w/out3"
compgen -G "$w/.backup-tmp.self/fitness-coach-backup-*.tgz" >/dev/null || stop "step 5b: the .backup-tmp.* directory lost its archive" "$w/out3"

# 6. the three parts, flat
[[ "$(tar -tzf "$archive" | sort | tr '\n' ' ')" == "coach.db garmin_tokens.json gateway.sqlite3 " ]] \
  || stop "step 6: wrong archive contents" "$w/out2"

# 7. the parts hold the stack's data
check_db() {
  tar -xzOf "$archive" "$1" | python3 -c '
import sqlite3, sys
conn = sqlite3.connect(":memory:")
conn.deserialize(sys.stdin.buffer.read())
ok = conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
sys.exit(0 if ok and conn.execute(sys.argv[1]).fetchone()[0] >= 1 else 1)
' "$2"
}
check_db coach.db "SELECT count(*) FROM goals WHERE title = 'E2E goal'" || stop "step 7: coach.db lacks the goal from the e2e flow"
check_db gateway.sqlite3 "SELECT count(*) FROM grants" || stop "step 7: gateway.sqlite3 has no grant"
tar -xzOf "$archive" garmin_tokens.json | grep -q E2E-FAKE-TOKEN-MARKER || stop "step 7: garmin_tokens.json lacks the test token"

# 8. privacy: the script output never carries data
if grep -q -e E2E-FAKE -e 'E2E goal' "$w/out1" "$w/out1b" "$w/out2" "$w/out3"; then
  stop "step 8: the backup output contains data"
fi

ok "E2E backup OK"
