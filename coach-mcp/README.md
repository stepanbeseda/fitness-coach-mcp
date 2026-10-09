# coach-mcp (coach memory)

A small MCP server that keeps the coach's state between conversations: goals, constraints (injuries, available
days, equipment), a day-by-day training plan with status, and a notes log. The data lives in an SQLite file on the
`coach-data` volume. It runs as Streamable HTTP (stateless, JSON) on `:8000/mcp` in a private Docker network; MCP
clients reach it only through the gateway (`/coach/mcp`), which sends an internal bearer token
(`/run/secrets/coach_backend_token`). Without the token the server rejects every MCP request (fail closed). The
only services it calls itself are `garmin-mcp` (four read tools, read-only token `garmin_readonly_token`),
`hevy-mcp` (two read tools, internal token `hevy_backend_token`), optionally the Telegram Bot API (morning
briefing) and, on request, Open-Meteo (weather forecast, rounded coordinates only). Every night it stores the
daily history from Garmin and strength workouts from Hevy for trends, the weekly report, correlations and strength
progress, and uses that history to suggest a status for past planned sessions. Nothing is deleted: goals are
closed, constraints are marked as resolved, and replaced sessions stay as `superseded`.

## Tools

| Tool | Type | What it does |
|---|---|---|
| `get_coach_context` | read | active goals (with days left to the target date), active constraints, sessions of the current ISO week, planned sessions of the last 7 days with a suggested status (`to_confirm`) and the latest 10 notes, in one call |
| `get_plan` | read | sessions in a date range (default this week, max. 92 days) and a completion summary: counts of planned / done / skipped / moved, overdue planned sessions, `completion_rate`; past planned sessions carry a suggested status `suggestion` (see Plan reconciliation) |
| `set_goal` | write | creates a goal, or (with `goal_id`) changes the given fields; closed with status `achieved` / `abandoned` |
| `set_constraint` | write | creates a constraint (`kind` + title + details), or changes the given fields; status `resolved` once it no longer applies |
| `save_plan` | write | saves the plan for one ISO week: replaces planned sessions from today on (the old ones stay as `superseded`), keeps done, skipped, moved and past ones |
| `update_session` | write | sets a session's status (`planned` / `done` / `skipped` / `moved`); `moved` with `new_date` creates a linked planned copy; optional note |
| `add_note` | write | adds a dated note to the log, optionally linked to a session |
| `get_daily_briefing` | read | today's morning briefing: green / yellow / red verdict, the numbers behind it, what is missing, today's plan and alerts; computed the same way as the morning push, from current data |
| `get_trends` | read | stored daily Garmin history (HRV, sleep, resting HR, Body Battery, stress, readiness, load, VO2max, activities by sport) for any period by day, week or month: count, mean, minimum, maximum, change |
| `get_weekly_report` | read | one ISO week against the average of the previous four: endurance by sport, sleep, HRV, resting HR, plan completion, strength volume and sets per muscle group from Hevy, new records (including estimated 1RM) and warnings; sections without data have `available: false` |
| `get_strength_progress` | read | estimated 1RM (Epley) per exercise and workout from the stored Hevy history: best set of each training day, best, first and latest value and a plateau flag (`plateau_weeks`, default 4) |
| `search_notes` | read | full-text search in the whole notes log (`get_coach_context` has only the latest 10): all words of the query, newest first, optionally a date range or a session, max. 50 results and a `more` flag |
| `list_goals` | read | all goals including achieved (`achieved`) and abandoned (`abandoned`) ones, with status and creation and last-change time |
| `list_constraints` | read | all constraints, active and resolved (`resolved`), with status and creation and last-change time |
| `get_correlations` | read | pairwise correlations between 2–6 metrics from the `get_trends` catalogue: Pearson and Spearman and the number of paired days, optionally with a lag of 0–3 days (metric `a` on day D against `b` on day D + lag); under 14 pairs or for a constant series `available: false`; always with a caveat that correlation is not causation |
| `get_weather_forecast` | read | 7-day Open-Meteo forecast for the home location from `.env` or given coordinates: per day sunrise and sunset, a summary and hourly temperature, apparent temperature, precipitation probability and wind; the best window for outdoor training each day; 30 min cache |

All writes are annotated as not read-only and not destructive; their description contains the sentence "Only after
the athlete confirmed", so the coach proposes them and writes only after confirmation. Dates are `YYYY-MM-DD` in
local time (`TZ`); "today" and the week (Monday to Sunday, ISO) are computed from it.

