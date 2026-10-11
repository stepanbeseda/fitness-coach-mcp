"""Performance metrics, running tolerance, personal records and gear.

Shaping of raw Garmin payloads + the tool logic."""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .activities import none_if_missing
from .formatting import as_dict, as_list, clean, clock, km, num, pace_per_km, resolve_date, resolve_range, today
from .garmin_client import GarminService, is_section_error, section_error
from .health import UNAVAILABLE, _section

log = logging.getLogger("garmin_coach_mcp.performance")

RACE_DISTANCES = (
    ("5k", "time5K", 5000.0),
    ("10k", "time10K", 10000.0),
    ("half_marathon", "timeHalfMarathon", 21097.5),
    ("marathon", "timeMarathon", 42195.0),
)
RUNNING_RECORDS = {1: ("1k", 1000.0), 2: ("1_mile", 1609.344), 3: ("5k", 5000.0),
                   4: ("10k", 10000.0), 5: ("half_marathon", 21097.5), 6: ("marathon", 42195.0)}
LONGEST_RUN = 7
MIN_WEEKS, MAX_WEEKS = 2, 52
MIN_TOLERANCE_WEEKS, MAX_TOLERANCE_WEEKS = 1, 26
# Garmin does not document the running tolerance payload: weeks pass its own fields through (PR 19 D2).
TOLERANCE_DATE_KEYS = ("calendarDate", "weekStartDate", "startDate", "date")
MAX_TOLERANCE_FIELDS = 20
MAX_TOLERANCE_TEXT = 40
_ID_KEY = re.compile(r"(?:^id$|Id$|ID$|Pk$|PK$|Uuid$|UUID$)")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


# ------------------------------------------------------------------ helpers


def _day(value: Any) -> str | None:
    return value[:10] if isinstance(value, str) and len(value) >= 10 else None


def _entries(raw: Any, marker: str) -> list[dict[str, Any]]:
    """Entries of a Garmin payload that may be a list, a dict wrapping a list or a single entry."""
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        if marker in raw:
            return [raw]
        for value in raw.values():
            if isinstance(value, list):
                found = [v for v in value if isinstance(v, dict)]
                if found:
                    return found
    return []


def _time_and_pace(seconds: Any, meters: float) -> dict[str, Any]:
    s = num(seconds, 3)
    if s is None or s <= 0:
        return {}
    return {"time": clock(s), "pace": pace_per_km(meters / s)}


# ---------------------------------------------------------- race predictions


def shape_race_predictions(raw: Any) -> dict[str, Any]:
    items = [d for d in as_list(raw) if isinstance(d, dict)]
    if not items:
        return dict(UNAVAILABLE)
    latest = max(items, key=lambda d: str(d.get("calendarDate") or ""))
    out: dict[str, Any] = {"date": _day(latest.get("calendarDate"))}
    found = False
    for name, key, meters in RACE_DISTANCES:
        value = num(latest.get(key), 3)
        if value is None:
            continue
        part = _time_and_pace(value, meters)
        if part:
            out[name] = part
            found = True
    return clean(out) if found else dict(UNAVAILABLE)


MAX_PREDICTION_SECONDS = 86400


