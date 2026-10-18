import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import fixtures as fx
from conftest import AUTH_ERROR, CONN_ERROR
from garmin_coach_mcp import body
from garmin_coach_mcp.formatting import today

TZ = "Europe/Prague"


# ----------------------------------------------------------- body composition


def test_shape_weigh_in_converts_units_and_drops_missing():
    assert body.shape_weigh_in(fx.BODY_COMPOSITION["dateWeightList"][1]) == {
        "date": "2026-09-02",
        "weight_kg": 79.1,
        "bmi": 24.2,
        "body_fat_pct": 16.2,
        "muscle_mass_kg": 37.5,
        "body_water_pct": 58.1,
        "bone_mass_kg": 3.3,
        "source": "index_scale",
    }
    manual = body.shape_weigh_in({"calendarDate": "2026-09-10", "weight": 78600.0, "sourceType": "MANUAL"})
    assert manual == {"date": "2026-09-10", "weight_kg": 78.6, "source": "manual"}
    assert body.shape_weigh_in({"calendarDate": "2026-09-10", "weight": 0, "muscleMass": -1}) == {"date": "2026-09-10"}


def test_shape_body_composition_one_entry_per_day_oldest_first():
    out = body.shape_body_composition(fx.BODY_COMPOSITION)
    assert out["count"] == 5
    assert [e["date"] for e in out["entries"]] == ["2026-09-02", "2026-09-10", "2026-09-24", "2026-09-28", "2026-09-30"]
    assert [e["weight_kg"] for e in out["entries"]] == [79.1, 78.6, 78.9, 78.0, 77.8]
    # 2026-09-24 has two weigh-ins; the later one (by "date" ms, listed first) wins
    assert out["entries"][2] == {"date": "2026-09-24", "weight_kg": 78.9, "bmi": 24.1, "source": "manual"}
    assert "bmi" not in out["entries"][1]
    assert "available" not in out


def test_shape_body_composition_summary():
    out = body.shape_body_composition(fx.BODY_COMPOSITION)
    assert out["summary"] == {
        "first": {"date": "2026-09-02", "weight_kg": 79.1},
        "last": {"date": "2026-09-30", "weight_kg": 77.8},
        "change_kg": -1.3,
        "avg_7d_kg": 78.23,  # 09-24, 09-28, 09-30 (window of 7 days ending at the last weigh-in)
        "body_fat_pct": {"first": 16.2, "last": 15.1, "change": -1.1},
    }


def test_shape_body_composition_summary_without_fat_and_with_one_entry():
    single = {"dateWeightList": [{"calendarDate": "2026-09-30", "date": 1, "weight": 80000.0}]}
    out = body.shape_body_composition(single)
    assert out["count"] == 1
    assert out["summary"] == {
        "first": {"date": "2026-09-30", "weight_kg": 80.0},
        "last": {"date": "2026-09-30", "weight_kg": 80.0},
        "change_kg": 0,
        "avg_7d_kg": 80.0,
    }
    fat_once = {"dateWeightList": [
        {"calendarDate": "2026-09-01", "date": 1, "weight": 80000.0, "bodyFat": 16.0},
        {"calendarDate": "2026-09-30", "date": 2, "weight": 79000.0},
    ]}
    assert body.shape_body_composition(fat_once)["summary"]["body_fat_pct"] == {"first": 16.0}


def test_shape_body_composition_skips_entries_without_weight_or_date():
    raw = {"dateWeightList": [
        {"calendarDate": "2026-09-01", "date": 1, "weight": None, "bodyFat": 16.0},
        {"calendarDate": None, "date": 2, "weight": 79000.0},
        {"calendarDate": "2026-09-03", "date": 3, "weight": 0},
        {"calendarDate": "2026-09-04", "date": 4, "weight": 78000.0},
    ]}
    out = body.shape_body_composition(raw)
    assert out["count"] == 1 and out["entries"][0]["date"] == "2026-09-04"


@pytest.mark.parametrize("raw", [None, {}, {"dateWeightList": []}, {"dateWeightList": None}, [], "nope"])
def test_shape_body_composition_empty_range(raw):
    assert body.shape_body_composition(raw) == {"available": False, "count": 0, "entries": []}


def test_shape_body_composition_accepts_bare_list():
    out = body.shape_body_composition(fx.BODY_COMPOSITION["dateWeightList"])
    assert out["count"] == 5 and out["summary"]["change_kg"] == -1.3


# ------------------------------------------------------------------ hydration


def test_shape_hydration():
    assert body.shape_hydration(fx.HYDRATION) == {
        "intake_ml": 1750,
        "goal_ml": 2800,
        "percent_of_goal": 62,
        "sweat_loss_ml": 420,
        "activity_intake_ml": 500,
        "daily_average_ml": 2100,
    }


