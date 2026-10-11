import copy
from datetime import timedelta

import pytest
from garminconnect.workout import create_strength_set
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

import fixtures as fx
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


# A strength session as the coach sends it: three mapped exercises (one with a title that is not sent), one unmapped,
# a timed hold. Its payload is the request body Garmin accepted on 2026-10-12 (fx.STRENGTH_UPLOAD).
STRENGTH_SPEC = {
    "name": "MAO spike PR07 - delete me",
    "sport": "strength",
    "description": "Test upload of PR 07 (strength workouts on the watch). Safe to delete.",
    "steps": [
        {"type": "exercise", "category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS", "title": "Bench Press (Barbell)",
         "sets": 3, "reps": 8, "weight_kg": 60, "rest_seconds": 90},
        {"type": "exercise", "category": "PULL_UP", "name": "PULL_UP", "sets": 3, "reps": 6, "rest_seconds": 120,
         "note": "spike note on a mapped step"},
        {"type": "exercise", "title": "Hevy: Zercher Carry", "sets": 2, "reps": 10, "rest_seconds": 60},
        {"type": "exercise", "category": "PLANK", "name": "PLANK", "sets": 2, "seconds": 45, "rest_seconds": 30},
    ],
}

# Single sets, no rest step, a note on an unmapped exercise, a weight on a timed step.
ONE_SET_SPEC = {
    "name": "x",
    "sport": "strength",
    "steps": [
        {"type": "exercise", "title": "Farmer Carry", "note": "slow", "sets": 1, "seconds": 40, "weight_kg": 22.5,
         "rest_seconds": 0},
        {"type": "exercise", "category": "PLANK", "name": "PLANK", "sets": 1, "seconds": 60, "rest_seconds": 45},
        {"type": "exercise", "category": "PUSH_UP", "name": "PUSH_UP", "sets": 3, "reps": 12, "rest_seconds": 0},
    ],
}

# What both get_workout (of the stored workout) and create_workout (of the payload) show for STRENGTH_SPEC.
BENCH = {"exercise": "Barbell Bench Press", "category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS",
         "sets": 3, "reps": 8, "weight_kg": 60.0, "rest_seconds": 90}
PULL_UP = {"exercise": "Pull-up", "category": "PULL_UP", "name": "PULL_UP", "sets": 3, "reps": 6,
           "rest_seconds": 120, "note": "spike note on a mapped step"}
STRENGTH_STEPS = [
    BENCH,
    PULL_UP,
    {"sets": 2, "reps": 10, "rest_seconds": 60, "note": "Hevy: Zercher Carry"},
    {"exercise": "Plank", "category": "PLANK", "name": "PLANK", "sets": 2, "seconds": 45, "rest_seconds": 30},
]


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
        (lambda s: s["steps"].append({"type": "exercise", "title": "t", "sets": 1, "reps": 1, "rest_seconds": 0}),
         "exercise items are only supported"),
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


# ------------------------------------------------------------------ strength (PR 07)


def _strength(**changes):
    spec = copy.deepcopy(STRENGTH_SPEC)
    spec.update(changes)
    return spec


def _exercise_item(**changes):
    item = {"type": "exercise", "title": "t", "sets": 1, "reps": 1, "rest_seconds": 0}
    item.update(changes)
    return item


def test_build_strength_workout_equals_the_spike_payload():
    payload = w.build_garmin_workout(w.WorkoutSpec(**STRENGTH_SPEC))
    assert payload == fx.STRENGTH_UPLOAD  # the request body Garmin accepted on 2026-10-12
    assert payload["estimatedDurationInSecs"] == 1140
    bench = _steps(payload)[0]["workoutSteps"][0]
    assert bench["exerciseName"] == "BARBELL_BENCH_PRESS" and "description" not in bench  # the title is not sent


def test_strength_block_equals_the_library_helper():
    spec = w.WorkoutSpec(
        name="x", sport="strength",
        steps=[{"type": "exercise", "category": "SQUAT", "name": "BARBELL_BACK_SQUAT", "sets": 4, "reps": 5,
                "weight_kg": 82.5, "rest_seconds": 180}],
    )
    library = create_strength_set(
        "SQUAT", 1, 4, 5, 180, exercise_name="BARBELL_BACK_SQUAT", weight_kg=82.5
    ).model_dump(exclude_none=True, mode="json")
    assert _steps(w.build_garmin_workout(spec))[0] == library


