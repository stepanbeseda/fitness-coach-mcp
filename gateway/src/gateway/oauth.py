"""OAuth 2.1 authorization server for MCP clients.

Implements what claude.ai, Claude Code and mcp-remote need (MCP authorization
spec 2025-11-25): RFC 8414 metadata, RFC 9728 protected resource metadata,
authorization code + PKCE (S256 only), RFC 8707 resource indicators (every token
is bound to exactly one MCP endpoint), RFC 9207 `iss`, refresh token rotation
with reuse detection and RFC 7009 revocation. Clients are pre-registered
(including pinned Client ID Metadata Documents); there is no open registration.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import anyio
from starlette.datastructures import QueryParams
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from .audit import Audit
from .config import SCOPE, Backend, Client, Settings, is_loopback_redirect, normalize_resource
from .netctx import NetContext, net_context
from .pages import consent_page, error_page
from .ratelimit import RateLimiter
from .security import LoginGuard, hash_token, is_valid_code_challenge, new_id, new_token, verify_pkce
from .store import AuthCode, AuthRequest, Grant, Store, Token

MAX_FORM_BYTES = 16 * 1024
MAX_ATTEMPTS_PER_REQUEST = 5
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class DuplicateParameter(Exception):
    pass


def _single(values: list[str] | None) -> str | None:
    if not values:
        return None
    if len(values) > 1:
        raise DuplicateParameter
    return values[0]


def query_single(params: QueryParams, key: str) -> str | None:
    return _single(params.getlist(key))


def form_single(form: dict[str, list[str]], key: str) -> str | None:
    return _single(form.get(key))


async def read_body(request: Request, limit: int) -> bytes | None:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def parse_form(body: bytes) -> dict[str, list[str]] | None:
    try:
        pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=64)
    except (UnicodeDecodeError, ValueError):
        return None
    form: dict[str, list[str]] = {}
    for key, value in pairs:
        form.setdefault(key, []).append(value)
    return form


def redirect_uri_allowed(client: Client, candidate: str) -> bool:
    """Exact match, except loopback redirects where any port is accepted (RFC 8252)."""
    try:
        parts = urlsplit(candidate)
        _ = parts.port  # validates the port
    except ValueError:
        return False
    if parts.fragment or parts.username or parts.password:
        return False
    for registered in client.redirect_uris:
        if is_loopback_redirect(registered):
            reg = urlsplit(registered)
            if (
                parts.scheme == "http"
                and parts.hostname == reg.hostname
                and parts.path == reg.path
                and parts.query == reg.query
            ):
                return True
        elif candidate == registered:
            return True
    return False


def oauth_error(status: int, error: str, description: str | None = None, headers: dict | None = None) -> JSONResponse:
    body = {"error": error}
    if description:
        body["error_description"] = description
    return JSONResponse(body, status_code=status, headers={**NO_STORE, **(headers or {})})


@dataclass(frozen=True)
class Minted:
    access: str
    refresh: str
    access_row: Token
    refresh_row: Token


class OAuthServer:
    def __init__(self, settings: Settings, store: Store, audit: Audit, guard: LoginGuard) -> None:
        self.settings = settings
        self.store = store
        self.audit = audit
        self.guard = guard
        self._limit_authorize = RateLimiter(settings.rate_authorize.per_minute, settings.rate_authorize.burst)
        self._limit_token = RateLimiter(settings.rate_token_endpoint.per_minute, settings.rate_token_endpoint.burst)

    # -- metadata -------------------------------------------------------------------

    def authorization_server_metadata(self, request: Request) -> JSONResponse:
        base = self.settings.issuer
        return JSONResponse(
            {
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "revocation_endpoint": f"{base}/revoke",
                "response_types_supported": ["code"],
                "response_modes_supported": ["query"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "revocation_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": [SCOPE],
                "client_id_metadata_document_supported": True,
                "authorization_response_iss_parameter_supported": True,
            },
            headers={"Cache-Control": "public, max-age=300"},
        )

    def protected_resource_metadata(self, backend: Backend) -> JSONResponse:
        return JSONResponse(
            {
                "resource": backend.resource,
                "authorization_servers": [self.settings.issuer],
                "scopes_supported": [SCOPE],
                "bearer_methods_supported": ["header"],
                "resource_name": f"{backend.title} (fitness-coach-mcp)",
            },
            headers={"Cache-Control": "public, max-age=300"},
        )

    # -- helpers ----------------------------------------------------------------------

    def token_origin_allowed(self, ctx: NetContext) -> bool:
        """Token-bearing traffic: the owner's tailnet devices, or Funnel traffic from
        the allowed networks (Anthropic's outbound range)."""
        if ctx.is_owner(self.settings.owner_tailscale_login):
            return True
        return ctx.via_funnel and self.settings.ip_allowed_for_tokens(ctx.client_ip)

    def _login_origin_denied(self, ctx: NetContext) -> Response | None:
        if not self.settings.login_tailnet_only or ctx.is_owner(self.settings.owner_tailscale_login):
            return None
        self.audit.event("auth.login_origin_denied", src=ctx.source, ip=ctx.client_ip if ctx.via_funnel else None)
        return error_page(
            403,
            "Přístup odepřen",
            "Přihlášení je dostupné jen z tvých zařízení v tailnetu. Zapni Tailscale a otevři odkaz znovu.",
        )

    def _redirect(self, uri: str, params: dict[str, str | None], status: int) -> RedirectResponse:
        parts = urlsplit(uri)
        query = parse_qsl(parts.query, keep_blank_values=True)
        query += [(k, v) for k, v in params.items() if v is not None]
        location = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
        return RedirectResponse(location, status_code=status, headers={**NO_STORE, "Referrer-Policy": "no-referrer"})

    def _redirect_error(
        self, uri: str, state: str | None, error: str, description: str | None = None, status: int = 302
    ) -> RedirectResponse:
        return self._redirect(
            uri,
            {"error": error, "error_description": description, "state": state, "iss": self.settings.issuer},
            status,
        )

    def _cookie_name(self, txn_hash: str) -> str:
        prefix = "__Host-fcm-csrf-" if self.settings.secure_cookies else "fcm-csrf-"
        return prefix + txn_hash[:12]

    def _mint(self, grant_id: str, now: int) -> Minted:
        access, refresh = new_token(), new_token()
        return Minted(
            access=access,
            refresh=refresh,
            access_row=Token(new_id(), hash_token(access), "access", grant_id, now,
                             now + self.settings.access_token_ttl, None, None, None),
            refresh_row=Token(new_id(), hash_token(refresh), "refresh", grant_id, now,
                              now + self.settings.refresh_token_ttl, None, None, None),
        )

    def _token_response(self, minted: Minted) -> JSONResponse:
        return JSONResponse(
            {
                "access_token": minted.access,
                "token_type": "Bearer",
                "expires_in": self.settings.access_token_ttl,
                "refresh_token": minted.refresh,
                "scope": SCOPE,
            },
            headers=NO_STORE,
        )

    def _backend_name(self, resource: str) -> str | None:
        backend = self.settings.backend_for_resource(resource)
        return backend.name if backend else None

    # -- /authorize ---------------------------------------------------------------------

    async def authorize(self, request: Request) -> Response:
        ctx = net_context(request)
        if denied := self._login_origin_denied(ctx):
            return denied
        if self._limit_authorize.hit(ctx.tailscale_login or ctx.client_ip or "unknown"):
            return error_page(429, "Příliš mnoho požadavků", "Zkus to prosím za chvíli.")
        if request.method == "POST":
            return await self._authorize_submit(request, ctx)
        return self._authorize_start(request, ctx)

    def _authorize_start(self, request: Request, ctx: NetContext) -> Response:
        params = request.query_params
        try:
            client_id = query_single(params, "client_id")
            redirect_uri = query_single(params, "redirect_uri")
        except DuplicateParameter:
            return error_page(400, "Neplatný požadavek", "Parametr client_id nebo redirect_uri je uveden víckrát.")
        client = self.settings.clients.get(client_id or "")
        if client is None:
            self.audit.event("oauth.authorize_unknown_client", client_id=(client_id or "")[:200], src=ctx.source)
            return error_page(400, "Neznámý klient", "Tahle aplikace není na bráně povolená.")
        if not redirect_uri or not redirect_uri_allowed(client, redirect_uri):
            self.audit.event("oauth.authorize_bad_redirect", client=client.name, src=ctx.source)
            return error_page(400, "Neplatné přesměrování", "Adresa redirect_uri neodpovídá registrovanému klientovi.")

        # From here on errors go back to the (verified) client redirect URI.
        try:
            state = query_single(params, "state")
            response_type = query_single(params, "response_type")
            challenge = query_single(params, "code_challenge")
            method = query_single(params, "code_challenge_method")
            query_single(params, "scope")
        except DuplicateParameter:
            return self._redirect_error(redirect_uri, None, "invalid_request", "duplicate parameter")
        if response_type != "code":
            return self._redirect_error(redirect_uri, state, "unsupported_response_type")
        if method != "S256" or not is_valid_code_challenge(challenge):
            return self._redirect_error(redirect_uri, state, "invalid_request", "PKCE with S256 is required")
        resources = params.getlist("resource")
        if len(resources) != 1:
            return self._redirect_error(redirect_uri, state, "invalid_target", "exactly one resource is required")
        resource = normalize_resource(resources[0])
        backend = self.settings.backend_for_resource(resource) if resource else None
        if backend is None:
            return self._redirect_error(redirect_uri, state, "invalid_target", "unknown resource")

        now = int(time.time())
        txn, csrf = new_token(), new_token()
        txn_hash = hash_token(txn)
        self.store.create_auth_request(
            AuthRequest(
                txn_hash=txn_hash,
                csrf_hash=hash_token(csrf),
                client_id=client.client_id,
                redirect_uri=redirect_uri,
                code_challenge=challenge,
                resource=backend.resource,
                scope=SCOPE,
                state=state,
                tailscale_login=ctx.tailscale_login,
                attempts=0,
                created_at=now,
                expires_at=now + self.settings.auth_request_ttl,
            )
        )
        self.audit.event("oauth.authorize_started", client=client.name, resource=backend.name, src=ctx.source)
        response = consent_page(
            txn=txn,
            client_name=client.name,
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            resource_title=backend.title,
            resource=backend.resource,
        )
        response.set_cookie(
            self._cookie_name(txn_hash),
            csrf,
            max_age=self.settings.auth_request_ttl,
            path="/",
            secure=self.settings.secure_cookies,
            httponly=True,
            samesite="strict",
        )
        return response

    async def _authorize_submit(self, request: Request, ctx: NetContext) -> Response:
        body = await read_body(request, MAX_FORM_BYTES)
        form = parse_form(body) if body is not None else None
        if form is None:
            return error_page(400, "Neplatný požadavek", "Formulář se nepodařilo zpracovat.")
        try:
            txn = form_single(form, "txn")
            decision = form_single(form, "decision")
            password = form_single(form, "password") or ""
            code = form_single(form, "totp") or ""
        except DuplicateParameter:
            return error_page(400, "Neplatný požadavek", "Parametr je uveden víckrát.")
        now = int(time.time())
        req = self.store.get_auth_request(hash_token(txn), now) if txn else None
        if req is None:
            return error_page(400, "Žádost vypršela", "Začni přihlášení znovu z aplikace (Claude).")
        cookie = request.cookies.get(self._cookie_name(req.txn_hash))
        if not cookie or not hmac.compare_digest(hash_token(cookie), req.csrf_hash):
            self.audit.event("auth.csrf_failed", src=ctx.source)
            return error_page(400, "Neplatný formulář", "Chybí ochranná cookie. Začni přihlášení znovu z aplikace.")
        if self.settings.login_tailnet_only and (req.tailscale_login or "").casefold() != (
            ctx.tailscale_login or ""
        ).casefold():
            return error_page(403, "Přístup odepřen", "Žádost patří jinému uživateli.")
        client = self.settings.clients.get(req.client_id)
        backend = self.settings.backend_for_resource(req.resource)
        if client is None or backend is None:
            self.store.delete_auth_request(req.txn_hash)
            return error_page(400, "Neplatná žádost", "Klient nebo resource už není nakonfigurovaný.")

        def again(message: str, status: int) -> Response:
            return consent_page(
                txn=txn,
                client_name=client.name,
                client_id=client.client_id,
                redirect_uri=req.redirect_uri,
                resource_title=backend.title,
                resource=backend.resource,
                error=message,
                status=status,
            )

        if decision == "deny":
            self.store.delete_auth_request(req.txn_hash)
            self.audit.event("oauth.authorize_denied", client=client.name, resource=backend.name)
            return self._redirect_error(req.redirect_uri, req.state, "access_denied", status=303)
        if decision != "approve":
            return error_page(400, "Neplatný požadavek", "Neznámá volba.")
        if req.attempts >= MAX_ATTEMPTS_PER_REQUEST:
            self.store.delete_auth_request(req.txn_hash)
            return error_page(400, "Příliš mnoho pokusů", "Začni přihlášení znovu z aplikace.")
        locked = self.guard.locked_for(now)
        if locked:
            self.audit.event("auth.login_locked", retry_after=locked, src=ctx.source)
            return again(f"Přihlášení je dočasně zablokované. Zkus to za {max(1, round(locked / 60))} min.", 429)

        ok = await anyio.to_thread.run_sync(self.guard.attempt, password, code)
        if not ok:
            self.store.bump_auth_request_attempts(req.txn_hash)
            self.audit.event("auth.login_failed", client=client.name, src=ctx.source)
            return again("Neplatné heslo nebo kód.", 401)

        self.store.delete_auth_request(req.txn_hash)
        auth_code = new_token()
        self.store.create_code(
            AuthCode(
                code_hash=hash_token(auth_code),
                client_id=req.client_id,
                redirect_uri=req.redirect_uri,
                code_challenge=req.code_challenge,
                resource=req.resource,
                scope=req.scope,
                tailscale_login=req.tailscale_login,
                created_at=now,
                expires_at=now + self.settings.auth_code_ttl,
                used_at=None,
                grant_id=None,
            )
        )
        self.audit.event("auth.login_ok", client=client.name, resource=backend.name, src=ctx.source)
        response = self._redirect(
            req.redirect_uri, {"code": auth_code, "state": req.state, "iss": self.settings.issuer}, 303
        )
        response.delete_cookie(self._cookie_name(req.txn_hash), path="/", secure=self.settings.secure_cookies,
                               httponly=True, samesite="strict")
        return response

    # -- /token ---------------------------------------------------------------------------

    async def _client_request(self, request: Request, ctx: NetContext, endpoint: str):
        """Common checks for /token and /revoke. Returns (form, client) or a Response."""
        if not self.token_origin_allowed(ctx):
            self.audit.event(f"oauth.{endpoint}_origin_denied", src=ctx.source, ip=ctx.client_ip)
            return oauth_error(403, "access_denied", "not allowed from this network")
        if retry := self._limit_token.hit(ctx.client_ip or ctx.tailscale_login or "unknown"):
            return oauth_error(429, "slow_down", "too many requests", {"Retry-After": str(retry)})
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            return oauth_error(400, "invalid_request", "expected application/x-www-form-urlencoded")
        body = await read_body(request, MAX_FORM_BYTES)
        form = parse_form(body) if body is not None else None
        if form is None:
            return oauth_error(400, "invalid_request", "malformed body")
        try:
            client_id = form_single(form, "client_id")
            basic = request.headers.get("authorization")
            if basic:
                # Public clients only: accept "Basic base64(client_id:)" with an empty secret.
                scheme, _, value = basic.partition(" ")
                if scheme.lower() != "basic":
                    return oauth_error(401, "invalid_client", "unsupported client authentication")
                try:
                    user, _, secret = base64.b64decode(value, validate=True).decode("utf-8").partition(":")
                except (binascii.Error, UnicodeDecodeError):
                    return oauth_error(401, "invalid_client", "malformed client authentication")
                if secret or (client_id and client_id != user):
                    return oauth_error(401, "invalid_client", "public clients must not use a client secret")
                client_id = user
        except DuplicateParameter:
            return oauth_error(400, "invalid_request", "duplicate parameter")
        client = self.settings.clients.get(client_id or "")
        if client is None:
            self.audit.event(f"oauth.{endpoint}_unknown_client", src=ctx.source)
            return oauth_error(401, "invalid_client", "unknown client")
        return form, client

    async def token(self, request: Request) -> Response:
        ctx = net_context(request)
        result = await self._client_request(request, ctx, "token")
        if isinstance(result, Response):
            return result
        form, client = result
        try:
            grant_type = form_single(form, "grant_type")
            if grant_type == "authorization_code":
                return self._code_grant(form, client, ctx)
            if grant_type == "refresh_token":
                return self._refresh_grant(form, client, ctx)
        except DuplicateParameter:
            return oauth_error(400, "invalid_request", "duplicate parameter")
        return oauth_error(400, "unsupported_grant_type")

    def _requested_resource_ok(self, form: dict[str, list[str]], bound: str) -> bool:
        resources = form.get("resource", [])
        if not resources:
            return True
        return len(resources) == 1 and normalize_resource(resources[0]) == bound

    def _code_grant(self, form: dict[str, list[str]], client: Client, ctx: NetContext) -> Response:
        code = form_single(form, "code")
        redirect_uri = form_single(form, "redirect_uri")
        verifier = form_single(form, "code_verifier")
        if not code or not redirect_uri or not verifier:
            return oauth_error(400, "invalid_request", "code, redirect_uri and code_verifier are required")
        now = int(time.time())
        code_hash = hash_token(code)
        status, row = self.store.take_code(code_hash, now)
        if status == "reused" and row is not None:
            if row.grant_id:
                self.store.revoke_grant(row.grant_id, "authorization code reused", now)
            self.audit.event("oauth.code_reused", client=client.name, src=ctx.source)
            return oauth_error(400, "invalid_grant", "authorization code already used")
        if status != "ok" or row is None:
            return oauth_error(400, "invalid_grant", "invalid or expired authorization code")
        if row.client_id != client.client_id or row.redirect_uri != redirect_uri:
            self.audit.event("oauth.code_binding_mismatch", client=client.name, src=ctx.source)
            return oauth_error(400, "invalid_grant", "authorization code was issued to another client or redirect_uri")
        if not verify_pkce(verifier, row.code_challenge):
            self.audit.event("oauth.pkce_failed", client=client.name, src=ctx.source)
            return oauth_error(400, "invalid_grant", "PKCE verification failed")
        if not self._requested_resource_ok(form, row.resource):
            return oauth_error(400, "invalid_target", "resource does not match the authorization request")

        grant = Grant(new_id(), client.client_id, row.resource, row.scope, row.tailscale_login, now, now, None, None)
        minted = self._mint(grant.id, now)
        self.store.create_grant_with_tokens(grant, minted.access_row, minted.refresh_row, code_hash)
        self.audit.event(
            "oauth.token_issued", client=client.name, resource=self._backend_name(row.resource),
            grant=grant.id[:8], src=ctx.source,
        )
        return self._token_response(minted)

    def _refresh_grant(self, form: dict[str, list[str]], client: Client, ctx: NetContext) -> Response:
        presented = form_single(form, "refresh_token")
        if not presented:
            return oauth_error(400, "invalid_request", "refresh_token is required")
        now = int(time.time())
        token = self.store.get_token(hash_token(presented))
        if token is None or token.kind != "refresh":
            return oauth_error(400, "invalid_grant", "unknown refresh token")
        grant = self.store.get_grant(token.grant_id)
        if grant is None or grant.revoked_at is not None:
            return oauth_error(400, "invalid_grant", "grant revoked")
        if grant.client_id != client.client_id:
            self.audit.event("oauth.refresh_client_mismatch", client=client.name, src=ctx.source)
            return oauth_error(400, "invalid_grant", "refresh token was issued to another client")
        grace_start = now - self.settings.refresh_reuse_grace
        if token.revoked_at is not None or (token.rotated_at is not None and token.rotated_at < grace_start):
            self.store.revoke_grant(grant.id, "refresh token reuse", now)
            self.audit.event("oauth.refresh_reuse_detected", client=client.name, grant=grant.id[:8], src=ctx.source)
            return oauth_error(400, "invalid_grant", "refresh token reuse detected; grant revoked")
        if token.expires_at <= now:
            return oauth_error(400, "invalid_grant", "refresh token expired")
        if not self._requested_resource_ok(form, grant.resource):
            return oauth_error(400, "invalid_target", "resource does not match the grant")
        minted = self._mint(grant.id, now)
        if not self.store.rotate_refresh(token, minted.access_row, minted.refresh_row, now, grace_start):
            return oauth_error(400, "invalid_grant", "refresh token no longer valid")
        self.audit.event(
            "oauth.token_refreshed", client=client.name, resource=self._backend_name(grant.resource),
            grant=grant.id[:8], retry=token.rotated_at is not None or None, src=ctx.source,
        )
        return self._token_response(minted)

    # -- /revoke ----------------------------------------------------------------------------

    async def revoke(self, request: Request) -> Response:
        ctx = net_context(request)
        result = await self._client_request(request, ctx, "revoke")
        if isinstance(result, Response):
            return result
        form, client = result
        try:
            presented = form_single(form, "token")
        except DuplicateParameter:
            return oauth_error(400, "invalid_request", "duplicate parameter")
        if presented:
            now = int(time.time())
            token = self.store.get_token(hash_token(presented))
            grant = self.store.get_grant(token.grant_id) if token else None
            if token and grant and grant.client_id == client.client_id:
                if token.kind == "refresh":
                    self.store.revoke_grant(grant.id, "revoked by client", now)
                else:
                    self.store.revoke_token(token.id, now)
                self.audit.event("oauth.token_revoked", client=client.name, kind=token.kind, grant=grant.id[:8])
        return Response(status_code=200, headers=NO_STORE)
