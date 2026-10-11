# garmin-mcp (garmin-coach-mcp)

A thin MCP server on top of [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) 0.3.17
with ~23 selected tools for the coach. It runs as Streamable HTTP (stateless, JSON) on `:8000/mcp`
in a private Docker network; it is reachable only through the gateway, which sends an internal bearer token
(`/run/secrets/garmin_backend_token`). Without the token the server rejects all MCP requests (fail closed).
A second, read-only token (`/run/secrets/garmin_readonly_token`) belongs to `coach-mcp`. With it the server answers
`tools/list` and every read tool, but a tool not marked as read-only (`readOnlyHint`) is rejected with
an error before it runs; a tool without the marking counts as a write tool. When the file is missing or empty,
the server accepts only the gateway token and logs this at startup. The tokens must differ: the same value in both
files counts as read-only.

Why a custom server: `AbhimanyuV04/garmin-mcp` has no license and no MFA, and uses a login that Garmin
broke in March 2026; `Taxuspt/garmin_mcp` has 153 tools and is stuck on garminconnect 0.3.2
(without the MFA and token-refresh fixes).

## Tools

| Read (readOnly) | Write |
|---|---|
| `get_recovery_snapshot` – readiness, HRV, sleep, Body Battery, resting HR, stress, training status in one call | `create_workout` – structured run/ride (+ optionally schedule it) |
| `get_training_readiness`, `get_hrv_trend`, `get_sleep`, `get_body_battery`, `get_daily_summary`, `get_training_status` | `schedule_workout` – on a date (idempotent) |
| `list_activities`, `get_activity` (laps, HR zones, weather; for strength activities the sets recorded on the watch: exercise, reps, kg) | `unschedule_workout` – remove from the calendar (destructive) |
| `get_performance_metrics` – race predictions (time + pace/km), lactate threshold (HR, pace, power), heart-rate zones per sport, cycling power zones, endurance and hill score, weekly VO2max trend | |
| `get_race_predictions` – Garmin's daily race predictions for a period (1–366 days, default 28): seconds per distance (5 km, 10 km, half marathon, marathon); days without a prediction are left out | |
| `get_running_tolerance` – Garmin's weekly running load vs. tolerance (1–26 weeks, default 8; Garmin's own field names) | |
| `get_device_status` – when Garmin last received data: the device that uploaded last and the upload time (UTC and local), the primary activity tracker (name, status) and the number of registered devices | |
| `get_personal_records` (personal records), `get_gear` (shoes, bikes: distance covered, activity count, limit, retired) | `log_weight` – manual weigh-in in kg (optional date, not in the future) for athletes without a Garmin scale; not idempotent |
| `get_body_composition` – weigh-ins for a period (max 366 days, default 30): weight, BMI, body fat %, muscle mass, body water %, bone mass + summary (first/last, change, 7-day average) | |
| `get_nutrition` – one day: calories vs. goal, protein/carbs/fat vs. goals, meals, hydration vs. goal; whatever Garmin does not have returns `available: false` | |
| `list_workouts`, `get_workout`, `list_scheduled_workouts` | *(no deleting workouts or editing activities)* |

Step targets: HR zone 1–5, heart-rate range, pace `M:SS`/km (running), power in W (cycling), cadence.

## Login and tokens

Tokens: `$GARMINTOKENS/garmin_tokens.json` (volume `garmin-tokens`, directory 0700, file 0600).
The password is never stored. The library renews the access token itself (the refresh token rotates and is written
back); in addition, the server makes one cheap call every ~6 h so the refresh token does not expire
(`GARMIN_KEEPALIVE_HOURS`).

```bash
# first login / new login (e-mail, password, MFA code) – from the repo root
docker compose run --rm -it --no-deps --entrypoint python garmin-mcp -m garmin_coach_mcp.login
# validity check (exit 0 = OK)
docker compose run --rm --no-deps --entrypoint python garmin-mcp -m garmin_coach_mcp.login --check
```

(`scripts/garmin-login.sh` in the repo root wraps these commands.) The running server picks up a new token file
on its own at the next call. `GET /healthz` reports `garmin_auth: ok | missing | error` (no network
call; `error` = Garmin rejected the last session → log in again).

