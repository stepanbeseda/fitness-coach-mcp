# Plan — fitness-coach-mcp

Created: 2026-10-03. The plan is extended by `/mao`: every new task gets a
chapter and a table row; existing chapters are never rewritten, only the
"Delivered" blocks and the table status are added.

> Historic chapters are kept as written. Until 2026-10-09 the documentation was in Czech, so older chapters
> name Czech README sections (e.g. "Zálohy" = Backups) and, before the move to `docs/operations.md`, sections of
> the README that are now in that file. A short overview of the process is in
> [how-this-was-built.md](how-this-was-built.md).

## 1. Decisions and boundaries

- **What it is.** A self-hosted, single-user stack that gives a Claude
  fitness coach access to the owner's Hevy and Garmin Connect data: two MCP
  backends (`hevy-mcp/`, `garmin-mcp/`) behind one OAuth 2.1 gateway
  (`gateway/`), exposed only through a dedicated Tailscale node. It is not
  multi-user, has no web UI of its own and stores no health data outside the
  owner's server; the only exceptions are the morning briefing, which since
  PR 08 is sent to the owner's Telegram chat and kept there by Telegram, and,
  since PR 22, what the owner writes to that bot, which passes through
  Telegram before `coach-mcp` fetches it.
- **One way in.** Every MCP backend is reachable only through the gateway. A
  new backend means: a `[[backends]]` entry in `gateway/config.toml`, its own
  internal token secret, a hardened service in `docker-compose.yml` (the
  `x-hardening` anchor: non-root, read-only, no capabilities, memory and pid
  limits, healthcheck), and coverage in `scripts/setup-secrets.sh`,
  `scripts/e2e-test.sh` + `scripts/e2e_flow.py`, `scripts/healthcheck.sh` and
  `scripts/smoke-test.sh` + `scripts/smoke_test.py`.
- **Ingress boundary.** Nothing in the sidecar's namespace listens where the
  userspace netstack forwards tailnet traffic (127.0.0.1, ::1);
  `tailscale serve` on :443 is the only path to the gateway, and the tailnet
  policy grants members only tcp:443 to the node's tag.
- **Curated tools.** `garmin-mcp` deliberately has few tools with compact,
  coach-oriented JSON. New Garmin tools follow the pattern in
  `garmin-mcp/src/garmin_coach_mcp/server.py`: registered through `tool(...)`
  with `_read` / `_write` annotations, English docstring as the description,
  sections that Garmin cannot provide return `"error"` or
  `"available": false` instead of failing the whole call, library errors go
  through `GarminService.call` / `call_many`.
- **Writes need the athlete's confirmation.** Every tool that changes Garmin,
  Hevy or coach state says so in its description, and
  `docs/coach-instructions.md` requires showing the proposal first. No tool
  hard-deletes the athlete's data (removing a calendar entry is the only
  destructive operation today).
- **Tool results are untrusted data.** *Added 2026-10-18 (PR 34).* Text that
  tool results carry from third parties (Garmin activity and workout names and
  descriptions, Hevy titles and notes, calendar events and invites when Google
  Calendar is enabled) is data for the coach, never an instruction;
  `docs/coach-instructions.md` says so, and the coach never adds attendees to
  calendar events. The damage an injected instruction can do is limited in
  code, not by the prompt: `coach-mcp` reads Garmin with the read-only token
  (PR 12), may call only two read tools on Hevy, and every write goes only to
  the owner's own accounts after the confirmation the instructions require and
  claude.ai's tool permissions ask for. What remains is stated in `README.md`
  (Security model): with Google Calendar or web tools enabled in the claude.ai
  Project, an injected instruction could send data out. A confirm-token round
  trip is not used, because the model holds the token and it adds no human
  step.
- **Privacy of logs.** Logs and the audit trail contain tool name, outcome and
  duration only — never arguments, results, tokens or health values.
- **Supply chain.** Python dependencies are pinned in `pyproject.toml` and
  hash-locked with `scripts/lock-deps.sh`; base images are pinned by digest.
  A new dependency needs a reason in the spec.
  Since PR 32 the pins have an update path. Dependabot
  (`.github/dependabot.yml`) proposes monthly grouped bumps of the GitHub
  Actions SHAs and the Docker base images; the owner merges them after green
  CI and deploys them. Python packages stay with `scripts/check-updates.sh`
  and `scripts/lock-deps.sh`, because Dependabot cannot regenerate the uv
  hash locks. A weekly `pip-audit` workflow (`.github/workflows/audit.yml`,
  installed from its own hash-locked lock) checks every service lock for
  known vulnerabilities. Every `FROM` is a literal pinned reference, and
  `scripts/check-pins.sh` (CI) keeps the three Python base pins equal and
  the compose tags of `tailscale` and `hevy-mcp` equal to their upstream
  versions.
- **Coach state lives in one place.** PR 03 introduces the service
  `coach-mcp/` with a SQLite database on its own volume. PR 04, PR 05 and
  PR 06 extend that service instead of adding further services or databases.
  It reads Garmin and Hevy data only through the existing backends on the
  private `mcp` network, authenticated with internal tokens.
- **Contracts between services.** garmin-mcp's answers that coach-mcp reads
  are pinned by golden files checked on both sides; errors carry a
  machine-readable kind; coach-mcp matches error prose only as a fallback.
- **Language.** Code, tool descriptions, commit messages and this plan are in
  English. `README.md`, `garmin-mcp/README.md` and
  `docs/coach-instructions.md` are in Czech and stay Czech; text sent to the
  athlete (push messages) is Czech.
  *Superseded on 2026-10-09 (open-source release):* all documentation,
  scripts, the gateway pages and CLI output are English. Text sent to the
  athlete comes from the catalogue in `coach-mcp/src/coach_mcp/messages.py`,
  chosen by `COACH_LANGUAGE` (`en` default, `cs`); a new athlete-facing
  string goes into every catalogue. Backup side copies are named
  `<file>.before-restore-<UTC time>` (was `.pred-obnovou-`). Earlier chapters
  keep their original Czech references.

## 2. Common PR conditions

- One PR = one chapter; documentation in the same PR.
- Green check: `docker build --target test garmin-mcp`,
  `docker build --target test gateway`, `docker build --target test
  coach-mcp` (since PR 03) and `scripts/e2e-test.sh` (a new service adds its
  own `--target test` build to this list in the PR that introduces it).
- A test for every acceptance criterion that can be verified offline. Garmin
  tests use the fake client and canned payloads in
  `garmin-mcp/tests/conftest.py` and `garmin-mcp/tests/fixtures.py`; no test
  touches the network.
- Criteria that need the real Garmin / Hevy account are marked "on the test
  host" — the live deployment, verified with `scripts/smoke-test.sh`.
- When the tool list changes, update the tool table in `garmin-mcp/README.md`
  (or the new service's README), the overview in `README.md` and the workflow
  in `docs/coach-instructions.md`.
- No secrets, personal hostnames or account identifiers in the repo.

## 3. PR overview

| NN | Name | Depends on | Status |
|---|---|---|---|
| 01 | Performance metrics and race predictions | — | done |
| 02 | Body composition, hydration and nutrition | — | done |
| 03 | Coach memory server | — | done |
| 04 | Morning briefing and alerts | 03 | done |
| 05 | History store, trends and weekly report | 03 | done |
| 06 | Weather and calendar | 03 | done |
| 07 | Strength workouts on the watch | — | done |
| 08 | Daily briefing via Telegram | 04 | done |
| 09 | Strength history and progress | 05 | done |
| 10 | Separate e2e image tags and a configurable time zone | — | done |
| 11 | Online and scheduled backups | 03 | done |
| 12 | Read-only Garmin token for the coach | 04 | done |
| 13 | Plan reconciliation | 05 | done |
| 14 | Goal progress and the Monday report | 05, 08 | done |
| 15 | Searchable coach memory | 03 | done |
| 16 | Correlations | 05 | done |
| 17 | Sync-aware briefing | 04 | done |
| 18 | History repair | 05 | done |
| 19 | More Garmin signals | — | done |
| 20 | Operations alerts via Telegram | 08 | done |
| 21 | Static checks in the test stage | — | done |
| 22 | Two-way Telegram | 08 | done |
| 23 | One Plan module for sessions and notes | 22 | done |
| 24 | History view | — | done |
| 25 | Garmin sections in one place | — | done |
| 26 | Daily delivery and a local clock | — | done |
| 27 | Nightly history run in parts | 24, 26 | planned |
| 28 | Garmin tool definitions next to their code | 25 | planned |
| 29 | Recovery snapshot parsed once | 24, 27 | planned |
| 30 | Gateway reachable only through tailscale serve | — | **in progress** (branch `mao/pr30-gateway-serve-only`) |
| 31 | Garmin answer and error contract | 25 | **in progress** (branch `mao/pr31-garmin-contract`) |
| 32 | Automated dependency updates and audit | — | **in progress** (branch `mao/pr32-dependency-updates`) |
| 33 | Versioned images and rollback | — | **in progress** (branch `mao/pr33-versioned-images-rollback`) |
| 34 | Security model and gateway design note | 30 | **in progress** (branch `mao/pr34-security-model-design-note`) |
| 35 | Accurate showcase docs and a readable diagram | 30, 31, 32, 33, 34 | **in progress** (branch `mao/pr35-showcase-docs-diagram`) |

Chapters 10–22 come from the project review of 2026-10-09. Suggested order:
10, 11, 12, 13, 14, 09, 07, 15, 16, 17, 18, then 19, 20, 21 and last 22 (the
only chapter that accepts inbound messages). PR 12 goes before PR 09, so
`coach-mcp` does not collect a second full-access backend token.

Chapters 23–29 come from the architecture review of 2026-10-15. They are
refactors: behaviour stays the same, except for the two owner decisions
recorded in PR 23 and PR 28. Suggested order: 23 and 25 first (different
services, so they can run in parallel), then 24 and 28, then 26, 27 and
last 29. PR 23 goes first because its two copies of the rules already
disagree.

## 4. PR briefs

### PR 01 — Performance metrics and race predictions

Today the coach prescribes paces and zones without knowing the athlete's
thresholds. Add three read-only tools to `garmin-mcp` so plans can be based on
measured values. `get_performance_metrics` returns, in one call with fail-soft
sections: race predictions (5 km, 10 km, half marathon, marathon as `H:MM:SS`
and pace per km), lactate threshold (heart rate, pace, power when available),
heart-rate zones and cycling power zones with their boundaries, endurance
score and hill score, and the VO2max trend over a configurable number of weeks.
`get_personal_records` returns Garmin personal records (type, value in human
units, date, activity id). `get_gear` returns the athlete's gear (shoes,
bikes) with total distance, activity count and retired flag, so the coach can
flag worn-out shoes.

The underlying library methods already exist in python-garminconnect 0.3.17
(`get_race_predictions`, `get_lactate_threshold`, `get_heart_rate_zones`,
`get_power_zones`, `get_endurance_score`, `get_hill_score`,
`get_max_metrics_range`, `get_personal_record`, `get_gear`); the work is
shaping their payloads into compact JSON. Put the code in a new module next
to `garmin-mcp/src/garmin_coach_mcp/health.py`.

Out of scope: any write (editing gear, records or zones), per-activity power
curves, badges and challenges.

**Done when:**
1. `tools/list` on the Garmin endpoint contains `get_performance_metrics`,
   `get_personal_records` and `get_gear`, all annotated read-only.
2. `get_performance_metrics` returns the sections listed above; a section
   whose Garmin call fails or is empty returns `"error"` /
   `"available": false` and the others still arrive (test).
3. Race predictions and threshold pace are formatted with the existing
   helpers in `garmin-mcp/src/garmin_coach_mcp/formatting.py`, not as raw
   seconds or m/s (test).
4. `get_personal_records` and `get_gear` return the fields listed above from
   canned payloads added to `garmin-mcp/tests/fixtures.py` (tests).
5. `docker build --target test garmin-mcp` and `scripts/e2e-test.sh` are
   green.
6. `garmin-mcp/README.md` (tool table), `README.md` and
   `docs/coach-instructions.md` are updated; the instructions tell the coach
   to derive target paces and zones from these values.
7. On the test host: `scripts/smoke-test.sh` prints the athlete's race
   predictions and heart-rate zones.

**Requested:** 2026-10-03, by the owner, from the feature brainstorm (idea 4).

**Delivered:** 2026-10-04, PR #1 (`mao/pr01-performance-metrics`), merged
into `main` as `6032986`. New module
`garmin-mcp/src/garmin_coach_mcp/performance.py` with the three read-only
tools registered in `server.py`; 28 new tests in
`garmin-mcp/tests/test_performance.py` plus canned payloads in
`fixtures.py` / `conftest.py`; docs and `scripts/smoke_test.py` updated; no
new dependency. Criteria 1–6 verified offline (`docker build --target test
garmin-mcp`: 104 passed; gateway test build and `scripts/e2e-test.sh`
green, Garmin `tools/list` reports 18 tools). Criterion 7 verified
2026-10-04 on the test host after the owner deployed `garmin-mcp` to the
live stack: `scripts/smoke-test.sh` printed `race_predictions` (5 km, 10 km,
half marathon, marathon with time and pace per km, internally consistent)
and `heart_rate_zones` (one `HR_MAX` profile with max and threshold HR and
five contiguous zones) from the real account. Still unconfirmed on a real
account because the smoke test does not print them: the lactate-threshold
speed unit heuristic and the personal-record `typeId` mapping (spec risk
R1) — check them the first time the coach calls those sections live.
Deviation accepted in review: race times use the
existing `formatting.clock()` output (`21:30`, `1:39:30`), not zero-padded
`H:MM:SS`. Non-blocking follow-ups from the review: N1 — the `activityType`
guard in `shape_personal_records` has no test; N2 — `garminconnect` logs a
gear UUID at WARNING on a 404 in `get_gear_stats` (quiet the logger or
accept it explicitly). Lessons recorded in `.claude/mao/lessons.md`.

### PR 02 — Body composition, hydration and nutrition

For fat-loss or muscle-gain goals the coach sees training but not the body or
the intake. Add to `garmin-mcp`: `get_body_composition` (weigh-ins in a date
range, max 366 days: weight, BMI, body fat %, muscle mass, body water where
the scale provides them, plus a summary with first/last value, change and
7-day average), `get_nutrition` (one day: calories consumed vs. goal, protein
/ carbs / fat, meals, and hydration intake vs. goal) and one write tool
`log_weight` (weight in kg, optional date) for athletes without a Garmin
scale.

The library methods exist (`get_body_composition`, `get_weigh_ins`,
`get_daily_weigh_ins`, `get_hydration_data`, `get_nutrition_daily_food_log`,
`get_nutrition_daily_meals`, `get_nutrition_daily_settings`, `add_weigh_in`),
but the nutrition endpoints are unverified on a real account: when Garmin
returns nothing, the section must answer `"available": false`, not fail.
Hevy body measurements are already exposed by the upstream Hevy server
through the gateway; this PR only documents in `docs/coach-instructions.md`
that Garmin is the primary source for weight and that the coach must not
write the same measurement to both systems.

Out of scope: deleting weigh-ins, logging food or water, blood pressure,
menstrual data.

