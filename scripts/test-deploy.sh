#!/usr/bin/env bash
# Offline test of scripts/deploy.sh (deploy, --no-backup, --status, --rollback). It builds a fake repo with real
# git in a temporary directory, with copies of scripts/lib.sh and scripts/deploy.sh and a stub backup.sh, and runs
# the script there under `env -i` with a stub `docker` (a python script that keeps images and containers in a
# state file and implements exactly the call shapes deploy.sh may use; anything else is logged as UNEXPECTED and
# fails the row) and a stub `date` (a clock that goes up one second per call). The fake project is `fcm-test` and
# its images live under `example.test/`, so nothing in deploy.sh can be hard-coded. No Docker, no network, no
# access to ./secrets or .env.
#
#   scripts/test-deploy.sh
#   FAIL_FAST=1 scripts/test-deploy.sh     stop at the first failed row (used to run mutants)
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILED=0
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
R=example.test

check() {
  if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; FAILED=1; [[ -z "${FAIL_FAST:-}" ]] || exit 1; fi
}

mkdir -p "$WORK/bin" "$WORK/home" "$WORK/tmp" "$WORK/cases"
REAL_DATE="$(command -v date)"

# --- stub state shared by the docker stub and the case controller ----------------------------------------------
cat > "$WORK/fakelib.py" <<'PY'
import hashlib
import json
import os

FAKE = os.environ["FAKE"]
PREFIX = "org.opencontainers.image."
COMPOSE_LABELS = {"com.docker.compose.project": "fcm-test"}
VERSIONED = ("gateway", "garmin-mcp", "coach-mcp")


def path(name):
    return os.path.join(FAKE, name)


def load():
    if os.path.exists(path("state.json")):
        with open(path("state.json")) as handle:
            return json.load(handle)
    return {"n": 0, "images": {}, "containers": {}}


def save(state):
    with open(path("state.json"), "w") as handle:
        json.dump(state, handle)


def new_id(state):
    state["n"] += 1
    return "sha256:" + hashlib.sha256(("id%d" % state["n"]).encode()).hexdigest()


def config():
    version = os.environ.get("FCM_VERSION") or "local"
    cwd = os.getcwd()
    services = {name: {"image": "example.test/%s:%s" % (name, version)} for name in VERSIONED}
    services["ts"] = {"image": "example.test/tailscale:1.0", "volumes": [
        {"type": "bind", "source": cwd + "/tailscale/serve.json", "target": "/config/serve.json"},
        {"type": "volume", "source": "ts-state", "target": "/var/lib/tailscale"}]}
    services["hevy-mcp"] = {"image": "example.test/hevy-mcp:1.0"}
    secrets = {name: {"file": "%s/secrets/%s" % (cwd, name)} for name in ("ts_authkey", "hevy_api_key", "gateway_secret")}
    return {"name": "fcm-test", "services": dict(sorted(services.items())), "secrets": secrets}


def image(state, labels=None, nolabels=False, **extra):
    entry = {"id": new_id(state), "labels": None if nolabels else dict(COMPOSE_LABELS, **(labels or {}))}
    entry.update(extra)
    return entry
PY

cat > "$WORK/bin/docker" <<'PY'
#!/usr/bin/env python3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fakelib import FAKE, PREFIX, VERSIONED, COMPOSE_LABELS, config, image, load, new_id, path, save  # noqa: E402

argv = sys.argv[1:]


def log(line):
    for name in ("docker.log", "calls.log"):
        with open(path(name), "a") as handle:
            handle.write(line + "\n")


def unexpected():
    with open(os.environ["UNEXPECTED_LOG"], "a") as handle:
        handle.write("UNEXPECTED docker %s\n" % " ".join(argv))
    sys.exit(99)


def flag(name):
    return os.path.exists(path(name))


log("FCM_VERSION=%s %s" % (os.environ.get("FCM_VERSION", ""), " ".join(argv)))
state = load()
images = state["images"]
containers = state["containers"]

if argv == ["info"]:
    sys.exit(0)
elif argv == ["compose", "config", "--format", "json"]:
    import json
    print(json.dumps(config()))
elif argv == ["compose", "ps", "-q"]:
    for svc in sorted(containers):
        print(containers[svc]["cid"])
elif len(argv) == 4 and argv[:3] == ["compose", "ps", "-q"]:
    if argv[3] in containers:
        print(containers[argv[3]]["cid"])
elif len(argv) == 4 and argv[:2] == ["inspect", "--format"] and argv[2] == "{{.Image}} {{.Config.Image}}":
    for container in containers.values():
        if container["cid"] == argv[3]:
            print(container["image"], container["ref"])
            break
    else:
        print("Error: No such object", file=sys.stderr)
        sys.exit(1)
elif len(argv) == 3 and argv[:2] == ["image", "inspect"]:
    import json
    entry = images.get(argv[2])
    if entry is None or entry.get("broken"):
        print("[]")
        print("Error: No such image: %s" % argv[2], file=sys.stderr)
        sys.exit(1)
    config_part = {} if entry["labels"] is None else {"Labels": entry["labels"]}
    print(json.dumps([{"Id": entry["id"], "Config": config_part}]))
elif len(argv) == 6 and argv[:3] == ["image", "ls", "--filter"] and argv[3].startswith("reference=") \
        and argv[4:] == ["--format", "{{.Repository}}:{{.Tag}}"]:
    repo = argv[3][len("reference="):]
    for ref in sorted(images):
        if ref.startswith(repo + ":"):
            print(ref)
elif len(argv) == 2 and argv[0] == "rmi":
    failing = open(path("rmi_fail")).read().split() if flag("rmi_fail") else []
    if argv[1] in failing or argv[1] not in images:
        print("Error response from daemon: conflict: unable to remove %s" % argv[1], file=sys.stderr)
        sys.exit(1)
    del images[argv[1]]
    save(state)
    print("Untagged: " + argv[1])