### Fallback login (when Garmin/Cloudflare blocks the server's IP)

On a Mac (home network). The library needs Python ≥ 3.12, hence `uv`; paste the commands one at a time:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
scp <vps>:dev/fitness-coach-mcp/garmin-mcp/src/garmin_coach_mcp/login.py /tmp/garmin_login.py
uv run --python 3.13 --with garminconnect==0.3.17 python /tmp/garmin_login.py --tokens-dir ~/garmin-tokens
scp ~/garmin-tokens/garmin_tokens.json <vps>:/tmp/garmin_tokens.json
rm -rf ~/garmin-tokens /tmp/garmin_login.py
```

On the VPS (the import is written with mode 0600 and verified):

```bash
scripts/garmin-login.sh --import /tmp/garmin_tokens.json && shred -u /tmp/garmin_tokens.json
```

## Configuration (env)

| Variable | Default | Meaning |
|---|---|---|
| `GARMINTOKENS` | `/data/garmin` | token directory |
| `GARMIN_MCP_BEARER_TOKEN_FILE` | `/run/secrets/garmin_backend_token` | internal token from the gateway (min. 16 characters) |
| `GARMIN_MCP_READONLY_TOKEN_FILE` | `/run/secrets/garmin_readonly_token` | read-only token for `coach-mcp` (min. 16 characters; without it read-only access is disabled) |
| `TZ` | `Europe/Prague` | what "today" means; in compose it is set via `ATHLETE_TZ` in `.env` |
| `GARMIN_KEEPALIVE_HOURS` | `6` | keepalive interval (0 = off) |
| `GARMIN_API_SESSION` | `requests` | `curl_cffi` = workaround for python-garminconnect #444 (HTTP 403 on data calls) |
| `GARMIN_MCP_ALLOWED_HOSTS` | *(empty)* | enables the Host header check (e.g. `garmin-mcp:8000`) |
| `GARMIN_MCP_LOG_LEVEL` | `INFO` | logs: only the tool name, result and duration – never data or tokens |

## Development

```bash
docker build --target test garmin-mcp
```

The test stage runs `ruff check src tests`, `mypy` (strict mode, `src/`; both configured in `pyproject.toml`)
and the tests (no network, fake Garmin client) on every build.
Dependency locks (`requirements.lock`, `requirements-dev.lock`) are regenerated only through
`scripts/lock-deps.sh`.

Known uncertainty: Garmin's nutrition endpoints (`nutrition-service`) have not been verified on a real account; `get_nutrition`
looks for values under several key names and returns `available: false` for an unknown response shape (hydration and weigh-ins
use verified endpoints). Garmin is the primary source for weight; the same weigh-in is not also written to Hevy.

Known uncertainty: the key names of the exercise sets and the activity weather were captured on a real account (values
were not). Set weights are read as grams and the weather as °F and mph, then converted to kg, °C and km/h; this is not
verified yet. On the account it was tested with, `get_running_tolerance` returns no weeks (`available: false`), so its entries are
untested on real data: each week passes Garmin's own field names through (snake_case, identifiers dropped). On a Garmin
server error the library logs the API path, which contains the activity id (as for the other activity calls), and can
include Garmin's error message; no health values are logged.

Garmin's daily race-prediction answer (`get_race_predictions`) was captured on a real account on 2026-10-11 (types
only): one entry per day, keyed `calendarDate`, `time5K`, `time10K`, `timeHalfMarathon` and `timeMarathon` in
integer seconds, plus `fromCalendarDate`, `toCalendarDate` and `userId`, which are ignored. The tool returns
`available: false` or an `error` section for any other shape. On a Garmin server error the library logs the
API path, which contains the Garmin display name, as for the other calls.

The device payloads (`mylastused` and the device list) were captured on a real account on 2026-10-11 (types only):
the upload time `lastUsedDeviceUploadTime` is an integer in epoch milliseconds, UTC; the device list has no upload
time per device, so the tool reports the account's last upload and names the device it came from. Identifiers and
serial numbers are not returned.

Known uncertainty: a custom cycling power range is sent as `power.zone` + `targetValueOne/Two`
(the convention of python-garminconnect's typed models); after creating the first such workout, check in Garmin
Connect that the range is displayed correctly.
