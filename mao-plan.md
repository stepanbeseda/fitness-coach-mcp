# Plan — fitness-coach-mcp

Created: 2026-10-03. The plan is extended by `/mao`: every new task gets a
chapter and a table row; existing chapters are never rewritten, only the
"Delivered" blocks and the table status are added.

## 1. Decisions and boundaries

- **What it is.** A self-hosted, single-user stack that gives a Claude
  fitness coach access to the owner's Hevy and Garmin Connect data: two MCP
  backends (`hevy-mcp/`, `garmin-mcp/`) behind one OAuth 2.1 gateway
  (`gateway/`), exposed only through a dedicated Tailscale node. It is not
  multi-user, has no web UI of its own and stores no health data outside the
  owner's server; the only exception is the morning briefing, which since
  PR 08 is sent to the owner's Telegram chat and kept there by Telegram.
- **One way in.** Every MCP backend is reachable only through the gateway. A
  new backend means: a `[[backends]]` entry in `gateway/config.toml`, its own
  internal token secret, a hardened service in `docker-compose.yml` (the
  `x-hardening` anchor: non-root, read-only, no capabilities, memory and pid
  limits, healthcheck), and coverage in `scripts/setup-secrets.sh`,
  `scripts/e2e-test.sh` + `scripts/e2e_flow.py`, `scripts/healthcheck.sh` and
  `scripts/smoke-test.sh` + `scripts/smoke_test.py`.
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
- **Privacy of logs.** Logs and the audit trail contain tool name, outcome and
  duration only — never arguments, results, tokens or health values.
- **Supply chain.** Python dependencies are pinned in `pyproject.toml` and
  hash-locked with `scripts/lock-deps.sh`; base images are pinned by digest.
  A new dependency needs a reason in the spec.
- **Coach state lives in one place.** PR 03 introduces the service
  `coach-mcp/` with a SQLite database on its own volume. PR 04, PR 05 and
  PR 06 extend that service instead of adding further services or databases.
  It reads Garmin and Hevy data only through the existing backends on the
  private `mcp` network, authenticated with internal tokens.
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
| 07 | Strength workouts on the watch | — | planned |
| 08 | Daily briefing via Telegram | 04 | done |
| 09 | Strength history and progress | 05 | done |
| 10 | Separate e2e image tags and a configurable time zone | — | done |
| 11 | Online and scheduled backups | 03 | done |
| 12 | Read-only Garmin token for the coach | 04 | done |
| 13 | Plan reconciliation | 05 | done |
| 14 | Goal progress and the Monday report | 05, 08 | done |
| 15 | Searchable coach memory | 03 | done |
| 16 | Correlations | 05 | done |
| 17 | Sync-aware briefing | 04 | planned |
| 18 | History repair | 05 | done |
| 19 | More Garmin signals | — | done |
| 20 | Operations alerts via Telegram | 08 | done |
| 21 | Static checks in the test stage | — | planned |
| 22 | Two-way Telegram | 08 | planned |

Chapters 10–22 come from the project review of 2026-10-09. Suggested order:
10, 11, 12, 13, 14, 09, 07, 15, 16, 17, 18, then 19, 20, 21 and last 22 (the
only chapter that accepts inbound messages). PR 12 goes before PR 09, so
`coach-mcp` does not collect a second full-access backend token.

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
payloads only (owner capture, section 5); decision D4 accepts the library's
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

Out of scope: device settings, battery, alarms.

**Done when:**
1. `get_device_status` returns the shaped fields from canned payloads and is
   fail-soft (tests); the real payload shape is recorded in the PR.
2. A retry without a new upload makes one cheap call instead of the full
   read; a new upload triggers the full read (tests with a fake backend).
3. The missing-data line names the last sync time and falls back to today's
   wording when the time is unknown (tests).
4. Tool lists, both service READMEs and `docs/coach-instructions.md` are
   updated.
5. The four-command check is green.
6. On the test host: a briefing before the morning sync names the last sync
   time.

**Requested:** 2026-10-09, by the owner, from the project review (feature 7).

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
   plain Czech text.
3. Logs carry outcome words only; the README privacy note covers inbound
   messages.
4. The four-command check is green.
5. On the test host: `/plan` and a free-text note work from the owner's
   phone, and the note is returned by `get_coach_context`.

**Requested:** 2026-10-09, by the owner, from the project review (feature 3).

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
  account); the row stays planned.
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
  PR 20 (merged 2026-10-11) wait for `DEPLOY-2026-10-11.md`.
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
  turns out noisy; PR 17 on sync-aware briefing is the natural place). (b) The
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
  payload (key names such as `calendarDate` and `time5K`, list against dict, int
  against float, order, what "no predictions" looks like) is unverified: the
  shaper was built from the library source and canned payloads, and degrades to
  `available: false` or an error section when the real shape differs. The owner
  captures it once on the test host (spec section 11,
  `docker compose exec -T garmin-mcp python - < .claude/mao-log/PR-14-proto/capture.py`,
  key names, types and counts only); a difference goes back to a designer as an
  addendum for fixtures and shaper keys. (b) Accepted D4: on a Garmin 5xx the
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
- **Not planned yet.** An English README and an ingress option without
  Tailscale for other users of the repo; a shared package for the bearer
  middleware if a third Python service appears (`auth.py` is copied between
  `garmin-mcp` and `coach-mcp`); creating a Hevy workout from a strength
  session recorded on the watch (after PR 19). The weekly report push is part
  of PR 14 since 2026-10-09.
