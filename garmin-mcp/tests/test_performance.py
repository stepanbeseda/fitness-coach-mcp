import json
from datetime import date, timedelta

import pytest
from garminconnect import GarminConnectNotFoundError
from mcp.server.mcpserver.exceptions import ToolError

import fixtures as fx
from conftest import AUTH_ERROR, CONN_ERROR
from garmin_coach_mcp import performance
from garmin_coach_mcp.formatting import today

END = date(2026, 9, 30)

VO2MAX_EXPECTED = {
    "running": 53.4,
    "cycling": 55.1,
    "measured_on": "2026-09-28",
    "change_running": 0.8,
    "change_cycling": 0.2,
    "weekly": [
        {"week": "2026-07-06", "running": 52.8, "cycling": 54.9},
        {"week": "2026-09-14", "cycling": 55.1},
        {"week": "2026-09-21", "running": 53.1},
        {"week": "2026-09-28", "running": 53.4},
    ],
}


# ------------------------------------------------------------------ shapers


def test_shape_race_predictions_formats_with_helpers():
    assert performance.shape_race_predictions(fx.RACE_PREDICTIONS) == {
        "date": "2026-09-30",
        "5k": {"time": "21:30", "pace": "4:18"},
        "10k": {"time": "45:00", "pace": "4:30"},
        "half_marathon": {"time": "1:39:30", "pace": "4:43"},
        "marathon": {"time": "3:25:10", "pace": "4:52"},
    }


def test_shape_race_predictions_list_and_empty():
    older = {**fx.RACE_PREDICTIONS, "calendarDate": "2026-09-01", "time5K": 1400}
    out = performance.shape_race_predictions([older, fx.RACE_PREDICTIONS])
    assert out["5k"]["time"] == "21:30"
    out = performance.shape_race_predictions([fx.RACE_PREDICTIONS, older])
    assert out["5k"]["time"] == "21:30"
    assert performance.shape_race_predictions({}) == {"available": False}
    assert performance.shape_race_predictions(None) == {"available": False}


def test_lt_speed_scaling():
    assert performance.lt_speed_mps(0.3636) == pytest.approx(3.636)
    assert performance.lt_speed_mps(3.636) == pytest.approx(3.636)
    assert performance.lt_speed_mps(None) is None
    assert performance.lt_speed_mps(0) is None


def test_shape_lactate_threshold():
    assert performance.shape_lactate_threshold(fx.LACTATE_THRESHOLD) == {
        "date": "2026-09-12",
        "heart_rate": 172,
        "pace": "4:35",
        "power_watts": 345,
        "power_to_weight": 4.79,
    }
    no_power = performance.shape_lactate_threshold({**fx.LACTATE_THRESHOLD, "power": {}})
    assert "power_watts" not in no_power and no_power["pace"] == "4:35"
    assert performance.shape_lactate_threshold({"speed_and_heart_rate": {}, "power": {}}) == {"available": False}
    assert performance.shape_lactate_threshold(None) == {"available": False}


def test_shape_heart_rate_zones():
    out = performance.shape_heart_rate_zones(fx.HEART_RATE_ZONES)
    profiles = out["profiles"]
    assert len(profiles) == 3  # SWIMMING has no floors
    assert profiles[0] == {
        "sport": "default",
        "method": "LACTATE_THRESHOLD",
        "max_hr": 191,
        "resting_hr": 48,
        "threshold_hr": 172,
        "zones": [
            {"zone": 1, "min_bpm": 103, "max_bpm": 137},
            {"zone": 2, "min_bpm": 138, "max_bpm": 154},
            {"zone": 3, "min_bpm": 155, "max_bpm": 162},
            {"zone": 4, "min_bpm": 163, "max_bpm": 171},
            {"zone": 5, "min_bpm": 172, "max_bpm": 191},
        ],
    }
    assert profiles[1]["sport"] == "running"
    assert profiles[2]["sport"] == "cycling" and "threshold_hr" not in profiles[2]
    assert performance.shape_heart_rate_zones([]) == {"available": False}


