"""MCP server definition: coach memory tools, the daily briefing and the stored history."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date, datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from . import __version__, tools, trends
from .auth import BackendToken, BearerAuthMiddleware
from .config import Settings
from .garmin import GarminReader, GarminSource, HistorySource, exception_kind
from .history import HistoryJob
from .push import PushSender, TelegramPush
from .scheduler import BriefingScheduler, compose_briefing
from .store import Store
from .tools import PlannedSession, to_json

log = logging.getLogger("coach_mcp.tools")
briefing_log = logging.getLogger("coach_mcp.briefing")
history_log = logging.getLogger("coach_mcp.history")

INSTRUCTIONS = (
    "Coach memory of the athlete: goals, constraints (injuries, limitations, availability, equipment), "
    "the training plan by date and a notes log. Call get_coach_context at the start of every planning "
    "conversation; still read fresh Garmin and Hevy data. "
    "Dates are YYYY-MM-DD in the athlete's local time zone. "
    "set_goal, set_constraint, save_plan, update_session and add_note change the stored state: show the "
    "proposal and get explicit confirmation first. "
    "Nothing is deleted: goals end as achieved or abandoned, constraints as resolved, replaced sessions "
    "as superseded. "
    "get_daily_briefing returns the morning verdict (green / yellow / red) with its numbers, missing data, "
    "today's planned sessions and alerts, computed the same way as the morning push from the current Garmin "
    "data; still read fresh Garmin data before planning. "
    "get_trends returns the stored daily Garmin history (collected nightly) aggregated by day, week or month: "
    "use it for anything older than 28 days. get_weekly_report compares one ISO week with the previous four: "
    "use it for weekly reviews. Strength history from Hevy is not stored yet. "
    "Past planned sessions carry a suggestion from the stored Garmin activities (matched with the day's totals "
    "of that sport, or no_activity; final false while a late watch sync can still change it); "
    "get_coach_context lists those of the last 7 days in to_confirm. A suggestion never changes a status: "
    "check it with the athlete and confirm it with update_session."
)

GoalStatus = Literal["active", "achieved", "abandoned"]
ConstraintKind = Literal["injury", "limitation", "availability", "equipment", "other"]
ConstraintStatus = Literal["active", "resolved"]
SessionStatus = Literal["planned", "done", "skipped", "moved"]


def _read(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, read_only_hint=True, open_world_hint=False)


def _read_open(title: str) -> ToolAnnotations:
    """A read-only tool that reaches an outside service (here: garmin-mcp)."""
    return ToolAnnotations(title=title, read_only_hint=True, open_world_hint=True)


def _write(title: str, *, idempotent: bool = False) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=idempotent,
        open_world_hint=False,
    )


def _logged(name: str, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[str]]:
    """Log tool name, outcome and duration only (never arguments or results)."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        started = time.monotonic()
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            log.info("tool=%s status=error kind=%s ms=%.0f", name, type(exc).__name__, (time.monotonic() - started) * 1000)
            raise
        log.info("tool=%s status=ok ms=%.0f", name, (time.monotonic() - started) * 1000)
        return to_json(result)

    return wrapper


