"""Shared-secret bearer checks of this backend: the gateway (full access) and coach-mcp (read-only).

Fail closed: without a usable gateway token every MCP request gets 503. coach-mcp keeps a
single-token copy of this file (coach-mcp/src/coach_mcp/auth.py) for its own inbound check.
"""

from __future__ import annotations

import hmac
import json
import logging
from pathlib import Path

from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("garmin_coach_mcp.auth")

# ASGI scope key with the role of the presented token, read by the tool-call guard (access.py).
ROLE_KEY = "garmin_coach_mcp.role"
ROLE_FULL = "full"
ROLE_READ_ONLY = "read_only"


class BackendToken:
    """Reads the expected token from a (Docker secret) file, re-reading on change."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cached: bytes | None = None
        self._mtime: float | None = None

    def value(self) -> bytes | None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._cached, self._mtime = None, None
            return None
        if mtime != self._mtime:
            try:
                token = self.path.read_text(encoding="utf-8").strip()
            except OSError:
                token = ""
            self._cached = token.encode() if len(token) >= 16 else None
            self._mtime = mtime
        return self._cached


class BearerAuthMiddleware:
    """Requires ``Authorization: Bearer <token>`` on every path under ``prefix``.

    ``token`` is the gateway's (full access). ``readonly_token`` is optional: while its file is
    missing or too short it is simply not accepted. The matching role goes into the scope under
    ``ROLE_KEY``; a value equal to both tokens counts as read-only.
    """

    def __init__(
        self,
        app: ASGIApp,
        token: BackendToken,
        prefix: str = "/mcp",
        *,
        readonly_token: BackendToken | None = None,
    ) -> None:
        self.app = app
        self.token = token
        self.prefix = prefix
        self.readonly_token = readonly_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.prefix):
            await self.app(scope, receive, send)
            return
        expected = self.token.value()
        if expected is None:
            log.error("backend token file missing or too short: refusing MCP requests")
            await _reject(send, 503, "backend token not configured")
            return
        header = next((v for k, v in scope["headers"] if k == b"authorization"), b"")
        scheme, _, presented = header.partition(b" ")
        presented = presented.strip()
        readonly = self.readonly_token.value() if self.readonly_token is not None else None
        is_readonly = readonly is not None and hmac.compare_digest(presented, readonly)
        is_full = hmac.compare_digest(presented, expected)
        if scheme.lower() != b"bearer" or not (is_readonly or is_full):
            await _reject(send, 401, "unauthorized", www_authenticate=True)
            return
        role = ROLE_READ_ONLY if is_readonly else ROLE_FULL
        await self.app({**scope, ROLE_KEY: role}, receive, send)


async def _reject(send: Send, status: int, message: str, *, www_authenticate: bool = False) -> None:
    body = json.dumps({"error": message}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if www_authenticate:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
