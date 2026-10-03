# MAO — project configuration fitness-coach-mcp

Project layer of the `/mao` skill (`~/.claude/skills/mao/`). Whatever stands here
takes precedence over the skill's general procedure. Do not copy the repo's
own rules (`README.md`) here — only link to them.

Stack: Python >= 3.13 MCP servers in Docker (`garmin-mcp/`, `gateway/`,
`hevy-mcp/`), hash-locked dependencies, no UI. The host has only Python 3.12,
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

All commands run from ROOT (`/home/ubuntu/dev/fitness-coach-mcp`).

- Check (must be green before the PR), all three:
  1. `docker build --target test garmin-mcp`
  2. `docker build --target test gateway`
  3. `scripts/e2e-test.sh` (builds all images, separate compose project
     `fcm-e2e`, throwaway secrets, no internet/Tailscale; ~50 s; ends with
     "E2E OK")
  A new service adds its own `docker build --target test <svc>`. Docker layer
  cache can re-print a cached result for an unchanged tree; that is fine.
- Targeted tests (tests are baked into the image, rebuild after edits):
  `docker build --target test -t fcm-garmin-test garmin-mcp && docker run --rm --network none --entrypoint python fcm-garmin-test -m pytest tests/<file>.py -k <expr>`
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
  `scripts/e2e-test.sh` is safe (own project `fcm-e2e`).

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
`fitness-coach-mcp`: `ts`, `gateway`, `hevy-mcp`, `garmin-mcp`).

- Plan criteria marked "On the test host" are verified with
  `scripts/smoke-test.sh` (tools/list on both endpoints through the tailnet,
  last 3 Hevy workouts, today's Garmin recovery snapshot).
- The worker verifies the profile is up: `docker compose ps` in ROOT shows
  `ts`, `gateway`, `hevy-mcp`, `garmin-mcp` all healthy, and the garmin
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
  `docs/coach-instructions.md` and the `INSTRUCTIONS` string in `server.py`.
- Logs and audit never contain arguments, results or health values.

### Worker (additional spec checks)
- Verify every `garminconnect` method named in a spec against the library
  installed in the image: `docker run --rm --network none --entrypoint python fcm-garmin-test -c "import garminconnect, inspect; ..."`
  (the host has no library).
- Fixtures named in a spec exist in `garmin-mcp/tests/fixtures.py` or are
  marked new; `FakeGarmin.responses` in `garmin-mcp/tests/conftest.py` has a
  key for every new `get_*` call.

### Implementer
- Tests use only the fake client and canned payloads; no network.
- No secrets, personal hostnames or account ids in the repo (plan section 2).

### Reviewer (repo rules)
- Check plan section 1 (curated tools, log privacy, supply chain, language)
  and section 2 conditions; confirm the three-command check was run and no
  secrets or hostnames are in the diff.
