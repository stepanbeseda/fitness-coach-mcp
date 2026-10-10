import json

import pytest
from garminconnect import GarminConnectNotFoundError
from mcp.server.mcpserver.exceptions import ToolError

import fixtures as fx
from conftest import AUTH_ERROR, CONN_ERROR
from garmin_coach_mcp import activities

WEATHER_KEYS = {
    "temp_c", "feels_like_c", "dew_point_c", "humidity_pct", "wind_kmh", "wind_gust_kmh", "wind_from", "conditions",
}
UNAVAILABLE = {"available": False}
NOT_FOUND = GarminConnectNotFoundError("API Error 404")


def test_row_shapes_per_sport():
    run, bike, strength = (activities.shape_activity_row(a) for a in fx.ACTIVITIES)
    assert run["avg_pace_min_per_km"] == "5:00"
    assert run["distance_km"] == 10.02 and run["duration_min"] == 50.1
    assert run["avg_cadence_spm"] == 172 and run["te_label"] == "TEMPO"
    assert "avg_speed_kmh" not in run
    assert bike["avg_speed_kmh"] == 27.4 and bike["norm_power_w"] == 205
    assert "avg_pace_min_per_km" not in bike and "max_speed_kmh" not in bike
    assert strength["sets"] == 18 and "distance_km" not in strength


@pytest.mark.anyio
async def test_list_activities_sorted_and_limited(service, fake):
    out = await activities.list_activities(service, 7, None, "2026-09-30", None, 2)
    assert out["start"] == "2026-09-24" and out["count"] == 3
    assert [a["id"] for a in out["activities"]] == [20001, 20003]
    assert out["truncated"] is True
    name, args = next(c for c in fake.calls if c[0] == "get_activities_by_date")
    assert args == ("2026-09-24", "2026-09-30", None)


@pytest.mark.anyio
async def test_list_activities_validates(service):
    with pytest.raises(ToolError):
        await activities.list_activities(service, 7, None, None, None, 500)


@pytest.mark.anyio
async def test_get_activity_detail(service, fake):
    out = await activities.get_activity(service, 20001)
    assert out["name"] == "Praha Běh" and out["type"] == "running"
    assert out["duration"] == "50:05" and out["avg_pace_min_per_km"] == "5:00"
    assert len(out["laps"]) == 10 and out["laps"][0]["duration"] == "4:59"
    assert [z["zone"] for z in out["hr_zones"]] == [1, 2, 3, 4]
    assert out["hr_zones"][2] == {"zone": 3, "from_bpm": 140, "minutes": 25.0, "percent": 50}
    assert "exercise_sets" not in out and "active_sets" not in out and "total_reps" not in out
    assert out["weather"]["temp_c"] == 15.0
    assert ("get_activity_weather", ("20001",)) in fake.calls
    assert ("get_activity_exercise_sets", ("20001",)) in fake.calls


@pytest.mark.anyio
async def test_get_activity_tolerates_missing_splits(service, fake):
    fake.fail["get_activity_splits"] = CONN_ERROR
    out = await activities.get_activity(service, 20001)
    assert "laps_error" in out and out["name"] == "Praha Běh"


@pytest.mark.anyio
async def test_get_activity_main_failure_raises(service, fake):
    fake.fail["get_activity"] = CONN_ERROR
    with pytest.raises(ToolError, match="500"):
        await activities.get_activity(service, 20001)


# ------------------------------------------------- PR 19: exercise sets and weather

SETS_EXPECTED = {
    "count": 4,
    "exercises": [
        {"exercise": "barbell_back_squat", "sets": 2, "reps": 16, "max_weight_kg": 82.5},
        {"exercise": "barbell_bench_press", "sets": 1, "reps": 10, "max_weight_kg": 60.0},
        {"exercise": "plank", "sets": 1},
    ],
    "sets": [
        {"set": 1, "exercise": "barbell_back_squat", "category": "squat", "reps": 8, "weight_kg": 80.0,
         "duration_s": 42},
        {"set": 2, "exercise": "barbell_back_squat", "category": "squat", "reps": 8, "weight_kg": 82.5,
         "duration_s": 40},
        {"set": 3, "exercise": "barbell_bench_press", "category": "bench_press", "reps": 10, "weight_kg": 60.0,
         "duration_s": 35},
        {"set": 4, "exercise": "plank", "duration_s": 60},
    ],
}


