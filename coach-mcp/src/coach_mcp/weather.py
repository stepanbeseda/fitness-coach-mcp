"""Weather forecast from Open-Meteo (the ``get_weather_forecast`` tool).

The request URL carries the (rounded) coordinates, so log lines carry outcome words, status codes,
durations and exception class names only, never the URL, the coordinates or the response text.
``__main__.configure_logging`` keeps the ``httpx2`` logger, which logs every request URL at INFO,
at WARNING.

Successful answers are cached in memory for 30 minutes per rounded coordinate pair; the shaping
(days still to come, outdoor windows that have not started) runs on every call with the current time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx2
from mcp.server.mcpserver.exceptions import ToolError

from .config import Settings, round_coordinate

log = logging.getLogger("coach_mcp.weather")

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_TTL = timedelta(minutes=30)
CACHE_MAX_ENTRIES = 16
MAX_RESPONSE_BYTES = 512 * 1024
TOTAL_TIMEOUT_SECONDS = 20.0  # the whole request, on top of the per-read httpx2 timeouts
FEELS_LIKE_MIN_C = -5.0
FEELS_LIKE_MAX_C = 25.0
PRECIPITATION_PROBABILITY_MAX_PCT = 30
WIND_SPEED_MAX_KMH = 25.0
MIN_DAYLIGHT_MINUTES = 30
WINDOW_RULE = (
    "per day the longest run of hours in daylight (at least 30 min of the hour between sunrise and sunset), "
    "not yet started, feels-like -5 to 25 °C, precipitation probability at most 30 %, wind at most 25 km/h; "
    "ties: lower precipitation probability, then earlier"
)

NO_LOCATION = (
    "No home location configured: set COACH_HOME_LATITUDE and COACH_HOME_LONGITUDE in .env, "
    "or give latitude and longitude."
)
ONE_COORDINATE = "Give both latitude and longitude, or neither."
OUT_OF_RANGE = "latitude must be between -90 and 90 and longitude between -180 and 180."
TIMED_OUT = "Weather service timed out."
UNEXPECTED = "Weather service returned an unexpected response."

# Dates near date.min / date.max would overflow the hour arithmetic of the shaping; no forecast looks like that.
_SANE_YEARS = range(1900, 9000)
_HOUR = timedelta(hours=1)
_MIN_DAYLIGHT = timedelta(minutes=MIN_DAYLIGHT_MINUTES)
_HOURLY_FIELDS = ("temperature_2m", "apparent_temperature", "precipitation_probability", "wind_speed_10m")


class _BadPayload(Exception):
    """The response is not the Open-Meteo forecast we expect (never carries response text)."""


class _TooLarge(Exception):
    pass


@dataclass(frozen=True)
class Hour:
    start: datetime  # naive, local to the forecast location
    temperature: float | None
    apparent: float | None
    precipitation_probability: float | None
    wind: float | None


@dataclass(frozen=True)
class Day:
    date: date
    sunrise: datetime | None
    sunset: datetime | None


@dataclass(frozen=True)
class Forecast:
    timezone: str
    utc_offset_seconds: int
    hours: list[Hour]
    days: list[Day]


# ------------------------------------------------------------------ validation


def _num(value: Any) -> float | None:
    """A finite number kept as is; null, strings, booleans and non-finite values become None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value if math.isfinite(value) else None


def _list(container: dict[str, Any], key: str, length: int | None = None) -> list[Any]:
    value = container.get(key)
    if not isinstance(value, list) or (length is not None and len(value) != length):
        raise _BadPayload
    return value


def _local_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise _BadPayload
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _BadPayload from None
    if parsed.tzinfo is not None or parsed.year not in _SANE_YEARS:
        raise _BadPayload
    return parsed


def _parse(body: bytes) -> Forecast:
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):  # ValueError includes UnicodeDecodeError
        raise _BadPayload from None
    if not isinstance(data, dict):
        raise _BadPayload
    timezone = data.get("timezone")
    offset = data.get("utc_offset_seconds")
    if not isinstance(timezone, str) or isinstance(offset, bool) or not isinstance(offset, int) or abs(offset) > 86400:
        raise _BadPayload
    hourly, daily = data.get("hourly"), data.get("daily")
    if not isinstance(hourly, dict) or not isinstance(daily, dict):
        raise _BadPayload

    times = _list(hourly, "time")
    columns = [_list(hourly, key, len(times)) for key in _HOURLY_FIELDS]
    hours = [
        Hour(_local_datetime(t), _num(a), _num(b), _num(c), _num(d))
        for t, a, b, c, d in zip(times, *columns, strict=True)
    ]

    dates = _list(daily, "time")
    sunrises = _list(daily, "sunrise", len(dates))
    sunsets = _list(daily, "sunset", len(dates))
    days = []
    for raw_date, raw_rise, raw_set in zip(dates, sunrises, sunsets, strict=True):
        if not isinstance(raw_date, str):
            raise _BadPayload
        try:
            day = date.fromisoformat(raw_date)
        except ValueError:
            raise _BadPayload from None
        if day.year not in _SANE_YEARS:
            raise _BadPayload
        days.append(
            Day(
                day,
                None if raw_rise is None else _local_datetime(raw_rise),
                None if raw_set is None else _local_datetime(raw_set),
            )
        )
    return Forecast(timezone, offset, hours, days)


