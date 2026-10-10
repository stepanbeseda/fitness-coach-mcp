#!/usr/bin/env bash
# Online backup of the stack state into one archive (mode 0600) while the services run:
# coach.db and gateway.sqlite3 through SQLite's online backup API inside their containers,
# plus the Garmin token file. Keeps the newest BACKUP_KEEP archives. Exit 0 = archive written,
# 1 = no archive (every problem is listed), 2 = bad arguments.
#
#   scripts/backup.sh                       BACKUP_DIR and BACKUP_KEEP from .env
#   scripts/backup.sh --quiet               print only problems (for cron)
#   scripts/backup.sh --dir DIR --keep N    override .env (scripts/e2e_backup.sh)
source "$(dirname "$0")/lib.sh"
require_docker
require_env
umask 077

usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

QUIET=0
opt_dir=""
opt_keep=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --quiet) QUIET=1; shift ;;
    --dir)   [[ $# -ge 2 ]] || usage; opt_dir="$2"; shift 2 ;;
    --keep)  [[ $# -ge 2 ]] || usage; opt_keep="$2"; shift 2 ;;
    *)       usage ;;
  esac
done

dir="${opt_dir:-${BACKUP_DIR:-}}"
keep="${opt_keep:-${BACKUP_KEEP:-14}}"
[[ -n "$dir" ]] || die "Set BACKUP_DIR in .env: an absolute path to a directory outside the repo."
[[ "$dir" == /* ]] || die "BACKUP_DIR must be an absolute path (is: $dir)."
real_dir="$(realpath -m -- "$dir")"
real_repo="$(realpath -- "$REPO_ROOT")"
if [[ "$real_dir" == "$real_repo" || "$real_dir" == "$real_repo"/* ]]; then
  die "BACKUP_DIR must not be inside the repo ($REPO_ROOT): the archives hold personal data and Garmin tokens."
fi
[[ "$keep" =~ ^[1-9][0-9]{0,2}$ ]] || die "BACKUP_KEEP must be a number from 1 to 999."

mkdir -p -- "$dir"
exec 9>>"$dir/.backup.lock"
flock -n 9 || die "Another backup into $dir is already running."
# Under the lock no other run owns a staging directory: what is left is from a killed run (it holds copies of the data).
find "$dir" -mindepth 1 -maxdepth 1 -type d -name '.backup-tmp.*' -exec rm -rf -- {} +

ts="$(date -u +%Y%m%dT%H%M%SZ)"
name="fitness-coach-backup-$ts.tgz"
stage="$(mktemp -d "$dir/.backup-tmp.XXXXXX")"
trap 'rm -rf "$stage"' EXIT

problems=()

# take_part PART SERVICE COMMAND...: the command's stdout becomes $stage/PART. The content is never
# echoed, kept in a variable or put on a command line; only the last line of stderr is shown.
take_part() {
  local part="$1" svc="$2"
  shift 2
  local out="$stage/$part" err="$stage/.err" line
  if dc exec -T "$svc" "$@" >"$out" 2>"$err" && [[ -s "$out" ]]; then
    return 0
  fi
  if [[ "$part" == garmin_tokens.json ]] && grep -q 'No such file' "$err"; then
    problems+=("$part: Garmin tokens missing (log in: scripts/garmin-login.sh)")
    return 0
  fi
  line="$(sed '/^[[:space:]]*$/d' "$err" | tail -n 1)"
  problems+=("$part: $svc: ${line:-empty output}")
}

take_part coach.db coach-mcp python -m coach_mcp backup
take_part gateway.sqlite3 gateway python -m gateway.cli backup-db
take_part garmin_tokens.json garmin-mcp cat /data/garmin/garmin_tokens.json

if [[ -s "$stage/garmin_tokens.json" ]] && ! python3 -c '
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
sys.exit(0 if isinstance(doc, dict) and isinstance(doc.get("di_token"), str) and doc["di_token"] else 1)
' "$stage/garmin_tokens.json" 2>/dev/null; then
  problems+=("garmin_tokens.json: not a valid token file")
fi

if [[ ${#problems[@]} -gt 0 ]]; then
  for problem in "${problems[@]}"; do
    fail "$problem"
  done
  die "No backup written (${#problems[@]} problem(s)); older archives left unchanged."
fi

tar -C "$stage" -czf "$stage/archive.tgz" coach.db gateway.sqlite3 garmin_tokens.json \
  || die "Cannot create the archive (tar failed; disk space?); older archives left unchanged."
chmod 600 "$stage/archive.tgz"
[[ ! -e "$dir/$name" ]] || die "Archive $name already exists (two backups in the same second?)."
ln "$stage/archive.tgz" "$dir/$name" 2>/dev/null || die "Cannot write archive $name to $dir."

# Rotation: the archive written by this run is never touched. Of the other files that look like our archives
# the newest keep-1 stay, so at most $keep archives remain; anything else in the directory is never touched.
others=()
while IFS= read -r file; do
  [[ "$file" =~ ^fitness-coach-backup-[0-9]{8}T[0-9]{6}Z\.tgz$ && "$file" != "$name" ]] && others+=("$file")
done < <(find "$dir" -maxdepth 1 -type f -name 'fitness-coach-backup-*.tgz' -printf '%f\n' | LC_ALL=C sort)
# A newer name than the one just written (clock was ahead, foreign archive) would pose as the freshest backup.
for file in "${others[@]}"; do
  if [[ "$file" > "$name" ]]; then
    warn "Archive $file has a newer time than the one just written, $name (clock was ahead or a foreign archive?); check BACKUP_DIR."
  fi
done
excess=$(( ${#others[@]} - (keep - 1) ))
for (( i = 0; i < excess; i++ )); do
  rm -f -- "$dir/${others[$i]}"
  [[ $QUIET == 1 ]] || info "Removed old archive: ${others[$i]}"
done
[[ -f "$dir/$name" ]] || die "Archive $name is missing after rotation."

[[ $QUIET == 1 ]] || ok "Backup: $dir/$name ($(du -h -- "$dir/$name" | cut -f1))"