## Searching notes

`search_notes` searches the whole notes log through an SQLite FTS5 full-text index (`notes_fts`), not just the
latest 10 notes from `get_coach_context`. Every word of the query must appear in the note. Diacritics and case are
ignored (`boli` finds "Bolí"). FTS5 does no stemming, so a different word form does not match (`knees` does not
find "knee", `bolest` does not find "bolí"); search by word prefix instead: `knee*` finds knee and knees, `bol*`
finds bolí, bolest and bolelo. Supported syntax is `"exact phrase"`, `OR`, `NOT` and `prefix*`; a word with
punctuation (`-`, `:`, `,`, `.`, `?`, `'`) goes in double quotes (`"low-carb"`). An invalid query returns a tool
error with this advice (without any text from SQLite). Results are newest first: 20 by default, at most 50,
`more: true` when there are further matches. Optional `start_date` and `end_date` (both inclusive) and
`session_id`; an unknown session is an error. The query is never written to the log.

## Morning briefing

Once a day (and on request through the `get_daily_briefing` tool) the server reads the morning data from
`garmin-mcp` (`get_recovery_snapshot`, `get_hrv_trend`, `list_scheduled_workouts`), adds today's plan from its own
memory and computes a verdict without an LLM. The **worst indicator** decides; a missing value rules out green (at
most yellow) and is named in the message.

| Indicator | Green | Yellow | Red |
|---|---|---|---|
| Readiness (`morning_score`, otherwise `score`) | 70 or more | 40 to 69 | under 40 |
| Last night's HRV vs. the lower bound of the baseline | at least at the bound | below the bound | below the bound 3 or more nights in a row |
| Sleep (in whole minutes) | 7 h or more | 6 to 7 h | under 6 h |
| Body Battery at wake | 50 or more | under 50 | never |

Alerts (the message lists them only when they are **new**):

- HRV below baseline 3 or more nights in a row;
- resting HR 5 or more bpm above the 7-day average;
- acute to chronic load ratio outside Garmin's optimal range (status `LOW`, `HIGH` or `VERY_HIGH`);
- a workout scheduled yesterday in the Garmin calendar was not completed;
- a session planned yesterday in the coach's plan with no activity of the same sport in Garmin (according to the
  plan reconciliation suggestion);
- Garmin is not logged in.

New = holds this morning and did not hold at the previous evaluation; the state is in the `alert_state` table. The
exception is both missed-session alerts (Garmin calendar and the coach's plan): they are reported every day on
which some session planned for yesterday was left undone (it is a different session each day). An unknown
condition (missing data) does not change the state.

The scheduler runs at `COACH_BRIEFING_TIME` (default 06:30, time zone `TZ`). When sleep or last night's HRV is
missing, it retries every `COACH_BRIEFING_RETRY_MINUTES` minutes and at `COACH_BRIEFING_CUTOFF` at the latest it
sends what it has. A Garmin login problem means the push goes out immediately. When the service starts after the
cutoff, nothing is sent that day. A missing HRV baseline (Garmin computes it only after about 3 weeks of wearing
the watch) is not waited for, only named. On a temporary Telegram error (5xx, 429, network) sending is retried
until the cutoff; on a permanent error (another 4xx, e.g. a wrong token or chat id, bot blocked) it gives up for
that day.

The message is plain text: the first line is the verdict with a coloured circle and the date
(`🟡 YELLOW · morning briefing 7 Oct`, with `⚠️` after the circle when there are new alerts), then an empty line and
the text. The whole message is truncated to 4000 bytes (Telegram accepts at most 4096 characters). Without
`COACH_TELEGRAM_CHAT_ID` the scheduler does not start and `/healthz` reports `"scheduler":"off"`. When the
scheduler stops unexpectedly, the log has `briefing status=stopped` and `/healthz` reports `"scheduler":"stopped"`
(`healthcheck.sh` reports it as a problem).

The push text is localized: `COACH_LANGUAGE` is `en` (default) or `cs`. Only the words change; the verdict, the
rules, the numbers and the JSON from `get_daily_briefing` are the same in every language (apart from its `push`
text). The catalogue is in `src/coach_mcp/messages.py`; adding a language means one more `Texts` instance in
`LANGUAGES`. Session titles from the plan and Garmin workout names are shown as stored. Example (English):

```text
🟡 ⚠️ YELLOW · morning briefing 7 Oct

Verdict: yellow – cut volume by 20–30 %, intensity at most RPE 7–8, cardio in zone 2.
Readiness: 61 (yellow)
HRV: missing
Sleep: 6 h 15 min, score 70 (yellow)
Body Battery at wake: missing
Missing: HRV, Body Battery (the watch may not have synced yet). Without these data the verdict cannot be green.
For full load: readiness at least 70; sleep at least 7 h.
Today in Garmin: Easy run 40 min
Alerts:
- Resting HR 58 bpm, 6 above the 7-day average of 52 bpm.
- Yesterday's scheduled workout was not completed: Intervals 6×800 m.
```

Manual send (the alert state does not change):

```bash
docker compose exec coach-mcp python -m coach_mcp send-briefing
```

Exit code: 0 sent, 1 failed, 2 push not configured or wrong argument.

## Nightly history

Every night (`COACH_HISTORY_TIME`, default 03:30, time zone `TZ`) the server stores daily values from `garmin-mcp`
in the `history_days` table, one row per day: readiness, last night's HRV and the weekly average, sleep (duration,
score), resting HR, Body Battery (highest and lowest), stress, acute and chronic load, load ratio, VO2max and
activity totals by sport (count, duration, distance, training load). The row for day D has the sleep and HRV of
the night that ended on the morning of D, and the activities that started on D. Today is not stored (it is not
complete): a day is loaded for the first time the day after, and then twice more over the next two days to catch a
late watch sync. `get_trends`, `get_weekly_report` and `get_correlations` work on the stored history;
`get_hrv_trend` reaches back only 28 days and `get_body_battery` 31.