def _prediction_seconds(raw: Any) -> int | None:
    """One predicted time in whole seconds, or None. A string is no value (`num` would convert it)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    v = num(raw, 0)
    return v if v is not None and 0 < v <= MAX_PREDICTION_SECONDS else None


def shape_race_prediction_days(raw: Any, start: date, end: date) -> dict[str, Any]:
    """Daily race predictions as {"count", "days": [{"date", "5k_seconds", ...}]}.

    The shape of the daily answer (a list of entries keyed `calendarDate`, times as integer seconds) was confirmed
    by the owner's capture on 2026-10-11; every other shape still degrades to "available": false.
    """
    by_day: dict[str, dict[str, Any]] = {}
    for entry in _entries(raw, "calendarDate"):
        text = _day(entry.get("calendarDate"))
        if text is None:
            continue
        try:
            day = date.fromisoformat(text)
        except ValueError:
            continue
        if not start <= day <= end:
            continue
        values = {}
        for name, key, _meters in RACE_DISTANCES:
            seconds = _prediction_seconds(entry.get(key))
            if seconds is not None:
                values[f"{name}_seconds"] = seconds
        if values:
            by_day[day.isoformat()] = values  # a repeated date: the later entry wins
    days = [{"date": d, **by_day[d]} for d in sorted(by_day)]
    out: dict[str, Any] = {"count": len(days), "days": days}
    if not days:
        out["available"] = False
    return out


# ---------------------------------------------------------- lactate threshold


def lt_speed_mps(value: Any) -> float | None:
    """Garmin reports lactate-threshold speed in tenths of m/s; a real value below 1 m/s does not exist."""
    v = num(value, 6)
    if v is None or v <= 0:
        return None
    return v * 10 if v < 1.0 else v


def shape_lactate_threshold(raw: Any) -> dict[str, Any]:
    root = as_dict(raw)
    shr = as_dict(root.get("speed_and_heart_rate"))
    pw = as_dict(root.get("power"))
    out = clean(
        {
            "date": _day(shr.get("calendarDate")) or _day(pw.get("calendarDate")),
            "heart_rate": num(shr.get("heartRate"), 0),
            "heart_rate_cycling": num(shr.get("heartRateCycling"), 0),
            "pace": pace_per_km(lt_speed_mps(shr.get("speed"))),
            "power_watts": num(pw.get("functionalThresholdPower"), 0),
            "power_to_weight": num(pw.get("powerToWeight"), 2),
        }
    )
    if not any(k in out for k in ("heart_rate", "pace", "power_watts")):
        return dict(UNAVAILABLE)
    return out


# ------------------------------------------------------------------- zones


def shape_heart_rate_zones(raw: Any) -> dict[str, Any]:
    profiles: list[dict[str, Any]] = []
    for item in as_list(raw):
        if not isinstance(item, dict):
            continue
        floors = [num(item.get(f"zone{n}Floor"), 0) for n in range(1, 6)]
        if all(f is None for f in floors):
            continue
        max_hr = num(item.get("maxHeartRateUsed"), 0)
        zones: list[dict[str, Any]] = []
        for n in range(1, 6):
            floor = floors[n - 1]
            if floor is None:
                continue
            if n < 5:
                nxt = floors[n]
                upper = nxt - 1 if nxt is not None else None
            else:
                upper = max_hr
            zones.append({"zone": n, "min_bpm": floor, "max_bpm": upper})
        profiles.append(
            {
                "sport": str(item.get("sport") or "").lower() or "default",
                "method": item.get("trainingMethod"),
                "max_hr": max_hr,
                "resting_hr": num(item.get("restingHeartRateUsed"), 0),
                "threshold_hr": num(item.get("lactateThresholdHeartRateUsed"), 0),
                "zones": zones,
            }
        )
    if not profiles:
        return dict(UNAVAILABLE)
    return {"profiles": clean(profiles)}


def shape_power_zones(raw: Any) -> dict[str, Any]:
    item = next(
        (d for d in as_list(raw) if isinstance(d, dict) and str(d.get("sport") or "").upper() == "CYCLING"),
        None,
    )
    if item is None:
        return dict(UNAVAILABLE)
    floors: list[float | int] = []
    for n in range(1, 11):
        floor = num(item.get(f"zone{n}Floor"), 0)
        if floor is None:
            break
        floors.append(floor)
    zones = [
        {"zone": i + 1, "min_watts": floor, "max_watts": floors[i + 1] - 1 if i + 1 < len(floors) else None}
        for i, floor in enumerate(floors)
    ]
    ftp = num(item.get("functionalThresholdPower"), 0)
    if not zones and ftp is None:
        return dict(UNAVAILABLE)
    return clean({"sport": "cycling", "ftp_watts": ftp, "zones": zones})


# ------------------------------------------------- endurance / hill / VO2max


def shape_endurance_score(raw: Any) -> dict[str, Any]:
    d = as_dict(raw)
    dto = as_dict(d.get("enduranceScoreDTO"))
    groups = sorted(as_dict(d.get("groupMap")).items())
    last_avg = num(as_dict(groups[-1][1]).get("groupAverage"), 0) if groups else None
    latest = num(dto.get("overallScore"), 0)
    if latest is None:
        latest = last_avg
    if latest is None:
        return dict(UNAVAILABLE)
    day = _day(dto.get("calendarDate")) or (groups[-1][0] if groups else None)
    first = num(as_dict(groups[0][1]).get("groupAverage"), 0) if len(groups) >= 2 else None
    return clean(
        {
            "date": day,
            "score": latest,
            "classification": dto.get("classification"),
            "change": latest - first if first is not None else None,
        }
    )


def shape_hill_score(raw: Any) -> dict[str, Any]:
    items = as_list(as_dict(raw).get("hillScoreDTOList")) if isinstance(raw, dict) else as_list(raw)
    scored = [d for d in items if isinstance(d, dict) and num(d.get("overallScore"), 0) is not None]
    if not scored:
        return dict(UNAVAILABLE)
    scored.sort(key=lambda d: str(d.get("calendarDate") or ""))
    last = scored[-1]
    change = None
    if len(scored) >= 2:
        # every entry of `scored` has a numeric overallScore (the filter above)
        change = num(last.get("overallScore"), 0) - num(scored[0].get("overallScore"), 0)  # type: ignore[operator]
    return clean(
        {
            "date": _day(last.get("calendarDate")),
            "score": num(last.get("overallScore"), 0),
            "strength": num(last.get("strengthScore"), 0),
            "endurance": num(last.get("enduranceScore"), 0),
            "classification": last.get("hillScoreClassificationId"),
            "change": change,
        }
    )


def shape_vo2max_trend(raw: Any) -> dict[str, Any]:
    points: dict[str, list[tuple[str, float]]] = {"running": [], "cycling": []}
    for entry in _entries(raw, "generic"):
        for sport_key, out_key in (("generic", "running"), ("cycling", "cycling")):
            part = as_dict(entry.get(sport_key))
            value = num(part.get("vo2MaxPreciseValue") or part.get("vo2MaxValue"), 1)
            day = _day(part.get("calendarDate")) or _day(entry.get("calendarDate"))
            if value is None or day is None:
                continue
            points[out_key].append((day, value))
    for series in points.values():
        series.sort(key=lambda p: p[0])
    if not points["running"] and not points["cycling"]:
        return dict(UNAVAILABLE)

    weekly: dict[str, dict[str, Any]] = {}
    for sport, series in points.items():
        latest_in_week: dict[str, float] = {}
        for day, value in series:  # sorted: a later day overwrites an earlier one in the same week
            d = date.fromisoformat(day)
            week = (d - timedelta(days=d.weekday())).isoformat()
            latest_in_week[week] = value
        for week, value in latest_in_week.items():
            weekly.setdefault(week, {"week": week})[sport] = value

    def change(series: list[tuple[str, float]]) -> float | int | None:
        return num(series[-1][1] - series[0][1], 1) if len(series) >= 2 else None

    run, bike = points["running"], points["cycling"]
    return clean(
        {
            "running": run[-1][1] if run else None,
            "cycling": bike[-1][1] if bike else None,
            "measured_on": run[-1][0] if run else bike[-1][0],
            "change_running": change(run),
            "change_cycling": change(bike),
            "weekly": [weekly[w] for w in sorted(weekly)],
        }
    )


# --------------------------------------------------------- running tolerance


def _snake(key: str) -> str:
    return _CAMEL.sub("_", key).lower()


def _tolerance_scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return num(value, 1)
    if isinstance(value, str) and len(value) <= MAX_TOLERANCE_TEXT:
        return value
    return None


def _tolerance_week(entry: dict[str, Any]) -> dict[str, Any] | None:
    day = next((d for d in (_day(entry.get(k)) for k in TOLERANCE_DATE_KEYS) if d), None)
    fields: dict[str, Any] = {}
    for key, value in entry.items():
        if not isinstance(key, str) or key in TOLERANCE_DATE_KEYS or _ID_KEY.search(key):
            continue
        if isinstance(value, dict):  # one level: rangeDTO {min, max} -> range_min, range_max
            prefix = _snake(key).removesuffix("_dto")
            for sub, inner in value.items():
                if not isinstance(sub, str) or _ID_KEY.search(sub):
                    continue
                kept = _tolerance_scalar(inner)
                if kept is not None:
                    fields[f"{prefix}_{_snake(sub)}"] = kept
            continue
        kept = _tolerance_scalar(value)
        if kept is not None:
            fields[_snake(key)] = kept
    if not any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in fields.values()):
        return None
    return clean({"date": day, **dict(list(fields.items())[:MAX_TOLERANCE_FIELDS])})


def shape_running_tolerance(raw: Any) -> dict[str, Any]:
    weeks = [w for w in (_tolerance_week(e) for e in _entries(raw, "calendarDate")) if w]
    if not weeks:
        return dict(UNAVAILABLE)
    weeks.sort(key=lambda w: str(w.get("date") or ""))
    return {"count": len(weeks), "weeks": weeks}


# ------------------------------------------------------- personal records


def shape_personal_records(raw: Any) -> dict[str, Any]:
    records: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for r in _entries(raw, "typeId"):
        tid = r.get("typeId")
        atype = r.get("activityType")
        day = _day(r.get("prStartTimeLocalFormatted")) or _day(r.get("activityStartDateTimeLocal"))
        aid = r.get("activityId")
        sort_id = tid if isinstance(tid, int) and not isinstance(tid, bool) else 9999
        running = atype in (None, "running")
        if tid in RUNNING_RECORDS and running:
            name, meters = RUNNING_RECORDS[tid]
            item = {"type": name, **_time_and_pace(r.get("value"), meters), "date": day, "activity_id": aid}
            records.append(((0, sort_id), item))
        elif tid == LONGEST_RUN and running:
            item = {"type": "longest_run", "distance_km": km(r.get("value")), "date": day, "activity_id": aid}
            records.append(((0, sort_id), item))
        else:
            item = {"type_id": tid, "activity_type": atype, "value": num(r.get("value"), 1),
                    "date": day, "activity_id": aid}
            records.append(((1, sort_id), item))
    records.sort(key=lambda p: p[0])
    shaped = [clean(item) for _, item in records]
    return {"count": len(shaped), "records": shaped}


# -------------------------------------------------------------------- gear


def shape_gear_item(item: dict[str, Any], stats: Any) -> dict[str, Any]:
    make_model = " ".join(str(p) for p in (item.get("gearMakeName"), item.get("gearModelName")) if p)
    name = item.get("displayName") or item.get("customMakeModel") or make_model or "unnamed"
    retired = str(item.get("gearStatusName") or "").lower() == "retired"
    limit = num(item.get("maximumMeters"), 3)
    s = {} if is_section_error(stats) else as_dict(stats)
    gear_type = item.get("gearTypeName")
    return clean(
        {
            "name": name,
            "type": str(gear_type).lower() if gear_type else None,
            "retired": retired,
            "since": _day(item.get("dateBegin")),
            "until": _day(item.get("dateEnd")) if retired else None,
            "distance_km": km(s.get("totalDistance")),
            "activities": num(s.get("totalActivities"), 0),
            "max_km": km(limit) if limit and limit > 0 else None,
            "stats_error": section_error(stats)["error"] if is_section_error(stats) else None,
        }
    )


# --------------------------------------------------------------- tool logic


def _latest_lactate(client: Any) -> Any:
    try:
        return client.get_lactate_threshold()
    except (TypeError, KeyError, AttributeError) as exc:  # library merge on an odd payload
        log.warning("lactate threshold payload not understood: %s", type(exc).__name__)
        return None


async def performance_metrics(service: GarminService, weeks: int, end: date | None = None) -> dict[str, Any]:
    if not MIN_WEEKS <= weeks <= MAX_WEEKS:
        raise ToolError(f"weeks must be between {MIN_WEEKS} and {MAX_WEEKS}")
    end = end or today(service.settings.timezone)
    start = end - timedelta(days=weeks * 7 - 1)
    s, e = start.isoformat(), end.isoformat()
    results = await service.call_many(
        {
            "race_predictions": lambda c: c.get_race_predictions(),
            "lactate_threshold": _latest_lactate,
            "heart_rate_zones": lambda c: c.get_heart_rate_zones(),
            "power_zones": lambda c: c.get_power_zones(),
            "endurance_score": lambda c: c.get_endurance_score(s, e),
            "hill_score": lambda c: c.get_hill_score(s, e),
            "vo2max": lambda c: c.get_max_metrics_range(s, e),
        }
    )
    return {
        "window": {"start": s, "end": e, "weeks": weeks},
        "race_predictions": _section(results, "race_predictions", shape_race_predictions),
        "lactate_threshold": _section(results, "lactate_threshold", shape_lactate_threshold),
        "heart_rate_zones": _section(results, "heart_rate_zones", shape_heart_rate_zones),
        "power_zones": _section(results, "power_zones", shape_power_zones),
        "endurance_score": _section(results, "endurance_score", shape_endurance_score),
        "hill_score": _section(results, "hill_score", shape_hill_score),
        "vo2max": _section(results, "vo2max", shape_vo2max_trend),
    }


async def race_predictions(service: GarminService, start_date: str | None, end_date: str | None) -> dict[str, Any]:
    try:
        start, end = resolve_range(start_date, end_date, service.settings.timezone, default_days=28, max_days=366)
    except OverflowError as exc:
        raise ToolError("start_date or end_date is out of range") from exc
    s, e = start.isoformat(), end.isoformat()
    # positional arguments: the library signature is (startdate, enddate, _type)
    raw = await service.call(none_if_missing(lambda c: c.get_race_predictions(s, e, "daily")))
    try:
        shaped = shape_race_prediction_days(raw, start, end)
    except Exception as exc:  # unexpected payload shape: degrade, don't fail the tool
        log.warning("could not shape %s: %s", "race_predictions", type(exc).__name__)
        shaped = {"error": "unexpected Garmin response for race_predictions"}
    return {"start": s, "end": e, **shaped}


async def running_tolerance(service: GarminService, weeks: int, end_date: str | None) -> dict[str, Any]:
    if not MIN_TOLERANCE_WEEKS <= weeks <= MAX_TOLERANCE_WEEKS:
        raise ToolError(f"weeks must be between {MIN_TOLERANCE_WEEKS} and {MAX_TOLERANCE_WEEKS}")
    end = resolve_date(end_date, service.settings.timezone, "end_date")
    try:
        start = end - timedelta(days=weeks * 7 - 1)
    except OverflowError as exc:
        raise ToolError("end_date is out of range") from exc
    s, e = start.isoformat(), end.isoformat()
    raw = await service.call(none_if_missing(lambda c: c.get_running_tolerance(s, e, "weekly")))
    try:
        shaped = shape_running_tolerance(raw)
    except Exception as exc:  # unexpected payload shape: degrade, don't fail the tool
        log.warning("could not shape %s: %s", "running_tolerance", type(exc).__name__)
        shaped = {"error": "unexpected Garmin response for running_tolerance"}
    return {"start": s, "end": e, "aggregation": "weekly", **shaped}


async def personal_records(service: GarminService) -> dict[str, Any]:
    raw = await service.call(lambda c: c.get_personal_record())
    return shape_personal_records(raw)


def _gear_list(client: Any) -> Any:
    pid = getattr(client, "profile_id", None)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ToolError("Garmin did not report a profile id for this account, so gear cannot be listed. Retry later.")
    return client.get_gear(str(pid))


async def gear(service: GarminService) -> dict[str, Any]:
    items = _entries(await service.call(_gear_list), "uuid")
    uuids = [i["uuid"] for i in items if isinstance(i.get("uuid"), str) and i["uuid"]]
    stats: dict[str, Any] = {}
    if uuids:
        # mypy cannot infer a lambda whose default binds the loop variable
        stats = await service.call_many(
            {u: (lambda c, u=u: c.get_gear_stats(u)) for u in uuids}  # type: ignore[misc]
        )
    # an item without a string uuid has no stats: the lookup then finds nothing
    shaped = [shape_gear_item(i, stats.get(i.get("uuid"))) for i in items]  # type: ignore[arg-type]
    shaped.sort(key=lambda g: (g["retired"], -(g.get("distance_km") or 0)))
    return {"count": len(shaped), "gear": shaped}
