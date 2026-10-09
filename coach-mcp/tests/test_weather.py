"""WeatherService against canned Open-Meteo responses (httpx2.MockTransport); no network, no wall clock."""

from __future__ import annotations

import asyncio
import copy
import logging
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from weather_payloads import WEATHER_T0, handler_for, open_meteo_payload

from coach_mcp.config import Settings
from coach_mcp.weather import (
    NO_LOCATION,
    ONE_COORDINATE,
    OUT_OF_RANGE,
    UNEXPECTED,
    WINDOW_RULE,
    WeatherService,
)

PRAGUE = (50.09, 14.42)
SYDNEY = (-33.87, 151.21)
DAY0 = "2026-10-07"
HOME = Settings(home_latitude=PRAGUE[0], home_longitude=PRAGUE[1])
EXPECTED_PARAMS = {
    "latitude": "50.09",
    "longitude": "14.42",
    "hourly": "temperature_2m,apparent_temperature,precipitation_probability,wind_speed_10m",
    "daily": "sunrise,sunset",
    "timezone": "auto",
    "forecast_days": "7",
    "temperature_unit": "celsius",
    "wind_speed_unit": "kmh",
}


class Clock:
    def __init__(self, instant: datetime = WEATHER_T0) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


def make(
    payload: Any = None,
    *,
    settings: Settings = HOME,
    clock: Clock | None = None,
    handler: Callable[[httpx2.Request], httpx2.Response] | None = None,
) -> tuple[WeatherService, list[httpx2.Request], Clock]:
    seen: list[httpx2.Request] = []
    clock = clock or Clock()
    handler = handler or handler_for(payload if payload is not None else open_meteo_payload(), seen)
    service = WeatherService(settings, now=clock.now, transport=httpx2.MockTransport(handler))
    return service, seen, clock


def good_only(hours: list[int], day: str = DAY0) -> dict[str, dict[str, Any]]:
    """Per-hour overrides for one day: every hour is rainy (precipitation 90 %) except ``hours``."""
    return {f"{day}T{h:02d}:00": {"precipitation_probability": 90} for h in range(24) if h not in hours}


def window_of(result: dict[str, Any], day: str = DAY0) -> tuple[str, str] | None:
    found = [w for w in result["best_outdoor_windows"] if w["date"] == day]
    assert len(found) <= 1
    return (found[0]["start"], found[0]["end"]) if found else None


# ------------------------------------------------------------------- T1


@pytest.mark.anyio
async def test_request_and_fields():
    payload = open_meteo_payload()
    service, seen, _clock = make(payload)
    result = await service.forecast()

    assert len(seen) == 1
    url = seen[0].url
    assert url.host == "api.open-meteo.com" and url.path == "/v1/forecast"
    assert dict(url.params) == EXPECTED_PARAMS

    assert result["location"] == {"latitude": 50.09, "longitude": 14.42, "source": "home"}
    assert result["timezone"] == "Europe/Prague"
    assert result["fetched_at"] == "2026-10-06T22:00:00+00:00"
    assert result["units"] == {"temperature": "°C", "precipitation_probability": "%", "wind_speed": "km/h"}
    assert result["window_rule"] == WINDOW_RULE
    assert len(result["days"]) == 7
    day = result["days"][0]
    assert day["date"] == DAY0 and day["sunrise"] == "07:12" and day["sunset"] == "18:31"
    hourly = day["hourly"]
    assert hourly["time"] == [f"{h:02d}:00" for h in range(24)]
    assert hourly["temperature_c"] == payload["hourly"]["temperature_2m"][:24]
    assert hourly["apparent_temperature_c"] == payload["hourly"]["apparent_temperature"][:24]
    assert hourly["precipitation_probability_pct"] == payload["hourly"]["precipitation_probability"][:24]
    assert hourly["wind_speed_kmh"] == payload["hourly"]["wind_speed_10m"][:24]
    assert day["summary"] == {
        "temperature_min_c": 12.0,
        "temperature_max_c": 12.0,
        "apparent_temperature_min_c": 10.0,
        "apparent_temperature_max_c": 10.0,
        "precipitation_probability_max_pct": 10,
        "wind_speed_max_kmh": 10.0,
    }
    assert result["best_outdoor_windows"][0] == {
        "date": DAY0,
        "start": "07:12",
        "end": "18:31",
        "minutes": 679,
        "apparent_temperature_min_c": 10.0,
        "apparent_temperature_max_c": 10.0,
        "precipitation_probability_max_pct": 10,
        "wind_speed_max_kmh": 10.0,
    }
    assert [w["date"] for w in result["best_outdoor_windows"]] == [d["date"] for d in result["days"]]
    assert "50.088" not in str(result) and "14.420" not in str(result)  # the grid-snapped echo is dropped


