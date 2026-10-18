# Operations and troubleshooting

Day-to-day running of a deployed stack: routine commands, the optional Telegram features, the nightly history,
weather, Garmin token renewal, updates, backups and fixes for common problems. Setup is in the
[README](../README.md#installation).

## Routine tasks

| Task | Command |
|---|---|
| Status of the whole service | `scripts/healthcheck.sh` (for cron: `--quiet`, exit 1 = problem; `--notify` sends changes to Telegram) |
| Logs | `docker compose logs -f gateway` (no query strings or tokens) |
| Audit | `scripts/admin.sh audit 100` |
| Connected clients (grants) | `scripts/admin.sh grants list` |
| Disconnect a client | `scripts/admin.sh grants revoke <id>` (or `--all`) |
| Rotate the Hevy key | generate a new one in Hevy → `scripts/setup-secrets.sh hevy-key` (verifies it and restarts hevy-mcp) |
| Change the password / new TOTP | `scripts/setup-secrets.sh password` / `scripts/setup-secrets.sh totp` |
| Rotate internal tokens | `scripts/setup-secrets.sh backend-tokens` (including the read-only token `coach-mcp` uses for `garmin-mcp`) |
| Morning briefing by hand | `docker compose exec coach-mcp python -m coach_mcp send-briefing` |
| Weekly report by hand | `docker compose exec coach-mcp python -m coach_mcp send-weekly-report` |
| Nightly history (progress) | `docker compose logs coach-mcp` (lines `history status=…`) |
| Backup | `scripts/backup.sh` (for cron: `--quiet`, exit 1 = no archive was created) |
| Telegram bot | `scripts/setup-secrets.sh telegram-token` (token from @BotFather, prints the chat id), `scripts/setup-secrets.sh telegram-chat-id` (the chat id again) |
| New versions | Dependabot pull requests and `scripts/check-updates.sh` (see "Updates") |
| Deploy / roll back | `scripts/deploy.sh` / `scripts/deploy.sh --rollback` (`scripts/deploy.sh --status` shows what runs) |
| Known vulnerabilities | `gh workflow run audit.yml` (runs every Monday by itself) |
| Test without touching production | `scripts/e2e-test.sh` (separate Compose project, no internet, no Tailscale) |
| Full check (tests + e2e) | `scripts/check.sh --tag <tag>` (ends with the line `CHECK OK tree=…`) |

`scripts/e2e-test.sh` runs as the Compose project `fcm-e2e` with images `fcm-e2e/<service>:test`. The variables
`E2E_PROJECT` (`fcm-e2e` or `fcm-e2e-<suffix>`) and `E2E_TAG` give a run its own project and images, so two e2e
tests can run at the same time: `E2E_PROJECT=fcm-e2e-pr07 E2E_TAG=pr07 scripts/e2e-test.sh`. Networks and volumes
carry the project prefix, and no service publishes a port on the host.

`scripts/check.sh` is the single command for checking a change. It builds the test stages of `garmin-mcp`,
`gateway` and `coach-mcp` (`docker build --target test --no-cache-filter test`, so `ruff`, `mypy` and the tests
really run), then runs the e2e test with `E2E_PROJECT=fcm-e2e-<tag>` and `E2E_TAG=<tag>`.
- The tag is `--tag`, else `CHECK_TAG`, else `local`.
- Every build and the e2e run hold the lock `/tmp/fcm-docker.lock` (`CHECK_LOCK=0` turns it off). It does not
  read `.env`.
- Logs go to a temporary directory printed at the start. On failure it names the step and prints the last 40
  lines of its log.
- A test stage runs `ruff check src tests`, `mypy` (strict mode, `src/`) and `pytest`; both static checks are
  configured in the service's `pyproject.toml`. A stage whose log lacks the ruff or mypy summary line fails
  the check too.
- The last line on success is `CHECK OK tree=<HEAD tree, 12 chars>[-dirty] garmin=<n> gateway=<n> coach=<n>
  e2e=OK` (`n` = number of passed tests, `-dirty` = uncommitted changes).
- `--only garmin|gateway|coach|e2e` runs a subset (`CHECK PARTIAL`). `--keep-images` keeps the built images
  `fcm-<service>-check-<tag>` and `fcm-e2e/*:<tag>`; otherwise they are removed.

Recommended cron. The first line checks the stack every 15 minutes and sends changes in its problems to your
Telegram chat ([Operations alerts](#operations-alerts-telegram) below); the second one makes the nightly backup:

```cron
*/15 * * * * cd /path/to/fitness-coach-mcp && scripts/healthcheck.sh --quiet --notify >/dev/null
47 2 * * * cd /path/to/fitness-coach-mcp && scripts/backup.sh --quiet
```

With `--notify`, cron mails you only when the alert cannot be sent: the problem lines on stdout went to Telegram
(`>/dev/null`), the failure and the current problems are on stderr. Without the Telegram bot, use
`17 7 * * * cd /path/to/fitness-coach-mcp && scripts/healthcheck.sh --quiet` instead; cron then mails the
problems once a day.

## Morning briefing (Telegram)

Once a day, `coach-mcp` can send a short briefing to your chat with your own Telegram bot. It contains:
- a green / yellow / red verdict by the rules in section 2 of `docs/coach-instructions.md`;
- the numbers behind it (readiness, HRV vs. baseline, sleep, Body Battery);
- today's plan from the coach memory;
- new alerts: HRV below baseline 3+ nights, resting HR 5+ bpm above its 7-day average, load outside Garmin's
  optimal range, yesterday's missed workout in Garmin's calendar and in the coach plan (held back until the
  watch has synced after midnight), an invalid Garmin login.

It is computed without an LLM, and the `get_daily_briefing` tool computes it the same way from current Garmin
data. The text is in English by default; `COACH_LANGUAGE=cs` switches it to Czech (catalogue in
`coach-mcp/src/coach_mcp/messages.py`).

**Privacy:** the messages contain health data and the Monday report also your goal titles. They leave your server
and tailnet for Telegram's servers and stay in the chat history until you delete them; bot chats are not
end-to-end encrypted. The bot token (`secrets/telegram_bot_token`) gives full control of the bot. If it leaks,
revoke it with @BotFather and store the new one with `scripts/setup-secrets.sh telegram-token`.
The same chat also receives the healthcheck alerts, which carry no health data ([Operations alerts](#operations-alerts-telegram)).
What you write to the bot is covered under [Two-way Telegram](#two-way-telegram) below.

To turn it on:

1. In Telegram, send @BotFather `/newbot` and choose the bot's name and username; you get its token. Then send
   your new bot any message (e.g. `/start`); until you do, the bot cannot write to you.
2. `scripts/setup-secrets.sh telegram-token` stores the token (hidden input, verified with Telegram), prints
   the chat id of your chat with the bot (again: `scripts/setup-secrets.sh telegram-chat-id`) and restarts
   `coach-mcp`. An empty `secrets/telegram_bot_token` is already created by `scripts/setup-secrets.sh`;
   `coach-mcp` does not start without it.
3. Add `COACH_TELEGRAM_CHAT_ID=` with that number to `.env` (the time and other options are in `.env.example`).
4. `docker compose up -d coach-mcp`: only `.env` changed, so the configured image is recreated.

The message is plain text; the first line is the verdict with a coloured circle and the date (`🟡 YELLOW ·
morning briefing 7 Oct`, with `⚠️` when there are new alerts). To turn the briefing off, remove
`COACH_TELEGRAM_CHAT_ID` from `.env` and run `docker compose up -d coach-mcp`.

The briefing goes out at `COACH_BRIEFING_TIME` (default 06:30, time zone `TZ`). When the watch does not have
sleep or HRV yet, it retries every `COACH_BRIEFING_RETRY_MINUTES` minutes (15). A retry first asks only when the
watch last uploaded and reads the full data again only after a new upload; when data is missing, the message names
that time (for example `last sync with Garmin: yesterday 22:14`). At `COACH_BRIEFING_CUTOFF` (09:30) at the
latest, it sends what it has and says what is missing. When Garmin is not logged in, it sends the briefing right
away. If the service starts after the cutoff, nothing is sent that day. By hand (e.g. for
a test; the alert state does not change):

```bash
docker compose exec coach-mcp python -m coach_mcp send-briefing
```

The scheduler log (`docker compose logs coach-mcp`) contains only outcomes (`briefing status=sent|waiting|skipped|
gave_up`, `push status=…`, `garmin status=…`, `garmin sync_check status=…`), never the message content, numbers, chat id or token. A manual
`send-briefing` runs as a separate process: it prints the same lines to the terminal only, not to the
container log.

**Monday report.** On Mondays, right after the briefing, a second message follows: last week's numbers from
`get_weekly_report` (endurance, strength, sleep, HRV, resting HR, plan, new bests and watch-outs) and the
progress of your goals with a metric (`get_goal_progress`). It goes out once per ISO week (the week it reports)
and never without the briefing: when the briefing was not sent that day (it gave up, or the service started after
the cutoff), there is no Monday report that week. It uses the same retry interval and cutoff as the briefing.
`COACH_WEEKLY_PUSH=false` in `.env` turns it off (the briefing stays). By hand, for a look at the last complete
week on any day (no state is written, the setting is ignored):

```bash
docker compose exec coach-mcp python -m coach_mcp send-weekly-report
```

The log words are `weekly status=sent|waiting|skipped|gave_up|crashed|state_error`.

## Two-way Telegram

With the briefing on and `COACH_TELEGRAM_CHAT_ID` set to your private chat with the bot, `coach-mcp` also reads
what you write to the bot and answers by fixed rules, without an LLM:

| You send | What happens |
|---|---|
| `/today` or `/dnes` | The answer is today's briefing, computed now from current Garmin data (it can take a minute or two). Nothing is recorded and the scheduled briefing is not affected; alerts already sent this morning are not repeated. |
| `/plan` | This week's sessions (Monday to Sunday) with their ids (`#12`) and status. |
| `/done <id>`, `/skip <id>` | The session is marked done or skipped (`/done 12` or `/done #12`). Without an id, or with one that does not exist, nothing changes and the answer lists today's open sessions with their ids. A session of a later day cannot be done yet; one that was moved or replaced by a newer plan cannot be changed. |
| `/help`, `/start`, an unknown command | The list of commands. |
| any other text | It is saved as a note, dated by the day you sent it (at most 2000 characters; a longer text is refused, not cut). |

Your own message is the confirmation: unlike the coach in Claude, the bot does not ask again. Moving or creating
sessions stays with the coach. The answers are plain text in the language of `COACH_LANGUAGE`.

- **Who is heard.** Only text messages in the private chat whose chat id and sender id both equal
  `COACH_TELEGRAM_CHAT_ID`. Everything else (other people, groups, channels, edited messages) is dropped without
  an answer. Photos, voice messages and other media from you get a short "text only" answer.
- **How it connects.** `coach-mcp` asks Telegram for new messages itself (long polling of `getUpdates`, outbound
  HTTPS like the briefing), so no port is opened and nothing reaches the server from outside.
- **After a stop.** Messages sent while `coach-mcp` was stopped are handled when it starts again, as long as
  Telegram still holds them (about 24 hours). The first start with a bot ignores what was sent to it before.
- **No answer?** The note or the status may still have been stored (an answer is tried twice): `/plan` shows the
  status, the coach sees the note. Editing a message in Telegram does not change a stored note.
- **Turning it off.** `COACH_TELEGRAM_INBOUND=false` in `.env` and `docker compose up -d coach-mcp`; the briefing
  stays. With a group chat id (negative) the inbound part is always off.
- **One reader only.** Do not set a webhook for the bot or run another program that reads its updates; the log
  then says `inbound poll=conflict`. While `coach-mcp` runs, `scripts/setup-secrets.sh telegram-chat-id` finds
  no chat for the same reason: stop `coach-mcp` first (`docker compose stop coach-mcp`), start it afterwards.
  These two Telegram behaviours follow its documentation and were not run against the real bot before the first
  deploy (see `coach-mcp/README.md`).

`scripts/healthcheck.sh` reports the inbound part (`on`, `off`, or `stopped` as a problem). The log
(`docker compose logs coach-mcp`) has one line per handled message with the command word and the outcome
(`inbound cmd=note status=ok reply=sent`), `inbound poll=…` when Telegram cannot be asked and
`inbound status=ignored count=…` for dropped messages; never a message text, the chat id or the token.

**Privacy:** what you write to the bot passes through Telegram's servers and stays in the chat history, like
the briefing, and so do the answers (for example the week's plan from `/plan`). `coach-mcp` keeps the text of a
note in its database (the `coach-data` volume and its backups) and nothing else from your messages. Messages from
anyone else are neither stored nor logged. Whoever can write from your Telegram account can add notes and mark
sessions, and whoever holds the bot token can read what you send to the bot until `coach-mcp` has fetched it.

## Nightly history

`get_hrv_trend` reaches only 28 days back and `get_body_battery` 31. So the coach can see further, `coach-mcp`
stores the daily Garmin values every night (03:30) in SQLite in the `coach-data` volume: HRV, sleep, resting
HR, Body Battery, stress, readiness, load, VO2max and activity totals per sport.
- `get_trends` reads them by day, week or month.
- `get_weekly_report` builds the weekly review from them.
- `get_correlations` computes correlations (association, not cause).
- `get_goal_progress` follows the goals that have a metric: Garmin's race predictions and running VO2max.

Once a night, after the Garmin part, one more call (`get_race_predictions`) fetches Garmin's daily race
predictions for the missing range (on the first run the whole backfill range) into the same database. Its log
line is `history status=… part=predictions days=… ms=…`. A failed call changes nothing, and the next night asks
for the whole missed range again.

The data stays on the server, so no personal data is newly exposed. From the stored activities the server
suggests a status for the planned sessions of the last 7 days every night (`matched` / `no_activity`). It
never changes the status itself; the coach confirms it with `update_session`.

The first run is the largest. A minute after deployment it starts loading 90 days back: about 471 Garmin
Connect calls in 104 `garmin-mcp` calls (one of them for the race predictions of the whole range) with a 15 s
pause, roughly 30 minutes in total. After that it is about 17 calls a night (one for the race predictions), plus
at most 5 answered repair calls for days on which Garmin answered a section with an error (they are asked for
again on later nights, see `coach-mcp/README.md`). Nothing is called between
`COACH_BRIEFING_TIME` and `COACH_BRIEFING_CUTOFF`, so the morning briefing and the history do not share Garmin's
rate limit; the run continues after the cutoff. If `COACH_HISTORY_TIME` falls inside this window, the nightly run
starts only at `COACH_BRIEFING_CUTOFF` (logged once as `history status=config_warning`).

To turn it off: `COACH_HISTORY_ENABLED=false` in `.env` and `docker compose up -d coach-mcp` (stored data
stays, it just stops being extended). The other `COACH_HISTORY_*` options are in `.env.example`.

**Strength workouts from Hevy.** After Garmin, the same run reads, through `hevy-mcp`, the workouts changed
since the last complete run, including edited and deleted ones (`get-workout-events`, 10 per page). For each
exercise it reads its template once, to get the primary muscle group (`get-exercise-template`).
- Only a per-exercise summary is stored (working sets, volume in kg, the best set and its estimated 1RM by
  Epley), not individual sets. Warm-up sets are not counted.
- The first run reads the same `COACH_HISTORY_BACKFILL_DAYS` days back with the same pause between calls (one
  call per 10 workouts and one per exercise). Later nights usually take one call.
- When Hevy or `hevy-mcp` does not work, the Garmin part finishes normally and the strength data stays
  uncovered (`available: false`). The next attempt is in an hour, or in 1 to 8 hours when rate-limited;
  nothing is given up.
- For Hevy the log has a second line, `history status=… part=strength workouts=… deleted=… templates=…
  failed=… skipped=… ms=…` (`skipped` = Hevy events that could not be used). It runs before plan
  reconciliation, so it appears in the log before the Garmin line.

The log (`docker compose logs coach-mcp`) has one Garmin line per run, `history status=done|partial|
paused|rate_limited|service_error|auth|unreachable|no_credentials fetched=… failed=… pending=… given_up=…
ms=… repaired=…`, never data or values (`repaired` = days whose missing sections loaded in a repair call). With
`COACH_HISTORY_TIME` inside the briefing window there is also one
`history status=config_warning` line after startup. `healthcheck.sh` reports the job state (`on` / `off` /
`stopped`).

## Weather

`get_weather_forecast` returns a 7-day forecast from [Open-Meteo](https://open-meteo.com/). Per day it gives
sunrise and sunset, a summary (minimum and maximum temperature, feels-like temperature, highest precipitation
probability and wind) and hourly temperature, feels-like temperature, precipitation probability and wind (°C,
%, km/h, local times of the place).

For each day it adds the single best window for outdoor training: the longest continuous run of daylight hours
(at least 30 minutes of the hour between sunrise and sunset) that have not started yet, with a feels-like
temperature of −5 to 25 °C, precipitation probability at most 30 % and wind at most 25 km/h. On a tie, the
lower precipitation probability wins, then the earlier window.

- **Privacy:** only rounded coordinates (two decimals, about 1 km) and the server's IP address go to
  Open-Meteo, no health data. The service is free for non-commercial use and needs no key.
- **Setup:** add `COACH_HOME_LATITUDE` and `COACH_HOME_LONGITUDE` to `.env` (decimal degrees, e.g. 51.48 and
  0.00; both or neither), then `docker compose up -d coach-mcp`. Without them the tool returns only the error
  "No home location configured"; coordinates given as arguments (travel) always work. An invalid value stops
  `coach-mcp` from starting with a clear error.
- **Cache:** a response is kept in memory for 30 minutes (gone after a restart); errors are not cached.
- **Log** (`docker compose logs coach-mcp`): `weather status=fetched|error reason=…`, never with coordinates.
- **Calendar:** the Google Calendar connector is built into claude.ai and optional. Nothing in this repository
  handles it; the coach works with it according to [`docs/coach-instructions.md`](coach-instructions.md).

Weather data by [Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0 licence).

## Renewing Garmin tokens

The server refreshes its access token by itself and makes a cheap call about every 6 hours so that the refresh
token rotates too. A new login is needed when Garmin invalidates the tokens (long inactivity, a password
change, a change of Garmin's login flow). You will see it in `healthcheck.sh`, or the Garmin tools return an
error saying to run `scripts/garmin-login.sh`.

```bash
scripts/garmin-login.sh            # new login with MFA
```

When Garmin blocks logins from the server (HTTP 429), log in on your Mac and transfer only the token file. The
library needs Python ≥ 3.12 (the macOS system Python is 3.9), hence [uv](https://docs.astral.sh/uv/), which
downloads Python by itself. On the Mac, one line at a time (zsh treats `#` as a comment only after
`setopt interactivecomments`):

```bash
brew install uv                      # or see docs.astral.sh/uv for other installers
scp <vps>:fitness-coach-mcp/garmin-mcp/src/garmin_coach_mcp/login.py /tmp/garmin_login.py
uv run --python 3.13 --with garminconnect==0.3.17 python /tmp/garmin_login.py --tokens-dir ~/garmin-tokens
scp ~/garmin-tokens/garmin_tokens.json <vps>:/tmp/garmin_tokens.json
rm -rf ~/garmin-tokens /tmp/garmin_login.py
```

On the VPS:

```bash
scripts/garmin-login.sh --import /tmp/garmin_tokens.json && shred -u /tmp/garmin_tokens.json
```

Garmin often rejects the mobile login variants with 429; the library then tries the web widget by itself,
which usually works. When all variants return 429, wait at least an hour (further attempts extend the block)
and try once, ideally over a mobile hotspot.

When the Garmin login breaks for everyone (a change on Garmin's side), watch
[python-garminconnect](https://github.com/cyberjunky/python-garminconnect/issues). The fix usually means a
version bump in `garmin-mcp/pyproject.toml` → `scripts/lock-deps.sh` → rebuild → `garmin-login.sh`.

## Updates

Three things watch the pins; none of them deploys anything.

- **Dependabot** (`.github/dependabot.yml`) opens pull requests once a month: one for the GitHub Actions and one
  per base image. The `python:3.13-slim` digest of `gateway`, `garmin-mcp` and `coach-mcp` moves in one PR; a new
  Python minor is not proposed, because the locks are compiled for 3.13. CI runs on every such PR. A `tailscale`
  or `hevy-mcp` PR stays red in the `scripts` job (`scripts/check-pins.sh`) until you add a commit to it that
  sets the same version in that service's `image:` tag in `docker-compose.yml`. Merge a green PR, then deploy
  it with steps 3 to 5 below.
- **The audit** (`.github/workflows/audit.yml`) runs `pip-audit` on every `requirements*.lock` each Monday, on
  demand (`gh workflow run audit.yml`) and on pull requests that change a lock or a `pyproject.toml`. GitHub
  mails you when the scheduled run fails. A red run names the package and the fixed version: raise it (step 2)
  and run the audit again. GitHub turns scheduled workflows off after 60 days without a commit in a public
  repository; turn it back on in the Actions tab.
- **`scripts/check-updates.sh`** reports what Dependabot does not: newer Python packages, the uv image of
  `scripts/lock-deps.sh` and, with Docker, a moved digest of the base images.

The procedure by hand (Python packages, or a pin without a Dependabot PR):

1. `scripts/check-updates.sh`: what is new.
2. Change the pin (`tailscale/Dockerfile` or `hevy-mcp/Dockerfile` including the digest, with the same version in
   the `image:` tag in `docker-compose.yml`; or `pyproject.toml`). `scripts/check-pins.sh` checks the pins.
   For Python, run `scripts/lock-deps.sh` (optionally `--upgrade`). `ruff` and `mypy` are pinned to exact
   versions in the `test` / `dev` extra of each `pyproject.toml`, because a newer version can report new
   findings; `scripts/check-updates.sh` does not list them. The deploy needs a committed tree, so the change is
   committed (a PR) before the deploy.
3. `scripts/check.sh` (tests of all services + e2e: build and the whole OAuth/MCP flow without touching
   production).
4. `scripts/setup-secrets.sh` adds the secrets a new version is missing (e.g. `secrets/garmin_readonly_token`
   or an empty `secrets/telegram_bot_token`). Without them the service that reads them does not start. On a
   complete installation it changes nothing.
5. `scripts/deploy.sh` → `scripts/smoke-test.sh`.

The Garmin read-only token (`secrets/garmin_readonly_token`): when you move to the version that introduces it,
step 4 creates it and restarts nothing. It takes effect only in step 5. `garmin-mcp` and `coach-mcp` must be
deployed together (`scripts/deploy.sh` deploys every service together): an older `garmin-mcp` image
does not know the new token, and an older `coach-mcp` image looks for a token the new `docker-compose.yml` no
longer mounts for it. A second run of `setup-secrets.sh` changes nothing.

Digest for a new tag: `docker buildx imagetools inspect <image:tag>` (the `Digest` line).

### Versions and rollback

- `scripts/deploy.sh` deploys the commit the checkout is on: it refuses uncommitted changes and missing secret
  files, backs up (when `BACKUP_DIR` is set and the stack runs; `--no-backup` skips it), builds the three images
  of this repo tagged with `git describe` (the test stage runs inside the build; nothing is rebuilt when the
  images of this commit exist), writes `FCM_VERSION` and `FCM_PREVIOUS_VERSION` to `.env`, starts the stack and
  waits until every service is healthy. Every new commit recreates `gateway`, `garmin-mcp` and `coach-mcp` (a few
  seconds without connectors); `ts` and `hevy-mcp` restart only when their pin or settings change.
- It keeps the running version and two older ones of each image and removes older images it built itself; it
  never removes tags it did not create.
- `scripts/deploy.sh --status` compares what runs with what is configured (it replaces comparing image IDs by
  hand) and lists the kept versions; it changes nothing.
- `scripts/deploy.sh --rollback` switches to `FCM_PREVIOUS_VERSION` (or `--rollback <version>`) without a build;
  run it again to switch forward. It refuses when the images are gone, when `docker-compose.yml` or a file it
  mounts from the checkout changed between the two commits (then: a revert PR and `scripts/deploy.sh`), and when
  the coach database is newer than the old `coach-mcp` (then the down-migrations in `coach-mcp/README.md` first).
  Images not built by the script (the `0.1.0` ones) are reached only by naming them, and only while the
  `gateway/config.toml` inside the old gateway image equals the checkout's. Otherwise the old gateway would listen
  where the current `tailscale/serve.json` and the ingress rules do not expect it.
- A build by hand in the live checkout (`docker compose build`, or `up` with `--build`) overwrites the image of the
  configured version with the current tree; `--status` then reports `not built by scripts/deploy.sh`. Deploy with
  the script only.
- Do not deploy while a batch of agents builds on the same machine: `scripts/deploy.sh` does not take the build
  lock.

### First deploy with scripts/deploy.sh

Moving from the `0.1.0` images, once:

1. `git status --short` is empty. (`scripts/deploy.sh --status` does not exist in the old tree.)
2. `git pull --ff-only`.
3. Nothing in between: no `docker compose up|run|create|build`, no `scripts/setup-secrets.sh` (its gateway image now
   comes from Compose) and no `scripts/garmin-login.sh`. `scripts/deploy.sh --status` is allowed: it is read-only,
   exits 1 and shows `image missing` for the three images of this repo.
4. `scripts/deploy.sh`. It backs up (when `BACKUP_DIR` is set), then builds the three images; the connectors stay up
   during the build. It writes `FCM_VERSION=<commit>` and `FCM_PREVIOUS_VERSION=0.1.0`, starts the stack and
   recreates the services once.
5. `scripts/deploy.sh --status` exits 0; then `scripts/healthcheck.sh` and `scripts/smoke-test.sh`.
6. Only after that, if wanted, `docker image ls 'fitness-coach-mcp/*'` and remove the old tags with `docker rmi`.

- After `git pull` and before `scripts/deploy.sh`, `.env` has no `FCM_VERSION`: any command that creates a
  container would build the new code under the tag `local` and start it without a backup.
- On this first deploy `ts` and `hevy-mcp` may restart too: Compose recreates a container whose tag was rebuilt
  after it started (`--status` shows them as `DIFFERENT` before the deploy). Their images are not rebuilt.
- Do not roll back to `0.1.0` afterwards: from this version on, the gateway listens on another address, and
  `scripts/deploy.sh --rollback 0.1.0` refuses because the old gateway's `config.toml` differs. The way back is a
  revert PR and `scripts/deploy.sh`.

## Backups

The `coach-data` volume is the **only copy** of the coach's goals, constraints, plan and notes, and no
script can recreate it. The gateway database (grants and refresh tokens) and the Garmin tokens can be recreated
(reconnect the connectors, `scripts/garmin-login.sh`), but a backup saves that work. Not included:
`audit.jsonl` (a log, not state), `ts-state` (a new auth key recreates the Tailscale node) and `secrets/`
(`scripts/setup-secrets.sh`).

**How.** `scripts/backup.sh` runs while the stack is up.
- It makes consistent copies of both databases with SQLite's online backup API inside their containers and
  copies the Garmin token.
- Everything goes into one archive, `fitness-coach-backup-<UTC time>.tgz` (mode 0600), in `BACKUP_DIR`.
- It keeps the newest `BACKUP_KEEP` archives (default 14) and never deletes other files in the directory.
- Any error (a service not running, missing Garmin tokens, a corrupt database) means exit 1, no archive, and
  older archives stay unchanged. Each problem is printed on its own line.
- Leftovers of an interrupted backup (`.backup-tmp.*`) are removed by the next run.
- Rotation never deletes the archive it just created. If the directory has an archive with a newer time in
  its name (clock moved, a foreign archive), the script says so even with `--quiet`.
- The name of the newest archive is the time of the last successful backup **in UTC**. A run at 02:47 in Prague
  is named `…T004700Z` in summer and `…T014700Z` in winter.
- `scripts/healthcheck.sh` reports a problem when the newest archive is older than `BACKUP_MAX_AGE_HOURS` (default
  26), when `BACKUP_DIR` holds no archive or cannot be read, and when the newest name lies more than an hour in
  the future. Without `BACKUP_DIR` in `.env` it checks nothing. With `--notify` the problem goes to Telegram.

Setup: add `BACKUP_DIR=` (an absolute path outside the repository) and optionally `BACKUP_KEEP=` to `.env` with an
editor, run `scripts/backup.sh` once and add the cron line from [Routine tasks](#routine-tasks). The `backup`, `restore`,
`backup-db` and `restore-db` commands inside the containers exist only in images built from this version.
After an update, deploy first ([Updates](#updates), `scripts/deploy.sh`). Older images
answer `restore` with a usage line only (exit 2) and change nothing.

**Where to keep them.** The archives contain personal training data and Garmin login tokens: keep them out of
the repository and out of public storage. Copying them off this machine (encrypted) is up to you; the script
does not do it.

**Restore** one part at a time, always with that part's service stopped, one command at a time.
`docker compose run` starts a one-off container of the configured version with the service's volumes; for the
gateway, `ts` must be running (the gateway uses its network). First set the archive path (the name contains the
time in UTC; use your own archive):

```bash
ARCHIVE=/srv/backups/fitness-coach-mcp/fitness-coach-backup-20261009T004700Z.tgz
```

Coach memory:

```bash
docker compose stop coach-mcp
tar -xzOf "$ARCHIVE" coach.db | docker compose run --rm --no-deps -T coach-mcp restore
docker compose start coach-mcp
```

Gateway (grants):

```bash
docker compose stop gateway
tar -xzOf "$ARCHIVE" gateway.sqlite3 | docker compose run --rm --no-deps -T --entrypoint python gateway -m gateway.cli restore-db
docker compose start gateway
```

Garmin tokens:

```bash
docker compose stop garmin-mcp
tar -xzOf "$ARCHIVE" garmin_tokens.json | docker compose run --rm --no-deps -T --entrypoint python garmin-mcp -m garmin_coach_mcp.login --tokens-dir /data/garmin --import-file - --no-verify
docker compose start garmin-mcp
scripts/garmin-login.sh --check
```

A restore first verifies the file: SQLite integrity, the expected tables, and that the coach database schema is
not newer than the image.
- On any error, coach and gateway end with a line `… nothing changed`. The Garmin token import rejects an
  invalid file with `The file does not look like a garmin_tokens.json …`. In all cases the volume stays as it
  was.
- On success, the previous files in the volume stay as `<file>.before-restore-<UTC time>`. That includes any
  leftover `-journal` / `-wal`, which SQLite would otherwise replay into the restored file.
- `coach-mcp` migrates an older coach database archive at startup and refuses a newer one (deploy a newer
  image).
- The Garmin tokens are replaced without a side copy (they can be recreated with `scripts/garmin-login.sh`);
  `--check` shows an expired token from the archive.
- On a new host, first start the stack according to the [README](../README.md#installation) (`scripts/deploy.sh`)
  so that Compose creates the volumes, and only then restore.
- When a command ends with an error, do not continue with the next one; after `docker compose start` the
  service runs with its existing data.

**Going back after a restore** (for the gateway likewise with `-m gateway.cli restore-db
/data/<file>.before-restore-…`). The first command lists the side copies in the volume; restore from the one
with the time you want to go back to. The listing also shows `<file>.before-restore-<time>-journal` / `-wal` /
`-shm` files; `restore` gets the name **without** that suffix. Going back keeps the current database as another
side copy and consumes the `-journal` / `-wal` / `-shm` of the chosen copy, because SQLite uses them when
reading it; the copy itself stays.

```bash
docker compose run --rm --no-deps -T --entrypoint ls coach-mcp -l /data/coach
docker compose stop coach-mcp
docker compose run --rm --no-deps -T coach-mcp restore /data/coach/coach.db.before-restore-20261009T101500Z
docker compose start coach-mcp
```

Delete side copies you no longer need:

```bash
docker compose run --rm --no-deps -T --entrypoint sh coach-mcp -c 'rm -f /data/coach/coach.db.before-restore-*'
```

The same for the gateway:

```bash
docker compose run --rm --no-deps -T --entrypoint sh gateway -c 'rm -f /data/gateway.sqlite3.before-restore-*'
```

The pattern also deletes the `-journal` / `-wal` / `-shm` helper files and the copy you just went back from.

The daily history (`history_days`) is in the same file as `coach.db`. If it is lost, `coach-mcp` reloads the
last `COACH_HISTORY_BACKFILL_DAYS` days from Garmin after startup. The Hevy strength history (tables
`strength_workouts`, `hevy_templates`, `hevy_sync`) is likewise reloaded from Hevy after a loss.

## Operations alerts (Telegram)

`scripts/healthcheck.sh --notify` sends the healthcheck's problems to the chat of the morning briefing, through
the same bot. It sends a message only when the set of problems changes: when a problem appears or goes away, and
once when every check passes again. An unchanged set sends nothing, so the script can run from cron every 15
minutes ([Routine tasks](#routine-tasks)).
- A message lists the `✘` lines exactly as the healthcheck prints them, under `New:`, `Still:` and `Resolved:`.
  Warnings (`!` lines) are not sent.
- Among the problems: a container that is not running or not healthy, the morning briefing scheduler or the
  nightly history `stopped` in `coach-mcp`, invalid Garmin tokens, a rejected Hevy key, and the backup age
  ([Backups](#backups)): a failed nightly backup shows up a few hours later.
- A problem stays the same problem while its check keeps failing, even when its line changes (for example
  `running/starting` and then `running/unhealthy`).
- Setup: the bot and `COACH_TELEGRAM_CHAT_ID` from Morning briefing, steps 1 to 3. Nothing is restarted: the
  script runs on the host and reads `secrets/telegram_bot_token` itself, so the cron user must be able to read it.
- The token reaches `curl` on stdin, never on a command line. The chat id and the text travel in a temporary
  file that is deleted afterwards, and no output of the script contains the token or the chat id.
- When the chat id or the token is missing or malformed, or Telegram does not accept the message, the script
  still prints the problems, writes `Notify failed: …` and the current problems to stderr and exits 1. The next
  run sends the message again.
- What was sent last is kept in `~/.local/state/fitness-coach-mcp/healthcheck-notify.state` (under
  `$XDG_STATE_HOME` when it is set). The first run with `--notify` sends the problems that exist at that moment;
  delete the file to have the next run send the current problems again.

**Privacy:** the alerts go to Telegram's servers and stay in the chat history, like the briefing. They carry no
health values: service names and states, HTTP codes, the node's host name, the Funnel IP address and the age of
the newest backup.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| claude.ai: "Couldn't connect" right after adding | Funnel is not running or the funnel `nodeAttrs` for the tag is missing → `healthcheck.sh`. Public DNS: `curl -s -H 'accept: application/dns-json' 'https://cloudflare-dns.com/dns-query?name=fitness-mcp.<tailnet>&type=A'`. |
| The login page says "Access denied" | You came in over the internet, not the tailnet. Turn on Tailscale, turn off iCloud Private Relay / custom DNS-over-HTTPS in the browser (MagicDNS must work) and try again. |
| "Invalid password or code" | Check the time on your phone. Each TOTP code can be used only once. After 5 failures the login locks (1 min, then exponentially up to 1 h). |
| "Request expired" | The login page is valid for 10 min. Start connecting again from Claude. |
| A connector stops working after a while and asks for login | The refresh token expired or the grant was revoked (`scripts/admin.sh grants list --all` shows the reason, e.g. `refresh token reuse`). Reconnect from the tailnet. |
| MCP calls return 403 | The token came through Funnel from an IP outside `160.79.104.0/21`. For claude.ai this means Anthropic changed the range: check the [docs](https://platform.claude.com/docs/en/api/ip-addresses) and adjust `FUNNEL_TOKEN_CIDRS` in `.env`. |
| MCP calls return 502 `backend_misconfigured` | The gateway ↔ backend internal token does not match → `scripts/setup-secrets.sh backend-tokens`. |
| The gateway does not start after an update: `cannot read secret /run/secrets/coach_backend_token` | `scripts/setup-secrets.sh` (adds the missing internal token), then `scripts/deploy.sh`. |
| `coach-mcp` unhealthy, log says `database schema version N is newer` | An older image is running than the data: `scripts/deploy.sh --rollback` back to the newer version, or `scripts/deploy.sh`; do not delete the `coach-data` volume. |
| `scripts/deploy.sh` refuses: … | The line says why; nothing was changed. `--status` shows what runs. |
| `scripts/backup.sh` ends with `No backup written` | The lines above name the part: `is not running` (Compose message) = the service is not running (`docker compose ps`); `Garmin tokens missing` = `scripts/garmin-login.sh`; `database is larger than 32 MiB` / `stayed locked` = report it (the limit is raised in a PR). No archive was created and older ones stay. |
| `healthcheck.sh` says `backup: newest archive is … h old` or `no archive in BACKUP_DIR` | The nightly `scripts/backup.sh` failed or did not run: run it by hand and read its lines (row above), then check its cron line and `BACKUP_DIR` in `.env`. Weekly backups need a larger `BACKUP_MAX_AGE_HOURS` (for example 192). |
| `healthcheck.sh` says `tailnet: port 8080 of the node accepts connections that bypass tailscale serve` (or 9002) | Something in the `ts` namespace listens on `127.0.0.1` again (an edited `TS_LOCAL_ADDR_PORT` or a changed image), and the tailnet policy lets the host through on that port. Run `scripts/check-ingress.py`, restore `127.0.0.2`, recreate `ts` and `gateway` together (`docker compose up -d --build ts gateway`), and check the policy rule of README install step 1. |
| `healthcheck.sh --notify` ends with `Notify failed: COACH_TELEGRAM_CHAT_ID …` or `… secrets/telegram_bot_token …` | The chat id is missing from `.env`, or the token file is missing, unreadable for the cron user or not a bot token → Morning briefing, steps 2 and 3 (`scripts/setup-secrets.sh telegram-token`, `scripts/setup-secrets.sh telegram-chat-id`). The problems are still printed and the exit code is 1. |
| `Notify failed: Telegram did not accept the message (HTTP …)` | Telegram rejected the message (token, chat id, bot blocked or nobody has written to it yet) or did not respond (`HTTP 000`). Nothing is lost: the next run sends it again. |
| `Notify failed: another healthcheck.sh --notify run is in progress` | Two runs overlapped (for example a manual run during the cron run); the other run sends the message. |
| `Notify failed: the message could not be composed` | The state file `~/.local/state/fitness-coach-mcp/healthcheck-notify.state` cannot be read (it is a directory or the file system fails). Fix or delete it; the next run sends the current problems. |
| `Notify failed: cannot create a temporary directory` | `/tmp` (or `$TMPDIR` of the cron user) is full or not writable. The problems are still on stderr; the next run tries again. |
| `coach-mcp` does not start: `bind source path does not exist: …/secrets/telegram_bot_token` | The bot token file is missing (an empty one is enough) → `scripts/setup-secrets.sh` and `docker compose up -d coach-mcp`. |
| `garmin-mcp` or `coach-mcp` does not start: `bind source path does not exist: …/secrets/garmin_readonly_token` | The coach's read-only token is missing → `scripts/setup-secrets.sh` and `scripts/deploy.sh`. |
| The briefing and history have no Garmin data, the `garmin-mcp` log says `read-only token file missing or too short` | `secrets/garmin_readonly_token` is empty → `scripts/setup-secrets.sh` fills it; running services pick up the new token by themselves. |
| `coach-mcp` keeps restarting, log says `COACH_TELEGRAM_CHAT_ID is set but … does not hold a Telegram bot token` | `.env` has a chat id but the token is missing or corrupt → `scripts/setup-secrets.sh telegram-token`, or delete the chat id from `.env` (briefing off). |
| `get_trends` / `get_weekly_report` have no data, `get_correlations` returns `too_few_pairs` | The history is still loading (first run ~30 min, outside the briefing window) or Garmin is not logged in: `docker compose logs coach-mcp`, lines `history status=…` (`rate_limited` = Garmin limited the calls, next attempt in 1 to 8 h; `auth` = `scripts/garmin-login.sh`; `service_error` = Garmin Connect or the internet did not work, next attempt in 1 to 8 h, the days do not count; `given_up` > 0 = days that failed three times although Garmin returned other days). |
| `get_goal_progress` shows `available: false` or a goal without a projection | `reason` says why. `available: false`: `no_recent_value` / `too_few_values` = no value in the last 14 days or fewer than 14 values over 4 weeks (the race predictions need a night of history first; `docker compose logs coach-mcp`, lines `part=predictions`). `available: true` without a projection: `no_target_date`, `target_date_passed`, `target_date_too_far` (needs a target date within 26 weeks), `trend_too_steep`; `no_target_value` = projection but no verdict (give `set_goal` a `target_value`). A goal without `metric` is not listed (give it one with `set_goal`). |
| `get_plan` has no `suggestion` on a past session | The session is not `planned`, is of type `mobility` / `other`, the day has not loaded yet (first the day after the session at 03:30), it is more than 7 days older than the deployment, or the history is off (`COACH_HISTORY_ENABLED=false`); see the `history status=… matched=… no_activity=…` line in `docker compose logs coach-mcp`. |
| `get_strength_progress` or the strength part of `get_weekly_report` has no data | The strength history is still loading or `hevy-mcp` does not work: `docker compose logs coach-mcp`, lines `history status=… part=strength` (`no_credentials` = the `coach-mcp` container has no `hevy_backend_token`, after an update run `docker compose up -d coach-mcp`; `unreachable` = `hevy-mcp` does not respond; `auth` = invalid Hevy key, `scripts/setup-secrets.sh hevy-key`; `rate_limited` = Hevy limited the calls, next attempt in 1 to 8 h; `service_error` = Hevy returned an error or an unexpected page, next attempt in 1 to 8 h). |
| `get_weather_forecast`: "No home location configured" | `COACH_HOME_LATITUDE` and `COACH_HOME_LONGITUDE` are missing from `.env` → add both and `docker compose up -d coach-mcp`; or pass coordinates as arguments. |
| `get_weather_forecast`: "Weather service unreachable / timed out / answered HTTP …" | Open-Meteo or the internet does not work. Errors are not cached, so the next call tries again; `docker compose logs coach-mcp`, lines `weather status=error`. |
| `coach-mcp` does not start, log says `COACH_HOME_… must be set together / must be a number / must be between` | Fix or delete both `COACH_HOME_LATITUDE` and `COACH_HOME_LONGITUDE` in `.env`. |
| `coach-mcp` does not start, log says `COACH_LANGUAGE must be one of en, cs` | Fix or delete `COACH_LANGUAGE` in `.env`. |
| The bot does not answer in Telegram | `docker compose logs coach-mcp`, lines `inbound …`: `poll=unauthorized` = the token is wrong or revoked → `scripts/setup-secrets.sh telegram-token`; `poll=conflict` = a webhook or another program reads the bot's updates; `status=off reason=group_chat` = the chat id is a group; no `inbound` line at all = `COACH_TELEGRAM_INBOUND=false` or no chat id; `status=ignored` = the message came from another chat or account than `COACH_TELEGRAM_CHAT_ID`; `reply=failed` = the message was handled but the answer could not be sent. |
| `scripts/setup-secrets.sh telegram-chat-id` finds no chat although you wrote to the bot | Two-way Telegram already fetched the message: `docker compose stop coach-mcp`, write to the bot, run the command again, then `docker compose start coach-mcp`. |
| The morning notification did not arrive | `docker compose logs coach-mcp`: `skipped` = the service started after the cutoff; `gave_up` = Telegram rejected the message (token, chat id, bot blocked or nobody has written to it yet) or did not respond; no `briefing` line = the briefing is off (`healthcheck.sh` says so). |
| Hevy tools return 401 | Invalid Hevy key or Hevy PRO expired → `scripts/setup-secrets.sh hevy-key`. |
| Garmin tools say to run scripts/garmin-login.sh | Tokens are missing or expired → `scripts/garmin-login.sh`. |
| Garmin login: 429 / 403 from Cloudflare | Blocked from a datacenter IP → log in on your Mac and import the tokens ([Renewing Garmin tokens](#renewing-garmin-tokens)). |
| Garmin data: 403 on every call | Try `GARMIN_API_SESSION=curl_cffi` on the garmin-mcp service (python-garminconnect #444 workaround). |
| The node does not log in | The auth key is used up / expired or has no tag → new key, `scripts/setup-secrets.sh ts-authkey`, `docker compose up -d --force-recreate ts gateway`. |
| The gateway does not work after restarting `ts` | The gateway shares its network namespace: `docker compose up -d --force-recreate gateway`. |
| `permission denied ... docker.sock` | The user is not in the docker group in the current session: log in again or `sg docker -c "<command>"`. |