elif len(argv) > 3 and argv[:3] == ["compose", "build", "--progress=plain"]:
    if flag("build_fail"):
        for number in range(1, 61):
            print("build error line %d" % number, file=sys.stderr)
        sys.exit(1)
    services = config()["services"]
    log("BUILD-ENV FCM_REVISION=%s FCM_CREATED=%s" % (os.environ.get("FCM_REVISION", ""), os.environ.get("FCM_CREATED", "")))
    for svc in argv[3:]:
        if svc not in services:
            unexpected()
        labels = {}
        if svc in VERSIONED:
            labels = {PREFIX + "version": os.environ.get("FCM_VERSION") or "local",
                      PREFIX + "revision": os.environ.get("FCM_REVISION", ""),
                      PREFIX + "created": os.environ.get("FCM_CREATED", "")}
        images[services[svc]["image"]] = image(state, labels)
    save(state)
elif argv == ["compose", "up", "-d", "--no-build", "--wait", "--wait-timeout", "300"]:
    services = config()["services"]
    for svc, entry in services.items():
        if entry["image"] not in images:
            print("Error: No such image: %s" % entry["image"], file=sys.stderr)
            sys.exit(1)
    for svc, entry in services.items():
        ref = entry["image"]
        current = containers.get(svc)
        if current is None:
            log("CREATE " + svc)
        elif current["image"] != images[ref]["id"] or current["ref"] != ref:
            log("RECREATE " + svc)
        else:
            continue
        containers[svc] = {"cid": "cid-" + new_id(state)[7:19], "image": images[ref]["id"], "ref": ref}
    save(state)
    if flag("up_fail"):
        print("container is unhealthy", file=sys.stderr)
        sys.exit(1)
elif len(argv) == 10 and argv[:9] == ["compose", "run", "--rm", "--no-deps", "-T", "--entrypoint", "python",
                                       "coach-mcp", "-c"]:
    if "PRAGMA user_version" not in argv[9] or "mode=ro" not in argv[9]:
        unexpected()
    print("Container fcm-test-coach-mcp-run Created", file=sys.stderr)
    if config()["services"]["coach-mcp"]["image"] not in images or flag("probe_fail"):
        sys.exit(1)
    print(open(path("db_schema")).read().strip() if flag("db_schema") else 9)
elif len(argv) == 9 and argv[:6] == ["run", "--rm", "--network", "none", "--entrypoint", "python"] and argv[7] == "-c":
    if argv[8] != "from coach_mcp.store import SCHEMA_VERSION; print(SCHEMA_VERSION)":
        unexpected()
    entry = images.get(argv[6])
    if entry is None or flag("probe_fail"):
        sys.exit(1)
    print(entry.get("schema", 9))
elif len(argv) == 8 and argv[:6] == ["run", "--rm", "--network", "none", "--entrypoint", "cat"] \
        and argv[7] == "/app/config.toml":
    entry = images.get(argv[6])
    if entry is None or flag("cat_fail"):
        sys.exit(1)
    if entry.get("config") is not None:
        sys.stdout.write(entry["config"])
    else:
        sys.stdout.write(open(os.path.join(os.getcwd(), "gateway", "config.toml")).read())
else:
    unexpected()
PY

# The case controller: sets up and reads the stub state without going through the docker stub.
cat > "$WORK/ctl.py" <<'PY'
#!/usr/bin/env python3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakelib import PREFIX, image, load, new_id, save  # noqa: E402

cmd, args = sys.argv[1], sys.argv[2:]
state = load()
images, containers = state["images"], state["containers"]
opts = dict(a.split("=", 1) for a in args if "=" in a)
pos = [a for a in args if "=" not in a]
LABELS = {"version": PREFIX + "version", "revision": PREFIX + "revision", "created": PREFIX + "created"}


def apply(entry):
    for key, value in opts.items():
        if key in LABELS:
            entry["labels"][LABELS[key]] = value
        elif key == "schema":
            entry["schema"] = int(value)
        elif key == "config":
            entry["config"] = value
        elif key == "broken":
            entry["broken"] = True


if cmd == "image":
    labels = {LABELS[k]: v for k, v in opts.items() if k in LABELS}
    entry = image(state, labels, nolabels="nolabels" in opts)
    if entry["labels"] is not None:
        apply(entry)
    elif "broken" in opts:
        entry["broken"] = True
    images[pos[0]] = entry
elif cmd == "setimg":
    apply(images[pos[0]])
elif cmd == "copy":
    entry = dict(images[pos[0]])
    entry["id"] = new_id(state)
    entry["labels"] = dict(entry["labels"])
    images[pos[1]] = entry
elif cmd == "container":
    svc, ref = pos
    image_id = images[ref]["id"] if "stale" not in opts else "sha256:" + "0" * 64
    containers[svc] = {"cid": "cid-" + new_id(state)[7:19], "image": image_id, "ref": opts.get("ref", ref)}
elif cmd == "stop":
    containers.pop(pos[0], None)
elif cmd == "rm":
    images.pop(pos[0], None)
elif cmd == "id":
    print(images[pos[0]]["id"] if pos[0] in images else "")
    sys.exit(0 if pos[0] in images else 1)
elif cmd == "running":
    print(containers[pos[0]]["image"] if pos[0] in containers else "")
    sys.exit(0)
elif cmd == "tags":
    print(" ".join(sorted(ref.split(":", 1)[1] for ref in images if ref.startswith(pos[0] + ":"))))
    sys.exit(0)
elif cmd == "label":
    print(images[pos[0]]["labels"].get(LABELS[pos[1]], ""))
    sys.exit(0)
else:
    sys.exit(64)
save(state)
PY

# A clock that goes up one second per `date -u +%Y-%m-%dT%H:%M:%SZ` call; any other date call is the real one.
cat > "$WORK/bin/date" <<SHIM
#!/bin/bash
if [[ "\$*" == "-u +%Y-%m-%dT%H:%M:%SZ" ]]; then
  now=1900000000
  [[ ! -f "\$FAKE/clock" ]] || now="\$(cat "\$FAKE/clock")"
  echo \$((now + 1)) > "\$FAKE/clock"
  exec $REAL_DATE -u -d "@\$now" +%Y-%m-%dT%H:%M:%SZ
fi
exec $REAL_DATE "\$@"
SHIM
chmod +x "$WORK/bin/docker" "$WORK/bin/date"

