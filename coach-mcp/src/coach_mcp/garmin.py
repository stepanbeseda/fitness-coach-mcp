"""Read-only client for garmin-mcp: allowlisted read tools over MCP.

The morning briefing calls ``BRIEFING_TOOLS``, the nightly history job ``HISTORY_TOOLS``. The
coach authenticates with its own read-only token (garmin-mcp refuses it every tool not annotated
read-only) and calls nothing outside ``ALLOWED_TOOLS``.
Log lines carry outcome words, durations and exception class names only.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

import anyio
import httpx2
import mcp.types
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from .config import Settings

log = logging.getLogger("coach_mcp.garmin")

BRIEFING_TOOLS = ("get_recovery_snapshot", "get_hrv_trend", "list_scheduled_workouts")
HISTORY_TOOLS = ("get_recovery_snapshot", "list_activities", "get_race_predictions")
# Every garmin-mcp tool the coach may call; garmin-mcp's write tools are not in it.
ALLOWED_TOOLS = (
    "get_recovery_snapshot",
    "get_hrv_trend",
    "list_scheduled_workouts",
    "list_activities",
    "get_race_predictions",
)
# garmin-mcp's garmin_client.py: "Garmin Connect rate limit (HTTP 429). Wait a few minutes and retry."
RATE_LIMIT_HINT = "HTTP 429"
# Both NOT_LOGGED_IN and SESSION_EXPIRED in garmin-mcp's garmin_client.py contain this hint.
GARMIN_LOGIN_HINT = "scripts/garmin-login.sh"
MIN_TOKEN_LENGTH = 16
AUTH_VALUES = ("ok", "missing", "error")


def exception_kind(exc: BaseException) -> str:
    """Class name of the first leaf of an (exception) group."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return type(exc).__name__


@dataclass(frozen=True)
class GarminData:
    # "ok" | "missing" | "error" from garmin-mcp /healthz; None: not reachable or no token.
    auth: str | None
    calls: Mapping[str, str] = field(default_factory=dict)  # tool name -> ok | auth_error | error
    snapshot: dict[str, Any] | None = None
    hrv_trend: dict[str, Any] | None = None
    scheduled: dict[str, Any] | None = None
    # The coach itself has no usable garmin-mcp token (a configuration problem, not a Garmin login).
    credentials_missing: bool = False

    @property
    def auth_problem(self) -> bool:
        return self.auth in ("missing", "error") or "auth_error" in self.calls.values()


class GarminSource(Protocol):
    async def fetch(self, day: date) -> GarminData: ...


class HistorySource(Protocol):
    async def auth_state(self) -> str | None: ...

    async def history_call(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]: ...


async def _call(client: Client, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """One allowlisted tool call -> (outcome, payload); the payload is a dict only for ``ok``."""
    if name not in ALLOWED_TOOLS:
        raise ValueError(f"tool {name!r} is not allowed")
    try:
        result = await client.call_tool(name, args)
        text = "".join(c.text for c in result.content if isinstance(c, mcp.types.TextContent))
        if result.is_error:
            return ("auth_error" if GARMIN_LOGIN_HINT in text else "error"), None
        payload = json.loads(text)
    except Exception:
        return "error", None
    return ("ok", payload) if isinstance(payload, dict) else ("error", None)


class GarminReader:
    def __init__(self, settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport  # for tests only

    def _token(self) -> str | None:
        try:
            token = self._settings.garmin_token_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return token if len(token) >= MIN_TOKEN_LENGTH else None

    async def _auth(self, http: httpx2.AsyncClient) -> str | None:
        base = self._settings.garmin_url.removesuffix("/mcp")
        try:
            response = await http.get(f"{base}/healthz")
            value = response.json().get("garmin_auth") if response.status_code == 200 else None
        except Exception:
            return None
        return value if value in AUTH_VALUES else None

    async def auth_state(self) -> str | None:
        """garmin-mcp's ``garmin_auth`` from /healthz ("ok", "missing", "error"); None when not reachable."""
        async with httpx2.AsyncClient(timeout=httpx2.Timeout(30.0), transport=self._transport) as http:
            return await self._auth(http)

    async def history_call(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """One history tool call -> (outcome, payload); the payload is a dict only for ``ok``.

        Outcomes: ok, rate_limited, auth_error, error (the backend answered but not usably),
        unreachable, no_credentials. Logs nothing: the history job logs one line per run.
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
                        streamable_http_client(self._settings.garmin_url, http_client=http),
                        read_timeout_seconds=90,
                    ) as client:
                        result = await client.call_tool(name, args)
        except Exception:  # never BaseException: a cancel (shutdown) must propagate
            return "unreachable", None
        text = "".join(c.text for c in result.content if isinstance(c, mcp.types.TextContent))
        if result.is_error:
            if RATE_LIMIT_HINT in text:
                return "rate_limited", None
            return ("auth_error" if GARMIN_LOGIN_HINT in text else "error"), None
        try:
            payload = json.loads(text)
        except ValueError:
            return "error", None
        if not isinstance(payload, dict):
            return "error", None
        # garmin-mcp turns a 429 of one library call into an error section while the tool itself succeeds.
        for value in payload.values():
            if isinstance(value, dict) and RATE_LIMIT_HINT in str(value.get("error", "")):
                return "rate_limited", None
        return "ok", payload

    async def fetch(self, day: date) -> GarminData:
        started = time.monotonic()
        outcomes = dict.fromkeys(BRIEFING_TOOLS, "error")
        token = self._token()
        if token is None:
            log.warning("garmin status=error kind=no_credentials")
            return GarminData(auth=None, calls=outcomes, credentials_missing=True)

        arguments: dict[str, dict[str, Any]] = {
            "get_recovery_snapshot": {"date": day.isoformat()},
            "get_hrv_trend": {"days": 7, "end_date": day.isoformat()},
            "list_scheduled_workouts": {
                "start_date": (day - timedelta(days=1)).isoformat(),
                "end_date": day.isoformat(),
            },
        }
        payloads: dict[str, dict[str, Any] | None] = dict.fromkeys(BRIEFING_TOOLS)
        failure = ""
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx2.Timeout(30.0, read=90.0),
            transport=self._transport,
        ) as http:
            auth = await self._auth(http)
            if auth is not None:
                try:
                    with anyio.fail_after(180):
                        async with Client(
                            streamable_http_client(self._settings.garmin_url, http_client=http),
                            read_timeout_seconds=90,
                        ) as client:
                            for name in BRIEFING_TOOLS:
                                outcomes[name], payloads[name] = await _call(client, name, arguments[name])
                except Exception as exc:  # never BaseException: a cancel (shutdown) must propagate
                    failure = f" kind={exception_kind(exc)}"

        data = GarminData(
            auth=auth,
            calls=outcomes,
            snapshot=payloads["get_recovery_snapshot"],
            hrv_trend=payloads["get_hrv_trend"],
            scheduled=payloads["list_scheduled_workouts"],
        )
        if auth is None:
            status = "unreachable"
        elif data.auth_problem:
            status = "auth"
        elif all(v == "ok" for v in outcomes.values()):
            status = "ok"
        else:
            status = "partial"
        log.info("garmin status=%s ms=%.0f%s", status, (time.monotonic() - started) * 1000, failure)
        return data