# ---------------------------------------------------------------- T2 - T5


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("args", "query", "location"),
    [
        ((-33.8688, 151.2093), ("-33.87", "151.21"), (-33.87, 151.21)),
        ((-0.001, 0.004), ("0.00", "0.00"), (0.0, 0.0)),
    ],
)
async def test_coordinates_are_rounded_before_the_request(args, query, location):
    service, seen, _ = make(settings=Settings())
    result = await service.forecast(*args)
    assert (seen[0].url.params["latitude"], seen[0].url.params["longitude"]) == query
    assert result["location"] == {"latitude": location[0], "longitude": location[1], "source": "arguments"}
    assert str(result["location"]["latitude"]) == str(location[0])  # 0.0, never -0.0


@pytest.mark.anyio
async def test_arguments_override_home():
    service, seen, _ = make()
    home = await service.forecast()
    assert home["location"] == {"latitude": 50.09, "longitude": 14.42, "source": "home"}
    assert (seen[0].url.params["latitude"], seen[0].url.params["longitude"]) == ("50.09", "14.42")
    away = await service.forecast(48.2082, 16.3738)
    assert away["location"] == {"latitude": 48.21, "longitude": 16.37, "source": "arguments"}
    assert (seen[1].url.params["latitude"], seen[1].url.params["longitude"]) == ("48.21", "16.37")


@pytest.mark.anyio
async def test_missing_location_is_a_clear_error():
    service, seen, _ = make(settings=Settings())
    with pytest.raises(ToolError) as exc:
        await service.forecast()
    assert str(exc.value) == NO_LOCATION == (
        "No home location configured: set COACH_HOME_LATITUDE and COACH_HOME_LONGITUDE in .env, "
        "or give latitude and longitude."
    )
    assert seen == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("args", "text"),
    [
        ((48.2, None), ONE_COORDINATE),
        ((None, 16.4), ONE_COORDINATE),
        ((math.nan, 0.0), OUT_OF_RANGE),
        ((91.0, 0.0), OUT_OF_RANGE),
        ((0.0, math.inf), OUT_OF_RANGE),
        ((0.0, -181.0), OUT_OF_RANGE),
    ],
)
async def test_one_coordinate_is_rejected(args, text):
    service, seen, _ = make()  # a home is configured: no silent fallback
    with pytest.raises(ToolError) as exc:
        await service.forecast(*args)
    assert str(exc.value) == text
    assert seen == []
    assert ONE_COORDINATE == "Give both latitude and longitude, or neither."
    assert OUT_OF_RANGE == "latitude must be between -90 and 90 and longitude between -180 and 180."


