from datetime import timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from conftest import CONN_ERROR
from garmin_coach_mcp import workouts as w
from garmin_coach_mcp.formatting import today

RUN_SPEC = {
    "name": "Tempo 4x1 km",
    "sport": "running",
    "description": "Easy start",
    "steps": [
        {"type": "warmup", "end": {"type": "time", "seconds": 900}, "target": {"type": "heart_rate_zone", "zone": 2}},
        {
            "type": "repeat",
            "iterations": 4,
            "steps": [
                {"type": "interval", "end": {"type": "distance", "meters": 1000},
                 "target": {"type": "pace", "slowest": "4:30", "fastest": "4:15"}, "note": "Tempo"},
                {"type": "recovery", "end": {"type": "time", "seconds": 120}},
            ],
        },
        {"type": "cooldown", "end": {"type": "lap_button"}},
    ],
}

BIKE_SPEC = {
    "name": "Sweet spot",
    "sport": "cycling",
    "steps": [
        {"type": "warmup", "end": {"type": "time", "seconds": 600}, "target": {"type": "cadence", "min": 85, "max": 95}},
        {"type": "interval", "end": {"type": "time", "seconds": 1200}, "target": {"type": "power", "min_watts": 220, "max_watts": 240}},
        {"type": "recovery", "end": {"type": "time", "seconds": 300}, "target": {"type": "heart_rate", "min_bpm": 100, "max_bpm": 125}},
        {"type": "cooldown", "end": {"type": "time", "seconds": 600}},
    ],
}


def _steps(payload):
    return payload["workoutSegments"][0]["workoutSteps"]


def test_build_running_workout_json():
    payload = w.build_garmin_workout(w.WorkoutSpec(**RUN_SPEC))
    assert payload["workoutName"] == "Tempo 4x1 km"
    assert payload["sportType"]["sportTypeKey"] == "running"
    assert payload["description"] == "Easy start"
    warmup, repeat, cooldown = _steps(payload)

    assert warmup["type"] == "ExecutableStepDTO" and warmup["stepOrder"] == 1
    assert warmup["stepType"]["stepTypeKey"] == "warmup"
    assert warmup["endCondition"]["conditionTypeId"] == 2 and warmup["endConditionValue"] == 900.0
    assert warmup["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone" and warmup["zoneNumber"] == 2
    assert "targetValueOne" not in warmup

    assert repeat["type"] == "RepeatGroupDTO" and repeat["stepOrder"] == 2
    assert repeat["numberOfIterations"] == 4
    assert repeat["endCondition"] == {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False}
    interval, recovery = repeat["workoutSteps"]
    assert (interval["stepOrder"], recovery["stepOrder"]) == (3, 4)
    assert interval["endCondition"]["conditionTypeKey"] == "distance" and interval["endConditionValue"] == 1000.0
    assert interval["targetType"] == {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6}
    # slower pace = lower speed = targetValueOne
    assert interval["targetValueOne"] == pytest.approx(1000 / 270, rel=1e-6)
    assert interval["targetValueTwo"] == pytest.approx(1000 / 255, rel=1e-6)
    assert "zoneNumber" not in interval
    assert interval["description"] == "Tempo"
    assert recovery["targetType"]["workoutTargetTypeKey"] == "no.target"

    assert cooldown["stepOrder"] == 5
    assert cooldown["endCondition"]["conditionTypeKey"] == "lap.button" and "endConditionValue" not in cooldown
    # 900 + 4 * (1000 m at ~4:22.5/km + 120) = 900 + 4 * (262.5 + 120)
    assert payload["estimatedDurationInSecs"] == pytest.approx(2430, abs=2)


def test_build_cycling_workout_json():
    payload = w.build_garmin_workout(w.WorkoutSpec(**BIKE_SPEC))
    assert payload["sportType"] == {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2}
    warmup, interval, recovery, cooldown = _steps(payload)
    assert warmup["targetType"]["workoutTargetTypeKey"] == "cadence"
    assert (warmup["targetValueOne"], warmup["targetValueTwo"]) == (85.0, 95.0)
    assert interval["targetType"]["workoutTargetTypeId"] == 2 and interval["targetType"]["workoutTargetTypeKey"] == "power.zone"
    assert (interval["targetValueOne"], interval["targetValueTwo"]) == (220.0, 240.0) and "zoneNumber" not in interval
    assert recovery["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert (recovery["targetValueOne"], recovery["targetValueTwo"]) == (100.0, 125.0) and "zoneNumber" not in recovery
    assert payload["estimatedDurationInSecs"] == 2700


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda s: s["steps"][1]["steps"][0].update(target={"type": "pace", "slowest": "4:00", "fastest": "4:30"}), "slower"),
        (lambda s: s.update(sport="cycling"), "pace targets"),
        (lambda s: s["steps"][0].update(target={"type": "power", "min_watts": 200, "max_watts": 250}), "power targets"),
        (lambda s: s["steps"][0].update(target={"type": "heart_rate_zone", "zone": 6}), "less than or equal"),
        (lambda s: s["steps"][0].update(end={"type": "time", "seconds": 3}), "greater than or equal"),
        (lambda s: s["steps"][0].update(bogus=1), "Extra inputs"),
        (lambda s: s["steps"].extend([{"type": "repeat", "iterations": 2, "steps": [{"type": "interval", "end": {"type": "lap_button"}}] * 10}] * 5), "too many steps"),
    ],
)
def test_spec_validation(mutate, message):
    import copy

    spec = copy.deepcopy(RUN_SPEC)
    mutate(spec)
    with pytest.raises(ValidationError, match=message):
        w.WorkoutSpec(**spec)