# --- the template repo: real git, one commit, copied for every case --------------------------------------------
TPL="$WORK/template"
mkdir -p "$TPL/scripts" "$TPL/tailscale" "$TPL/gateway" "$TPL/secrets"
cp "$SRC/scripts/lib.sh" "$SRC/scripts/deploy.sh" "$TPL/scripts/"
cat > "$TPL/scripts/backup.sh" <<'STUB'
#!/bin/bash
echo BACKUP >> "$FAKE/calls.log"
exit "$(cat "$FAKE/backup_rc" 2>/dev/null || echo 0)"
STUB
chmod +x "$TPL/scripts/"*.sh
printf '# fake compose file\n' > "$TPL/docker-compose.yml"
printf '{}\n' > "$TPL/tailscale/serve.json"
printf 'listen_host = "127.0.0.2"\n' > "$TPL/gateway/config.toml"
printf 'readme\n' > "$TPL/README.md"
printf '.env\n*.env\nsecrets/\n' > "$TPL/.gitignore"
for name in ts_authkey hevy_api_key gateway_secret; do printf 'x\n' > "$TPL/secrets/$name"; done
GIT=(git -c user.name=t -c user.email=t@example.test)
git -C "$TPL" init -q -b main
git -C "$TPL" add -A
"${GIT[@]}" -C "$TPL" commit -qm c0

# --- case helpers -----------------------------------------------------------------------------------------------
declare -A VER REV

# begin NAME: a fresh case with its own repo, stub state, default .env (mode 600) and no containers.
begin() {
  CASE="$WORK/cases/$1"
  REPO="$CASE/repo"
  FAKE="$CASE/fake"
  export FAKE
  mkdir -p "$CASE" "$FAKE"
  cp -a "$TPL" "$REPO"
  write_env 'BACKUP_DIR=/x'
}
# case_from NAME SOURCE: a case that starts as a copy of the case SOURCE (repo, .env, stub state).
case_from() {
  CASE="$WORK/cases/$1"
  REPO="$CASE/repo"
  FAKE="$CASE/fake"
  export FAKE
  cp -a "$WORK/cases/$2" "$CASE"
}

write_env() {
  { echo 'PUBLIC_BASE_URL=https://fitness-mcp.example.ts.net'; for line in "$@"; do echo "$line"; done; } > "$REPO/.env"
  chmod 600 "$REPO/.env"
}
ctl() { python3 "$WORK/ctl.py" "$@"; }
fake() { printf '%s\n' "${2:-x}" > "$FAKE/$1"; }
unfake() { rm -f "$FAKE/$1"; }
reset_logs() { : > "$FAKE/calls.log"; : > "$FAKE/docker.log"; }

# legacy: the state before the first scripts/deploy.sh: :0.1.0 images without labels, five running containers.
legacy() {
  local svc
  for svc in gateway garmin-mcp coach-mcp; do
    ctl image "$R/$svc:0.1.0"
    ctl container "$svc" "$R/$svc:0.1.0"
  done
  ctl image "$R/tailscale:1.0"; ctl image "$R/hevy-mcp:1.0"
  ctl container ts "$R/tailscale:1.0"; ctl container hevy-mcp "$R/hevy-mcp:1.0"
}

# commit NAME: a commit that changes README.md (or FILE, appended to); sets VER[NAME] and REV[NAME].
commit() {
  local file="${2:-README.md}"
  printf 'change %s\n' "$1" >> "$REPO/$file"
  "${GIT[@]}" -C "$REPO" commit -qam "$1"
  VER[$1]="$(git -C "$REPO" describe --tags --always --abbrev=12)"
  REV[$1]="$(git -C "$REPO" rev-parse HEAD)"
}

# run_deploy ARGS...: one run of scripts/deploy.sh under a clean environment; sets RC and the files out and err.
run_deploy() {
  cp "$REPO/.env" "$CASE/env.before" 2>/dev/null || true
  : > "$WORK/unexpected.log"
  touch "$FAKE/calls.log" "$FAKE/docker.log"
  RC=0
  env -i PATH="$WORK/bin:/usr/bin:/bin" HOME="$WORK/home" LANG=C.UTF-8 TMPDIR="$WORK/tmp" \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null FAKE="$FAKE" UNEXPECTED_LOG="$WORK/unexpected.log" \
    bash "$REPO/scripts/deploy.sh" "$@" > "$WORK/out" 2> "$WORK/err" < /dev/null || RC=$?
  check "no unexpected docker call" '[[ ! -s "$WORK/unexpected.log" ]]'
}
out_has() { grep -qF -- "$1" "$WORK/out" "$WORK/err" 2> /dev/null; }
n_calls() { grep -cE -- "$1" "$FAKE/calls.log" || true; }
env_same() { cmp -s "$REPO/.env" "$CASE/env.before"; }
# nochange: .env is byte-identical, nothing was built, started, removed or backed up.
nochange() { env_same && [[ "$(n_calls ' compose (build|up) | rmi |^BACKUP')" == 0 ]]; }
env_line() { grep -c "^$1\$" "$REPO/.env" || true; }
env_val() { sed -n "s/^$1=//p" "$REPO/.env"; }
ver_head() { git -C "$REPO" describe --tags --always --abbrev=12; }
running_is() { [[ "$(ctl running "$1")" == "$(ctl id "$2")" ]]; }
recreated() { grep -E '^RECREATE ' "$FAKE/calls.log" | cut -d' ' -f2 | sort | paste -sd' ' -; }

# --- T0: the environment of every run ----------------------------------------------------------------------------
echo "[T0: the stub docker shadows the real one]"
check "docker resolves to the stub" '[[ "$(env -i PATH="$WORK/bin:/usr/bin:/bin" bash -c "command -v docker")" == "$WORK/bin/docker" ]]'