def test_shape_power_zones_cycling_only():
    out = performance.shape_power_zones(fx.POWER_ZONES)
    assert out["sport"] == "cycling" and out["ftp_watts"] == 250
    assert len(out["zones"]) == 7
    assert out["zones"][0] == {"zone": 1, "min_watts": 0, "max_watts": 137}
    assert out["zones"][6] == {"zone": 7, "min_watts": 375}
    assert performance.shape_power_zones([fx.POWER_ZONES[1]]) == {"available": False}


def test_shape_endurance_score():
    assert performance.shape_endurance_score(fx.ENDURANCE_SCORE) == {
        "date": "2026-09-30",
        "score": 7250,
        "classification": 4,
        "change": 240,
    }
    no_dto = {k: v for k, v in fx.ENDURANCE_SCORE.items() if k != "enduranceScoreDTO"}
    out = performance.shape_endurance_score(no_dto)
    assert out["score"] == 7240 and out["date"] == "2026-09-28" and out["change"] == 230
    assert performance.shape_endurance_score({}) == {"available": False}


def test_shape_hill_score():
    assert performance.shape_hill_score(fx.HILL_SCORE) == {
        "date": "2026-09-29",
        "score": 68,
        "strength": 60,
        "endurance": 66,
        "classification": 3,
        "change": 6,
    }
    assert performance.shape_hill_score({"hillScoreDTOList": []}) == {"available": False}


def test_shape_vo2max_trend():
    assert performance.shape_vo2max_trend(fx.MAX_METRICS_RANGE) == VO2MAX_EXPECTED
    assert performance.shape_vo2max_trend([]) == {"available": False}


def test_shape_personal_records():
    out = performance.shape_personal_records(fx.PERSONAL_RECORDS)
    assert out["count"] == 4
    assert out["records"] == [
        {"type": "5k", "time": "21:05", "pace": "4:13", "date": "2026-05-02", "activity_id": 20001},
        {"type": "marathon", "time": "3:30:50", "pace": "5:00", "date": "2025-10-12", "activity_id": 20002},
        {"type": "longest_run", "distance_km": 43.12, "date": "2025-09-14", "activity_id": 20003},
        {"type_id": 12, "value": 31245.0, "date": "2026-06-01"},
    ]
    assert performance.shape_personal_records(None) == {"count": 0, "records": []}


def test_shape_gear():
    shoes = performance.shape_gear_item(fx.GEAR[0], fx.GEAR_STATS[fx.GEAR_UUID_SHOES])
    assert shoes == {
        "name": "Pegasus 40",
        "type": "shoes",
        "retired": False,
        "since": "2025-11-01",
        "distance_km": 652.34,
        "activities": 81,
        "max_km": 700.0,
    }
    bike = performance.shape_gear_item(fx.GEAR[2], fx.GEAR_STATS[fx.GEAR_UUID_BIKE])
    assert bike["name"] == "Canyon Endurace" and "max_km" not in bike
    old = performance.shape_gear_item(fx.GEAR[1], {})
    assert old["retired"] is True and old["until"] == "2025-10-30" and "distance_km" not in old


def test_entries_accepts_list_wrapped_and_single():
    assert performance._entries([{"a": 1}, "x"], "a") == [{"a": 1}]
    assert performance._entries({"meta": 1, "items": [{"a": 1}]}, "a") == [{"a": 1}]
    assert performance._entries({"a": 1}, "a") == [{"a": 1}]
    assert performance._entries({"meta": 1}, "a") == []
    assert performance._entries(None, "a") == []
    assert performance._entries("text", "a") == []


def test_shape_personal_records_dict_shapes():
    assert performance.shape_personal_records({"personalRecords": fx.PERSONAL_RECORDS})["count"] == 4
    assert performance.shape_personal_records(fx.PERSONAL_RECORDS[2])["records"] == [
        {"type": "5k", "time": "21:05", "pace": "4:13", "date": "2026-05-02", "activity_id": 20001}
    ]
    assert performance.shape_personal_records({"x": 1}) == {"count": 0, "records": []}