def test_shape_hydration_partial_and_missing():
    assert body.shape_hydration({"calendarDate": fx.DAY, "valueInML": 0.0, "goalInML": 2500.0}) == {
        "intake_ml": 0, "goal_ml": 2500, "percent_of_goal": 0,
    }
    assert body.shape_hydration({"valueInML": 300.0, "goalInML": 0}) == {"intake_ml": 300, "goal_ml": 0}
    assert body.shape_hydration({"valueInML": None, "goalInML": 2500.0}) == {"goal_ml": 2500}
    assert body.shape_hydration({"calendarDate": fx.DAY, "valueInML": None, "goalInML": None}) == {"available": False}
    assert body.shape_hydration(None) == {"available": False}
    assert body.shape_hydration([]) == {"available": False}


# ------------------------------------------------------------------ nutrition


def test_find_searches_nested_keys_case_insensitively():
    assert body._find({"a": {"b": {"TotalCalories": 5}}}, body.CALORIE_KEYS) == 5
    assert body._find({"a": {"b": {"c": {"d": {"calories": 5}}}}}, body.CALORIE_KEYS) is None  # deeper than SEARCH_DEPTH
    assert body._find({"calories": None, "energy": 7}, body.CALORIE_KEYS) == 7
    assert body._find([{"calories": 5}], body.CALORIE_KEYS) is None
    assert body._find_num({"protein": "142.5"}, body.PROTEIN_KEYS, 1) == 142.5
    assert body._find_num({"protein": "lots"}, body.PROTEIN_KEYS, 1) is None


def test_shape_calories():
    assert body.shape_calories(fx.NUTRITION_FOOD_LOG, fx.NUTRITION_SETTINGS) == {
        "consumed": 2150, "goal": 2400, "remaining": 250,
    }
    assert body.shape_calories({"calories": 1800}, None) == {"consumed": 1800}
    assert body.shape_calories({"calendarDate": fx.DAY}, fx.NUTRITION_SETTINGS) == {"available": False, "goal": 2400}
    assert body.shape_calories(None, None) == {"available": False}
    assert body.shape_calories({"weird": 1}, {"weird": 2}) == {"available": False}


def test_shape_macros():
    assert body.shape_macros(fx.NUTRITION_FOOD_LOG, fx.NUTRITION_SETTINGS) == {
        "protein_g": 142.5, "carbs_g": 231.0, "fat_g": 71.2,
        "protein_goal_g": 150, "carbs_goal_g": 270, "fat_goal_g": 80,
    }
    flat = {"protein": 100, "carbohydrates": 200}
    assert body.shape_macros(flat, None) == {"protein_g": 100, "carbs_g": 200}
    assert body.shape_macros({}, fx.NUTRITION_SETTINGS) == {
        "available": False, "protein_goal_g": 150, "carbs_goal_g": 270, "fat_goal_g": 80,
    }
    assert body.shape_macros(None, None) == {"available": False}


def test_shape_meals():
    out = body.shape_meals(fx.NUTRITION_MEALS)
    assert out["count"] == 4
    assert out["meals"][0] == {"meal": "breakfast", "calories": 520, "protein_g": 32.0, "carbs_g": 60.0, "fat_g": 15.0,
                               "items": 3}
    assert out["meals"][2] == {"meal": "dinner", "calories": 800, "protein_g": 55.5, "carbs_g": 81.0, "fat_g": 28.2,
                               "items": 0}
    assert out["meals"][3] == {"meal": "snack"}  # a named meal without calories is kept
    assert all(m.get("meal") != "no name, no calories" for m in out["meals"])


def test_shape_meals_variants_and_missing():
    bare_list = [{"name": "Lunch", "totalCalories": 700}]
    assert body.shape_meals(bare_list) == {"count": 1, "meals": [{"meal": "lunch", "calories": 700}]}
    wrapped = {"data": {"mealList": [{"calories": 300}]}}
    assert body.shape_meals(wrapped) == {"count": 1, "meals": [{"calories": 300}]}
    assert body.shape_meals({"meals": []}) == {"available": False}
    assert body.shape_meals({"meals": [{"notes": "x"}]}) == {"available": False}
    assert body.shape_meals(None) == {"available": False}
    assert body.shape_meals({"calendarDate": fx.DAY}) == {"available": False}


# ------------------------------------------------------------------- tools


