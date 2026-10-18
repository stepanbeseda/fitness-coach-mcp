"""Read-only client for garmin-mcp: allowlisted read tools over MCP.

The morning briefing calls ``BRIEFING_TOOLS`` and, on a retry, only ``SYNC_TOOL``, the nightly history job
``HISTORY_TOOLS`` (the daily race predictions, ``get_race_predictions``, are one of them; PR 14). The
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
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

import anyio
import httpx2
import mcp.types
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from .config import Settings

log = logging.getLogger("coach_mcp.garmin")

BRIEFING_TOOLS = ("get_device_status", "get_recovery_snapshot", "get_hrv_trend", "list_scheduled_workouts")
SYNC_TOOL = "get_device_status"
HISTORY_TOOLS = ("get_recovery_snapshot", "list_activities", "get_race_predictions")
# Every garmin-mcp tool the coach may call; garmin-mcp's write tools are not in it.
ALLOWED_TOOLS = (
    "get_device_status",
    "get_recovery_snapshot",
    "get_hrv_trend",
    "list_scheduled_workouts",
    "list_activities",
    "get_race_predictions",
)
# garmin-mcp's error kinds (garmin-mcp/src/garmin_coach_mcp/errors.py): a tool error carries its kind in `_meta`
# under this key, an error section next to its text as "error_kind". The coach reads two of them.
ERROR_KIND_KEY = "local.garmin-coach-mcp/error_kind"
RATE_LIMITED_KIND = "rate_limited"
GARMIN_AUTH_KIND = "garmin_auth"
# The fallback for an answer without a kind (a garmin-mcp older than PR 31): garmin-mcp's RATE_LIMIT text, and
# NOT_LOGGED_IN and SESSION_EXPIRED in its garmin_client.py contain these hints.
RATE_LIMIT_HINT = "HTTP 429"
GARMIN_LOGIN_HINT = "scripts/garmin-login.sh"
MIN_TOKEN_LENGTH = 16
AUTH_VALUES = ("ok", "missing", "error")
SYNC_YEARS = (2000, 2100)


def exception_kind(exc: BaseException) -> str:
    """Class name of the first leaf of an (exception) group."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return type(exc).__name__


@dataclass(frozen=True)
class LastSync:
    utc: datetime  # aware, UTC, to the second: compared between reads
    local: datetime  # naive wall time in garmin-mcp's TZ (the same ATHLETE_TZ as the coach): shown to the athlete


def parse_last_sync(device: Any) -> LastSync | None:
    """``last_upload`` of a get_device_status payload; None when absent, unavailable, malformed or outside 2000-2100."""
    section = device.get("last_upload") if isinstance(device, Mapping) else None
    if not isinstance(section, Mapping):
        return None
    raw_utc, raw_local = section.get("time_utc"), section.get("time_local")
    if not isinstance(raw_utc, str) or not isinstance(raw_local, str):
        return None
    try:
        utc_time = datetime.strptime(raw_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        local_time = datetime.strptime(raw_local, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    low, high = SYNC_YEARS
    if not (low <= utc_time.year <= high and low <= local_time.year <= high):
        return None
    return LastSync(utc_time, local_time)


@dataclass(frozen=True)
class GarminData:
    # "ok" | "missing" | "error" from garmin-mcp /healthz; None: not reachable or no token.
    auth: str | None
    calls: Mapping[str, str] = field(default_factory=dict)  # tool name -> ok | auth_error | error
    snapshot: dict[str, Any] | None = None
    hrv_trend: dict[str, Any] | None = None
    scheduled: dict[str, Any] | None = None
    device: dict[str, Any] | None = None  # get_device_status: when Garmin last received data
    # The coach itself has no usable garmin-mcp token (a configuration problem, not a Garmin login).
    credentials_missing: bool = False

    @property
    def auth_problem(self) -> bool:
        return self.auth in ("missing", "error") or "auth_error" in self.calls.values()

    @property
    def last_sync(self) -> LastSync | None:
        return parse_last_sync(self.device)


class GarminSource(Protocol):
    async def fetch(self, day: date) -> GarminData: ...

    async def sync_check(self) -> LastSync | None: ...


class HistorySource(Protocol):
    async def auth_state(self) -> str | None: ...

    async def history_call(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]: ...


def error_outcome(result: mcp.types.CallToolResult) -> str:
    """rate_limited, auth_error or error for an error result: by its kind, by its text when it has none."""
    meta = result.meta if isinstance(result.meta, Mapping) else {}
    kind = meta.get(ERROR_KIND_KEY)
    if isinstance(kind, str):
        return {RATE_LIMITED_KIND: "rate_limited", GARMIN_AUTH_KIND: "auth_error"}.get(kind, "error")
    text = "".join(c.text for c in result.content if isinstance(c, mcp.types.TextContent))
    if RATE_LIMIT_HINT in text:
        return "rate_limited"
    return "auth_error" if GARMIN_LOGIN_HINT in text else "error"


def section_rate_limited(section: Any) -> bool:
    """An error section of a 429: by its kind, by its text when it has none."""
    if not isinstance(section, Mapping) or "error" not in section:
        return False
    kind = section.get("error_kind")
    if isinstance(kind, str):
        return kind == RATE_LIMITED_KIND
    return RATE_LIMIT_HINT in str(section.get("error", ""))


async def _call(client: Client, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """One allowlisted tool call -> (outcome, payload); the payload is a dict only for ``ok``."""
    if name not in ALLOWED_TOOLS:
        raise ValueError(f"tool {name!r} is not allowed")
    try:
        result = await client.call_tool(name, args)
        if result.is_error:
            return ("auth_error" if error_outcome(result) == "auth_error" else "error"), None
        text = "".join(c.text for c in result.content if isinstance(c, mcp.types.TextContent))
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
        if result.is_error:
            return error_outcome(result), None
        text = "".join(c.text for c in result.content if isinstance(c, mcp.types.TextContent))
        try:
            payload = json.loads(text)
        except ValueError:
            return "error", None
        if not isinstance(payload, dict):
            return "error", None
        # garmin-mcp turns a 429 of one library call into an error section while the tool itself succeeds.
        if any(section_rate_limited(value) for value in payload.values()):
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
            "get_device_status": {},
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
            device=payloads["get_device_status"],
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

    async def sync_check(self) -> LastSync | None:
        """The cheap retry check: only get_device_status (2 Garmin Connect requests, no /healthz).

        None when there is no token, garmin-mcp does not answer, the tool fails or the time is unknown.
        """
        started = time.monotonic()
        token = self._token()
        outcome, payload = "no_credentials", None
        if token is not None:
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
                            outcome, payload = await _call(client, SYNC_TOOL, {})
            except Exception:  # never BaseException: a cancel (shutdown) must propagate
                outcome = "unreachable"
        last = parse_last_sync(payload) if outcome == "ok" else None
        status = "ok" if last is not None else ("unknown" if outcome == "ok" else outcome)
        log.info("garmin sync_check status=%s ms=%.0f", status, (time.monotonic() - started) * 1000)
        return last