def test_shape_vo2max_trend_dict_shapes():
    assert performance.shape_vo2max_trend(fx.MAX_METRICS_RANGE[3]) == {
        "running": 53.4,
        "measured_on": "2026-09-28",
        "weekly": [{"week": "2026-09-28", "running": 53.4}],
    }
    assert performance.shape_vo2max_trend({"x": 1}) == {"available": False}


# -------------------------------------------------------------------- tools


@pytest.mark.anyio
async def test_performance_metrics_all_sections(service, fake):
    out = await performance.performance_metrics(service, 12, end=END)
    assert out["window"] == {"start": "2026-07-09", "end": "2026-09-30", "weeks": 12}
    for key, section in out.items():
        if key != "window":
            assert "error" not in section and "available" not in section, key
    assert out["race_predictions"]["marathon"]["time"] == "3:25:10"
    assert out["lactate_threshold"]["pace"] == "4:35"
    assert len(out["heart_rate_zones"]["profiles"]) == 3
    assert out["vo2max"]["running"] == 53.4
    assert out["vo2max"] == VO2MAX_EXPECTED
    window = ("2026-07-09", "2026-09-30")
    assert ("get_max_metrics_range", window) in fake.calls
    assert ("get_endurance_score", window) in fake.calls
    assert ("get_hill_score", window) in fake.calls
    names = {n for n, _ in fake.calls}
    assert {"get_race_predictions", "get_lactate_threshold", "get_heart_rate_zones", "get_power_zones"} <= names
    assert len(json.dumps(out)) < 4000


@pytest.mark.anyio
async def test_performance_metrics_degrades_per_section(service, fake):
    fake.fail["get_race_predictions"] = CONN_ERROR
    fake.responses["get_heart_rate_zones"] = None
    fake.responses["get_endurance_score"] = {"weird": True}
    out = await performance.performance_metrics(service, 12, end=END)
    assert "500" in out["race_predictions"]["error"]
    assert out["heart_rate_zones"] == {"available": False}
    assert out["endurance_score"] == {"available": False}
    assert out["vo2max"]["running"] == 53.4
    assert out["lactate_threshold"]["heart_rate"] == 172


@pytest.mark.anyio
async def test_performance_metrics_odd_payload_is_section_error(service, fake):
    fake.responses["get_max_metrics_range"] = [
        {"generic": {"calendarDate": "not-a-date", "vo2MaxPreciseValue": 50}}
    ]
    out = await performance.performance_metrics(service, 12, end=END)
    assert out["vo2max"] == {"error": "unexpected Garmin response for vo2max"}
    assert out["race_predictions"]["5k"]["time"] == "21:30"


@pytest.mark.anyio
async def test_performance_metrics_lactate_library_crash_is_fail_soft(service, fake):
    fake.fail["get_lactate_threshold"] = TypeError("'NoneType' object is not iterable")
    out = await performance.performance_metrics(service, 12, end=END)
    assert out["lactate_threshold"] == {"available": False}
    assert out["race_predictions"]["5k"]["time"] == "21:30"


@pytest.mark.anyio
async def test_performance_metrics_auth_error_fails_whole_tool(service, fake):
    fake.fail["get_power_zones"] = AUTH_ERROR
    with pytest.raises(ToolError, match="garmin-login.sh"):
        await performance.performance_metrics(service, 12, end=END)


@pytest.mark.anyio
@pytest.mark.parametrize("weeks", [1, 53])
async def test_performance_metrics_validates_weeks(service, fake, weeks):
    with pytest.raises(ToolError, match="weeks must be between 2 and 52"):
        await performance.performance_metrics(service, weeks, end=END)
    assert fake.calls == []