# --- T1: first deploy from the legacy images ----------------------------------------------------------------------
echo "[T1: first deploy over the :0.1.0 images]"
begin t1
legacy
printf 'KEEP_ME=1\n' >> "$REPO/.env"
run_deploy
V="$(ver_head)"; REV1="$(git -C "$REPO" rev-parse HEAD)"
check "exit 0" '[[ $RC == 0 ]]'
check "version is the 12-hex abbreviated commit" '[[ "$V" == "$(git -C "$REPO" rev-parse --short=12 HEAD)" ]]'
check "backup, then build, then up" 'b=$(grep -n "^BACKUP" "$FAKE/calls.log" | head -n1 | cut -d: -f1); c=$(grep -n " compose build " "$FAKE/calls.log" | head -n1 | cut -d: -f1); u=$(grep -n " compose up " "$FAKE/calls.log" | head -n1 | cut -d: -f1); [[ -n "$b" && -n "$c" && -n "$u" && $b -lt $c && $c -lt $u ]]'
check "one build of exactly the three images with the version" '[[ "$(n_calls " compose build ")" == 1 ]] && grep -qxF "FCM_VERSION=$V compose build --progress=plain gateway garmin-mcp coach-mcp" "$FAKE/calls.log"'
check "revision is the full sha, created is a UTC time" 'grep -qE "^BUILD-ENV FCM_REVISION=$REV1 FCM_CREATED=[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z$" "$FAKE/calls.log"'
check "labels of the built image carry version, revision and created" '[[ "$(ctl label $R/gateway:$V version)" == "$V" && "$(ctl label $R/gateway:$V revision)" == "$REV1" && -n "$(ctl label $R/gateway:$V created)" ]]'
check ".env is the old bytes plus the two lines, mode 600" 'printf "PUBLIC_BASE_URL=https://fitness-mcp.example.ts.net\nBACKUP_DIR=/x\nKEEP_ME=1\nFCM_VERSION=%s\nFCM_PREVIOUS_VERSION=0.1.0\n" "$V" | cmp -s - "$REPO/.env" && [[ "$(stat -c %a "$REPO/.env")" == 600 ]]'
check "exactly the three versioned services are recreated" '[[ "$(recreated)" == "coach-mcp garmin-mcp gateway" ]]'
check "the containers run the new images" 'running_is gateway $R/gateway:$V && running_is coach-mcp $R/coach-mcp:$V && running_is garmin-mcp $R/garmin-mcp:$V'
check "the legacy images are kept" 'ctl id $R/gateway:0.1.0 >/dev/null && ctl id $R/garmin-mcp:0.1.0 >/dev/null && ctl id $R/coach-mcp:0.1.0 >/dev/null'
check "no image was removed" '[[ "$(n_calls " rmi ")" == 0 ]]'
check "the final line names the version and the previous one" 'out_has "Deployed $V (previous: 0.1.0)."'
check "no temporary file is left" '[[ -z "$(ls -A "$WORK/tmp")" && ! -e "$REPO/.deploy-tmp.env" ]]'
check "the checkout is still clean" '[[ -z "$(git -C "$REPO" status --porcelain)" ]]'
T1_CASE="$CASE"

echo "[T1b: ts and hevy-mcp run images their tags no longer point to]"
begin t1b
legacy
ctl container ts $R/tailscale:1.0 stale=1
ctl container hevy-mcp $R/hevy-mcp:1.0 stale=1
run_deploy
check "exit 0" '[[ $RC == 0 ]]'
check "all five services are recreated" '[[ "$(recreated)" == "coach-mcp garmin-mcp gateway hevy-mcp ts" ]]'
check "only the three versioned images are built" 'grep -q " compose build --progress=plain gateway garmin-mcp coach-mcp$" "$FAKE/calls.log" && [[ "$(n_calls " compose build ")" == 1 ]]'

echo "[T1c: the running coach-mcp comes from another repository: no previous version is guessed]"
begin t1c
legacy
ctl container coach-mcp $R/coach-mcp:0.1.0 ref=other.test/coach-mcp:0.1.0
run_deploy
V="$(ver_head)"
check "exit 0, FCM_VERSION written, no FCM_PREVIOUS_VERSION" '[[ $RC == 0 && "$(env_line "FCM_VERSION=$V")" == 1 && "$(env_line "FCM_PREVIOUS_VERSION=.*")" == 0 ]]'

# --- T2..T4: refusals before anything changes ---------------------------------------------------------------------
echo "[T2: uncommitted changes]"
begin t2
legacy
echo edit >> "$REPO/README.md"
run_deploy
check "exit 1, says uncommitted, nothing changed" '[[ $RC == 1 ]] && out_has "uncommitted" && nochange'
check "the changed file is listed" 'out_has "README.md"'

echo "[T2b: an untracked file]"
begin t2b
legacy
echo new > "$REPO/notes.txt"
run_deploy
check "exit 1, nothing changed" '[[ $RC == 1 ]] && out_has "uncommitted" && nochange'

echo "[T3: a secret file is missing]"
begin t3
legacy
rm "$REPO/secrets/gateway_secret"
run_deploy
check "exit 1, the path is in the message, nothing changed" '[[ $RC == 1 ]] && out_has "secrets/gateway_secret" && nochange'

echo "[T4: the git describe version is not a valid image tag]"
begin t4
legacy
git -C "$REPO" tag 'rel+1'
run_deploy
check "exit 1, not a valid image tag, nothing changed" '[[ $RC == 1 ]] && out_has "not a valid image tag" && nochange'

# --- T5: the backup -----------------------------------------------------------------------------------------------
echo "[T5: a failed backup refuses the deploy]"
begin t5
legacy
fake backup_rc 1
run_deploy
check "exit 1, one backup, no build, .env unchanged" '[[ $RC == 1 && "$(n_calls "^BACKUP")" == 1 && "$(n_calls " compose build ")" == 0 ]] && env_same && out_has "The backup failed"'

echo "[T5b: --no-backup deploys although the backup would fail]"
begin t5b
legacy
fake backup_rc 1
run_deploy --no-backup
check "exit 0, no backup, a warning" '[[ $RC == 0 && "$(n_calls "^BACKUP")" == 0 ]] && out_has "No backup before the deploy (--no-backup)."'

echo "[T5c: no BACKUP_DIR]"
begin t5c
write_env
legacy
run_deploy
check "exit 0, no backup" '[[ $RC == 0 && "$(n_calls "^BACKUP")" == 0 ]] && out_has "BACKUP_DIR is not set"'

echo "[T5d: BACKUP_DIR is set but nothing runs (first install)]"
begin t5d
run_deploy
V="$(ver_head)"
check "exit 0, no backup, an info line" '[[ $RC == 0 && "$(n_calls "^BACKUP")" == 0 ]] && out_has "Nothing of the stack is running"'
check "all five images are built (none existed), the services are created" 'grep -q " compose build --progress=plain gateway garmin-mcp coach-mcp hevy-mcp ts$" "$FAKE/calls.log" && [[ "$(n_calls "^CREATE ")" == 5 ]]'
check "no previous version on a first install" '[[ "$(env_line "FCM_VERSION=$V")" == 1 && "$(env_line "FCM_PREVIOUS_VERSION=.*")" == 0 ]]'