def _sets(*items):
    return {"activityId": 1, "exerciseSets": list(items)}


def _plain(**fields):
    return {"setType": "ACTIVE", "exercises": [{"category": "CURL", "probability": 90.0}], **fields}


def test_shape_exercise_sets():
    assert activities.shape_exercise_sets(fx.EXERCISE_SETS) == SETS_EXPECTED


def test_shape_exercise_sets_branches():
    shape = activities.shape_exercise_sets
    # (a) a rest set is skipped even when it carries reps and weight
    assert shape(_sets(_plain(setType="REST", repetitionCount=0, weight=0.0), _plain(repetitionCount=5))) == {
        "count": 1, "exercises": [{"exercise": "curl", "sets": 1, "reps": 5}],
        "sets": [{"set": 1, "exercise": "curl", "reps": 5}],
    }
    # (b) a set without setType counts as active
    no_type = {"exercises": [{"category": "CURL"}], "repetitionCount": 5}
    assert shape(_sets(no_type))["count"] == 1
    # (c) nothing in the set: no exercise, no reps, no weight
    assert shape(_sets({"setType": "ACTIVE", "exercises": [], "duration": 30.0})) == UNAVAILABLE
    # (d) zero and negative weights are no weight
    out = shape(_sets(_plain(weight=-1000.0, repetitionCount=3), _plain(weight=0.0, repetitionCount=3)))
    assert out["count"] == 2 and all("weight_kg" not in r for r in out["sets"])
    assert "max_weight_kg" not in out["exercises"][0]
    # (e) a single exercise dict instead of a list
    single = shape(_sets({"setType": "ACTIVE", "exercises": {"category": "ROW", "name": "SEATED_ROW"}}))
    assert single["sets"] == [{"set": 1, "exercise": "seated_row", "category": "row"}]
    # (f) the captured single item: category only, reps / weight / name null
    item = fx.exercise_set(0, "CURL", None, 90.0, None, None, 30.0, "ACTIVE", "00:00")
    assert shape(_sets(item)) == {
        "count": 1,
        "exercises": [{"exercise": "curl", "sets": 1}],
        "sets": [{"set": 1, "exercise": "curl", "duration_s": 30}],
    }
    # an exercise-less set that has reps is kept and summarised as "unknown"
    unknown = shape(_sets({"setType": "ACTIVE", "repetitionCount": 12}))
    assert unknown["exercises"] == [{"exercise": "unknown", "sets": 1, "reps": 12}]
    assert unknown["sets"] == [{"set": 1, "reps": 12}]
    # non-dict sets are ignored
    assert shape({"exerciseSets": ["x", None, 3]}) == UNAVAILABLE


def test_shape_exercise_sets_summary_and_choice_rules():
    shape = activities.shape_exercise_sets
    # max_weight_kg is the maximum, not the last weight
    falling = shape(_sets(_plain(repetitionCount=5, weight=82500.0), _plain(repetitionCount=5, weight=80000.0)))
    assert falling["exercises"] == [{"exercise": "curl", "sets": 2, "reps": 10, "max_weight_kg": 82.5}]
    # setType is compared case-insensitively
    lower = shape(_sets(_plain(setType="rest", repetitionCount=5), _plain(setType="Rest", repetitionCount=5)))
    assert lower == UNAVAILABLE
    # equal probabilities: the first candidate wins
    tie = {"setType": "ACTIVE", "exercises": [{"category": "ROW", "name": "FIRST", "probability": 50.0},
                                              {"category": "ROW", "name": "SECOND", "probability": 50.0}]}
    assert shape(_sets(tie))["sets"] == [{"set": 1, "exercise": "first", "category": "row"}]
    # a candidate without a probability loses against one with a probability
    mixed = {"setType": "ACTIVE", "exercises": [{"category": "ROW", "name": "NONE"},
                                                {"category": "ROW", "name": "SOME", "probability": 1.0}]}
    assert shape(_sets(mixed))["sets"][0]["exercise"] == "some"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        fx.EXERCISE_SETS_NONE,
        {"exerciseSets": [{"setType": "REST", "duration": 60}]},
        _sets(fx.exercise_set(0, "CURL", None, 90.0, None, None, 30.0, "REST", "00:00")),
    ],
)
def test_shape_exercise_sets_unavailable(raw):
    assert activities.shape_exercise_sets(raw) == UNAVAILABLE