def test_summarize_existing_workout():
    import fixtures as fx

    out = w.summarize_workout(fx.WORKOUT_DETAIL, with_steps=True)
    assert out["steps"][0] == {"type": "warmup", "end": "time 15:00", "target": "HR zone 2"}
    assert out["steps"][1]["repeat"] == 5
    assert out["steps"][1]["steps"][0] == {"type": "interval", "end": "distance 1.0 km", "target": "pace 5:10-5:00 /km"}
    assert out["steps"][1]["steps"][1] == {"type": "recovery", "end": "lap button"}
    assert out["url"].endswith("/9001")


@pytest.mark.anyio
async def test_create_workout_uploads_and_schedules(service, fake):
    day = (today("Europe/Prague") + timedelta(days=2)).isoformat()
    out = await w.create_workout(service, **{k: v for k, v in RUN_SPEC.items()}, schedule_date=day)
    assert out["workout_id"] == 424242
    assert out["scheduled"] == {"date": day, "schedule_id": 31337}
    assert out["url"] == "https://connect.garmin.com/modern/workout/424242"
    assert fake.uploaded[0]["workoutName"] == "Tempo 4x1 km"
    assert fake.scheduled == [(424242, day)]
    assert out["steps"][1]["repeat"] == 4


@pytest.mark.anyio
async def test_create_workout_rejects_invalid_before_upload(service, fake):
    bad = dict(BIKE_SPEC, sport="running")
    with pytest.raises(ToolError, match="power targets"):
        await w.create_workout(service, **bad, description=None, schedule_date=None)
    assert fake.uploaded == []


@pytest.mark.anyio
async def test_create_workout_schedule_date_bounds(service, fake):
    with pytest.raises(ToolError, match="between yesterday"):
        await w.create_workout(service, **BIKE_SPEC, description=None, schedule_date="2020-01-01")
    assert fake.uploaded == []


@pytest.mark.anyio
async def test_create_workout_reports_scheduling_failure(service, fake):
    fake.fail["schedule_workout"] = CONN_ERROR
    out = await w.create_workout(service, **BIKE_SPEC, description=None, schedule_date=today("Europe/Prague").isoformat())
    assert out["workout_id"] == 424242
    assert "scheduling failed" in out["scheduled"]["error"]


@pytest.mark.anyio
async def test_schedule_workout_is_idempotent(service, fake):
    day = (today("Europe/Prague") + timedelta(days=1)).isoformat()
    fake.responses["query_garmin_graphql"] = {"data": {"workoutScheduleSummariesScalar": [
        {"scheduledWorkoutId": 99, "scheduleDate": day, "workoutId": 9001}]}}
    out = await w.schedule_workout(service, 9001, day)
    assert out == {"workout_id": 9001, "date": day, "already_scheduled": True, "schedule_id": 99}
    assert fake.scheduled == []
    out = await w.schedule_workout(service, 9002, day)
    assert out["already_scheduled"] is False and out["schedule_id"] == 31337
    assert fake.scheduled == [(9002, day)]


@pytest.mark.anyio
async def test_list_scheduled_graphql_and_fallback(service, fake):
    out = await w.list_scheduled_workouts(service, "2026-09-28", "2026-10-11")
    assert [s["schedule_id"] for s in out["scheduled"]] == [778, 777]
    assert out["scheduled"][0]["completed"] is True and out["scheduled"][1]["completed"] is False

    fake.fail["query_garmin_graphql"] = CONN_ERROR
    out = await w.list_scheduled_workouts(service, "2026-10-01", "2026-10-31")
    assert out["scheduled"] == [{"schedule_id": 555, "date": "2026-10-03", "workout_id": 9001, "name": "Intervaly 5x3", "sport": "running"}]


@pytest.mark.anyio
async def test_list_scheduled_range_limit(service):
    with pytest.raises(ToolError):
        await w.list_scheduled_workouts(service, "2026-01-01", "2026-06-30")


@pytest.mark.anyio
async def test_list_and_get_and_unschedule(service, fake):
    listed = await w.list_workouts(service, 20)
    assert listed["workouts"][0] == {"workout_id": 9001, "name": "Intervaly 5x3", "sport": "running",
                                     "estimated_duration_min": 50, "updated": "2026-09-20"}
    detail = await w.get_workout(service, 9001)
    assert detail["name"] == "Intervaly 5x3" and len(detail["steps"]) == 2
    out = await w.unschedule_workout(service, 777)
    assert out["unscheduled"] is True and fake.unscheduled == [777]
