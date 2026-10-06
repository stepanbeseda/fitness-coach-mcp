"""Shared-secret bearer check between the gateway and this backend (fail closed)."""
# Copy of garmin-mcp/src/garmin_coach_mcp/auth.py; keep both in sync.

from __future__ import annotations

import hmac
import json
import logging
from pathlib import Path

from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("coach_mcp.auth")


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
    """Requires ``Authorization: Bearer <token>`` on every path under ``prefix``."""

    def __init__(self, app: ASGIApp, token: BackendToken, prefix: str = "/mcp") -> None:
        self.app = app
        self.token = token
        self.prefix = prefix

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
        if scheme.lower() != b"bearer" or not hmac.compare_digest(presented.strip(), expected):
            await _reject(send, 401, "unauthorized", www_authenticate=True)
            return
        await self.app(scope, receive, send)


async def _reject(send: Send, status: int, message: str, *, www_authenticate: bool = False) -> None:
    body = json.dumps({"error": message}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if www_authenticate:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
