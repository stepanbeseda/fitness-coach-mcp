"""Fakes for the briefing tests: Garmin source, push transport, clock and a fake garmin-mcp app."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from coach_mcp.auth import BearerAuthMiddleware
from coach_mcp.garmin import GarminData
from coach_mcp.push import PushMessage

from garmin_payloads import GREEN_SCHEDULED, GREEN_SNAPSHOT, GREEN_TREND


class StopLoop(BaseException):
    """Raised by FakeClock to end ``BriefingScheduler.run``; a BaseException so ``except Exception`` lets it pass."""


class FakeClock:
    def __init__(
        self, start: datetime, stop_when: Callable[[], bool] = lambda: False, max_sleeps: int = 2000
    ) -> None:
        assert start.tzinfo is not None
        self._instant = start.astimezone(UTC)  # a UTC instant: wall-clock arithmetic would be wrong across DST
        self._stop_when = stop_when
        self._max_sleeps = max_sleeps
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._instant

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self._stop_when() or len(self.sleeps) >= self._max_sleeps:
            raise StopLoop
        self._instant += timedelta(seconds=seconds)


class FakeGarmin:
    """``data`` is a GarminData, an exception to raise, or a callable ``(day, call_index)`` returning either."""

    def __init__(self, data: GarminData | Callable[[date, int], GarminData | Exception] | Exception) -> None:
        self._data = data
        self.days: list[date] = []
        self.calls = 0

    async def fetch(self, day: date) -> GarminData:
        index = self.calls
        self.calls += 1
        self.days.append(day)
        result = self._data(day, index) if callable(self._data) else self._data
        if isinstance(result, Exception):
            raise result
        return result


class FakePush:
    def __init__(self, outcomes: list[str] | None = None, clock: FakeClock | None = None) -> None:
        self._outcomes = list(outcomes or ["sent"])
        self._clock = clock
        self.sent: list[tuple[datetime | None, PushMessage]] = []

    async def send(self, msg: PushMessage) -> Any:
        self.sent.append((self._clock.now() if self._clock else None, msg))
        index = min(len(self.sent) - 1, len(self._outcomes) - 1)
        return self._outcomes[index]


class _StaticToken:
    def __init__(self, token: str) -> None:
        self._token = token.encode()

    def value(self) -> bytes:
        return self._token


def fake_garmin_app(
    token: str,
    auth: str = "ok",
    errors: dict[str, str] | None = None,
    hang: frozenset[str] = frozenset(),
) -> tuple[BearerAuthMiddleware, list[tuple[str, dict[str, Any]]]]:
    """A garmin-mcp look-alike (Streamable HTTP + /healthz behind the bearer check) and its call log."""
    errors = errors or {}
    calls: list[tuple[str, dict[str, Any]]] = []
    server = MCPServer("fake-garmin")

    async def respond(name: str, arguments: dict[str, Any], payload: Any) -> str:
        calls.append((name, {k: v for k, v in arguments.items() if v is not None}))
        if name in hang:
            await anyio.sleep_forever()
        if name in errors:
            raise ToolError(errors[name])
        return json.dumps(payload)

    async def get_recovery_snapshot(date: str | None = None) -> str:
        return await respond("get_recovery_snapshot", {"date": date}, GREEN_SNAPSHOT)

    async def get_hrv_trend(days: int = 7, end_date: str | None = None) -> str:
        return await respond("get_hrv_trend", {"days": days, "end_date": end_date}, GREEN_TREND)

    async def list_scheduled_workouts(start_date: str | None = None, end_date: str | None = None) -> str:
        return await respond(
            "list_scheduled_workouts", {"start_date": start_date, "end_date": end_date}, GREEN_SCHEDULED
        )

    async def create_workout() -> str:
        return await respond("create_workout", {}, {"created": True})

    for fn in (get_recovery_snapshot, get_hrv_trend, list_scheduled_workouts, create_workout):
        server.add_tool(fn, name=fn.__name__, description=fn.__name__, structured_output=False)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "garmin_auth": auth})

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host="0.0.0.0",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False, allowed_hosts=[], allowed_origins=[]
        ),
    )
    return BearerAuthMiddleware(app, _StaticToken(token), prefix="/mcp"), calls  # type: ignore[arg-type]


@asynccontextmanager
async def fake_garmin_running(app: BearerAuthMiddleware) -> AsyncIterator[None]:
    inner = app.app
    async with inner.router.lifespan_context(inner):  # type: ignore[attr-defined]
        yield
