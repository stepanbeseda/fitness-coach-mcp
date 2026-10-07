"""Trends and the weekly report over the stored Garmin history (``history_days``).

Pure functions over the store with today's date passed in; nothing here reads the clock or calls Garmin.
A value that was never stored is skipped, never counted as zero: a day without a value is not a day with
a low value. Activity totals are the exception by design: a day whose activities were fetched and found
empty is a real zero.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import math
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from . import tools
from .history import ENDURANCE_SPORTS, SPORTS
from .store import Store

# ----------------------------------------------------------------- catalogue

# name (a history_days column) -> (unit, decimals)
DAILY_METRICS: dict[str, tuple[str, int]] = {
    "readiness": ("score", 1),
    "hrv_last_night_ms": ("ms", 1),
    "hrv_weekly_avg_ms": ("ms", 1),
    "sleep_duration_h": ("h", 2),
    "sleep_score": ("score", 1),
    "resting_hr": ("bpm", 1),
    "body_battery_high": ("level", 1),
    "body_battery_low": ("level", 1),
    "stress_avg": ("level", 1),
    "acute_load": ("load", 1),
    "chronic_load": ("load", 1),
    "acwr_ratio": ("ratio", 2),
    "vo2max_running": ("ml/kg/min", 1),
    "vo2max_cycling": ("ml/kg/min", 1),
}
TOTAL_GROUPS: dict[str, tuple[str, ...]] = {
    "endurance": ENDURANCE_SPORTS,
    **{sport: (sport,) for sport in SPORTS},
}
TOTAL_FIELDS: dict[str, tuple[str, int]] = {
    "duration_min": ("min", 1),
    "distance_km": ("km", 2),
    "training_load": ("load", 1),
    "count": ("activities", 0),
}
# name -> (group, field, unit, decimals)
TOTAL_METRICS: dict[str, tuple[str, str, str, int]] = {
    f"{group}_{field}": (group, field, unit, decimals)
    for group in TOTAL_GROUPS
    for field, (unit, decimals) in TOTAL_FIELDS.items()
}
METRIC_NAMES: tuple[str, ...] = (*DAILY_METRICS, *TOTAL_METRICS)
TrendMetric = Literal[METRIC_NAMES]  # type: ignore[valid-type]

MAX_DAY_RANGE = 92
MAX_LONG_RANGE = 731
DEFAULT_RANGE_DAYS = 90

# ------------------------------------------------------------------ helpers


def _round(value: float, decimals: int) -> float | int:
    rounded = round(value, decimals)
    return int(rounded) if decimals == 0 else rounded


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return value


def _activities(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """The per-sport totals of a stored day; None when never fetched or not valid JSON."""
    if row is None or row.get("activities") is None:
        return None
    try:
        parsed = json.loads(row["activities"])
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _sport_sum(acts: dict[str, Any], sports: tuple[str, ...], field: str) -> float:
    total = 0.0
    for sport in sports:
        entry = acts.get(sport)
        if isinstance(entry, dict):
            total += _number(entry.get(field)) or 0
    return total


def _daily(row: dict[str, Any], metric: str) -> float | None:
    if metric in DAILY_METRICS:
        return _number(row.get(metric))
    group, field, _, _ = TOTAL_METRICS[metric]
    acts = _activities(row)
    return None if acts is None else _sport_sum(acts, TOTAL_GROUPS[group], field)


def _bucket(day: dt.date, period: str) -> tuple[str, dt.date, dt.date]:
    if period == "day":
        return day.isoformat(), day, day
    if period == "week":
        start, end, label = tools.week_of(day)
        return label, start, end
    last = calendar.monthrange(day.year, day.month)[1]
    return f"{day.year}-{day.month:02d}", day.replace(day=1), day.replace(day=last)


# ---------------------------------------------------------------- get_trends


def get_trends(
    store: Store,
    today: dt.date,
    *,
    metrics: list[str],
    start_date: str | None,
    end_date: str | None,
    period: str,
) -> dict[str, Any]:
    names = list(dict.fromkeys(metrics))
    end = tools.parse_date(end_date, "end_date") if end_date is not None else today - dt.timedelta(days=1)
    if end > today:
        raise ToolError("end_date must not be after today")
    start = tools.parse_date(start_date, "start_date") if start_date is not None else end - dt.timedelta(
        days=DEFAULT_RANGE_DAYS - 1
    )
    if start > end:
        raise ToolError("start_date must be on or before end_date")
    limit = MAX_DAY_RANGE if period == "day" else MAX_LONG_RANGE
    if (end - start).days + 1 > limit:
        raise ToolError(f"date range too long for period {period} (max {limit} days)")

    rows = store.history_rows(start, end)
    by_day = {dt.date.fromisoformat(r["date"]): r for r in rows}
    days_with_data = sum(1 for r in rows if r["recovery_fetched_on"] or r["activities_fetched_on"])
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "period": period,
        "days_with_data": days_with_data,
        "metrics": {name: _trend(name, by_day, start, end, period) for name in names},
    }


def _trend(
    metric: str, by_day: dict[dt.date, dict[str, Any]], start: dt.date, end: dt.date, period: str
) -> dict[str, Any]:
    if metric in DAILY_METRICS:
        unit, decimals = DAILY_METRICS[metric]
        kind = "daily"
    else:
        _, _, unit, decimals = TOTAL_METRICS[metric]
        kind = "total"
    values: list[tuple[dt.date, float]] = []
    for day in sorted(by_day):
        value = _daily(by_day[day], metric)
        if value is not None:
            values.append((day, value))
    out: dict[str, Any] = {"unit": unit, "kind": kind}
    if not values:
        return {**out, "summary": {"n": 0}, "buckets": []}

    grouped: dict[str, tuple[dt.date, dt.date, list[float]]] = {}
    for day, value in values:
        label, first, last = _bucket(day, period)
        grouped.setdefault(label, (max(first, start), min(last, end), []))[2].append(value)

    buckets: list[dict[str, Any]] = []
    means: list[float] = []  # what each bucket stands for in the change: its mean (the value for a day)
    for label, (first, last, vals) in grouped.items():
        mean = sum(vals) / len(vals)
        means.append(mean)
        if period == "day":
            buckets.append({"period": label, "value": _round(vals[0], decimals)})
            continue
        bucket: dict[str, Any] = {
            "period": label,
            "start": first.isoformat(),
            "end": last.isoformat(),
            "n": len(vals),
            "mean": _round(mean, decimals),
            "min": _round(min(vals), decimals),
            "max": _round(max(vals), decimals),
        }
        if kind == "total":
            bucket["total"] = _round(sum(vals), decimals)
        buckets.append(bucket)

    every = [v for _, v in values]
    summary: dict[str, Any] = {
        "n": len(every),
        "mean": _round(sum(every) / len(every), decimals),
        "min": _round(min(every), decimals),
        "max": _round(max(every), decimals),
        "first": None,
        "last": None,
        "change": None,
        "change_pct": None,
    }
    if len(means) >= 2:
        first_value, last_value = means[0], means[-1]
        change = last_value - first_value
        summary["first"] = _round(first_value, decimals)
        summary["last"] = _round(last_value, decimals)
        summary["change"] = _round(change, decimals)
        summary["change_pct"] = round(change * 100 / first_value, 1) if first_value != 0 else None
    return {**out, "summary": summary, "buckets": buckets}


# ------------------------------------------------------------- weekly report

STRENGTH_NOT_AVAILABLE = {
    "available": False,
    "reason": "strength history from Hevy is not stored yet; read Hevy directly",
}
BASELINE_WEEKS = 4
FLAG_PCT = 30.0
MIN_DAYS_FOR_FLAG = 4
MIN_BASELINE_WEEKS_FOR_FLAG = 2
MIN_VO2MAX_DAYS = 28
MIN_BEST_WEEKS = 4


class _Week:
    """The seven stored days of one ISO week (a missing row is None)."""

    def __init__(self, monday: dt.date, by_day: dict[dt.date, dict[str, Any]]) -> None:
        self.monday = monday
        self.days: list[tuple[dt.date, dict[str, Any] | None]] = [
            (monday + dt.timedelta(days=n), by_day.get(monday + dt.timedelta(days=n))) for n in range(7)
        ]
        self.acts: list[dict[str, Any] | None] = [_activities(row) for _, row in self.days]

    def values(self, column: str) -> list[float]:
        out = []
        for _, row in self.days:
            value = _number(row.get(column)) if row else None
            if value is not None:
                out.append(value)
        return out

    @property
    def activity_days(self) -> int:
        return sum(1 for acts in self.acts if acts is not None)

    @property
    def complete(self) -> bool:
        """All seven days have activities fetched: a total of this week is a real weekly total."""
        return self.activity_days == 7

    def total(self, sports: tuple[str, ...], field: str) -> float:
        return sum(_sport_sum(acts, sports, field) for acts in self.acts if acts is not None)


@dataclass(frozen=True)
class _Stat:
    week: float
    baseline: float | None
    baseline_weeks: int
    decimals: int

    @property
    def change(self) -> float | None:
        return None if self.baseline is None else round(self.week - self.baseline, 9)

    @property
    def change_pct(self) -> float | None:
        # Unrounded (to 9 places, against float noise): the flags compare it with their thresholds.
        if self.baseline is None or self.baseline == 0:
            return None
        return round((self.week - self.baseline) * 100 / self.baseline, 9)

    def out(self) -> dict[str, Any]:
        pct = self.change_pct
        return {
            "week": _round(self.week, self.decimals),
            "baseline": None if self.baseline is None else _round(self.baseline, self.decimals),
            "baseline_weeks": self.baseline_weeks,
            "change": None if self.change is None else _round(self.change, self.decimals),
            "change_pct": None if pct is None else round(pct, 1),
        }


def _stat(week_value: float, baseline_values: list[float], decimals: int) -> _Stat:
    baseline = sum(baseline_values) / len(baseline_values) if baseline_values else None
    return _Stat(week_value, baseline, len(baseline_values), decimals)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def get_weekly_report(store: Store, today: dt.date, *, week: str | None) -> dict[str, Any]:
    if week is not None:
        monday, sunday, label = tools.week_of(tools.parse_date(week, "week"))
    else:
        monday, sunday, label = tools.week_of(today - dt.timedelta(days=7))
    if monday > today:
        raise ToolError("week must not be in the future")
    partial = sunday >= today

    baseline_mondays = [monday - dt.timedelta(days=7 * n) for n in range(BASELINE_WEEKS, 0, -1)]
    rows = store.history_rows(baseline_mondays[0], sunday)
    by_day = {dt.date.fromisoformat(r["date"]): r for r in rows}
    this = _Week(monday, by_day)
    baselines = [_Week(m, by_day) for m in baseline_mondays]
    complete_baselines = [w for w in baselines if w.complete]
    final = this.complete and not partial  # a week whose totals are real totals and will not change

    endurance = _endurance(this, complete_baselines)
    sleep = _daily_section(this, baselines, "sleep_duration_h", "duration_h", "sleep_score", "score")
    hrv = _hrv_section(this, baselines)
    resting = _daily_section(this, baselines, "resting_hr", "bpm")
    adherence = _adherence(store, today, monday, sunday, baseline_mondays)
    stats = {
        "load": _stat_for(this, complete_baselines, "endurance", "training_load"),
        "time": _stat_for(this, complete_baselines, "endurance", "duration_min"),
        "sleep": _daily_stat(this, baselines, "sleep_duration_h"),
        "hrv": _daily_stat(this, baselines, "hrv_last_night_ms"),
        "resting": _daily_stat(this, baselines, "resting_hr"),
    }
    return {
        "week": {
            "start": monday.isoformat(),
            "end": sunday.isoformat(),
            "iso_week": label,
            "partial": partial,
        },
        "baseline_weeks": [tools.week_of(m)[2] for m in baseline_mondays],
        "endurance": endurance,
        "strength": dict(STRENGTH_NOT_AVAILABLE),
        "sleep": sleep,
        "hrv": hrv,
        "resting_hr": resting,
        "adherence": adherence,
        "new_bests": _new_bests(store, this, monday, final),
        "flags": _flags(this, final, stats),
    }


def _stat_for(this: _Week, complete_baselines: list[_Week], group: str, field: str) -> _Stat | None:
    """Weekly total of an activity group against the complete baseline weeks; None without activity days."""
    if this.activity_days == 0:
        return None
    sports = TOTAL_GROUPS[group]
    decimals = TOTAL_FIELDS[field][1]
    return _stat(this.total(sports, field), [w.total(sports, field) for w in complete_baselines], decimals)


def _daily_stat(this: _Week, baselines: list[_Week], column: str) -> _Stat | None:
    """Mean of a daily metric against the baseline weeks that have at least one value."""
    values = this.values(column)
    if not values:
        return None
    decimals = DAILY_METRICS[column][1]
    return _stat(_mean(values), [_mean(v) for w in baselines if (v := w.values(column))], decimals)


def _endurance(this: _Week, complete_baselines: list[_Week]) -> dict[str, Any]:
    if this.activity_days == 0:
        return {"available": False}
    count = int(this.total(ENDURANCE_SPORTS, "count"))
    total: dict[str, Any] = {"count": count}
    for field in ("duration_min", "distance_km", "training_load"):
        stat = _stat_for(this, complete_baselines, "endurance", field)
        assert stat is not None
        total[field] = stat.out()

    by_sport: dict[str, Any] = {}
    for sport in ENDURANCE_SPORTS:
        sport_count = int(this.total((sport,), "count"))
        base_duration = (
            _mean([w.total((sport,), "duration_min") for w in complete_baselines]) if complete_baselines else None
        )
        if sport_count == 0 and not (base_duration or 0) > 0:
            continue
        base_load = (
            _mean([w.total((sport,), "training_load") for w in complete_baselines]) if complete_baselines else None
        )
        by_sport[sport] = {
            "count": sport_count,
            "duration_min": _round(this.total((sport,), "duration_min"), 1),
            "distance_km": _round(this.total((sport,), "distance_km"), 2),
            "training_load": _round(this.total((sport,), "training_load"), 1),
            "baseline_duration_min": None if base_duration is None else _round(base_duration, 1),
            "baseline_training_load": None if base_load is None else _round(base_load, 1),
        }

    out: dict[str, Any] = {"activity_days": this.activity_days, "total": total, "by_sport": by_sport}
    strength_count = int(this.total(("strength",), "count"))
    if strength_count > 0:
        out["garmin_strength"] = {
            "count": strength_count,
            "duration_min": _round(this.total(("strength",), "duration_min"), 1),
            "training_load": _round(this.total(("strength",), "training_load"), 1),
        }
    latest = next(
        (row for _, row in reversed(this.days) if row is not None and _number(row.get("acute_load")) is not None),
        None,
    )
    if latest is not None:
        out["load"] = {
            "date": latest["date"],
            "acute": _number(latest.get("acute_load")),
            "chronic": _number(latest.get("chronic_load")),
            "acwr_ratio": _number(latest.get("acwr_ratio")),
        }
    return out


def _daily_section(
    this: _Week, baselines: list[_Week], main: str, main_key: str, second: str | None = None, second_key: str = ""
) -> dict[str, Any]:
    """``days`` and the stat of one or two daily columns; unavailable when no day has any of them."""
    columns = [(main, main_key)] + ([(second, second_key)] if second else [])
    if not any(this.values(column) for column, _ in columns):
        return {"available": False}
    out: dict[str, Any] = {"days": len(this.values(main))}
    for column, key in columns:
        stat = _daily_stat(this, baselines, column)
        out[key] = None if stat is None else stat.out()
    return out


def _hrv_section(this: _Week, baselines: list[_Week]) -> dict[str, Any]:
    values = this.values("hrv_last_night_ms")
    if not values:
        return {"available": False}
    stat = _daily_stat(this, baselines, "hrv_last_night_ms")
    assert stat is not None
    status = next(
        (row["hrv_status"] for _, row in reversed(this.days) if row is not None and row.get("hrv_status")), None
    )
    return {
        "days": len(values),
        "last_night_ms": stat.out(),
        "min_ms": _round(min(values), 1),
        "status": status,
    }


def _adherence(
    store: Store, today: dt.date, monday: dt.date, sunday: dt.date, baseline_mondays: list[dt.date]
) -> dict[str, Any]:
    rows = store.sessions_between(monday, sunday)
    if not rows:
        return {"available": False}
    rates = []
    for m in baseline_mondays:
        rate = tools.adherence(store.sessions_between(m, m + dt.timedelta(days=6)), today)["completion_rate"]
        if rate is not None:
            rates.append(rate)
    return {
        **tools.adherence(rows, today),
        "baseline_completion_rate": round(_mean(rates), 2) if rates else None,
        "baseline_weeks": len(rates),
    }


# Weekly-total records: metric name -> (sports, field); a week counts only when all its days are fetched.
_WEEKLY_BESTS: tuple[tuple[str, str, str], ...] = (
    ("running_distance_km", "running", "distance_km"),
    ("cycling_distance_km", "cycling", "distance_km"),
    ("swimming_distance_km", "swimming", "distance_km"),
    ("endurance_duration_min", "endurance", "duration_min"),
    ("endurance_training_load", "endurance", "training_load"),
)


def _new_bests(store: Store, this: _Week, monday: dt.date, final: bool) -> list[dict[str, Any]]:
    earlier_rows = store.history_rows(None, monday - dt.timedelta(days=1))
    out: list[dict[str, Any]] = []
    for column in ("vo2max_running", "vo2max_cycling"):
        earlier = [v for r in earlier_rows if (v := _number(r.get(column))) is not None]
        week_values = this.values(column)
        if len(earlier) >= MIN_VO2MAX_DAYS and week_values and max(week_values) > max(earlier):
            out.append(
                {
                    "metric": column,
                    "value": _round(max(week_values), 1),
                    "previous_best": _round(max(earlier), 1),
                }
            )
    if not final:
        return out

    by_day = {dt.date.fromisoformat(r["date"]): r for r in earlier_rows}
    first = min(by_day, default=monday)
    first_monday = first - dt.timedelta(days=first.weekday())
    earlier_weeks = []
    candidate = first_monday
    while candidate < monday:
        week = _Week(candidate, by_day)
        if week.complete:
            earlier_weeks.append(week)
        candidate += dt.timedelta(days=7)
    if len(earlier_weeks) < MIN_BEST_WEEKS:
        return out
    for metric, group, field in _WEEKLY_BESTS:
        sports = TOTAL_GROUPS[group]
        decimals = TOTAL_FIELDS[field][1]
        value = this.total(sports, field)
        best = max(w.total(sports, field) for w in earlier_weeks)
        if value > 0 and value > best:
            out.append(
                {
                    "metric": metric,
                    "value": _round(value, decimals),
                    "previous_best": _round(best, decimals),
                    "weeks_compared": len(earlier_weeks),
                }
            )
    return out


def _flag(name: str, stat: _Stat) -> dict[str, Any]:
    out = stat.out()
    return {
        "flag": name,
        "week": out["week"],
        "baseline": out["baseline"],
        "change": out["change"],
        "change_pct": out["change_pct"],
    }


def _flags(this: _Week, final: bool, stats: dict[str, _Stat | None]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    load, time_ = stats["load"], stats["time"]
    if final and load is not None and load.baseline_weeks >= MIN_BASELINE_WEEKS_FOR_FLAG:
        pct = load.change_pct
        if pct is not None and pct > FLAG_PCT:
            flags.append(_flag("endurance_load_up", load))
        if pct is not None and pct < -FLAG_PCT:
            flags.append(_flag("endurance_load_down", load))
    if final and time_ is not None and time_.baseline_weeks >= MIN_BASELINE_WEEKS_FOR_FLAG:
        pct = time_.change_pct
        if pct is not None and pct > FLAG_PCT:
            flags.append(_flag("endurance_time_up", time_))

    sleep, hrv, resting = stats["sleep"], stats["hrv"], stats["resting"]
    if (
        sleep is not None
        and len(this.values("sleep_duration_h")) >= MIN_DAYS_FOR_FLAG
        and sleep.baseline_weeks >= MIN_BASELINE_WEEKS_FOR_FLAG
        and sleep.change is not None
        and sleep.change <= -0.5
    ):
        flags.append(_flag("sleep_down", sleep))
    if (
        hrv is not None
        and len(this.values("hrv_last_night_ms")) >= MIN_DAYS_FOR_FLAG
        and hrv.baseline_weeks >= MIN_BASELINE_WEEKS_FOR_FLAG
        and hrv.change_pct is not None
        and hrv.change_pct <= -5
    ):
        flags.append(_flag("hrv_down", hrv))
    if (
        resting is not None
        and len(this.values("resting_hr")) >= MIN_DAYS_FOR_FLAG
        and resting.baseline_weeks >= MIN_BASELINE_WEEKS_FOR_FLAG
        and resting.change is not None
        and resting.change >= 5
    ):
        flags.append(_flag("resting_hr_up", resting))
    return flags