Only missing days are filled in, so after a restart it continues where it stopped (progress is given by the stored
data). The first run comes one minute after the service starts and loads `COACH_HISTORY_BACKFILL_DAYS` days back
(default 90): about 470 Garmin Connect calls in 103 `garmin-mcp` calls, with a pause of
`COACH_HISTORY_CALL_DELAY_SECONDS` (15 s) between them, roughly 30 minutes in total. Between
`COACH_BRIEFING_TIME` and `COACH_BRIEFING_CUTOFF` the run pauses and continues after the cutoff. Every following
night it is about 16 calls.

- Garmin answers 429 (rate limit): the run stops and retries in an hour, then in 2, 4 and at most 8 hours.
- Garmin is not logged in or `garmin-mcp` does not respond: no calls, next attempt in an hour (at the latest at
  `COACH_HISTORY_TIME`).
- Garmin Connect is down (outage, server without internet): `garmin-mcp` returns an error for every day. After
  three errors in a row the server asks for a day that has already succeeded once; when that one fails too, the
  run ends as `service_error`, the outage is not counted against the days and the next attempt is in 1, 2, 4 and
  at most 8 hours.
- An error that concerns only one day (Garmin returns other days) is counted against that day; a day that fails
  like this three times stays empty (`given_up` in the log). A day that already has data stops being reloaded
  after three errors.
- On a completely empty history the server has no day to compare with and treats three errors in a row as an
  outage; this resolves as soon as the first day succeeds (with the next day at the latest).

Strength workouts from Hevy: after Garmin, the same run loads through `hevy-mcp` the workouts changed since the
last complete run, including edited and deleted ones (`get-workout-events`, 10 per page), and for each exercise
its template once to get the primary muscle group (`get-exercise-template`). Only a per-exercise summary is stored
(working sets, volume in kg, best set and its estimated 1RM by Epley), not individual sets; warm-up sets are not
counted. The first run loads the same `COACH_HISTORY_BACKFILL_DAYS` days back with the same pause between calls
(one call per 10 workouts and one per exercise), the following nights usually one call. Every listing re-reads the
last 3 days to catch late syncs. When Hevy or `hevy-mcp` is down, the Garmin part finishes normally, the strength
data stays uncovered (`available: false`) and the next attempt is in an hour, under a rate limit in 1 to 8
hours; nothing is given up, a failed listing is repeated in full next time and a failed template is retried at
most once a day.

