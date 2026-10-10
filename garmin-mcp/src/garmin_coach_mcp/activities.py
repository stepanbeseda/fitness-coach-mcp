"""Activity list / detail shaping and tool logic."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from garminconnect.exceptions import GarminConnectNotFoundError
from mcp.server.mcpserver.exceptions import ToolError

from .formatting import (
    as_dict,
    as_list,
    clean,
    clock,
    km,
    local_str,
    minutes,
    num,
    pace_per_km,
    resolve_range,
    speed_kmh,
)
from .garmin_client import GarminService, is_section_error, section_error
from .health import UNAVAILABLE, _section

ActivityType = Literal[
    "running", "cycling", "swimming", "walking", "hiking", "fitness_equipment", "multi_sport", "other"
]

MAX_LAPS = 60
MAX_SETS = 60
STRENGTH_MARKER = "strength"


def _uses_pace(type_key: str) -> bool:
    return any(part in type_key for part in ("run", "walk", "hik"))


def _uses_speed(type_key: str) -> bool:
    return any(part in type_key for part in ("cycl", "bik", "ride"))


def _speed_fields(type_key: str, avg_speed: Any, max_speed: Any = None) -> dict[str, Any]:
    if _uses_pace(type_key):
        return {"avg_pace_min_per_km": pace_per_km(avg_speed)}
    if _uses_speed(type_key):
        return {"avg_speed_kmh": speed_kmh(avg_speed), "max_speed_kmh": speed_kmh(max_speed)}
    return {}


def shape_activity_row(a: dict[str, Any]) -> dict[str, Any]:
    type_key = str(as_dict(a.get("activityType")).get("typeKey") or "")
    distance = num(a.get("distance"), 1)
    row: dict[str, Any] = {
        "id": a.get("activityId"),
        "start_local": local_str(a.get("startTimeLocal")),
        "type": type_key or None,
        "name": a.get("activityName"),
        "duration_min": minutes(a.get("duration")),
        "distance_km": km(distance) if distance else None,
        "elevation_gain_m": num(a.get("elevationGain"), 0),
        "avg_hr": num(a.get("averageHR"), 0),
        "max_hr": num(a.get("maxHR"), 0),
        **(_speed_fields(type_key, a.get("averageSpeed")) if distance else {}),
        "avg_power_w": num(a.get("avgPower"), 0),
        "norm_power_w": num(a.get("normPower"), 0),
        "avg_cadence_spm": num(a.get("averageRunningCadenceInStepsPerMinute"), 0),
        "aerobic_te": num(a.get("aerobicTrainingEffect"), 1),
        "anaerobic_te": num(a.get("anaerobicTrainingEffect"), 1),
        "te_label": a.get("trainingEffectLabel"),
        "training_load": num(a.get("activityTrainingLoad"), 0),
        "calories": num(a.get("calories"), 0),
        "sets": a.get("totalSets"),
        "reps": a.get("totalReps"),
    }
    row.pop("max_speed_kmh", None)
    return clean(row)


def shape_activity_detail(activity: Any) -> dict[str, Any]:
    act = as_dict(activity)
    s = as_dict(act.get("summaryDTO"))
    type_key = str(as_dict(act.get("activityTypeDTO")).get("typeKey") or as_dict(act.get("activityType")).get("typeKey") or "")
    description = act.get("description")
    distance = num(s.get("distance"), 1)
    return clean(
        {
            "id": act.get("activityId"),
            "name": act.get("activityName"),
            "type": type_key or None,
            "event_type": as_dict(act.get("eventTypeDTO")).get("typeKey"),
            "description": description[:500] if isinstance(description, str) else None,
            "start_local": local_str(s.get("startTimeLocal")),
            "duration": clock(s.get("duration")),
            "moving_time": clock(s.get("movingDuration")),
            "distance_km": km(distance) if distance else None,
            "elevation_gain_m": num(s.get("elevationGain"), 0),
            "elevation_loss_m": num(s.get("elevationLoss"), 0),
            "avg_hr": num(s.get("averageHR"), 0),
            "max_hr": num(s.get("maxHR"), 0),
            **(_speed_fields(type_key, s.get("averageSpeed"), s.get("maxSpeed")) if distance else {}),
            "avg_power_w": num(s.get("averagePower"), 0),
            "max_power_w": num(s.get("maxPower"), 0),
            "norm_power_w": num(s.get("normalizedPower"), 0),
            "avg_cadence": num(s.get("averageRunCadence") or s.get("averageBikeCadence"), 0),
            "aerobic_te": num(s.get("trainingEffect"), 1),
            "anaerobic_te": num(s.get("anaerobicTrainingEffect"), 1),
            "te_label": s.get("trainingEffectLabel"),
            "training_load": num(s.get("activityTrainingLoad"), 0),
            "calories": num(s.get("calories"), 0),
            "active_sets": num(s.get("activeSets"), 0),
            "total_reps": num(s.get("totalExerciseReps"), 0),
        }
    )


def shape_laps(splits: Any, type_key: str) -> dict[str, Any]:
    laps = [lap for lap in as_list(as_dict(splits).get("lapDTOs")) if isinstance(lap, dict)]
    out = []
    for lap in laps[:MAX_LAPS]:
        distance = num(lap.get("distance"), 1)
        out.append(
            clean(
                {
                    "lap": lap.get("lapIndex"),
                    "distance_km": km(distance) if distance else None,
                    "duration": clock(lap.get("duration")),
                    "avg_hr": num(lap.get("averageHR"), 0),
                    "max_hr": num(lap.get("maxHR"), 0),
                    **(_speed_fields(type_key, lap.get("averageSpeed")) if distance else {}),
                    "avg_power_w": num(lap.get("averagePower"), 0),
                    "avg_cadence": num(lap.get("averageRunCadence") or lap.get("averageBikeCadence"), 0),
                    "elevation_gain_m": num(lap.get("elevationGain"), 0),
                    "intensity": lap.get("intensityType"),
                    "workout_step": lap.get("wktStepIndex"),
                }
            )
        )
    result: dict[str, Any] = {"laps": out}
    if len(laps) > MAX_LAPS:
        result["laps_truncated"] = len(laps)
    return result


def shape_hr_zones(raw: Any) -> list[dict[str, Any]]:
    zones = [z for z in as_list(raw) if isinstance(z, dict)]
    total = sum(float(z.get("secsInZone") or 0) for z in zones) or 0.0
    return [
        clean(
            {
                "zone": z.get("zoneNumber"),
                "from_bpm": z.get("zoneLowBoundary"),
                "minutes": minutes(z.get("secsInZone")),
                "percent": num(float(z.get("secsInZone") or 0) / total * 100, 0) if total else None,
            }
        )
        for z in sorted(zones, key=lambda z: z.get("zoneNumber") or 0)
    ]


def _exercise_of(items: Any) -> tuple[str | None, str | None]:
    """(exercise, category) of the most probable candidate; category only when the name was used."""
    candidates = [e for e in as_list(items) if isinstance(e, dict)]
    if not candidates:
        return None, None
    best = max(candidates, key=lambda e: num(e.get("probability"), 3) or 0)
    name, category = best.get("name"), best.get("category")
    name = name.lower() if isinstance(name, str) and name else None
    category = category.lower() if isinstance(category, str) and category else None
    return (name, category) if name else (category, None)


def _grams_to_kg(grams: Any) -> float | None:
    """Garmin's set weight is assumed to be grams (unverified, PR 19 D6)."""
    g = num(grams, 3)
    if g is None or g <= 0:
        return None
    return num(g / 1000, 2)