# ensure_chain4: legacy, then c1..c4 deployed once (c1 is already removed by the clean-up of c4); a retag of c1's
# coach image and a hand build of the gateway tag "local" (no created label) are taken along.
ensure_chain4() {
  local n
  if [[ ! -d "$WORK/cases/chain4" ]]; then
    begin chain4
    legacy
    ctl image "$R/gateway:local" version=local created=
    for n in c1 c2 c3 c4; do
      commit "$n"
      run_deploy
      check "deploy $n exit 0" '[[ $RC == 0 ]]'
      if [[ $n == c1 ]]; then ctl copy "$R/coach-mcp:${VER[c1]}" "$R/coach-mcp:pre-x"; fi
    done
    CHAIN_V=("${VER[c1]}" "${VER[c2]}" "${VER[c3]}" "${VER[c4]}")
  fi
}

# chain NAME [CLOCKJUMP]: a copy of chain4 plus the deploy of c5 (after a clock that went back an hour, if asked).
chain() {
  ensure_chain4
  case_from "$1" chain4
  VER[c1]="${CHAIN_V[0]}"; VER[c2]="${CHAIN_V[1]}"; VER[c3]="${CHAIN_V[2]}"; VER[c4]="${CHAIN_V[3]}"
  if [[ -n "${2:-}" ]]; then echo $(( $(cat "$FAKE/clock") - 3600 )) > "$FAKE/clock"; fi
  commit c5
  run_deploy
  check "deploy c5 exit 0" '[[ $RC == 0 ]]'
}

# --- T6, T7: failures after the preconditions ---------------------------------------------------------------------
echo "[T6: the build fails]"
begin t6
legacy
fake build_fail
run_deploy
LOG="$(sed -n 's/.*(log: \([^,]*\), last lines above).*/\1/p' "$WORK/err")"
check "exit 1, .env unchanged, nothing started" '[[ $RC == 1 && "$(n_calls " compose up ")" == 0 ]] && env_same'
check "the last log lines are shown" 'grep -q "build error line 60" "$WORK/err" && ! grep -q "build error line 10$" "$WORK/err"'
check "the log path is named and the log exists" '[[ -n "$LOG" && -f "$LOG" ]]'
rm -f "$LOG"

echo "[T7: a service does not become healthy (a state where a clean-up would remove an old version)]"
ensure_chain4
case_from t7 chain4
VER[c4]="${CHAIN_V[3]}"
commit c5
reset_logs
fake up_fail
run_deploy
V="$(ver_head)"
check "exit 1, .env has the new version" '[[ $RC == 1 && "$(env_val FCM_VERSION)" == "$V" && "$(env_val FCM_PREVIOUS_VERSION)" == "${VER[c4]}" ]]'
check "no clean-up after the failed start (c2 would have been removed)" '[[ "$(n_calls " rmi ")" == 0 ]] && ctl id $R/gateway:${CHAIN_V[1]} >/dev/null'
check "the output names the way back" 'out_has "scripts/deploy.sh --rollback"'
check "the status was printed" 'out_has "gateway: $R/gateway:$V"'

# --- T8: idempotence ----------------------------------------------------------------------------------------------
echo "[T8: the same commit again]"
CASE="$T1_CASE"; REPO="$CASE/repo"; FAKE="$CASE/fake"; export FAKE
V="$(ver_head)"
reset_logs
run_deploy
check "exit 0, no build, no recreate" '[[ $RC == 0 && "$(n_calls " compose build ")" == 0 && "$(recreated)" == "" ]]'
check "the previous version is kept" '[[ "$(env_val FCM_PREVIOUS_VERSION)" == 0.1.0 && "$(env_val FCM_VERSION)" == "$V" ]]'

echo "[T8b: an image under the tag that was not built from this commit is rebuilt]"
ctl setimg "$R/gateway:$V" revision=
reset_logs
run_deploy
check "only gateway is built" '[[ $RC == 0 ]] && grep -qxF "FCM_VERSION=$V compose build --progress=plain gateway" "$FAKE/calls.log"'

echo "[T8c: a missing ts image is built, the connectors are not touched]"
ctl rm "$R/tailscale:1.0"
reset_logs
run_deploy
check "only ts is built" '[[ $RC == 0 ]] && grep -qxF "FCM_VERSION=$V compose build --progress=plain ts" "$FAKE/calls.log"'

# --- T9: clean-up -------------------------------------------------------------------------------------------------
tags_of() { ctl tags "$R/$1"; }

echo "[T9: keep the running version, the previous one and one more]"
chain t9
check "each repository keeps the deploy-built c5, c4, c3 and the images that are not deploy-built" \
  '[[ "$(tags_of gateway)" == "$(printf "%s\n" 0.1.0 local ${VER[c3]} ${VER[c4]} ${VER[c5]} | sort | paste -sd" " -)" && "$(tags_of garmin-mcp)" == "$(printf "%s\n" 0.1.0 ${VER[c3]} ${VER[c4]} ${VER[c5]} | sort | paste -sd" " -)" ]]'
check "coach-mcp also keeps the retag" '[[ "$(tags_of coach-mcp)" == "$(printf "%s\n" 0.1.0 pre-x ${VER[c3]} ${VER[c4]} ${VER[c5]} | sort | paste -sd" " -)" ]]'
check "c1 and c2 of every repository were removed, nothing else" \
  '[[ "$(grep -cE "^FCM_VERSION=[^ ]* rmi " "$FAKE/calls.log")" == 6 ]] && for s in gateway garmin-mcp coach-mcp; do for c in c1 c2; do grep -qE " rmi $R/$s:${VER[$c]}$" "$FAKE/calls.log" || exit 1; done; done'
check "ts and hevy-mcp images are kept" 'ctl id $R/tailscale:1.0 >/dev/null && ctl id $R/hevy-mcp:1.0 >/dev/null'
check "the previous version recorded is c4" '[[ "$(env_val FCM_PREVIOUS_VERSION)" == "${VER[c4]}" ]]'

