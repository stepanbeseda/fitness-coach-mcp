# fitness-coach-mcp

[![CI](https://github.com/stepanbeseda/fitness-coach-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/stepanbeseda/fitness-coach-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)

A self-hosted AI fitness coach for Claude. Three [MCP](https://modelcontextprotocol.io) servers give Claude
access to your real training data and a memory that survives between conversations. They run behind one
OAuth 2.1 gateway on your own server:

- **Garmin Connect** (`garmin-mcp/`, custom server on
  [`python-garminconnect`](https://github.com/cyberjunky/python-garminconnect) 0.3.17). It reads HRV, sleep,
  Body Battery, training readiness, activities (with the weather and the sets of strength sessions recorded on
  the watch), performance metrics (race predictions and their daily history, lactate threshold, heart-rate and
  power zones, VO2max), running tolerance, personal records, gear, body composition, nutrition and hydration. It
  can log a manual weigh-in and create structured run, ride and strength workouts scheduled to the watch.
- **Hevy** (`hevy-mcp/`, upstream [`chrisdoc/hevy-mcp`](https://github.com/chrisdoc/hevy-mcp) 6.1.16). It covers
  strength-training history, routines, exercise templates and writing routines and workouts.
- **Coach memory** (`coach-mcp/`, custom server on SQLite). It stores goals, constraints (injuries, available
  days, equipment), a dated training plan with status, and a notes log. On top of that it computes:
  - a deterministic morning briefing (green / yellow / red), optionally pushed to Telegram;
  - answers to what you write to the Telegram bot: today's briefing, this week's plan, done / skipped, notes;
  - goal progress (race predictions, VO2max) and a Monday weekly report in Telegram;
  - nightly Garmin and Hevy history for trends beyond 28 days, a weekly report, strength progress (estimated
    1RM) and correlations;
  - plan reconciliation against recorded activities;
  - a 7-day weather forecast with the best outdoor training windows.

It is built for one athlete (the owner) who does both strength and endurance training. All three servers are
reachable only through a dedicated Tailscale node. A typical conversation in a claude.ai Project: *"How am I
today, and what should I train this week?"* The coach reads recovery, load, the plan and its notes, proposes
the week and writes it to the plan, Garmin and Hevy only after you confirm.

> **Status:** a personal project, published as a reference implementation and portfolio piece. It is not a
> hosted service and not medical advice. The Garmin integration uses an unofficial library with your own
> credentials, which is fine for personal use but not for running a service for others. It is not affiliated
> with Garmin, Hevy, Tailscale or Anthropic.

**Contents:** [Demo](#demo) · [Design notes](#design-notes) · [Architecture](#architecture) ·
[Security model](#security-model) · [Installation](#installation) · [Funnel](#funnel) ·
[Adding the connectors](#adding-the-connectors) · [Operations and troubleshooting](docs/operations.md) ·
[Development](#development) · [Repository layout](#repository-layout)

## Demo

One morning with the coach, start to finish. The numbers are synthetic; the tool names, the rules the coach
follows and the briefing text are the real ones. The briefing below was rendered by `coach-mcp`'s own
`build_briefing` from synthetic Garmin data.

**06:30 — the morning briefing arrives in Telegram** (computed by rules, no LLM; `COACH_LANGUAGE=en`, Czech
with `cs`):

```text
🟡 ⚠️ YELLOW · morning briefing 12 Oct

Verdict: yellow – cut volume by 20–30 %, intensity at most RPE 7–8, cardio in zone 2.
Readiness: 58 (yellow)
HRV: 49 ms, baseline 52–68 ms, below baseline 2 nights in a row (yellow)
Sleep: 6 h 24 min, score 71 (yellow)
Body Battery at wake: 46 (yellow)
For full load: readiness at least 70; HRV back to at least 52 ms; sleep at least 7 h; Body Battery at wake at least 50.
Today's plan:
- strength: Lower body A: back squat 4×5, RDL 3×8, split squat 3×10 (60 min)
Alerts:
- Resting HR 54 bpm, 5 above the 7-day average of 49 bpm.
```

**07:10 — in the claude.ai "Fitness coach" project:**

> **Athlete:** Got a yellow this morning. What should I do today, and can you plan the rest of the week?

<details>
<summary>Tool calls the coach makes before answering (all read-only)</summary>

| # | Tool | What it returns here |
|---|---|---|
| 1 | Coach `get_coach_context` | Goal: half marathon under 1:40 on 22 Nov; constraint: left knee, no deep lunges; this week's plan; yesterday's long run in `to_confirm` with suggestion `matched` |
| 2 | Coach `get_daily_briefing` | The verdict above with its numbers |
| 3 | Garmin `get_recovery_snapshot` | Fresh readiness, HRV, sleep, Body Battery, resting HR, training status |
| 4 | Garmin `get_hrv_trend` (14 days) | Two nights below the 52 ms baseline after a stable week around 60 ms |
| 5 | Garmin `list_activities` (7 days) | Sunday 18 km long run, acute load 1.1× chronic (optimal) |
| 6 | Garmin `get_performance_metrics` | Lactate threshold 4:35 /km at 168 bpm, half-marathon prediction 1:39:50, HR zones |
| 7 | Hevy `get-workouts`, `get-exercise-history` | Last squat session 4×5 at 100 kg, RPE 8 |
| 8 | Coach `get_weather_forecast` | Thursday 75 % rain all day; Friday best window 07:00–10:00, 9 °C |

</details>

> **Coach:** Where you are this morning:
> - Readiness **58** (yellow), HRV **49 ms**, under your 52–68 ms baseline for the **2nd night**.
> - Sleep **6 h 24 min**, Body Battery **46** at wake, resting HR **+5 bpm** over your 7-day average.
> - Load is fine: acute/chronic 1.1, so this looks like poor recovery (short sleep), not overreaching.
>
> **Today:** keep Lower body A but cut it by about 25 %: back squat **3×5 at 90 kg** (RPE 7, not 4×5 at
> 100 kg), RDL 2×8. Dropping split squats entirely: your knee constraint says no deep lunges, and today is not
> the day to test it. Full load again once readiness is ≥ 70 and HRV is back above 52 ms.
>
> **The rest of the week** (the threshold run moves from Thursday to Friday because of the rain):
>
> | Day | Session | Target and why |
> |---|---|---|
> | Tue | Easy run 45 min | Zone 2, HR ≤ 148 (your `running` zones) |
> | Wed | Upper body A | Normal load if tomorrow's verdict is green |
> | Thu | Mobility 30 min, indoors | 75 % rain all day; the indoor swap keeps intervals off a wet day |
> | Fri | Threshold 3×10 min, 3 min jog rest | 4:35–4:40 /km, 166–170 bpm, from your lactate threshold; 07:00–10:00 window |
> | Sat | Upper body B, light | No heavy legs the day before the long run |
> | Sun | Long run 16 km | Zone 2; down from 18 km while HRV recovers |
>
> Also, Sunday's 18 km long run matches a Garmin activity: shall I mark it **done**?
>
> Should I save this? That would: save the week to the plan, mark Sunday done, update today's routine in Hevy,
> and send Friday's threshold run to your watch.

> **Athlete:** Yes, save it all.

<details>
<summary>Writes, only after that confirmation</summary>

| Tool | Change |
|---|---|
| Coach `save_plan` | This week's sessions from today on (the replaced ones are kept as `superseded`) |
| Coach `update_session` | Sunday's long run → `done` |
| Hevy `search-exercise-templates`, `update-routine` | "Lower body A": squat 3×5 at 90 kg, RDL 2×8, split squat removed |
| Garmin `create_workout`, `schedule_workout` | "Threshold 3×10" with pace and HR targets, scheduled for Friday |
| Garmin `find_strength_exercise`, `create_workout` | "Lower body A" from Hevy sent to the watch: squat 3×5 at 90 kg, RDL 2×8, one exercise Garmin does not know sent by its title |
| Coach `add_note` | "Yellow day 12 Oct (short sleep, HRV 2 nights below baseline): lower body cut 25 %, threshold moved to Fri for rain." |

</details>

> **Coach:** Done. Sync your watch to get Friday's run. I'll check tomorrow's verdict before Wednesday's upper
> body session.

## Design notes

The parts most relevant to building LLM agents that act on real, personal data:

- **Tools designed for a model, not a REST mirror.** 25 curated Garmin tools return compact,
  coach-oriented JSON instead of raw API payloads. A section Garmin cannot provide comes back as `"error"` or
  `"available": false` instead of failing the whole call. Tool descriptions and the servers' MCP
  `instructions` tell the model which tool to call first and when (for example, read performance metrics
  before prescribing paces).
- **Deterministic where it matters.** The morning verdict is a pure function with fixed thresholds, not an LLM
  judgement. The push notification and the `get_daily_briefing` tool call the same code, so they cannot
  disagree. A missing input never makes the verdict greener: it caps it at yellow and says what is missing.
- **Memory outside the model.** Goals, constraints, the plan and notes live in SQLite with FTS5 search. The
  coach is told to search its memory before asking the athlete again. Nothing is hard-deleted: goals end as
  achieved or abandoned, constraints as resolved, replaced sessions as superseded.
- **Human confirmation for every write (instructed, plus claude.ai tool permissions).** Every tool that changes
  Garmin, Hevy or coach state says so in its MCP annotations and description. The
  [coach instructions](docs/coach-instructions.md) require showing the proposal and getting explicit
  confirmation first, and claude.ai by default asks before it runs a tool that is not marked read-only. No server
  enforces the confirmation; what limits a wrong write is described in the Security model below (LLM / prompt
  injection). Plan reconciliation only *suggests* a session status from recorded activities, and the athlete
  confirms it.
- **Least privilege between components.** The coach server reads Garmin with a separate read-only token, so
  `garmin-mcp` rejects its write tools before they run. It may call only two read tools on Hevy. The client's
  OAuth token is never forwarded to a backend.
- **Honest statistics.** `get_correlations` returns Pearson and Spearman with the number of paired days, refuses
  below 14 pairs, and carries a "correlation is not a cause" caveat that the instructions require the coach
  to repeat.
- **Tested offline.** More than 1,200 test functions run against fakes and canned payloads, with no network. An
  end-to-end test runs the full OAuth + MCP flow in a throwaway Compose project (`scripts/check.sh`).
- **Statically checked.** Every image build lints the three Python services with `ruff` and type-checks their
  `src/` with `mypy` in strict mode before the tests run; a finding fails the build.
- **Plan-driven, AI-assisted development.** The project was built in small reviewed PRs from a written plan,
  largely with Claude Code agents working to a review checklist and a shared lessons file. See
  [How this was built](docs/how-this-was-built.md).

## Architecture

![Architecture: Claude clients reach a Tailscale node and the OAuth gateway, which routes to the hevy, coach and garmin MCP backends; coach-mcp runs background jobs and reads garmin-mcp and hevy-mcp read-only; a host cron runs the healthcheck with Telegram alerts and the backup](docs/architecture.svg)

Editable source: [`docs/architecture.html`](docs/architecture.html).

The `ts` sidecar runs `tailscale/tailscale` v1.102.5 in userspace mode as the node `fitness-mcp` (tag
`tag:fitness-mcp`). In userspace mode tailscaled forwards a tailnet connection to any port without a
`tailscale serve` handler to `127.0.0.1` on the same port, so nothing in the sidecar's namespace listens there:
the gateway listens on `127.0.0.2:8080` and tailscaled's health endpoint on `127.0.0.2:9002`. `tailscale serve`
on :443 is the only way to the gateway. The backends make only the outbound calls shown.

Connector URLs (the same from claude.ai and from the tailnet; inside the tailnet MagicDNS returns the 100.x
address). `<tailnet>` is your tailnet's DNS suffix: `tailscale status --json | jq -r .MagicDNSSuffix`, e.g.
`tail1234ab.ts.net`.

- `https://fitness-mcp.<tailnet>/hevy/mcp`
- `https://fitness-mcp.<tailnet>/garmin/mcp`
- `https://fitness-mcp.<tailnet>/coach/mcp`

Why a dedicated node rather than Funnel on the host: claude.ai calls connectors only on port 443, and Funnel
is enabled for a whole port. A dedicated node leaves whatever the host already serves on :443 (for example,
tailnet-only) untouched, and the gateway does not have to publish any port on the host.

## Security model

| Layer | What it does |
|---|---|
| Network | No published port on the host. The gateway listens on 127.0.0.2 in the sidecar's namespace, not on 127.0.0.1, where tailscaled in userspace mode forwards tailnet connections for ports without a serve handler; `tailscale serve` on :443 is the only way in, the tailnet policy allows only tcp:443 to the node (install step 1), and `healthcheck.sh` checks that ports 8080 and 9002 of the node accept no tailnet connection. Backends are only on a private Docker network. They call out only to their own APIs (Hevy, Garmin Connect). `coach-mcp` optionally calls the Telegram Bot API (`api.telegram.org`) for the morning briefing and to fetch what you write to the bot (long polling, an outbound call too; see [Morning briefing](docs/operations.md#morning-briefing-telegram) and [Two-way Telegram](docs/operations.md#two-way-telegram)) and Open-Meteo (`api.open-meteo.com`, no key) for the weather forecast, which receives only rounded coordinates (see [Weather](docs/operations.md#weather)). Other host services and the host's own `tailscale serve` are not changed. |
| Funnel | Only discovery (`/.well-known/*`), the 401 response and `/token` are public. Anything carrying a token, and `/token` itself, passes through Funnel only from `160.79.104.0/21` (Anthropic's published outbound range for MCP connectors). |
| Login | `/authorize` (argon2id password + TOTP) only from your own tailnet devices (`Tailscale-User-Login` = `OWNER_TAILSCALE_LOGIN`, never through Funnel). Lockout with exponential backoff, TOTP code reuse protection, CSRF cookie. |
| OAuth | MCP auth spec 2025-11-25: PKCE S256 required. `resource` (RFC 8707) makes each token valid for one endpoint only (a Hevy token does not work on Garmin). `iss` (RFC 9207). Access token 1 h, refresh token 90 days, rotation with reuse detection (with a 120 s retry window). Revocation (RFC 7009). |
| Clients | No open registration (DCR). Allowed clients are listed in `gateway/config.toml`: Claude (claude.ai, CIMD), Claude Code (CIMD) and `mcp-remote`. CIMD documents are pinned (no runtime fetch, no SSRF), and `healthcheck.sh` watches for changes. |
| Backends | The client token is never forwarded. The gateway sends its own internal token per backend (`secrets/*_backend_token`). `coach-mcp` reads `garmin-mcp` with its own read-only token (`secrets/garmin_readonly_token`). `garmin-mcp` rejects any tool not marked as read-only (`create_workout`, `schedule_workout`, `unschedule_workout`, `log_weight`) before it runs. The Hevy tool `feedback` (sends a report to its author) is blocked. `coach-mcp` reads `hevy-mcp` directly with its internal token and may call only two read tools (`get-workout-events`, `get-exercise-template`). The `hevy-mcp` token has full rights, so only this allowlist in code keeps the coach from writing to Hevy. |
| Tokens | Opaque 256-bit. SQLite stores only their SHA-256 hash. |
| Containers | Non-root (uid 10001), `read_only`, `cap_drop: ALL`, `no-new-privileges`, memory and process limits, healthchecks, log rotation. Images pinned by `tag@sha256`, Python dependencies hash-locked. |
| Secrets | `secrets/` (0700, files 0640 with group 10001), Docker secrets. Never in git, env or logs. |
| Audit | `/data/audit.jsonl` + stdout: logins, token issue/refresh/revocation, every MCP call (method, tool, outcome, duration). Never arguments, results, tokens or passwords. |
| LLM / prompt injection | Tool results carry text that third parties can write: Garmin activity and workout names and descriptions, Hevy titles and notes, and calendar events and invites when Google Calendar is enabled. The [coach instructions](docs/coach-instructions.md) tell the model to treat that text as data, never as instructions, and never to add attendees to calendar events; that is a prompt rule, not enforcement. What limits the damage is in code: `coach-mcp` reads Garmin with the read-only token and may call only two read tools on Hevy, and the write tools of this stack change only your own Garmin, Hevy and coach data. Keep claude.ai asking before each write tool runs; do not let write tools run without approval. What remains: with Google Calendar or web tools enabled in the claude.ai Project, an injected instruction could send data out, for example as an event invitation to an outside address. Telegram messages are not third-party text: `coach-mcp` accepts only your own chat and sender id. |

Why the gateway is a custom OAuth server, its threat model, a conformance matrix that names the test behind each
standard, its non-goals and its known limitations: [Gateway design note](docs/gateway-design.md).

## Installation

Prerequisites:

- A Linux server with Docker and Compose v2, joined to your tailnet. Tested on Ubuntu 24.04, linux/arm64
  (Oracle Cloud Ampere); all base images are multi-arch.
- A Tailscale account with MagicDNS and HTTPS certificates enabled (admin console → DNS).
- A Claude plan with custom connectors, Hevy PRO (for the API key), a Garmin Connect account.
- Tailscale running on the Mac/PC you connect the connectors from.

### 0. Clone

```bash
git clone <url-of-this-repo> fitness-coach-mcp && cd fitness-coach-mcp
```

All further commands run from the repository root.

### 1. Tailscale policy (admin console → Access controls)

Add a tag for the new node and allow Funnel for it. Tagged nodes are not part of `autogroup:member`, hence
the explicit `nodeAttrs`:

```jsonc
"tagOwners": {
  "tag:fitness-mcp": ["autogroup:admin"]
},
"nodeAttrs": [
  // ...keep your existing entries...
  { "target": ["tag:fitness-mcp"], "attr": ["funnel"] }
]
```

Allow your devices to reach the node on tcp:443 only. This is required, not optional:

```jsonc
"grants": [
  // ...your other grants; none of them may cover tag:fitness-mcp...
  { "src": ["autogroup:member"], "dst": ["tag:fitness-mcp"], "ip": ["tcp:443"] }
]
```

The sidecar runs tailscaled in userspace mode, which forwards a tailnet connection to any port without a
`tailscale serve` handler to `127.0.0.1` inside the container. The stack keeps nothing listening there (the gateway
is on `127.0.0.2`, see [Security model](#security-model)); the rule above keeps every other port of the node closed
to the tailnet as well. A rule whose `dst` includes `*` also covers tagged nodes: narrow its `dst` so that it no
longer includes `tag:fitness-mcp`, and preview the change in the admin console.

### 2. Auth key

Admin console → Settings → Keys → **Generate auth key**: Reusable **no**, Ephemeral **no**, Pre-approved
**yes**, Tags **`tag:fitness-mcp`**. The key is needed only for the node's first login (key expiry is disabled
for tagged nodes; the `ts-state` volume keeps the state).

### 3. Configuration

```bash
cp .env.example .env && chmod 600 .env
```

In `.env`, fill in `PUBLIC_BASE_URL` (`https://fitness-mcp.<tailnet>`) and `OWNER_TAILSCALE_LOGIN` (your
Tailscale login, e.g. `jane@github`). The commands that print both values are in `.env.example`. If you do
not live in the `Europe/Prague` time zone, set `ATHLETE_TZ` (e.g. `Europe/London`). It decides what "today"
is, and when the morning briefing and the nightly history run. `COACH_LANGUAGE` picks the language of the
morning briefing (`en` by default, or `cs`).

Optional settings:
- Morning briefing to Telegram: `COACH_TELEGRAM_CHAT_ID` and `COACH_BRIEFING_*` (see
  [Morning briefing](docs/operations.md#morning-briefing-telegram)).
- Nightly Garmin history is on by default; its `COACH_HISTORY_*` options are in `.env.example` (see
  [Nightly history](docs/operations.md#nightly-history)).
- Backups: set `BACKUP_DIR` (an absolute path outside the repository) and optionally `BACKUP_KEEP` (see
  [Backups](docs/operations.md#backups)).
- Location for the weather forecast: `COACH_HOME_LATITUDE` and `COACH_HOME_LONGITUDE` (see
  [Weather](docs/operations.md#weather)).

### 4. Secrets

```bash
scripts/setup-secrets.sh
```

The script asks (hidden input) for the auth key, the Hevy API key (verified against the Hevy API right away)
and the gateway password, and sets up TOTP (QR code in the terminal + verification with a code). It
generates the internal tokens between the gateway and the backends, and the read-only token `coach-mcp` uses
to read `garmin-mcp`.

### 5. Start

```bash
scripts/deploy.sh
docker compose ps          # all services healthy
```

`scripts/deploy.sh` builds the images (the tests run inside the build), tags them with the commit and starts the
stack; versions and rollback: [docs/operations.md](docs/operations.md#updates).

The `fitness-mcp` node appears in the admin console with the `tag:fitness-mcp` tag. Funnel turns on by
itself from `tailscale/serve.json` (`AllowFunnel`).

### 6. Garmin login (once, interactively over SSH)

```bash
scripts/garmin-login.sh            # email, password, MFA code; the password is not stored
scripts/garmin-login.sh --check
```

### 7. Verification

```bash
scripts/healthcheck.sh             # containers, node, Funnel, discovery, tailnet-only login, closed direct ports, Garmin, Hevy, coach database
scripts/smoke-test.sh              # tools/list on all three endpoints, coach memory summary, last 3 Hevy workouts, today's HRV/sleep, race predictions and HR zones, running tolerance, strength sets and activity weather, strength exercise catalogue
```

## Funnel

- It is enabled declaratively in `tailscale/serve.json` (`"AllowFunnel": {"${TS_CERT_DOMAIN}:443": true}`) and
  only on the `fitness-mcp` node. Nothing changes on the host.
- Status: `docker compose exec ts tailscale --socket /tmp/tailscaled.sock funnel status`
- To turn it off (tailnet only; claude.ai then cannot reach the connectors), delete the `AllowFunnel` block
  from `serve.json`. The sidecar watches the file, so the change applies by itself.
- The node's public DNS name can be discovered (Certificate Transparency). The model accounts for that:
  nothing sensitive is public.

## Adding the connectors

### claude.ai (web; also applies to mobile and Desktop)

1. On your Mac (or phone) **with Tailscale on**, open claude.ai → Settings → Connectors → **Add custom
   connector**.
2. Name `Hevy`, URL `https://fitness-mcp.<tailnet>/hevy/mcp`. Leave the OAuth Client ID/Secret empty (claude.ai
   identifies itself with CIMD) → **Add** → **Connect**.
3. The gateway's login page opens: enter the password + TOTP code → **Allow**.
4. The same for `Garmin`, URL `https://fitness-mcp.<tailnet>/garmin/mcp`.
5. The same for `Coach`, URL `https://fitness-mcp.<tailnet>/coach/mcp`.
6. Create a project (e.g. "Fitness coach") and enable all three connectors in it. The Custom Instructions are
   in [`docs/coach-instructions.md`](docs/coach-instructions.md).
7. Optionally, enable the built-in **Google Calendar** connector in the project (Settings → Connectors). The
   coach then checks free slots before proposing times and offers to create events after you confirm.
   Without it everything works the same, and the coach asks you for a time.

The connectors appear in Claude Desktop and the mobile app automatically (calls always come from Anthropic's
cloud through Funnel). You need to log in only when connecting for the first time, when the refresh token
expires (90 days without use) or when you revoke the grant.

### Claude Code (over the tailnet)

```bash
claude mcp add --transport http hevy   https://fitness-mcp.<tailnet>/hevy/mcp
claude mcp add --transport http garmin https://fitness-mcp.<tailnet>/garmin/mcp
claude mcp add --transport http coach  https://fitness-mcp.<tailnet>/coach/mcp
# in Claude Code: /mcp → pick the server → Authenticate (opens the browser with the gateway login)
```

Claude Code identifies itself with CIMD and uses the callback `http://localhost:<port>/callback` (allowed in
`gateway/config.toml`). It works only with Tailscale on; the gateway rejects tokens from the public internet
outside Anthropic's range.

### Claude Desktop locally over the tailnet (without Anthropic's cloud)

The claude.ai connectors work in Desktop by themselves. If you want a connection purely over the tailnet, add
this to `~/Library/Application Support/Claude/claude_desktop_config.json` (requires Node.js on the Mac):

```json
{
  "mcpServers": {
    "hevy-tailnet": {
      "command": "npx",
      "args": ["-y", "mcp-remote@0.14.3", "https://fitness-mcp.<tailnet>/hevy/mcp",
               "--static-oauth-client-info", "{\"client_id\":\"mcp-remote\"}",
               "--resource", "https://fitness-mcp.<tailnet>/hevy/mcp"]
    },
    "garmin-tailnet": {
      "command": "npx",
      "args": ["-y", "mcp-remote@0.14.3", "https://fitness-mcp.<tailnet>/garmin/mcp",
               "--static-oauth-client-info", "{\"client_id\":\"mcp-remote\"}",
               "--resource", "https://fitness-mcp.<tailnet>/garmin/mcp"]
    },
    "coach-tailnet": {
      "command": "npx",
      "args": ["-y", "mcp-remote@0.14.3", "https://fitness-mcp.<tailnet>/coach/mcp",
               "--static-oauth-client-info", "{\"client_id\":\"mcp-remote\"}",
               "--resource", "https://fitness-mcp.<tailnet>/coach/mcp"]
    }
  }
}
```

`mcp-remote` is pinned (the repository changed maintainers in August 2026). After restarting Desktop, the
gateway login opens.

## Development

The tests need no Docker, network or real accounts: Garmin, Hevy and Telegram are faked with canned payloads.
Per service (`garmin-mcp`, `gateway` or `coach-mcp`), with Python 3.13:

```bash
cd coach-mcp
python3.13 -m venv .venv && . .venv/bin/activate
pip install --no-deps --require-hashes -r requirements.lock
pip install --no-deps --require-hashes -r requirements-dev.lock
ruff check src tests && mypy && pytest
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the same three checks for every service, the
offline tests of the operations scripts, and then `scripts/check.sh`: the Docker test stage of each service and
the end-to-end OAuth + MCP flow in a throwaway Compose project. Run `scripts/check.sh` locally before a PR.
Dependencies are hash-locked; after changing a `pyproject.toml`, regenerate the locks with
`scripts/lock-deps.sh`. Dependabot ([`.github/dependabot.yml`](.github/dependabot.yml)) proposes monthly bumps of
the pinned Actions and base images, and [`.github/workflows/audit.yml`](.github/workflows/audit.yml) runs
`pip-audit` on every lock each week. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Repository layout

```
docker-compose.yml       services, hardening, networks, volumes, secrets
compose.test.yml         overlay for the e2e test (no Tailscale login, no internet)
.env.example             non-secret configuration (hostname, owner, UID/GID, briefing and Telegram, weather location)
CHANGELOG.md             release notes: schema version, secrets, new settings per version
tailscale/               sidecar image (non-root) + serve.json with Funnel
gateway/                 OAuth 2.1 gateway (Python, Starlette), tests, config.toml
hevy-mcp/                wrapper around upstream chrisdoc/hevy-mcp, pinned (secrets from files, telemetry off)
garmin-mcp/              Garmin MCP server (Python, python-garminconnect + MCP SDK), tests
coach-mcp/               coach memory (Python, MCP SDK, SQLite), tests
scripts/                 setup-secrets, deploy, garmin-login, healthcheck, backup, smoke-test, admin,
                         check-updates, lock-deps; tests: check (the full check), e2e-test, check-pins, check-ingress
.github/workflows/       CI: per-service ruff, mypy and pytest, script tests, scripts/check.sh; weekly pip-audit
.github/dependabot.yml   monthly update PRs for the Actions and base images
docs/coach-instructions.md   Custom Instructions for the "Fitness coach" Claude project
docs/operations.md       running the stack: Telegram features, history, backups, updates, troubleshooting
docs/gateway-design.md   gateway threat model, why it is custom, conformance matrix, known limitations
docs/how-this-was-built.md   the development process in short
docs/development-plan.md the full development plan: decisions, boundaries and one brief per PR
.claude/mao/             project configuration, review checklist and lessons for the coding agents
.claude/settings.json    Claude Code deny rules for .env, secrets/ and data/ (defence in depth, not a boundary)
```

## License

[MIT](LICENSE). Weather data by Open-Meteo (CC BY 4.0).