@pytest.mark.anyio
async def test_personal_records_tool(service, fake):
    out = await performance.personal_records(service)
    assert out["count"] == 4 and out["records"][0]["type"] == "5k"
    fake.fail["get_personal_record"] = CONN_ERROR
    with pytest.raises(ToolError):
        await performance.personal_records(service)


@pytest.mark.anyio
async def test_gear_tool_fetches_stats_per_item(service, fake):
    fake.gear_stats_fail[fx.GEAR_UUID_BIKE] = CONN_ERROR
    out = await performance.gear(service)
    assert ("get_gear", ("1001",)) in fake.calls
    stats_calls = [a for n, a in fake.calls if n == "get_gear_stats"]
    assert sorted(stats_calls) == sorted([(fx.GEAR_UUID_SHOES,), (fx.GEAR_UUID_BIKE,), (fx.GEAR_UUID_OLD,)])
    assert out["count"] == 3
    assert [g["name"] for g in out["gear"]] == ["Pegasus 40", "Canyon Endurace", "Old trail shoes"]
    assert "500" in out["gear"][1]["stats_error"]
    assert out["gear"][0]["distance_km"] == 652.34
    assert out["gear"][2]["retired"] is True


@pytest.mark.anyio
async def test_gear_tool_sorts_by_distance(service, fake):
    out = await performance.gear(service)
    assert [g["name"] for g in out["gear"]] == ["Canyon Endurace", "Pegasus 40", "Old trail shoes"]


@pytest.mark.anyio
async def test_gear_tool_without_profile_id(service, fake):
    fake.profile_id = None
    with pytest.raises(ToolError, match="profile id"):
        await performance.gear(service)
    assert "get_gear" not in {n for n, _ in fake.calls}


@pytest.mark.anyio
async def test_gear_tool_empty(service, fake):
    fake.responses["get_gear"] = []
    assert await performance.gear(service) == {"count": 0, "gear": []}
    assert "get_gear_stats" not in {n for n, _ in fake.calls}


@pytest.mark.anyio
async def test_gear_tool_accepts_wrapped_list(service, fake):
    fake.responses["get_gear"] = {"gear": fx.GEAR}
    out = await performance.gear(service)
    assert out["count"] == 3


# --------------------------------------------------- PR 19: running tolerance

TOLERANCE_EXPECTED = {
    "count": 3,
    "weeks": [
        {"date": "2026-09-07", "weekly_running_load": 380.0, "tolerance_low": 290.0, "tolerance_high": 500.0,
         "tolerance_status": "WITHIN", "is_valid": True},
        {"date": "2026-09-14", "weekly_running_load": 560.0, "tolerance_low": 295.0, "tolerance_high": 510.0,
         "tolerance_status": "ABOVE"},
        {"date": "2026-09-21", "weekly_running_load": 455.4, "tolerance_low": 300.0, "tolerance_high": 520.0,
         "tolerance_status": "WITHIN", "range_min": 280, "range_max": 540.2},
    ],
}
UNAVAILABLE = {"available": False}


def test_shape_running_tolerance_passthrough():
    out = performance.shape_running_tolerance(fx.RUNNING_TOLERANCE)
    assert out == TOLERANCE_EXPECTED
    text = json.dumps(out)
    assert "1001" not in text and "device" not in text and "notes" not in text


def test_shape_running_tolerance_entry_shapes():
    shape = performance.shape_running_tolerance
    wrapped = {"userProfilePk": 1, "stats": [{"weekStartDate": "2026-09-28", "load": 2}]}
    assert shape(wrapped) == {"count": 1, "weeks": [{"date": "2026-09-28", "load": 2}]}
    single = shape({"calendarDate": "2026-09-28", "load": 1})
    assert single == {"count": 1, "weeks": [{"date": "2026-09-28", "load": 1}]}
    assert shape([{"date": "2026-09-28T00:00:00", "load": 1}])["weeks"] == [{"date": "2026-09-28", "load": 1}]
    assert shape([{"startDate": "2026-09-21", "load": 1}])["weeks"] == [{"date": "2026-09-21", "load": 1}]
    both = [{"calendarDate": "2026-09-28", "load": 1}, {"load": 2}]
    assert shape(both)["weeks"] == [{"load": 2}, {"date": "2026-09-28", "load": 1}]