# --------------------------------------------------------------------- shaping


def _clock(value: datetime | None) -> str | None:
    return None if value is None else value.strftime("%H:%M")


def _span(values: list[Any], pick: Callable[[list[Any]], Any]) -> Any:
    present = [v for v in values if v is not None]
    return pick(present) if present else None


def _summary(hours: list[Hour]) -> dict[str, Any]:
    temp = [h.temperature for h in hours]
    feels = [h.apparent for h in hours]
    return {
        "temperature_min_c": _span(temp, min),
        "temperature_max_c": _span(temp, max),
        "apparent_temperature_min_c": _span(feels, min),
        "apparent_temperature_max_c": _span(feels, max),
        "precipitation_probability_max_pct": _span([h.precipitation_probability for h in hours], max),
        "wind_speed_max_kmh": _span([h.wind for h in hours], max),
    }


def _eligible(hour: Hour, day: Day, local_now: datetime) -> bool:
    if day.sunrise is None or day.sunset is None:
        return False
    if hour.start < local_now:  # past and already started hours never count
        return False
    if min(hour.start + _HOUR, day.sunset) - max(hour.start, day.sunrise) < _MIN_DAYLIGHT:
        return False
    if hour.apparent is None or hour.precipitation_probability is None or hour.wind is None:
        return False
    return (
        FEELS_LIKE_MIN_C <= hour.apparent <= FEELS_LIKE_MAX_C
        and hour.precipitation_probability <= PRECIPITATION_PROBABILITY_MAX_PCT
        and hour.wind <= WIND_SPEED_MAX_KMH
    )


