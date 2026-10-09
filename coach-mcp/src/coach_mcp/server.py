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

from . import __version__, strength, tools, trends
from .auth import BackendToken, BearerAuthMiddleware
from .config import Settings
from .garmin import GarminReader, GarminSource, HistorySource, exception_kind
from .hevy import HevyReader, StrengthSource
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
    "use it for weekly reviews (it includes strength volume and sets per muscle group from Hevy). "
    "get_strength_progress returns the estimated 1RM (Epley) per exercise over time from the stored Hevy history "
    "with a plateau flag: use it for strength progression; for the latest workout and exact sets call Hevy. "
    "Past planned sessions carry a suggestion from the stored Garmin activities and Hevy workouts (matched with the day's totals "
    "of that sport, or no_activity; final false while a late watch sync can still change it); "
    "get_coach_context lists those of the last 7 days in to_confirm. A suggestion never changes a status: "
    "check it with the athlete and confirm it with update_session."
    " search_notes searches the whole notes log (get_coach_context has only the latest 10 notes); "
    "list_goals and list_constraints include achieved, abandoned and resolved entries. Search there "
    "before asking the athlete about something they may already have told the coach."
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
        sessions carry a suggestion (matched or no_activity, from the nightly Garmin and Hevy history); confirm it with
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
        Default: the 90 days ending yesterday; today and often yesterday are not stored yet. Strength history
        from Hevy: get_strength_progress and the strength section of get_weekly_report."""
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
        time, distance and training load by sport, Garmin load at the end of the week, strength volume and
        working sets per muscle group from Hevy, sleep, HRV, resting heart rate, plan adherence from the
        coach plan, new bests (VO2max, weekly distance and load, estimated 1RM per exercise) and flags such
        as training load or strength volume up more than 30 %. Sections without data are marked available:
        false. Reads the stored history only."""
        return trends.get_weekly_report(store, today(), week=week)

    @tool("get_strength_progress", _read("Strength progress"))
    async def get_strength_progress(
        exercise: Annotated[
            str | None,
            Field(
                max_length=100,
                description="Hevy exercise_template_id or part of the exercise title; default every exercise.",
            ),
        ] = None,
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD; default end_date - 89 days.")] = None,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD, not after today; default yesterday.")] = None,
        plateau_weeks: Annotated[
            int,
            Field(ge=2, le=12, description="Weeks without beating the earlier best that count as a plateau (2-12)."),
        ] = 4,
        limit: Annotated[int, Field(ge=1, le=20, description="Max exercises, most trained first (1-20).")] = 10,
    ) -> Any:
        """Strength progression from the stored Hevy history (collected nightly by coach-mcp): per exercise the
        estimated one-rep max by Epley (weight x (1 + reps / 30); a single counts as its weight; warm-up sets and
        sets above 12 reps do not count) of the best set of every training day in the range, the best, first and
        last value, and a plateau flag when the last plateau_weeks weeks (at least two sessions) did not beat the
        best before them. Default: the 90 days ending yesterday. Returns numbers only; render charts yourself."""
        return strength.get_strength_progress(
            store,
            today(),
            exercise=exercise,
            start_date=start_date,
            end_date=end_date,
            plateau_weeks=plateau_weeks,
            limit=limit,
        )

    @tool("search_notes", _read("Search notes"))
    async def search_notes(
        query: Annotated[
            str,
            Field(max_length=200, description='Words to find (all must match); also "exact phrase", OR, NOT, prefix*.'),
        ],
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD, inclusive; default no lower bound.")] = None,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD, inclusive; default no upper bound.")] = None,
        session_id: Annotated[int | None, Field(gt=0, description="Only notes linked to this session.")] = None,
        limit: Annotated[
            int, Field(ge=1, le=tools.SEARCH_LIMIT_MAX, description="Maximum notes returned, 1-50.")
        ] = tools.SEARCH_LIMIT,
    ) -> Any:
        """Full-text search in the whole coach notes log (get_coach_context shows only the latest 10 notes):
        notes containing every word of the query, newest first, optionally within a date range or linked to
        one session; more is true when further matches exist. Accents and case are ignored (boli finds
        bolí). Czech words are not stemmed: search by prefix, e.g. bol* finds bolí, bolest and bolelo.
        Syntax: plain words, "exact phrase", OR, NOT, prefix*; put a word with punctuation in double
        quotes, e.g. "low-carb". Search here before asking the athlete about something they may already
        have told the coach."""
        return tools.search_notes(
            store, today(), query=query, start_date=start_date, end_date=end_date, session_id=session_id, limit=limit
        )

    @tool("list_goals", _read("All goals"))
    async def list_goals() -> Any:
        """Every goal ever set, including achieved and abandoned ones (get_coach_context lists only active
        goals): status, target, target date, days remaining (negative once the date has passed) and
        created_at / updated_at in UTC; for a closed goal updated_at is when it was closed or last changed."""
        return tools.list_goals(store, today())

    @tool("list_constraints", _read("All constraints"))
    async def list_constraints() -> Any:
        """Every constraint ever recorded, active and resolved (get_coach_context lists only active ones):
        kind, title, details, status and created_at / updated_at in UTC; for a resolved constraint (e.g. a
        past injury) updated_at is when it was resolved or last changed."""
        return tools.list_constraints(store, today())

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
    strength_source: StrengthSource | None = None,
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
        HistoryJob(
            settings,
            store,
            history_source or reader,
            now=now,
            sleep=history_sleep or asyncio.sleep,
            strength=strength_source or HevyReader(settings),
        )
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