@pytest.mark.parametrize(
    "raw",
    [None, {}, [], {"x": 1}, "text", [{"calendarDate": "2026-09-28", "toleranceStatus": "NO_DATA"}]],
)
def test_shape_running_tolerance_unavailable(raw):
    assert performance.shape_running_tolerance(raw) == UNAVAILABLE


def test_shape_running_tolerance_field_rules():
    entry = {
        "calendarDate": "2026-09-28",
        "longText": "x" * 41,
        "okText": "y" * 40,
        "aList": [1, 2],
        "nested": {"inner": {"deep": 1}, "flat": 2},
        "deviceID": 5,
        "id": 6,
        "userProfilePK": 7,
        "isValid": False,
        "loadDTO": {"value": 3.25, "deviceId": 9, "label": "ok", "items": [1]},
        "none": None,
        1: 5,
    }
    assert performance.shape_running_tolerance([entry])["weeks"] == [
        {"date": "2026-09-28", "ok_text": "y" * 40, "nested_flat": 2, "is_valid": False, "load_value": 3.2,
         "load_label": "ok"}
    ]
    wide = {"calendarDate": "2026-09-28", **{f"f{i:02d}": i for i in range(25)}}
    week = performance.shape_running_tolerance([wide])["weeks"][0]
    assert list(week) == ["date"] + [f"f{i:02d}" for i in range(20)]
    # a bool alone is not a number: the entry is dropped
    assert performance.shape_running_tolerance([{"calendarDate": "2026-09-28", "isValid": True}]) == UNAVAILABLE


def test_shape_running_tolerance_non_string_nested_key():
    entry = {"calendarDate": "2026-09-28", "rangeDTO": {1: 5, "min": 3}, "load": 2}
    assert performance.shape_running_tolerance([entry])["weeks"] == [
        {"date": "2026-09-28", "range_min": 3, "load": 2}
    ]


def test_shape_running_tolerance_far_dates():
    raw = [{"calendarDate": "9999-12-31", "load": 1}, {"calendarDate": "0001-01-01", "load": 2}]
    out = performance.shape_running_tolerance(raw)
    assert [w["date"] for w in out["weeks"]] == ["0001-01-01", "9999-12-31"]


@pytest.mark.anyio
async def test_running_tolerance_tool(service, fake):
    out = await performance.running_tolerance(service, 4, "2026-09-30")
    assert ("get_running_tolerance", ("2026-09-03", "2026-09-30", "weekly")) in fake.calls
    assert out["start"] == "2026-09-03" and out["end"] == "2026-09-30" and out["aggregation"] == "weekly"
    assert out["count"] == 3 and out["weeks"] == TOLERANCE_EXPECTED["weeks"]


@pytest.mark.anyio
async def test_running_tolerance_weeks_bounds(service, fake):
    assert (await performance.running_tolerance(service, 1, "2026-09-30"))["start"] == "2026-09-24"
    assert (await performance.running_tolerance(service, 26, "2026-09-30"))["start"] == "2026-04-02"
    fake.calls.clear()
    for weeks in (0, 27):
        with pytest.raises(ToolError, match="weeks must be between 1 and 26"):
            await performance.running_tolerance(service, weeks, "2026-09-30")
    assert fake.calls == []


@pytest.mark.anyio
async def test_running_tolerance_check_order(service, fake):
    with pytest.raises(ToolError, match="weeks must be between 1 and 26"):
        await performance.running_tolerance(service, 0, "bad")
    with pytest.raises(ToolError, match="end_date"):
        await performance.running_tolerance(service, 4, "bad")


