"""MCP server definition: curated Garmin tools for a fitness coach."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from . import __version__, activities, health, performance, workouts
from .auth import BackendToken, BearerAuthMiddleware
from .config import Settings
from .formatting import to_json
from .garmin_client import GarminService

log = logging.getLogger("garmin_coach_mcp.tools")

INSTRUCTIONS = (
    "Garmin Connect data of the athlete, for coaching. Call get_recovery_snapshot first "
    "(today's training readiness, HRV, sleep, Body Battery, resting HR, stress, training status), "
    "then list_activities for recent load. "
    "Before prescribing paces, heart-rate or power targets call get_performance_metrics (race predictions, lactate threshold, zones, VO2max trend). "
    "Dates are YYYY-MM-DD in the athlete's local time zone; "
    "omit them for today. create_workout, schedule_workout and unschedule_workout change the "
    "athlete's Garmin library/calendar: show the plan and get explicit confirmation first."
)

DateArg = Annotated[str | None, Field(description="YYYY-MM-DD in local time; omit for today.")]


def _read(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, read_only_hint=True, open_world_hint=True)


def _write(title: str, *, destructive: bool, idempotent: bool = False) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        read_only_hint=False,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=True,
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


def build_server(service: GarminService, settings: Settings) -> MCPServer:
    @contextlib.asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        task = asyncio.create_task(service.keepalive_loop()) if settings.keepalive_hours > 0 else None
        try:
            yield None
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    server = MCPServer(
        name="garmin-coach",
        title="Garmin Connect (fitness coach)",
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
                description=inspect.cleandoc(fn.__doc__ or ""),
                annotations=annotations,
                structured_output=False,
            )
            return fn

        return register

    # ------------------------------------------------------------ recovery

    @tool("get_recovery_snapshot", _read("Recovery snapshot"))
    async def get_recovery_snapshot(date: DateArg = None) -> Any:
        """One call with everything needed before planning a session: training readiness (score,
        level, contributing factors, recovery time), last night's HRV vs. baseline, sleep (score,
        duration, stages), Body Battery, resting HR (+7-day average), stress and training status
        (acute/chronic load, VO2max). Sections that Garmin cannot provide contain an "error" or
        "available": false instead of failing the whole call."""
        return await health.recovery_snapshot(service, date)

    @tool("get_training_readiness", _read("Training readiness"))
    async def get_training_readiness(date: DateArg = None) -> Any:
        """Garmin Training Readiness for a day: latest score and level, morning score, recovery
        time in hours and each contributing factor (sleep, sleep history, recovery time, acute
        load, HRV, stress history) with its percent and feedback."""
        return await health.training_readiness(service, date)

    @tool("get_hrv_trend", _read("HRV trend"))
    async def get_hrv_trend(
        days: Annotated[int, Field(ge=1, le=28, description="Number of days ending at end_date (1-28).")] = 14,
        end_date: DateArg = None,
    ) -> Any:
        """Nightly HRV (ms) per day with weekly average, HRV status and personal baseline range,
        plus a summary (mean, min/max, change between the first and second half of the period).
        Use it to judge whether recovery is trending up or down."""
        return await health.hrv_trend(service, days, end_date)

    @tool("get_sleep", _read("Sleep"))
    async def get_sleep(date: DateArg = None) -> Any:
        """Sleep for the night ending on the given date: score and quality, duration, deep/light/
        REM/awake minutes, start/end time, overnight HRV, resting HR, respiration, SpO2, Body
        Battery change and Garmin's score components."""
        return await health.sleep(service, date)

    @tool("get_body_battery", _read("Body Battery"))
    async def get_body_battery(
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD; defaults to end_date.")] = None,
        end_date: DateArg = None,
    ) -> Any:
        """Body Battery per day (max 31 days): charged, drained, start, high, low and latest
        level."""
        return await health.body_battery(service, start_date, end_date)

    @tool("get_daily_summary", _read("Daily summary"))
    async def get_daily_summary(date: DateArg = None) -> Any:
        """Daily wellness summary: steps, distance, calories, resting/min/max heart rate, stress
        (average, max, minutes per level), intensity minutes, Body Battery, sleep hours, SpO2."""
        return await health.daily_summary(service, date)

    @tool("get_training_status", _read("Training status"))
    async def get_training_status(date: DateArg = None) -> Any:
        """Training status (e.g. PRODUCTIVE, MAINTAINING, RECOVERY, UNPRODUCTIVE, DETRAINING,
        OVERREACHING), acute and chronic training load with ACWR, 4-week load balance
        (aerobic low/high, anaerobic vs. targets) and VO2max for running and cycling."""
        return await health.training_status(service, date)

    # --------------------------------------------------------- performance

    @tool("get_performance_metrics", _read("Performance metrics"))
    async def get_performance_metrics(
        weeks: Annotated[
            int, Field(ge=2, le=52, description="Weeks of history for the VO2max trend and the endurance/hill score change (2-52).")
        ] = 12,
    ) -> Any:
        """Measured performance values for setting paces and zones, in one call: Garmin race
        predictions (5 km, 10 km, half marathon, marathon: time and pace per km), lactate
        threshold (heart rate, pace, running power), heart-rate zones per sport profile and
        cycling power zones with their boundaries, endurance score and hill score (latest value
        and change over the window) and the weekly VO2max trend. Sections that Garmin cannot
        provide contain an "error" or "available": false instead of failing the whole call."""
        return await performance.performance_metrics(service, weeks)

    @tool("get_personal_records", _read("Personal records"))
    async def get_personal_records() -> Any:
        """Garmin personal records: type (1k, 1_mile, 5k, 10k, half_marathon, marathon,
        longest_run; other record types as type_id with the raw value), time and pace or
        distance, date and activity id."""
        return await performance.personal_records(service)

    @tool("get_gear", _read("Gear"))
    async def get_gear() -> Any:
        """The athlete's gear (shoes, bikes, other), active and retired: name, type, start date,
        total distance, activity count, Garmin's distance limit when set and a retired flag.
        Use it to flag worn-out shoes."""
        return await performance.gear(service)

    # ---------------------------------------------------------- activities

    @tool("list_activities", _read("List activities"))
    async def list_activities(
        days: Annotated[int, Field(ge=1, le=366, description="Days back from end_date (ignored if start_date is set).")] = 7,
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD; optional.")] = None,
        end_date: DateArg = None,
        activity_type: Annotated[activities.ActivityType | None, Field(description="Optional filter.")] = None,
        limit: Annotated[int, Field(ge=1, le=100, description="Maximum activities returned (newest first).")] = 50,
    ) -> Any:
        """Recorded activities, newest first: id, start time, type, name, duration, distance,
        elevation, average/max HR, pace (run/walk/hike) or speed (bike), power, cadence, aerobic/
        anaerobic training effect, training load, calories. Default: the last 7 days."""
        return await activities.list_activities(service, days, start_date, end_date, activity_type, limit)

    @tool("get_activity", _read("Activity detail"))
    async def get_activity(
        activity_id: Annotated[int, Field(gt=0, description="Activity id from list_activities.")],
    ) -> Any:
        """One activity in detail: summary metrics, laps/splits (pace or speed, HR, power,
        cadence, workout step) and time in each heart-rate zone."""
        return await activities.get_activity(service, activity_id)

    # ------------------------------------------------------------ workouts

    @tool("list_workouts", _read("List workouts"))
    async def list_workouts(
        limit: Annotated[int, Field(ge=1, le=100, description="Maximum workouts returned.")] = 20,
    ) -> Any:
        """Workouts saved in the athlete's Garmin Connect workout library (id, name, sport,
        estimated duration/distance, last update)."""
        return await workouts.list_workouts(service, limit)

    @tool("get_workout", _read("Workout detail"))
    async def get_workout(
        workout_id: Annotated[int, Field(gt=0, description="Workout id from list_workouts.")],
    ) -> Any:
        """One saved workout with its steps (type, duration/distance, target) and its Garmin
        Connect URL."""
        return await workouts.get_workout(service, workout_id)

    @tool("create_workout", _write("Create structured workout", destructive=False))
    async def create_workout(
        name: Annotated[str, Field(min_length=1, max_length=80, description="Workout name shown on the watch.")],
        sport: Annotated[Literal["running", "cycling"], Field(description="Sport of the workout.")],
        steps: Annotated[
            list[workouts.WorkoutItem],
            Field(min_length=1, max_length=30, description="Ordered steps; use a 'repeat' item for interval blocks."),
        ],
        description: Annotated[str | None, Field(max_length=500, description="Optional notes.")] = None,
        schedule_date: Annotated[
            str | None, Field(description="YYYY-MM-DD to also put it on the Garmin calendar (syncs to the watch).")
        ] = None,
    ) -> Any:
        """Create a structured running or cycling workout in Garmin Connect and optionally
        schedule it, so it syncs to the watch. Only call after the athlete confirmed the plan.
        Each step: type (warmup|interval|recovery|rest|cooldown), end ({"type":"time","seconds":N}
        | {"type":"distance","meters":N} | {"type":"lap_button"}) and optional target:
        {"type":"heart_rate_zone","zone":1-5} | {"type":"heart_rate","min_bpm","max_bpm"} |
        {"type":"pace","slowest":"5:30","fastest":"5:10"} (running, per km) |
        {"type":"power","min_watts","max_watts"} (cycling) | {"type":"cadence","min","max"}.
        Interval blocks: {"type":"repeat","iterations":N,"steps":[...]}. Creating twice makes two
        workouts, so do not retry blindly after an error; check list_workouts first."""
        return await workouts.create_workout(service, name, sport, steps, description, schedule_date)

    @tool("schedule_workout", _write("Schedule workout", destructive=False, idempotent=True))
    async def schedule_workout(
        workout_id: Annotated[int, Field(gt=0, description="Workout id (list_workouts / create_workout).")],
        date: Annotated[str, Field(description="YYYY-MM-DD (today up to one year ahead).")],
    ) -> Any:
        """Put an existing workout on the Garmin calendar for a date; it reaches the watch on the
        next sync. Idempotent: an identical existing entry is reported instead of duplicated."""
        return await workouts.schedule_workout(service, workout_id, date)

    @tool("list_scheduled_workouts", _read("Scheduled workouts"))
    async def list_scheduled_workouts(
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD; default today.")] = None,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD; default start_date + 13 days (max 62 days).")] = None,
    ) -> Any:
        """Workouts on the Garmin calendar in a date range (schedule_id, date, workout, sport,
        completed or not, training plan)."""
        return await workouts.list_scheduled_workouts(service, start_date, end_date)

    @tool("unschedule_workout", _write("Remove workout from calendar", destructive=True, idempotent=True))
    async def unschedule_workout(
        schedule_id: Annotated[int, Field(gt=0, description="schedule_id from list_scheduled_workouts.")],
    ) -> Any:
        """Remove one calendar entry (the workout itself stays in the library). Only after the
        athlete confirmed."""
        return await workouts.unschedule_workout(service, schedule_id)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "garmin_auth": service.auth_state()})

    return server


def create_app(settings: Settings | None = None, service: GarminService | None = None) -> ASGIApp:
    settings = settings or Settings.from_env()
    service = service or GarminService(settings)
    server = build_server(service, settings)
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