# --------------------------------------------------------------------- T6


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("apparent_temperature", 25.0, ("10:00", "14:00")),
        ("apparent_temperature", 25.1, ("12:00", "14:00")),
        ("apparent_temperature", -5.0, ("10:00", "14:00")),
        ("apparent_temperature", -5.1, ("12:00", "14:00")),
        ("precipitation_probability", 30, ("10:00", "14:00")),
        ("precipitation_probability", 31, ("12:00", "14:00")),
        ("wind_speed_10m", 25.0, ("10:00", "14:00")),
        ("wind_speed_10m", 25.1, ("12:00", "14:00")),
        ("apparent_temperature", None, ("12:00", "14:00")),
        ("precipitation_probability", None, ("12:00", "14:00")),
        ("wind_speed_10m", None, ("12:00", "14:00")),
    ],
)
async def test_window_thresholds(field, value, expected):
    hours = good_only([10, 11, 12, 13])
    hours[f"{DAY0}T11:00"] = {field: value}
    service, _, _ = make(open_meteo_payload(hours=hours))
    assert window_of(await service.forecast()) == expected


# --------------------------------------------------------------------- T7


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("daily", "expected", "minutes"),
    [
        ({}, ("07:12", "18:31"), 679),
        ({"sunrise": "07:30"}, ("07:30", "18:31"), 661),
        ({"sunrise": "07:31"}, ("08:00", "18:31"), 631),
        ({"sunset": "18:30"}, ("07:12", "18:30"), 678),
        ({"sunset": "18:29"}, ("07:12", "18:00"), 648),
    ],
)
async def test_daylight_overlap(daily, expected, minutes):
    service, _, _ = make(open_meteo_payload(daily={DAY0: daily}))
    result = await service.forecast()
    assert window_of(result) == expected
    assert result["best_outdoor_windows"][0]["minutes"] == minutes


@pytest.mark.anyio
async def test_missing_sunrise_or_sunset_gives_no_window_for_that_day():
    payload = open_meteo_payload(daily={"2026-10-08": {"sunrise": None}, "2026-10-09": {"sunset": None}})
    service, _, _ = make(payload)
    result = await service.forecast()
    days = {d["date"]: d for d in result["days"]}
    assert len(days) == 7
    assert days["2026-10-08"]["sunrise"] is None and days["2026-10-08"]["sunset"] == "18:31"
    assert days["2026-10-09"]["sunrise"] == "07:12" and days["2026-10-09"]["sunset"] is None
    assert window_of(result, "2026-10-08") is None and window_of(result, "2026-10-09") is None
    assert len(result["best_outdoor_windows"]) == 5
    assert days["2026-10-08"]["hourly"]["time"][0] == "00:00"  # the hourly values stay


# --------------------------------------------------------------------- T8


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        # (hours, precipitation) of each run; a longer run wins even with the higher precipitation
        (([8, 9], 5), ([12, 13, 14], 30), ("12:00", "15:00")),
        # same length: the drier one wins even though it is later
        (([8, 9], 20), ([12, 13], 10), ("12:00", "14:00")),
        # same length, same precipitation: the earlier one
        (([12, 13], 10), ([8, 9], 10), ("08:00", "10:00")),
    ],
)
async def test_best_window_tie_breaks(first, second, expected):
    hours = good_only(first[0] + second[0])
    for run_hours, precipitation in (first, second):
        for h in run_hours:
            hours[f"{DAY0}T{h:02d}:00"] = {"precipitation_probability": precipitation}
    service, _, _ = make(open_meteo_payload(hours=hours))
    assert window_of(await service.forecast()) == expected


# --------------------------------------------------------------------- T9


@pytest.mark.anyio
async def test_started_hours_and_past_days_are_excluded():
    clock = Clock(datetime.fromisoformat("2026-10-07T12:20:00+00:00"))  # 14:20 local
    service, _, _ = make(clock=clock)
    assert window_of(await service.forecast()) == ("15:00", "18:31")

    clock = Clock(datetime.fromisoformat("2026-10-07T12:00:00+00:00"))  # 14:00 local: the hour has just begun
    service, _, _ = make(clock=clock)
    assert window_of(await service.forecast()) == ("14:00", "18:31")


