# MAO — project configuration fitness-coach-mcp

Project layer of the `/mao` skill (`~/.claude/skills/mao/`). Whatever stands here
takes precedence over the skill's general procedure. Do not copy the repo's
own rules (`README.md`) here — only link to them.

Stack: Python >= 3.13 MCP servers in Docker (`garmin-mcp/`, `gateway/`,
`hevy-mcp/`, `coach-mcp/`), hash-locked dependencies, no UI. The host has only Python 3.12,
no pytest/ruff/uv/just/make — tests run inside Docker.

## Files

- Plan: `mao-plan.md` (repo root; the only name that differs from the defaults)
- Checklist: `.claude/mao/checklist.md`
- Lessons: `.claude/mao/lessons.md`
- Host gate: none (details are in "Test host" below)

## Plan

- File: `mao-plan.md`
- Decisions and boundaries: `## 1.`
- Common PR conditions: `## 2.`
- PR table: `## 3.`; row `| NN | <name> | <depends on> | <status> |`,
  read the number with the regex `^\| (\d{2}) \|`
- Chapters: `### PR NN — <name>`; a new one goes after the last `### PR`
  and before `## 5.`
- Open items: `## 5.`

## Commands

All commands run from ROOT (`/home/ubuntu/dev/fitness-coach-mcp`), or from the
PR's worktree where noted.

- `docker-compose.yml` fixes `name: fitness-coach-mcp`, so `docker compose`
  without `-p` targets the live project from any directory. When a PR's code
  lives in a separate worktree, run only `scripts/check.sh`, `docker build`,
  `docker run`, `scripts/lock-deps.sh` and `scripts/e2e-test.sh` there (from
  the worktree); `scripts/setup-secrets.sh`, `healthcheck.sh`, `smoke-test.sh`,
  `admin.sh` and `garmin-login.sh` run only in the live checkout.

- Check (must be green before the PR), one command, from the PR's worktree:
  `scripts/check.sh --tag pr<NN>` (~3 min on an idle host). It runs, in order,
  each step under `flock /tmp/fcm-docker.lock`:
  1. `docker build --target test --no-cache-filter test` of `garmin-mcp`,
     `gateway` and `coach-mcp` (images `fcm-<svc>-check-pr<NN>`), reading the
     pytest "N passed" count from each build log;
  2. `scripts/e2e-test.sh` with `E2E_PROJECT=fcm-e2e-pr<NN> E2E_TAG=pr<NN>`
     (builds all images as `fcm-e2e/<svc>:pr<NN>`, throwaway secrets, no
     internet/Tailscale), requiring "E2E OK" and "E2E backup OK".
  Logs go to a temp dir printed at the start; a failure names the step and
  prints the last 40 lines of its log. The evidence is the last line:
  `CHECK OK tree=<12 chars of HEAD^{tree}>[-dirty] garmin=<n> gateway=<n> coach=<n> e2e=OK`.
  `--only garmin|gateway|coach|e2e` (repeatable) runs a subset and ends with
  `CHECK PARTIAL`, which is not the check. The script removes its images
  unless `--keep-images`. The check forces the test stage, so the suite really
  runs; compare `tree=` with `git rev-parse --short=12 'HEAD^{tree}'` to know
  which tree was checked (`-dirty` = uncommitted changes; commit, then check).
  A new service adds its own step to `scripts/check.sh`.
- Targeted tests (tests are baked into the image, rebuild after edits), with
  PR-private tags; build and run in one lock hold:
  `flock /tmp/fcm-docker.lock sh -c 'docker build --target test -t fcm-garmin-test-pr<NN> garmin-mcp && docker run --rm --network none --entrypoint python fcm-garmin-test-pr<NN> -m pytest tests/<file>.py -k <expr>'`
  and for coach-mcp:
  `flock /tmp/fcm-docker.lock sh -c 'docker build --target test -t fcm-coach-test-pr<NN> coach-mcp && docker run --rm --network none --entrypoint python fcm-coach-test-pr<NN> -m pytest tests/<file>.py -k <expr>'`
  (gateway: `/venv/bin/python` instead of `python`). Remove the image after use.
- Dependency locking: change dependencies only via `scripts/lock-deps.sh`
  (regenerates the `requirements*.lock` hash locks); never edit a lock by hand.
- Forbidden (this machine runs the LIVE stack, compose project
  `fitness-coach-mcp`, and holds real secrets):
  - `docker compose up|down|restart|build|run` without `-p` (live project);
    `docker compose down -v` on the live project (volumes hold Garmin tokens
    and the gateway DB);
  - `scripts/setup-secrets.sh`, `scripts/garmin-login.sh`,
    `scripts/admin.sh grants revoke`, `docker compose run ... login`;
  - reading or printing `.env`, `secrets/*`, `data/*`.
  `scripts/check.sh` and `scripts/e2e-test.sh` are safe (own projects
  `fcm-e2e*`; `E2E_PROJECT` must be `fcm-e2e` or `fcm-e2e-<suffix>`).

## Parallel runs

Several PRs may be implemented at once in separate worktrees (background:
`.claude/mao/lessons.md`, "Parallel runs").

- At most 2 concurrent implementers on this machine (2 CPUs, 11 GB).
- Every `docker build` and every e2e run holds `flock /tmp/fcm-docker.lock`
  (`scripts/check.sh` does it itself; `CHECK_LOCK=0` only for a deliberate
  parallel test). Waiting for the lock is normal; never kill another PR's build.
- Images a PR builds carry its number: `fcm-<svc>-<purpose>-pr<NN>`
  (`fcm-coach-test-pr07`, reviewers `fcm-coach-review-pr07`), never a bare
  `fcm-coach-test`. Images built by another PR or an earlier run are no evidence.