def test_strength_single_set_and_no_rest():
    payload = w.build_garmin_workout(w.WorkoutSpec(**ONE_SET_SPEC))
    carry, plank, rest, group = _steps(payload)
    assert [(s["type"], s["stepOrder"]) for s in (carry, plank, rest)] == [("ExecutableStepDTO", n) for n in (1, 2, 3)]
    assert "category" not in carry and "exerciseName" not in carry
    assert carry["endCondition"]["conditionTypeKey"] == "time" and carry["endConditionValue"] == 40.0
    assert carry["weightValue"] == 22500.0 and carry["description"] == "Farmer Carry - slow"
    assert plank["category"] == "PLANK" and plank["endConditionValue"] == 60.0 and "description" not in plank
    assert rest["stepType"]["stepTypeKey"] == "rest" and rest["endConditionValue"] == 45.0
    assert group["type"] == "RepeatGroupDTO" and group["stepOrder"] == 4 and group["numberOfIterations"] == 3
    assert [(c["type"], c["stepOrder"]) for c in group["workoutSteps"]] == [("ExecutableStepDTO", 5)]  # no rest step
    assert payload["estimatedDurationInSecs"] == 235  # 40 + (60 + 45) + 3 * 30


@pytest.mark.parametrize("kg, grams", [(0.1, 100.0), (2.2679, 2268.0), (67.3, 67300.0), (500, 500000.0)])
def test_strength_weight_is_whole_grams(kg, grams):
    spec = w.WorkoutSpec(name="x", sport="strength", steps=[_exercise_item(weight_kg=kg)])
    step = _steps(w.build_garmin_workout(spec))[0]
    assert step["weightValue"] == grams
    assert step["weightUnit"] == {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}
    bodyweight = w.build_garmin_workout(w.WorkoutSpec(name="x", sport="strength", steps=[_exercise_item()]))
    assert "weightValue" not in _steps(bodyweight)[0] and "weightUnit" not in _steps(bodyweight)[0]


def test_strength_step_count():
    block = STRENGTH_SPEC["steps"][0]  # sets 3 with a rest step: 3 Garmin steps
    w.WorkoutSpec(**_strength(steps=[block] * 16))  # 48
    with pytest.raises(ValidationError, match=r"too many steps \(51\)"):
        w.WorkoutSpec(**_strength(steps=[block] * 17))
    w.WorkoutSpec(**_strength(steps=[_exercise_item()] * 30))  # 30 single sets without rest


def _append(item):
    return lambda s: s["steps"].append(item)


def _set(index, **changes):
    return lambda s: s["steps"][index].update(changes)


def _drop(index, key):
    return lambda s: s["steps"][index].pop(key)


def _both(*mutations):
    def run(s):
        for mutation in mutations:
            mutation(s)

    return run