@pytest.mark.anyio
async def test_running_tolerance_out_of_range_end(service, fake):
    with pytest.raises(ToolError, match="out of range"):
        await performance.running_tolerance(service, 2, "0001-01-10")
    assert fake.calls == []


@pytest.mark.anyio
async def test_running_tolerance_not_found_is_unavailable(service, fake):
    fake.fail["get_running_tolerance"] = GarminConnectNotFoundError("API Error 404")
    out = await performance.running_tolerance(service, 4, "2026-09-30")
    assert out == {"start": "2026-09-03", "end": "2026-09-30", "aggregation": "weekly", "available": False}


@pytest.mark.anyio
async def test_running_tolerance_library_error_fails_tool(service, fake):
    fake.fail["get_running_tolerance"] = CONN_ERROR
    with pytest.raises(ToolError, match="500"):
        await performance.running_tolerance(service, 4, "2026-09-30")


@pytest.mark.anyio
async def test_running_tolerance_auth_error_fails_tool(service, fake):
    fake.fail["get_running_tolerance"] = AUTH_ERROR
    with pytest.raises(ToolError, match="garmin-login.sh"):
        await performance.running_tolerance(service, 4, "2026-09-30")


@pytest.mark.anyio
async def test_running_tolerance_captured_empty(service, fake):
    fake.responses["get_running_tolerance"] = fx.RUNNING_TOLERANCE_EMPTY
    out = await performance.running_tolerance(service, 8, "2026-09-30")
    assert out == {"start": "2026-08-06", "end": "2026-09-30", "aggregation": "weekly", "available": False}


# ------------------------------------------------- daily race predictions (PR 14)

DAILY_START, DAILY_END = date(2026, 9, 28), date(2026, 9, 30)


def test_shape_race_prediction_days_daily_list():
    out = performance.shape_race_prediction_days(fx.RACE_PREDICTIONS_DAILY, DAILY_START, DAILY_END)
    assert out["count"] == 3 and "available" not in out
    assert [d["date"] for d in out["days"]] == ["2026-09-28", "2026-09-29", "2026-09-30"]  # sorted, 09-20 dropped
    assert out["days"][0] == {"date": "2026-09-28", "5k_seconds": 1300, "10k_seconds": 2720,
                              "half_marathon_seconds": 6010, "marathon_seconds": 12400}
    assert all(isinstance(v, int) for d in out["days"] for k, v in d.items() if k != "date")


def test_shape_race_prediction_days_entry_shapes():
    shape = performance.shape_race_prediction_days
    single = shape(fx.RACE_PREDICTIONS, DAILY_START, DAILY_END)
    assert single["count"] == 1 and single["days"][0]["date"] == "2026-09-30"
    wrapped = shape(fx.RACE_PREDICTIONS_WRAPPED, DAILY_START, DAILY_END)
    assert [d["date"] for d in wrapped["days"]] == ["2026-09-28", "2026-09-29"]
    for raw in (None, [], {}, "x", 5):
        assert shape(raw, DAILY_START, DAILY_END) == {"count": 0, "days": [], "available": False}


def test_shape_race_prediction_days_values():
    def entry(**kw):
        return {"calendarDate": "2026-09-30", **kw}

    shape = performance.shape_race_prediction_days
    odd = entry(time5K=0, time10K=-5, timeHalfMarathon=86401, timeMarathon=True)
    assert shape([odd], DAILY_START, DAILY_END)["count"] == 0
    assert shape([entry(time5K="1290", time10K=None)], DAILY_START, DAILY_END)["count"] == 0  # a string is no value
    kept = shape([entry(time5K=86400, time10K=1, timeHalfMarathon=1290.6, timeMarathon=float("nan"))],
                 DAILY_START, DAILY_END)
    assert kept["days"] == [{"date": "2026-09-30", "5k_seconds": 86400, "10k_seconds": 1, "half_marathon_seconds": 1291}]
    partial = shape([entry(time5K=1290, time10K="x", timeHalfMarathon=0)], DAILY_START, DAILY_END)
    assert partial["days"] == [{"date": "2026-09-30", "5k_seconds": 1290}]


