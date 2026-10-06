"""Canned garmin-mcp payloads for the briefing tests (day = 2026-10-07 unless noted)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from coach_mcp.garmin import ALLOWED_TOOLS, GarminData

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

ALL_OK = dict.fromkeys(ALLOWED_TOOLS, "ok")


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
    return garmin_data(snapshot=YELLOW_SNAPSHOT, hrv_trend=YELLOW_TREND, scheduled=YELLOW_SCHEDULED, **overrides)