INTERVAL = {"type": "interval", "end": {"type": "time", "seconds": 60}}
PACE = {"type": "pace", "slowest": "5:30", "fastest": "5:00"}
HR_ZONE = {"type": "heart_rate_zone", "zone": 2}
ONLY = "only exercise items"
NO_PACE = "takes no pace, power"
EITHER = "exactly one of reps and seconds"
TOGETHER = "category and name go together"
UNKNOWN = "unknown Garmin exercise"
STRENGTH_ROWS = [
    ("interval pace", _append({**INTERVAL, "target": PACE}), ONLY),
    ("interval power", _append({**INTERVAL, "target": {"type": "power", "min_watts": 200, "max_watts": 250}}), ONLY),
    ("interval heart rate", _append({**INTERVAL, "target": {"type": "heart_rate", "min_bpm": 120, "max_bpm": 140}}), ONLY),
    ("plain interval", _append(dict(INTERVAL)), ONLY),
    ("repeat", _append({"type": "repeat", "iterations": 2, "steps": [dict(INTERVAL)]}), ONLY),
    ("exercise with a pace target", _set(0, target=PACE), NO_PACE),
    ("exercise with a zone target", _set(0, target=HR_ZONE), NO_PACE),
    ("exercise in running", lambda s: s.update(sport="running"), "only supported for strength"),
    ("17 exercises in running", lambda s: s.update(sport="running", steps=[s["steps"][0]] * 17),
     "only supported for strength"),
    ("reps and seconds", _set(0, seconds=30), EITHER),
    ("neither", _drop(0, "reps"), EITHER),
    ("category without name", _drop(0, "name"), TOGETHER),
    ("name without category", _drop(0, "category"), TOGETHER),
    ("unknown pair", _set(0, name="BARBELL_BENCH_PRES"), "unknown Garmin exercise BENCH_PRESS/BARBELL_BENCH_PRES"),
    ("lower-case pair", _set(0, category="bench_press", name="barbell_bench_press"), UNKNOWN),
    ("swapped pair", _set(0, category="PULL_UP"), "unknown Garmin exercise PULL_UP/BARBELL_BENCH_PRESS"),
    ("unmapped without title", _drop(2, "title"), "needs a title"),
    # order: a rule that is checked earlier wins when two apply
    ("O1 only-exercises before the step limit",
     lambda s: s.update(steps=[{"type": "repeat", "iterations": 2, "steps": [dict(INTERVAL)]}] * 30), ONLY),
    ("O2 either before together", _both(_set(0, seconds=30), _drop(0, "name")), EITHER),
    ("O3 together before title", _drop(1, "category"), TOGETHER),
    ("O4 either before title", _both(_set(2, seconds=30), _drop(2, "title")), EITHER),
    ("O5 either before unknown", _both(_set(0, seconds=30), _set(0, name="BARBELL_BENCH_PRES")), EITHER),
    ("O6 target before the others", _both(_set(0, seconds=30), _set(0, target=HR_ZONE)), NO_PACE),
    # limits: maximum + 1 (the maxima are in test_strength_limits_accepted)
    ("category of 61", _set(0, category="C" * 61), "at most 60 characters"),
    ("name of 81", _set(0, name="N" * 81), "at most 80 characters"),
    ("empty title", _set(2, title=""), "at least 1 character"),
    ("sets 0", _set(0, sets=0), "greater than or equal to 1"),
    ("sets 21", _set(0, sets=21), "less than or equal to 20"),
    ("reps 0", _set(0, reps=0), "greater than or equal to 1"),
    ("reps 201", _set(0, reps=201), "less than or equal to 200"),
    ("seconds 4", _set(3, seconds=4), "greater than or equal to 5"),
    ("seconds 3601", _set(3, seconds=3601), "less than or equal to 3600"),
    ("weight 0", _set(0, weight_kg=0), "greater than or equal to 0.1"),
    ("weight 500.1", _set(0, weight_kg=500.1), "less than or equal to 500"),
    ("rest -1", _set(0, rest_seconds=-1), "greater than or equal to 0"),
    ("rest 901", _set(0, rest_seconds=901), "less than or equal to 900"),
    ("rest missing", _drop(0, "rest_seconds"), "Field required"),
    ("title of 61", _set(2, title="t" * 61), "at most 60 characters"),
    ("note of 101", _set(1, note="n" * 101), "at most 100 characters"),
    ("unknown key", _set(0, bogus=1), "Extra inputs"),
    ("17 blocks", lambda s: s.update(steps=[s["steps"][0]] * 17), r"too many steps \(51\)"),
    ("31 items", lambda s: s.update(steps=[_exercise_item()] * 31), "at most 30 items"),
]


@pytest.mark.parametrize("mutate, message", [row[1:] for row in STRENGTH_ROWS], ids=[row[0] for row in STRENGTH_ROWS])
def test_strength_spec_validation(mutate, message):
    spec = _strength()
    mutate(spec)
    with pytest.raises(ValidationError, match=message):
        w.WorkoutSpec(**spec)


def test_strength_limits_accepted():
    def build(index=None, *, steps=None, **changes):
        spec = _strength()
        if index is not None:
            spec["steps"][index].update(changes)
        if steps is not None:
            spec["steps"] = steps
        return w.build_garmin_workout(w.WorkoutSpec(**spec))

    build(0, sets=20, reps=200, weight_kg=500, rest_seconds=900)
    build(3, seconds=3600, sets=1)
    build(3, seconds=5, rest_seconds=0, weight_kg=0.1)
    build(2, title="t" * 60)
    build(1, note="n" * 100)
    build(steps=[STRENGTH_SPEC["steps"][0]] * 16)
    thirty = build(steps=[_exercise_item()] * 30)
    assert len(_steps(thirty)) == 30


