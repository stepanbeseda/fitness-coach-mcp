"""Read-only client for hevy-mcp: allowlisted read tools over MCP.

The nightly history job calls ``HISTORY_TOOLS`` with the internal token hevy-mcp already accepts from the
gateway. Nothing outside the allowlist can be called, so hevy-mcp's write tools (create-*, update-*,
replace-*, feedback) are unreachable from here. hevy-mcp has one full-access token, so this allowlist is the
only thing that keeps the coach from writing to Hevy. Logs nothing: the history job logs one line per run.
"""

from __future__ import annotations

from typing import Any, Protocol

import anyio
import httpx2
import mcp.types
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from .config import Settings

HISTORY_TOOLS = ("get-workout-events", "get-exercise-template")
# Every hevy-mcp tool the coach may call; hevy-mcp's write tools are not in it.
ALLOWED_TOOLS = HISTORY_TOOLS
# A Hevy 429 is recognised from two sources: the structured error of hevy-mcp (``error.status`` 429, which is
# what its retried GET tools answer after four attempts) and these texts of calls hevy-mcp does not retry:
# "Rate limited by Hevy. Please wait and retry." and "Rate limited by Hevy (HTTP 429). ...".
RATE_LIMIT_HINT = "Rate limited by Hevy"
# hevy-mcp's text for HTTP 401/403 from the Hevy API (the structured ``error.status`` is checked first).
AUTH_HINT = "The Hevy API key is invalid or has expired"
MIN_TOKEN_LENGTH = 16


class StrengthSource(Protocol):
    async def history_call(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]: ...


def _error_status(result: mcp.types.CallToolResult) -> int | None:
    """``error.status`` of hevy-mcp's structured error content, when it is an int."""
    structured = result.structured_content
    err = structured.get("error") if isinstance(structured, dict) else None
    status = err.get("status") if isinstance(err, dict) else None
    return status if isinstance(status, int) and not isinstance(status, bool) else None


class HevyReader:
    def __init__(self, settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport  # for tests only

    def _token(self) -> str | None:
        try:
            token = self._settings.hevy_token_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return token if len(token) >= MIN_TOKEN_LENGTH else None

    async def history_call(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """One history tool call -> (outcome, payload); the payload is a dict only for ``ok``.

        Outcomes: ok, rate_limited, auth_error, error (the backend answered but not usably),
        unreachable, no_credentials. Only the structured content is used: hevy-mcp puts a bare list (or a
        sentence) into the text and the pagination only into the structured content.
        """
        if name not in HISTORY_TOOLS:
            raise ValueError(f"tool {name!r} is not allowed")
        token = self._token()
        if token is None:
            return "no_credentials", None
        try:
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
                timeout=httpx2.Timeout(30.0, read=90.0),
                transport=self._transport,
            ) as http:
                with anyio.fail_after(180):
                    async with Client(
                        streamable_http_client(self._settings.hevy_url, http_client=http),
                        read_timeout_seconds=90,
                    ) as client:
                        result = await client.call_tool(name, args)
        except Exception:  # never BaseException: a cancel (shutdown) must propagate
            return "unreachable", None
        if result.is_error:
            text = "".join(c.text for c in result.content if isinstance(c, mcp.types.TextContent))
            status = _error_status(result)
            if status == 429 or RATE_LIMIT_HINT in text:
                return "rate_limited", None
            if status in (401, 403) or AUTH_HINT in text:
                return "auth_error", None
            return "error", None
        structured = result.structured_content
        if not isinstance(structured, dict):
            return "error", None
        return "ok", structured