def test_shape_exercise_sets_truncates():
    out = activities.shape_exercise_sets(_sets(*[_plain(repetitionCount=10)] * 61))
    assert out["count"] == 61 and len(out["sets"]) == 60 and out["sets_truncated"] == 61
    assert out["exercises"] == [{"exercise": "curl", "sets": 61, "reps": 610}]
    exact = activities.shape_exercise_sets(_sets(*[_plain(repetitionCount=10)] * 60))
    assert len(exact["sets"]) == 60 and "sets_truncated" not in exact


def test_shape_activity_weather():
    out = activities.shape_activity_weather(fx.ACTIVITY_WEATHER)
    assert out == {
        "temp_c": 15.0, "feels_like_c": 13.9, "dew_point_c": 8.9, "humidity_pct": 67, "wind_kmh": 14,
        "wind_from": "SSW", "conditions": "Mostly Cloudy",
    }
    assert set(out) <= WEATHER_KEYS
    text = json.dumps(out)
    assert "45.0" not in text and "Synthetic" not in text


def test_shape_activity_weather_guards():
    shape = activities.shape_activity_weather
    odd = {"temp": 212, "relativeHumidity": 140, "windSpeed": -3, "windDirectionCompassPoint": "NORTH",
           "weatherTypeDTO": {"desc": ""}}
    assert shape(odd) == UNAVAILABLE
    assert shape({"dewPoint": 50}) == UNAVAILABLE
    assert shape({"temp": 59, "windSpeed": 0}) == {"temp_c": 15.0, "wind_kmh": 0}
    assert shape({"temp": 59, "windGust": 17}) == {"temp_c": 15.0, "wind_gust_kmh": 27}
    assert shape({"temp": 1e308}) == UNAVAILABLE
    assert shape(fx.ACTIVITY_WEATHER_INDOOR) == UNAVAILABLE
    for raw in (None, {}, "text"):
        assert shape(raw) == UNAVAILABLE
    # each guard drops only its own value
    assert shape({"temp": 59, "relativeHumidity": 140}) == {"temp_c": 15.0}
    assert shape({"temp": 59, "relativeHumidity": 100}) == {"temp_c": 15.0, "humidity_pct": 100}
    assert shape({"temp": 59, "relativeHumidity": 0}) == {"temp_c": 15.0, "humidity_pct": 0}
    assert shape({"temp": 59, "windSpeed": -3}) == {"temp_c": 15.0}
    assert shape({"temp": 59, "windDirectionCompassPoint": "NORTH"}) == {"temp_c": 15.0}
    assert shape({"temp": 59, "windDirectionCompassPoint": ""}) == {"temp_c": 15.0}
    assert shape({"temp": 59, "weatherTypeDTO": {"desc": "x" * 61}}) == {"temp_c": 15.0}
    assert shape({"temp": 59, "weatherTypeDTO": {"desc": "x" * 60}}) == {"temp_c": 15.0, "conditions": "x" * 60}
    assert shape({"temp": 59, "weatherTypeDTO": "text"}) == {"temp_c": 15.0}
    assert shape({"temp": -76}) == {"temp_c": -60.0}
    assert shape({"temp": -77}) == UNAVAILABLE
    assert shape({"temp": -77, "windSpeed": 1}) == {"wind_kmh": 2}
    assert shape({"temp": 140}) == {"temp_c": 60.0}
    assert shape({"temp": 141}) == UNAVAILABLE