def _best_window(day: Day, hours: list[Hour], local_now: datetime) -> dict[str, Any] | None:
    sunrise, sunset = day.sunrise, day.sunset
    if sunrise is None or sunset is None:
        return None
    runs: list[list[Hour]] = []
    current: list[Hour] = []
    for hour in hours:
        if _eligible(hour, day, local_now):
            current.append(hour)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    windows = []
    for run in runs:
        start = max(run[0].start, sunrise)
        end = min(run[-1].start + _HOUR, sunset)
        feels = [h.apparent for h in run]
        windows.append(
            {
                "date": day.date.isoformat(),
                "start": _clock(start),
                "end": _clock(end),
                "minutes": int((end - start).total_seconds() // 60),
                "apparent_temperature_min_c": min(feels),
                "apparent_temperature_max_c": max(feels),
                "precipitation_probability_max_pct": max(h.precipitation_probability for h in run),
                "wind_speed_max_kmh": max(h.wind for h in run),
                "_start": start,
            }
        )
    if not windows:
        return None
    best = min(windows, key=lambda w: (-w["minutes"], w["precipitation_probability_max_pct"], w["_start"]))
    return {k: v for k, v in best.items() if k != "_start"}


def _shape(forecast: Forecast, now_utc: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    local_now = (now_utc + timedelta(seconds=forecast.utc_offset_seconds)).replace(tzinfo=None)
    days_out: list[dict[str, Any]] = []
    windows: list[dict[str, Any]] = []
    for day in forecast.days:
        if day.date < local_now.date():
            continue
        hours = [h for h in forecast.hours if h.start.date() == day.date]
        days_out.append(
            {
                "date": day.date.isoformat(),
                "sunrise": _clock(day.sunrise),
                "sunset": _clock(day.sunset),
                "summary": _summary(hours),
                "hourly": {
                    "time": [_clock(h.start) for h in hours],
                    "temperature_c": [h.temperature for h in hours],
                    "apparent_temperature_c": [h.apparent for h in hours],
                    "precipitation_probability_pct": [h.precipitation_probability for h in hours],
                    "wind_speed_kmh": [h.wind for h in hours],
                },
            }
        )
        window = _best_window(day, hours, local_now)
        if window is not None:
            windows.append(window)
    return days_out, windows


# --------------------------------------------------------------------- service


class WeatherService:
    """Open-Meteo forecast for the athlete's home or given coordinates, cached in memory."""

    def __init__(
        self,
        settings: Settings,
        *,
        now: Callable[[], datetime],
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._now = now
        self._transport = transport  # for tests only
        self._cache: dict[tuple[float, float], tuple[datetime, Forecast]] = {}

    def _location(self, latitude: float | None, longitude: float | None) -> tuple[float, float, str]:
        if latitude is None and longitude is None:
            home_lat, home_lon = self._settings.home_latitude, self._settings.home_longitude
            if home_lat is None or home_lon is None:
                raise ToolError(NO_LOCATION)
            return round_coordinate(home_lat), round_coordinate(home_lon), "home"
        if latitude is None or longitude is None:
            raise ToolError(ONE_COORDINATE)
        if not (math.isfinite(latitude) and math.isfinite(longitude)) or abs(latitude) > 90 or abs(longitude) > 180:
            raise ToolError(OUT_OF_RANGE)
        return round_coordinate(latitude), round_coordinate(longitude), "arguments"

    async def forecast(self, latitude: float | None = None, longitude: float | None = None) -> dict[str, Any]:
        lat, lon, source = self._location(latitude, longitude)
        now_utc = self._now().astimezone(UTC)
        key = (lat, lon)
        entry = self._cache.get(key)
        if entry is not None and timedelta(0) <= now_utc - entry[0] < CACHE_TTL:
            fetched_at, forecast = entry
            log.debug("weather status=cached")
        else:
            forecast = await self._fetch(lat, lon)
            fetched_at = now_utc
            self._cache[key] = (fetched_at, forecast)
            while len(self._cache) > CACHE_MAX_ENTRIES:
                del self._cache[min(self._cache, key=lambda k: self._cache[k][0])]
        days, windows = _shape(forecast, now_utc)
        return {
            "location": {"latitude": lat, "longitude": lon, "source": source},
            "timezone": forecast.timezone,
            "fetched_at": fetched_at.isoformat(timespec="seconds"),
            "units": {"temperature": "°C", "precipitation_probability": "%", "wind_speed": "km/h"},
            "window_rule": WINDOW_RULE,
            "best_outdoor_windows": windows,
            "days": days,
        }

    async def _fetch(self, lat: float, lon: float) -> Forecast:
        params = {
            "latitude": f"{lat:.2f}",
            "longitude": f"{lon:.2f}",
            "hourly": ",".join(_HOURLY_FIELDS),
            "daily": "sunrise,sunset",
            "timezone": "auto",
            "forecast_days": "7",
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
        }
        started = time.monotonic()
        code = 0
        body = b""
        try:
            async with (
                asyncio.timeout(TOTAL_TIMEOUT_SECONDS),
                httpx2.AsyncClient(timeout=httpx2.Timeout(10.0, connect=5.0), transport=self._transport) as http,
            ):
                # Never raise_for_status() or log/str() an exception or exc.request: they carry the URL
                # with the coordinates. Only the class name is used.
                async with http.stream("GET", OPEN_METEO_URL, params=params) as response:
                    code = response.status_code
                    if 200 <= code < 300:
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_RESPONSE_BYTES:
                                raise _TooLarge
                            chunks.append(chunk)
                        body = b"".join(chunks)
        except _TooLarge:
            log.warning("weather status=error reason=too_large")
            raise ToolError(UNEXPECTED) from None
        except (httpx2.TimeoutException, TimeoutError):  # TimeoutError is an OSError: keep it before that handler
            log.warning("weather status=error reason=timeout ms=%.0f", _ms(started))
            raise ToolError(TIMED_OUT) from None
        except (httpx2.RequestError, OSError) as exc:
            kind = type(exc).__name__
            log.warning("weather status=error reason=unreachable kind=%s ms=%.0f", kind, _ms(started))
            raise ToolError(f"Weather service unreachable ({kind}).") from None
        if not 200 <= code < 300:
            log.warning("weather status=error reason=http code=%d ms=%.0f", code, _ms(started))
            raise ToolError(f"Weather service answered HTTP {code}.")
        try:
            forecast = _parse(body)
        except _BadPayload:
            log.warning("weather status=error reason=bad_payload")
            raise ToolError(UNEXPECTED) from None
        log.info("weather status=fetched ms=%.0f", _ms(started))
        return forecast


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000