echo "[T9b: the clock goes back an hour before the last deploy]"
chain t9b jump
check "the current version is never a candidate: c5, c4 and c3 are kept" \
  '[[ "$(tags_of gateway)" == "$(printf "%s\n" 0.1.0 local ${VER[c3]} ${VER[c4]} ${VER[c5]} | sort | paste -sd" " -)" ]]'

echo "[T9c: an image that cannot be removed stays and the deploy still succeeds]"
begin t9c
legacy
commit c1; run_deploy
fake rmi_fail "$R/gateway:${VER[c1]}"
for n in c2 c3 c4; do commit "$n"; run_deploy; done
check "the last deploy exits 0" '[[ $RC == 0 ]]'
check "a Could not remove line names the image" 'out_has "Could not remove $R/gateway:${VER[c1]}"'
check "the image stays, the others of c1 are gone" 'ctl id $R/gateway:${VER[c1]} >/dev/null && ! ctl id $R/garmin-mcp:${VER[c1]} >/dev/null'

# --- T10, T11: rollback -------------------------------------------------------------------------------------------
# two_deploys NAME: legacy, c1 and c2 deployed (README changes only). The state is built once and copied.
two_deploys() {
  if [[ ! -d "$WORK/cases/two" ]]; then
    begin two
    legacy
    commit c1; run_deploy
    commit c2; run_deploy
    check "both deploys exit 0 (c2 is the current version)" '[[ $RC == 0 && "$(env_val FCM_VERSION)" == "${VER[c2]}" ]]'
    TWO_V=("${VER[c1]}" "${VER[c2]}"); TWO_R=("${REV[c1]}" "${REV[c2]}")
  fi
  case_from "$1" two
  VER[c1]="${TWO_V[0]}"; VER[c2]="${TWO_V[1]}"; REV[c1]="${TWO_R[0]}"; REV[c2]="${TWO_R[1]}"
}

echo "[T10: rollback to the previous version]"
two_deploys t10
reset_logs
run_deploy --rollback
check "exit 0 (T11c2: a change of README.md alone does not refuse)" '[[ $RC == 0 ]]'
check ".env points to c1 and keeps c2 as the previous version" '[[ "$(env_val FCM_VERSION)" == "${VER[c1]}" && "$(env_val FCM_PREVIOUS_VERSION)" == "${VER[c2]}" ]]'
check "no build, no backup, no clean-up" '[[ "$(n_calls " compose build ")" == 0 && "$(n_calls "^BACKUP")" == 0 && "$(n_calls " rmi ")" == 0 ]]'
check "the three containers run c1s images, only they are recreated" \
  'running_is gateway $R/gateway:${VER[c1]} && running_is garmin-mcp $R/garmin-mcp:${VER[c1]} && running_is coach-mcp $R/coach-mcp:${VER[c1]} && [[ "$(recreated)" == "coach-mcp garmin-mcp gateway" ]]'
check "up runs with the target version" 'grep -qxF "FCM_VERSION=${VER[c1]} compose up -d --no-build --wait --wait-timeout 300" "$FAKE/calls.log"'
check "the end line names both versions" 'out_has "Rolled back to ${VER[c1]} (previous: ${VER[c2]})."'
reset_logs
run_deploy --rollback
check "a second rollback switches forward again" '[[ $RC == 0 && "$(env_val FCM_VERSION)" == "${VER[c2]}" && "$(env_val FCM_PREVIOUS_VERSION)" == "${VER[c1]}" ]] && running_is gateway $R/gateway:${VER[c2]}'

echo "[T10b: rollback to a named version]"
two_deploys t10b
commit c3; run_deploy
run_deploy --rollback "${VER[c1]}"
check "exit 0, the named version is configured, the current one is the previous" '[[ $RC == 0 && "$(env_val FCM_VERSION)" == "${VER[c1]}" && "$(env_val FCM_PREVIOUS_VERSION)" == "${VER[c3]}" ]]'

echo "[T11a: the images of the target are gone]"
two_deploys t11a
for s in gateway garmin-mcp coach-mcp; do ctl rm "$R/$s:${VER[c1]}"; done
reset_logs
run_deploy --rollback
check "exit 1, the three references are listed, nothing changed" \
  '[[ $RC == 1 ]] && out_has "$R/gateway:${VER[c1]}" && out_has "$R/garmin-mcp:${VER[c1]}" && out_has "$R/coach-mcp:${VER[c1]}" && out_has "are not kept" && nochange'

echo "[T11b: docker-compose.yml changed between the commits]"
begin t11b
legacy
commit c1; run_deploy
commit c2 docker-compose.yml; run_deploy
reset_logs
run_deploy --rollback
check "exit 1, the file is listed, nothing changed" '[[ $RC == 1 ]] && out_has "docker-compose.yml" && out_has "since then these files changed" && nochange'

echo "[T11c: a file the compose file mounts changed between the commits]"
begin t11c
legacy
commit c1; run_deploy
commit c2 tailscale/serve.json; run_deploy
reset_logs
run_deploy --rollback
check "exit 1, the mounted file is listed, nothing changed" '[[ $RC == 1 ]] && out_has "tailscale/serve.json" && ! out_has "docker-compose.yml" && nochange'

echo "[T11d: the database is newer than the target coach-mcp]"
two_deploys t11d
ctl setimg "$R/coach-mcp:${VER[c1]}" schema=8
fake db_schema 9
reset_logs
run_deploy --rollback
check "exit 1, both numbers and the procedure are named, nothing changed" \
  '[[ $RC == 1 ]] && out_has "schema 9" && out_has "supports 8" && out_has "Going back to a version without" && nochange'

echo "[T11d2: the database has the schema of the target]"
two_deploys t11d2
ctl setimg "$R/coach-mcp:${VER[c1]}" schema=8
fake db_schema 8
run_deploy --rollback
check "exit 0" '[[ $RC == 0 ]]'

echo "[T11d3: the database is older than the target]"
two_deploys t11d3
ctl setimg "$R/coach-mcp:${VER[c1]}" schema=8
fake db_schema 7
run_deploy --rollback
check "exit 0" '[[ $RC == 0 ]]'

