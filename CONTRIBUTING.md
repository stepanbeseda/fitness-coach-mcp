# Contributing

This is a personal project, published as a reference implementation. Issues and small pull requests are welcome;
for anything larger, please open an issue first so we can agree on the approach.

## Ground rules

- **One change per PR**, with its documentation and tests in the same PR.
- **Tests are offline.** Garmin, Hevy, Telegram and Open-Meteo are faked with canned payloads; no test touches the
  network or a real account.
- **Green check.** `ruff check src tests`, `mypy` and `pytest` pass for every service you touch, and
  `scripts/check.sh` (Docker test stages + end-to-end flow) ends with `CHECK OK`. CI runs the same.
- **Writes need confirmation.** A tool that changes Garmin, Hevy or coach state says so in its annotations and
  description, and `docs/coach-instructions.md` keeps the confirm-before-write rule.
- **Privacy of logs.** Logs and the audit trail carry tool names, outcomes and durations only: never arguments,
  results, tokens or health values.
- **Supply chain.** Dependencies are pinned and hash-locked (`scripts/lock-deps.sh`), base images pinned by digest.
  A new dependency needs a reason in the PR.
- **No secrets, personal hostnames or account identifiers** in code, docs or tests.
- English for code, docs and commit messages (short imperative subject, e.g. "Add weekly report tool").

The design decisions behind these rules are in [docs/development-plan.md](docs/development-plan.md), section 1.
