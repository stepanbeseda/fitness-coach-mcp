"""Activity list / detail shaping and tool logic."""

from __future__ import annotations

from typing import Any, Literal

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

ActivityType = Literal[
    "running", "cycling", "swimming", "walking", "hiking", "fitness_equipment", "multi_sport", "other"
]

MAX_LAPS = 60


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
    return detail