def test_summarize_strength_workout():
    out = w.summarize_workout(fx.STRENGTH_WORKOUT_DETAIL, with_steps=True)
    assert out == {
        "workout_id": 9002,
        "name": "MAO spike PR07 - delete me",
        "sport": "strength_training",
        "description": "Test upload of PR 07 (strength workouts on the watch). Safe to delete.",
        "estimated_duration_min": 19,
        "updated": "2026-10-12",
        "steps": STRENGTH_STEPS,
        "url": "https://connect.garmin.com/modern/workout/9002",
    }


LAP = {"conditionTypeId": 1, "conditionTypeKey": "lap.button"}
REST = {"type": "rest", "end": "time 1:30"}


def _nested(entry):
    """An exercise entry as shown inside a repeat that is not a plain set block (no sets, no rest)."""
    return {k: v for k, v in entry.items() if k not in ("sets", "rest_seconds")}


def _v2(g):
    g[0]["workoutSteps"].append({**copy.deepcopy(g[1]["workoutSteps"][0]), "stepOrder": 3.5})


def _v3(g):
    g[0]["workoutSteps"][0]["stepOrder"] = 3.5


def _v4(g):
    g[0]["workoutSteps"][1].update(endCondition=LAP, endConditionValue=None)


def _v5(g):
    g[0]["workoutSteps"][0]["endCondition"] = LAP  # the value 8.0 stays


def _v6(g):
    g[0:1] = g[0]["workoutSteps"]


def _v7(g):
    g[0] = {"type": "RepeatGroupDTO", "stepOrder": 1, "numberOfIterations": 2, "workoutSteps": [g[0]]}


def _v13(g):
    g[0]["workoutSteps"].pop(0)


def _v16(g):
    g[0]["workoutSteps"].append({**copy.deepcopy(g[0]["workoutSteps"][1]), "stepOrder": 3.5})


def _v18(g):
    g[0]["workoutSteps"][1] = {**copy.deepcopy(g[3]["workoutSteps"][0]), "stepOrder": 3}


def _v20(g):
    g.insert(0, "junk")
    g[1]["workoutSteps"].append(None)


def _first(**changes):
    return lambda g: g[0]["workoutSteps"][0].update(changes)


VARIANTS = [
    ("V1 no rest", lambda g: g[0]["workoutSteps"].pop(), [{k: v for k, v in BENCH.items() if k != "rest_seconds"}]),
    ("V2 three children", _v2, [{"repeat": 3, "steps": [_nested(BENCH), REST, _nested(PULL_UP)]}]),
    ("V3 rest first", _v3, [{"repeat": 3, "steps": [REST, _nested(BENCH)]}]),
    ("V4 rest by lap button", _v4, [{"repeat": 3, "steps": [_nested(BENCH), {"type": "rest", "end": "lap button"}]}]),
    ("V5 exercise by lap button", _v5, [{**{k: v for k, v in BENCH.items() if k != "reps"}, "end": "lap button"}]),
    ("V6 no group", _v6, [{**{k: v for k, v in BENCH.items() if k != "rest_seconds"}, "sets": 1}, REST]),
    ("V7 group in a group", _v7, [{"repeat": 2, "steps": [BENCH]}]),
    ("V8 name not in the catalogue", _first(exerciseName="NOT_A_GARMIN_NAME"),
     [{**{k: v for k, v in BENCH.items() if k != "exercise"}, "name": "NOT_A_GARMIN_NAME"}]),
    ("V9 no name", _first(exerciseName=None), [{k: v for k, v in BENCH.items() if k not in ("exercise", "name")}]),
    ("V10 weight not a number", _first(weightValue="heavy"), [{k: v for k, v in BENCH.items() if k != "weight_kg"}]),
    ("V11 weight zero", _first(weightValue=0.0), [{k: v for k, v in BENCH.items() if k != "weight_kg"}]),
    ("V12 repeat by stepType only", lambda g: g[0].update(type="SomethingElse"), [BENCH]),
    ("V13 lone rest", _v13, [{"repeat": 3, "steps": [REST]}]),
    ("V14 reversed", lambda g: g.reverse(), [BENCH]),
    ("V15 weight 82.5", _first(weightValue=82500.0), [{**BENCH, "weight_kg": 82.5}]),
    ("V16 two rests", _v16, [{"repeat": 3, "steps": [_nested(BENCH), REST, REST]}]),
    ("V17 repeat by type only", lambda g: g[0].update(stepType={"stepTypeKey": "other"}), [BENCH]),
    ("V18 exercise then a timed exercise", _v18,
     [{"repeat": 3, "steps": [_nested(BENCH),
                              {"exercise": "Plank", "category": "PLANK", "name": "PLANK", "seconds": 45}]}]),
    ("V19 empty group", lambda g: g[0].update(workoutSteps=[]), [{"repeat": 3, "steps": []}, PULL_UP]),
    ("V20 non-dict steps", _v20, [BENCH, PULL_UP]),
]