- The e2e stack of a PR runs as `E2E_PROJECT=fcm-e2e-pr<NN> E2E_TAG=pr<NN>`
  (what `scripts/check.sh --tag pr<NN>` passes). The default project `fcm-e2e`
  and the `fcm-e2e/*:test` images belong to nobody: whatever they hold may be
  another branch's code.
- Migration numbers are renumbered mechanically on rebase: the PR that merges
  later takes the next free number after `main`.
- After the base branch of a local branch was squash-merged, rebase with
  `git rebase --onto origin/main <old-base-tip>`, not a plain
  `git rebase origin/main`.
- A rebase that leaves the tree identical (`git diff <checked> HEAD` is empty,
  so the `tree=` of the CHECK line still matches) keeps the check valid; any
  other rebase or conflict resolution runs `scripts/check.sh` again.

## Git

- Base branch: `main`; remote: `origin`
- Run branch: `mao/pr<NN>-<slug>`
- Commits: English, short imperative sentence, no Conventional Commits
  prefix (e.g. "Add Garmin Connect MCP server"); trailer
  `Co-Authored-By: <model name> <noreply@anthropic.com>`
- PR title: `PR NN — <name from the table>`. The repo squash setting is
  `COMMIT_OR_PR_TITLE`: a single-commit PR lands on `main` with the commit
  title, a multi-commit PR with the PR title.
- Merge: `gh pr merge --squash --delete-branch`
- CI checks: none (no `.github/`); the local check above is the only gate.
  Do not add CI.

## Test host

Profile `live` = the live docker compose stack on this machine (project
`fitness-coach-mcp`: `ts`, `gateway`, `hevy-mcp`, `garmin-mcp`, `coach-mcp`).

- Plan criteria marked "On the test host" are verified with
  `scripts/smoke-test.sh` (tools/list on all three endpoints through the tailnet,
  last 3 Hevy workouts, today's Garmin recovery snapshot).
- The worker verifies the profile is up: `docker compose ps` in ROOT shows
  `ts`, `gateway`, `hevy-mcp`, `garmin-mcp`, `coach-mcp` all healthy, and the garmin
  `/healthz` returns `{"status":"ok","garmin_auth":"ok"}`.
- EVERY deploy to the host (`docker compose up -d --build <svc>` or anything
  that restarts live services) is stop-and-ask; smoke-test criteria are
  checked only after the user approves the deploy.
- Without the profile (not requested or not up), integration criteria are
  marked "requires test host".

## Preview

None.

## Stop-and-ask

Always ask the user before:
- any deploy to the live stack (see "Test host") or any `docker compose` command
  on the live project;
- touching secrets: never read or commit `.env`, `secrets/`, `data/`, Garmin
  tokens; `scripts/setup-secrets.sh`, `scripts/garmin-login.sh`, mutating
  `scripts/admin.sh` subcommands;
- deleting data (volumes, the gateway DB, tokens);
- force push;
- calls that write to the athlete's real Garmin/Hevy data (`create_workout`,
  `schedule_workout`, `unschedule_workout`, Hevy create-*/update-*);
- adding a Python dependency or changing a pinned image digest (the spec must
  give a reason; re-lock via `scripts/lock-deps.sh`).

## Role rules

Rules live in the plan and docs, linked here, not copied: `mao-plan.md`
sections 1 and 2; `README.md` (Czech, operations and architecture);
`garmin-mcp/README.md` (tool table, env); `docs/coach-instructions.md`
(Czech). Czech docs stay Czech; code, comments, commits and the plan are English.

### Designer (in addition to the skill's spec)
- New Garmin tools follow the pattern in
  `garmin-mcp/src/garmin_coach_mcp/server.py` (`tool(name, _read|_write(...))`,
  English docstring = description, fail-soft sections, library calls through
  `GarminService.call` / `call_many`).
- A tool-list change touches `garmin-mcp/tests/test_http.py` (`EXPECTED_TOOLS`,
  `WRITE_TOOLS`), `garmin-mcp/README.md` tool table, `README.md`,
  `docs/coach-instructions.md` and the `INSTRUCTIONS` string in `server.py`;
  for coach-mcp: `coach-mcp/tests/test_http.py` (`EXPECTED_TOOLS`,
  `WRITE_TOOLS`), `coach-mcp/README.md`, `INSTRUCTIONS` in
  `coach-mcp/src/coach_mcp/server.py`.
- Logs and audit never contain arguments, results or health values.

### Worker (additional spec checks)
- Verify every `garminconnect` method named in a spec against the library
  installed in the image, built under a PR-private tag in one lock hold:
  `flock /tmp/fcm-docker.lock sh -c 'docker build --target test -t fcm-garmin-spec-pr<NN> garmin-mcp && docker run --rm --network none --entrypoint python fcm-garmin-spec-pr<NN> -c "import garminconnect, inspect; ..."'`
  (the host has no library).
- Fixtures named in a spec exist in `garmin-mcp/tests/fixtures.py` or are
  marked new; `FakeGarmin.responses` in `garmin-mcp/tests/conftest.py` has a
  key for every new `get_*` call.

### Implementer
- Tests use only the fake client and canned payloads; no network.
- No secrets, personal hostnames or account ids in the repo (plan section 2).

### Reviewer (repo rules)
- Check plan section 1 (curated tools, log privacy, supply chain, language)
  and section 2 conditions; confirm `scripts/check.sh --tag pr<NN>` ended
  with `CHECK OK` for the reviewed tree (`tree=`, no `-dirty`) and no secrets
  or hostnames are in the diff.
