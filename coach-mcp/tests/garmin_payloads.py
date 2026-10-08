"""Canned garmin-mcp payloads for the briefing tests (day = 2026-10-07 unless noted)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from coach_mcp.garmin import BRIEFING_TOOLS, GarminData

DAY = date(2026, 10, 7)

GREEN_SNAPSHOT: dict[str, Any] = {
    "readiness": {"score": 78},
    "hrv": {"last_night_avg_ms": 62, "baseline_balanced_ms": [52, 68]},
    "sleep": {"duration_h": 7.5, "score": 84},
    "body_battery": {"at_wake": 81},
    "resting_hr": {"today": 48, "avg_7d": 47},
    "training_status": {"acwr_ratio": 1.1, "acwr_status": "OPTIMAL"},
}

YELLOW_SNAPSHOT: dict[str, Any] = {
    "readiness": {"score": 55, "morning_score": 61},
    "hrv": {"available": False},
    "sleep": {"duration_h": 6.25, "score": 70},
    "body_battery": {"error": "x"},
    "resting_hr": {"today": 58, "avg_7d": 52},
    "training_status": {"acwr_ratio": 1.62, "acwr_status": "HIGH"},
}


def trend(days: dict[str, int | None], low: int = 52, high: int = 68) -> dict[str, Any]:
    """A get_hrv_trend payload; a ``None`` value leaves out ``last_night_avg_ms`` for that date."""
    entries = []
    for day, last in sorted(days.items()):
        entry: dict[str, Any] = {"date": day, "baseline_balanced_ms": [low, high]}
        if last is not None:
            entry["last_night_avg_ms"] = last
        entries.append(entry)
    return {"start": min(days), "end": max(days), "days": entries}


def days_range(first: date, last: date, value: int | None) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    current = first
    while current <= last:
        out[current.isoformat()] = value
        current += timedelta(days=1)
    return out


GREEN_TREND = trend(days_range(date(2026, 10, 1), date(2026, 10, 7), 60))
YELLOW_TREND = trend(days_range(date(2026, 10, 1), date(2026, 10, 6), 60))


def scheduled(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"start": "2026-10-06", "end": "2026-10-07", "count": len(entries), "scheduled": entries}


GREEN_SCHEDULED = scheduled([{"date": "2026-10-06", "name": "Tempo 8 km", "completed": True}])
YELLOW_SCHEDULED = scheduled(
    [
        {"date": "2026-10-06", "name": "Intervaly 6×800 m", "completed": False},
        {"date": "2026-10-07", "name": "Lehký běh 40 min"},
    ]
)

ALL_OK = dict.fromkeys(BRIEFING_TOOLS, "ok")


def garmin_data(**overrides: Any) -> GarminData:
    """Green data with auth "ok" and every call "ok"; keyword arguments override fields."""
    fields: dict[str, Any] = {
        "auth": "ok",
        "calls": dict(ALL_OK),
        "snapshot": GREEN_SNAPSHOT,
        "hrv_trend": GREEN_TREND,
        "scheduled": GREEN_SCHEDULED,
    }
    fields.update(overrides)
    return GarminData(**fields)


def yellow_data(**overrides: Any) -> GarminData:
    fields: dict[str, Any] = {"snapshot": YELLOW_SNAPSHOT, "hrv_trend": YELLOW_TREND, "scheduled": YELLOW_SCHEDULED}
    fields.update(overrides)
    return garmin_data(**fields)


# ---- the nightly history job (get_recovery_snapshot of any day, list_activities rows)

HISTORY_SECTIONS: tuple[str, ...] = (
    "readiness",
    "hrv",
    "sleep",
    "body_battery",
    "resting_hr",
    "stress",
    "training_status",
)


def history_snapshot(day: str, **sections: Any) -> dict[str, Any]:
    """A full ``get_recovery_snapshot`` payload; keyword arguments replace a whole section."""
    payload: dict[str, Any] = {
        "date": day,
        "readiness": {"score": 70, "morning_score": 72},
        "hrv": {"last_night_avg_ms": 55, "weekly_avg_ms": 57, "status": "BALANCED"},
        "sleep": {"duration_h": 7.4, "score": 81},
        "body_battery": {"high": 88, "low": 21},
        "resting_hr": {"today": 47},
        "stress": {"avg": 28},
        "training_status": {
            "acute": 410,
            "chronic": 380,
            "acwr_ratio": 1.08,
            "vo2max_running": 52.3,
            "vo2max_cycling": 50.1,
        },
    }
    unknown = set(sections) - set(HISTORY_SECTIONS)
    assert not unknown, unknown
    payload.update(sections)
    return payload


# The columns ``recovery_values(history_snapshot(...))`` must produce (readiness prefers morning_score).
HISTORY_ROW: dict[str, Any] = {
    "readiness": 72,
    "hrv_last_night_ms": 55,
    "hrv_weekly_avg_ms": 57,
    "hrv_status": "BALANCED",
    "sleep_duration_h": 7.4,
    "sleep_score": 81,
    "resting_hr": 47,
    "body_battery_high": 88,
    "body_battery_low": 21,
    "stress_avg": 28,
    "acute_load": 410,
    "chronic_load": 380,
    "acwr_ratio": 1.08,
    "vo2max_running": 52.3,
    "vo2max_cycling": 50.1,
}


def activity(
    day: str,
    type_: str | None,
    duration_min: Any = None,
    distance_km: Any = None,
    training_load: Any = None,
    hhmm: str = "07:00",
) -> dict[str, Any]:
    """A ``list_activities`` row (garmin-mcp drops absent values, so ``None`` leaves the key out)."""
    row: dict[str, Any] = {"id": 1, "start_local": f"{day}T{hhmm}", "type": type_}
    for key, value in (("duration_min", duration_min), ("distance_km", distance_km), ("training_load", training_load)):
        if value is not None:
            row[key] = value
    return row


def activities_payload(rows: list[dict[str, Any]], truncated: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {"start": "x", "end": "y", "count": len(rows), "activities": rows}
    if truncated:
        payload["truncated"] = True
    return payload