@pytest.mark.parametrize("change, expected", [row[1:] for row in VARIANTS], ids=[row[0] for row in VARIANTS])
def test_summarize_strength_variants(change, expected):
    segment_steps = copy.deepcopy(fx.STRENGTH_WORKOUT_DETAIL)["workoutSegments"][0]["workoutSteps"]
    change(segment_steps)
    assert w.summarize_strength_steps(segment_steps)[: len(expected)] == expected


def test_list_row_reads_update_date():
    row = copy.deepcopy(fx.STRENGTH_WORKOUT_ROW)
    assert w.summarize_workout(row, with_steps=False) == {
        "workout_id": 9002, "name": "Strength A", "sport": "strength_training",
        "estimated_duration_min": 19, "updated": "2026-10-12"}
    del row["updateDate"]
    assert w.summarize_workout(row, with_steps=False)["updated"] == "2026-10-10"  # createdDate
    row["updatedDate"] = "2026-10-11T08:00:00.0"
    assert w.summarize_workout(row, with_steps=False)["updated"] == "2026-10-11"  # updatedDate wins
    row["updateDate"] = "2026-10-12T08:00:00.0"
    assert w.summarize_workout(row, with_steps=False)["updated"] == "2026-10-11"  # ... over updateDate


def test_strength_steps_under_another_sport_keep_the_old_summary():
    detail = copy.deepcopy(fx.STRENGTH_WORKOUT_DETAIL)
    detail["sportType"] = {"sportTypeId": 1, "sportTypeKey": "running"}
    first = w.summarize_workout(detail, with_steps=True)["steps"][0]
    assert first == {"repeat": 3, "steps": [{"type": "interval", "end": "reps"}, {"type": "rest", "end": "time 1:30"}]}


@pytest.mark.anyio
async def test_create_strength_workout_uploads_schedules_and_reports_unmapped(service, fake):
    day = (today("Europe/Prague") + timedelta(days=2)).isoformat()
    out = await w.create_workout(service, **STRENGTH_SPEC, schedule_date=day)
    assert fake.uploaded[0] == fx.STRENGTH_UPLOAD
    assert out["workout_id"] == 424242 and out["sport"] == "strength" and out["estimated_duration_min"] == 19
    assert out["steps"] == STRENGTH_STEPS  # the summary of the payload equals that of the stored workout
    assert out["unmapped"] == ["Hevy: Zercher Carry"]
    assert out["scheduled"] == {"date": day, "schedule_id": 31337}
    assert fake.scheduled == [(424242, day)]


@pytest.mark.anyio
async def test_create_strength_workout_all_mapped_has_no_unmapped_key(service):
    spec = _strength(steps=[s for s in STRENGTH_SPEC["steps"] if "category" in s])
    out = await w.create_workout(service, **spec, schedule_date=None)
    assert out["workout_id"] == 424242 and "unmapped" not in out


@pytest.mark.anyio
async def test_create_strength_workout_unmapped_is_the_title_not_the_note(service):
    out = await w.create_workout(service, **ONE_SET_SPEC, description=None, schedule_date=None)
    assert out["unmapped"] == ["Farmer Carry"]  # the exercise has the note "slow"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad, message",
    [
        ({**INTERVAL, "target": PACE}, ONLY),
        ({**INTERVAL, "target": {"type": "power", "min_watts": 200, "max_watts": 250}}, ONLY),
        ({**INTERVAL, "target": {"type": "heart_rate", "min_bpm": 120, "max_bpm": 140}}, ONLY),
        ({**STRENGTH_SPEC["steps"][0], "target": PACE}, NO_PACE),
    ],
    ids=["pace", "power", "heart rate", "exercise target"],
)
async def test_create_strength_workout_rejects_before_upload(service, fake, bad, message):
    spec = _strength(steps=[*STRENGTH_SPEC["steps"], bad])
    with pytest.raises(ToolError, match=message):
        await w.create_workout(service, **spec, schedule_date=None)
    assert fake.uploaded == []
