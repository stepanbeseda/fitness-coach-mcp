"""Canned Open-Meteo forecast responses for the weather tests (shape verified against the live API)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx2

from conftest import TODAY

# 2026-10-07 00:00 in Prague (UTC+02:00): the day the canned payloads start.
WEATHER_T0 = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)

DEFAULT_HOUR: dict[str, Any] = {
    "temperature_2m": 12.0,
    "apparent_temperature": 10.0,
    "precipitation_probability": 10,
    "wind_speed_10m": 10.0,
}


def open_meteo_payload(
    start: date = TODAY,
    days: int = 7,
    *,
    timezone: str = "Europe/Prague",
    utc_offset_seconds: int = 7200,
    sunrise: str = "07:12",
    sunset: str = "18:31",
    hour: dict[str, Any] | None = None,
    hours: dict[str, dict[str, Any]] | None = None,
    daily: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """24 x ``days`` hourly entries from ``start`` 00:00.

    ``hour`` replaces fields of the default hour; ``hours`` overrides fields per ``"YYYY-MM-DDTHH:00"``;
    ``daily`` overrides ``{"sunrise": "HH:MM" | None, "sunset": ...}`` per ``"YYYY-MM-DD"``.
    """
    base = {**DEFAULT_HOUR, **(hour or {})}
    stamps: list[str] = []
    columns: dict[str, list[Any]] = {key: [] for key in DEFAULT_HOUR}
    for d in range(days):
        for h in range(24):
            stamp = f"{(start + timedelta(days=d)).isoformat()}T{h:02d}:00"
            stamps.append(stamp)
            values = {**base, **(hours or {}).get(stamp, {})}
            for key in columns:
                columns[key].append(values[key])
    dates = [(start + timedelta(days=d)).isoformat() for d in range(days)]
    rises, sets = [], []
    for day in dates:
        override = (daily or {}).get(day, {})
        rise = override["sunrise"] if "sunrise" in override else sunrise
        set_ = override["sunset"] if "sunset" in override else sunset
        rises.append(None if rise is None else f"{day}T{rise}")
        sets.append(None if set_ is None else f"{day}T{set_}")
    return {
        "latitude": 50.08804,  # grid-snapped by the real API; the service must never echo it
        "longitude": 14.42076,
        "generationtime_ms": 0.0821,
        "utc_offset_seconds": utc_offset_seconds,
        "timezone": timezone,
        "timezone_abbreviation": "GMT+2",
        "elevation": 191.0,
        "hourly_units": {
            "time": "iso8601",
            "temperature_2m": "°C",
            "apparent_temperature": "°C",
            "precipitation_probability": "%",
            "wind_speed_10m": "km/h",
        },
        "hourly": {"time": stamps, **columns},
        "daily_units": {"time": "iso8601", "sunrise": "iso8601", "sunset": "iso8601"},
        "daily": {"time": dates, "sunrise": rises, "sunset": sets},
    }


def handler_for(payload: Any, seen: list[httpx2.Request]) -> Callable[[httpx2.Request], httpx2.Response]:
    """A ``MockTransport`` handler that answers with ``payload`` as JSON and records every request."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, content=json.dumps(payload).encode())

    return handler