def test_shape_race_prediction_days_dates():
    shape = performance.shape_race_prediction_days
    entries = [{"calendarDate": c, "time5K": 1290} for c in ("2026-13-01", None, "2026-9-1", 20260930)]
    entries.append({"time5K": 1290})
    assert shape(entries, DAILY_START, DAILY_END)["count"] == 0
    stamped = shape([{"calendarDate": "2026-09-30T00:00:00.0", "time5K": 1290}], DAILY_START, DAILY_END)
    assert stamped["days"][0]["date"] == "2026-09-30"
    twice = shape([{"calendarDate": "2026-09-30", "time5K": 1290, "time10K": 2700},
                   {"calendarDate": "2026-09-30", "time5K": 1280}], DAILY_START, DAILY_END)
    assert twice["days"] == [{"date": "2026-09-30", "5k_seconds": 1280}]
    edges = shape([{"calendarDate": "2026-09-27", "time5K": 1}, {"calendarDate": "2026-10-01", "time5K": 1},
                   {"calendarDate": "2026-09-28", "time5K": 1}, {"calendarDate": "2026-09-30", "time5K": 1}],
                  DAILY_START, DAILY_END)
    assert [d["date"] for d in edges["days"]] == ["2026-09-28", "2026-09-30"]


@pytest.mark.anyio
async def test_race_predictions_tool_positional_call(service, fake):
    fake.responses["get_race_predictions"] = fx.RACE_PREDICTIONS_DAILY
    out = await performance.race_predictions(service, "2026-09-03", "2026-09-30")
    assert ("get_race_predictions", ("2026-09-03", "2026-09-30", "daily")) in fake.calls
    assert out["start"] == "2026-09-03" and out["end"] == "2026-09-30" and out["count"] == 4


@pytest.mark.anyio
async def test_race_predictions_tool_defaults(service, fake):
    out = await performance.race_predictions(service, None, None)
    end = today(service.settings.timezone)
    assert out["end"] == end.isoformat() and out["start"] == (end - timedelta(days=27)).isoformat()
    assert ("get_race_predictions", (out["start"], out["end"], "daily")) in fake.calls


@pytest.mark.anyio
async def test_race_predictions_range_limits(service, fake):
    ok = await performance.race_predictions(service, "2025-10-01", "2026-10-01")  # 366 days
    assert ok["start"] == "2025-10-01"
    with pytest.raises(ToolError, match="date range too long"):
        await performance.race_predictions(service, "2025-09-30", "2026-10-01")
    with pytest.raises(ToolError, match="on or before"):
        await performance.race_predictions(service, "2026-10-02", "2026-10-01")
    calls = len(fake.calls)
    with pytest.raises(ToolError, match="out of range"):
        await performance.race_predictions(service, None, "0001-01-01")
    assert len(fake.calls) == calls
    far = await performance.race_predictions(service, "9999-12-01", "9999-12-31")
    assert ("get_race_predictions", ("9999-12-01", "9999-12-31", "daily")) in fake.calls
    assert far["end"] == "9999-12-31"


@pytest.mark.anyio
async def test_race_predictions_failures(service, fake):
    fake.fail["get_race_predictions"] = GarminConnectNotFoundError("API Error 404")
    out = await performance.race_predictions(service, "2026-09-28", "2026-09-30")
    assert out == {"start": "2026-09-28", "end": "2026-09-30", "count": 0, "days": [], "available": False}
    fake.fail["get_race_predictions"] = CONN_ERROR
    with pytest.raises(ToolError, match="Garmin Connect request failed"):
        await performance.race_predictions(service, "2026-09-28", "2026-09-30")
    fake.fail["get_race_predictions"] = AUTH_ERROR
    with pytest.raises(ToolError, match="garmin-login.sh"):
        await performance.race_predictions(service, "2026-09-28", "2026-09-30")
