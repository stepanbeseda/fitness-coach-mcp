#!/usr/bin/env bash
# The one deploy path of the stack: builds the images of the checked-out commit, tags them with the commit, keeps
# the deployed and the previous version in .env, starts the stack and can go back. Run it in the live checkout.
# Exit codes: 0 = done, 1 = refused or failed, 2 = bad arguments.
#
#   scripts/deploy.sh                    build this commit's images, back up, switch .env, start, clean up
#   scripts/deploy.sh --no-backup        the same without the backup (when the stack cannot be backed up)
#   scripts/deploy.sh --status           what runs against what is configured; kept versions; changes nothing
#   scripts/deploy.sh --rollback [VER]   back to FCM_PREVIOUS_VERSION or to a kept VER; images only, no build
#
# A deploy refuses uncommitted changes and missing secret files before it changes anything. The test stage runs
# inside the build. Of gateway, garmin-mcp and coach-mcp it keeps the running version and two older ones, and it
# removes only older images it built itself. ts and hevy-mcp are built only when their image is missing.
# --rollback refuses when the kept images are gone, when docker-compose.yml or a file it mounts from the checkout
# differs from the old commit, and when the coach database is newer than the old coach-mcp.
source "$(dirname "$0")/lib.sh"

VERSIONED=(gateway garmin-mcp coach-mcp)   # services whose image tag is the version
KEEP=3                                     # versions kept per service: the configured one plus 2 others
TAG_RE='^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$'

usage() {
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0" >&2
  exit 2
}

