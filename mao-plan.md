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

## 2. Common PR conditions

- One PR = one chapter; documentation in the same PR.
- Green check: `docker build --target test garmin-mcp`,
  `docker build --target test gateway` and `scripts/e2e-test.sh` (a new
  service adds its own `--target test` build to this list in the PR that
  introduces it).
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
| 06 | Weather and calendar | 03 | planned |
| 07 | Strength workouts on the watch | — | planned |
| 08 | Daily briefing via Telegram | 04 | **in progress** (branch mao/pr08-briefing-telegram) |
| 09 | Strength history and progress | 05 | planned |

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
- **PR 07 strength payload and exercise catalogue.** Unverified; the chapter
  starts with a spike.
- **Cycling power target.** `garmin-mcp/README.md` notes that a custom power
  range is sent as `power.zone` with two target values and still needs a
  visual check in Garmin Connect.
- **PR 05 history job, non-blocking review findings.** Recorded 2026-10-07
  (review round 2, owner's decision: ship PR 05); a follow-up candidate for
  PR 09 or a later chapter. N5: a snapshot with one section in error is
  stored as ok, so a transient error of one Garmin endpoint during the first
  backfill leaves that metric NULL for every backfill day older than three
  days for good (spec-accepted; a risk to PR 05 Done-when 8 on the test
  host). N6: a flapping outage (a share of calls failing for several runs)
  can still give up a few days, because every ok between errors confirms the
  held errors. N7: an activities-only outage stops each run at the first
  blocks, so the recovery backfill is delayed until activities work again
  (nothing is lost). N8: two guard branches have no failing test, the
  `isinstance(..., list)` check of the activities probe and `given_up`
  counted for activities (`history.py`).
- **Not planned yet.** Correlation analysis (sleep vs. performance) once
  PR 05 has collected at least eight weeks of data; sending the weekly
  report through the PR 04 push channel; an English README and an ingress
  option without Tailscale for other users of the repo.
