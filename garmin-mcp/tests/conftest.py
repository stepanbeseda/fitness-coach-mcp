from __future__ import annotations

import copy
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError

import fixtures as fx
from garmin_coach_mcp.config import Settings
from garmin_coach_mcp.garmin_client import TOKEN_FILE_NAME, GarminService
from garmin_coach_mcp.server import create_app

BACKEND_TOKEN = "test-backend-token-0123456789abcdef"
TOKENS = {"di_token": "a.b.c", "di_refresh_token": "r", "di_client_id": "cid"}


class _FakeInnerClient:
    def __init__(self, tokens: dict[str, Any]) -> None:
        self.tokens = tokens
        self._api_session = object()

    def dumps(self) -> str:
        return json.dumps(self.tokens)


class FakeGarmin:
    """Stands in for garminconnect.Garmin with canned responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.uploaded: list[dict[str, Any]] = []
        self.scheduled: list[tuple[int, str]] = []
        self.unscheduled: list[int] = []
        self.weigh_ins: list[tuple[float, str, str]] = []
        self.fail: dict[str, Exception] = {}
        self.responses: dict[str, Any] = {
            "get_training_readiness": copy.deepcopy(fx.TRAINING_READINESS),
            "get_hrv_data": copy.deepcopy(fx.HRV),
            "get_hrv_data_range": copy.deepcopy(fx.HRV_RANGE),
            "get_sleep_data": copy.deepcopy(fx.SLEEP),
            "get_user_summary": copy.deepcopy(fx.USER_SUMMARY),
            "get_training_status": copy.deepcopy(fx.TRAINING_STATUS),
            "get_body_battery": copy.deepcopy(fx.BODY_BATTERY),
            "get_activities_by_date": copy.deepcopy(fx.ACTIVITIES),
            "get_activity": copy.deepcopy(fx.ACTIVITY),
            "get_activity_splits": copy.deepcopy(fx.SPLITS),
            "get_activity_hr_in_timezones": copy.deepcopy(fx.HR_ZONES),
            "get_workouts": copy.deepcopy(fx.WORKOUTS),
            "get_workout_by_id": copy.deepcopy(fx.WORKOUT_DETAIL),
            "query_garmin_graphql": copy.deepcopy(fx.SCHEDULES_GRAPHQL),
            "get_scheduled_workouts": copy.deepcopy(fx.CALENDAR_MONTH),
            "get_race_predictions": copy.deepcopy(fx.RACE_PREDICTIONS),
            "get_lactate_threshold": copy.deepcopy(fx.LACTATE_THRESHOLD),
            "get_heart_rate_zones": copy.deepcopy(fx.HEART_RATE_ZONES),
            "get_power_zones": copy.deepcopy(fx.POWER_ZONES),
            "get_endurance_score": copy.deepcopy(fx.ENDURANCE_SCORE),
            "get_hill_score": copy.deepcopy(fx.HILL_SCORE),
            "get_max_metrics_range": copy.deepcopy(fx.MAX_METRICS_RANGE),
            "get_personal_record": copy.deepcopy(fx.PERSONAL_RECORDS),
            "get_gear": copy.deepcopy(fx.GEAR),
            "get_gear_stats": copy.deepcopy(fx.GEAR_STATS),
            "get_body_composition": copy.deepcopy(fx.BODY_COMPOSITION),
            "get_daily_weigh_ins": copy.deepcopy(fx.DAILY_WEIGH_INS),
            "get_hydration_data": copy.deepcopy(fx.HYDRATION),
            "get_nutrition_daily_food_log": copy.deepcopy(fx.NUTRITION_FOOD_LOG),
            "get_nutrition_daily_meals": copy.deepcopy(fx.NUTRITION_MEALS),
            "get_nutrition_daily_settings": copy.deepcopy(fx.NUTRITION_SETTINGS),
        }
        self.profile_id: int | None = 1001  # fake; the real library sets it at login
        self.gear_stats_fail: dict[str, Exception] = {}
        self.client = _FakeInnerClient(dict(TOKENS))
        self.logged_in_with: str | None = None

    def _respond(self, name: str, *args: Any) -> Any:
        self.calls.append((name, args))
        if name in self.fail:
            raise self.fail[name]
        return copy.deepcopy(self.responses.get(name))

    def login(self, tokenstore: str | None = None) -> tuple[None, None]:
        self.calls.append(("login", (tokenstore,)))
        if "login" in self.fail:
            raise self.fail["login"]
        self.logged_in_with = tokenstore
        return None, None

    def connectapi(self, path: str, **kwargs: Any) -> Any:
        return self._respond("connectapi", path)

    def get_gear_stats(self, gear_uuid: str) -> Any:
        by_uuid = self._respond("get_gear_stats", gear_uuid)
        if gear_uuid in self.gear_stats_fail:
            raise self.gear_stats_fail[gear_uuid]
        return by_uuid.get(gear_uuid) if isinstance(by_uuid, dict) else None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("get_") or name == "query_garmin_graphql":
            return lambda *args, **kwargs: self._respond(name, *args)
        raise AttributeError(name)

    def upload_workout(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("upload_workout", ()))
        if "upload_workout" in self.fail:
            raise self.fail["upload_workout"]
        self.uploaded.append(payload)
        return {"workoutId": 424242, "workoutName": payload["workoutName"]}

    def schedule_workout(self, workout_id: int, date_str: str) -> dict[str, Any]:
        self.calls.append(("schedule_workout", (workout_id, date_str)))
        if "schedule_workout" in self.fail:
            raise self.fail["schedule_workout"]
        self.scheduled.append((workout_id, date_str))
        return {"workoutScheduleId": 31337, "calendarDate": date_str}

    def add_weigh_in(self, weight: float, unitKey: str = "kg", timestamp: str = "") -> None:
        self.calls.append(("add_weigh_in", (weight, unitKey, timestamp)))
        if "add_weigh_in" in self.fail:
            raise self.fail["add_weigh_in"]
        self.weigh_ins.append((weight, unitKey, timestamp))
        return None  # the real endpoint answers 204

    def unschedule_workout(self, scheduled_workout_id: int) -> Any:
        self.calls.append(("unschedule_workout", (scheduled_workout_id,)))
        self.unscheduled.append(scheduled_workout_id)
        return None


AUTH_ERROR = GarminConnectAuthenticationError("Authentication failed")
CONN_ERROR = GarminConnectConnectionError("API Error 500 - boom")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def tokens_dir(tmp_path: Path) -> Path:
    d = tmp_path / "garmin"
    d.mkdir(mode=0o700)
    (d / TOKEN_FILE_NAME).write_text(json.dumps(TOKENS), encoding="utf-8")
    return d


@pytest.fixture
def bearer_file(tmp_path: Path) -> Path:
    f = tmp_path / "garmin_backend_token"
    f.write_text(BACKEND_TOKEN + "\n", encoding="utf-8")
    return f


@pytest.fixture
def settings(tokens_dir: Path, bearer_file: Path) -> Settings:
    return Settings(tokens_dir=tokens_dir, bearer_token_file=bearer_file, keepalive_hours=0, timezone="Europe/Prague")


@pytest.fixture
def fake() -> FakeGarmin:
    return FakeGarmin()


@pytest.fixture
def service(settings: Settings, fake: FakeGarmin) -> GarminService:
    return GarminService(settings, garmin_factory=lambda: fake)


@asynccontextmanager
async def running_client(app: Any):
    """httpx client bound to the ASGI app with its lifespan running."""
    inner = app.app  # BearerAuthMiddleware -> Starlette
    async with inner.router.lifespan_context(inner):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://garmin-mcp:8000") as client:
            yield client


@pytest.fixture
def app(settings: Settings, service: GarminService) -> Any:
    return create_app(settings, service)


MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def auth_headers(token: str = BACKEND_TOKEN, protocol: str | None = "2025-06-18") -> dict[str, str]:
    headers = dict(MCP_HEADERS, Authorization=f"Bearer {token}")
    if protocol:
        headers["MCP-Protocol-Version"] = protocol
    return headers
