"""Starlette application: routing, middleware and lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .audit import Audit
from .config import Backend, Settings, read_secret
from .netctx import net_context
from .oauth import OAuthServer
from .proxy import McpProxy
from .ratelimit import RateLimiter
from .security import LoginGuard, PasswordVerifier, ThrottlePolicy, TotpVerifier
from .store import Store

http_log = logging.getLogger("gateway.http")


class EdgeMiddleware:
    """Per-IP limit for Funnel traffic, baseline security headers and a request
    log without query strings (they may contain OAuth state)."""

    def __init__(self, app: ASGIApp, public_limiter: RateLimiter) -> None:
        self.app = app
        self.public_limiter = public_limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        ctx = net_context(request)
        started = time.monotonic()
        status_holder = {"status": 0}

        if ctx.via_funnel and (retry := self.public_limiter.hit(f"ip:{ctx.client_ip}")):
            response = JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": str(retry)})
            await response(scope, receive, send)
            http_log.info("%s %s 429 src=%s ip=%s", scope["method"], scope["path"], ctx.source, ctx.client_ip)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers", []))
                names = {name.lower() for name, _ in headers}
                for name, value in (
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                ):
                    if name not in names:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            http_log.info(
                "%s %s %s %dms src=%s%s",
                scope["method"],
                scope["path"],
                status_holder["status"],
                round((time.monotonic() - started) * 1000),
                ctx.source,
                f" ip={ctx.client_ip}" if ctx.via_funnel else "",
            )


async def _not_found(request: Request, exc: Exception) -> Response:
    return JSONResponse({"error": "not_found"}, status_code=404)


async def _method_not_allowed(request: Request, exc: Exception) -> Response:
    return JSONResponse({"error": "method_not_allowed"}, status_code=405)


def create_app(
    settings: Settings,
    *,
    upstream_client: httpx.AsyncClient | None = None,
    store: Store | None = None,
    audit: Audit | None = None,
) -> Starlette:
    store = store or Store(settings.db_path)
    audit = audit or Audit(settings.audit_log_path)
    guard = LoginGuard(
        store,
        PasswordVerifier(read_secret(settings.password_hash_file)),
        TotpVerifier(read_secret(settings.totp_secret_file)),
        ThrottlePolicy(
            settings.login_max_failures,
            settings.login_failure_window,
            settings.login_lockout_base,
            settings.login_lockout_max,
        ),
    )
    client = upstream_client or httpx.AsyncClient(
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        follow_redirects=False,
        trust_env=False,
    )
    oauth = OAuthServer(settings, store, audit, guard)
    proxy = McpProxy(settings, store, audit, oauth, client)

    async def healthz(request: Request) -> Response:
        if net_context(request).via_funnel:
            return JSONResponse({"error": "not_found"}, status_code=404)
        store.get_login_state()
        return JSONResponse({"status": "ok"})

    def prm_endpoint(backend: Backend) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            return oauth.protected_resource_metadata(backend)

        return endpoint

    def mcp_endpoint(backend: Backend) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            return await proxy.handle(request, backend)

        return endpoint

    async def as_metadata(request: Request) -> Response:
        return oauth.authorization_server_metadata(request)

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", as_metadata, methods=["GET"]),
        *[Route(b.metadata_path, prm_endpoint(b), methods=["GET"]) for b in settings.backends],
        Route("/authorize", oauth.authorize, methods=["GET", "POST"]),
        Route("/token", oauth.token, methods=["POST"]),
        Route("/revoke", oauth.revoke, methods=["POST"]),
        *[
            Route(b.path, mcp_endpoint(b), methods=["GET", "POST", "DELETE", "OPTIONS"])
            for b in settings.backends
        ],
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async def housekeeping() -> None:
            while True:
                try:
                    store.cleanup(int(time.time()))
                except Exception:  # pragma: no cover - logged and retried
                    logging.getLogger("gateway").exception("cleanup failed")
                await asyncio.sleep(3600)

        task = asyncio.create_task(housekeeping())
        audit.event("gateway.started", backends=[b.name for b in settings.backends])
        try:
            yield
        finally:
            task.cancel()
            await client.aclose()

    app = Starlette(
        routes=routes,
        exception_handlers={404: _not_found, 405: _method_not_allowed},
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.audit = audit
    public = settings.rate_public_per_ip
    app.add_middleware(EdgeMiddleware, public_limiter=RateLimiter(public.per_minute, public.burst))
    return app