def _strength(fake):
    fake.responses["get_activity"] = fx.ACTIVITY_STRENGTH
    fake.responses["get_activity_exercise_sets"] = fx.EXERCISE_SETS
    fake.responses["get_activity_weather"] = fx.ACTIVITY_WEATHER_INDOOR


@pytest.mark.anyio
async def test_get_activity_strength_sets(service, fake):
    _strength(fake)
    out = await activities.get_activity(service, 20003)
    assert out["exercise_sets"] == SETS_EXPECTED
    assert out["type"] == "strength_training"
    assert out["active_sets"] == 4 and out["total_reps"] == 26
    assert out["weather"] == UNAVAILABLE


SECTION_ROWS = [
    ("strength", fx.EXERCISE_SETS, SETS_EXPECTED),
    ("strength", None, UNAVAILABLE),
    ("strength", {}, UNAVAILABLE),
    ("strength", fx.EXERCISE_SETS_NONE, UNAVAILABLE),
    ("strength", NOT_FOUND, UNAVAILABLE),
    ("strength", CONN_ERROR, "error"),
    ("run", fx.EXERCISE_SETS, SETS_EXPECTED),
    ("run", fx.EXERCISE_SETS_NONE, None),
    ("run", {}, None),
    ("run", NOT_FOUND, None),
    ("run", CONN_ERROR, None),
]


@pytest.mark.anyio
@pytest.mark.parametrize(("kind", "answer", "expected"), SECTION_ROWS)
async def test_get_activity_sections_degrade(service, fake, kind, answer, expected):
    if kind == "strength":
        fake.responses["get_activity"] = fx.ACTIVITY_STRENGTH
    if isinstance(answer, Exception):
        fake.fail["get_activity_exercise_sets"] = answer
    else:
        fake.responses["get_activity_exercise_sets"] = answer
    out = await activities.get_activity(service, 20003)
    assert out["name"]
    if expected is None:
        assert "exercise_sets" not in out
    elif expected == "error":
        assert "500" in out["exercise_sets"]["error"]
    else:
        assert out["exercise_sets"] == expected


@pytest.mark.anyio
async def test_get_activity_weather_degrades(service, fake):
    fake.fail["get_activity_weather"] = NOT_FOUND
    out = await activities.get_activity(service, 20001)
    assert out["weather"] == UNAVAILABLE and len(out["laps"]) == 10 and out["hr_zones"]
    fake.fail["get_activity_weather"] = CONN_ERROR
    out = await activities.get_activity(service, 20001)
    assert "500" in out["weather"]["error"] and len(out["laps"]) == 10 and out["hr_zones"]
    del fake.fail["get_activity_weather"]
    fake.responses["get_activity_weather"] = {}
    out = await activities.get_activity(service, 20001)
    assert out["weather"] == UNAVAILABLE and len(out["laps"]) == 10 and out["hr_zones"]


@pytest.mark.anyio
async def test_get_activity_odd_weather_payload_is_section_error(service, fake, monkeypatch):
    def boom(raw):
        raise KeyError("x")

    monkeypatch.setattr(activities, "shape_activity_weather", boom)
    out = await activities.get_activity(service, 20001)
    assert out["weather"] == {"error": "unexpected Garmin response for weather"} and out["name"]


@pytest.mark.anyio
async def test_get_activity_auth_error_in_new_section_fails_tool(service, fake):
    fake.fail["get_activity_weather"] = AUTH_ERROR
    with pytest.raises(ToolError, match="garmin-login.sh"):
        await activities.get_activity(service, 20001)


@pytest.mark.anyio
async def test_get_activity_sections_survive_failed_zones(service, fake):
    _strength(fake)
    fake.fail["get_activity_hr_in_timezones"] = CONN_ERROR
    out = await activities.get_activity(service, 20003)
    assert "hr_zones" not in out
    assert out["exercise_sets"]["count"] == 4 and out["weather"] == UNAVAILABLE
    del fake.fail["get_activity_hr_in_timezones"]
    fake.responses["get_activity_hr_in_timezones"] = []
    out = await activities.get_activity(service, 20003)
    assert "hr_zones" not in out
    assert out["exercise_sets"]["count"] == 4 and out["weather"] == UNAVAILABLE