From `garmin-mcp` the server may call only four read tools (`get_recovery_snapshot`, `get_hrv_trend`,
`list_scheduled_workouts`, `list_activities`). The coach code does not call its write tools, and `garmin-mcp`
rejects them with the coach's read-only token before they run. From `hevy-mcp` it may call only
`get-workout-events` and `get-exercise-template`. For each run the log has one line for Garmin:
`history status=… fetched=… failed=… pending=… given_up=… ms=… matched=… no_activity=…` (`reconcile=error kind=…`
when plan reconciliation fails; the history run does not fail because of it) and one for Hevy:
`history status=… part=strength workouts=… deleted=… templates=… failed=… skipped=… ms=…` (no data or values;
`skipped` = events from Hevy that could not be used). The Hevy part runs before plan reconciliation, so its line
comes before the Garmin line in the log. With `COACH_HISTORY_TIME` inside the briefing window there is also one
`history status=config_warning` line after startup. `/healthz` reports a `history` field: `on`, `off`
(`COACH_HISTORY_ENABLED=false`) or `stopped` (the task ended unexpectedly).

## Plan reconciliation

After every nightly history run the server compares the planned sessions of the last 7 days (excluding today)
with the stored activity totals of the same day and sport (`running`, `cycling`, `swimming`, `strength`;
`mobility` and `other` get no suggestion) and stores a suggestion in the `session_suggestions` table: `matched`
with evidence (sport, number of activities, duration and distance of that sport for the day) or `no_activity`. A
day whose activities have not been loaded yet, or were given up after three errors, has no suggestion. Several
sessions of the same sport on one day: activities are first assigned to sessions already marked `done`, the rest
to planned ones in plan order. A suggestion has `final: false` while the day can still be reloaded (until the third
day after it, late watch sync). The server never changes a session's status: `get_plan` and `get_coach_context`
show the suggestion only for sessions in status `planned`, the coach checks it and, after confirmation, writes
`update_session`. A strength session is matched with a strength record from the watch (`strength_training`) or
with a Hevy workout on the same day (evidence `source: hevy` with the number of sets and volume); a day not yet
covered by the Hevy strength history is matched only with the watch. Without the nightly history
(`COACH_HISTORY_ENABLED=false`) no suggestions are made. The morning briefing names yesterday's planned sessions
with `no_activity` (alert above).

## Data

- `goals`: a goal (title, optionally `target`, `target_date`, `target_result`), status `active` / `achieved` / `abandoned`.
- `athlete_constraints`: a constraint of kind `injury` / `limitation` / `availability` / `equipment` / `other`
  with a title and free text, status `active` / `resolved`.
- `sessions`: a session with date, type (`running`, `cycling`, `swimming`, `strength`, `mobility`, `other`),
  description and optionally intensity, duration, distance and a link to a Garmin workout or Hevy routine.
  Statuses: `planned`, `done`, `skipped`, `moved` (the original record stays and the copy on the new date points
  to it through `moved_from_id`), `superseded` (replaced by a newer week plan; hidden and not counted in the
  default listing).
- `notes`: dated notes, optionally linked to a session; the full-text index `notes_fts` (SQLite FTS5, insensitive
  to diacritics and case) is filled by a trigger on every note insert.
- `briefing_runs`: a day on which the scheduler successfully sent the briefing (verdict, send time).
- `alert_state`: for each alert the last known state, evaluation date and when it last fired.
- `session_suggestions`: suggested status of a planned session (`matched` with evidence or `no_activity`, `final`,
  evaluation date); one row per session, rewritten every night.
- `history_days`: one row per day with values from Garmin (readiness, HRV, sleep, resting HR, Body Battery,
  stress, load, VO2max) and activity totals by sport; when and with how many errors the day was last loaded.
- `strength_workouts`: one row per Hevy workout (id, local start date, last-change time in Hevy) with a
  per-exercise summary (template, title, working sets, volume in kg, best set and estimated 1RM); a deleted
  workout stays as a `deleted` marker without exercises.
- `hevy_templates`: Hevy exercise templates (title, type, primary muscle group), or a note that the template does
  not exist in Hevy.
- `hevy_sync`: from where and until when the strength history is complete (last complete run).

The schema version is in `PRAGMA user_version`; migrations are forward-only (an ordered list of SQL scripts, each
in one transaction). An older image refuses to open a newer database so that it does not damage it. Online backups
(`python -m coach_mcp backup`, called by `scripts/backup.sh`) and restore (`python -m coach_mcp restore`): see the
root `README.md` (section Backups); the `coach-data` volume is the only copy of this data.

Run `scripts/backup.sh` before moving to an older version.