def shape_exercise_sets(raw: Any) -> dict[str, Any]:
    sets = [s for s in as_list(as_dict(raw).get("exerciseSets")) if isinstance(s, dict)]
    rows: list[dict[str, Any]] = []
    for s in sets:
        if str(s.get("setType") or "").upper() == "REST":
            continue
        exercise, category = _exercise_of(s.get("exercises"))
        reps = num(s.get("repetitionCount"), 0)
        weight = _grams_to_kg(s.get("weight"))
        if exercise is None and reps is None and weight is None:
            continue
        rows.append(
            clean(
                {
                    "set": len(rows) + 1,
                    "exercise": exercise,
                    "category": category,
                    "reps": reps,
                    "weight_kg": weight,
                    "duration_s": num(s.get("duration"), 0),
                }
            )
        )
    if not rows:
        return dict(UNAVAILABLE)
    summary: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = summary.setdefault(
            row.get("exercise") or "unknown",
            {"exercise": row.get("exercise") or "unknown", "sets": 0, "reps": None, "max_weight_kg": None},
        )
        entry["sets"] += 1
        if "reps" in row:
            entry["reps"] = (entry["reps"] or 0) + row["reps"]
        if "weight_kg" in row:
            entry["max_weight_kg"] = max(entry["max_weight_kg"] or 0, row["weight_kg"])
    out: dict[str, Any] = {
        "count": len(rows),
        "exercises": [clean(e) for e in summary.values()],
        "sets": rows[:MAX_SETS],
    }
    if len(rows) > MAX_SETS:
        out["sets_truncated"] = len(rows)
    return out