MODE=deploy
NO_BACKUP=0
ROLLBACK_VER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-backup) NO_BACKUP=1; shift ;;
    --status)    [[ $MODE == deploy ]] || usage; MODE=status; shift ;;
    --rollback)
      [[ $MODE == deploy ]] || usage
      MODE=rollback; shift
      if [[ $# -gt 0 && "$1" != -* ]]; then ROLLBACK_VER="$1"; shift; fi ;;
    *) usage ;;
  esac
done
[[ $NO_BACKUP == 0 || $MODE == deploy ]] || usage

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
HELPER="$TMP/helper.py"

# One python helper for everything that is parsed: .env, `docker compose config`, `docker image inspect`. The
# data always comes as a file path in argv, never on stdin.
cat > "$HELPER" <<'PY'
import json
import os
import stat
import sys


def clean(value):
    return str(value).replace("\n", " ").replace("|", "/")


def env_read(path, key):
    with open(path, "rb") as handle:
        lines = handle.read().decode("utf-8", "surrogateescape").split("\n")
    values = [line.rstrip("\r")[len(key) + 1:] for line in lines if line.startswith(key + "=")]
    if len(values) > 1:
        sys.exit(3)
    if not values:
        return
    value = values[0]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    print(value)


def env_write(path, version, previous):
    with open(path, "rb") as handle:
        data = handle.read()
    for key, value in (("FCM_VERSION", version), ("FCM_PREVIOUS_VERSION", previous)):
        if value is None:
            continue
        prefix = key.encode() + b"="
        lines = data.split(b"\n")
        for index, line in enumerate(lines):
            if line.startswith(prefix):
                lines[index] = prefix + value.encode() + (b"\r" if line.endswith(b"\r") else b"")
                data = b"\n".join(lines)
                break
        else:
            if data and not data.endswith(b"\n"):
                data += b"\n"
            data += prefix + value.encode() + b"\n"
    tmp = os.path.join(os.path.dirname(os.path.abspath(path)), ".deploy-tmp.env")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fchmod(fd, stat.S_IMODE(os.stat(path).st_mode))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def image(path):
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
        entry = doc[0]
        labels = (entry.get("Config") or {}).get("Labels") or {}
        prefix = "org.opencontainers.image."
        print("|".join(clean(x) for x in (
            entry["Id"], labels.get(prefix + "version", ""), labels.get(prefix + "revision", ""),
            labels.get(prefix + "created", ""))))
    except (OSError, ValueError, LookupError, TypeError, AttributeError):
        sys.exit(1)


def config(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def deploy_built(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            tag, version, created = (line.rstrip("\n").split("\t") + ["", ""])[:3]
            if tag and version == tag and created:
                rows.append((tag, created))
    return rows


def newest_first(rows):
    return sorted(rows, key=lambda row: (row[1], row[0]), reverse=True)


def inside(repo, path):
    real = os.path.realpath(path)
    if real == repo or real.startswith(repo + os.sep):
        return os.path.relpath(real, repo)
    return None


def main(argv):
    cmd, args = argv[0], argv[1:]
    if cmd == "envread":
        env_read(*args)
    elif cmd == "envwrite":
        env_write(args[0], args[1], args[2] if len(args) > 2 else None)
    elif cmd == "image":
        image(args[0])
    elif cmd == "services":
        for name, svc in config(args[0])["services"].items():
            print(name, svc["image"])
    elif cmd == "secrets":
        for secret in (config(args[0]).get("secrets") or {}).values():
            if secret.get("file"):
                print(secret["file"])
    elif cmd == "guard":
        path, repo, compose_file = args
        repo = os.path.realpath(repo)
        seen = []
        for entry in compose_file.split(":"):
            if entry:
                rel = inside(repo, entry if os.path.isabs(entry) else os.path.join(repo, entry))
                if rel and rel not in seen:
                    seen.append(rel)
        for svc in config(path)["services"].values():
            for volume in svc.get("volumes") or []:
                if volume.get("type") == "bind":
                    rel = inside(repo, volume["source"])
                    if rel and rel not in seen:
                        seen.append(rel)
        print("\n".join(seen))
    elif cmd == "cleanup":
        current, previous, keep, rows_path = args
        rows = deploy_built(rows_path)
        previous_kept = bool(previous) and any(tag == previous for tag, _ in rows)
        slots = max(int(keep) - 1 - (1 if previous_kept else 0), 0)
        candidates = newest_first([row for row in rows if row[0] not in (current, previous)])
        for tag, _ in candidates[slots:]:
            print(tag)
    elif cmd == "kept":
        print(" ".join(tag for tag, _ in newest_first(deploy_built(args[0]))))
    else:
        sys.exit(64)


main(sys.argv[1:])
PY

hp() { python3 "$HELPER" "$@"; }

env_value() { hp envread "$REPO_ROOT/.env" "$1"; }

# image_fields REF: "<id>|<version>|<revision>|<created>" (the three labels are empty when absent); fails when
# the image is missing or cannot be read. The labels come from the full inspect JSON: a --format on
# .Config.Labels fails for an image without a Labels map.
image_fields() {
  docker image inspect "$1" > "$TMP/inspect.json" 2>/dev/null || return 1
  hp image "$TMP/inspect.json"
}

# load_config VERSION OUT: `docker compose config` as JSON with FCM_VERSION=VERSION (the current environment when
# VERSION is empty). Project, images, secrets and bind sources are read from it, so nothing is named here.
load_config() {
  local out="$2"
  if [[ -n "$1" ]]; then
    ( export FCM_VERSION="$1"; dc config --format json > "$out" ) || die "docker compose config failed (above)."
  else
    dc config --format json > "$out" || die "docker compose config failed (above)."
  fi
}

# read_services CONFIG: fills SVCS (config order) and IMG (service -> configured image).
read_services() {
  local name image
  SVCS=()
  declare -gA IMG=()
  while read -r name image; do
    SVCS+=("$name")
    IMG[$name]="$image"
  done < <(hp services "$1")
  [[ ${#SVCS[@]} -gt 0 ]] || die "docker compose config lists no services."
}

is_versioned() {
  local svc
  for svc in "${VERSIONED[@]}"; do [[ "$svc" == "$1" ]] && return 0; done
  return 1
}

clean_checkout() {
  local status
  status="$(git status --porcelain)"
  if [[ -n "$status" ]]; then
    die "The checkout has uncommitted or untracked changes; a deploy builds exactly a commit. Commit or remove them, then run again:
$(git status --short | head -n 10)"
  fi
}

# check_env_file: .env is a plain file with at most one line per key this script writes.
check_env_file() {
  local key rc
  [[ -e .env ]] || die "There is no .env file; scripts/deploy.sh records the version there."
  [[ ! -L .env ]] || die ".env is a symlink; scripts/deploy.sh rewrites it in place and needs a plain file."
  for key in FCM_VERSION FCM_PREVIOUS_VERSION; do
    rc=0
    env_value "$key" > /dev/null || rc=$?
    [[ $rc == 0 ]] || die ".env has more than one $key= line; keep one and run again."
  done
}

# tag_rows REPO OUT: one line "tag<TAB>version label<TAB>created label" per tag of REPO that can be read.
tag_rows() {
  local repo="$1" out="$2" line tag fields version created
  : > "$out"
  docker image ls --filter "reference=$repo" --format '{{.Repository}}:{{.Tag}}' > "$TMP/ls.txt" \
    || die "docker image ls failed (above)."
  while IFS= read -r line; do
    [[ "$line" == "$repo:"* ]] || continue
    tag="${line#"$repo":}"
    [[ -n "$tag" && "$tag" != "<none>" ]] || continue
    fields="$(image_fields "$line")" || continue
    IFS='|' read -r _ version _ created <<< "$fields"
    printf '%s\t%s\t%s\n' "$tag" "$version" "$created" >> "$out"
  done < "$TMP/ls.txt"
}

# status_report: what runs against what is configured. Exit status 0 only when everything matches.
status_report() {
  local cfg="$TMP/status-config.json" rc=0 svc ref fields id revision cid inspected running state previous kept repo
  dc config --format json > "$cfg" || return 1
  read_services "$cfg"
  for svc in "${SVCS[@]}"; do
    ref="${IMG[$svc]}"
    if ! fields="$(image_fields "$ref")"; then
      printf '%s: %s image missing\n' "$svc" "$ref"; rc=1; continue
    fi
    IFS='|' read -r id _ revision _ <<< "$fields"
    cid="$(dc ps -q "$svc" | head -n 1)" || cid=""
    if [[ -z "$cid" ]]; then
      printf '%s: %s not running\n' "$svc" "$ref"; rc=1; continue
    fi
    inspected="$(docker inspect --format '{{.Image}} {{.Config.Image}}' "$cid")" || inspected=""
    read -r running _ <<< "$inspected"
    if [[ "$running" == "$id" ]]; then
      state="running"
      if is_versioned "$svc"; then
        if [[ -n "$revision" ]]; then
          state="running, commit ${revision:0:12}"
        else
          state="running, not built by scripts/deploy.sh"; rc=1
        fi
      fi
    else
      state="DIFFERENT: runs $(printf '%s' "${running#sha256:}" | cut -c1-12), configured image is $(printf '%s' "${id#sha256:}" | cut -c1-12)"
      rc=1
    fi
    printf '%s: %s %s\n' "$svc" "$ref" "$state"
  done
  previous="$(env_value FCM_PREVIOUS_VERSION)" || previous=""
  printf 'Previous version: %s\n' "${previous:-none}"
  for svc in "${VERSIONED[@]}"; do
    repo="${IMG[$svc]%:*}"
    tag_rows "$repo" "$TMP/status-rows.$svc"
    kept="$(hp kept "$TMP/status-rows.$svc")"
    printf 'Kept %s: %s\n' "$svc" "${kept:-none}"
  done
  return $rc
}

# clean_up CURRENT PREVIOUS: removes the older deploy-built images (an image is deploy-built when its version
# label equals its tag and its created label is set), keeping KEEP versions per service. IMG holds the images
# of CURRENT.
clean_up() {
  local current="$1" previous="$2" svc repo tag
  local -a drop
  for svc in "${VERSIONED[@]}"; do
    repo="${IMG[$svc]%:*}"
    tag_rows "$repo" "$TMP/rows.$svc"
    mapfile -t drop < <(hp cleanup "$current" "$previous" "$KEEP" "$TMP/rows.$svc")
    for tag in "${drop[@]}"; do
      [[ -n "$tag" ]] || continue
      docker rmi "$repo:$tag" > /dev/null 2>&1 || warn "Could not remove $repo:$tag (still used by a container?); it stays."
    done
  done
}

# start_stack FAILTEXT: FCM_VERSION must be exported before this runs (a shell value beats .env).
start_stack() {
  if ! dc up -d --no-build --wait --wait-timeout 300; then
    status_report || true
    die "$1"
  fi
}

# ---------------------------------------------------------------------------------------------------------------
# --status

if [[ $MODE == status ]]; then
  require_docker
  require_env
  rc=0
  status_report || rc=$?
  exit $rc
fi

# ---------------------------------------------------------------------------------------------------------------
# --rollback

if [[ $MODE == rollback ]]; then
  require_docker
  require_env
  clean_checkout
  check_env_file
  current="$(env_value FCM_VERSION)"
  [[ -n "$current" ]] || die "No version deployed by scripts/deploy.sh yet (FCM_VERSION is not set in .env)."
  target="$ROLLBACK_VER"
  [[ -n "$target" ]] || target="$(env_value FCM_PREVIOUS_VERSION)"
  [[ -n "$target" ]] || die "No previous version recorded; name one: scripts/deploy.sh --rollback <version> (scripts/deploy.sh --status lists the kept ones)."
  [[ "$target" =~ $TAG_RE ]] || die "The version \"$target\" is not a valid image tag."
  [[ "$target" != "$current" ]] || die "$target is already the configured version."

  load_config "$target" "$TMP/target-config.json"
  read_services "$TMP/target-config.json"
  missing=()
  revisions=()
  for svc in "${VERSIONED[@]}"; do
    if fields="$(image_fields "${IMG[$svc]}")"; then
      IFS='|' read -r _ _ rev _ <<< "$fields"
      revisions+=("$rev")
    else
      missing+=("${IMG[$svc]}")
    fi
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    die "The images of $target are not kept:
$(printf '  %s\n' "${missing[@]}")
The way back is a revert PR and scripts/deploy.sh. Nothing was changed."
  fi

  # R3: the commit the images came from, and the files read from the checkout.
  empty=0
  for rev in "${revisions[@]}"; do [[ -n "$rev" ]] || empty=$((empty + 1)); done
  if [[ $empty == ${#revisions[@]} ]]; then
    [[ -n "$ROLLBACK_VER" ]] \
      || die "The previous version $target was not built by scripts/deploy.sh, so its commit is unknown. Name it explicitly to roll back to it (only possible while its gateway config matches the checkout)."
    legacy_config="gateway of $target was built with another gateway/config.toml than the checkout has (for example where it listens); starting it under the current docker-compose.yml and tailscale/serve.json is unsafe. The way back is a revert PR and scripts/deploy.sh. Nothing was changed."
    docker run --rm --network none --entrypoint cat "${IMG[gateway]}" /app/config.toml > "$TMP/baked-config.toml" 2> /dev/null \
      || die "The $legacy_config"
    cmp -s "$TMP/baked-config.toml" gateway/config.toml || die "The $legacy_config"
    warn "The images of $target carry no commit, so docker-compose.yml and the files it mounts from the checkout cannot be compared; their gateway config matches the checkout. Go on only if docker-compose.yml and tailscale/serve.json did not change since those images were built."
  elif [[ $empty != 0 || "${revisions[0]}" != "${revisions[1]}" || "${revisions[0]}" != "${revisions[2]}" ]]; then
    die "The images of $target come from different commits; nothing was changed."
  else
    rev="${revisions[0]}"
    git cat-file -e "$rev^{commit}" 2> /dev/null \
      || die "Commit ${rev:0:12} of $target is not in this checkout (git fetch?); nothing was changed."
    load_config "" "$TMP/current-config.json"
    mapfile -t guard < <(hp guard "$TMP/current-config.json" "$REPO_ROOT" "${COMPOSE_FILE:-docker-compose.yml}")
    if ! git diff --quiet "$rev" HEAD -- "${guard[@]}"; then
      die "$target was built from ${rev:0:12}; since then these files changed:
$(git diff --name-only "$rev" HEAD -- "${guard[@]}" | sed 's/^/  /')
They are read from the checkout, so the old images alone do not bring the old version back. The way back is a revert PR and scripts/deploy.sh. Nothing was changed."
    fi
  fi

  # R4: the coach database must not be newer than the target coach-mcp.
  s_target="$(docker run --rm --network none --entrypoint python "${IMG[coach-mcp]}" \
    -c 'from coach_mcp.store import SCHEMA_VERSION; print(SCHEMA_VERSION)' 2> /dev/null)" || s_target=""
  # shellcheck disable=SC2016
  probe='import os, sqlite3; p = "/data/coach/coach.db"; print(sqlite3.connect(f"file:{p}?mode=ro", uri=True).execute("PRAGMA user_version").fetchone()[0] if os.path.exists(p) else 0)'
  s_db="$( ( export FCM_VERSION="$target"; dc run --rm --no-deps -T --entrypoint python coach-mcp -c "$probe" < /dev/null 2> "$TMP/probe.err" ) )" || s_db=""
  [[ "$s_target" =~ ^[0-9]+$ ]] || die "Cannot read the schema version (the coach-mcp image of $target); nothing was changed."
  [[ "$s_db" =~ ^[0-9]+$ ]] || die "Cannot read the schema version (the coach database); nothing was changed."
  if (( s_db > s_target )); then
    die "The coach database has schema $s_db, the coach-mcp of $target supports $s_target. First run the down-migrations from $s_db to $s_target (coach-mcp/README.md, \"Going back to a version without …\"), then run this again. Nothing was changed."
  fi

  # R5: switch and start; no backup, no build, no clean-up.
  hp envwrite "$REPO_ROOT/.env" "$target" "$current"
  export FCM_VERSION="$target"
  start_stack "Not every service became healthy (status above). $target stays configured."
  status_report || true
  ok "Rolled back to $target (previous: $current)."
  exit 0
fi

# ---------------------------------------------------------------------------------------------------------------
# deploy

# D0: preconditions; nothing is changed yet.
require_docker
require_env
clean_checkout
version="$(git describe --tags --always --abbrev=12)"
[[ "$version" =~ $TAG_RE ]] || die "The version \"$version\" from git describe is not a valid image tag; rename the git tag."
revision="$(git rev-parse HEAD)"
check_env_file
old_version="$(env_value FCM_VERSION)"
load_config "$version" "$TMP/config.json"
read_services "$TMP/config.json"
while IFS= read -r secret_file; do
  [[ -e "$secret_file" ]] || die "Missing secret file $secret_file → scripts/setup-secrets.sh. Nothing was changed."
done < <(hp secrets "$TMP/config.json")

# D1: backup (only when something runs).
if [[ $NO_BACKUP == 1 ]]; then
  warn "No backup before the deploy (--no-backup)."
elif [[ -z "${BACKUP_DIR:-}" ]]; then
  info "BACKUP_DIR is not set: no backup before the deploy."
elif [[ -z "$(dc ps -q)" ]]; then
  info "Nothing of the stack is running: no backup before the deploy."
else
  "$REPO_ROOT/scripts/backup.sh" || die "The backup failed (above); nothing was deployed. Fix it, or deploy with --no-backup."
fi

# D2: build what is missing; the build is the gate (the test stage runs inside it).
to_build=()
for svc in "${VERSIONED[@]}"; do
  fields="$(image_fields "${IMG[$svc]}")" || { to_build+=("$svc"); continue; }
  IFS='|' read -r _ _ rev _ <<< "$fields"
  [[ "$rev" == "$revision" ]] || to_build+=("$svc")
done
for svc in "${SVCS[@]}"; do
  is_versioned "$svc" || image_fields "${IMG[$svc]}" > /dev/null || to_build+=("$svc")
done
if [[ ${#to_build[@]} -gt 0 ]]; then
  info "Building ${to_build[*]} (version $version)..."
  build_log="$(mktemp "${TMPDIR:-/tmp}/deploy-build.XXXXXX")"
  created="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  # --progress=plain: with the output redirected, Compose's tty renderer fails ("failed to get console").
  if ! ( export FCM_VERSION="$version" FCM_REVISION="$revision" FCM_CREATED="$created"
         dc build --progress=plain "${to_build[@]}" > "$build_log" 2>&1 ); then
    tail -n 40 "$build_log" >&2
    die "The build failed (log: $build_log, last lines above); nothing was deployed."
  fi
  rm -f "$build_log"
fi
for svc in "${VERSIONED[@]}"; do
  fields="$(image_fields "${IMG[$svc]}")" || die "The build did not produce ${IMG[$svc]}; nothing was deployed."
  IFS='|' read -r _ _ rev _ <<< "$fields"
  [[ "$rev" == "$revision" ]] || die "${IMG[$svc]} does not carry the revision $revision; nothing was deployed."
done

# D3: switch .env.
previous_arg=()
if [[ "$old_version" == "$version" ]]; then
  :
elif [[ -n "$old_version" ]]; then
  previous_arg=("$old_version")
else
  cid="$(dc ps -q coach-mcp | head -n 1)" || cid=""
  if [[ -n "$cid" ]]; then
    inspected="$(docker inspect --format '{{.Image}} {{.Config.Image}}' "$cid")" || inspected=""
    read -r _ running_ref <<< "$inspected"
    running_tag="${running_ref##*:}"
    if [[ "$running_ref" == *:* && "$running_tag" != */* && "${running_ref%:*}" == "${IMG[coach-mcp]%:*}" \
          && -n "$running_tag" && "$running_tag" != "$version" ]]; then
      previous_arg=("$running_tag")
    fi
  fi
fi
hp envwrite "$REPO_ROOT/.env" "$version" "${previous_arg[@]}"
export FCM_VERSION="$version"

# D4: start. --no-build never builds (D2 made sure every image exists); --wait fails on an unhealthy service.
start_stack "Not every service became healthy (status above). $version stays configured; to go back: scripts/deploy.sh --rollback"

# D5: status and clean-up.
status_report || true
previous="$(env_value FCM_PREVIOUS_VERSION)"
clean_up "$version" "$previous"
ok "Deployed $version (previous: ${previous:-none})."