@pytest.mark.anyio
async def test_cached_answer_drops_yesterday_after_local_midnight():
    clock = Clock(datetime.fromisoformat("2026-10-07T21:50:00+00:00"))  # 23:50 local on day 0
    service, seen, _ = make(clock=clock)
    first = await service.forecast()
    assert len(first["days"]) == 7
    assert window_of(first) is None  # nothing of day 0 has not started yet
    assert len(first["best_outdoor_windows"]) == 6

    clock.instant = datetime.fromisoformat("2026-10-07T22:10:00+00:00")  # 00:10 local on day 1: cache hit
    second = await service.forecast()
    assert len(seen) == 1
    assert second["days"][0]["date"] == "2026-10-08" and len(second["days"]) == 6
    assert second["fetched_at"] == first["fetched_at"]


# -------------------------------------------------------------- T11 - T14


@pytest.mark.anyio
async def test_cache_lifetime():
    service, seen, clock = make(settings=Settings())
    first = await service.forecast(*PRAGUE)
    assert len(seen) == 1 and first["fetched_at"] == "2026-10-06T22:00:00+00:00"
    await service.forecast(*SYDNEY)  # other coordinates: own request
    assert len(seen) == 2

    clock.instant = WEATHER_T0 + timedelta(minutes=29, seconds=59)
    again = await service.forecast(*PRAGUE)
    assert len(seen) == 2 and again["fetched_at"] == first["fetched_at"] and again == {**first}

    clock.instant = WEATHER_T0 + timedelta(minutes=30)
    later = await service.forecast(*PRAGUE)
    assert len(seen) == 3 and later["fetched_at"] == "2026-10-06T22:30:00+00:00"


@pytest.mark.anyio
async def test_errors_are_not_cached():
    seen: list[httpx2.Request] = []
    good = handler_for(open_meteo_payload(), seen)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if not seen:
            seen.append(request)
            return httpx2.Response(503)
        return good(request)

    service, _, _ = make(handler=handler)
    with pytest.raises(ToolError) as exc:
        await service.forecast()
    assert str(exc.value) == "Weather service answered HTTP 503."
    assert (await service.forecast())["location"]["source"] == "home"
    assert len(seen) == 2


@pytest.mark.anyio
async def test_stale_entry_is_not_served_after_a_failure():
    seen: list[httpx2.Request] = []
    good = handler_for(open_meteo_payload(), seen)
    failing = False

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503) if failing else good(request)

    service, _, clock = make(handler=handler)
    await service.forecast()
    failing = True
    clock.instant = WEATHER_T0 + timedelta(minutes=31)
    with pytest.raises(ToolError, match="HTTP 503"):
        await service.forecast()


@pytest.mark.anyio
async def test_clock_going_back_refetches():
    service, seen, clock = make()
    await service.forecast()
    clock.instant = WEATHER_T0 - timedelta(minutes=1)
    await service.forecast()
    assert len(seen) == 2


@pytest.mark.anyio
async def test_cache_is_bounded():
    service, seen, clock = make(settings=Settings())
    coords = [(40.0 + i / 100, 10.0) for i in range(17)]
    for i, (lat, lon) in enumerate(coords):
        clock.instant = WEATHER_T0 + timedelta(seconds=i)
        await service.forecast(lat, lon)
    assert len(seen) == 17
    await service.forecast(*coords[0])  # the oldest entry was evicted
    assert len(seen) == 18
    await service.forecast(*coords[16])  # the newest is still cached
    assert len(seen) == 18
    assert len(service._cache) == 16


# -------------------------------------------------------------------- T15


def _json(mutate: Callable[[dict[str, Any]], None]) -> Callable[[], httpx2.Response]:
    def build() -> httpx2.Response:
        payload = copy.deepcopy(open_meteo_payload())
        mutate(payload)
        return httpx2.Response(200, json=payload)

    return build