echo "[T11e: the three images of the target come from different commits]"
two_deploys t11e
ctl setimg "$R/garmin-mcp:${VER[c1]}" revision="${REV[c2]}"
reset_logs
run_deploy --rollback
check "exit 1, different commits, nothing changed" '[[ $RC == 1 ]] && out_has "different commits" && nochange'

echo "[T11f: the target is the configured version]"
two_deploys t11f
reset_logs
run_deploy --rollback "${VER[c2]}"
check "exit 1, nothing changed" '[[ $RC == 1 ]] && out_has "already the configured version" && nochange'

echo "[T11g: no previous version recorded]"
two_deploys t11g
sed -i '/^FCM_PREVIOUS_VERSION=/d' "$REPO/.env"
reset_logs
run_deploy --rollback
check "exit 1, the message names --rollback <version>" '[[ $RC == 1 ]] && out_has "--rollback <version>" && nochange'

echo "[T11g2: nothing deployed by the script yet]"
begin t11g2
legacy
run_deploy --rollback
check "exit 1, nothing changed" '[[ $RC == 1 ]] && out_has "No version deployed by scripts/deploy.sh yet" && nochange'

echo "[T11h: the previous version is a legacy one and no version is named]"
CASE="$T1_CASE"; REPO="$CASE/repo"; FAKE="$CASE/fake"; export FAKE
reset_logs
run_deploy --rollback
check "exit 1, was not built by scripts/deploy.sh, nothing changed" '[[ $RC == 1 ]] && out_has "was not built by scripts/deploy.sh" && nochange'

echo "[T11h2: a legacy version is named and its gateway config equals the checkout's]"
begin t11h2
legacy
commit c1; run_deploy
commit c2 docker-compose.yml
reset_logs
run_deploy --rollback 0.1.0
check "exit 0 with the legacy warning" '[[ $RC == 0 ]] && out_has "carry no commit"'
check "no file comparison refusal, the containers run :0.1.0" '! out_has "since then these files changed" && running_is gateway $R/gateway:0.1.0 && running_is coach-mcp $R/coach-mcp:0.1.0'
check "the version configured is 0.1.0" '[[ "$(env_val FCM_VERSION)" == 0.1.0 ]]'

echo "[T11h3: a legacy gateway with another config.toml is refused]"
begin t11h3
legacy
commit c1; run_deploy
ctl setimg "$R/gateway:0.1.0" config='listen_host = "127.0.0.1"'
reset_logs
run_deploy --rollback 0.1.0
check "exit 1, the legacy-config text, nothing changed" '[[ $RC == 1 ]] && out_has "was built with another gateway/config.toml" && nochange'

echo "[T11h4: reading the config of a legacy gateway fails]"
begin t11h4
legacy
commit c1; run_deploy
fake cat_fail
reset_logs
run_deploy --rollback 0.1.0
check "exit 1, the legacy-config text, nothing changed" '[[ $RC == 1 ]] && out_has "was built with another gateway/config.toml" && nochange'

echo "[T11i: the commit of the target is not in this checkout]"
two_deploys t11i
for s in gateway garmin-mcp coach-mcp; do ctl setimg "$R/$s:${VER[c1]}" revision=0123456789abcdef0123456789abcdef01234567; done
reset_logs
run_deploy --rollback
check "exit 1, not in this checkout, nothing changed" '[[ $RC == 1 ]] && out_has "not in this checkout" && nochange'

echo "[T11j: a dirty checkout]"
two_deploys t11j
echo edit >> "$REPO/README.md"
reset_logs
run_deploy --rollback
check "exit 1, nothing changed" '[[ $RC == 1 ]] && out_has "uncommitted" && nochange'

echo "[T11k: the schema version cannot be read]"
two_deploys t11k
fake probe_fail
reset_logs
run_deploy --rollback
check "exit 1, Cannot read the schema version, nothing changed" '[[ $RC == 1 ]] && out_has "Cannot read the schema version" && nochange'

echo "[T11l: the rollback fails to start the target]"
two_deploys t11l
fake up_fail
reset_logs
run_deploy --rollback
check "exit 1, the rolled-back version stays configured" '[[ $RC == 1 && "$(env_val FCM_VERSION)" == "${VER[c1]}" ]] && out_has "stays configured"'

# --- T12: --status ------------------------------------------------------------------------------------------------
only_status_calls() {   # every docker call of the run is a read-only one
  ! grep -vE '^(RECREATE|CREATE|BUILD-ENV|BACKUP)' "$FAKE/docker.log" \
    | grep -vE '^FCM_VERSION=[^ ]* (info|compose config |compose ps |inspect |image inspect |image ls )' | grep -q .
}

echo "[T12: --status]"
CASE="$T1_CASE"; REPO="$CASE/repo"; FAKE="$CASE/fake"; export FAKE
V="$(ver_head)"
reset_logs
run_deploy --status
check "exit 0 and every service is running" '[[ $RC == 0 ]] && [[ "$(grep -c " running" "$WORK/out")" == 5 ]]'
check "the three show their commit" 'grep -qE "^gateway: $R/gateway:$V running, commit ${REV1:0:12}$" "$WORK/out" && grep -qE "^coach-mcp: .* running, commit ${REV1:0:12}$" "$WORK/out" && grep -qE "^garmin-mcp: .* running, commit ${REV1:0:12}$" "$WORK/out"'
check "ts and hevy-mcp are running without a commit" 'grep -qxF "ts: $R/tailscale:1.0 running" "$WORK/out" && grep -qxF "hevy-mcp: $R/hevy-mcp:1.0 running" "$WORK/out"'
check "the previous version and the kept versions are listed" 'grep -qxF "Previous version: 0.1.0" "$WORK/out" && grep -qxF "Kept gateway: $V" "$WORK/out" && grep -qxF "Kept coach-mcp: $V" "$WORK/out"'
check "only read-only calls, .env unchanged" 'only_status_calls && env_same'

ctl container gateway "$R/gateway:0.1.0"
reset_logs
run_deploy --status
check "a container on another image: exit 1 and DIFFERENT" '[[ $RC == 1 ]] && grep -qE "^gateway: .* DIFFERENT: runs [0-9a-f]{12}, configured image is [0-9a-f]{12}$" "$WORK/out" && only_status_calls'
ctl container gateway "$R/gateway:$V"

