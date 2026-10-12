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

from . import __version__, activities, body, exercises, health, performance, workouts
from .access import ReadOnlyGuard
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
    "get_race_predictions returns Garmin's daily race predictions over a date range (their history). "
    "For weight, body-fat or nutrition goals call get_body_composition (weigh-in trend) and get_nutrition "
    "(calories, macros, meals, hydration); Garmin is the primary source for weight. "
    "Before raising weekly running volume call get_running_tolerance (weekly running load vs. tolerance). "
    "get_activity also returns the weather and, for strength activities, the sets recorded on the watch. "
    "When last night's sleep or HRV is missing, get_device_status says when the watch last synced. "
    "A strength session can go to the watch as well: map each exercise with find_strength_exercise, "
    'then create_workout with sport "strength"; Hevy stays the place of record for strength training. '
    "Dates are YYYY-MM-DD in the athlete's local time zone; "
    "omit them for today. create_workout, schedule_workout, unschedule_workout and log_weight change the "
    "athlete's Garmin data: show the plan or value and get explicit confirmation first."
)

DateArg = Annotated[str | None, Field(description="YYYY-MM-DD in local time; omit for today.")]


def _read(title: str, *, open_world: bool = True) -> ToolAnnotations:
    return ToolAnnotations(title=title, read_only_hint=True, open_world_hint=open_world)


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

    guard = ReadOnlyGuard()
    server = MCPServer(
        name="garmin-coach",
        title="Garmin Connect (fitness coach)",
        instructions=INSTRUCTIONS,
        version=__version__,
        lifespan=lifespan,
        # the membership test narrows the value at runtime; mypy does not narrow a str to a Literal
        log_level=(
            settings.log_level  # type: ignore[arg-type]
            if settings.log_level in ("DEBUG", "INFO", "WARNING", "ERROR")
            else "INFO"
        ),
        extensions=[guard],
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
            guard.register(name, annotations)
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

    @tool("get_device_status", _read("Device status"))
    async def get_device_status() -> Any:
        """When Garmin last received the athlete's data: the device that uploaded last and the upload time
        (time_utc to the second, time_local in the athlete's time zone), plus the primary activity tracker
        (name, status) and the number of registered devices. Call it when last night's sleep or HRV is
        missing, to tell "the watch has not synced yet" from "no data for the night". Sections that Garmin
        cannot provide contain an "error" or "available": false instead of failing the whole call."""
        return await health.device_status(service)

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

    @tool("get_race_predictions", _read("Race predictions"))
    async def get_race_predictions(
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD; default end_date - 27 days.")] = None,
        end_date: DateArg = None,
    ) -> Any:
        """Garmin's daily race predictions over a date range (max 366 days, default the 28 days ending
        today): per day the predicted 5 km, 10 km, half marathon and marathon times in seconds. Use it
        for the history of the predictions; get_performance_metrics has the latest ones with pace. Days
        without a prediction are left out; an empty range returns "available": false."""
        return await performance.race_predictions(service, start_date, end_date)

    @tool("get_running_tolerance", _read("Running tolerance"))
    async def get_running_tolerance(
        weeks: Annotated[int, Field(ge=1, le=26, description="Weeks ending at end_date (1-26).")] = 8,
        end_date: DateArg = None,
    ) -> Any:
        """Garmin's running tolerance per week: the athlete's weekly running load against the load
        Garmin estimates they can currently tolerate (watches with Running Tolerance). Garmin does not
        document this payload, so each week lists Garmin's own fields in snake_case (identifiers
        dropped). Use it before raising weekly running volume. Without the feature or data the result
        is "available": false."""
        return await performance.running_tolerance(service, weeks, end_date)

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

    # ------------------------------------------------- body and nutrition

    @tool("get_body_composition", _read("Body composition"))
    async def get_body_composition(
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD; default end_date - 29 days.")] = None,
        end_date: DateArg = None,
    ) -> Any:
        """Weigh-ins in a date range (max 366 days, default the last 30): one entry per day with
        weight (kg), BMI, body fat %, muscle mass (kg), body water % and bone mass (kg) where the
        scale provides them, plus a summary (first and last value, change, 7-day average weight,
        body-fat change). An empty range returns "available": false with an empty list."""
        return await body.body_composition(service, start_date, end_date)

    @tool("get_nutrition", _read("Nutrition and hydration"))
    async def get_nutrition(date: DateArg = None) -> Any:
        """One day of intake: calories consumed vs. goal, protein / carbs / fat (g) vs. goals,
        meals (name, calories, macros, number of items) and hydration (intake vs. goal in ml,
        sweat loss). Each part that Garmin does not provide answers "available": false or
        "error" while the others still arrive."""
        return await body.nutrition(service, date)

    @tool("log_weight", _write("Log weight", destructive=False))
    async def log_weight(
        weight_kg: Annotated[float, Field(ge=20, le=300, description="Body weight in kg (20-300).")],
        date: Annotated[str | None, Field(description="YYYY-MM-DD, today or earlier; omit for today.")] = None,
    ) -> Any:
        """Store a manual weigh-in in Garmin Connect for an athlete without a Garmin scale, and
        report the stored entry. Call it only after the athlete confirmed the value. Non-destructive
        but not idempotent: logging twice stores two weigh-ins, so check get_body_composition
        before retrying after an error. Garmin is the place of record for weight: do not write the
        same measurement to Hevy as well."""
        return await body.log_weight(service, weight_kg, date)

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
        cadence, workout step), time in each heart-rate zone and the weather during the activity
        (temperature, feels-like, humidity, wind, conditions). For strength activities also the sets
        recorded on the watch: exercise, reps, weight (kg) and duration per set, plus a summary per
        exercise, and the active-set and rep totals of the activity. A part Garmin cannot provide is
        "available": false or "error"."""
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
        """One saved workout with its steps (type, duration/distance, target; for a strength workout the
        exercises with sets, reps or seconds, weight in kg, rest and note) and its Garmin Connect URL."""
        return await workouts.get_workout(service, workout_id)

    @tool("find_strength_exercise", _read("Find strength exercise", open_world=False))
    async def find_strength_exercise(
        query: Annotated[
            str,
            Field(
                min_length=2,
                max_length=80,
                description="Exercise title, e.g. the Hevy title 'Bench Press (Barbell)' (2-80 characters).",
            ),
        ],
        limit: Annotated[int, Field(ge=1, le=20, description="Maximum matches returned (1-20).")] = 5,
    ) -> Any:
        """Search Garmin's strength exercise catalogue (bundled with the server, about 1 500 exercises)
        for an exercise title, for example a Hevy title such as "Bench Press (Barbell)". Returns the best
        matches first: exercise (Garmin's display name), category and name (pass both to create_workout)
        and a score from 0 to 1 (1 = the same words). It is a word match: check that the match is the
        same movement and equipment. An empty list, or no match that fits, means Garmin does not know the
        exercise: send it to create_workout with its title instead of category and name."""
        return exercises.find(query, limit)

    @tool("create_workout", _write("Create structured workout", destructive=False))
    async def create_workout(
        name: Annotated[str, Field(min_length=1, max_length=80, description="Workout name shown on the watch.")],
        sport: Annotated[Literal["running", "cycling", "strength"], Field(description="Sport of the workout.")],
        steps: Annotated[
            list[workouts.WorkoutItem],
            Field(
                min_length=1,
                max_length=30,
                description="Ordered steps; use a 'repeat' item for interval blocks, 'exercise' items for strength.",
            ),
        ],
        description: Annotated[str | None, Field(max_length=500, description="Optional notes.")] = None,
        schedule_date: Annotated[
            str | None, Field(description="YYYY-MM-DD to also put it on the Garmin calendar (syncs to the watch).")
        ] = None,
    ) -> Any:
        """Create a structured running, cycling or strength workout in Garmin Connect and optionally
        schedule it, so it syncs to the watch. Only call after the athlete confirmed the plan.
        Running and cycling steps: type (warmup|interval|recovery|rest|cooldown), end
        ({"type":"time","seconds":N} | {"type":"distance","meters":N} | {"type":"lap_button"}) and
        optional target: {"type":"heart_rate_zone","zone":1-5} | {"type":"heart_rate","min_bpm","max_bpm"} |
        {"type":"pace","slowest":"5:30","fastest":"5:10"} (running, per km) |
        {"type":"power","min_watts","max_watts"} (cycling) | {"type":"cadence","min","max"}.
        Interval blocks: {"type":"repeat","iterations":N,"steps":[...]}.
        Strength (sport "strength") takes only exercise items, in order:
        {"type":"exercise","category","name","sets","reps" or "seconds","weight_kg","rest_seconds","note"};
        category and name come from find_strength_exercise. An exercise Garmin does not know is sent with
        "title" (its name in Hevy) instead of category and name: the watch shows it without an exercise
        name, with the title as its note, and the result lists it under "unmapped". All sets of one item
        are the same; add a second item for a warm-up set or another weight. Creating twice makes two
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

    @server.custom_route("/healthz", methods=["GET"])  # type: ignore[untyped-decorator]  # not annotated in the SDK
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "garmin_auth": service.auth_state()})

    return server


def create_app(settings: Settings | None = None, service: GarminService | None = None) -> ASGIApp:
    settings = settings or Settings.from_env()
    service = service or GarminService(settings)
    return asgi_app(build_server(service, settings), settings)


def asgi_app(server: MCPServer, settings: Settings) -> ASGIApp:
    """Streamable HTTP app of ``server`` behind the bearer check (gateway token: full access,
    coach-mcp's token: read-only)."""
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
    readonly = BackendToken(settings.readonly_token_file)
    if readonly.value() is None:
        logging.getLogger("garmin_coach_mcp.auth").warning(
            "read-only token file missing or too short: only the gateway token is accepted"
        )
    return BearerAuthMiddleware(
        app, BackendToken(settings.bearer_token_file), prefix="/mcp", readonly_token=readonly
    )