def _body(content: bytes) -> Callable[[], httpx2.Response]:
    return lambda: httpx2.Response(200, content=content)


def _set(path: tuple[str, ...], value: Any) -> Callable[[], httpx2.Response]:
    def mutate(payload: dict[str, Any]) -> None:
        node: Any = payload
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return _json(mutate)


def _drop_last(*path: str) -> Callable[[], httpx2.Response]:
    def mutate(payload: dict[str, Any]) -> None:
        node: Any = payload
        for key in path:
            node = node[key]
        node.pop()

    return _json(mutate)


def _delete(key: str) -> Callable[[], httpx2.Response]:
    return _json(lambda payload: payload.pop(key))


BAD = ("Weather service returned an unexpected response.", "reason=bad_payload")
FAILURES: dict[str, tuple[Any, str, str]] = {
    "connect_error": (httpx2.ConnectError("x"), "Weather service unreachable (ConnectError).", "reason=unreachable kind=ConnectError"),
    "read_timeout": (httpx2.ReadTimeout("x"), "Weather service timed out.", "reason=timeout"),
    "os_error": (OSError("x"), "Weather service unreachable (OSError).", "reason=unreachable kind=OSError"),
    "http_503": (lambda: httpx2.Response(503), "Weather service answered HTTP 503.", "reason=http code=503"),
    "http_400": (
        lambda: httpx2.Response(400, json={"error": True, "reason": "Latitude must be in range of -90 to 90°. Given: 91.0."}),
        "Weather service answered HTTP 400.",
        "reason=http code=400",
    ),
    "html": (_body(b"<html>"), *BAD),
    "list": (_body(b"[]"), *BAD),
    "not_utf8": (_body(b"\xff\xfe{}"), *BAD),
    "no_hourly": (_delete("hourly"), *BAD),
    "hourly_short": (_drop_last("hourly", "wind_speed_10m"), *BAD),
    "hourly_time_bad": (_set(("hourly", "time", 0), "2026-13-07T00:00"), *BAD),
    "hourly_time_aware": (_set(("hourly", "time", 0), "2026-10-07T00:00+02:00"), *BAD),
    "hourly_time_int": (_set(("hourly", "time", 0), 5), *BAD),
    "offset_bool": (_set(("utc_offset_seconds",), True), *BAD),
    "offset_big": (_set(("utc_offset_seconds",), 90000), *BAD),
    "offset_float": (_set(("utc_offset_seconds",), 3600.5), *BAD),
    "timezone_int": (_set(("timezone",), 5), *BAD),
    "hourly_list": (_set(("hourly",), []), *BAD),
    "daily_str": (_set(("daily",), "x"), *BAD),
    "sunset_short": (_drop_last("daily", "sunset"), *BAD),
    "daily_time_bad": (_set(("daily", "time", 0), "2026-10-32"), *BAD),
    "daily_time_int": (_set(("daily", "time", 0), 5), *BAD),
    "sunrise_int": (_set(("daily", "sunrise", 0), 123), *BAD),
    "sunrise_no_date": (_set(("daily", "sunrise", 0), "07:12"), *BAD),
    "sunrise_aware": (_set(("daily", "sunrise", 0), "2026-10-07T07:12+02:00"), *BAD),
    "hourly_time_year_9999": (_set(("hourly", "time", 167), "9999-12-31T23:00"), *BAD),
    "daily_time_year_9999": (_set(("daily", "time", 6), "9999-12-31"), *BAD),
    "sunset_year_1": (_set(("daily", "sunset", 0), "0001-01-01T00:00"), *BAD),
    "nested": (_body(b"[" * 100000 + b"]" * 100000), *BAD),
    "too_large": (
        _body(b'{"timezone": "' + b"x" * (600 * 1024) + b'"}'),
        "Weather service returned an unexpected response.",
        "reason=too_large",
    ),
}