ctl stop coach-mcp
reset_logs
run_deploy --status
check "a stopped service: exit 1 and not running" '[[ $RC == 1 ]] && grep -qxF "coach-mcp: $R/coach-mcp:$V not running" "$WORK/out" && only_status_calls'

ctl container coach-mcp "$R/coach-mcp:$V"
ctl setimg "$R/garmin-mcp:$V" revision=
reset_logs
run_deploy --status
check "an image without a commit label: exit 1 and not built by scripts/deploy.sh" '[[ $RC == 1 ]] && grep -qxF "garmin-mcp: $R/garmin-mcp:$V running, not built by scripts/deploy.sh" "$WORK/out"'
ctl setimg "$R/garmin-mcp:$V" revision="$REV1"

ctl image "$R/coach-mcp:oldx" version=oldx created=2000-01-01T00:00:00Z broken=1
reset_logs
run_deploy --status
check "an unreadable listed tag is skipped, no traceback" '[[ $RC == 0 ]] && ! grep -q "Traceback" "$WORK/err" && grep -qxF "Kept coach-mcp: $V" "$WORK/out"'
ctl setimg "$R/gateway:$V" broken=1
reset_logs
run_deploy --status
check "a configured image that cannot be read is image missing, no traceback" '[[ $RC == 1 ]] && grep -qxF "gateway: $R/gateway:$V image missing" "$WORK/out" && ! grep -q "Traceback" "$WORK/err"'

echo "[T12b: --status before the first deploy]"
begin t12b
legacy
run_deploy --status
check "exit 1, image missing for the three" '[[ $RC == 1 && "$(grep -c " image missing" "$WORK/out")" == 3 ]] && grep -qxF "gateway: $R/gateway:local image missing" "$WORK/out"'
check "ts and hevy-mcp run as configured" 'grep -qxF "ts: $R/tailscale:1.0 running" "$WORK/out"'
check "Previous version: none, only read-only calls, .env unchanged" 'grep -qxF "Previous version: none" "$WORK/out" && only_status_calls && env_same'

echo "[T12c: --status before the first deploy, with containers of repointed tags]"
begin t12c
legacy
ctl container ts $R/tailscale:1.0 stale=1
ctl container hevy-mcp $R/hevy-mcp:1.0 stale=1
run_deploy --status
check "exit 1, DIFFERENT for ts and hevy-mcp" '[[ $RC == 1 ]] && grep -qE "^ts: .* DIFFERENT:" "$WORK/out" && grep -qE "^hevy-mcp: .* DIFFERENT:" "$WORK/out"'

# --- T13: .env handling -------------------------------------------------------------------------------------------
echo "[T13: two FCM_VERSION lines]"
begin t13
legacy
printf 'FCM_VERSION=a\nFCM_VERSION=b\n' >> "$REPO/.env"
run_deploy
check "exit 1 before the backup, nothing changed" '[[ $RC == 1 ]] && out_has "more than one FCM_VERSION= line" && nochange'

echo "[T13b: .env without a final newline]"
begin t13b
legacy
printf 'PUBLIC_BASE_URL=https://fitness-mcp.example.ts.net\nBACKUP_DIR=/x' > "$REPO/.env"
run_deploy
V="$(ver_head)"
check "old bytes, a newline, then the two lines" '[[ $RC == 0 ]] && printf "PUBLIC_BASE_URL=https://fitness-mcp.example.ts.net\nBACKUP_DIR=/x\nFCM_VERSION=%s\nFCM_PREVIOUS_VERSION=0.1.0\n" "$V" | cmp -s - "$REPO/.env"'

echo "[T13c: a quoted FCM_VERSION is the previous version]"
begin t13c
legacy
printf 'FCM_VERSION="c1"\n' >> "$REPO/.env"
run_deploy
V="$(ver_head)"
check "exit 0, the version replaced, the quoted value is the previous one" '[[ $RC == 0 && "$(env_val FCM_VERSION)" == "$V" && "$(env_val FCM_PREVIOUS_VERSION)" == c1 && "$(env_line "FCM_VERSION=.*")" == 1 ]]'

echo "[T13d: .env is a symlink]"
begin t13d
legacy
mv "$REPO/.env" "$REPO/real.env"
ln -s real.env "$REPO/.env"
run_deploy
check "exit 1, nothing changed, the link is still a link" '[[ $RC == 1 ]] && out_has "is a symlink" && [[ -L "$REPO/.env" ]] && nochange'

echo "[T13e: other keys and the file mode survive a switch]"
begin t13e
legacy
chmod 640 "$REPO/.env"
printf 'FCM_VERSION=old\nFCM_PREVIOUS_VERSION=older\nTAIL=1\n' >> "$REPO/.env"
run_deploy
V="$(ver_head)"
check "lines replaced in place, the rest and the mode unchanged" 'printf "PUBLIC_BASE_URL=https://fitness-mcp.example.ts.net\nBACKUP_DIR=/x\nFCM_VERSION=%s\nFCM_PREVIOUS_VERSION=old\nTAIL=1\n" "$V" | cmp -s - "$REPO/.env" && [[ "$(stat -c %a "$REPO/.env")" == 640 ]]'

# --- T14, T15: arguments and the static check ---------------------------------------------------------------------
echo "[T14: bad arguments]"
begin t14
legacy
for args in "--bogus" "--status x" "--rollback a b" "--rollback --rollback" "--no-backup --status" "--status --rollback" "--no-backup --rollback"; do
  # shellcheck disable=SC2086
  run_deploy $args
  check "exit 2 and usage for: $args" '[[ $RC == 2 ]] && grep -q "scripts/deploy.sh --rollback" "$WORK/err" && [[ "$(n_calls " compose (build|up) ")" == 0 ]]'
done

echo "[T15: no project or image is named in the script]"
check "no fitness-coach-mcp in scripts/deploy.sh" '[[ "$(grep -c fitness-coach-mcp "$SRC/scripts/deploy.sh" || true)" == 0 ]]'
check "the copy under test is the script" 'cmp -s "$SRC/scripts/deploy.sh" "$TPL/scripts/deploy.sh"'

echo
if [[ $FAILED == 0 ]]; then echo "DEPLOY OK"; else echo "DEPLOY FAILED"; exit 1; fi