def build_server(
    store: Store,
    settings: Settings,
    today: Callable[[], date],
    *,
    garmin: GarminSource,
    scheduler: BriefingScheduler | None,
    history: HistoryJob | None = None,
) -> MCPServer:
    state = {"scheduler": "on" if scheduler is not None else "off", "history": "on" if history is not None else "off"}

    def on_scheduler_done(done: asyncio.Task[None]) -> None:
        if done.cancelled():  # the normal shutdown
            return
        state["scheduler"] = "stopped"
        exc = done.exception()  # also marks the exception as retrieved
        briefing_log.error("briefing status=stopped kind=%s", exception_kind(exc) if exc is not None else "returned")

    def on_history_done(done: asyncio.Task[None]) -> None:
        if done.cancelled():  # the normal shutdown
            return
        state["history"] = "stopped"
        exc = done.exception()  # also marks the exception as retrieved
        history_log.error("history status=stopped kind=%s", exception_kind(exc) if exc is not None else "returned")

    @contextlib.asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        tasks: list[asyncio.Task[None]] = []
        if scheduler is not None:
            tasks.append(asyncio.create_task(scheduler.run()))
            tasks[-1].add_done_callback(on_scheduler_done)
        if history is not None:
            tasks.append(asyncio.create_task(history.run()))
            tasks[-1].add_done_callback(on_history_done)
        try:
            yield None
        finally:
            running = [task for task in tasks if not task.done()]  # a task that already ended is not awaited
            for task in running:
                task.cancel()
            for task in running:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    server = MCPServer(
        name="coach-memory",
        title="Coach memory (fitness coach)",
        instructions=INSTRUCTIONS,
        version=__version__,
        lifespan=lifespan,
        log_level=settings.log_level if settings.log_level in ("DEBUG", "INFO", "WARNING", "ERROR") else "INFO",
    )

    def tool(name: str, annotations: ToolAnnotations) -> Callable[[Callable[..., Awaitable[Any]]], Any]:
        def register(fn: Callable[..., Awaitable[Any]]) -> Any:
            server.add_tool(
                _logged(name, fn),
                name=name,
                title=annotations.title,
                description=" ".join(inspect.cleandoc(fn.__doc__ or "").split()),
                annotations=annotations,
                structured_output=False,
            )
            return fn

        return register

    @tool("get_coach_context", _read("Coach context"))
    async def get_coach_context() -> Any:
        """Everything the coach should remember, in one call: active goals with days remaining, active
        constraints (injuries, limitations, availability, equipment), this week's planned and completed
        sessions, the planned sessions of the last 7 days that carry a suggestion (to_confirm) and the
        latest 10 notes. Call it first in every planning conversation."""
        return tools.get_coach_context(store, today())

    @tool("get_plan", _read("Training plan"))
    async def get_plan(
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD; default Monday of this ISO week.")] = None,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD; default start_date + 6 days (max range 92 days).")] = None,
        include_superseded: Annotated[bool, Field(description="Also list sessions replaced by a later plan.")] = False,
    ) -> Any:
        """Planned sessions in a date range (default: this ISO week, max 92 days) with an adherence
        summary: planned, done, skipped and moved counts, overdue planned sessions and the completion rate
        done / (done + skipped). Superseded sessions are hidden unless include_superseded is true. Past planned
        sessions carry a suggestion (matched or no_activity, from the nightly Garmin history); confirm it with
        update_session."""
        return tools.get_plan(
            store, today(), start_date=start_date, end_date=end_date, include_superseded=include_superseded
        )

    @tool("set_goal", _write("Set goal"))
    async def set_goal(
        goal_id: Annotated[int | None, Field(gt=0, description="Omit to create a goal; give it to change one.")] = None,
        title: Annotated[str | None, Field(max_length=200, description="Goal title (required when creating).")] = None,
        target: Annotated[str | None, Field(max_length=200, description="Target in words, e.g. 'sub 1:45'.")] = None,
        target_date: Annotated[str | None, Field(description="YYYY-MM-DD; empty string clears it.")] = None,
        target_result: Annotated[str | None, Field(max_length=200, description="Measurable result, e.g. '1:45:00'.")] = None,
        status: Annotated[GoalStatus | None, Field(description="active (default), achieved or abandoned.")] = None,
    ) -> Any:
        """Create a goal (omit goal_id) or change one (give goal_id and only the fields to change; an empty
        string clears target, target_date or target_result). Close goals with status achieved or
        abandoned; nothing is deleted. Only after the athlete confirmed."""
        return tools.set_goal(
            store,
            today(),
            goal_id=goal_id,
            title=title,
            target=target,
            target_date=target_date,
            target_result=target_result,
            status=status,
        )

    @tool("set_constraint", _write("Set constraint"))
    async def set_constraint(
        constraint_id: Annotated[int | None, Field(gt=0, description="Omit to create; give it to change one.")] = None,
        kind: Annotated[ConstraintKind | None, Field(description="Required when creating.")] = None,
        title: Annotated[str | None, Field(max_length=200, description="Short title (required when creating).")] = None,
        details: Annotated[str | None, Field(max_length=2000, description="Free-text details; empty string clears it.")] = None,
        status: Annotated[ConstraintStatus | None, Field(description="active (default) or resolved.")] = None,
    ) -> Any:
        """Create a constraint (omit constraint_id; kind and title required) or change one; set status
        resolved when it no longer applies. Kinds: injury, limitation, availability, equipment, other. Only
        after the athlete confirmed."""
        return tools.set_constraint(
            store, today(), constraint_id=constraint_id, kind=kind, title=title, details=details, status=status
        )

    @tool("save_plan", _write("Save week plan", idempotent=True))
    async def save_plan(
        week: Annotated[str, Field(description="Any date (YYYY-MM-DD) inside the ISO week to plan.")],
        sessions: Annotated[
            list[PlannedSession], Field(max_length=30, description="The sessions of that week; may be empty.")
        ],
    ) -> Any:
        """Save the confirmed plan for one ISO week (week = any date in it). Planned sessions of that week
        from today on are replaced (kept as superseded); done, skipped, moved and past sessions stay.
        Session dates must lie in that week and not before today. A session moved into this week is a
        planned session too: include it if it should stay. Only after the athlete confirmed."""
        return tools.save_plan(store, today(), week=week, sessions=sessions)

    @tool("update_session", _write("Update session"))
    async def update_session(
        session_id: Annotated[int, Field(gt=0, description="Session id from get_coach_context or get_plan.")],
        status: Annotated[SessionStatus, Field(description="planned, done, skipped or moved.")],
        new_date: Annotated[str | None, Field(description="YYYY-MM-DD; required for moved, otherwise not allowed.")] = None,
        note: Annotated[str | None, Field(max_length=2000, description="Optional note stored with the session.")] = None,
    ) -> Any:
        """Record what happened to a session after checking Garmin and Hevy: done, skipped, back to
        planned, or moved (with new_date; creates a planned copy on that date). An optional note is stored
        with the session. Only after the athlete confirmed."""
        return tools.update_session(store, today(), session_id=session_id, status=status, new_date=new_date, note=note)

    @tool("add_note", _write("Add note"))
    async def add_note(
        text: Annotated[str, Field(max_length=2000, description="The note.")],
        date: Annotated[str | None, Field(description="YYYY-MM-DD; default today.")] = None,
        session_id: Annotated[int | None, Field(gt=0, description="Optional session the note belongs to.")] = None,
    ) -> Any:
        """Add a dated note to the coach log (why the plan changed, how a session felt), optionally linked to
        a session. Only after the athlete confirmed."""
        return tools.add_note(store, today(), text=text, date=date, session_id=session_id)

    @tool("get_daily_briefing", _read_open("Daily briefing"))
    async def get_daily_briefing() -> Any:
        """Today's morning briefing, computed the same way as the push notification from the current Garmin
        data: a green / yellow / red verdict from deterministic rules (readiness, last night's HRV vs.
        baseline, sleep, Body Battery at wake), the numbers behind it, what is missing, today's planned
        sessions and the alerts (HRV below baseline 3+ nights, resting HR 5+ bpm above its 7-day average,
        load ratio outside Garmin's optimal range, yesterday's scheduled workout not completed,
        yesterday's planned session without a recorded activity, Garmin login). Reads Garmin through
        garmin-mcp; changes nothing."""
        return (await compose_briefing(store, garmin, today())).payload

    @tool("get_trends", _read("Trends"))
    async def get_trends(
        metrics: Annotated[
            list[trends.TrendMetric], Field(min_length=1, max_length=8, description="Metric names, 1-8.")
        ],
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD; default end_date - 89 days.")] = None,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD, not after today; default yesterday.")] = None,
        period: Annotated[
            Literal["day", "week", "month"],
            Field(description="day (max 92 days), week or month (max 731 days)."),
        ] = "week",
    ) -> Any:
        """Stored daily history from Garmin (one row per day, collected nightly by coach-mcp, so it reaches
        further back than get_hrv_trend (28 days) and get_body_battery (31 days)): chosen metrics over a date
        range, aggregated by day, ISO week or month, with n, mean, min and max per period (and the total for
        activity metrics) and the change from the first to the last period. Daily metrics: readiness, HRV,
        sleep, resting HR, Body Battery, stress, acute and chronic load, load ratio, VO2max. Activity metrics
        per sport (endurance = every sport except strength): duration, distance, training load and count.
        Default: the 90 days ending yesterday; today and often yesterday are not stored yet. Strength (Hevy)
        history is not stored."""
        return trends.get_trends(
            store, today(), metrics=metrics, start_date=start_date, end_date=end_date, period=period
        )

    @tool("get_weekly_report", _read("Weekly report"))
    async def get_weekly_report(
        week: Annotated[
            str | None,
            Field(description="Any date (YYYY-MM-DD) in the ISO week; default the last complete week."),
        ] = None,
    ) -> Any:
        """One ISO week (default: the last complete one) against the average of the previous four: endurance
        time, distance and training load by sport, Garmin load at the end of the week, sleep, HRV, resting
        heart rate, plan adherence from the coach plan, new bests (VO2max, weekly distance and load) and flags
        such as training load up more than 30 %. Sections without data are marked available: false; strength
        is not available yet (read Hevy directly). Reads the stored history only."""
        return trends.get_weekly_report(store, today(), week=week)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        try:
            version = store.check()
        except sqlite3.Error as exc:
            log.warning("healthz: database check failed kind=%s", type(exc).__name__)
            return JSONResponse({"status": "error", "db": "error"}, status_code=503)
        return JSONResponse(
            {
                "status": "ok",
                "db": "ok",
                "schema_version": version,
                "scheduler": state["scheduler"],
                "history": state["history"],
            }
        )

    return server


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    today: Callable[[], date] | None = None,
    *,
    garmin: GarminSource | None = None,
    push: PushSender | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    history_source: HistorySource | None = None,
    history_sleep: Callable[[float], Awaitable[None]] | None = None,
) -> ASGIApp:
    settings = settings or Settings.from_env()
    store = store or Store(settings.db_path)
    zone = ZoneInfo(settings.timezone)
    if now is None:

        def now() -> datetime:
            return datetime.now(zone)

    if today is None:
        clock = now

        def today() -> date:
            return clock().astimezone(zone).date()

    reader = GarminReader(settings)
    garmin = garmin or reader
    history = (
        HistoryJob(settings, store, history_source or reader, now=now, sleep=history_sleep or asyncio.sleep)
        if settings.history_enabled
        else None
    )
    scheduler = None
    if settings.push_enabled:
        scheduler = BriefingScheduler(
            settings, store, garmin, push or TelegramPush(settings), now=now, sleep=sleep or asyncio.sleep
        )
    server = build_server(store, settings, today, garmin=garmin, scheduler=scheduler, history=history)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(settings.allowed_hosts),
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=[],
    )
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host=settings.host,
        transport_security=security,
        max_request_body_size=256 * 1024,
    )
    return BearerAuthMiddleware(app, BackendToken(settings.bearer_token_file), prefix="/mcp")