@pytest.mark.anyio
async def test_body_composition_tool_passes_range(service, fake):
    out = await body.body_composition(service, "2026-09-01", "2026-09-30")
    assert fake.calls[-1] == ("get_body_composition", ("2026-09-01", "2026-09-30"))
    assert out["start"] == "2026-09-01" and out["end"] == "2026-09-30"
    assert out["count"] == 5 and out["summary"]["avg_7d_kg"] == 78.23


@pytest.mark.anyio
async def test_body_composition_tool_default_is_last_30_days(service, fake):
    end = today(TZ)
    await body.body_composition(service, None, None)
    assert fake.calls[-1] == ("get_body_composition", ((end - timedelta(days=29)).isoformat(), end.isoformat()))


@pytest.mark.anyio
async def test_body_composition_tool_empty_range(service, fake):
    fake.responses["get_body_composition"] = {"startDate": "2026-09-01", "endDate": "2026-09-30", "dateWeightList": []}
    out = await body.body_composition(service, "2026-09-01", "2026-09-30")
    assert out == {"start": "2026-09-01", "end": "2026-09-30", "available": False, "count": 0, "entries": []}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("2025-09-29", "2026-09-30", "max 366 days"),
        ("2026-09-30", "2026-09-01", "on or before"),
        ("2026/09/01", "2026-09-30", "YYYY-MM-DD"),
    ],
)
async def test_body_composition_tool_validates_range_before_calling(service, fake, start, end, message):
    with pytest.raises(ToolError, match=message):
        await body.body_composition(service, start, end)
    assert fake.calls == []


@pytest.mark.anyio
async def test_body_composition_tool_accepts_366_days(service, fake):
    await body.body_composition(service, "2025-09-30", "2026-09-30")
    assert fake.calls[-1] == ("get_body_composition", ("2025-09-30", "2026-09-30"))


@pytest.mark.anyio
async def test_body_composition_tool_propagates_errors(service, fake):
    fake.fail["get_body_composition"] = CONN_ERROR
    with pytest.raises(ToolError, match="500"):
        await body.body_composition(service, "2026-09-01", "2026-09-30")


@pytest.mark.anyio
async def test_nutrition_tool_all_sections(service, fake):
    out = await body.nutrition(service, "2026-09-30")
    assert out == {
        "date": "2026-09-30",
        "calories": {"consumed": 2150, "goal": 2400, "remaining": 250},
        "macros": {"protein_g": 142.5, "carbs_g": 231.0, "fat_g": 71.2,
                   "protein_goal_g": 150, "carbs_goal_g": 270, "fat_goal_g": 80},
        "meals": body.shape_meals(fx.NUTRITION_MEALS),
        "hydration": body.shape_hydration(fx.HYDRATION),
    }
    calls = ("get_nutrition_daily_food_log", "get_nutrition_daily_meals", "get_nutrition_daily_settings", "get_hydration_data")
    for name in calls:
        assert (name, ("2026-09-30",)) in fake.calls
    assert len(json.dumps(out)) < 1500


@pytest.mark.anyio
async def test_nutrition_tool_each_missing_part_is_unavailable(service, fake):
    fake.responses["get_nutrition_daily_food_log"] = None
    fake.responses["get_nutrition_daily_meals"] = {}
    fake.responses["get_hydration_data"] = {"calendarDate": "2026-09-30", "valueInML": None, "goalInML": None}
    out = await body.nutrition(service, "2026-09-30")
    assert out["calories"] == {"available": False, "goal": 2400}
    assert out["macros"] == {"available": False, "protein_goal_g": 150, "carbs_goal_g": 270, "fat_goal_g": 80}
    assert out["meals"] == {"available": False}
    assert out["hydration"] == {"available": False}
    assert "goals_error" not in out


@pytest.mark.anyio
async def test_nutrition_tool_degrades_per_section(service, fake):
    fake.fail["get_nutrition_daily_food_log"] = CONN_ERROR
    fake.fail["get_nutrition_daily_settings"] = CONN_ERROR
    out = await body.nutrition(service, "2026-09-30")
    assert "500" in out["calories"]["error"] and "500" in out["macros"]["error"]
    assert "500" in out["goals_error"]
    assert out["meals"]["count"] == 4
    assert out["hydration"]["intake_ml"] == 1750


@pytest.mark.anyio
async def test_nutrition_tool_goals_error_only_drops_goals(service, fake):
    fake.fail["get_nutrition_daily_settings"] = CONN_ERROR
    out = await body.nutrition(service, "2026-09-30")
    assert out["calories"] == {"consumed": 2150}
    assert out["macros"] == {"protein_g": 142.5, "carbs_g": 231.0, "fat_g": 71.2}
    assert "500" in out["goals_error"]


