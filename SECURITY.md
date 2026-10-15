# Security policy

This is a single-user, self-hosted project: there is no hosted service to attack, and every deployment is run by
its owner. Reports about the code are still very welcome, above all about the OAuth 2.1 gateway (`gateway/`),
which is reachable from the internet through Tailscale Funnel.

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability** on this repository. Do not open a
public issue for a security problem. Include the affected component, the steps to reproduce and the impact you
expect. You should get an answer within a week.

## Scope

In scope:
- `gateway/`: authorization, PKCE, token issue/refresh/revocation, resource binding, the Funnel source-IP
  restriction, login lockout, CSRF, the reverse proxy and audit logging.
- `coach-mcp/`, `garmin-mcp/`: tool access control (the read-only token), handling of health data in logs, the
  Telegram integration.
- `docker-compose.yml`, `tailscale/` and `scripts/`: container hardening, secret handling.

Out of scope: the upstream projects this repository builds on (`chrisdoc/hevy-mcp`, `python-garminconnect`,
Tailscale, the MCP SDK); report those to their maintainers. Issues that need an attacker already on the owner's
tailnet or server are of limited interest but still welcome.

The design and its assumptions are described in the README, [Security model](README.md#security-model).