**Done when:**
1. `tools/list` contains `get_body_composition`, `get_nutrition` (read-only)
   and `log_weight` (write, non-destructive, description says "only after the
   athlete confirmed").
2. `get_body_composition` returns per-day entries in kg / % and the summary;
   an empty range returns an empty list with `"available": false` (test).
3. `get_nutrition` returns calories, macros, meals and hydration for the day;
   each missing part is `"available": false` and the call succeeds (test).
4. `log_weight` rejects values outside a plausible range (20–300 kg) and
   dates in the future before calling Garmin, and reports the stored entry
   (tests with the fake client).
5. `docker build --target test garmin-mcp` and `scripts/e2e-test.sh` are
   green.
6. `garmin-mcp/README.md`, `README.md` and `docs/coach-instructions.md` are
   updated, including the "Garmin is primary for weight" rule.
7. On the test host: `get_body_composition` for the last 30 days and
   `get_nutrition` for today answer without an error (data or
   `"available": false`).

**Requested:** 2026-10-03, by the owner, from the feature brainstorm (idea 5).

**Delivered:** 2026-10-04, PR #4 (`claude/dazzling-mccarthy-0wom8v`), merged
into `main` as `5a38ad5`. New module
`garmin-mcp/src/garmin_coach_mcp/body.py` with `get_body_composition` and
`get_nutrition` (read-only) and `log_weight` (write), registered in
`server.py`; 47 new tests in `garmin-mcp/tests/test_body.py` plus canned
payloads in `fixtures.py` / `conftest.py`; `garmin-mcp/README.md`,
`README.md`, `docs/coach-instructions.md` (including the "Garmin is primary
for weight" rule) and `scripts/smoke_test.py` updated; no new dependency.
Criteria 1–6 verified offline (`garmin-mcp` test build: 151 passed;
`scripts/e2e-test.sh` green, Garmin `tools/list` reports 21 tools).
Criterion 7 was verified late: `garmin-mcp` was deployed to the live stack
only on 2026-10-05, during the PR 03 run, when the owner ran `docker compose
up -d --build --no-deps garmin-mcp`; until then the live container predated
the merge and `scripts/smoke-test.sh` failed with `Unknown tool:
get_body_composition`. After the deploy `scripts/healthcheck.sh` and
`scripts/smoke-test.sh` were green, including the Garmin body-composition
section. The real nutrition payload shape is still not recorded (section 5).

### PR 03 — Coach memory server

Every conversation starts from zero: the coach does not know the athlete's
goal, injuries, or what was planned last week (`docs/coach-instructions.md`
even tells it not to rely on older conversations). Add a third MCP backend,
`coach-mcp/` (new directory), built like `garmin-mcp/`: Python 3.13, the same
MCP SDK, stateless Streamable HTTP on the private `mcp` network, bearer
middleware with an internal token, hardened container, hash-locked
dependencies, a `test` build target. It stores state in SQLite on a new
volume `coach-data` with a schema version and forward migrations.

Stored data: **goals** (title, event or target, target date, target result,
status active / achieved / abandoned), **constraints** (injuries,
limitations, available days and equipment; active or resolved), the
**plan** (sessions per date: type, description, target intensity, optional
Garmin workout id or Hevy routine id, status planned / done / skipped /
moved) and a dated **notes** log (why a plan changed, how a session felt).
Tools: `get_coach_context` (one read call: active goals with days remaining,
active constraints, the current week's sessions, the latest notes),
`get_plan` (date range, with an adherence summary: planned / done / skipped
counts), and write tools `set_goal`, `set_constraint`, `save_plan`,
`update_session`, `add_note`. Goals, constraints and sessions are closed or
marked, never hard-deleted. Session status is set by the coach after it has
checked Garmin and Hevy; automatic matching is not part of this PR.

Wire the backend end to end: service and secret `coach_backend_token` in
`docker-compose.yml`, `[[backends]]` entry with path `/coach/mcp` in
`gateway/config.toml`, token generation in `scripts/setup-secrets.sh`, third
backend in `scripts/e2e_flow.py`, `scripts/healthcheck.sh` and
`scripts/smoke_test.py`, lock files via `scripts/lock-deps.sh`, and in
`README.md` the architecture diagram, the third connector URL, the new volume
under backups, and troubleshooting. `docs/coach-instructions.md` starts every
planning session with `get_coach_context` and records confirmed plans.

Out of scope: calling Garmin or Hevy from this service (PR 04), scheduled
jobs, history of health metrics (PR 05), multi-user data.

**Done when:**
1. `docker build --target test coach-mcp` is green and becomes part of the
   project's check from this PR on.
2. The store creates its schema on an empty volume, records the schema
   version and survives a restart with data intact (tests on a temp file).
3. Each tool has a test: `get_coach_context` returns the four sections,
   `save_plan` replaces only the given week's planned sessions and keeps
   done ones, `get_plan` reports the adherence counts, invalid dates and
   unknown ids give a clear tool error.
4. Without the internal token the service rejects MCP requests (fail closed),
   as `garmin-mcp` does (test).
5. `scripts/e2e-test.sh` runs the OAuth + MCP flow against `/coach/mcp` too:
   `tools/list`, one tool call, and a token issued for another backend is
   rejected.
6. `scripts/setup-secrets.sh`, `scripts/healthcheck.sh` and
   `scripts/smoke_test.py` cover the new backend.
7. `README.md` and `docs/coach-instructions.md` are updated as described
   above; the new service has its own README with the tool table and env
   variables.
8. On the test host: the connector `/coach/mcp` can be added in claude.ai and
   a goal saved in one conversation is returned by `get_coach_context` in a
   new one.

**Requested:** 2026-10-03, by the owner, from the feature brainstorm (idea 2).

**Delivered:** 2026-10-06, PR #5 (`mao/pr03-coach-memory-server`), merged
into `main` as `3fa1882`. New service `coach-mcp/` (SQLite on the new volume
`coach-data` with schema version and forward migrations; tools
`get_coach_context`, `get_plan`, `set_goal`, `set_constraint`, `save_plan`,
`update_session`, `add_note`; 86 tests) wired through `docker-compose.yml`
(service, volume, secret `coach_backend_token`), `gateway/config.toml`
(`/coach/mcp`), `scripts/setup-secrets.sh`, `scripts/e2e_flow.py`,
`scripts/healthcheck.sh`, `scripts/smoke_test.py` and the lock files;
`README.md` (diagram, third connector URL, `coach-data` backup and restore,
troubleshooting), `coach-mcp/README.md` and `docs/coach-instructions.md`
updated. Nothing is hard-deleted: sessions replaced by `save_plan` become
`superseded`. The project check now has four commands (the `coach-mcp` test
build added). All eight criteria met. Verified offline: the `coach-mcp`
test build (86 passed) and `scripts/e2e-test.sh` (`E2E OK`, coach
`tools/list` reports 7 tools, a token issued for another backend is
rejected). Live deploy 2026-10-05 from the PR head: `setup-secrets.sh`
created `coach_backend_token`, `coach-mcp` was created and the gateway
recreated; `scripts/healthcheck.sh` and `scripts/smoke-test.sh` green with
both coach sections OK. Criterion 8 confirmed 2026-10-06: the owner added
the claude.ai connector `Kouč`, and the gateway audit shows `set_goal`,
`set_constraint` and then `get_coach_context` ok from claude.ai, with the
goal returned in a new conversation (an attempt on 2026-10-05 stored
nothing and was repeated). Non-blocking follow-ups from the review: O1 —
the `coach-data` restore recipe does not check that `coach.db` is a valid
SQLite file (the timestamped safety backup keeps the previous state). The
volume now holds real data: never `docker compose down -v`. Lessons recorded
in `.claude/mao/lessons.md`.

### PR 04 — Morning briefing and alerts

The stack only answers when asked. Add a scheduler to `coach-mcp` (a
background task in the server lifespan, like the keepalive loop in
`garmin-mcp/src/garmin_coach_mcp/garmin_client.py`) that once a day, at a
configurable local time, reads today's recovery snapshot, the HRV trend and
the scheduled workouts from `garmin-mcp` over the private network (as an MCP
client with the internal token), reads today's planned session from its own
store, and sends one push message in Czech: a green / yellow / red verdict
with the numbers behind it and what is planned today. The verdict comes from
deterministic rules in code that mirror section 2 of
`docs/coach-instructions.md` — no LLM call. The watch often syncs after the
athlete wakes up: when last night's sleep or HRV is missing, retry at a
fixed interval until a cutoff time, then send with what is available and say
what is missing.

Alerts, each at most once per day and only when its condition newly holds:
HRV below the baseline range for three or more consecutive nights, resting
heart rate at least 5 bpm above its 7-day average, acute:chronic load ratio
outside Garmin's optimal range, a scheduled workout from yesterday not
completed, Garmin authentication not `ok`. The same payload is available on
demand through a read tool `get_daily_briefing`, so the coach and the push
never disagree.

Push channel: ntfy (HTTP POST to a configurable server URL and topic, access
token as a Docker secret). The feature is off unless configured. `README.md`
states plainly that with a public ntfy server the message — which contains
health values — leaves the tailnet, and recommends a self-hosted server. The
scheduler must tolerate an unreachable push server or Garmin backend (log
outcome only, retry next cycle) and must never log message content.

Out of scope: Telegram or e-mail channels, weekly reports (PR 05), any write
to Garmin or Hevy, two-way interaction from the notification.

**Done when:**
1. The verdict rules are a pure function with tests for green, yellow, red
   and for missing inputs (falls back to the more conservative verdict and
   names the missing data).
2. Each alert has a test for firing, for not firing, and for not repeating on
   the same day (state kept in the SQLite store).
3. The scheduler runs at the configured time in the configured time zone,
   retries while sleep / HRV is missing and sends at the cutoff (tests with a
   fake clock, fake Garmin backend and fake push transport).
4. `get_daily_briefing` returns the same verdict, numbers and alerts as the
   push for the same inputs (test).
5. With no push configuration the scheduler does not start and
   `/healthz` says so; a failing push server does not crash the service
   (test).
6. `docker build --target test coach-mcp` and `scripts/e2e-test.sh` are
   green; `scripts/setup-secrets.sh` can store the ntfy token.
7. `README.md`, the service README (env variables with defaults) and
   `docs/coach-instructions.md` are updated, including the privacy note.
8. On the test host: a manually triggered briefing arrives on the owner's
   phone with today's real readiness, HRV, sleep and planned session.

**Requested:** 2026-10-03, by the owner, from the feature brainstorm (idea 1).

**Delivered:** 2026-10-06, PR #7 (`mao/pr04-morning-briefing-and-alerts`),
merged into `main` as `03d5ec3`. New modules in `coach-mcp` (`briefing.py`,
`garmin.py`, `push.py`, `scheduler.py`); the read tool `get_daily_briefing`
(the coach server now has 8 tools) and the container command `python -m
coach_mcp send-briefing` as the manual trigger. Schema migration 2 adds
`briefing_runs` and `alert_state` in one transaction; after it the old image
refuses the database (`SchemaTooNewError`), and a scratch-tested downgrade
recipe is in `coach-mcp/README.md`. New optional environment variables
`COACH_NTFY_URL`, `COACH_NTFY_TOPIC`, `COACH_BRIEFING_TIME`,
`COACH_BRIEFING_CUTOFF`, `COACH_BRIEFING_RETRY_MINUTES`,
`COACH_GARMIN_MCP_URL`, `COACH_GARMIN_TOKEN_FILE` and `COACH_NTFY_TOKEN_FILE`;
new Docker secret `ntfy_token`. Scripts: `scripts/setup-secrets.sh`
(`ntfy-token`), `scripts/healthcheck.sh` (section 5c), `scripts/smoke-test.sh`
and `scripts/smoke_test.py`, and the e2e probe in `scripts/e2e_flow.py`;
`README.md`, `coach-mcp/README.md` and `docs/coach-instructions.md` updated;
no new dependency. Criteria 1–5 and 7 verified offline (`coach-mcp` test
build: 293 passed; `scripts/e2e-test.sh` green, coach `tools/list` reports 8
tools); criterion 6 verified offline and live (L1). Criterion 8 verified
2026-10-06 on the test host after the owner deployed `coach-mcp` (`coach-data`
was backed up first; migration 2 ran on the real data): the manual
`send-briefing` sent the push (200), verdict yellow, no missing data, and the
owner confirmed the notification and its numbers; the live `acwr_status` was
observed as `OPTIMAL` only. Design decisions taken with the owner: a public
ntfy.sh server with the full message (privacy note in the README), alerts
evaluated only in the morning briefing, and the container command instead of a
tool as the manual trigger. One spec error was found in review (the
missed-workout alert is an event, not a state) and fixed by Addendum 1.
Non-blocking follow-ups: the `LOW` / `HIGH` / `VERY_HIGH` `acwr_status`
vocabulary is unverified live (an unknown value only suppresses the alert); a
morning without a sleep sync can cost up to about 91 Garmin Connect calls; the
coach's Garmin token could reach `garmin-mcp` write tools (only the code
allowlist prevents it); the e2e test shares image tags with the live stack
(section 5; it retagged the live tag before the deploy in this run). Lessons
recorded in `.claude/mao/lessons.md`.

### PR 05 — History store, trends and weekly report

Garmin tools look back at most 28 days (HRV) or 31 days (Body Battery), and
nothing joins strength and endurance load. Add a nightly snapshot job to
`coach-mcp` that stores one row per day in SQLite: from `garmin-mcp` the
nightly HRV, sleep duration and score, resting heart rate, Body Battery high
and low, average stress, training readiness, acute and chronic load, VO2max,
and per-sport activity totals (duration, distance, training load); from
`hevy-mcp` the day's strength volume (kg), sets per muscle group and best
set per exercise. On first run it backfills a configurable number of days
(default 90) slowly enough to stay inside Garmin's and Hevy's rate limits;
snapshots are idempotent and re-fetch the last few days to pick up late
syncs.

Tools, all read-only: `get_trends` (chosen metrics over any stored range,
aggregated by day, week or month, with mean, min, max and change),
`get_weekly_report` (one ISO week against the average of the previous four:
endurance load and time by sport, strength volume and sets per muscle group,
sleep, HRV, resting heart rate, plan adherence from the PR 03 store, new
bests, and flags such as "volume up more than 30 %"), and
`get_strength_progress` (estimated 1RM per exercise over time using the
Epley formula, with a plateau flag when it has not improved for a
configurable number of weeks). The tools return numbers; Claude renders the
charts. If the PR has to be split, the dividing line is between the
Garmin-side snapshot with `get_trends` and the Hevy-side volume with
`get_strength_progress`.
Split on 2026-10-07 at the owner's decision: this PR delivers the
Garmin-side snapshot, `get_trends` and `get_weekly_report` with its strength
sections marked as having no data; the Hevy side and `get_strength_progress`
moved to PR 09.

Out of scope: a dashboard or any UI, sending the report by push (can reuse
PR 04's channel in a later chapter), correlation analysis, exporting data.

**Done when:**
1. The snapshot of a day is idempotent, tolerates a missing section (stores
   null, does not fail the day) and a schema migration from the PR 03 schema
   is tested.
2. Backfill covers the configured range, resumes after a restart where it
   stopped and respects a configurable delay between backend calls (test
   with fake backends).
3. `get_trends` returns correct day / week / month aggregates for a seeded
   database, including ranges longer than 31 days (test).
4. `get_weekly_report` returns every section listed above for a seeded
   database and marks sections without data instead of failing (test); the
   strength sections stay marked as having no data until PR 09.
5. Moved to PR 09 (`get_strength_progress`, Epley e1RM and plateau flag).
6. `docker build --target test coach-mcp` and `scripts/e2e-test.sh` are
   green.
7. `README.md`, the service README and `docs/coach-instructions.md` are
   updated: the coach uses `get_weekly_report` for weekly reviews and
   `get_trends` for anything older than 28 days.
8. On the test host: after backfill, `get_trends` returns 90 days of HRV and
   `get_weekly_report` shows last week's real Garmin training (Hevy: PR 09).

**Requested:** 2026-10-03, by the owner, from the feature brainstorm (idea 3).

**Delivered:** 2026-10-08, PR #11 (`mao/pr05-history-trends`), squash-merged
into `main` as `1bed894`. Scope after the 2026-10-07 split: schema migration
3 (`history_days`); a nightly history job in `coach-mcp` as its own lifespan
task (default 03:30 in `TZ`) with a paced backfill of
`COACH_HISTORY_BACKFILL_DAYS` (90) days and `COACH_HISTORY_CALL_DELAY_SECONDS`
(15) between `garmin-mcp` calls, a pause in the briefing window, a D+1..D+3
re-fetch, the held-error / probe outage rule with a shared 1 h to 8 h backoff
and the `COACH_HISTORY_ENABLED` switch; the `GarminReader` allowlist extended
by `list_activities` only; the read tools `get_trends` and
`get_weekly_report` (strength sections fixed to `available: false` until
PR 09); the history state in `/healthz`; additions to `scripts/e2e_flow.py`,
`scripts/healthcheck.sh` and the smoke test; `README.md`,
`coach-mcp/README.md` (including the 3 to 2 downgrade recipe) and
`docs/coach-instructions.md` updated. Criteria 1-4, 6 and 7 met offline
(`coach-mcp` test build: 575 passed; the four-command check green).
Criterion 5 moved to PR 09 with the Hevy side. Criterion 8 met on the live
host 2026-10-08, deployed from the PR head before the merge: the first run
logged `history status=done fetched=103 failed=0 given_up=0` after 26
minutes (about 470 Garmin Connect requests), the smoke test was OK,
`get_trends` returned 90 days with data (71 nights with HRV) and
`get_weekly_report` for W40 showed real Garmin activities with sport names.
Deviations accepted: count metrics keep two decimals for mean / first / last /
change (review 1, N1); a `COACH_HISTORY_TIME` inside the briefing window is
accepted with a one-time warning instead of refusing to start (N3); the check
is the four commands of `project.md`. Review took two rounds; round 1 found a
spec error (a per-day failure counter was charged for a backend-wide outage),
fixed by a spec addendum. Open non-blocking findings N5-N8 are recorded in
section 5 ("PR 05 history job"). The live checkout moves to `main` after this
merge.

### PR 06 — Weather and calendar

Outdoor sessions are planned without knowing the weather or the athlete's
day. Weather: add a read-only tool `get_weather_forecast` to `coach-mcp`
that calls the Open-Meteo forecast API (no key) for the next 7 days at the
athlete's home location and returns, per day, hourly temperature, apparent
temperature, precipitation probability, wind speed, sunrise and sunset, plus
a short list of the best outdoor windows. The home location comes from
configuration (latitude and longitude rounded to two decimals, in `.env`,
documented in `.env.example`); optional tool arguments override it for
travel. Without a configured location and without arguments the tool returns
a clear error. Responses are cached briefly in memory so repeated planning
calls do not hammer the API.

Calendar: no code. The Google Calendar connector of claude.ai is used as it
is; `docs/coach-instructions.md` gains a section telling the coach, when the
connector is enabled, to check free slots before proposing session times and
to offer creating calendar events for confirmed sessions — and to work
without it when it is not enabled. `README.md` lists the connector as
optional.

Out of scope: air quality, storing weather history, geocoding place names,
any calendar integration inside this repo.

**Done when:**
1. `get_weather_forecast` returns the fields listed above from a canned
   Open-Meteo response and computes the outdoor windows deterministically
   (tests with a fake HTTP transport).
2. Missing location gives a clear tool error; argument coordinates override
   the configured ones; coordinates are rounded before the request (tests).
3. An unreachable or failing weather API yields a tool error with a short
   reason and never crashes the service (test).
4. The cache returns the stored response within its lifetime and refetches
   after it (test with a fake clock).
5. `docker build --target test coach-mcp` and `scripts/e2e-test.sh` are
   green (the e2e network has no internet: the tool must fail cleanly there).
6. `.env.example`, `README.md`, the service README and
   `docs/coach-instructions.md` (weather rules for heat, rain and wind; the
   calendar section) are updated.
7. On the test host: the forecast for the configured location is returned
   and matches Open-Meteo for the same coordinates.

**Requested:** 2026-10-03, by the owner, from the feature brainstorm (idea 7).

**Delivered:** 2026-10-09, PR #20 (`mao/pr06-weather-calendar`), squash-merged
into `main` as `6fda3f4`. New `coach-mcp/src/coach_mcp/weather.py` and the
read-only tool `get_weather_forecast` (Open-Meteo, 7 days): per day sunrise,
sunset, a summary, hourly temperature, apparent temperature, precipitation
probability and wind, and one best outdoor window; the location comes from
the arguments or the home location and is rounded to two decimals before the
request. Safety: 512 KiB response cap, 10 s per read and 20 s overall
deadline, strict payload validation (years 1900-8999), an in-memory cache
(30 min, 16 entries, failures never cached, a stale entry never served after
a failure), short `ToolError` texts, logs without coordinates. New optional
settings `COACH_HOME_LATITUDE` and `COACH_HOME_LONGITUDE` (both or neither,
validated at start-up, never echoed); `e2e-test.sh` blanks them, `e2e_flow.py`
gains `exercise_weather`, `smoke_test.py` prints a `počasí:` line. The
calendar part is documentation only (Google Calendar connector of claude.ai,
a section in `docs/coach-instructions.md`). `README.md`, `coach-mcp/README.md`
and `.env.example` updated. No schema change, no new secret, no new
dependency (`httpx2` was already direct). Criteria 1-6 met offline
(`coach-mcp` test build: 987 passed after the rebase, `garmin-mcp` 186,
`gateway` 80, `E2E OK`). Criterion 7 requires the test host: not deployed in
this run; the owner sets the two variables (rounded to two decimals), deploys
`coach-mcp`, runs `scripts/smoke-test.sh` and compares sunrise, sunset and
temperatures with an Open-Meteo request for the same coordinates. Designer
decisions: the variable names, two-decimal rounding, 30 min cache, one best
window per day; the MCP SDK prefixes tool-error text over HTTP, so HTTP tests
assert "contains" (spec check B1, decision D13). Review took two rounds, both
APPROVED; the four non-blocking findings of round 1 (an `OverflowError` on
absurd dates, no overall deadline, JSON `true` accepted as a latitude,
cosmetics) were fixed in `73fa161` on the rebase. Open follow-ups in
section 5 ("PR 06 weather").

### PR 07 — Strength workouts on the watch

`create_workout` in `garmin-mcp/src/garmin_coach_mcp/workouts.py` supports
only running and cycling, so strength sessions planned in Hevy never reach
the watch. Extend it with the sport `strength`: an ordered list of exercises,
each with sets, a repetition target, an optional weight in kg and a rest
between sets, built on the library's `StrengthWorkout` model and uploaded
like the existing sports, with the same optional `schedule_date`. Exercises
are identified by Garmin's exercise category and name; add a read-only tool
`find_strength_exercise` that searches a catalogue bundled with the server,
so the coach can map a Hevy exercise title to a Garmin exercise. An exercise
with no match is sent as a generic step carrying the Hevy title in its
notes. The Hevy side needs no code: the coach reads the routine with the
existing Hevy tools and calls `create_workout`.

This is the least certain chapter: the payload shape Garmin accepts for
strength steps and the source of the exercise catalogue are unverified.
The first step of the work is therefore a spike on the test host — upload
one three-exercise workout and confirm it in Garmin Connect. If Garmin
rejects the payload or drops exercise names, stop and report instead of
shipping a partial feature.

Out of scope: swimming and other sports, uploading completed Hevy workouts
to Garmin as activities, editing or deleting workouts, automatic two-way
sync.

**Done when:**
1. `create_workout` accepts `sport: "strength"` with the exercise step shape
   above and rejects pace, power and heart-rate targets on strength steps
   with a clear error (tests).
2. The uploaded payload for a three-exercise workout matches the shape
   verified in the spike (test against a canned payload in
   `garmin-mcp/tests/fixtures.py`).
3. `find_strength_exercise` returns ranked matches with category and name
   for a query and an empty list for nonsense (test).
4. An unmapped exercise becomes a generic step with its title in the notes
   and the result reports which exercises were not mapped (test).
5. `get_workout` and `list_workouts` show strength workouts with their
   exercises, sets and repetitions (test).
6. `docker build --target test garmin-mcp` and `scripts/e2e-test.sh` are
   green; `garmin-mcp/README.md`, `README.md` and
   `docs/coach-instructions.md` are updated (strength sessions can be sent
   to the watch after confirmation; Hevy stays the place of record).
7. On the test host: a workout created from a Hevy routine appears in Garmin
   Connect with exercise names, sets, repetitions and weights, and reaches
   the watch after sync.

**Requested:** 2026-10-03, by the owner, from the feature brainstorm (idea 6).

**Delivered:** 2026-10-12, PR #40 (`mao/pr07-strength-workouts-watch`),
squash-merged into `main` as `df6886d`, no rebase. `garmin-mcp` only:
`create_workout` takes `sport: "strength"` and a third item type `exercise`
(Garmin `category` and `name`, or a `title` for an exercise Garmin does not
know; `sets` 1 to 20; `reps` or `seconds`; optional `weight_kg`;
`rest_seconds`; `note`). A set block is a repeat group of an exercise step and a
timed rest step; the weight is sent in grams with the kilogram unit. Pace,
power and heart-rate targets on strength steps are refused with a clear error,
and so is a whitespace-only `title`; the limit of 50 steps counts the Garmin
steps. The result lists the `unmapped` exercises. New module `exercises.py` and
the read-only tool `find_strength_exercise` (query of 3 or more characters,
ranked matches with category and name, an empty list for nonsense) search the
catalogue shipped in `garminconnect` 0.3.17 (1 527 exercises, 47 categories),
make no Garmin call and are open to the coach's read-only token; the Garmin tool
count is 25. `get_workout` summarises a strength workout (exercises, sets, reps
or seconds, weight in kg, rest, note) and shows what it does not understand as a
plain `repeat` / `end`; `list_workouts` lists it with its sport. Tests:
`test_exercises.py`, additions to `test_workouts.py`, `test_http.py`,
`test_read_only.py` and `test_library_logs.py` (`upload_workout` through the
library's real inner client, with its log records), the canned request of the
spike as `STRENGTH_UPLOAD` in `tests/fixtures.py`; Garmin tests 305 to 424.
`scripts/smoke_test.py` gets a read-only "Garmin: strength exercise catalogue"
section. `garmin-mcp/README.md`, `README.md` (also a stale "About 20 curated
Garmin tools" corrected to 25) and `docs/coach-instructions.md` (a "Strength on
the watch" rule: map with `find_strength_exercise`, show the mapping, send only
after confirmation, Hevy stays the place of record) updated. No coach-mcp or
gateway change, no schema, config, secret, compose or dependency change.
Criteria 1 to 6 met offline (`CHECK OK tree=db6a4d6ff636 garmin=424 gateway=91
coach=1542 e2e=OK`; round 1 65 mutants and the delta 18, the survivors were test
gaps on correct code, closed in round 2, or near-equivalent). Reworded "Done
when" (the chapter text above is unchanged, plan section 1, PR 19 precedent; the
acceptance basis was `PR-07-brief.md`): (1) an exercise takes `reps` or
`seconds`, by the owner's answer to Q2; (2) the canned payload is the
four-block request Garmin accepted in the spike (a mapped exercise with a
weight, a mapped one with a note, an unmapped one, a timed one), not a
three-exercise shape; (4) an unmapped exercise is a step without a Garmin
exercise, its title in the note, instead of a generic step; (5) `list_workouts`
lists a strength workout with its sport only, because Garmin's list carries no
steps, and `get_workout` shows the exercises, sets, repetitions or seconds,
weights and rests; (6) the check is `scripts/check.sh --tag pr07`. Deviations:
timed exercises were added by the owner's answer to Q2; the closest-category
fallback of Q3 is not built, because Garmin accepted a step without an
exercise (spec decision D3, it stays the documented fallback); a blank title
is refused and a two-set step count is tested, both added after review round 1.
Spike (2026-10-12, run once by the orchestrator on the owner's own typed
command): Garmin accepted the four-block workout at the first upload and stored
the exercise names, the weight (grams with the kilogram unit), the step notes,
a step without an exercise (category and name null, the note kept) and a hold by
time; the owner saw them in the Garmin Connect mobile app and on the watch ("all
good", notes visible in both). The request body of that upload is the canned
payload of criterion 2. All three clarifying questions were answered with the
recommended default. Review took two rounds: round 1 APPROVED (0 blocking, 12
non-blocking; O1 to O5, O7, O10 and O11 fixed in two commits), round 2 a delta
review APPROVED with 2 new non-blocking (N1, N2). Spec check: 2 blocking (B1
four mutation-table entries named a test that does not kill the mutant, B2 the
library-log claim held only for the stub), 5 non-blocking, fixed in place and
folded in before implementation.
Live verification 2026-10-12 (owner rebuilt `garmin-mcp` at `df6886d` and pasted
the changed `docs/coach-instructions.md` into the project): `scripts/smoke-test.sh`
green (exit 0), 25 Garmin tools with `find_strength_exercise` listed, the
"strength exercise catalogue" section printed 3 matches for "bench press".
Criterion 7 met by the owner's words: names, sets, repetitions, weights and the
label of a step without an exercise match in Garmin Connect and on the watch.
Gateway audit since the rebuild (all status 200): `find_strength_exercise` 10
calls, `create_workout` 1 call. This settles U1: an exercise Garmin does not
know is shown with the heading "--" and its note (the Hevy title) below it;
`garmin-mcp/README.md` and `docs/coach-instructions.md` still name no label.
The owner did not say which squat the coach picked, so O6 was not observed.
Still unverified: U2 (the shapes the spike did not send: a single set outside a
repeat group, a set block without a rest, a weight on a step without an exercise
or on a timed step, a note longer than 27 characters, more than 4 blocks, a
fractional weight; the one real `create_workout` call returned 200, but the
audit holds no arguments, so its shape is not recorded), U3 (workouts authored in the Garmin app: supersets,
pound units, lap-button ends) and U4 (Garmin's step limit; the 50 stays). Open
non-blocking: (O6) a plain "Squat (Barbell)" ranks Barbell Back Squat fourth
(design D9); (O8) `get_workout` shows a warm-up step without its type; (O9) a
`TypeError` on a mixed `stepOrder` of a workout authored in the Garmin app; (N1)
a validator returning the stripped title would survive the suite; (N2) a
zero-width space as the whole title passes the blank-title check and a blank
`note` on an unmapped exercise gives the description `"t -    "`; all in section
5. The owner deletes the spike workout "MAO spike PR07 - delete me" in Garmin
Connect (the server has no delete tool).

### PR 08 — Daily briefing via Telegram

The morning briefing from PR 04 goes to ntfy. Send it to the owner's
private chat with their own Telegram bot instead, and remove ntfy
completely. This reverses "Out of scope: Telegram" in the PR 04 chapter.
The push transport in `coach-mcp/src/coach_mcp/push.py` calls the Bot API
method `sendMessage` (HTTPS POST from the `coach-mcp` container, no new
dependency) with the chat id from the environment and the bot token as a
Docker secret. The feature stays off unless a chat id is configured. A chat
id without a usable token stops `coach-mcp` at start-up with an error that
names the secret file, never its content. The token is part of the request
URL, so no log line, error or script argument may contain the URL, the
token, the chat id or the message.

The message keeps its full content, the numbers included, and stays plain
Czech text with no Telegram formatting. Its first line is the verdict with a
coloured circle and the date, and every briefing notifies with sound. The
ntfy title, priority and tags disappear. The `push` part of
`get_daily_briefing` carries the exact text that is sent. The verdict, the
alerts, the schedule, the retry and cutoff, the alert state and the manual
`send-briefing` command do not change, and there is no schema migration.
`scripts/setup-secrets.sh` stores and checks the bot token and helps the
owner find the chat id. `README.md` says plainly that Telegram keeps the
chat history, with the health values, on its servers, and that bot chats are
not end-to-end encrypted.

Out of scope: keeping ntfy as an alternative channel, group or channel
targets beyond accepting a numeric chat id, Telegram formatting (Markdown
or HTML), silent notifications, any interaction with the bot beyond
receiving the briefing, and the weekly report (PR 05).

**Done when:**
1. The Telegram transport sends one `sendMessage` with the configured chat
   id and the full briefing text. It treats 2xx as sent, 429, 5xx and
   network errors as transient, and every other status as permanent. Its
   logs never contain the bot token, the chat id or the text (tests with a
   mock transport).
2. Push is on only when `COACH_TELEGRAM_CHAT_ID` is set. A malformed chat
   id, or a set chat id with a missing, empty or malformed token file,
   fails at start-up. Without a chat id the scheduler does not start and
   `/healthz` says so (tests).
3. The message is plain text: an emoji verdict line with the date, then
   the body, and the whole text stays within Telegram's limit. The
   `get_daily_briefing` field `push.text` equals the text the transport
   sends (tests).
4. ntfy is gone: no ntfy code, setting, secret, script subcommand or test
   remains, and the only documentation that mentions it is the README
   rollback note of item 7 (`grep -rni ntfy` over the repo, excluding
   `mao-plan.md` and `.claude/`, finds only that note).
5. `scripts/setup-secrets.sh telegram-token` stores the token after
   checking it with Telegram, never passes it as a command-line argument or
   prints it, and shows the owner's chat id. `all` creates an empty
   placeholder so the stack starts with push off. The behaviour is shown
   with an offline stub harness, and `scripts/e2e-test.sh` keeps push off.
6. `docker build --target test garmin-mcp`,
   `docker build --target test gateway`,
   `docker build --target test coach-mcp` and `scripts/e2e-test.sh` are
   green.
7. `README.md`, `coach-mcp/README.md` and `.env.example` are updated:
   the Telegram setup (BotFather, chat id), the privacy note, the secrets
   table, troubleshooting, the env table, and a rollback note saying that
   the PR 04 image needs the ntfy secret file back.
8. On the test host, after the owner approves the deploy, the manual
   `send-briefing` message arrives in the owner's Telegram chat with
   today's real readiness, HRV, sleep and planned session. Once the owner
   confirms it, the owner removes the ntfy secret file and the
   `COACH_NTFY_*` lines from `.env`.

**Requested:** 2026-10-06, by the owner, from the chat after PR 04 ("Send the daily briefing to Telegram instead of ntfy"); questions and answers round 1.

**Delivered:** 2026-10-07, PR #9 (`mao/pr08-briefing-telegram`), merged into
`main` as `c81bfb7`. `TelegramPush` replaces `NtfyPush` in
`coach-mcp/src/coach_mcp/push.py`: it calls the Bot API `sendMessage`, re-reads
the token on every send, reads no response body, and keeps the outcome mapping
unchanged. New configuration `COACH_TELEGRAM_CHAT_ID` and
`COACH_TELEGRAM_TOKEN_FILE` with start-up validation, and the Docker secret
`telegram_bot_token`. `PushMessage` carries a title and a body, the title is
the emoji verdict, a 4000-byte guard covers the whole text, and
`get_daily_briefing` returns `push.text`. `scripts/setup-secrets.sh` gained
`telegram-token`, `telegram-chat-id` and an empty placeholder (the token never
appears on a command line; 16-case stub harness). Compose, the e2e test (push
off), `scripts/healthcheck.sh` (section 5c), `README.md` (Telegram setup,
privacy note, rollback note), `coach-mcp/README.md` and `.env.example` updated;
ntfy removed everywhere except the README rollback note; the clause in section
1 and the superseded note in section 5 updated; no new dependency and no
schema change. Criteria 1–7 verified offline (`coach-mcp` test build: 309
passed; `scripts/e2e-test.sh` green, 8 coach tools; `grep -rni ntfy` finds only
the rollback note). Criterion 8 verified 2026-10-07 on the test host after the
owner deployed `coach-mcp` (`coach-data` was backed up first): the manual
`send-briefing` reached the owner's Telegram chat, first with the data-gap
lines before the watch had synced, then with today's full values, confirmed by
the owner; the `COACH_NTFY_*` lines and the empty `secrets/ntfy_token` were
removed from the live host. Design decisions taken with the owner: replace
ntfy entirely, send the full message, plain text with sound, and a live deploy
with the clean-up inside the run. Non-blocking follow-ups: a Telegram HTTP 200
with `ok:false` counts as sent; real 400, 403 and 429 replies are unverified;
the scheduled path over Telegram first runs on 2026-10-08. Lessons recorded in
`.claude/mao/lessons.md`.

### PR 09 — Strength history and progress

PR 05 stores the Garmin side of the daily history; its weekly report marks
the strength sections as having no data. Add the Hevy side to `coach-mcp`: a
read-only client for `hevy-mcp` built like
`coach-mcp/src/coach_mcp/garmin.py` (an MCP client on the private `mcp`
network with the internal token `hevy_backend_token`, mounted into
`coach-mcp` in `docker-compose.yml`, and a code allowlist of read tools). The
PR 05 snapshot job also stores per day (local start date of the workout) the
strength volume in kg, the sets per muscle group, and per exercise the sets,
the volume and the best set by Epley e1RM. Warm-up sets are excluded; a set
counts for the primary muscle group of its exercise template. Workouts with
their sets come from `get-workout-events` since a stored cursor, which also
reports edited and deleted workouts; muscle groups come from the exercise
templates, cached in the store. The backfill covers the same configured range
as the Garmin side, uses the same delay between backend calls and resumes
after a restart. A schema migration adds the strength tables.

`get_weekly_report` fills its strength sections: volume and sets per muscle
group against the average of the previous four weeks, new e1RM bests and the
"volume up more than 30 %" flag. A new read-only tool
`get_strength_progress` returns the estimated 1RM per exercise over time with
a plateau flag when it has not improved for a configurable number of weeks
(default 4). The tools return numbers; Claude renders the charts.

Out of scope: storing raw sets, strength workouts on the watch (PR 07), any
write to Hevy, a dashboard or any UI.

**Done when:**
1. The Hevy client calls only allowlisted read tools and fails soft: an
   unreachable `hevy-mcp` or a missing token stores null for the day's
   strength section without failing the Garmin part (tests with a fake Hevy
   backend).
2. The strength snapshot of a day is idempotent, excludes warm-up sets,
   counts sets for the primary muscle group, and an edited or deleted Hevy
   workout updates its day on the next run (tests).
3. The strength backfill covers the configured range, resumes after a
   restart and respects the configured delay; the schema migration upgrades a
   database of the PR 05 schema with its data intact (tests).
4. `get_weekly_report` returns the strength sections, new bests and the
   volume flag for a seeded database (test).
5. `get_strength_progress` computes Epley e1RM per exercise over time and the
   plateau flag on seeded sets (test).
6. `docker build --target test garmin-mcp`,
   `docker build --target test gateway`,
   `docker build --target test coach-mcp` and `scripts/e2e-test.sh` are
   green.
7. `README.md`, `coach-mcp/README.md` (tools, env variables, data, downgrade
   recipe) and `docs/coach-instructions.md` (the coach uses
   `get_strength_progress` for strength progression) are updated.
8. On the test host, after the owner approves the deploy and the backfill:
   `get_weekly_report` shows last week's real Hevy training and
   `get_strength_progress` returns e1RM history for an exercise trained in
   the last 90 days.

**Requested:** 2026-10-07, by the owner, split from PR 05 (PR 05 questions and answers round 1).

**Delivered:** 2026-10-09, PR #17 (`mao/pr09-strength-history`),
squash-merged into `main` as `4cbf023`, rebased onto PR 11, 12 and 13.
`coach_mcp/hevy.py` is a read-only client for `hevy-mcp` on the private
network with the existing secret `hevy_backend_token` (now mounted into
`coach-mcp`) and an allowlist of two tools (`get-workout-events`,
`get-exercise-template`; any other name raises before a request); failures
are soft with an outcome word. `strength.py` holds the workout summaries
(warm-up sets excluded, a set counts for the primary muscle group), event
parsing (edit, move, delete; a deletion is a terminal tombstone), Epley e1RM,
the plateau flag and week totals; only summaries are stored. Schema
migration 5 adds `strength_workouts`, `hevy_templates` and `hevy_sync`
(additive; the spec said 4, PR 13 took it first). The nightly job gets a
strength part after the Garmin part and before PR 13's reconcile step: events
since a stored sync point with a 3-day overlap, a template cache, the same
pacing as the Garmin calls (also between the last Garmin and the first Hevy
call), the briefing-window pause and resume after a restart; a Hevy failure
never changes what the Garmin part stores; the log line is
`part=strength status=... workouts=... failed=... skipped=... ms=...`.
`get_weekly_report` fills its strength section with new e1RM bests and the
`strength_volume_up` flag (over 30 %); new read-only tool
`get_strength_progress` (`plateau_weeks` 2-12, default 4); the coach has 11
tools at this point. Integration with PR 13 (spec section 12, applied on the
rebase): a Hevy workout of a covered day is evidence (`source: hevy`, `sets`,
`volume_kg`) for a planned strength session, so a session logged only in Hevy
stops showing `no_activity`. New settings `COACH_HEVY_MCP_URL` and
`COACH_HEVY_TOKEN_FILE` have defaults; no `.env` change. Also
`scripts/e2e_hevy_client.py` (against the real hevy-mcp image) and a strength
line in `smoke_test.py`. Criteria 1-7 met offline (`coach-mcp` 841 passed,
`garmin-mcp` 186, `gateway` 80, `E2E OK`; the 5 to 4 downgrade recipe was run
on scratch volumes, chained before PR 13's 4 to 3). Criterion 8 requires the
test host: not deployed in this run; the Hevy facts that offline tests cannot
settle are listed in section 5 ("PR 09 strength history"). Deviations
accepted: the weekly strength section has no `available` key when data exists
(spec 4.6 over 6.7, a spec error in 6.7); test names differ from the spec
list. Review took two rounds, both APPROVED: round 1 findings N1 (skipped
events invisible) and N2 (an `OverflowError` on an extreme timestamp would
crash the run every hour) were fixed in the rebase round; round 2 found
model-facing texts that still said suggestions are Garmin-only (a spec
error), fixed in `f08bc0c` before the merge.

### PR 10 — Separate e2e image tags and a configurable time zone

`scripts/e2e-test.sh` builds its images under the live tags
(`fitness-coach-mcp/*:0.1.0`), so a test build of a branch replaces the image
the live stack is recreated from, and the next `docker compose up -d` in the
live checkout deploys and migrates it without a backup (section 5, open since
PR 03; it happened before the PR 04 deploy). Give the e2e builds their own
tags in the `compose.test.yml` overlay, so the live compose file does not
change for it. Separately, the athlete's time zone is baked into the
`garmin-mcp` and `coach-mcp` images as `TZ=Europe/Prague` and cannot be set
from `.env`, although both service READMEs document `TZ`. Pass it from a new
`.env` setting `ATHLETE_TZ` (default `Europe/Prague`) as `TZ` to both
services; a dedicated name keeps a shell `TZ` (Compose prefers shell
variables) and the scripts' own clock (`scripts/lib.sh` exports `.env`) out
of it.

Out of scope: validating the time zone in `garmin-mcp` at start-up
(`coach-mcp` already refuses an unknown zone), image tags of the live stack,
removing old test images.

**Done when:**
1. `compose.test.yml` gives all five services an `fcm-e2e/*:test` image and
   `scripts/e2e-test.sh` uses no live tag; the image IDs of the five live
   tags are the same before and after an e2e run.
2. `docker compose config` resolves `TZ` to `Europe/Prague` without
   `ATHLETE_TZ` and to its value when it is set, for both services.
3. `.env.example`, `README.md` (installation step 3), `coach-mcp/README.md`
   and `garmin-mcp/README.md` describe `ATHLETE_TZ`; the e2e open item in
   section 5 and the matching lesson are updated.
4. The four-command check is green.

**Requested:** 2026-10-09, by the owner, from the project review (items U3
and U4).

### PR 11 — Online and scheduled backups

`coach-data` is the only copy of goals, constraints, plan and notes, and the
README recipe stops `coach-mcp` to tar the volume; nothing schedules it. Add
a backup that runs while the stack is up: a container command (for example
`python -m coach_mcp backup`) that takes a consistent copy of `coach.db`
through SQLite's online backup API (available in the image: SQLite 3.46.1,
`sqlite3.Connection.backup`), the same for the gateway database
(`/data/gateway.sqlite3`, grants and refresh tokens), and the Garmin token
file. A new `scripts/backup.sh` writes one timestamped archive (mode 0600)
into a directory outside the repo given in `.env`, keeps the newest N
archives and exits non-zero on any failure; `README.md` gets a cron line next
to the healthcheck one and a restore recipe per archive part.

Out of scope: off-site copies and encryption of the archive (the README says
where to put it), backing up `ts-state` (a new auth key recreates the node),
`secrets/` (re-created by `scripts/setup-secrets.sh`).

**Done when:**
1. The backup of a database that another connection is writing at the same
   moment opens as a valid SQLite file with the expected rows (test on a temp
   file).
2. `scripts/backup.sh` against the e2e stack produces an archive with all
   parts, keeps only the newest N, and fails with a clear message when a part
   is missing; it never prints secret or health values.
3. The restore recipe is run on scratch volumes, fresh and existing (lesson
   from PR 03), and the result is recorded in the PR.
4. `.env.example`, `README.md` (Zálohy) and `coach-mcp/README.md` are
   updated; the old stop-and-tar recipe is replaced, not kept beside it.
5. The four-command check is green.
6. On the test host: one scheduled run produces an archive whose `coach.db`
   has the live goal count.

**Requested:** 2026-10-09, by the owner, from the project review (item U1).

**Delivered:** 2026-10-09, PR #14 (`mao/pr11-online-backups`),
squash-merged into `main` as `f2f60df`. `python -m coach_mcp backup` and
`restore` (an online SQLite backup of `coach.db` streamed to stdout, 32 MiB
cap, 60 s busy deadline; restore validates before replacing, moves the
previous files aside as `*.pred-obnovou-<UTC>[-journal|-wal|-shm]` and
refuses a newer schema) and `backup-db` / `restore-db` in `gateway.cli` for
`/data/gateway.sqlite3`. `scripts/backup.sh` writes one all-or-nothing archive
`fitness-coach-backup-<UTC>Z.tgz` (mode 0600: `coach.db`, the gateway
database, the Garmin token file), keeps the newest `BACKUP_KEEP` under
`flock` and takes `--dir`, `--keep` and `--quiet`; `scripts/e2e_backup.sh`
runs from `scripts/e2e-test.sh`. New settings `BACKUP_DIR` (no default,
absolute, outside the repo) and `BACKUP_KEEP` (default 14), read by the
script from `.env`; no schema, compose, secret or dependency change.
`README.md` (Zálohy, a cron line next to the healthcheck line, a restore
recipe per archive part, the way back, troubleshooting), `coach-mcp/README.md`
and `.env.example` updated; the old stop-and-tar recipe is replaced, not kept.
Criteria 1-5 met offline (`coach-mcp` 606 passed, `gateway` 80, `E2E OK` and
`E2E backup OK`); criterion 3 by a restore run on scratch volumes, fresh and
existing, verdict PASS (run file `PR-11-restore-run.md`). Criterion 6
requires the test host: not deployed in this run. Designer decisions: three
parts in one all-or-nothing archive; `audit.jsonl`, `ts-state` and `secrets/`
are not backed up; archive names use UTC; the Garmin token restore keeps no
side copy. Deviations accepted: the in-memory guard test uses 3.5x instead of
3x (CPython `serialize()` alone peaks near 3.15x). Review took two rounds:
round 1 CHANGES with a spec error (rotation by name could delete the archive
just written when later-named archives exist, and still report success),
fixed by Addendum A1 (the new archive is never a candidate, a warning names
newer archives, success needs the file; the busy wait now covers the size
probe); round 2 APPROVED, and its one new finding (the clean-up `find` needed
`-mindepth 1`) was fixed in `68c1a1c`. Open follow-ups in section 5
("PR 11 backups").

### PR 12 — Read-only Garmin token for the coach

`coach-mcp` reads `garmin-mcp` with `garmin_backend_token`, the same token the
gateway uses, so only the code allowlist in
`coach-mcp/src/coach_mcp/garmin.py` keeps it from `create_workout`,
`schedule_workout`, `unschedule_workout` and `log_weight` (follow-up of
PR 04). Give `garmin-mcp` a second, read-only token: its bearer middleware
learns which token was presented, and a `tools/call` of a tool that is not
annotated read-only is refused for the read-only token before the tool runs.
`scripts/setup-secrets.sh` generates the new secret
(`garmin_readonly_token`), `docker-compose.yml` mounts it into `coach-mcp`
instead of `garmin_backend_token`, and `COACH_GARMIN_TOKEN_FILE` points to it.
`coach-mcp/src/coach_mcp/auth.py` is a copy of the `garmin-mcp` file; the PR
decides whether the copy follows the change or stays as it is.

Out of scope: a read-only role for `hevy-mcp` (an upstream image with one
bearer token; see section 5), per-tool scopes in the gateway.

**Done when:**
1. With the read-only token every read tool works and every write tool is
   refused with a clear error; with the gateway token every tool works; a
   tool missing from the annotation map counts as a write (tests).
2. `coach-mcp` no longer mounts `garmin_backend_token`; the briefing and the
   history job work with the new token in the e2e stack.
3. `scripts/setup-secrets.sh` creates the token for a new and an existing
   installation (offline stub harness, lesson from PR 03).
4. `README.md` (security model, secrets) and both service READMEs are
   updated.
5. The four-command check is green.
6. On the test host, after the owner runs `setup-secrets.sh` and approves the
   deploy: the morning briefing and the nightly history still run.

**Requested:** 2026-10-09, by the owner, from the project review (item U2).

**Delivered:** 2026-10-09, PR #15 (`mao/pr12-garmin-readonly-token`),
squash-merged into `main` as `782265d`. `garmin-mcp` accepts a second token
(secret `garmin_readonly_token`, file path `GARMIN_MCP_READONLY_TOKEN_FILE`)
next to the gateway token and records the role of the presented token in the
ASGI scope; the new `access.py` adds `ReadOnlyGuard`, an SDK extension that
intercepts every `tools/call` and, for the read-only role, refuses any tool
not annotated read-only before it runs (`READ_ONLY_REFUSAL`, `isError`). A
tool missing from the map and an unknown name count as writes, an unknown role
fails closed, the gateway token keeps every tool. `docker-compose.yml` mounts
`garmin_readonly_token` into `garmin-mcp` and `coach-mcp`; `coach-mcp` no
longer mounts `garmin_backend_token` and its two config defaults point to the
new file. `coach-mcp`'s inbound `auth.py` stays single-token (decision D7).
`scripts/setup-secrets.sh` creates the token for new and existing installs
(`ensure_garmin_readonly_token`, no service recreate, part of rotation and of
the full run); the offline harness `scripts/test-setup-secrets.sh` (7 cases)
runs from `e2e-test.sh`; `scripts/e2e_coach_garmin.py` checks the real coach
container (mounts, history calls, a refused write probe). `README.md` and
both service READMEs updated (security model, secrets, upgrade paragraph,
troubleshooting). No schema change. Criteria 1-5 met offline (`garmin-mcp` 186
passed with 35 new tests in `test_read_only.py`, `gateway` 80, `coach-mcp` 606,
`SETUP-SECRETS OK`, `COACH-GARMIN OK`, `E2E OK`); the reviewer's probes (name
variants, batch calls, spoofed headers, a double `Authorization` header, a
runtime rewrite of the token file, protocol `2026-07-28`) were all refused.
Criterion 6 requires the test host: not deployed in this run. Deviations:
none. Review took one round, APPROVED with no blocking finding; N1-N3 are in
section 5 ("PR 12 read-only token"). The Garmin part of the open item on
full-access backend tokens is resolved by this PR; the Hevy part stays open.

### PR 13 — Plan reconciliation

The coach sets session status by hand, and the missed-workout alert looks
only at Garmin's calendar, never at the coach plan. Add a nightly step to the
history job of `coach-mcp`: for every planned session of the last seven days
it looks for a recorded activity of the same day and sport (from the stored
activity totals, running / cycling / swimming / strength; mobility and other
are never matched) and stores a suggestion: `matched` with the evidence
(sport, duration, distance, and the activity id when the history keeps it) or
`no_activity`. Section 1 says writes need the athlete's confirmation and
PR 03 leaves the status to the coach, so the step changes no status:
`get_plan`, `get_coach_context` and the briefing show the suggestion, and the
coach confirms it with `update_session`. When PR 09 has landed, a Hevy
workout of the day matches a strength session too.

Out of scope: changing a status automatically, matching by workout id or
time of day, sessions older than seven days.

**Done when:**
1. The matching is a pure function with tests for a match, no activity, a
   different sport, two sessions on one day and a day whose activities were
   never fetched (no suggestion).
2. `get_plan` and `get_coach_context` return the suggestion per session; a
   session the coach has already marked keeps its status and drops the
   suggestion (tests).
3. The briefing names yesterday's planned sessions without an activity, as an
   event with the day as its key (lesson from PR 04), and the alert state
   migration is tested.
4. `coach-mcp/README.md` and `docs/coach-instructions.md` (check the
   suggestions, then propose `update_session`) are updated.
5. The four-command check is green.
6. On the test host: last week's planned sessions show suggestions that
   agree with Garmin Connect.

**Requested:** 2026-10-09, by the owner, from the project review (feature 1).

**Delivered:** 2026-10-09, PR #16 (`mao/pr13-plan-reconciliation`),
squash-merged into `main` as `a0a8118`, rebased onto PR 11 and 12. New pure
module `coach_mcp/reconcile.py` (`suggest`): planned running, cycling,
swimming and strength sessions of the last 7 days are compared with the
stored Garmin day totals of the same day and sport and get `matched` with
evidence or `no_activity`; mobility, other and non-planned sessions get
nothing, a day whose activities were never fetched gets no suggestion, and
several sessions of one sport share the day's count. `history.py` gains
`activities_settled`, `day_activities` and `HistoryJob._reconcile`, which runs
after every run (also with nothing pending, with Garmin not logged in and
after a stopped run) and appends `matched=N no_activity=M` or
`reconcile=error kind=<Class>` to the one log line; a failing step never
changes the job's outcome or scheduling. Schema migration 4 rebuilds
`alert_state` with all its rows so the CHECK accepts `missed_planned_session`
and creates `session_suggestions` (upserted nightly, never deleted).
`get_plan` and `get_coach_context` show `suggestion` on planned sessions only
(a session the coach marked keeps its status and drops it);
`get_coach_context` adds `to_confirm`. A new event alert
`missed_planned_session` joins the morning briefing, de-duplicated by the day.
No new tool, config value, secret or dependency; the server never changes a
session status and stays read-only toward Garmin. `README.md`,
`coach-mcp/README.md` (including a 4 to 3 downgrade recipe) and
`docs/coach-instructions.md` updated. Criteria 1-5 met offline (`coach-mcp`
664 passed after the rebase, `garmin-mcp` 186, `gateway` 80, `E2E OK`; the
downgrade recipe was run on scratch volumes by the implementer and the
reviewer, fresh volume included). Criterion 6 requires the test host: not
deployed in this run; sessions older than 7 days at the first run never get a
suggestion. Here strength sessions match only Garmin `strength_training`
totals; PR 09 added Hevy evidence on its rebase. Designer decision 7: the
morning alert reads the provisional suggestion of the 03:30 fetch (see
section 5, "PR 13 plan reconciliation"). The spec check found a blocking name
clash (`yesterday` in `alert_conditions`), fixed in the spec. Review took one
round, APPROVED with no blocking finding and four non-blocking ones, two of
them fixed on the rebase.

### PR 14 — Goal progress and the Monday report

Goals are free text, so nothing measures whether the athlete is on track.
Give `set_goal` two optional fields, a `metric` (race_5k, race_10k,
race_half_marathon, race_marathon, vo2max_running) and a `target_value`
(time as `H:MM:SS` or a number), with a schema migration. Store Garmin's race
predictions per day in the history: a new read-only `garmin-mcp` tool returns
them over a date range (the library's `get_race_predictions(startdate,
enddate, _type)`), so the backfill takes one call per range and the night one
call. A new read tool `get_goal_progress` returns, per active goal with a
metric, the current value, the values 4 and 12 weeks ago, the trend per week,
the value projected to the target date, the days remaining and an on-track
flag. On Monday, after the morning briefing, the scheduler sends a second
Telegram message in Czech with last week's `get_weekly_report` numbers and
the goal progress (plain text, the same size guard, de-duplicated by ISO
week; `COACH_WEEKLY_PUSH`, on while push is on), and a container command
`send-weekly-report` sends it by hand.

Out of scope: weight and body-composition goals, goals without a metric
(they keep working as today), charts.

**Done when:**
1. The migration adds the goal fields and the prediction history with data
   intact; `set_goal` validates metric and value (tests).
2. `get_goal_progress` computes the numbers on a seeded history, including a
   goal without enough data (`available: false`) (tests).
3. The Monday message is sent once per ISO week, after the briefing, never
   instead of it, and a failed send retries like the briefing (tests with a
   fake clock and fake push).
4. The new `garmin-mcp` tool is read-only, fail-soft and in the coach
   allowlist; `garmin-mcp/tests/test_http.py` and the coach tool list are
   updated.
5. `README.md`, both service READMEs, `.env.example` and
   `docs/coach-instructions.md` (set a metric with every race goal) are
   updated; the privacy note covers the second message.
6. The four-command check is green.
7. On the test host: `get_goal_progress` for the owner's goal, and one Monday
   message in the owner's chat.

**Requested:** 2026-10-09, by the owner, from the project review (feature 2).

**Delivered:** 2026-10-11, PR #29 (`mao/pr14-goal-progress-monday-report`),
squash-merged into `main` as `836a263`, no rebase needed. `coach-mcp` schema 7
to 8: migration 8 adds `goals.metric` and `goals.target_value` (with CHECKs;
old goals keep both NULL) and the tables `race_predictions`, `prediction_sync`
and `weekly_reports`. `set_goal` validates the metric (`race_5k`, `race_10k`,
`race_half_marathon`, `race_marathon`, `vo2max_running`) and the target (H:MM:SS
or MM:SS for races, ml/kg/min for VO2max); changing the metric needs a new
target. New read tool `get_goal_progress` (least-squares trend over 12 weeks,
the values 4 and 12 weeks ago, a projection at most 26 weeks ahead, reason codes
instead of prose, `available: false` without enough data). New read-only
`garmin-mcp` tool `get_race_predictions` (daily range up to 366 days, whole
seconds per distance, fail-soft), in the coach allowlist. The nightly history
gets a race-prediction part (one paced call, no charge or give-up on an item
error, the whole missed range asked again the next night; log word
`part=predictions`). `BriefingScheduler` sends the Monday report once per ISO
week after a sent briefing (same window, cutoff, late-start and retry rules;
text from the new `WeeklyTexts` catalogue in `messages.py`, `en` and `cs`, so
"in Czech" of the chapter became "in every catalogue language");
`COACH_WEEKLY_PUSH` (default in `config.py`, on while push is on) and the
container command `send-weekly-report` (writes no state). `README.md`, both
service READMEs, `.env.example`, `docker-compose.yml`,
`docs/coach-instructions.md` and the privacy note updated; `scripts/smoke-test.sh`
prints counts-only `get_race_predictions` and `get_goal_progress` lines; an 8
to 7 downgrade recipe (README, `coach-mcp/README.md`) was run verbatim on scratch
volumes by the implementer and the reviewer. No secret, mount or dependency
change. Criteria 1 to 6 met offline (`CHECK OK tree=746f4071402d garmin=258
gateway=80 coach=1193 e2e=OK`; about 100 implementer mutants and 42 reviewer
mutants, all killed or equivalent). Deviations: the shape of Garmin's daily
race-prediction payload was established from the library source and canned
payloads only (owner capture, section 5; confirmed 2026-10-11); decision D4 accepts the library's
ERROR log line on a Garmin 5xx, which contains the account display name (as for
PR 01 N2; test T-P10 pins it and will turn red on purpose when the redacting
log filter of section 5 lands); small decisions outside the spec (VO2max bounds
checked on the stored 1-decimal value, `points` 0 and not null without a
current value, `update_goal` with an unknown id and nothing else still says
"nothing to update") were accepted by the reviewer. Review took two rounds:
round 1 APPROVED with five non-blocking findings, O1 (pause before the
predictions call), O2 (trend window anchor and the Monday-only rule) and O4
(wrong doc rows) fixed in one commit and a delta review (round 2) APPROVED;
O3 and O5 stay open (section 5). Spec check found two blocking errors, both
fixed in the spec before implementation: T-P3 prescribed that the string
`"1290"` is dropped although `formatting.num` keeps numeric strings (the shaper
now has an explicit type guard), and the T-P10 DEBUG control. Still needs the
test host or the deploy (criterion 7): the owner's capture of the daily
race-prediction payload (spec section 11), `scripts/smoke-test.sh`,
`get_goal_progress` for the owner's goal after `set_goal` with a metric, and
one Monday message in the owner's chat (or `send-weekly-report` once).

### PR 15 — Searchable coach memory

`get_coach_context` returns the active goals and constraints and the latest
ten notes, so an older note or a closed goal cannot be found. Add read tools
to `coach-mcp`: `search_notes` (full-text query, optional date range and
session, newest first, limited) over an SQLite FTS5 index of the notes
(FTS5 is available in the image), `list_goals` (every status) and
`list_constraints` (active and resolved). A schema migration creates the
index for the existing notes and keeps it current on insert (notes are never
deleted).

Out of scope: searching session descriptions, semantic search, editing
notes.

**Done when:**
1. The migration indexes existing notes; a new note is found at once;
   Czech diacritics match with and without accents (tests).
2. A malformed full-text query gives a clear tool error, not an SQLite
   error (test).
3. `list_goals` and `list_constraints` return closed entries with their
   status (tests).
4. `coach-mcp/README.md` and `docs/coach-instructions.md` (search before
   asking the athlete again) are updated.
5. The four-command check is green.

**Requested:** 2026-10-09, by the owner, from the project review (feature 5).

**Delivered:** 2026-10-09, PR #18 (`mao/pr15-searchable-memory`),
squash-merged into `main` as `57b5854`, rebased after PR 09. Schema migration
6 creates `notes_fts` (FTS5, external content, `unicode61 remove_diacritics
2`), indexes the existing notes with a `rebuild` and keeps the index current
with an `AFTER INSERT` trigger (notes are never deleted); the spec said 4 and
was renumbered on the rebase. New read-only tools: `search_notes` (raw FTS5
query syntax, optional date range and session, newest first, limit 20
default and 50 maximum, `more` from a `limit + 1` probe), `list_goals` (every
status) and `list_constraints` (active and resolved). A malformed query
becomes a fixed `ToolError` without SQLite text (only `SQLITE_ERROR` is
wrapped, a locked database stays a database error, raised `from None`).
`INSTRUCTIONS`, `coach-mcp/README.md` (section Hledání v poznámkách and a
6 to 5 downgrade recipe, run verbatim on scratch volumes including a fresh
volume), `docs/coach-instructions.md` (search before asking again) and
`README.md` updated. No config value, secret, compose, script or dependency
change. All criteria met offline (`coach-mcp` 876 passed, `garmin-mcp` 186,
`gateway` 80, `E2E OK`; about 21 reviewer mutations red); no criterion needs
the test host. Deviations: none. Review took one round, APPROVED with no
blocking finding; the misleading Czech sentence about declension and the
untested `from None` were fixed on the rebase. Not verified: how the coach
behaves with the real model in claude.ai (it needs a deploy).

### PR 16 — Correlations

The history holds 90 days or more of daily values, enough to ask whether
load, sleep and recovery move together. Add a read tool `get_correlations`
to `coach-mcp`: two or more stored metrics (the `get_trends` catalogue), a
date range and a lag of 0 to 3 days, returning Pearson and Spearman
coefficients with the number of paired days, `available: false` below a
minimum number of pairs, and a fixed caveat that a correlation is not a
cause. Pure Python, no new dependency.

Out of scope: p-values and confidence intervals, more than pairwise
analysis, strength metrics before PR 09.

**Done when:**
1. The coefficients match reference values for seeded series, including a
   lag, ties (Spearman) and a constant series (no coefficient) (tests).
2. Days without one of the two values are skipped, never counted as zero
   (test).
3. `coach-mcp/README.md` and `docs/coach-instructions.md` (how to word a
   correlation for the athlete) are updated; this chapter replaces the
   "correlation analysis" item of section 5.
4. The four-command check is green.

**Requested:** 2026-10-09, by the owner, from the project review (feature 6).

**Delivered:** 2026-10-09, PR #19 (`mao/pr16-correlations`), squash-merged
into `main` as `85dcfb5`, rebased after PR 15. New `coach_mcp/correlations.py`
and the read-only tool `get_correlations`: 2 to 6 metrics of the `get_trends`
catalogue (46 names), a date range (default 90 days ending yesterday, at most
731) and `lag_days` 0 to 3; for every pair Pearson and Spearman coefficients
(stdlib `statistics.correlation`, average ranks for ties, 3 decimals) and
`n_pairs`, plus a fixed "correlation is not a cause" caveat. Below 14 paired
days the pair is `available: false, reason: too_few_pairs`, a series that does
not change on the paired days is `reason: constant`; days without a value are
skipped, never counted as zero. No schema change, config value, secret or
dependency. `INSTRUCTIONS`, `coach-mcp/README.md`, `README.md` and
`docs/coach-instructions.md` (how to word a correlation, Czech) updated;
`scripts/e2e_flow.py` calls the tool on the empty store. All criteria met
offline (`coach-mcp` 902 passed, `garmin-mcp` 186, `gateway` 80, `E2E OK`; 12
own mutants and the reviewer's extra ones red; the reviewer recomputed the
reference values independently); none needs the test host. Designer
decisions: `lag_days = k` pairs the first listed metric on day d with the
second on day d + k (both days inside the range); the minimum of 14 pairs is
fixed and returned as `min_pairs`; the four Garmin-watch `strength_*` totals
stay in the catalogue, Hevy strength metrics (volume, sets, e1RM) are not
offered. Deviation accepted: section 5 had no "correlation analysis" item, so
"replace" became removing the pointer "Correlation analysis is PR 16". Review
took two rounds: round 1 CHANGES with one blocking finding (a partial spec
error: the prescribed HTTP test used a lag and a range equal to the defaults,
so a server that dropped `lag_days` and the dates stayed green; fixed with one
more call, both mutants red) and four cosmetic items, all fixed; round 2
APPROVED.

### PR 17 — Sync-aware briefing

When last night's data is missing, the briefing guesses that the watch has
not synced yet (`briefing.py`, "hodinky možná ještě nesynchronizovaly") and
re-reads the full snapshot every retry; a morning without a sync can cost
about 91 Garmin Connect calls (PR 04 follow-up). Add a read-only
`garmin-mcp` tool `get_device_status` (the primary device and its last
upload time, from the library's `get_device_last_used` and `get_devices`; the
payload fields are confirmed on the owner's account first). The briefing
names the last sync time when data is missing, and the scheduler checks the
device status first on a retry and reads the snapshot again only when the
upload time has changed.

The upload time also settles open item PR 13 (a) (owner's answer, round 1): yesterday's missed-workout alert
(Garmin calendar) and missed-session alert (coach plan) are held back while the watch has not synced since the end
of yesterday, and a missed-session line built from a nightly check that ran before the last sync says so.

Out of scope: device settings, battery, alarms.

**Done when:**
1. `get_device_status` returns the shaped fields from canned payloads and is
   fail-soft (tests); the real payload shape is recorded in the PR.
2. A retry without a new upload makes one cheap call instead of the full
   read; a new upload triggers the full read (tests with a fake backend).
3. The missing-data line names the last sync time and falls back to today's
   wording when the time is unknown (tests).
4. Yesterday's missed-workout and missed-session alerts do not fire while the last sync is older than the start of
   today, a missed-session line whose nightly check predates the last sync is marked as possibly out of date, and
   with the sync time unknown both alerts behave as before (tests).
5. Tool lists, both service READMEs and `docs/coach-instructions.md` are
   updated.
6. The four-command check is green.
7. On the test host: a briefing before the morning sync names the last sync
   time.

**Requested:** 2026-10-09, by the owner, from the project review (feature 7); via `/mao`, questions and answers
round 1 (2026-10-11: shape capture of the device payloads, missed-session alerts added, no early send).

**Delivered:** 2026-10-11, PR #33 (`mao/pr17-sync-aware-briefing`), squash-merged into `main` as `31e3246`, no rebase
needed (the base `8c59f09` was current). `garmin-mcp`: new read-only tool `get_device_status` (no arguments;
`last_upload` from the library's `get_device_last_used` with `device`, `time_utc` to the second and `time_local` to
the minute, and `primary_device` from `get_devices` with `name`, `status`, `primary_activity_tracker` and
`registered_devices`; each section fail-soft, an upload time outside 2000 to 2100 or not an integer is unknown, ids
and serial numbers are never returned; 24 tools in `tools/list`). `coach-mcp`: `GarminReader.sync_check()` makes the
cheap check (the device status only, 2 Garmin Connect requests, bounded to 180 s) and `parse_last_sync` turns the
answer into a UTC and a local time; the device status is also the first call of the full read. The scheduler retry
gate (`SETTLE`, a 30-minute constant, no new configuration value): a retry asks for the upload time first and reuses
the previous full read when the time has not changed, the read was clean (every call `ok`, no section in error) and
it started at least 30 minutes after the upload; a new upload, an unsettled read, an unknown time or a failed check
give the full read as before, and nothing is given up or charged for a failed check. The missing-data line reads
`Missing: sleep (last sync with Garmin: yesterday HH:MM)` (and the Czech text; the new fields are in both
`messages.py` catalogues) and keeps the old wording when the time is unknown. Open item PR 13 (a) is settled:
yesterday's missed-workout and missed-session alerts are held back while the last sync is dated before the briefing
day (an event, not repeated the next day), a missed-session line whose nightly check ran before the last sync says
so (new `store.suggestion_times`, `get_plan` and `get_coach_context` unchanged), and with the sync time unknown both
alerts behave as before. Answer to the PR 04 follow-up "about 91 Garmin Connect calls": with the default
06:30 / 09:30 / 15 min window and sleep missing all morning, the worst case was 13 full reads x 7 = 91 requests; now
it is 1 full read and 12 checks (9 + 12 x 2) = 33 when the upload was settled before 06:00, 51 when it happened at
06:20, and as before plus the checks when a check fails or a section is in error. The tool, `send-briefing` and the
history job still read in full. Both service READMEs (tool lists, the briefing example, log lines),
`INSTRUCTIONS`, `docs/coach-instructions.md` and `scripts/smoke_test.py` (new section "Garmin: device status (last
upload)") updated. No migration (schema stays 8), no secret, mount, dependency or configuration change; no
gateway-side allowlist exists, so the tool is reachable from claude.ai after the deploy. Criteria 1 to 6 met
offline (`CHECK OK tree=ec2edf0c95cc garmin=294 gateway=80 coach=1270 e2e=OK`, run by the implementer and by the
reviewer; 61 implementer mutants and 17 reviewer mutants, all killed or equivalent except O1 and O2 below).
Deviations: the chapter was extended on the branch (commit `5086237`) with the missed-session alerts and the "possibly
out of date" line after the owner's answer in round 1, which made the Done-when list seven items; Done-when 4 reads
"older than the start of today" and the code compares the last sync's local date with the briefing day (confirmed by
the reviewer); the real payload shape was captured read-only on the owner's account on 2026-10-11 (key names and
types only, recorded in the PR body and in `garmin-mcp/README.md`; the upload time is epoch milliseconds in UTC and
account-wide, `get_devices` has no per-device time, so the device that uploaded is named and not assumed to be the
primary one). Review took one round: APPROVED with eight non-blocking findings O1 to O8 (open items, section 5). Spec
check: 0 blocking, 6 non-blocking (N1 to N6), all applied by the implementer; the implementer's first check run
failed once on a test of its own that asserted the order of two concurrent library calls (fixed, six repeated test
stages green). Still needs the test host or the deploy (criterion 7): `docker compose up -d --build garmin-mcp
coach-mcp`, `scripts/smoke-test.sh` printing the new section, and a `send-briefing` before the phone syncs the
watch whose message names yesterday's last sync time; the full procedure is in the deploy bundle
`DEPLOY-2026-10-11-pr17.md`.

### PR 18 — History repair

PR 05 review finding N5: `HistoryJob._fetch_snapshot` stores a day as fetched
even when `recovery_values` skipped a section in error, so one transient
error of a Garmin endpoint during the backfill leaves that metric NULL for
good on every day older than three days. Record per day which sections were
in error and let such a day be fetched again a limited number of times after
the D+3 window, a few days per night, without charging an outage (the
probe rule stays). Include the missing tests of N8. N6 and N7 stay open
unless the fix closes them as a side effect.

Out of scope: re-fetching days whose values are missing because the watch
was not worn (no section error), the activity part.

**Done when:**
1. A day with a section in error is fetched again after D+3 until it
   succeeds or reaches its limit; a complete day is not (tests).
2. The outage scenario of the checklist (every call fails for N runs, then
   recovers) gives up no day and charges nothing (test).
3. The schema migration keeps existing rows; how rows stored before it are
   treated is stated in the PR (tests).
4. The N8 guard branches have failing tests when mutated.
5. The four-command check is green.

**Requested:** 2026-10-09, by the owner, from the project review (item U7).

**Delivered:** 2026-10-10, PR #26 (`mao/pr18-history-repair`), squash-merged
into `main` as `0c53bac`, no rebase needed. `coach-mcp` schema 6 to 7: migration
7 adds three columns to `history_days`, `recovery_missing` (comma list of
sections never read without an error), `recovery_repairs` (charged answered
repair calls, `NOT NULL DEFAULT 0`) and `recovery_repaired_on`; the regular
fetch records which sections stayed in error, and after the D+3 window such a
day is fetched again, newest first, up to 3 charged answered attempts per day
and 5 answered repair calls per local date. Repairs run before the final probe,
so their errors follow the held-error / probe rule; a section that fails for
every day of the run is not asked for and nothing is charged (the outage
scenario gives up no day). New `repair_history_recovery` writes only the
repaired sections and never touches `recovery_fetched_on`; the run log line
gets a `repaired=` counter. Rows stored before migration 7 do not know their
section errors: the migration marks every recovery section whose columns are
all empty as missing, so each such day gets one more call (days without data
because the watch was not worn are among them, since old rows cannot tell them
apart; at most 5 calls a night); rows that never loaded keep no mark. The N8
guard tests T21 (probe `isinstance`) and T22 (`given_up` for recovery only,
`any`) are in. `README.md` and `coach-mcp/README.md` (nightly history, log
line, data bullet, a 7 to 6 downgrade recipe, run on scratch volumes by the
implementer and the reviewer) updated. No tool, config value, secret,
dependency, athlete-facing text or `garmin-mcp` change. All four criteria met
offline (`CHECK OK tree=f67a034bc850 garmin=186 gateway=80 coach=1060
e2e=OK`; the reviewer ran 31 mutants against the two test files, every one
of the spec's table red). Deviations: three test setups differ from the spec
text (T9 cuts the backfill with a 429 because candidates are read at the start
of a run, T16 asserts the exact call lists of the three outage nights, T20
seeds `fetched_on = D+3`; the reviewer judged all three correct), and the
legacy-row treatment above widens the chapter's "watch not worn is out of
scope" for old rows only. Review took two rounds: round 1 APPROVED with six
non-blocking findings, O1, O2 (tests for the "every" charge rule and for
repair answers not counting as evidence, with `_confirm` pinned), O4, O5 and
O6 (wording and wrapping) fixed in round 2, a delta review APPROVED; O3 stays
open (section 5). Spec check found one blocking error, T19 expected
`unreachable` for `auth = "missing"` where the code gives `auth` (found by
running the test, fixed in the spec before implementation). Still needs the
test host or the deploy: how Garmin answers a section of a day without data
(an error or an empty section; it decides how many legacy days settle after one
call) and the nightly `repaired=` counter on the live database, which may
still be at schema 3 and then migrates 3 to 7 in one start.

### PR 19 — More Garmin signals

`garminconnect` 0.3.17 has calls the coach does not use yet. Add to
`garmin-mcp`: a read tool `get_running_tolerance` (Garmin's weekly running
load tolerance over a range, `get_running_tolerance`), and two fail-soft
sections in `get_activity`: the exercise sets recorded on the watch for
strength activities (`get_activity_exercise_sets`: exercise, reps, weight)
and the weather of the activity (`get_activity_weather`). The payload shapes
are captured from the owner's account first and stored as canned fixtures.

Out of scope: writing exercise sets, creating Hevy workouts from watch
sessions (a later chapter), per-activity training-load metrics (already in
`list_activities`).

**Done when:**
1. Each new part is shaped from canned payloads and degrades to
   `available: false` or `error` (tests); the library's own log lines for the
   new calls are checked (lesson from PR 01).
2. `FakeGarmin.responses` has a key for every new call; the tool list in
   `garmin-mcp/tests/test_http.py` is updated.
3. `garmin-mcp/README.md`, `README.md` and `docs/coach-instructions.md` (use
   the tolerance when raising weekly running volume) are updated.
4. The four-command check is green.
5. On the test host: the tolerance and one strength activity with its sets
   are returned.

**Requested:** 2026-10-09, by the owner, from the project review (feature 8).

**Delivered:** 2026-10-10, PR #27 (`mao/pr19-more-garmin-signals`),
squash-merged into `main` as `2723b83`, rebased after PR 18 (clean, no
conflicts). `garmin-mcp` only: new read tool `get_running_tolerance` (weekly
load tolerance, `weeks` 1 to 26 and optional `end_date`; an unknown, empty or
404 answer is `available: false`, a library error a tool error) and two
fail-soft `get_activity` sections, `exercise_sets` (exercise, reps, weight,
capped at 60 sets) and `weather` (location fields never output); each
degrades to `available: false` or `error` and survives a failed HR-zones
call. `get_activity` also returns `active_sets` and `total_reps` from the
activity summary (not in the chapter, added from the capture so the coach sees
the volume when the per-set endpoint has no usable sets). New fixtures,
`FakeGarmin.responses` keys, shaper and HTTP tests, and `test_library_logs.py`,
which runs the real `garminconnect` client against a stub and checks the
library's own log lines (success and 404 log nothing at WARNING or above, a
5xx logs the API path, the logger stays at WARNING or above under DEBUG).
`garmin-mcp/README.md`, `README.md`, `docs/coach-instructions.md` (use the
tolerance when raising weekly running volume), `INSTRUCTIONS` and
`scripts/smoke_test.py` (a "Garmin: PR 19 signals" block as evidence for
criterion 5) updated. No coach-mcp, schema (stays 7), config, secret,
compose or dependency change. Criteria 1 to 4 met offline (`CHECK OK
tree=a86c1c9d32b4 garmin=245 gateway=80 coach=1060 e2e=OK`; 46 mutants run,
none survives after round 2). Deviations: the tolerance part of criterion 5
cannot be met on the owner's account, because Garmin returns an empty list for
weekly 56 and 182 days and for daily 28 days (only 1 run in 56 days, so the
account has no data and the request is not the cause); the tool answers
`available: false` and stays in for watches that have the feature. The
fixtures are synthetic, built from the shape-only captures of 2026-10-10 (the
chapter said "captured from the owner's account"; no values leave the
account). The library's 5xx log lines are accepted explicitly: the API path
(with the activity id for the two activity calls) and Garmin's own error
message, as `get_activity` already does. Review took two rounds: round 1
APPROVED, 18 mutants run, 4 survivors that were test gaps on correct code (max
weight, lower-case `REST`, probability tie, nested key type guard), covered in
round 2; round 2 was a delta review after the rebase, APPROVED. Spec check: 0
blocking, 6 non-blocking, all folded in. Still needs the test host (criterion
5, after the deploy): `scripts/smoke-test.sh`, the sets of one strength
activity (or `active_sets` and `total_reps` with `available: false`), a real
non-empty tolerance entry, and the units of weight (grams), temperature (°F)
and wind (mph) compared with Garmin Connect.

### PR 20 — Operations alerts via Telegram

`scripts/healthcheck.sh --quiet` reports problems only to whoever reads the
cron mail. Let it send them to the owner's Telegram chat through the existing
bot: `--notify` posts the problem lines (never health values, the token or
the chat id on a command line) when the set of problems changes, and once
when everything is healthy again. The `coach-mcp` scheduler or history job
reaching `stopped` and a failed backup (PR 11) are reported the same way.

Out of scope: a second bot or channel, alerts on health values, host
monitoring.

**Done when:**
1. A changed problem set sends one message, an unchanged one sends nothing,
   and the recovery message is sent once (offline stub harness).
2. The token is never on a command line or in output; a missing token or
   chat id makes `--notify` fail with a clear message without hiding the
   problems themselves.
3. `README.md` (Provoz, cron line) is updated.
4. The four-command check is green.
5. On the test host: a stopped `coach-mcp` produces one message and its
   restart one recovery message.

**Requested:** 2026-10-09, by the owner, from the project review (item U5).

**Delivered:** 2026-10-11, PR #30 (`mao/pr20-operations-alerts-telegram`),
squash-merged into `main` as `ba6de49`, rebased onto PR 14 without conflicts
(plus one README sentence in PR 14's Telegram privacy note pointing to
Operations alerts). Scripts, tests and docs only: no `coach-mcp` or
`garmin-mcp` change, no migration (schema stays 8), no image rebuild, no
compose, secret or dependency change. `scripts/healthcheck.sh` gives every
failing check a stable problem key; `--notify` posts one plain-text message
(`New:`, `Still:`, `Resolved:`) when the set of keys changes and once when it
becomes empty (state in `${XDG_STATE_HOME:-~/.local/state}/fitness-coach-mcp/`,
never in the repo); a changed line under the same key is not a change. New
section 8, backup age: with `BACKUP_DIR` set, a missing, unreadable, too old
(`BACKUP_MAX_AGE_HOURS`, default 26) or future-dated newest archive is the
problem `backup`, which is how a failed `scripts/backup.sh` reaches Telegram.
A missing or malformed chat id or token, an unreadable token file, a busy lock,
an unwritable state directory, a failed `mktemp -d` or a message Telegram did
not accept ends with `Notify failed: ...` and the current problems on stderr,
exit 1 and the state untouched. The token reaches `curl` on stdin, the chat id
and text travel in a temporary file, neither is on a command line or in any
output. `TELEGRAM_TOKEN_RE` and `telegram_api` moved from `setup-secrets.sh` to
`lib.sh`. New offline harness `scripts/test-healthcheck.sh` (stub `docker` and
`curl`, argv shims), run by `scripts/e2e-test.sh`, ends with `HEALTHCHECK OK`.
`README.md` (Operations row and cron line, "Operations alerts (Telegram)",
Backups bullet, Troubleshooting rows) and `.env.example`
(`BACKUP_MAX_AGE_HOURS`) updated. Criteria 1 to 4 met offline (`CHECK OK
tree=958f957d3f39 garmin=258 gateway=80 coach=1193 e2e=OK`; 47 implementer and
14 reviewer mutants, all killed). Deviations: a healthy run whose notify step
fails prints nothing on stdout and exits 1; a ninth failure reason (`mktemp`)
and a state file read with `errors="replace"` came from review 1; the owner
decisions D4, O5 and O7 of section 5. Review took three rounds: round 1
CHANGES (one blocking test gap: the unreadable-token case that criterion 2
lists had no harness row, so its guard survived mutation; eight non-blocking
findings), round 2 APPROVED, round 3 a delta review after the rebase
APPROVED. The branch was squashed to one commit before the push because an
earlier commit carried a random-looking 10-digit harness chat id from the spec
(lesson in `lessons.md`). Spec check: 0 blocking, 8 non-blocking, all applied.
Still needs the test host (criterion 5, owner-run, after the batch's other
deploys): stop `coach-mcp` once outside 03:30 to 04:30 and 06:30 to 09:30, one
message, restart, one recovery message; the full procedure is in the deploy
bundle `DEPLOY-2026-10-11.md`.

### PR 21 — Static checks in the test stage

The Python code is fully typed, but nothing checks it. Add `ruff` and `mypy`
to the `test` stage of `garmin-mcp`, `gateway` and `coach-mcp` (the check
stays inside Docker; no CI), configured in each `pyproject.toml`, hash-locked
as dev dependencies through `scripts/lock-deps.sh`, and fix what they find.

Out of scope: formatting the whole tree, CI.

**Done when:**
1. Each test build fails on an introduced lint error and on an introduced
   type error (mutation), and passes on the tree.
2. The new dependencies are hash-locked with a reason in the spec.
3. The fixes change no behaviour: the existing tests pass unchanged.
4. The four-command check is green.

**Requested:** 2026-10-09, by the owner, from the project review (item U6).

**Delivered:** 2026-10-11, PR #35 (`mao/pr21-static-checks`), squash-merged
into `main` as `81d6310`, no rebase. `ruff` 0.17.0 (lint of `src` and `tests`)
and `mypy` 2.4.0 (`strict = true`, `src`) run as two separate `RUN` steps in
the `test` stage of `garmin-mcp`, `gateway` and `coach-mcp`, configured in each
`pyproject.toml` (`select = ["E4", "E7", "E902", "F", "B", "RUF100"]`; no
formatter, no import sorting, no line-length rule). The six dev locks gain
exactly ruff, mypy and mypy's four own requirements (`mypy-extensions`,
`pathspec`, `librt`, `ast-serialize`) through `scripts/lock-deps.sh`; the
runtime locks and images are unchanged. The fixes in `src` (20 files) are type
annotations, typing-only imports and 21 `# type: ignore[code]` comments with a
reason each; no behaviour changes (AST equivalence of base and head, identical
`tools/list`, the base `tests/` pass on the new `src`). Five one-line lint
edits in four existing `coach-mcp` tests. New `tests/test_static_config.py`
(identical in the three services, 11 tests each) runs the installed ruff and
mypy with the service's own `pyproject.toml` on canned snippets, one row per
`select` entry and one for `strict`. `scripts/check.sh` requires the ruff
summary (`All checks passed!`) and the mypy summary (`Success: no issues
found`) in each build log, so a stage that stops running them fails with
`CHECK FAILED step=<svc>`. `README.md`, both service READMEs and
`.claude/mao/project.md` updated. No compose, secret, schema or runtime
dependency change; no deploy needed. All four criteria met offline (`CHECK OK
tree=ec3de085d85d garmin=305 gateway=91 coach=1281 e2e=OK`; 12 build-level
mutants and 21 config mutants caught, the `check.sh` guard proven). Owner
decisions (2026-10-11, both "recommended"): the new dev dependencies (ruff,
mypy and mypy's four requirements, dev locks only) approved; ruff lints `src`
and `tests`, which reads Done-when 3 ("the existing tests pass unchanged") as
"no test is adapted to the fixes" (the base tests also pass unchanged on the
new `src`: 294 / 80 / 1270). Deviation: `E902` is in `select` and each config
test has a `missing.py-E902` row, beyond the spec, because the spec's claim
that a missing path is `E902` "whether selected or not" was false (spec check
N1, review O1); fixed in a second commit and delta-reviewed. Review took two
rounds: round 1 APPROVED (0 blocking, 6 non-blocking), round 2 a delta review
of the `E902` fix APPROVED. Spec check: 0 blocking, 5 non-blocking. Accepted
and left open: (O2) a bare `# type: ignore` without a code is still accepted
(`enable_error_code = ["ignore-without-code"]` would close it, no occurrence
in the tree); (O3) the config tests guard `select` and `strict` only, so a
path-scoped weakening (`per-file-ignores`, `exclude`, a mypy per-module
override) shows only in a diff of `pyproject.toml`; a path removed from the
`ruff check` line (`ruff check src`) is accepted in writing in spec 6.3 and
not caught by `E902`. Note for later PRs (for example PR 22): every branch must
now pass ruff and strict mypy in the test stage, so new code is annotated and
lint-clean before the check; the expected test counts are now 305 / 91 / 1281
(garmin / gateway / coach). Nothing needs the test host.

### PR 22 — Two-way Telegram

The briefing reaches the owner's phone, but answering it means opening
Claude. Let `coach-mcp` read the owner's replies to the bot: long polling of
`getUpdates` (outbound HTTPS only, so no port is published), accepting
messages only from the configured chat id. Commands, deterministic and
without an LLM: `/dnes` (today's briefing), `/plan` (this week),
`/done <id>`, `/skip <id>` and free text, which becomes a note for today. The
athlete's own command counts as the confirmation that section 1 requires.
This reverses "any interaction with the bot beyond receiving the briefing"
in the PR 08 chapter.

Out of scope: an LLM in the bot, group chats, moving or creating sessions
from Telegram, media messages.

**Done when:**
1. Messages from any other chat or sender are ignored and never logged with
   their text; the update offset survives a restart (tests with a fake
   transport).
2. Each command has a test for success and for a bad argument; replies are
   plain text in the configured language (`COACH_LANGUAGE`, from the
   `messages.py` catalogue), tested in both catalogues.
3. Logs carry outcome words only; the README privacy note covers inbound
   messages.
4. The four-command check is green.
5. On the test host: `/plan` and a free-text note work from the owner's
   phone, and the note is returned by `get_coach_context`.

**Requested:** 2026-10-09, by the owner, from the project review (feature 3); questions and answers round 1 (2026-10-11).

**Delivered:** 2026-10-12, PR #37 (`mao/pr22-two-way-telegram`), squash-merged
into `main` as `8f30e91`, no rebase. `coach-mcp` gets a third background task
next to the scheduler and the history job, `InboundPoller` in the new module
`coach_mcp/inbound.py`: long polling of the Bot API method `getUpdates`
(outbound HTTPS, no port), handling only text messages of the private chat
whose chat id and sender id both equal `COACH_TELEGRAM_CHAT_ID`, answering by
`sendMessage` as plain text. Commands: `/dnes` or `/today` (the current
briefing text, records nothing), `/plan` (this week with session ids), `/done
<id>`, `/skip <id>`, `/help`; any other text is a note for today (2000
characters at most, dated by the day it was sent). Text after the id, a bad,
unknown or unchangeable id are bad arguments and change nothing. Schema
migration 9 adds the table `telegram_inbound` (the next update offset, one
row); a note and its offset are written in one transaction, the first start
(or another bot) discards the backlog, and an offset older than two days is not
sent. The 9 to 8 way back is in `coach-mcp/README.md`. Replies come from the new
`ReplyTexts` catalogue in `messages.py` (English and Czech, 24 fields each,
exact text pinned in both). `COACH_TELEGRAM_INBOUND` (default `true`; on only
with the briefing on and a private chat id, `false` turns it off) is in
`docker-compose.yml`, `.env.example` and both READMEs; `/healthz` gets the key
`inbound`, `scripts/healthcheck.sh` reports a stopped poller as
`coach-inbound`, and `setup-secrets.sh telegram-chat-id` hints when no chat is
found. Logs carry outcome words only. No new tool, secret or dependency
(`httpx2` was already locked). The README privacy note covers what the owner
writes and the answers (for example the week's plan). Criteria 1 to 4 met
offline (`CHECK OK tree=c67391b7a5ca garmin=305 gateway=91 coach=1542 e2e=OK`;
188 mutants, none survived). Deviations: criterion 2 reworded by the owner's
answer to question Q1 before the spec (replies come from the catalogue in both
languages, not Czech text outside it); the test count is 1542, not the spec's
1515 (18 tests apply spec-check notes N1 and N2, 9 come from review 1); the
spec-check note N4 (a test clock for `time.monotonic()` in `step`) was first
declined by the implementer and applied in round 2 as a patch of the clock for
the `inbound` module only, without a constructor change. All six clarifying
questions were answered with the recommended default. Review took two rounds:
round 1 APPROVED (0 blocking, 8 non-blocking, O1 to O5 and O8 fixed in one
commit), round 2 a delta review of that commit APPROVED, `src/` unchanged.
Spec check: 0 blocking, 4 non-blocking, all applied or answered. Still needs
the test host (criterion 5, owner-run, after the batch's earlier deploys): `/plan`
and a free-text note from the owner's phone, the note returned by
`get_coach_context`, and the evidence for the Bot API facts that were not run
(`getUpdates` with a JSON POST body, the offset and 24-hour rules, 409 for a
second reader or a webhook, the shape of a private text message, a photo
without `text`, the `/command@botname` form); the procedure is in the deploy
bundle `DEPLOY-2026-10-12.md`. Open non-blocking: (O6) an `update_id` of 2**63
or more would stop the queue; (O7) every `sqlite3.Error` is treated as
transient (spec decision D11); both in section 5.
Live verification 2026-10-12 (owner deploy per `DEPLOY-2026-10-12.md`, live at
`59de7f3`): `/healthz` schema 9 and `inbound` `on`, `healthcheck.sh` and
`smoke-test.sh` green, no `inbound poll=` line. Criterion 5 met: `/plan` and a
free-text note from the owner's phone (`inbound cmd=plan status=ok`,
`cmd=note status=ok`), today's note count 0 to 1, the note returned by
`get_coach_context` (gateway audit: outcome `ok`). The week held no session, so
no `#id` line, `/done` or `/skip` ran on live data.

### PR 23 — One Plan module for sessions and notes

The rules for changing a planned session and for taking a note exist twice:
on the MCP path in `tools.update_session` and `tools.add_note`
(`coach-mcp/src/coach_mcp/tools.py`), and on the Telegram path in
`InboundPoller._mark` and its note branch (`coach-mcp/src/coach_mcp/inbound.py`,
PR 22). The two copies have drifted:
- `/done` refuses a session dated after today; `update_session` accepts it.
- The tool path reports a moved session whose copy a later plan superseded;
  the Telegram path does not.

The Telegram note limits (`NOTE_MAX`, the NUL check) are a copy of the
`Field(max_length=2000)` and `_no_nul` of the tool path. `_mark` reads the
session and checks it, then calls `update_session`, which reads it again.
`_mark` catches the MCP `ToolError` to notice a session that changed in
between, and `tests/test_inbound.py` reaches that window by patching
`store.get_session`.

Move these rules into one new Plan module in `coach_mcp` that both paths
call. Marking a session and adding a note are each one call that checks and
writes in one SQLite transaction:
- Marking a session covers done, skipped, back to planned, and moved with a
  new date, each with an optional note.
- Adding a note checks its length, NUL characters and empty text. It takes an
  optional session, and on the Telegram path it is written together with the
  update offset, as today.

Each call returns the result or a refusal reason from a closed set: not
found, superseded, moved to another session, that copy superseded, future,
not planned, same date, past date, text too long. The MCP tools turn a
refusal into the English `ToolError` text they give today. Telegram turns it
into the `ReplyTexts` catalogue. A reason with no reply yet gets one in both
catalogues. Neither path keeps rules of its own.

Owner decision (2026-10-15): a session dated after today cannot be marked
`done` on either path. This is the only change in behaviour.

Out of scope: new commands or tools, schema changes, other wording changes.

**Done when:**
1. The session and note rules are checked only in the Plan module. `tools.py`
   and `inbound.py` keep no status checks and no note limits of their own,
   and `inbound.py` no longer catches `ToolError`.
2. Each refusal reason has a test against a temporary Store, through the Plan
   module. Because the check and the write share one transaction, the test
   that patched `store.get_session` to race is deleted, not ported.
3. `update_session` refuses `done` for a session dated after today, with a
   clear message (test). The tool description and `coach-mcp/README.md`
   say so.
4. Apart from criterion 3, the MCP tools return the same results and error
   texts as before, and every existing Telegram reply is unchanged in both
   catalogues. Existing tests are rewritten only where they reached past the
   interface, and the PR lists them.
5. Inbound tests cover the mapping from a refusal to a reply. No inbound test
   patches `tools` or `store` internals.
6. The four-command check is green.

**Requested:** 2026-10-15, by the owner, from the architecture review of
2026-10-15 (candidate 1); owner decision on a future `done` the same day.

**Delivered:** 2026-10-16, PR #44 (`mao/pr23-plan-module`), squash-merged into
`main` as `b85a111`, no rebase. New `coach_mcp/plan.py` holds the rules for
marking a planned session and for adding a note (`note_text`, `mark_session`,
`add_note`); they return `Marked | Noted | Refusal` with a closed `Reason` set
and never read a clock (`today` and the note day come from the caller).
`Store.transaction()` (SQLite `BEGIN IMMEDIATE`, commit at the end of the
block, rollback on an exception) makes the check and the write one transaction
on both paths, so the race window of the old Telegram `_mark` is gone; the SQL
of `update_session_status` and `moved_to` exists once in shared helpers.
`tools.update_session` and `tools.add_note` keep only argument parsing and
`refusal_error(refusal, field)` (a `match` with `assert_never`, so a reason
without a text fails mypy); `inbound._mark` and the note branch of `_handle`
call Plan and map a refusal through `refusal_reply` to the `ReplyTexts`
catalogue. `NOTE_MAX`, the NUL and length checks, the session reads and the
`ToolError` import are gone from `inbound.py`. No schema change (schema stays
9), no migration, no new tool, config value, secret or dependency. All six
criteria met offline (`CHECK OK tree=2b17695a897d garmin=424 gateway=91
coach=1560 e2e=OK`; coach 1542 to 1560, 18 new tests; all 34 mutants of the
spec table killed, and of the reviewer's 19 own 12 killed and 7 survived, see
below). Owner decision of 2026-10-15 in effect: a session dated after today
cannot be marked `done` on either path, re-marking an already `done` future
session included; `skipped` and `planned` still work. Deviations (accepted in
review): Plan has 12 refusal reasons, not the chapter's 9 (MOVED, TEXT_EMPTY
and TEXT_NUL added, spec D3), the set is asserted equal to `set(Reason)` in
three test files; Telegram gets one new reply, `session_moved_superseded`, in
both catalogues for a moved session whose copy a later plan replaced (it used
to get the wrong `session_moved_to`; no existing string changed, spec D7); a
direct call with a 2001-character note now raises `ToolError` instead of
`sqlite3.IntegrityError` (over MCP nothing changes). One test was deleted
(`test_a_session_that_changes_under_the_command_is_refused`; the guarantee is
held once by
`test_store.py::test_a_transaction_holds_the_write_lock_from_its_first_read`)
and eight rewritten, all listed in the PR body. Review took one round:
APPROVED (0 blocking, 4 non-blocking). Spec check: 1 blocking (the negative
greps G3 and G4 needed `-w`), 4 non-blocking, fixed in the spec before
implementation. Open non-blocking: O1 to O4, section 5. Nothing needs the test
host; not tested on the live stack. Deploy: rebuild `coach-mcp`
(`DEPLOY-2026-10-16.md`).

### PR 24 — History view

Every reader of the stored history rebuilds the training day from the raw
rows of `coach-mcp/src/coach_mcp/store.py` (`history_rows`,
`strength_workout_rows`, `hevy_templates`, `hevy_sync_state`,
`race_prediction_rows`). The same steps are copied across modules:
- Range defaults and limits, in `trends.py`, `correlations.py` and
  `strength.py`, with a variant in `tools.get_plan`.
- Parsing the `activities` JSON column, in `history.day_activities` and
  `trends._activities`, with different error handling.
- Assembling Hevy coverage, templates and strength day totals, in
  `history.py`, `strength.py` and twice in `trends.py`.
- A finite-number guard, six times: `history`, `trends`, `briefing`,
  `strength`, `reconcile` and `goals`.

Read-side tests seed write-side storage shapes through
`tests/history_helpers.py`.

Add a History view over the Store. Given a date range, it returns finished
day values:
- the recovery values;
- the parsed activities with their settled flag;
- the strength day merged with Hevy coverage;
- the race predictions.

The view also owns range defaults and limits. JSON parsing and the coverage
rules live only in the view. The finite-number guard becomes one shared
helper, which modules that read fresh Garmin data (for example the briefing)
also use. `trends`, `correlations`, `goals`, `strength` and the reconcile step of
`history.py` read through the view, and `tools.get_plan` uses its range
defaults. The write side (the
nightly job, migrations, the writes in `store.py`) is unchanged.

Out of scope: schema changes, the nightly job (PR 27), new metrics or tools.

**Done when:**
1. No module outside the view parses the `activities` column, assembles Hevy
   coverage, templates or totals, or defines its own range defaults. One
   finite-number guard remains. The PR states the searches used to check
   this.
2. The view has its own tests, against a temporary Store, for activities JSON
   edge cases (missing, malformed, not a list), Hevy coverage boundaries, and
   range defaults and limits.
3. Tests of trends, correlations, goals and strength build their input from
   view values. Tests that only re-checked parsing or coverage through those
   modules are deleted in favour of the view's tests, and the PR lists them.
4. `get_trends`, `get_weekly_report`, `get_correlations`, `get_goal_progress`,
   `get_strength_progress` and `get_plan` return the same JSON for the same
   stored data. Where the two old JSON parsers treated a malformed row
   differently, the PR states which handling is kept.
5. The four-command check is green.

**Requested:** 2026-10-15, by the owner, from the architecture review of
2026-10-15 (candidate 2).

**Delivered:** 2026-10-16, PR #46 (`mao/pr24-history-view`), squash-merged
into `main` as `ab2d76d`; the branch started on `b85a111` (PR 23) and was
rebased once onto `f0cbbb8` (PR 25, garmin-only) without a conflict. New
`coach_mcp/history_view.py` (`HistoryView`, `history_range`, `plan_range`, the
re-fetch constants `REFETCH_DAYS`, `MAX_FAILURES`, `OVERLAP`) turns the stored
rows into finished values (recovery values, parsed activities with their
settled flag, Hevy strength days merged with the coverage, race predictions)
and owns the range defaults and limits. New `coach_mcp/values.py` is the one
finite-number guard (`finite`, `finite_float`, `whole`, `valid_id`) plus
`parse_date`, moved from `tools.py`; it is the only module that calls
`math.isfinite` (12 hits in 8 modules on the base, an AST test keeps it so).
`trends`, `correlations`, `goals`, `strength.get_strength_progress`,
`history._reconcile` and `tools.get_plan` read through the view; each reader
gains a keyword-only last parameter `history=None`, so `server.py` and
`scheduler.py` are unchanged, and the write side is untouched. No tool,
argument, description, `INSTRUCTIONS`, env variable, dependency, log line,
error text, schema or data change. All five criteria met offline (`CHECK OK
tree=6f93f135f082 garmin=449 gateway=91 coach=1633 e2e=OK`, reviewer, on
`dce4f7c`; coach 1560 to 1633). Criterion 4 by a golden:
`tests/test_history_golden.py` compares 70 entries (69 reader calls of the six
tools, 17 of them error texts, plus the reconcile step) as exact JSON text
with `tests/history_golden.json`, committed first and green on the old code
(recorded on `da44156`, never regenerated; the reviewer regenerated it
byte-identical). Mutation: the 41 mutants of the spec table all killed.
Malformed-row handling kept (the stricter rule wins for every reader; all rows
the nightly job never writes): an unreadable stored `date` in the range is
skipped by every reader (it was a tool error in `get_trends`,
`get_correlations` and `get_weekly_report`, and crashed the reconcile step);
activities without a readable `activities_fetched_on`, or stored as a BLOB,
are ignored; `days_with_data` counts a day only with a readable fetch date.
Deviations: spec D4 said only `get_weekly_report` raised on an unreadable
date, but `get_trends` and `get_correlations` raised too (the code and the
golden are right; review O2); spec-check notes N1 to N5 applied beyond the
spec text (test imports, 12 extra view cases killing six surviving predicate
mutants, `is not None` instead of `or`, the AST guard also rejects
from-imports of `isfinite`, `isnan` and `isinf`, `weather._location` keeps its
bool coordinate behaviour). Review took one round: APPROVED (0 blocking, 5
non-blocking). Spec check: 0 blocking, 6 non-blocking, all applied. Open
non-blocking: O1, O3, O4, O5 and N6, section 5. Nothing needs the test host.
Deploy: rebuild `coach-mcp` (`DEPLOY-2026-10-16.md`).

### PR 25 — Garmin sections in one place

Section 1 says that a section Garmin cannot provide returns `"error"` or
`"available": false` instead of failing the whole call. That rule has no
module of its own; it is spread over the `garmin-mcp/src/garmin_coach_mcp/`
modules:
- `garmin_client.py` has `call_many` and `section_error`.
- `health.py` has `UNAVAILABLE` and the private `_section`, which
  `activities.py`, `body.py` and `performance.py` import.
- `performance.py` copies the `_section` try/except twice (race predictions,
  running tolerance).
- `activities.none_if_missing` and `body._pair_section` are further variants,
  `body.py` imports `performance._day`, and `get_activity` unwraps section
  errors by hand.

Tests re-check the degrade path in each module by monkeypatching a shaper to
raise.

Add one interface on `GarminService`, or in a module next to it, that takes
a declaration of sections and returns the shaped dict. Each section declares
its library call, its shaper, whether a 404 means empty, and whether the
section is required. The interface:
- runs the calls concurrently;
- aborts on an authentication failure, as today;
- turns a 404 into empty;
- turns a failed or badly shaped section into the existing `"error"` or
  `"available": false` value, with the same log lines;
- fails the whole tool only when a required section fails.

Every tool module uses the new interface, and no tool module imports a
private name from another tool module.

Out of scope: new tools, changes to response shapes, tool registration in
`server.py` (PR 28).

**Done when:**
1. Every tool response is unchanged for the canned payloads, including
   degraded sections. The existing tool tests pass, changed only where they
   patched internals, and the PR lists them.
2. The rules are tested once against the new interface with `FakeGarmin`: 404
   as empty, a section in error, a shaper exception, the authentication
   abort, and a required section. The per-module shaper monkeypatches are
   deleted.
3. A test fails when a tool module imports an `_`-prefixed name from another
   tool module.
4. Library log lines and their privacy are unchanged
   (`tests/test_library_logs.py` passes unchanged).
5. `garmin-mcp/README.md` describes how a new tool declares its sections.
6. The four-command check is green.

**Requested:** 2026-10-15, by the owner, from the architecture review of
2026-10-15 (candidate 3).

**Delivered:** 2026-10-16, PR #45 (`mao/pr25-garmin-sections`), squash-merged
into `main` as `f0cbbb8`; the branch was rebased once onto `b85a111` (PR 23,
coach-only) without a conflict and the reviewer ran the check on the rebased
tree. New `garmin_coach_mcp/sections.py` (`Call` with `missing_is_empty` and
`required`, `Section` with `uses` and `if_error`, `fetch_sections`,
`UNAVAILABLE`, `error_value`) holds the fail-soft rule once (404 as empty, a
section in error, a badly shaped payload, the authentication abort, a required
call) on top of `GarminService.call_many`, which stays the concurrency and
authentication engine; `garmin_client.py` is unchanged. Nine tool functions in
four modules declare calls and sections: `recovery_snapshot`, `device_status`
and the per-day fallback of `hrv_trend` (`health.py`); `performance_metrics`,
`race_predictions`, `running_tolerance` and `gear` (`performance.py`);
`nutrition` (`body.py`); `get_activity` (`activities.py`). Removed:
`health._section`, `performance._day` (now public `formatting.date_prefix`),
`activities.none_if_missing`, `body._pair_section` and two copies of the
try/except. `tests/test_module_boundaries.py` fails when a tool module imports
an `_`-prefixed name from another one (positive control on the old source:
`health._section` and `performance._day`, in three modules).
`tests/test_section_answers.py` compares the exact answers of the nine tools
in 302 scenarios with a recording made on the old code first (commit
`5a1e3af`; re-recorded byte-equal by the reviewer); only the declared D5 key
differs. No tool-list, argument, `server.py`, dependency, config, schema or
image-digest change. All six criteria met offline (`CHECK OK tree=bdb5c04aeef3
garmin=449 gateway=91 coach=1560 e2e=OK`; garmin 424 to 449, 12 more than the
spec's 437: three tests from spec-check notes N1 to N3 and nine boundary cases
from N4). Mutants: the spec table 21 of 21 killed, the implementer's four
extra killed, of the reviewer's own 18 thirteen killed (the survivors are
findings 1 to 3). Deviations, accepted by the orchestrator at design: D5, the
one intended behaviour change: a shaper exception in `get_activity` detail,
laps or HR zones, and in `gear` item shaping, now degrades like every section
(`unexpected Garmin response ...`, `laps_error`, zones left out,
`stats_error`) instead of failing the whole tool; D2, the chapter's "every
tool module" is read as every module that has sections (a tool with one strict
call keeps `service.call`); D6, the log line `could not shape <call>:
<ExceptionType>` keeps its text and level, its logger is now
`garmin_coach_mcp.sections`. Found by review, not declared in the spec
(finding 4): `get_race_predictions` and `get_running_tolerance` moved to
`call_many`, so a non-`ToolError` exception inside the library now surfaces as
an `ExceptionGroup` text, as it already did for the seven tools on
`call_many`; the tool fails either way. Review took one round: APPROVED (0
blocking, 6 non-blocking, kept open). Spec check: 0 blocking, 5 non-blocking,
all applied. Open non-blocking: section 5. Nothing needs the test host.
Deploy: rebuild `garmin-mcp` (`DEPLOY-2026-10-16.md`).

### PR 26 — Daily delivery and a local clock

`coach-mcp/src/coach_mcp/scheduler.py` sends the morning briefing
(`BriefingScheduler.tick`) and the Monday report (`_weekly_step`) with the
same copied outcome logic: sent, permanent failure, final attempt, retry. The
CLI commands `send_briefing_command` and `send_weekly_report_command` both
repeat the `push_enabled` check, today's date and the exception wrapping.

Local time has the same problem. `_at` and `_until` are copied verbatim
between `scheduler.py` and `history.py`, DST comment included. "Today in the
configured zone" is built in `server.py`, in both CLI commands and in
`inbound.py`. Tests reach the retry rules by patching
`scheduler_module.compose_weekly` and Store methods.

Add two modules:
- **A local clock:** zone, now, today, the instant of a local clock time on
  a given day, and the seconds until an instant. Every module that needs
  local time uses it.
- **A daily delivery module:** owns sending once a day. Given compose,
  already-sent and record functions, it decides whether to send, skip, retry
  or give up. The morning briefing and the Monday report are its two
  adapters, and the CLI commands use it too.

Tests pass a fake clock through the interface instead of patching module
time.

Out of scope: send times, retry counts, message text, `PushSender`.

**Done when:**
1. `_at`, `_until` and "today in the zone" exist once. The DST transition days
   (spring forward, fall back) are tested once, on the clock.
2. The delivery rules are tested once, on the delivery module, with stub
   compose and record functions. No test patches `compose_weekly` or a Store
   method to reach them.
3. Behaviour is unchanged: send times, retries, log lines, outcome words and
   CLI exit codes stay the same. Existing scheduler tests are rewritten only
   where they patched internals.
4. The four-command check is green.

**Requested:** 2026-10-15, by the owner, from the architecture review of
2026-10-15 (candidate 4).

**Delivered:** 2026-10-16, PR #47 (`mao/pr26-daily-delivery`), squash-merged
into `main` as `df65171`. The branch was implemented on `b85a111` (PR 23),
then stacked on PR 24's approved tip `dce4f7c` for its rebase round (one
conflict, the `strength.py` import block, resolved as spec-check note N2
prescribed: PR 24's `history_view` import line plus `from .clock import
local_at, local_day`); after PR 24's squash merge `git rebase --onto
origin/main dce4f7c` left the tree identical (`2b724487759e`), so the check
and both reviews held. New `coach_mcp/clock.py` (`LocalClock`: zone, now,
today, the instant of a local clock time on a day, seconds until an instant,
next occurrence) replaces `_at`, `_until` and the "today in the zone" helpers
copied between `scheduler.py` and `history.py`;
`tests/test_clock.py::test_local_time_exists_once` is an AST guard that fails
when a module other than `clock.py` and `config.py` builds local time (it sees
five shapes only, review O5). New `coach_mcp/delivery.py` (`DailyDelivery`)
sends one message a day and decides send, skip, retry or give up; it owns the
window, the per-day state and the order of the two messages. `scheduler.py`
keeps the loop, the retry gate and two adapters (morning briefing, Monday
report); `send_briefing_command` and `send_weekly_report_command` use
`send_now`. `history.py`, `inbound.py`, `server.py` and `strength.py` build
local time through the clock; their constructors keep `now=` and their
signatures. No dependency, config, secret, schema or tool-list change. All
four criteria met offline (`CHECK OK tree=2b724487759e garmin=449 gateway=91
coach=1673 e2e=OK`, reviewer round 2; coach 1633 to 1673). Behaviour
unchanged, shown by the base tree's 737 tests of six files running on the new
source and a 4160-case old-vs-new differential run (13 start instants
including the DST days, 8 push scripts, 4 Garmin modes, 5 windows, weekly on
and off; all identical, positive controls fail it); all 29 designer mutants
killed on both trees. Deviations: per-message crash handling stays a setting
(`raise` for the briefing, `retry` for the Monday report) instead of one rule,
because unifying would move the briefing's retry after a crash near the cutoff
from 09:35 to 09:30 (spec decision 2, pinned by two new scheduler tests);
`HistoryJob` still adds `timedelta`s to the zone-aware `now()` (spec decision
5). Review took two rounds: round 1 APPROVED (0 blocking, 6 non-blocking),
round 2 a delta review of the rebase APPROVED (no new findings). Spec check: 0
blocking, 5 non-blocking, all applied. Open non-blocking, and the DST
follow-up proposed for PR 27: section 5. No criterion needs the test host; the
next 06:30 briefing and the Monday report show the live effect after the
deploy. Deploy: rebuild `coach-mcp` (`DEPLOY-2026-10-16.md`).

### PR 27 — Nightly history run in parts

`HistoryJob` in `coach-mcp/src/coach_mcp/history.py` is about 530 lines. It
runs four pipelines and their scheduling in one class:
- the Garmin day fetch, with its repairs (PR 18);
- race predictions;
- the Hevy strength sync;
- the reconcile step.

`run_once` orchestrates them by hand, with separate crash and timing
variables per pipeline, and `_call` and `_strength_call` duplicate each
other. The run's counters are visible only in log lines, so
`tests/test_history.py` (2853 lines) checks log text about 170 times. The
strength tests are appended to the same file with `# noqa: E402` imports.

Split each pipeline into a part behind one interface that runs the pipeline
and returns its outcome and counters. `HistoryJob` keeps the schedule, the
backoff, the briefing window and how the part outcomes combine. The log lines
are written from the returned counters and keep their text. Each part's tests
check its returned result, and the job's tests use stub parts.

Out of scope: what is fetched, limits, repair rules, the schedule, log text,
and the read side (PR 24).

**Done when:**
1. Each part has tests that check its returned outcome and counters, not log
   text. Log text is covered only by formatting tests, and the PR states how
   many log-text checks remain.
2. Job tests with stub parts cover how outcomes combine (one part crashes,
   all succeed, an outage) and the scheduling and backoff.
3. Behaviour is unchanged for the existing scenarios: the same Garmin and
   Hevy calls in the same order, the same stored rows and the same log lines.
   The outage scenario of the checklist still passes.
4. `_call` and `_strength_call` are merged. The strength tests live in their
   own file, without `# noqa: E402`.
5. The four-command check is green.

**Requested:** 2026-10-15, by the owner, from the architecture review of
2026-10-15 (candidate 5).

### PR 28 — Garmin tool definitions next to their code

The interface of each `garmin-mcp` tool lives in two places. `build_server`
in `garmin-mcp/src/garmin_coach_mcp/server.py` has 24 one-line wrappers, and
they carry the `Field` limits and the descriptions. The tool modules hold the
same limits again as constants and checks:
- weeks 2–52 in `performance.py`;
- weight 20–300 kg in `body.py`;
- positive ids, `limit` and `days` in `activities.py` and `workouts.py`.

The two places have drifted. `schedule_workout` documents "today up to one
year ahead", but `workouts._check_schedule_date` accepts yesterday up to 366
days ahead. `list_scheduled_workouts` re-implements `formatting.resolve_range`
by hand.

Each tool module exports its tool definitions: name, read or write
annotation, a signature whose limits come from the module's own constants,
the description, and the implementation. `server.py` registers them in a
loop, with the read-only guard and logging as today. Checks that the
signature already enforces are deleted from the modules.

Owner decision (2026-10-15): `schedule_workout` keeps accepting yesterday up
to one year ahead, and its description says so.

Out of scope: new or renamed tools, response shapes, section handling
(PR 25).

**Done when:**
1. `tools/list` is identical to the one before the change (names,
   annotations, input schemas), except for the `schedule_workout`
   description. A test compares it with a snapshot taken before the change.
2. Every numeric limit in a description and in a schema comes from the
   module's constant. A test fails when a description and its constant
   disagree.
3. Limit tests run against the published schema or over HTTP. The duplicate
   checks in the modules are deleted.
4. `list_scheduled_workouts` uses `resolve_range`, with unchanged behaviour
   (test).
5. The read-only guard still refuses every write tool for the read-only token
   (`tests/test_read_only.py` passes unchanged).
6. The tool table in `garmin-mcp/README.md` matches the new
   `schedule_workout` description, and the four-command check is green.

**Requested:** 2026-10-15, by the owner, from the architecture review of
2026-10-15 (candidate 6); owner decision on the `schedule_workout` date range
the same day.

### PR 29 — Recovery snapshot parsed once

`coach-mcp` reads the Garmin recovery snapshot from the raw `garmin-mcp`
payload in three places, and each decides on its own whether a section is in
error:
- `briefing.criteria_from_snapshot`, with the rule that `morning_score` wins
  over `score`;
- `history.recovery_values`, with the same rule;
- `scheduler.reusable`.

The MCP client setup (token file, bearer client, timeout, streamable HTTP
client) is copied three times in `coach-mcp/src/coach_mcp/garmin.py` and once
in `hevy.py`. Both client test files patch `anyio.fail_after`. Fakes have to
produce raw payloads (`tests/garmin_payloads.py`).

Parse the snapshot once into a typed recovery day: its values plus the
sections in error. The briefing, history and the scheduler all use it. The
Garmin and Hevy sources return typed values for what `coach-mcp` reads, and
one helper sets up every MCP call. Raw payloads appear only in the client
contract tests.

Out of scope: changes to `garmin-mcp`, new metrics.

**Done when:**
1. The snapshot rules (score preference, sections in error, missing values)
   exist once and are tested once.
2. Briefing, history and scheduler tests use typed fakes. Raw payload
   fixtures are used only by the client contract tests.
3. One helper sets up MCP calls, and its timeout is tested once.
4. Briefing text, stored history rows and alerts are unchanged for the
   existing scenarios.
5. The four-command check is green.

**Requested:** 2026-10-15, by the owner, from the architecture review of
2026-10-15 (candidate 7).

### PR 30 — Gateway reachable only through tailscale serve

The `ts` sidecar runs tailscaled in userspace mode (`TS_USERSPACE: "true"` in `docker-compose.yml`). The
architecture review of 2026-10-18 (gateway-01, high) found that in this mode tailscaled forwards a tailnet
TCP connection to any port that has no serve handler straight to `127.0.0.1` on the same port. The gateway
listens on `127.0.0.1:8080` (`gateway/config.toml`) and tailscaled's own health listener on `127.0.0.1:9002`
(`TS_LOCAL_ADDR_PORT`), so any device the tailnet policy lets through reaches both without `tailscale
serve`. Such a peer can send its own `Tailscale-User-Login`, `Tailscale-Funnel-Request` and
`X-Forwarded-For`, which `gateway/src/gateway/netctx.py` trusts. It reaches the owner-only login page, can
keep the owner's login locked out, and can use `/token` and the MCP endpoints with a stolen token from
outside the Funnel network rule. Password + TOTP and the tokens still hold. `README.md` claims serve is
the only way in and makes the tcp:443-only grant optional. The review confirmed the forward on the live
node: a bare connect to the node's tailnet address is accepted on :8080 and :9002 and refused on :8081.

Fix it in code, so the boundary does not depend on the tailnet policy. Nothing in the sidecar's namespace
listens where netstack forwards tailnet traffic (`127.0.0.1`, `::1`). The gateway moves to a target only
`tailscale serve` uses: another loopback address or a unix socket, decided in the spec after running it
in the image. `tailscale/serve.json`, the compose healthchecks, `TS_LOCAL_ADDR_PORT` and
`scripts/e2e_flow.py` follow it. `scripts/healthcheck.sh` checks from the host that the node's tailnet
address does not accept connections on :8080 and :9002. `README.md` makes the tcp:443-only grant a
required install step. It explains why and how to keep the rest of the tailnet working. Applying the
policy to the owner's tailnet is an owner step in the admin console, not part of the PR. Owner decision
(2026-10-18, batch round B4): the owner sets the tcp:443-only rule for the tag now, before this PR's
deploy. A probe from the tailnet then only shows the policy blocking :8080 and :9002, so the test host
verifies the code fix inside the sidecar's namespace (criterion 8).

Add the missing negative tests of the trust boundary (tests-01, medium). Today a one-line regression such
as `split(',')[0]` (left-most `X-Forwarded-For`) keeps every gateway test green. The tests cover:
(1) `net_context`: right-most XFF and a forged identity on Funnel requests;
(2) the same rules at the endpoints (`/authorize`, `/token`, MCP);
(3) audit redaction (`gateway/src/gateway/audit.py`);
(4) the CSRF cookie flags (`gateway/src/gateway/oauth.py`);
(5) the busy limit through the GET stream slot (1 s timeout) or with the acquire timeout patched, so no
test waits 10 s;
(6) the CLI rules in `gateway/src/gateway/cli.py`: `hash-password` minimum length, `grants revoke`
prefix and `mint-token` TTL clamp.

Explicitly not: changing the tailnet policy, GitHub settings or the live stack (owner steps); the
gateway design note and `SECURITY.md` (PR 34); the diagram redraw (PR 35); the low test findings of
gateway-09 beyond the `net_context` tests above; the stale-namespace issue after a `ts` restart; no
schema, secret or dependency change.

**Done when:**
1. Nothing in the sidecar's namespace listens on `127.0.0.1` or `::1`. The gateway is served only on its
   new target. `serve.json`, both compose healthchecks, `TS_LOCAL_ADDR_PORT` and `e2e_flow.py` use the
   new addresses. The e2e run reaches the gateway there and asserts that a connection to
   `127.0.0.1:8080` in the shared namespace is refused (new).
2. A static test fails when the serve proxy target, the gateway listen setting and the compose
   healthcheck URLs disagree, or when any of them or `TS_LOCAL_ADDR_PORT` points to `127.0.0.1`, `::1`
   or `localhost`. It runs in the check or in CI; if it is a new script, this chapter allows adding its
   step to `.github/workflows/ci.yml`.
3. `scripts/healthcheck.sh` reports a problem under its own key when the node's tailnet address accepts
   a connection on :8080 or :9002, and reports OK when the connection is refused or blocked by the
   policy. The address comes from `tailscale status`, never from a hostname in the repo.
   `scripts/test-healthcheck.sh` covers both outcomes.
4. `net_context` unit tests cover:
   - a multi-entry XFF gives the right-most entry;
   - missing or empty headers give no client IP and the source `unknown`;
   - `Tailscale-Funnel-Request` with a forged `Tailscale-User-Login` gives no login and no owner;
   - the owner match ignores case.
   Endpoint tests cover:
   - a Funnel request with a forged owner login gets 403 on `/authorize`;
   - a Funnel request whose left-most XFF entry is in the allowed network and the right-most is not is
     refused on `/token` and on an MCP endpoint.
   The mutation `split(',')[0]` turns them red.
5. Audit test (new `test_audit`): the audit records of a login, a token exchange and an MCP call hold
   no password, TOTP code, authorization code, token, tool argument or tool result. Cookie test: the
   consent page sets the CSRF cookie with `HttpOnly`, `SameSite=Strict` and `Secure` (when secure
   cookies are on), and a successful login deletes it with the same attributes.
6. Busy-limit test: 503 with `Retry-After` through the GET stream slot or with a patched acquire
   timeout, and no test waits 10 s. CLI tests:
   - `hash-password` refuses a password shorter than `MIN_PASSWORD_LENGTH`;
   - `grants revoke` refuses an empty or ambiguous prefix and revokes exactly one grant on a unique
     prefix;
   - `mint-token` clamps the TTL to 60–3600 s.
7. These state the real condition:
   - `README.md` (the architecture paragraph, the Network row of the security model, install step 1:
     the tcp:443-only grant is required, why, and how to keep the rest of the tailnet working);
   - the `netctx.py` docstring and the `docker-compose.yml` comments;
   - the address label in `docs/architecture.html` and `docs/architecture.svg`.
   A grep for "the only way in is" and for `127.0.0.1:8080` outside the plan and `.claude/` finds only
   the new negative checks. The
   PR body gives the owner's deploy order (`ts` and `gateway` recreated together, since both read
   bind-mounted files from the checkout and share one namespace), the rollback, and the policy step.
   The four-command check is green.
8. On the test host, after the owner's approved deploy (the policy rule is already in place, owner step
   before the deploy):
   - a read-only command inside the sidecar's namespace shows that nothing listens on `127.0.0.1` or
     `::1` on the gateway port or the tailscaled local port (the spec picks the exact command);
   - `scripts/healthcheck.sh` and `scripts/smoke-test.sh` pass (the probe of criterion 3 reports OK,
     with the connection blocked by the policy);
   - the audit log shows a claude.ai connector call through Funnel after the deploy.

**Requested:** 2026-10-18, via `/mao` from a user request (fix the high and medium findings of the
architecture review of 2026-10-18; this chapter: gateway-01, tests-01); no questions round.

### PR 31 — Garmin answer and error contract

`coach-mcp` reads six `garmin-mcp` tools (`ALLOWED_TOOLS` in `coach-mcp/src/coach_mcp/garmin.py`), and the
contract between the two services is a convention:
- Errors are classified by matching prose. `RATE_LIMIT_HINT = "HTTP 429"` and `GARMIN_LOGIN_HINT` are matched in
  `_call`, in `history_call` and in each section's `error` text. The 429 text in `garmin_client.py` is an inline
  literal that no `garmin-mcp` test pins (`tests/test_garmin_client.py` matches only "rate limit"). If that text
  is reworded, the coach's rate limits turn into `error`, so the nightly job gets no backoff and keeps calling
  Garmin.
- The answer shapes are not declared. The coach's tests use hand-written payloads
  (`coach-mcp/tests/garmin_payloads.py`), and `garmin-mcp/tests/section_answers.json` holds hashes, not answers.
  If a shaper key is renamed (say `last_night_avg_ms`), both suites stay green. The briefing then shows the metric
  as missing, and the history stores NULLs.

This PR makes the contract explicit and tests it on both sides.
- **Error kind.** Every `garmin-mcp` tool error and every `{"error": ...}` section carries a machine-readable
  kind from a small, fixed, documented set (at least `garmin_auth` and `rate_limited`). Each message keeps its
  wording. The 429 text becomes a named constant next to `NOT_LOGGED_IN` and `SESSION_EXPIRED`.
  `coach-mcp` checks the kind first and uses the text only as a fallback, as `hevy.py` does with `error.status`.
  Because of the fallback, the two services can be deployed in either order.
- **Golden answers.** `garmin-mcp` commits the exact answers of the six tools, produced from its canned fixtures
  through the server. They cover full, degraded, section-rate-limited and whole-tool error scenarios, and a
  `garmin-mcp` test fails when the code produces anything else. Each service is built from its own directory,
  so `coach-mcp` keeps a byte-identical copy, and `scripts/check.sh` (which CI runs) fails when the two copies
  differ. A `coach-mcp` contract test feeds the copy to the coach's readers: the history columns, the briefing
  criteria, `scheduler.reusable`, the last sync, the HRV trend, the scheduled workouts, activities and race
  predictions. It fails when a metric the coach reads is missing.
- **Drift seen in the history.** `history.recovery_values` and `section_errors` treat a snapshot section as a
  section in error when it is an object, not `{"available": false}`, and holds none of the fields the history
  reads. The drift then shows in the run's log and is repaired through the PR 18 path. No schema change.

How this relates to the planned chapters:
- **PR 28.** This PR does not change the tool registration (names, annotations, input schemas, descriptions,
  `structured_output=False`), so PR 28 criterion 1 ("`tools/list` identical") still holds. PR 28 must keep the
  error kind and the contract test when it moves the definitions.
- **PR 29.** PR 29 can build its typed recovery day and its "client contract tests" on the coach copy of the
  goldens, and move the drift rule into its single parser. Its "no changes to `garmin-mcp`" boundary stays true,
  because this PR already does the `garmin-mcp` side.

Out of scope: publishing `outputSchema` or `structured_output=True` (it changes what claude.ai receives);
the typed snapshot parsing and the replacement of the coach's raw fixtures (PR 29); `hevy-mcp`; new tools,
arguments or metrics; a top-level shared directory read by both Docker builds; any new dependency.

**Done when:**
1. Every `garmin-mcp` tool error and every error section carries a documented kind. Each human-readable message
   keeps its wording. The spec chooses how the kind travels in a whole-tool error and verifies the choice by a run
   in the image (`mcp` 2.2.0). Over HTTP, a test pins the kind and the text of the auth errors (with
   `scripts/garmin-login.sh`) and the rate-limit error (with `HTTP 429`), for both a whole-tool error and an error
   section.
2. `coach-mcp` classifies `rate_limited` and `auth_error` by the kind in `_call`, in `history_call` and in error
   sections. Tests cover the kind with reworded text (classified by the kind) and the old text without a kind
   (classified by the fallback).
3. `garmin-mcp` commits golden answers of the six tools the coach calls (exact JSON text and error results) for
   full, degraded, section-rate-limited and whole-tool error scenarios. A test fails on any difference, and one
   documented command regenerates them.
4. `coach-mcp` has a byte-identical copy, and `scripts/check.sh` fails when the copies differ. A coach contract
   test feeds the copy to the coach's readers: no metric the coach reads is missing in the full scenario, and the
   error scenarios give `rate_limited` and `auth_error`. Each of these mutants turns the check red: renaming one
   shaper key, rewording the 429 text, regenerating the goldens without copying them, and copying them without
   adapting the coach.
5. In the history, a snapshot section that holds none of its expected fields counts as a section in error: its
   columns are kept, it shows in the run's log and it is repaired like any section error (test). Schema stays 9.
6. `tools/list` is identical to a snapshot taken before the change. Tool answers for the canned payloads are
   unchanged, except for the declared kind in error sections (`section_answers.json` scenarios in `CHANGED`, each
   with its own test).
7. Over real HTTP, `scripts/e2e_coach_garmin.py` checks that the coach classifies the not-logged-in answer of
   `garmin-mcp` by its kind.
8. `garmin-mcp/README.md` documents the error kinds, the golden files and the command that regenerates them, and
   `coach-mcp/README.md` documents how the coach classifies Garmin errors. The four-command check is green.

**Requested:** 2026-10-18, via `/mao` from a user request (fix the high and medium findings of the architecture
review of 2026-10-18; this chapter: system-01, garmin-03, quality-03); no questions round.

### PR 32 — Automated dependency updates and audit

Every dependency is pinned (Actions by SHA, base images by digest, Python packages by hash in six locks), but
nothing refreshes the pins or looks for known vulnerabilities: `docs/operations.md` says "Nothing updates by
itself", `scripts/check-updates.sh` reads the Python base digest only from `gateway/Dockerfile` and skips the uv
image in `scripts/lock-deps.sh`, and `garmin-mcp/Dockerfile` and `coach-mcp/Dockerfile` spell the same digest as
`ARG PYTHON_IMAGE` + `FROM ${PYTHON_IMAGE}`, which updaters do not follow (architecture review 2026-10-18,
tests-04 and ops-06). This chapter gives the pins an update path and an audit. A new
`.github/dependabot.yml` covers the `github-actions` ecosystem (`/`) and the `docker` ecosystem
(`/gateway`, `/garmin-mcp`, `/coach-mcp`, `/hevy-mcp`, `/tailscale`): monthly, one grouped PR per ecosystem
(so the three Python pins move together), open-PR limit 5. The garmin and coach `FROM` lines become literal with
the same digest, so the built images do not change. A new `.github/workflows/audit.yml` runs every week, on demand
and on pull requests that change a `requirements*.lock`, a `pyproject.toml`, the tools lock or the workflow. It
installs `pip-audit` from a new CI-only hash-locked tools lock made by `scripts/lock-deps.sh` (the official
`pypa/gh-action-pip-audit` installs without hashes) and runs `pip-audit --require-hashes --disable-pip -r <lock>`
on all six service locks. The audit is not one of the CI checks `garmin-mcp`, `gateway`, `coach-mcp`, `scripts`,
`check`, so a newly published CVE does not block unrelated PRs.

Python packages stay with `scripts/check-updates.sh` + `scripts/lock-deps.sh`: Dependabot cannot regenerate
the uv `--python-platform aarch64` hash locks. `scripts/check-updates.sh` reads the Python digest of all three
Dockerfiles (and says when they differ) and also reports the uv image. Actions SHAs are left to Dependabot.
A new offline `scripts/check-pins.sh` runs in the `scripts` CI job. It fails when the three Python `FROM` pins
differ, or when the `image:` tag of `tailscale` or `hevy-mcp` in `docker-compose.yml` (today `1.102.5`,
`6.1.16`) differs from the upstream version in its Dockerfile. Dependabot cannot edit those compose tags,
so its PR for `tailscale/` or `hevy-mcp/` stays red in CI until the owner adds a commit with the matching compose
tag to that PR. `docs/operations.md` → Updates states this rule. Dependabot PRs keep their own titles, get no plan
row, and are merged by the owner after green CI and deployed with the existing procedure. Each one is a
pinned-digest change and needs an owner-approved deploy.

Not in this chapter: Dependabot for pip; a Trivy image scan (its `trivy-action` and `setup-trivy` tags were
force-pushed with a secret-stealing payload on 2026-03-19, CVE-2026-33634); recorded as an open item in
section 5. Also out: applying the updates already pending (the `python:3.13-slim` digest moved, tailscale
v1.104.1), which arrive as the first Dependabot PRs. Dependabot alerts, secret scanning, push protection and
the `main` ruleset are owner settings at the public flip (presentation-02, ops-02). No pin, service lock, image,
compose service, secret or schema changes. No deploy.

**Done when:**
1. `.github/dependabot.yml` has exactly the two ecosystems and five docker directories above, monthly, one group
   per ecosystem, limit 5. `grep -rn PYTHON_IMAGE` is empty, and the three Python `FROM` lines carry the same
   digest as on `main`. The service locks are unchanged (`git diff --stat origin/main` lists no
   `<svc>/requirements*.lock`).
2. The tools lock comes from `scripts/lock-deps.sh`; a second run gives no diff. The PR body lists the new
   dependency with its transitive packages and the reason.
3. `.github/workflows/audit.yml` has the triggers above and installs only with `--require-hashes`. In a
   scratch container it reports no known vulnerabilities for all six locks and exits non-zero on a scratch lock
   holding a known-vulnerable pinned version (mutation).
4. `scripts/check-pins.sh` passes on the tree. Each of three mutants turns it red: one Python digest
   changed, the tailscale compose tag changed, the hevy-mcp compose tag changed.
5. `scripts/check-updates.sh` reports the Python digest of all three Dockerfiles (a mismatch line when they
   differ) and the uv image. New offline `scripts/test-check-updates.sh` (stub `curl` and `docker`, no network)
   proves it. Both new scripts run in the `scripts` job of `ci.yml`. Job names stay `garmin-mcp`, `gateway`,
   `coach-mcp`, `scripts` and `check`.
6. Docs updated in the same PR: `docs/operations.md` → Updates covers Dependabot PRs, the compose-tag rule,
   the weekly audit and what to do when it is red, and "Nothing updates by itself" disappears. Also
   `CONTRIBUTING.md` "Supply chain", the `README.md` CI paragraph and `.github/workflows/` layout line, the
   `.claude/mao/project.md` "CI:" paragraph, the plan section 1 "Supply chain" bullet and a section 5 item
   for Trivy.
7. `scripts/check.sh --tag pr32` ends with `CHECK OK` for the reviewed tree. CI on the PR is green, including
   the PR's own `audit.yml` run.
8. On GitHub after merge: `gh workflow run audit.yml` on `main` is green. Dependabot's first run shows both
   ecosystems without errors (Insights → Dependency graph → Dependabot). The pending Python digest update
   arrives as one PR that moves all three Dockerfiles.

**Requested:** 2026-10-18, via `/mao` from a user request (fix the high and medium findings of the architecture
review of 2026-10-18: tests-04 and ops-06); questions and answers round 1 (owner: every default).

### PR 33 — Versioned images and rollback

The three images built from this repo carry a fixed tag that never changes. They are
`fitness-coach-mcp/gateway:0.1.0`, `garmin-mcp:0.1.0` and `coach-mcp:0.1.0` in `docker-compose.yml`, and
every `pyproject.toml` is at `0.1.0`. The architecture review of 2026-10-18 (ops-01, medium) found three
consequences:
- A rebuild overwrites the only image, so there is nothing to roll back to.
- A plain `docker compose build` in a worktree still retags the live image (lessons, PR 04 entry).
- Scripts and docs hard-code the tag and the volume names: `scripts/setup-secrets.sh`, the restore and
  side-copy commands in `docs/operations.md`, and the 8 down-migration one-liners in `coach-mcp/README.md`.

Today the owner keeps rollback images by hand. The deploy bundles tag the running images
`:pre-<batch>`, compare the running image IDs with the tags, and retag them back to roll back. No git tag
or release exists.

Tag the three images `fitness-coach-mcp/<svc>:${FCM_VERSION:-local}`. A plain `docker compose up` keeps
working for a newcomer. A new `scripts/deploy.sh` becomes the one deploy path. When run, it:
- refuses a dirty checkout and any missing secret file before it touches anything;
- runs `scripts/backup.sh` first when `BACKUP_DIR` is set, and stops if the backup fails;
- builds the images under the commit's version (`git describe --tags --always`);
- in `.env`, rewrites only the `FCM_VERSION=` line and records the previous value as
  `FCM_PREVIOUS_VERSION=`, leaving every other byte and the mode as they were;
- runs `docker compose up -d`;
- keeps the running version and the 2 previous ones per service. It removes only older images that it
  tagged itself, never `:0.1.0`, `:pre-*` or any other tag.

`scripts/deploy.sh --status` shows, per service, the configured version, the running image and whether
they match. It replaces the manual image-ID comparison.

`scripts/deploy.sh --rollback [<version>]` restores the images only. It switches `FCM_VERSION` back to
the previous version, or to a named kept one, and runs `up -d` with no build and no git command. It
refuses, and says why, in three cases:
- the kept images are missing;
- `docker-compose.yml` or `tailscale/serve.json` differ between the two commits. The checkout supplies
  those files, so the way back is then a revert PR and a deploy.
- the live coach schema is newer than the target `coach-mcp` image. It then names the down-migration
  from `coach-mcp/README.md` to run first.

A rollback to the legacy `0.1.0` images is allowed with a warning. The live checkout's `.env` pins
`FCM_VERSION`, so a worktree build (no `.env`, tag `local`) can no longer replace a live image.

`ts` and `hevy-mcp` keep their upstream-version tags (`1.102.5`, `6.1.16`), so a deploy restarts the
Tailscale node only when its pin or its compose config changed. A pin bump in `tailscale/Dockerfile` or
`hevy-mcp/Dockerfile`, including a Dependabot one, must bump that compose tag with it; PR 32 guards this.

Drop the `gateway/config.toml` bind mount. The file is already copied into the image, so it is
versioned and rolls back with it. A config edit, such as the CIMD drift `scripts/healthcheck.sh`
reports, then ships with `scripts/deploy.sh`, and the healthcheck hint says so.

Scripts and docs reach images and volumes through `docker compose run --rm --no-deps -T <svc> ...`
instead of literal names. This covers `setup-secrets.sh`, the restore and side-copy recipes, and the 8
down-migration one-liners. Each rewritten recipe is run once on scratch data.

One version for the whole repo: the three `pyproject.toml` files and both `__version__` strings move to
`0.2.0`. A new `CHANGELOG.md` gets a `0.2.0` entry: coach schema version 9, the secret files the stack
needs, the new `.env` keys and the new deploy command. One line points to `docs/development-plan.md`
for the history before it.

**First deploy (owner, part of the batch's deploy bundle).** After `git pull`, `FCM_VERSION` is not in
`.env` yet. Any compose command that creates a container then resolves the missing `:local` tag, builds
it from the new tree and recreates the service from it, with no backup. This is the same shape as the
PR 04 and PR 12 lessons. So the order is:
1. Stop-and-ask the deploy.
2. `git pull --ff-only`.
3. Run nothing in between: no `docker compose up|run|create`, no `scripts/setup-secrets.sh` (its gateway
   image now comes from compose), no `scripts/garmin-login.sh`.
4. `scripts/deploy.sh`. It backs up first and records `0.1.0` as the previous version.
5. `scripts/deploy.sh --status`, `scripts/healthcheck.sh`, `scripts/smoke-test.sh`.
6. Only after that, the owner may delete the legacy `:0.1.0` and `:pre-*` tags with the command the PR
   body gives.

Later pulls are safe, because `.env` keeps naming images that exist.

Explicitly not:
- a tested down-migration CLI (`python -m coach_mcp downgrade --to N`): an open item in section 5;
- publishing images (GHCR) or a release workflow;
- pushing a git tag or a GitHub release. After the whole batch is merged the owner runs `git tag v0.2.0`
  (and optionally creates a release) with the command the PR body gives.
- a rollback of the checkout (no git command in the script);
- a live rollback rehearsal;
- changes to the test stage gate, the e2e tags (`compose.test.yml`, PR 10) or `scripts/check.sh`;
- any schema, secret or Python dependency change.

**Done when:**
1. The compose file and the check:
   - `docker-compose.yml` tags `gateway`, `garmin-mcp` and `coach-mcp` `${FCM_VERSION:-local}`; `ts` and
     `hevy-mcp` are unchanged, and the `config.toml` bind mount is gone.
   - `docker compose config` resolves `:local` without `FCM_VERSION` and its value with it.
   - The e2e run still uses only `fcm-e2e/*` tags.
   - The four-command check is green.
2. A new `scripts/test-deploy.sh` covers the deploy. It uses a fake `docker` and `git` and runs in CI
   next to `test-healthcheck.sh`, which this chapter allows adding to `.github/workflows/ci.yml`. It
   proves that `scripts/deploy.sh`:
   - refuses a dirty checkout, a missing secret file and an invalid version, with nothing changed;
   - stops when the backup fails;
   - builds and starts under the commit's version;
   - changes only the two `FCM_*` lines of `.env` (other bytes and mode identical);
   - keeps current + 2 versions and never removes a tag it did not create.
3. `scripts/test-deploy.sh` also covers `--rollback` and `--status`:
   - `--rollback` switches back without a build or a git call;
   - it refuses on missing images, on a `docker-compose.yml` or `tailscale/serve.json` difference
     between the commits, and on a newer live coach schema (naming the down-migration);
   - it warns for the legacy `0.1.0` images;
   - `--status` reports a mismatch between the configured and the running image.
4. Offline rehearsal with real Docker on a scratch compose project (PR-private project name and tags,
   under the docker lock, never `fitness-coach-mcp`). It deploys version A, deploys B and rolls back to
   A. The running image IDs match the expected version at each step, and `.env` of the rehearsal holds
   the expected lines. The result is recorded in the PR.
5. No literal image or volume names remain:
   - `scripts/setup-secrets.sh`, the restore and side-copy recipes in `docs/operations.md` and the 8
     one-liners in `coach-mcp/README.md` use `docker compose run`;
   - every rewritten recipe was run once on scratch volumes;
   - outside the plan, `CHANGELOG.md` and `.claude/`, a grep for `:0.1.0` image references and for
     `-v fitness-coach-mcp_` finds nothing.
6. These describe `scripts/deploy.sh` in place of `docker compose up -d --build`:
   - `README.md` (install step 5, repository layout);
   - `docs/operations.md` (Updates, Backups, Troubleshooting);
   - `coach-mcp/README.md`;
   - `.env.example`, which documents `FCM_VERSION` and `FCM_PREVIOUS_VERSION`;
   - the hint texts in `setup-secrets.sh` (with `test-setup-secrets.sh`) and `healthcheck.sh`.
   Also, the first-deploy order above is in `docs/operations.md`, and the PR body gives it to the batch
   deploy bundle.
7. Release notes and version:
   - `CHANGELOG.md` exists with the `0.2.0` entry;
   - the three `pyproject.toml` files and both `__version__` strings are `0.2.0`, with no lock change;
   - the PR body gives the owner's `git tag v0.2.0` command for after the batch is merged.
8. On the test host, the owner's first `scripts/deploy.sh` runs in the order above. Then:
   - `scripts/deploy.sh --status` shows the commit version, matching, for `gateway`, `garmin-mcp` and
     `coach-mcp`;
   - `scripts/healthcheck.sh` and `scripts/smoke-test.sh` pass;
   - the images of `ts` and `hevy-mcp` are unchanged by this PR.

**Requested:** 2026-10-18, via `/mao` from a user request (fix the high and medium findings of the
architecture review of 2026-10-18; this chapter: ops-01); questions and answers round 1 (owner accepted
every default).

### PR 34 — Security model and gateway design note

The architecture review of 2026-10-18 found three documentation gaps in the security story (gateway-02,
gap-architecture-01 and presentation-01, all medium). (1) `gateway/` is a hand-rolled OAuth 2.1 server of
about 1,500 lines (`oauth.py`, `proxy.py`, `store.py`, `security.py`) with no OAuth library in
`gateway/pyproject.toml`. Nothing says why, what the threat model is, or which test proves each standard it
claims. (2) The repo sells agents acting on personal data, but never mentions prompt injection. The "Human in
the loop for every write" bullet in the `README.md` design notes claims more than any server enforces.
(3) `.claude/mao/project.md`, linked from `docs/how-this-was-built.md`, shows that coding agents work on the
host of the live stack, guarded only by prose. It also carries host-specific details (an absolute path,
hardware, a local skill path).

This PR adds a new `docs/gateway-design.md`, linked from the `## Security model` section of `README.md` and
from `SECURITY.md`. It covers:
- the threat model: internet through Funnel, the tailnet and the container network, as PR 30 leaves the
  boundary;
- why the server is custom (single user, a login bound to the Tailscale identity, a network policy per
  endpoint, pinned CIMD clients with no runtime fetch, no DCR, a small dependency surface) and what a library
  would and would not replace;
- a conformance matrix that names the proving test for each line;
- non-goals and known limitations.

The README security table gets an "LLM / prompt injection" row, and the design-notes bullet is reworded. The
`SECURITY.md` scope gets the topic. `docs/coach-instructions.md` gets two rules for the coach. Section 2 of
`docs/how-this-was-built.md` states plainly where the agents run and what is a process guard rather than a
boundary. A minimal `.claude/settings.json` is committed with deny rules as defence in depth.
`.claude/mao/project.md` and `.claude/mao/lessons.md` lose the host-specific details, keep every rule, and
link the public MAO skill at `https://github.com/stepanbeseda/skills/tree/main/skills/mao`.

Explicitly not:
- any gateway source change: a matrix line that turns out not to be implemented becomes a known limitation
  (the fixes of gateway-06 stay out too);
- a change to the servers' MCP `INSTRUCTIONS` strings;
- the Claude Code sandbox or Bash deny patterns;
- moving the agents to a separate machine;
- an automated check of the matrix and any `.github/workflows/` change;
- `SECURITY.md` line 9 and GitHub settings (presentation-02 and ops-02 are owner steps);
- the README test count, the diagram and `docs/how-this-was-built.md` lines 3 and 51 (PR 35);
- the README network row and install step 1 (PR 30);
- historic lesson text;
- no deploy, schema, secret or dependency change.

**Done when:**
1. `docs/gateway-design.md` exists with these sections: threat model (the three zones as PR 30 leaves them,
   citing PR 30's trust-boundary tests by name), why custom, non-goals and known limitations.
   - Non-goals: DCR, confidential clients, per-tool scopes, JWT access tokens, multi-user, ingress without
     Tailscale.
   - Known limitations include the refresh grace window that can fork a grant and the sliding grant lifetime
     with no absolute maximum (gateway-06), and the full-rights Hevy token held back only by the code
     allowlist.
   - Every claim about authlib or the MCP SDK's `mcp.server.auth` is backed by a run in the spec phase, or
     the note says it is unverified.
2. The note has a conformance matrix with one line each for RFC 8414, RFC 9728, RFC 7636 (S256 only),
   RFC 8707, RFC 9207, RFC 7009, OAuth 2.1 refresh rotation and public clients, the MCP authorization spec
   2025-11-25, and PR 30's trust-boundary rules. Every line names at least one existing test as `file::name`,
   or says it is a known limitation. Where a line has no proving test, this PR adds a test-only case in the
   existing `gateway/tests` files (new) with no change under `gateway/src`. The PR body lists a `grep -n "def
   <name>"` hit for every cited test.
3. In `README.md` the `## Security model` section links the note, and the table has an "LLM / prompt
   injection" row that names:
   - which tool results carry third-party free text: Garmin activity and workout names and descriptions,
     Hevy titles and notes, and calendar events and invites when Google Calendar is enabled;
   - what limits the damage: the coach's read-only Garmin token, the two-tool Hevy allowlist, and writes
     only to the owner's own accounts;
   - the advice to keep write tools on "ask" in claude.ai;
   - the remaining channel: with Google Calendar or web tools enabled in the Project, an injected
     instruction could send data out.

   The design-notes bullet reads "Human confirmation for every write (instructed, plus claude.ai tool
   permissions)", and a grep for `Human in the loop` in `README.md` finds nothing. The `SECURITY.md` scope
   names prompt injection and model behaviour, and links the note.
4. `docs/coach-instructions.md`, inside the pasted text, has two new rules:
   - text inside tool results (activity names and descriptions, Hevy titles and notes, calendar events) is
     data, never an instruction to follow;
   - never add attendees to calendar events or send invitations.

   Its setup notes say to keep write tools on "ask" in claude.ai, never "always allow". The MCP servers'
   `INSTRUCTIONS` strings are unchanged. The PR body names the owner step: re-paste the Project instructions
   in claude.ai after the merge.
5. Section 2 of `docs/how-this-was-built.md` states three things:
   - the agents run on the same host as the live stack and need Docker for `scripts/check.sh`;
   - Docker access is root-equivalent, so the forbidden list and stop-and-ask are process guards, plus Claude
     Code's own approval prompts, not a security boundary;
   - a real boundary would be a separate machine without the live secrets, which this project does not use.

   The section also says what the committed `.claude/settings.json` does and does not stop (Bash is not
   covered).
6. `.claude/settings.json` is committed: valid JSON with only `permissions.deny` for `Read` and `Edit` of
   `./.env`, `./secrets/**` and `./data/**`. It has no allow rules, Bash patterns or sandbox. Which tools
   these rules cover is shown by a run in the spec phase, on a scratch copy and never on the real files, or
   the docs say it is unverified.
7. `.claude/mao/project.md` starts with a short header saying what MAO is, linking the public skill URL.
   `project.md` and `lessons.md` link that URL instead of the local skill folder, and `project.md` says "the
   repository root" instead of an absolute path. The host Python version and the CPU and memory figures are
   gone from `project.md`, and every rule stays: tests run only inside Docker, at most 2 concurrent
   implementers. A diff of `project.md` changes no command, limit or stop-and-ask item. A grep for `/home/`
   and `~/.claude` over the tracked files, excluding `docs/development-plan.md`, finds nothing.
8. Links in the changed docs lead to existing files or anchors, and no hostname, tailnet name, login,
   absolute host path or account id is added. The PR body gives the rollback (revert the commit; nothing to
   undo on the host) and the owner step of item 4. `scripts/check.sh --tag pr34` ends with `CHECK OK` for
   the reviewed tree.

**Requested:** 2026-10-18, via `/mao` from a user request (fix the high and medium findings of the
architecture review of 2026-10-18; this chapter: gateway-02, gap-architecture-01, presentation-01);
questions and answers round 1 (owner: every default accepted).

### PR 35 — Accurate showcase docs and a readable diagram

The architecture diagram (`docs/architecture.svg`, exported from the editable `docs/architecture.html`) is the
first technical thing a visitor sees in `README.md`, and in GitHub's README column its labels render at about
4.6 to 8 px. It also leaves out the gateway-data and ts-state volumes, the time-triggered work inside
`coach-mcp` (morning briefing and Monday report, nightly history, Telegram long poll) and the host cron jobs
of `docs/operations.md` (healthcheck with Telegram alerts, nightly backup). The claude.ai ingress edge uses
the "outbound API call" style, and one legend entry covers both "read-only" and "optional". The showcase
docs also carry stale statements: "about 770 test functions" in `README.md` and
`docs/how-this-was-built.md` (1,196 at the time of the review), "22 planned PRs" in
`docs/how-this-was-built.md`, "two MCP backends" in section 1 of this plan, "An English README" as not
planned in section 5, and "official image" for hevy-mcp in `README.md` and the diagram, which is ambiguous.
`hevy-mcp/Dockerfile` names the upstream `chrisdoc/hevy-mcp` image, pinned by digest.

Redraw the one diagram narrower with larger type and keep the look of commit 4dd1f77: palette, fonts, white
boxes with the accent gateway, mono uppercase edge labels, dashed optional boxes, the legend strip.
`docs/architecture.html` stays the editable source and shows the same drawing. The SVG stays its export, and
the README embeds the SVG. Add the missing content without clock times, because those are configurable.
Give each kind of edge its own style and legend entry. Correct the stale statements. This PR merges last in
the batch of 2026-10-18 and describes `main` after PR 30 to 34. Every count and every label, including the
gateway bind and ingress wording PR 30 changes, is taken from the final rebased tree, not from 4dd1f77. The
plan only grows: section 1 gets a dated "Superseded" note and the section 5 item a dated "Resolved"
sentence, the same way as the existing "Language" bullet and "Resolved by PR 10" item. No plan text is
deleted.

Out of scope: text that PR 30 owns (README tailnet boundary, operations), text that PR 34 owns (Security
model table, the "Human in the loop" design note, `SECURITY.md`, section 2 of
`docs/how-this-was-built.md`), any change to `.github/workflows/`, an automated check that the docs
numbers stay current, a second README-only drawing, and any code, configuration or deploy.

**Done when:**
1. The diagram is readable in the README. `docs/architecture.svg` is well-formed XML (`python3 -c` with
   `xml.etree.ElementTree.parse`). Its viewBox is at most 960 wide, and no `font-size` in it is below 12.
   `docs/architecture.html` shows the same drawing: its `svg` `min-width` equals the new viewBox width, and
   the visible texts are identical (a diff of the `<text>` contents of the two files is empty). The palette
   and fonts of 4dd1f77 are unchanged.
2. The diagram shows:
   - a "background jobs" strip inside coach-mcp: morning briefing and Monday report, nightly history,
     Telegram long poll, with the optional ones marked optional;
   - all four volumes: `ts-state`, `gateway-data`, `garmin-tokens`, `coach-data`;
   - an optional "host cron" box: `healthcheck.sh --notify` to Telegram, `backup.sh` to the backup
     directory;
   - hevy-mcp as "upstream chrisdoc/hevy-mcp (pinned)".

   It has no clock times. Its gateway bind and ingress labels match `docker-compose.yml`,
   `gateway/config.toml` and `tailscale/serve.json` on the final tree.
3. Edge styles, each with its own legend entry:
   - client requests: solid ink #2d3142, heavier;
   - internal calls: solid muted;
   - outbound API calls: solid blue #2e5aa8;
   - read-only internal calls: dotted, keeping the "READ ONLY" and "2 READ TOOLS" labels;
   - optional: the 4,3 dash, for boxes and lines.

   The combined legend entry "Read-only / optional" is gone.
4. hevy-mcp is no longer called the "official image" (`git grep -n "official image" -- README.md docs/` is
   empty; `tailscale/Dockerfile` keeps the phrase, correctly, for the Tailscale image). `README.md` "Repository layout" and the diagram say upstream `chrisdoc/hevy-mcp`, pinned. The README alt
   text of the diagram also names the background jobs and the host cron.
5. `README.md` "Design notes" and `docs/how-this-was-built.md` say "more than N test functions". N is the
   `def test_` count over `gateway`, `garmin-mcp` and `coach-mcp` on the final tree, rounded down to the
   hundred. `docs/how-this-was-built.md` names the `CHECK OK` line of `scripts/check.sh` as the source of
   the exact per-service numbers. `git grep -n "770 test" -- README.md docs/` is empty.
6. Every other number in `README.md`, `docs/how-this-was-built.md` and the diagram matches the final tree.
   This covers the PR count ("22 planned PRs" restated from the plan table as done and planned chapters),
   the checklist items, the lessons entries and the Garmin tool count. The PR description lists each number
   with the command that produced it.
7. In `docs/development-plan.md`, section 1 "What it is" has a dated note: "*Superseded <date>*: three MCP
   backends (`hevy-mcp/`, `garmin-mcp/`, `coach-mcp/`) since PR 03". The section 5 item "Not planned yet"
   has a dated sentence appended saying that the English README came with the open-source release on
   2026-10-09. The PR's diff of sections 1 and 5 only adds lines.
8. The diff touches only `docs/architecture.svg`, `docs/architecture.html`, `README.md`,
   `docs/how-this-was-built.md` and `docs/development-plan.md`. It adds no hostname, tailnet name, login or
   absolute host path. `scripts/check.sh --tag pr35` ends with `CHECK OK` for the reviewed tree.

**Requested:** 2026-10-18, via `/mao` from a user request (fix the high and medium findings of the
architecture review of 2026-10-18: system-03 and presentation-03); questions and answers round 1 (owner:
"Default" to every question).

## 5. Open items

- **PR 04 push channel.** Decided 2026-10-06 (PR 04): ntfy on the public
  server ntfy.sh with a random topic; the privacy note in `README.md` keeps
  a self-hosted server as the recommendation. Superseded 2026-10-06 by
  PR 08: the briefing goes to the owner's private chat with their own
  Telegram bot, with the full message, and ntfy is removed; the README
  privacy note says Telegram keeps the chat history on its servers.
- **PR 02 nutrition data.** The Garmin nutrition endpoints have not been
  called on the owner's account; they may return nothing.
- **e2e test shares image tags with the live stack.** `scripts/e2e-test.sh`
  builds images under the same tags as the live project (open since PR 03).
  Resolved by PR 10 (2026-10-09): `compose.test.yml` gives the e2e builds
  their own `fcm-e2e/*:test` tags, so the test never retags the live ones.
- **PR 07 strength payload and exercise catalogue.** Unverified; the chapter
  starts with a spike. Update 2026-10-09: the catalogue is not a risk any
  more. `garminconnect` 0.3.17 in the image ships `garminconnect.exercises`
  (1 527 exercises in 47 categories, with `find` and `resolve`), the
  `StrengthWorkout` model and `create_strength_set(category, sets, reps,
  rest_seconds, weight_kg)`; the bundled catalogue of the chapter can be this
  one. Whether Garmin accepts the uploaded payload and keeps the exercise
  names is still unverified, so the spike stays.
  Postponed by the owner on 2026-10-09 (no spike on the real Garmin
  account); the row stays planned. Resolved by PR 07 (2026-10-12): the spike
  ran on the owner's account, Garmin accepted the payload and kept the names,
  the weight, the notes, a step without an exercise and a timed hold, and the
  catalogue is the one in `garminconnect` (see the "Delivered" block of PR 07).
- **The coach's backend tokens have full access.** Garmin part resolved by PR 12
  (2026-10-09): `coach-mcp` reads `garmin-mcp` with the read-only token
  `garmin_readonly_token`, and `garmin-mcp` refuses every tool not annotated
  read-only for it. Hevy part open: PR 09 mounts `hevy_backend_token` into
  `coach-mcp`, and `hevy-mcp` is an upstream image with one bearer token, so a
  read-only Hevy role would need a filtering proxy; until then only the code
  allowlist of two read tools in `hevy.py` protects Hevy writes.
- **Cycling power target.** `garmin-mcp/README.md` notes that a custom power
  range is sent as `power.zone` with two target values and still needs a
  visual check in Garmin Connect.
- **PR 05 history job, non-blocking review findings.** Recorded 2026-10-07
  (review round 2, owner's decision: ship PR 05); a follow-up candidate for
  PR 09 or a later chapter (PR 09 did not take them). N5: a snapshot with one section in error is
  stored as ok, so a transient error of one Garmin endpoint during the first
  backfill leaves that metric NULL for every backfill day older than three
  days for good (spec-accepted; a risk to PR 05 Done-when 8 on the test
  host). N6: a flapping outage (a share of calls failing for several runs)
  can still give up a few days, because every ok between errors confirms the
  held errors. N7: an activities-only outage stops each run at the first
  blocks, so the recovery backfill is delayed until activities work again
  (nothing is lost). N8: two guard branches have no failing test, the
  `isinstance(..., list)` check of the activities probe and `given_up`
  counted for activities (`history.py`). Update 2026-10-10: N5 and N8 are
  resolved by PR 18 (per-day record of sections in error with bounded repairs,
  and the guard tests T21 and T22); N6 and N7 stay open.
- **Parallel run of 2026-10-09 is merged but not deployed.** PR 06, 09, 11, 12,
  13, 15 and 16 were squash-merged on 2026-10-09 with no live deploy; the live
  stack still runs the pre-run images, and PR 07 was left out. Deploy order
  matters. (1) Back up `coach-data` and the gateway volume first, in the old way
  (PR 11 replaced the README recipe, and the new backup commands exist only in
  the new images). (2) `git pull`, then `scripts/setup-secrets.sh` before any
  `docker compose up`: PR 12 mounts `garmin_readonly_token`, and `up` without it
  fails on a missing bind source (the old containers keep running). Run no
  `backend-tokens` or `telegram-token` rotation between the pull and the deploy
  (PR 12 N2). (3) Compare the running image IDs with the tags, then deploy
  `garmin-mcp`, `coach-mcp` and `gateway` together with `docker compose up -d
  --build`; `coach-mcp` migrates its database from schema 3 to 6 in one start
  (13, then 09, then 15). Add `BACKUP_DIR` (PR 11) and the optional
  `COACH_HOME_LATITUDE` / `COACH_HOME_LONGITUDE` (PR 06) to `.env` with an
  editor. Criteria that still need the test host: PR 06 #7, PR 09 #8, PR 11 #6,
  PR 12 #6, PR 13 #6; the owner steps are in the PR bodies. PR 18 and PR 19 are
  merged (2026-10-10) and also wait for the deploy bundle of 2026-10-10. Update 2026-10-10: the
  run of 2026-10-09 and PR 18 and PR 19 were deployed that night (schema 7); PR 14 and
  PR 20 (merged 2026-10-11) wait for `DEPLOY-2026-10-11.md`. PR 17 (merged 2026-10-11, #33)
  waits for `DEPLOY-2026-10-11-pr17.md`. PR 22 (merged 2026-10-12, #37)
  waits for `DEPLOY-2026-10-12.md`; `coach-mcp` then migrates 7 to 9 in one start when the earlier
  bundles have not run (spec appendix B, "Order"). Update 2026-10-12: the owner deployed `main`
  (`59de7f3`) that night, live is at schema 9 and PR 22 criterion 5 is verified (its "Delivered"
  block); the verification sections of the PR 14, PR 17 and PR 20 bundles are not recorded here yet.
- **PR 06 weather.** The CC BY 4.0 attribution sentence in the READMEs comes
  from the designer's knowledge of Open-Meteo's terms; confirm it on
  open-meteo.com. Open-Meteo's behaviour on DST transition days has not been
  observed (the code tolerates repeated and missing local hours in canned
  payloads).
- **PR 09 strength history, follow-ups.** (a) A strength `no_activity`
  suggestion can be marked `final` while a late Hevy edit can still change it:
  the Garmin side is final from D+3, a Hevy day is re-read until D+4 at the
  earliest and for as long as Hevy fails; the suggestions of the last 7 days are
  re-evaluated every night, so nothing is lost, but `final` is per day, not per
  sport (a design change). (b) `get_strength_progress` and the weekly "new best"
  read every stored row in the range, not only the synced coverage; the result
  carries `coverage`. (c) The e2e Hevy step cannot tell rejected arguments from
  an unreachable Hevy API; validating the call arguments against each tool's
  `input_schema` would close it. (d) A Hevy `rate_limited` or `service_error`
  doubles the backoff shared with the Garmin part; at 8 h the next run may land
  in the briefing window and wait for the cutoff (Garmin data is delayed, never
  given up).
- **PR 09 Hevy facts unverified offline.** To be settled by the live run
  (criterion 8): what `since` of `get-workout-events` filters on (event or
  workout time), whether it is inclusive and which format it accepts; event
  order across pages while data changes and whether a re-edited workout gives
  one event or several; the real format of `start_time` and `updated_at`
  (anything but ISO with `Z` or an offset is skipped and shows in `skipped=`);
  Hevy's rate limit for the 90-day backfill; that `get-exercise-template`
  returns `null` for an unknown or deleted custom template; that real account
  data passes the MCP client's output-schema validation (a mismatch would show
  as `unreachable` on every run; the Garmin part is unaffected).
- **PR 11 backups, accepted points.** The in-memory guard allows 3.5x the
  database size; `backup` and `backup-db` open the live file read-write, so
  SQLite may drop stale sidecars on open (do not run `backup` on a volume right
  before a restore if its sidecars matter); archive names use UTC (a 02:47 cron
  run is named `...T004700Z` in summer); side copies `*.pred-obnovou-*` stay in
  a volume until the owner deletes them with the README command; `audit.jsonl`,
  `ts-state` and `secrets/` are not backed up and off-site copies stay the
  owner's job; the README "Struktura repa" line is 120 characters. Resolved
  by PR 20 (2026-10-11): `scripts/healthcheck.sh` section 8 reads the newest
  archive's name as the freshness signal of the backup.
- **PR 12 read-only token, non-blocking findings.** N1: a missing read-only
  token is signalled only by a start-up WARNING in `garmin-mcp`; `/healthz` and
  `scripts/healthcheck.sh` do not report it and an emptied file at runtime logs
  nothing (a `readonly_token` field in the garmin `/healthz` would close it).
  N2: `setup-secrets.sh backend-tokens` or `telegram-token` run between `git
  pull` and the deploy recreates the old `coach-mcp` image under the new compose
  file without a Garmin token mount, so briefing and history report
  `credentials_missing` until the deploy (no connector goes down). N3:
  `scripts/e2e_coach_garmin.py` is mode 0644 while the other `scripts/*.py` are
  0755 (cosmetic).
- **PR 13 plan reconciliation.** (a) The morning alert `missed_planned_session`
  reads the provisional suggestion of the 03:30 fetch of the day before; if the
  watch syncs later, the line can name a session that was in fact done, and the
  next night corrects the stored suggestion after the push is sent (a follow-up
  could fire only on `final` suggestions or label the provisional state, if it
  turns out noisy; PR 17 on sync-aware briefing is the natural place). Resolved
  by PR 17 (2026-10-11): both of yesterday's missed alerts are held back while
  the last sync with Garmin is dated before the briefing day, and the
  missed-session line says when its nightly check ran before the last sync
  (the line is labelled, not suppressed: the 03:30 check precedes most morning
  syncs). (b) The
  real Garmin type keys of the athlete's activities (for example whether indoor
  strength is `strength_training`) were checked only with canned payloads;
  criterion 6 on the test host covers it. (c) `final` ignores
  `COACH_HISTORY_BACKFILL_DAYS` values below 7. (d) The migration CHECK line in
  `store.py` is longer than 120 characters (spec-prescribed).
- **PR 15 searchable memory, informational.** A query that starts with `*`
  followed by a word goes to the FTS5 special-query path (an empty result or the
  fixed error); `SQLITE_ERROR` is also the code of `no such table: notes_fts`,
  so a hand-edited database would report a syntax problem (not reachable in
  normal operation). How the coach uses `search_notes` with the real model in
  claude.ai is unverified.
- **PR 16 correlations.** Hevy strength metrics (volume, sets, e1RM) are not in
  the `get_trends` catalogue, so `get_correlations` cannot pair them; a later
  chapter can add them if the owner wants strength against recovery.
- **PR 18 history repair, open by decision.** Review O3: a bounded,
  never-ending repair under two simultaneous faults. A candidate whose missing
  set holds one section that loads elsewhere but fails for this day, plus one
  section that is down for every regular day, is asked every night and never
  charged; it drops out only when it leaves `COACH_HISTORY_BACKFILL_DAYS`. Five
  such days could starve older candidates; the cost stays within 5 answered
  repair calls a night. It follows from the "every" rule of the charge (spec
  decision 5), not from an implementation error. Also: rows stored before
  migration 7 are treated as having every empty recovery section missing, so
  each legacy no-data day is asked once more (at most 5 calls a night until the
  legacy candidates are drained); how Garmin answers a section of a day without
  data is unverified until the deploy.
- **PR 19 more Garmin signals.** (a) The running tolerance is empty on the
  owner's account (weekly 56 and 182 days, daily 28 days; only 1 run in 56
  days), so the tolerance part of criterion 5 cannot be met there and the shape
  of a real, non-empty entry is unverified; the tool answers `available:
  false`. (b) Grams (weight), degrees Fahrenheit (temperature) and mph (wind)
  are assumptions until criterion 5 is checked against Garmin Connect after the
  deploy; the converters are isolated, a correction is one line. The `setType`
  values are unverified too. (c) The `garminconnect` logger writes the API path
  (with the activity id for `get_activity`, the splits, the HR zones, the new
  exercise-sets and weather calls) and Garmin's own error message on a 5xx.
  Proposed: a redacting filter on that logger, masking ids, which would also
  close PR 01 N2; not part of PR 19. (d) Review-1 O3: `scripts/smoke_test.py`
  picks the first activity with a distance as the outdoor one, which can be a
  treadmill run or an indoor ride (the weather then shows `available: false`);
  the owner may need to pick an outdoor activity by hand for the temperature and
  wind comparison. Left open because fixing it changes smoke-test behaviour.
- **PR 14 goal progress.** (a) The shape of Garmin's daily race-prediction
  payload was resolved on 2026-10-11 by the owner's capture (spec section 11,
  key names, types and counts only): a list with one entry per day (7 entries
  for 7 days, 90 for 90), distinct ascending `calendarDate` strings, and
  `time5K`, `time10K`, `timeHalfMarathon`, `timeMarathon` and `userId` as ints
  (seconds); the entries also carry `fromCalendarDate` and `toCalendarDate`,
  which the shaper ignores (both are None in the latest answer). The shaper
  keys matched, so no addendum was needed; the fixtures now carry the two extra
  fields. (b) Accepted D4: on a Garmin 5xx the
  `garminconnect` library writes an ERROR line that contains the account display
  name, as it does for the existing latest-value call (PR 01 N2); test T-P10 pins
  the line and turns red on purpose when the redacting log filter proposed under
  PR 19 (c) lands. (c) Review-1 O3: the `ms=` field of the Garmin log line of the
  predictions part is not covered by a test (the mutant that stops subtracting the
  predictions seconds is cosmetic). (d) Review-1 O5: the README rollback command
  redeploys only `coach-mcp`, while the spec says `garmin-mcp` and `coach-mcp`
  together; keeping the new `garmin-mcp` is harmless.
- **PR 20 operations alerts, accepted by decision.** (a) Review-1 O4: warnings
  (`!` lines) are not sent, and the new cron line drops stdout, so
  `claude CIMD: cannot download` (which silently disables the client-metadata
  drift check), `hevy: API not responding` and an unreadable Hevy key file show
  only in a manual run of `scripts/healthcheck.sh`; the old daily mail carried
  them. (b) Review-1 O5: with Docker down or `.env` unreadable the script stops
  before the notify step, so the largest outage reaches only the cron mail
  (stderr), never Telegram. (c) Review-1 O7: the Claude client-metadata key is
  checked statically only; the failing branch needs a download the offline
  harness does not make. (d) Telegram's real reply to a rejected message and the
  live `docker compose ps -q` output of a stopped service are unverified until
  criterion 5 runs (both end in "not sent" or single-key handling). (e) A failed
  backup is detected as a stale newest archive about 2 hours after the missed run
  with the default limit; weekly backups need a larger `BACKUP_MAX_AGE_HOURS`
  (for example 192).
- **PR 17 sync-aware briefing, non-blocking review findings (round 1, 2026-10-11).**
  O1: the overall 180 s deadline of `sync_check` (`coach-mcp/src/coach_mcp/garmin.py`,
  `anyio.fail_after(180)`) has no test; the mutant that drops it survives, while `fetch` has
  `test_a_hung_tool_is_bounded_by_the_overall_timeout` (the 90 s httpx and SDK read timeouts still
  bound most hangs). O2: the hold-back predicate `last.local.date() < day` (`briefing.py`) mutated to
  `!= day` survives, because no test row has a last sync dated after the briefing day (only clock skew
  produces it; one row in `test_missed_alerts_are_held_back_until_a_sync_after_yesterday_ended`
  would pin it). O3: the module docstring of `scheduler.py` still says `compose_briefing` is the one
  place that reads Garmin, but `_garmin_data` now reads it too. O4: `README.md` line 327 lists what
  `scripts/smoke-test.sh` prints and lacks the new "Garmin: device status" section. O5: `README.md`
  line 507 is 139 characters (the paragraph wraps at about 118). O6: by design (spec 4.3 table) an
  unsettled cached read still pays the 2-request check before the full read that follows anyway;
  skipping the check when the read is not settled would save 2 requests per such attempt. O7: the
  30-minute settle assumption is not verifiable offline: if Garmin produces sleep or HRV more than 30
  minutes after an upload without a new upload, the scheduler reuses a read without them until the
  cutoff, where before PR 17 the next retry would have picked them up; to watch in the live log after
  the deploy: a `briefing garmin=unchanged` run that ends with the 09:30 send and `missing=sleep` on a
  morning the watch had synced (then raise `SETTLE` or reuse only reads that contain sleep). O8
  (informational): `mylastused` is account-wide, so an upload from another device (a scale, for
  example) after midnight lifts the hold-back and is the time named in the line; the line says "last
  sync with Garmin", which stays true, and the alert behaviour then equals that before PR 17.
  Criterion 7 and the real briefing text wait for the test host.
- **PR 22 two-way Telegram, open by decision (review 1, 2026-10-11).** O6: an `update_id` of 2**63 or more
  makes `advance_inbound` raise `OverflowError` (not `sqlite3.Error`), so the queue stops with no reply and no
  offset (`inbound.py`, `_updates` accepts any non-negative integer); Telegram's ids are far below it, an
  upper bound in `_updates` would turn it into the named state `bad_answer`. O7: every `sqlite3.Error` is
  transient (spec decision D11), so a permanent error of one message (`IntegrityError`) would repeat the batch
  for good; no input reaches a CHECK of `notes` or `telegram_inbound` today, and if one is added
  `IntegrityError` belongs with the per-message errors. The Telegram Bot API facts of spec table 4.6 and
  `coach-mcp/README.md` were not run offline: `getUpdates` with a JSON POST body, the offset and 24-hour
  rules, 409 for a second reader or a webhook, the shape of a private text message, a photo without `text`,
  the `/command@botname` form and the 4096-character limit of a message. Each has a named state in the code
  (`inbound poll=rejected`, `poll=conflict`, `status=ignored`); spec appendix B steps 6 and 7 are the
  evidence procedure, with criterion 5, after the deploy of `DEPLOY-2026-10-12.md`. Update 2026-10-12, live
  run: `getUpdates` with a JSON POST body and the shape of a private text message are confirmed (a command
  and a note were answered, no `poll=rejected`, no `status=ignored`). Still not observed: no replay after a
  restart, messages kept while the service is stopped, 409, a photo, the `/command@botname` form.
- **PR 07 strength workouts, open by decision (review 1 and 2, 2026-10-12).**
  O6: a plain "Squat (Barbell)" ranks Barbell Back Squat fourth in
  `find_strength_exercise` (design D9; the coach picks from the ranked list and
  shows the mapping before the upload; whether it picked a sensible squat in the
  live run was not observed). O8: a warm-up or cool-down step inside a workout
  authored in the Garmin app is shown by `get_workout` without its type. O9: a
  mixed `stepOrder` in such a workout raises a `TypeError` in `_ordered`. N1: no
  test pins that a padded `title` reaches the step as given (a validator returning
  the stripped title would survive; either behaviour is harmless). N2: a
  zero-width space as the whole title passes the blank-title check, and a blank
  `note` on an unmapped exercise gives the description `"t -    "` (cosmetic, the
  athlete confirms the mapping before the upload). Unverified Garmin side, each
  with a visible failure: U2 (a single set outside a repeat group, a set block
  without a rest, a weight on a step without an exercise or on a timed step, a
  note longer than 27 characters, more than 4 blocks, a fractional weight), U3
  (supersets, pound units and lap-button ends of workouts authored in the Garmin
  app) and U4 (Garmin's step limit; the limit of 50 is kept). The label of a step
  without an exercise is known since the live run ("--" with the note below, U1)
  but the READMEs and `docs/coach-instructions.md` name no label.
- **Batch of 2026-10-15 (PR 23 to 26) is merged but not deployed.**
  Squash-merged 2026-10-16: PR 23 #44 `b85a111`, PR 25 #45 `f0cbbb8`, PR 24
  #46 `ab2d76d`, PR 26 #47 `df65171`. `coach-mcp` and `garmin-mcp` are rebuilt
  together; no schema change (schema stays 9), no migration, no secret, `.env`
  or compose change, no criterion for the test host. The steps are in
  `DEPLOY-2026-10-16.md`. Behaviour changes on live: a session dated after
  today can no longer be marked `done` (MCP and Telegram); a shaper exception
  in `get_activity` or `gear` degrades the section instead of failing the
  tool.
- **PR 23 one Plan module, non-blocking review findings (round 1,
  2026-10-16).** O1: no test pins the order of the three move rules
  (NOT_PLANNED, SAME_DATE, PAST_DATE in `plan.py`); swapping any two keeps the
  suite green (the base had the same gap); one row per pair in
  `test_plan.py::test_every_session_refusal_writes_nothing` closes it. O2: the
  log status word of an unknown id (`bad_argument` in `inbound._mark`) is not
  pinned, `test_log_lines_carry_outcome_words_only` uses a malformed id
  (pre-existing). O3: the `plan.mark_session` docstring says the session is
  checked "in the order of `Reason`", which is loose (FUTURE comes after the
  move rules, COPY_SUPERSEDED before MOVED_TO; same result). O4:
  `test_add_note_refusals_write_nothing` is narrower than spec 6.1 (NOT_FOUND
  only without `inbound`); nothing reachable is left uncovered.
- **PR 24 history view, non-blocking review findings (round 1, 2026-10-16).**
  O1: the skip of a `race_predictions` row with an unreadable date
  (`history_view.py`, the `if day is not None:` branch) has no test; the
  mutant `if True:` survives, and such a row (for example `2026-09-31`) would
  crash `get_goal_progress` on sorting; not a regression, the old guard in
  `goals` was untested too; one raw-SQL row in
  `test_predictions_are_finite_values_of_the_range` closes it. O3: the helper
  `row()` in `tests/test_reconcile.py` is dead. O4: `goals.py:93` is 130
  characters after the `_number_text` rename. O5: other survivors, low impact
  (the `isinstance(status, str)` guard on `hrv_status`, `activity_days(first,
  today)` in `_reconcile`, the finite check of `morning_score` in the
  briefing, `_Acc.add_set` with `finite` instead of `finite_float`). N6: the
  delivered text of PR 09 above says `history.py` gains `activities_settled`
  and `day_activities`; since PR 24 they live in `history_view.py` (historic
  text, left as written).
- **PR 25 Garmin sections, non-blocking review findings (round 1,
  2026-10-16).** 1: the recording rows meant as the new home of the four
  deleted shaper-monkeypatch tests (`race_predictions`, `running_tolerance`,
  `device_status` and `get_activity` weather, "odd" payloads) never take the
  shaper-exception path (28 rows, 20 000 fuzzed payloads per shaper never
  raised), so only the generic S4 test covers it and the mutants
  `R-race-call-renamed` and `R-tolerance-shaped-outside` survive; this is an
  error in spec 6.3 (a recording row is a new home only if it takes the path);
  a tool-level test per tool with a shaper that raises would close it. 2: a
  gear item without a uuid (`performance.py`, the `stats[f"gear_{n}"] if ...
  else shape_gear_item(i, None)` branch) has no test, as before; real Garmin
  gear has a uuid; one line in `test_gear_tool_fetches_stats_per_item` closes
  it. 3: the night-without-summary branch of the HRV per-day fallback
  (`health.py`, `... if summary else None`) has no test (pre-existing); the
  `hrv_trend get_hrv_data odd *` rows do not reach it. 4: the `ExceptionGroup`
  surfacing in `get_race_predictions` and `get_running_tolerance` (see
  "Delivered"). 5: D5 is tested at tool level only for zones (detail, laps and
  gear only by S4), and `_gear_section.if_error` calls the shaper again inside
  the `except` block, so "`if_error` must not raise" is stated but not
  enforced. 6: the README sentence "Tool modules share helpers only through
  `formatting.py` and `sections.py`" overstates (`workouts.py` uses public
  functions of `exercises.py`); it should say "private helpers".
- **PR 26 daily delivery and local clock, non-blocking review findings (round
  1, 2026-10-16).** O1: "a new day resets the window flags" has no killing
  test (the same mutant survives on the base); a row in `test_delivery.py`:
  send on day D at 06:30, sleep to D+1 10:00, expect `skipped
  reason=late_start` and no second send. O2: the wiring of
  `briefing_retry_minutes` into the retry interval is not pinned (every
  scheduler test uses the default 15 minutes; a hard-coded
  `retry_seconds=900.0` survives; pre-existing). O3: the adapters' store-error
  path (`briefing status=state_error` through a real `Store`) is pinned only
  on stubs, by criterion 2; a `Store` subclass whose `record_briefing_sent`
  raises would test it without patching. O4: per-call-site local-date mutants
  in `history.py` and `server.py` survive (equivalent in production, where
  `now` is already zone-aware; the gap predates the PR); optional, one
  midnight test per site. O5: `coach-mcp/README.md` says `tests/test_clock.py`
  "fails when another module builds local time itself", but the AST guard sees
  five shapes only (not `zoneinfo.ZoneInfo(...)` used directly, an aliased
  import, `date.today()`, `datetime.datetime.now(zone)`,
  `.replace(tzinfo=zone)`); the wording should list the shapes. O6: cosmetic
  (`test_send_now_takes_today_from_the_configured_zone` reads the wall clock
  twice; the first commit of the branch is not green alone). Proposed for PR
  27, which owns the history schedule next: `HistoryJob` adds `timedelta`s to
  the zone-aware `now()`, so a backoff across a DST change wakes one hour off
  (`datetime(2026,10,24,22, tzinfo=Prague) + 8 h` is 9 real hours) and `min()`
  of two same-zone datetimes compares wall times; harmless (a retry an hour
  early or late, twice a year), kept deliberately by PR 26; PR 27 can do the
  arithmetic in UTC through `LocalClock`.
- **Chapters that build on the batch of 2026-10-15.** PR 27 and PR 29 build on
  `history_view.py` (PR 24) and PR 27 on `clock.py` (PR 26): they take stored
  history through `HistoryView`, build no local time themselves
  (`test_local_time_exists_once` fails otherwise) and no finite-number check
  outside `values.py`. New check-then-write code uses `Store.transaction()`
  (PR 23). PR 28 must classify every garmin module it adds in
  `tests/test_module_boundaries.py` (PR 25).
- **Image vulnerability scan (from PR 32).** The architecture review of
  2026-10-18 (ops-06) proposed a Trivy scan of the built images. PR 32 left
  it out: the `aquasecurity/trivy-action` and `setup-trivy` tags were
  force-pushed with a secret-stealing payload on 2026-03-19 (CVE-2026-33634),
  OS fixes of the base images arrive as Dependabot digest bumps, and
  `pip-audit` covers the Python packages. If wanted later: the Trivy binary
  pinned by version and checksum (or an image digest), weekly, report-only,
  HIGH and CRITICAL with a fix only.
- **Tested down-migration CLI, postponed from PR 33 (ops-01 item 4).** The
  eight SQL one-liners in `coach-mcp/README.md` ("Going back to a version
  without …") stay the way back across a schema change; since PR 33 they run
  through `docker compose run`, and `scripts/deploy.sh --rollback` refuses
  while the database is newer than the target image and points to them. A
  `python -m coach_mcp downgrade --to N` with a test per step 9 → 1 would
  replace them.
- **Not planned yet.** An English README and an ingress option without
  Tailscale for other users of the repo; a shared package for the bearer
  middleware if a third Python service appears (`auth.py` is copied between
  `garmin-mcp` and `coach-mcp`); creating a Hevy workout from a strength
  session recorded on the watch (after PR 19). The weekly report push is part
  of PR 14 since 2026-10-09.