**Going back to a version without note search** (schema 6 → 5). An older image refuses a database with schema 6
(`database schema version 6 is newer`). Before deploying the older version, drop only the full-text index of the
notes (the `notes_fts` table and the `notes_fts_insert` trigger); goals, constraints, plan, notes, briefing state
and history stay, and the index is rebuilt from all notes after the newer version is deployed again. The service
must be stopped; run the commands one at a time:

```bash
docker compose stop coach-mcp
docker run --rm --network none -v fitness-coach-mcp_coach-data:/data/coach --entrypoint python fitness-coach-mcp/coach-mcp:0.1.0 -c 'import sqlite3, sys; c = sqlite3.connect("file:/data/coach/coach.db?mode=rw", uri=True); v = c.execute("PRAGMA user_version").fetchone()[0]; v == 6 or sys.exit(f"schema version {v}, expected 6: nothing changed"); c.executescript("BEGIN IMMEDIATE; DROP TRIGGER notes_fts_insert; DROP TABLE notes_fts; PRAGMA user_version = 5; COMMIT;"); print("schema version", c.execute("PRAGMA user_version").fetchone()[0])'
```

The second command must print `schema version 5`. Then deploy the older version (`git checkout <commit before
PR 15>` and `docker compose up -d --build --no-deps coach-mcp`). When it prints `… nothing changed`, the database
does not have schema 6 and nothing changed. On an empty volume it ends with the error `unable to open database
file` and creates nothing. To go back to an even older version, run this 6 → 5 procedure first and then the ones
below.

**Going back to a version without strength history** (schema 5 → 4). An older image refuses a database with
schema 5 (`database schema version 5 is newer`). Before deploying the older version, drop only the strength
history tables; goals, constraints, plan, notes, briefing state and Garmin history stay (the server reloads the
strength history from Hevy after the newer version is deployed again). The service must be stopped; run the
commands one at a time:

```bash
docker compose stop coach-mcp
docker run --rm --network none -v fitness-coach-mcp_coach-data:/data/coach --entrypoint python fitness-coach-mcp/coach-mcp:0.1.0 -c 'import sqlite3, sys; c = sqlite3.connect("file:/data/coach/coach.db?mode=rw", uri=True); v = c.execute("PRAGMA user_version").fetchone()[0]; v == 5 or sys.exit(f"schema version {v}, expected 5: nothing changed"); c.executescript("BEGIN IMMEDIATE; DROP TABLE strength_workouts; DROP TABLE hevy_templates; DROP TABLE hevy_sync; PRAGMA user_version = 4; COMMIT;"); print("schema version", c.execute("PRAGMA user_version").fetchone()[0])'
```

The second command must print `schema version 4`. Then deploy the older version (`git checkout <commit before
PR 09>` and `docker compose up -d --build --no-deps coach-mcp`). When it prints `… nothing changed`, the database
does not have schema 5 and nothing changed. On an empty volume it ends with the error `unable to open database
file` and creates nothing. To go back further, run this 5 → 4 procedure first and then the following ones.

**Going back to a version without plan reconciliation** (schema 4 → 3). An older image refuses a database with
schema 4 (`database schema version 4 is newer`). Before deploying the older version, drop only the suggestions
table; goals, constraints, plan, notes, briefing state and history stay. The `alert_state` table keeps its
extended key check (and the `missed_planned_session` row); the older version ignores it and migration 4 runs again
after the newer version is deployed. The service must be stopped; run the commands one at a time:

```bash
docker compose stop coach-mcp
docker run --rm --network none -v fitness-coach-mcp_coach-data:/data/coach --entrypoint python fitness-coach-mcp/coach-mcp:0.1.0 -c 'import sqlite3, sys; c = sqlite3.connect("file:/data/coach/coach.db?mode=rw", uri=True); v = c.execute("PRAGMA user_version").fetchone()[0]; v == 4 or sys.exit(f"schema version {v}, expected 4: nothing changed"); c.executescript("BEGIN IMMEDIATE; DROP TABLE session_suggestions; PRAGMA user_version = 3; COMMIT;"); print("schema version", c.execute("PRAGMA user_version").fetchone()[0])'
```

The second command must print `schema version 3`. Then deploy the older version (`git checkout <commit before
PR 13>` and `docker compose up -d --build --no-deps coach-mcp`). When it prints `… nothing changed`, the database
does not have schema 4 and nothing changed. On an empty volume it ends with the error `unable to open database
file` and creates nothing. To go back to an even older version, run this 4 → 3 procedure first and then the
following ones.

