# Changelog

## 0.2.0

The release of the architecture-review batch of 2026-10-18 (PR 30 to PR 35 in
[docs/development-plan.md](docs/development-plan.md)).

- Coach database schema: version 9 (unchanged). An older `coach-mcp` refuses it; see "Going back to a version
  without …" in [coach-mcp/README.md](coach-mcp/README.md).
- Secrets (files in `secrets/`, created by `scripts/setup-secrets.sh`): `ts_authkey`, `gateway_password_hash`,
  `gateway_totp_secret`, `hevy_api_key`, `hevy_backend_token`, `garmin_backend_token`, `garmin_readonly_token`,
  `coach_backend_token`, `telegram_bot_token`. None is new in this version.
- New `.env` keys: `FCM_VERSION` and `FCM_PREVIOUS_VERSION`, written by `scripts/deploy.sh`.
- Deploys: `scripts/deploy.sh` replaces `docker compose` with `--build`; images are tagged with the commit
  and the previous ones are kept for `scripts/deploy.sh --rollback`. `gateway/config.toml` is no longer
  mounted; it ships inside the gateway image. Moving from 0.1.0: [docs/operations.md](docs/operations.md),
  "First deploy with scripts/deploy.sh".

## 0.1.0

Everything before 0.2.0: PR 01 to PR 29 in [docs/development-plan.md](docs/development-plan.md).