@pytest.mark.anyio
@pytest.mark.parametrize("name", list(FAILURES))
async def test_api_failures_are_short_tool_errors(name, caplog):
    response, text, log_part = FAILURES[name]

    def handler(_request: httpx2.Request) -> httpx2.Response:
        if isinstance(response, Exception):
            raise response
        return response()

    caplog.set_level(logging.DEBUG, logger="coach_mcp.weather")
    service, _, _ = make(handler=handler)
    with pytest.raises(ToolError) as exc:
        await service.forecast()
    assert str(exc.value) == text
    assert "50.09" not in str(exc.value) and "14.42" not in str(exc.value)
    assert f"weather status=error {log_part}" in caplog.text
    assert "50.09" not in caplog.text and "14.42" not in caplog.text and "50.088" not in caplog.text
    assert service._cache == {}  # nothing stored
    assert UNEXPECTED == "Weather service returned an unexpected response."


@pytest.mark.anyio
async def test_a_slow_trickle_hits_the_overall_deadline(monkeypatch, caplog):
    monkeypatch.setattr("coach_mcp.weather.TOTAL_TIMEOUT_SECONDS", 0.05)

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)  # a server that never finishes; each read stays under the per-read timeout
        return httpx2.Response(200)

    caplog.set_level(logging.DEBUG, logger="coach_mcp.weather")
    service, _, _ = make(handler=handler)
    with pytest.raises(ToolError) as exc:
        await service.forecast()
    assert str(exc.value) == "Weather service timed out."
    assert "weather status=error reason=timeout" in caplog.text
    assert service._cache == {}


@pytest.mark.anyio
async def test_success_is_logged_without_coordinates(caplog):
    caplog.set_level(logging.DEBUG, logger="coach_mcp.weather")
    service, _, _ = make()
    await service.forecast()
    await service.forecast()
    assert "weather status=fetched ms=" in caplog.text and "weather status=cached" in caplog.text
    assert "50.09" not in caplog.text and "14.42" not in caplog.text


# --------------------------------------------------------------- T16, T17


@pytest.mark.anyio
async def test_non_numeric_values_become_null():
    hours = good_only([10, 11, 12, 13, 14])
    hours[f"{DAY0}T11:00"] = {"apparent_temperature": "n/a"}
    hours[f"{DAY0}T12:00"] = {"wind_speed_10m": True}
    hours[f"{DAY0}T13:00"] = {"temperature_2m": math.nan}  # json.dumps writes NaN, which json.loads accepts
    service, _, _ = make(open_meteo_payload(hours=hours))
    result = await service.forecast()
    hourly = result["days"][0]["hourly"]
    assert hourly["apparent_temperature_c"][11] is None and hourly["apparent_temperature_c"][10] == 10.0
    assert hourly["wind_speed_kmh"][12] is None and hourly["wind_speed_kmh"][13] == 10.0
    assert hourly["temperature_c"][13] is None and hourly["temperature_c"][14] == 12.0
    # Hours 11 and 12 are not eligible: the window splits into 10:00 and 13:00-15:00; the longer one wins.
    assert window_of(result) == ("13:00", "15:00")


@pytest.mark.anyio
async def test_summary_ignores_nulls():
    payload = open_meteo_payload(
        hour={"temperature_2m": None},
        hours={"2026-10-08T05:00": {"temperature_2m": 8.0}, "2026-10-08T15:00": {"temperature_2m": 14.5}},
    )
    service, _, _ = make(payload)
    result = await service.forecast()
    day0, day1, day2 = result["days"][:3]
    assert day1["summary"]["temperature_min_c"] == 8.0 and day1["summary"]["temperature_max_c"] == 14.5
    assert day2["summary"]["temperature_min_c"] is None and day2["summary"]["temperature_max_c"] is None
    assert day0["summary"]["temperature_min_c"] is None
    assert day2["summary"]["apparent_temperature_min_c"] == 10.0  # other fields are unaffected