**Going back to a version without history** (schema 3 → 2). An older image refuses a database with schema 3
(`database schema version 3 is newer`). Before deploying the older version, drop only the history table; goals,
constraints, plan, notes and briefing state stay (the server reloads the history from Garmin after the newer
version is deployed again). The service must be stopped; run the commands one at a time:

```bash
docker compose stop coach-mcp
docker run --rm --network none -v fitness-coach-mcp_coach-data:/data/coach --entrypoint python fitness-coach-mcp/coach-mcp:0.1.0 -c 'import sqlite3, sys; c = sqlite3.connect("file:/data/coach/coach.db?mode=rw", uri=True); v = c.execute("PRAGMA user_version").fetchone()[0]; v == 3 or sys.exit(f"schema version {v}, expected 3: nothing changed"); c.executescript("BEGIN IMMEDIATE; DROP TABLE history_days; PRAGMA user_version = 2; COMMIT;"); print("schema version", c.execute("PRAGMA user_version").fetchone()[0])'
```

The second command must print `schema version 2`. Then deploy the older version (`git checkout <commit before
PR 05>` and `docker compose up -d --build --no-deps coach-mcp`). When it prints `… nothing changed`, the database
does not have schema 3 and nothing changed. On an empty volume it ends with the error `unable to open database
file` and creates nothing. To go all the way back to the version without the briefing (schema 1), run this 3 → 2
procedure first and then the following 2 → 1.

**Going back to a version without the morning briefing** (schema 2 → 1). An older image refuses a database with
schema 2 (`database schema version 2 is newer`). Before deploying the older version, drop only the briefing
tables; goals, constraints, plan and notes stay. The service must be stopped; run the commands one at a time:

```bash
docker compose stop coach-mcp
docker run --rm --network none -v fitness-coach-mcp_coach-data:/data/coach --entrypoint python fitness-coach-mcp/coach-mcp:0.1.0 -c 'import sqlite3, sys; c = sqlite3.connect("file:/data/coach/coach.db?mode=rw", uri=True); v = c.execute("PRAGMA user_version").fetchone()[0]; v == 2 or sys.exit(f"schema version {v}, expected 2: nothing changed"); c.executescript("BEGIN IMMEDIATE; DROP TABLE alert_state; DROP TABLE briefing_runs; PRAGMA user_version = 1; COMMIT;"); print("schema version", c.execute("PRAGMA user_version").fetchone()[0])'
```

The second command must print `schema version 1`. Then deploy the older version (`git checkout <older commit>`
and `docker compose up -d --build --no-deps coach-mcp`). When it prints `… nothing changed`, the database does not
have schema 2 and nothing changed. On an empty volume it ends with the error `unable to open database file` and
creates nothing.

## Weather

`get_weather_forecast` calls `GET https://api.open-meteo.com/v1/forecast` with the parameters `latitude`,
`longitude`, `hourly=temperature_2m,apparent_temperature,precipitation_probability,wind_speed_10m`,
`daily=sunrise,sunset`, `timezone=auto` (times are local to the location), `forecast_days=7` and units °C and
km/h. The coordinates come from `.env` (`COACH_HOME_LATITUDE`, `COACH_HOME_LONGITUDE`) or from the arguments (both;
they take precedence, for travel) and are rounded to two decimal places (about 1 km) before sending; nothing else
is sent. The response is limited to 512 KiB and validated (shape, times, numbers); a value that is not a finite
number is `null`.

Best window for outdoor training (`best_outdoor_windows`, at most one per day):

| Condition | Limit (inclusive) |
|---|---|
| daylight | at least 30 minutes of the hour between sunrise and sunset |
| hour not started yet | the start of the hour is not before the current local time |
| apparent temperature | −5 to 25 °C |
| precipitation probability | at most 30 % |
| wind | at most 25 km/h |
| choice | longest continuous run of hours; on a tie the lower maximum precipitation probability, then the earlier one |

Cache: the response is kept in memory for 30 minutes per rounded coordinates (at most 16 locations, the oldest is
dropped); errors are not cached and after 30 minutes it is fetched again. The shape of the answer (days from
today, windows that have not started yet) is computed on every call from the current time.