@pytest.mark.anyio
async def test_nutrition_tool_tolerates_non_numeric_values(service, fake):
    fake.responses["get_hydration_data"] = {"valueInML": "n/a", "goalInML": {"ml": 2500}}
    fake.responses["get_nutrition_daily_food_log"] = {"calories": [2000], "protein": "142.5"}
    out = await body.nutrition(service, "2026-09-30")
    assert out["hydration"] == {"available": False}
    assert out["calories"] == {"available": False, "goal": 2400}
    assert out["macros"]["protein_g"] == 142.5 and "carbs_g" not in out["macros"]


@pytest.mark.anyio
async def test_nutrition_tool_auth_error_fails_whole_tool(service, fake):
    fake.fail["get_hydration_data"] = AUTH_ERROR
    with pytest.raises(ToolError, match="garmin-login.sh"):
        await body.nutrition(service, "2026-09-30")


@pytest.mark.anyio
async def test_nutrition_tool_validates_date(service, fake):
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        await body.nutrition(service, "yesterday-ish")
    assert fake.calls == []


# --------------------------------------------------------------- log_weight


@pytest.mark.anyio
@pytest.mark.parametrize("weight", [19.99, 300.01, 0, -70, 1000])
async def test_log_weight_rejects_implausible_weight_before_calling(service, fake, weight):
    with pytest.raises(ToolError, match="between 20 and 300 kg"):
        await body.log_weight(service, weight, "2026-09-30")
    assert fake.calls == [] and fake.weigh_ins == []


@pytest.mark.anyio
async def test_log_weight_rejects_future_date_before_calling(service, fake):
    tomorrow = (today(TZ) + timedelta(days=1)).isoformat()
    with pytest.raises(ToolError, match="future"):
        await body.log_weight(service, 77.8, tomorrow)
    assert fake.calls == [] and fake.weigh_ins == []
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        await body.log_weight(service, 77.8, "30.9.2026")
    assert fake.calls == []


@pytest.mark.anyio
async def test_log_weight_past_date_uses_noon_local_time(service, fake):
    out = await body.log_weight(service, 77.8, "2026-09-30")
    assert fake.weigh_ins == [(77.8, "kg", "2026-09-30T12:00:00+02:00")]
    assert fake.calls[-1] == ("get_daily_weigh_ins", ("2026-09-30",))
    assert out == {
        "stored": True,
        "date": "2026-09-30",
        "weight_kg": 77.8,
        "source": "manual",
        "entry": {"date": "2026-09-30", "weight_kg": 77.8, "bmi": 23.8, "source": "manual"},
    }


@pytest.mark.anyio
async def test_log_weight_today_uses_current_local_time(service, fake):
    now = datetime.now(ZoneInfo(TZ))
    out = await body.log_weight(service, 77.8, None)
    weight, unit, ts = fake.weigh_ins[0]
    assert (weight, unit) == (77.8, "kg")
    stamped = datetime.fromisoformat(ts)
    assert stamped.utcoffset() is not None and stamped.date() == now.date()
    assert abs((stamped - now).total_seconds()) < 60
    assert fake.calls[-1] == ("get_daily_weigh_ins", (now.date().isoformat(),))
    assert out["stored"] is True and out["date"] == now.date().isoformat()


@pytest.mark.anyio
async def test_log_weight_rounds_and_boundaries(service, fake):
    await body.log_weight(service, 20, "2026-09-30")
    await body.log_weight(service, 300.004, "2026-09-30")
    assert [w for w, _, _ in fake.weigh_ins] == [20, 300.0]


@pytest.mark.anyio
async def test_log_weight_garmin_error_is_reported_and_nothing_stored(service, fake):
    fake.fail["add_weigh_in"] = CONN_ERROR
    with pytest.raises(ToolError, match="500"):
        await body.log_weight(service, 77.8, "2026-09-30")
    assert fake.weigh_ins == []
    assert "get_daily_weigh_ins" not in {n for n, _ in fake.calls}


@pytest.mark.anyio
async def test_log_weight_read_back_failure_still_reports_stored(service, fake):
    fake.fail["get_daily_weigh_ins"] = CONN_ERROR
    out = await body.log_weight(service, 77.8, "2026-09-30")
    assert fake.weigh_ins[0][0] == 77.8
    assert out["stored"] is True
    assert "500" in out["entry"]["error"] and out["entry"]["error"].startswith("stored, but")
    assert out["entry"]["error_kind"] == "garmin_unavailable"


@pytest.mark.anyio
async def test_log_weight_entry_not_yet_visible(service, fake):
    out = await body.log_weight(service, 76.5, "2026-09-30")
    assert out["stored"] is True
    assert out["entry"] == {
        "error": "stored, but the new weigh-in is not in Garmin's day view yet",
        "error_kind": "not_found",
    }
