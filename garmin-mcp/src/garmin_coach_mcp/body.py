"""Body composition, hydration and nutrition: shaping of raw Garmin payloads + the tool logic."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.mcpserver.exceptions import ToolError

from .formatting import as_dict, as_list, clean, date_prefix, num, resolve_date, resolve_range, today
from .garmin_client import GarminService
from .sections import UNAVAILABLE, Call, Section, fetch_sections

log = logging.getLogger("garmin_coach_mcp.body")

DEFAULT_RANGE_DAYS, MAX_RANGE_DAYS = 30, 366
MIN_WEIGHT_KG, MAX_WEIGHT_KG = 20.0, 300.0
PAST_WEIGH_IN_TIME = time(12, 0)  # wall-clock time used for a weigh-in logged for an earlier day
AVERAGE_DAYS = 7

# Key names under which Garmin's nutrition service may report a value. The nutrition
# endpoints are unverified on a real account (plan section 5), so the shaper searches a
# few spellings instead of assuming one and answers "available": false when none match.
CALORIE_KEYS = ("calories", "totalcalories", "caloriesconsumed", "consumedcalories", "energy", "kcal")
PROTEIN_KEYS = ("protein", "totalprotein", "proteingrams", "proteining")
CARB_KEYS = ("carbs", "carbohydrates", "carbohydrate", "totalcarbs", "totalcarbohydrates", "carbgrams")
FAT_KEYS = ("fat", "totalfat", "fats", "fatgrams")
CALORIE_GOAL_KEYS = ("caloriegoal", "caloriesgoal", "dailycaloriegoal", "calorietarget", "goalcalories")
PROTEIN_GOAL_KEYS = ("proteingoal", "proteintarget", "dailyproteingoal")
CARB_GOAL_KEYS = ("carbgoal", "carbsgoal", "carbohydrategoal", "carbohydratesgoal", "carbtarget")
FAT_GOAL_KEYS = ("fatgoal", "fattarget", "dailyfatgoal")
MEAL_LIST_KEYS = ("meals", "meallist", "dailymeals", "mealsummaries")
MEAL_NAME_KEYS = ("mealtype", "mealname", "name", "type", "title")
MEAL_ITEM_KEYS = ("foods", "fooditems", "items", "entries", "foodlogs")
SEARCH_DEPTH = 3


# ------------------------------------------------------------------ helpers


def _grams_to_kg(value: Any) -> float | None:
    g = num(value, 3)
    if g is None or g <= 0:
        return None
    return num(g / 1000, 2)


def _find(payload: Any, names: Iterable[str], depth: int = SEARCH_DEPTH) -> Any:
    """First value under a key matching one of ``names`` (case-insensitive), searching nested dicts."""
    if not isinstance(payload, dict) or depth < 0:
        return None
    wanted = tuple(names)
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for name in wanted:
        if name in lowered and lowered[name] is not None:
            return lowered[name]
    for value in payload.values():
        if isinstance(value, dict):
            found = _find(value, wanted, depth - 1)
            if found is not None:
                return found
    return None


def _find_num(payload: Any, names: Iterable[str], digits: int) -> float | int | None:
    return num(_find(payload, names), digits)


def _find_list(payload: Any, names: Iterable[str]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    found = _find(payload, names)
    return [m for m in found if isinstance(m, dict)] if isinstance(found, list) else []


# ----------------------------------------------------------- body composition


def shape_weigh_in(entry: dict[str, Any]) -> dict[str, Any]:
    source = entry.get("sourceType")
    return clean(
        {
            "date": date_prefix(entry.get("calendarDate")),
            "weight_kg": _grams_to_kg(entry.get("weight")),
            "bmi": num(entry.get("bmi"), 1),
            "body_fat_pct": num(entry.get("bodyFat"), 1),
            "muscle_mass_kg": _grams_to_kg(entry.get("muscleMass")),
            "body_water_pct": num(entry.get("bodyWater"), 1),
            "bone_mass_kg": _grams_to_kg(entry.get("boneMass")),
            "source": str(source).lower() if source else None,
        }
    )


def _weigh_ins(raw: Any) -> list[dict[str, Any]]:
    items = as_dict(raw).get("dateWeightList") if isinstance(raw, dict) else raw
    return [e for e in as_list(items) if isinstance(e, dict)]


def shape_body_composition(raw: Any) -> dict[str, Any]:
    """One entry per day (the latest weigh-in of that day) in kg / %, oldest first, plus a summary."""
    per_day: dict[str, dict[str, Any]] = {}
    for entry in sorted(_weigh_ins(raw), key=lambda e: num(e.get("date"), 0) or 0):
        shaped = shape_weigh_in(entry)
        if shaped.get("date") and shaped.get("weight_kg") is not None:
            per_day[shaped["date"]] = shaped  # sorted by time: a later weigh-in replaces an earlier one
    entries = [per_day[d] for d in sorted(per_day)]
    if not entries:
        return {**UNAVAILABLE, "count": 0, "entries": []}

    first, last = entries[0], entries[-1]
    last_day = date.fromisoformat(last["date"])
    window_start = (last_day - timedelta(days=AVERAGE_DAYS - 1)).isoformat()
    recent = [e["weight_kg"] for e in entries if e["date"] >= window_start]
    fat_first, fat_last = first.get("body_fat_pct"), last.get("body_fat_pct")
    summary = clean(
        {
            "first": {"date": first["date"], "weight_kg": first["weight_kg"]},
            "last": {"date": last["date"], "weight_kg": last["weight_kg"]},
            "change_kg": num(last["weight_kg"] - first["weight_kg"], 2),
            "avg_7d_kg": num(mean(recent), 2),
            "body_fat_pct": clean(
                {
                    "first": fat_first,
                    "last": fat_last,
                    "change": num(fat_last - fat_first, 1) if fat_first is not None and fat_last is not None else None,
                }
            ),
        }
    )
    return {"count": len(entries), "entries": entries, "summary": summary}


# ------------------------------------------------------------------ hydration


def shape_hydration(raw: Any) -> dict[str, Any]:
    d = as_dict(raw)
    intake = num(d.get("valueInML"), 0)
    goal = num(d.get("goalInML"), 0)
    if intake is None and goal is None:
        return dict(UNAVAILABLE)
    return clean(
        {
            "intake_ml": intake,
            "goal_ml": goal,
            "percent_of_goal": num(intake / goal * 100, 0) if intake is not None and goal else None,
            "sweat_loss_ml": num(d.get("sweatLossInML"), 0),
            "activity_intake_ml": num(d.get("activityIntakeInML"), 0),
            "daily_average_ml": num(d.get("dailyAverageinML"), 0),
        }
    )


# ------------------------------------------------------------------ nutrition


def shape_calories(food_log: Any, settings: Any) -> dict[str, Any]:
    consumed = _find_num(food_log, CALORIE_KEYS, 0)
    goal = _find_num(settings, CALORIE_GOAL_KEYS, 0)
    if consumed is None:  # nothing logged (or an unknown payload shape): the goal alone is still useful
        return clean({**UNAVAILABLE, "goal": goal})
    return clean({"consumed": consumed, "goal": goal, "remaining": goal - consumed if goal is not None else None})


def shape_macros(food_log: Any, settings: Any) -> dict[str, Any]:
    consumed = clean(
        {
            "protein_g": _find_num(food_log, PROTEIN_KEYS, 1),
            "carbs_g": _find_num(food_log, CARB_KEYS, 1),
            "fat_g": _find_num(food_log, FAT_KEYS, 1),
        }
    )
    goals = clean(
        {
            "protein_goal_g": _find_num(settings, PROTEIN_GOAL_KEYS, 1),
            "carbs_goal_g": _find_num(settings, CARB_GOAL_KEYS, 1),
            "fat_goal_g": _find_num(settings, FAT_GOAL_KEYS, 1),
        }
    )
    if not consumed:
        return {**UNAVAILABLE, **goals}
    return {**consumed, **goals}


def shape_meals(raw: Any) -> dict[str, Any]:
    meals: list[dict[str, Any]] = []
    for meal in _find_list(raw, MEAL_LIST_KEYS):
        name = _find(meal, MEAL_NAME_KEYS, depth=0)
        items = _find(meal, MEAL_ITEM_KEYS, depth=0)
        shaped = clean(
            {
                "meal": str(name).lower() if isinstance(name, str) and name else None,
                "calories": _find_num(meal, CALORIE_KEYS, 0),
                "protein_g": _find_num(meal, PROTEIN_KEYS, 1),
                "carbs_g": _find_num(meal, CARB_KEYS, 1),
                "fat_g": _find_num(meal, FAT_KEYS, 1),
                "items": len(items) if isinstance(items, list) else None,
            }
        )
        if "meal" in shaped or "calories" in shaped:
            meals.append(shaped)
    if not meals:
        return dict(UNAVAILABLE)
    return {"count": len(meals), "meals": meals}


# --------------------------------------------------------------- tool logic


async def body_composition(service: GarminService, start_date: str | None, end_date: str | None) -> dict[str, Any]:
    start, end = resolve_range(
        start_date, end_date, service.settings.timezone, default_days=DEFAULT_RANGE_DAYS, max_days=MAX_RANGE_DAYS
    )
    s, e = start.isoformat(), end.isoformat()
    raw = await service.call(lambda c: c.get_body_composition(s, e))
    return {"start": s, "end": e, **shape_body_composition(raw)}


def _no_goals_error(settings: Any) -> None:
    return None


async def nutrition(service: GarminService, date_arg: str | None) -> dict[str, Any]:
    d = resolve_date(date_arg, service.settings.timezone).isoformat()
    # calories and macros are built from two calls: an error in the food log is the section's error; the settings
    # (goals) are passed through as is, and a failed settings call only drops the goal values ("goals_error")
    sections = await fetch_sections(
        service,
        {
            "food_log": Call(lambda c: c.get_nutrition_daily_food_log(d)),
            "meals": Call(lambda c: c.get_nutrition_daily_meals(d)),
            "settings": Call(lambda c: c.get_nutrition_daily_settings(d)),
            "hydration": Call(lambda c: c.get_hydration_data(d)),
        },
        {
            "calories": Section("food_log", shape_calories, uses=("settings",)),
            "macros": Section("food_log", shape_macros, uses=("settings",)),
            "meals": Section("meals", shape_meals),
            "hydration": Section("hydration", shape_hydration),
            "goals_error": Section("settings", _no_goals_error, if_error=lambda message: message),
        },
    )
    goals_error = sections.pop("goals_error")
    out: dict[str, Any] = {"date": d, **sections}
    if goals_error is not None:
        out["goals_error"] = goals_error
    return out


def _weigh_in_timestamp(day: date, tz: str) -> str:
    """Timezone-aware ISO timestamp for the weigh-in: now for today, noon local time for an earlier day."""
    zone = ZoneInfo(tz)
    now = datetime.now(zone)
    if day == now.date():
        return now.replace(microsecond=0).isoformat()
    return datetime.combine(day, PAST_WEIGH_IN_TIME, tzinfo=zone).isoformat()


async def log_weight(service: GarminService, weight_kg: float, date_arg: str | None) -> dict[str, Any]:
    w = num(weight_kg, 2)
    if w is None or not MIN_WEIGHT_KG <= w <= MAX_WEIGHT_KG:
        raise ToolError(f"weight_kg must be between {MIN_WEIGHT_KG:.0f} and {MAX_WEIGHT_KG:.0f} kg")
    tz = service.settings.timezone
    day = resolve_date(date_arg, tz)
    if day > today(tz):
        raise ToolError("date cannot be in the future")
    d, ts = day.isoformat(), _weigh_in_timestamp(day, tz)

    await service.call(lambda c: c.add_weigh_in(w, "kg", ts))

    out: dict[str, Any] = {"stored": True, "date": d, "weight_kg": w, "source": "manual"}
    try:
        raw = await service.call(lambda c: c.get_daily_weigh_ins(d))
    except ToolError as exc:
        out["entry"] = {"error": f"stored, but reading the day back failed: {exc}"}
        return out
    entries = [shape_weigh_in(e) for e in _weigh_ins(raw)]
    match = next((e for e in reversed(entries) if e.get("weight_kg") == w), None)
    out["entry"] = match or {"error": "stored, but the new weigh-in is not in Garmin's day view yet"}
    return out
