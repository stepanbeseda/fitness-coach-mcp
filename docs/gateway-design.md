# Gateway design note

The gateway (`gateway/`) is the only part of this stack that the internet can reach. It is a small OAuth 2.1
authorization server and reverse proxy written for this project, without an OAuth library. This note gives its
threat model, why it is custom, what it implements and which test proves each part, what it leaves out on
purpose, and its known limitations. The summary is the README, [Security model](../README.md#security-model).

## Threat model

A request comes from one of three places. `gateway/src/gateway/netctx.py` decides which one, from the headers that
`tailscale serve` sets; `oauth.py` and `proxy.py` apply the rules for it.

### The internet, through Tailscale Funnel

Funnel brings claude.ai's connector traffic to `tailscale serve` on :443, which marks the request with
`Tailscale-Funnel-Request` and puts the caller's address in `X-Forwarded-For`. Anyone can send requests this way.

- Every Funnel request counts against a rate limit per address.
- Public: the discovery documents (`/.well-known/*`) and the 401 challenge of an MCP endpoint.
- `/token`, `/revoke` and the MCP endpoints answer a Funnel request only from `160.79.104.0/21`
  (`funnel_token_cidrs`), Anthropic's published outbound range for MCP connectors. The range belongs to
  Anthropic, not to the owner: it narrows where a stolen token can be used from, it does not identify anyone.
  The tokens do that: opaque, 256-bit, stored only as SHA-256 hashes, each valid for one MCP endpoint.
- `/healthz` answers every Funnel request with 404, and with the shipped `gateway/config.toml`
  (`login_tailnet_only = true`) the login page (`/authorize`) refuses every Funnel request with 403. A Funnel
  request has no Tailscale identity, even when it sends `Tailscale-User-Login` itself.

An attacker on the internet can read the discovery documents and spend the rate limits. Without a token nothing
else answers, and the login is not reachable at all.

### The tailnet

The owner's devices reach the node through `tailscale serve` too. Serve adds `Tailscale-User-Login` for a user's
own (untagged) device and removes any copy the client sent.

Nothing on the tailnet can go around `tailscale serve`. The sidecar runs tailscaled in userspace mode, which
forwards a tailnet connection for a port without a serve handler to `127.0.0.1` on the same port inside the
container, so nothing listens there: the gateway listens on `127.0.0.2:8080` and refuses to start on `127.0.0.1`
or `::1` (`listen_address_problem` in `gateway/src/gateway/config.py`), and tailscaled's health endpoint is on
`127.0.0.2:9002`. The tailnet policy allows members only tcp:443 to the node (README, install step 1).
`scripts/check-ingress.py` checks this wiring in every e2e run, and `scripts/healthcheck.sh` checks from the host
that ports 8080 and 9002 of the node accept no tailnet connection. Until PR 30 the gateway listened on
`127.0.0.1`, where a tailnet peer could reach it directly with forged identity headers. The tests behind these
rules are in the matrix row "Trust boundary (PR 30)".

- With `login_tailnet_only = true`, the login page answers only when that login equals `OWNER_TAILSCALE_LOGIN`
  (case is ignored). It then asks for
  the argon2id-hashed password and a TOTP code, with a lockout after repeated failures and no reuse of a code.
- The owner's devices may also call `/token` and the MCP endpoints directly (Claude Code, `mcp-remote`).
- Tagged nodes and other users of the tailnet have no owner identity: no login page, and `/token` and
  token-bearing MCP calls answer 403.

A device signed in to the owner's Tailscale account counts as the owner's. It still needs the password and a TOTP
code to connect a new client, and it can use any token it already holds.

### The container network

The gateway shares the network namespace of the `ts` sidecar and reaches the backends on the `mcp` Docker
network; no service publishes a port on the host.

- The client's token never leaves the gateway. A backend gets the gateway's own internal token
  (`secrets/*_backend_token`) and only an allowlist of request headers; the Tailscale and forwarding headers are
  not passed on.
- `coach-mcp` reads `garmin-mcp` with a separate read-only token, and `garmin-mcp` refuses every tool that is not
  read-only for it. `coach-mcp` reads `hevy-mcp` with the internal `hevy-mcp` token (`hevy_backend_token`),
  which has full rights there, held back by a two-tool allowlist in code (see Known limitations).
- A compromised backend holds only the secrets mounted into it (`coach-mcp` also holds the read-only Garmin
  token, `hevy_backend_token` and the Telegram bot token). It cannot mint client tokens.

What the model in claude.ai can be made to do through tool results is a separate question; see the README,
Security model, "LLM / prompt injection".

## Why a custom server

The parts that make this gateway safe are specific to one owner behind Tailscale:

- one user, whose identity comes from Tailscale, so the login is a password and TOTP check behind that identity;
- a network policy per endpoint (public, Anthropic's range, tailnet owner only);
- pre-registered clients: the client id metadata documents (CIMD) of claude.ai and Claude Code are pinned in
  `gateway/config.toml`, never fetched at runtime (no server-side request forgery), and there is no dynamic
  client registration;
- every token bound to exactly one MCP endpoint, and a hashed SQLite store with refresh rotation and reuse
  detection;
- a small dependency surface: six direct dependencies (Starlette, uvicorn, httpx, argon2-cffi, pyotp, segno),
  16 locked packages.

A library would provide the protocol plumbing, not these parts. As checked on 2026-10-18:

- The MCP Python SDK (`mcp` 2.2.0, the version `garmin-mcp` and `coach-mcp` use) has `mcp.server.auth`: handlers
  for `/authorize`, `/token`, an optional `/register` and `/revoke`, the RFC 8414 and RFC 9728 metadata, and bearer
  middleware that checks the RFC 8707 resource. The application still implements the provider: client lookup,
  storage of codes and tokens, and the login. Its server side has no CIMD option, its metadata advertises only
  `client_secret_post` and `client_secret_basic` client authentication (this gateway serves public clients with
  `none`), and its authorization handler adds no `iss` (RFC 9207). `mcp` itself requires 13
  packages. Nine of them, among them pydantic, jsonschema, pyjwt and opentelemetry-api, are not in the gateway's
  lock today; the other four (anyio, starlette, typing-extensions, uvicorn) are.
- authlib 1.9.1 has modules for RFC 6749, 7009, 7591, 7636, 8414 and 9207 among others, but none for RFC 8707
  resource indicators or RFC 9728 protected resource metadata, and its server integrations are for Flask, Django
  and SQLAlchemy only (for Starlette it has a client).

With either, the identity-gated login, the network policy, the pinned clients and the store would remain bespoke,
plus glue to the library. Writing the protocol part directly, with a test per rule (below), was the smaller and
more reviewable result.

## Conformance matrix

Each line names the code and the tests in `gateway/tests/` that prove it.

| Standard | What the gateway does | Code | Tests |
|---|---|---|---|
| RFC 8414 (server metadata) | `/.well-known/oauth-authorization-server`: issuer, endpoints, `code` only, the two grant types, S256 only, `none` client authentication, `iss` support; no `registration_endpoint` | `oauth.py` | `test_metadata.py::test_authorization_server_metadata`, `test_metadata.py::test_authorization_server_metadata_lists_only_implemented_features` |
| RFC 9728 (protected resource metadata) | One document per MCP endpoint at `/.well-known/oauth-protected-resource/<path>`, none at the root; a 401 names it in `resource_metadata` | `oauth.py`, `proxy.py` | `test_metadata.py::test_protected_resource_metadata_per_backend`, `test_metadata.py::test_root_protected_resource_metadata_is_not_served`, `test_metadata.py::test_unauthenticated_mcp_gets_challenge_from_anywhere` |
| RFC 7636 (PKCE), S256 only | Required on every authorization (a missing challenge or method → `invalid_request`); `plain` refused; the challenge is exactly 43 base64url characters, the verifier 43–128 characters; a wrong verifier fails the exchange | `security.py`, `oauth.py` | `test_units.py::test_pkce_rfc7636_vector`, `test_units.py::test_pkce_length_bounds`, `test_oauth.py::test_pkce_parameters_are_required`, `test_oauth.py::test_pkce_and_resource_are_mandatory`, `test_oauth.py::test_token_endpoint_rejects_wrong_verifier_and_reuse` |
| RFC 8707 (resource indicators) | Exactly one `resource` per authorization (none or two → `invalid_target`), normalised; the token works only on that endpoint; another resource at `/token` → `invalid_target`, at another endpoint → 401 `invalid_token` | `config.py`, `oauth.py`, `proxy.py` | `test_units.py::test_normalize_resource`, `test_oauth.py::test_pkce_and_resource_are_mandatory`, `test_oauth.py::test_exactly_one_resource_is_required`, `test_oauth.py::test_resource_is_normalized`, `test_oauth.py::test_token_endpoint_binding_checks`, `test_oauth.py::test_token_is_bound_to_one_resource` |
| RFC 9207 (`iss` in the response) | `iss` on the success redirect and on the error redirects, which one helper (`_redirect_error`) builds | `oauth.py` | `test_oauth.py::test_full_flow_claude_ai`, `test_oauth.py::test_pkce_and_resource_are_mandatory` |
| RFC 7009 (revocation) | A refresh token revokes its grant, an access token only itself; an unknown token answers 200; `/revoke` answers a Funnel request only from the token networks (403 otherwise). Deviation: see Known limitations | `oauth.py` | `test_oauth.py::test_revocation`, `test_oauth.py::test_revocation_is_refused_outside_the_token_networks`, `test_oauth.py::test_revoking_an_access_token_keeps_the_grant`, `test_oauth.py::test_revocation_by_another_client_changes_nothing` |
| OAuth 2.1, authorization code | `response_type=code` only; exact redirect URIs (any port for a loopback redirect, RFC 8252); codes single-use and bound to client and redirect URI; reuse revokes what the code issued | `oauth.py` | `test_oauth.py::test_pkce_and_resource_are_mandatory`, `test_oauth.py::test_unknown_client_and_bad_redirect_never_redirect`, `test_oauth.py::test_claude_code_loopback_redirect_any_port`, `test_units.py::test_redirect_uri_matching`, `test_oauth.py::test_token_endpoint_rejects_wrong_verifier_and_reuse`, `test_oauth.py::test_token_endpoint_binding_checks` |
| OAuth 2.1, refresh rotation | A new refresh token on every use; the old one works for `refresh_reuse_grace` more, 120 s in the shipped config (a lost response), after that its use revokes the grant; it works only for the client it was issued to, and `/token` serves it only to the allowed networks | `oauth.py`, `store.py` | `test_oauth.py::test_refresh_rotation_and_grace`, `test_oauth.py::test_refresh_reuse_after_grace_revokes_grant`, `test_oauth.py::test_refresh_grace_window_is_120_seconds`, `test_oauth.py::test_refresh_requires_same_client_and_network`, `test_oauth.py::test_refresh_retry_inside_grace_window_forks_the_grant` |
| OAuth 2.1, public clients | No client secrets: Basic authentication with a secret → `invalid_client`; an unknown client → 401 | `oauth.py` | `test_oauth.py::test_client_secret_basic_is_rejected_for_public_clients`, `test_oauth.py::test_token_endpoint_binding_checks` |
| OAuth 2.1, bearer tokens | Only in the `Authorization` header with the Bearer scheme; a token in the query string is ignored | `proxy.py` | `test_proxy.py::test_bearer_scheme_required`, `test_proxy.py::test_access_token_in_query_is_not_accepted` |
| MCP authorization 2025-11-25 (as implemented) | Discovery through the 401 challenge and the two metadata documents; CIMD client ids pinned in config (an unknown one is refused without a redirect); no dynamic registration endpoint; the client token is never passed to a backend | `oauth.py`, `proxy.py`, `config.toml` | `test_metadata.py::test_unauthenticated_mcp_gets_challenge_from_anywhere`, `test_oauth.py::test_unknown_client_and_bad_redirect_never_redirect`, `test_metadata.py::test_no_dynamic_client_registration`, `test_oauth.py::test_full_flow_claude_ai` |
| Trust boundary (PR 30) | The client is the right-most `X-Forwarded-For` entry; a Funnel request has no Tailscale identity even if it sends one; the owner match ignores case; the gateway never listens where netstack forwards; the audit log holds no password, code, token, tool argument or result; the CSRF cookie is `HttpOnly`, `SameSite=Strict` and `Secure`; a full slot answers 503; the admin CLI enforces its limits | `netctx.py`, `config.py`, `audit.py`, `oauth.py`, `proxy.py`, `cli.py` | `test_units.py::test_net_context_uses_the_right_most_forwarded_address`, `test_units.py::test_net_context_ignores_identity_on_funnel_requests`, `test_units.py::test_net_context_without_headers_is_unknown`, `test_units.py::test_owner_match_ignores_case`, `test_units.py::test_listen_address_problem_accepts_other_ipv4_loopback_addresses`, `test_units.py::test_listen_address_problem_rejects_the_rest`, `test_units.py::test_listen_host_default_and_validation`, `test_oauth.py::test_token_endpoint_uses_the_right_most_forwarded_address`, `test_oauth.py::test_funnel_request_with_forged_owner_login_is_not_the_owner`, `test_oauth.py::test_csrf_cookie_flags`, `test_proxy.py::test_mcp_uses_the_right_most_forwarded_address`, `test_proxy.py::test_busy_stream_slot_answers_503`, `test_proxy.py::test_busy_request_slot_answers_503`, `test_audit.py::test_audit_log_holds_no_secrets_or_tool_data`, `test_cli.py::test_hash_password_enforces_minimum_length`, `test_cli.py::test_grants_revoke_needs_an_unambiguous_prefix`, `test_cli.py::test_mint_token_clamps_ttl_and_rejects_unknown_backend`; outside pytest: `scripts/check-ingress.py --self-test` (run by `scripts/e2e-test.sh`) and case H28 of `scripts/test-healthcheck.sh` |

## Non-goals

- Dynamic client registration (RFC 7591): clients are added to `gateway/config.toml` by the owner.
- Confidential clients and client secrets.
- Scopes per tool: there is one scope, `mcp`. Which tools a token reaches follows from its MCP endpoint, the
  gateway's denylist (Hevy `feedback`) and the backends' own rules.
- JWT access tokens (RFC 9068) and token introspection (RFC 7662): tokens are opaque and checked against the
  gateway's own database.
- More than one user.
- Ingress without Tailscale.

## Known limitations

1. A refresh retry inside the 120 s grace window forks the grant: the old refresh token mints a second chain, and
   both stay valid. Someone who captured a refresh token could keep a parallel chain the client never notices.
   `test_oauth.py::test_refresh_retry_inside_grace_window_forks_the_grant` pins this behaviour and turns red when
   it is fixed.
2. Grants have a sliding lifetime and no absolute maximum: each refresh issues a new 90-day refresh token. A
   connector that is removed and added again leaves its old grant valid until that grant's refresh token expires,
   unless the client revoked it.
   `scripts/admin.sh grants list` shows the grants, `grants revoke` ends one.
3. Revocation of another client's token answers 200 and revokes nothing, where RFC 7009 §2.1 says the request is
   refused. The answer is the same as for an unknown token, so it reveals nothing.
   `test_oauth.py::test_revocation_by_another_client_changes_nothing` pins it.
4. `160.79.104.0/21` is Anthropic's range for every customer's connectors, not the owner's: it limits where a
   stolen token works, nothing more.
5. `coach-mcp` holds `hevy_backend_token`, the internal token for `hevy-mcp`, which has full rights there. Only
   the allowlist of two read tools in `coach-mcp/src/coach_mcp/hevy.py` keeps it from writing to Hevy; a
   compromised `coach-mcp` could call every `hevy-mcp` tool, including `feedback`, which the gateway blocks only on
   its own path (`tool_denylist`). A read-only Hevy role would need a filtering proxy in front of the upstream
   `hevy-mcp` image.
6. The owner's credential is one password plus TOTP behind the Tailscale identity; there is no hardware key.