def _f_to_c(value: Any) -> float | None:
    """Garmin's weather temperatures are assumed to be degrees Fahrenheit (unverified, PR 19 D6)."""
    f = num(value, 3)
    if f is None:
        return None
    c = num((f - 32) * 5 / 9, 1)
    return c if c is not None and -60 <= c <= 60 else None


def _mph_to_kmh(value: Any) -> int | float | None:
    """Garmin's wind speeds are assumed to be miles per hour (unverified, PR 19 D6)."""
    v = num(value, 3)
    if v is None or v < 0:
        return None
    return num(v * 1.609344, 0)


def shape_activity_weather(raw: Any) -> dict[str, Any]:
    """Selected weather values only; location, station and issue time are never output."""
    w = as_dict(raw)
    humidity = num(w.get("relativeHumidity"), 0)
    compass = w.get("windDirectionCompassPoint")
    desc = as_dict(w.get("weatherTypeDTO")).get("desc")
    out = clean(
        {
            "temp_c": _f_to_c(w.get("temp")),
            "feels_like_c": _f_to_c(w.get("apparentTemp")),
            "dew_point_c": _f_to_c(w.get("dewPoint")),
            "humidity_pct": humidity if humidity is not None and 0 <= humidity <= 100 else None,
            "wind_kmh": _mph_to_kmh(w.get("windSpeed")),
            "wind_gust_kmh": _mph_to_kmh(w.get("windGust")),
            "wind_from": compass.upper() if isinstance(compass, str) and 0 < len(compass) <= 3 else None,
            "conditions": desc if isinstance(desc, str) and 0 < len(desc) <= 60 else None,
        }
    )
    if not any(k in out for k in ("temp_c", "humidity_pct", "wind_kmh", "conditions")):
        return dict(UNAVAILABLE)
    return out


def none_if_missing(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Garmin answers 404 for data that does not exist (no weather, no sets, no tolerance): treat it as empty."""

    def call(client: Any) -> Any:
        try:
            return fn(client)
        except GarminConnectNotFoundError:
            return None

    return call


async def list_activities(
    service: GarminService,
    days: int,
    start_date: str | None,
    end_date: str | None,
    activity_type: str | None,
    limit: int,
) -> dict[str, Any]:
    if not 1 <= limit <= 100:
        raise ToolError("limit must be between 1 and 100")
    if not 1 <= days <= 366:
        raise ToolError("days must be between 1 and 366")
    start, end = resolve_range(start_date, end_date, service.settings.timezone, default_days=days, max_days=366)
    raw = await service.call(lambda c: c.get_activities_by_date(start.isoformat(), end.isoformat(), activity_type))
    rows = [shape_activity_row(a) for a in as_list(raw) if isinstance(a, dict)]
    rows.sort(key=lambda r: r.get("start_local") or "", reverse=True)
    out: dict[str, Any] = {"start": start.isoformat(), "end": end.isoformat(), "count": len(rows), "activities": rows[:limit]}
    if len(rows) > limit:
        out["truncated"] = True
    return out


async def get_activity(service: GarminService, activity_id: int) -> dict[str, Any]:
    if activity_id <= 0:
        raise ToolError("activity_id must be a positive integer")
    aid = str(activity_id)
    results = await service.call_many(
        {
            "activity": lambda c: c.get_activity(aid),
            "splits": lambda c: c.get_activity_splits(aid),
            "zones": lambda c: c.get_activity_hr_in_timezones(aid),
            "weather": none_if_missing(lambda c: c.get_activity_weather(aid)),
            "exercise_sets": none_if_missing(lambda c: c.get_activity_exercise_sets(aid)),
        }
    )
    if is_section_error(results["activity"]):
        raise ToolError(section_error(results["activity"])["error"])
    detail = shape_activity_detail(results["activity"])
    type_key = str(detail.get("type") or "")
    if is_section_error(results["splits"]):
        detail["laps_error"] = section_error(results["splits"])["error"]
    else:
        detail.update(shape_laps(results["splits"], type_key))
    if not is_section_error(results["zones"]):
        zones = shape_hr_zones(results["zones"])
        if zones:
            detail["hr_zones"] = zones
    detail["weather"] = _section(results, "weather", shape_activity_weather)
    sets = _section(results, "exercise_sets", shape_exercise_sets)
    if STRENGTH_MARKER in type_key or "sets" in sets:
        detail["exercise_sets"] = sets
    return detail