Errors are short and contain no coordinates: `No home location configured: …` (no location in `.env` and no
arguments), `Give both latitude and longitude, or neither.`, `latitude must be between -90 and 90 and longitude
between -180 and 180.` (direct calls only; the tool schema rejects it earlier), `Weather service timed out.` (one
read 10 s, the whole call 20 s), `Weather service unreachable (<error class>).`, `Weather service answered HTTP
<code>.` and `Weather service returned an unexpected response.` The log has only `weather status=fetched|error
reason=…` lines (result, code, duration, error class name), never coordinates.

Weather data: [Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0 licence).

## Configuration (env)

| Variable | Default | Meaning |
|---|---|---|
| `COACH_MCP_DB_PATH` | `/data/coach/coach.db` | SQLite file (absolute path, the directory must exist) |
| `COACH_MCP_BEARER_TOKEN_FILE` | `/run/secrets/coach_backend_token` | internal token from the gateway (min. 16 characters) |
| `COACH_MCP_HOST` | `0.0.0.0` | address the server listens on |
| `COACH_MCP_PORT` | `8000` | port (1 to 65535) |
| `TZ` | `Europe/Prague` | what "today" means; in compose it is set through `ATHLETE_TZ` in `.env` |
| `COACH_LANGUAGE` | `en` | language of the morning briefing push text: `en` or `cs` (case-insensitive); see Morning briefing |
| `COACH_MCP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`; logs: only the tool name, result and duration, never arguments or stored texts |
| `COACH_MCP_ALLOWED_HOSTS` | *(empty)* | enables Host header checking (e.g. `coach-mcp:8000`) |
| `COACH_TELEGRAM_CHAT_ID` | *(empty)* | chat id of your chat with the bot (integer, negative for groups); enables the morning briefing; never logged |
| `COACH_TELEGRAM_TOKEN_FILE` | `/run/secrets/telegram_bot_token` | bot token from @BotFather; with a chat id set it must look like a token, otherwise the service does not start |
| `COACH_GARMIN_MCP_URL` | `http://garmin-mcp:8000/mcp` | address of `garmin-mcp` (ends with `/mcp`) |
| `COACH_GARMIN_TOKEN_FILE` | `/run/secrets/garmin_readonly_token` | read-only token for `garmin-mcp` (min. 16 characters); `garmin-mcp` rejects write tools with it |
| `COACH_HEVY_MCP_URL` | `http://hevy-mcp:3000/mcp` | address of `hevy-mcp` (ends with `/mcp`) |
| `COACH_HEVY_TOKEN_FILE` | `/run/secrets/hevy_backend_token` | internal token for `hevy-mcp` (min. 16 characters); without it the strength history is not loaded, the Garmin part keeps running |
| `COACH_BRIEFING_TIME` | `06:30` | when the scheduler starts (`HH:MM`, time zone `TZ`) |
| `COACH_BRIEFING_CUTOFF` | `09:30` | latest send time, even with missing data (later than the start time) |
| `COACH_BRIEFING_RETRY_MINUTES` | `15` | pause between attempts when sleep or HRV is missing (1 to 180) |
| `COACH_HISTORY_ENABLED` | `true` | nightly history from Garmin and Hevy (`true` / `false`) |
| `COACH_HISTORY_TIME` | `03:30` | when (`HH:MM`, `TZ`); a time inside the briefing window moves the run to the cutoff |
| `COACH_HISTORY_BACKFILL_DAYS` | `90` | how many days back to load (1 to 366) |
| `COACH_HISTORY_CALL_DELAY_SECONDS` | `15` | pause between calls to `garmin-mcp` and `hevy-mcp` (1 to 600 s) |
| `COACH_HOME_LATITUDE` | *(empty)* | latitude of the home location for `get_weather_forecast` (decimal degrees, −90 to 90, rounded to two decimal places); only together with the longitude; never logged |
| `COACH_HOME_LONGITUDE` | *(empty)* | longitude (−180 to 180), otherwise the same |

An invalid value stops startup with an error (the container does not start) instead of failing later in a tool.

## Development

```bash
docker build --target test coach-mcp
```

Tests run on every build (no network, SQLite in a temporary directory, a fixed date 2026-10-07 and a fake clock
instead of real time; fake backends stand in for Garmin, Telegram and Open-Meteo).
Dependency locks (`requirements.lock`, `requirements-dev.lock`) are regenerated only through
`scripts/lock-deps.sh`.
