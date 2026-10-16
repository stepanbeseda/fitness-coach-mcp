"""Trends and the weekly report over the stored Garmin history (``history_days``).

Pure functions over the history view (``history_view``) with today's date passed in; nothing here reads the
clock or calls Garmin; only the weekly adherence reads the planned sessions from the store.
A value that was never stored is skipped, never counted as zero: a day without a value is not a day with
a low value. Activity totals are the exception by design: a day whose activities were fetched and found
empty is a real zero.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from . import strength, tools
from .history import ENDURANCE_SPORTS, SPORTS
from .history_view import HistoryDay, HistoryReader, HistoryView, history_range
from .store import Store
from .values import finite, parse_date

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
# field -> (unit, value decimals, mean decimals). Values, min, max and total of a count are whole numbers, but
# a mean, first, last and change of a count keep 2 decimals: 1 run in a week of 7 days is a mean of 0.14, not 0.
TOTAL_FIELDS: dict[str, tuple[str, int, int]] = {
    "duration_min": ("min", 1, 1),
    "distance_km": ("km", 2, 2),
    "training_load": ("load", 1, 1),
    "count": ("activities", 0, 2),
}
# name -> (group, field, unit, value decimals, mean decimals)
TOTAL_METRICS: dict[str, tuple[str, str, str, int, int]] = {
    f"{group}_{field}": (group, field, unit, decimals, mean_decimals)
    for group in TOTAL_GROUPS
    for field, (unit, decimals, mean_decimals) in TOTAL_FIELDS.items()
}
METRIC_NAMES: tuple[str, ...] = (*DAILY_METRICS, *TOTAL_METRICS)
TrendMetric = Literal[METRIC_NAMES]  # type: ignore[valid-type]

# ------------------------------------------------------------------ helpers


def _round(value: float, decimals: int) -> float | int:
    rounded = round(value, decimals)
    return int(rounded) if decimals == 0 else rounded


def _sport_sum(acts: Mapping[str, Any], sports: tuple[str, ...], field: str) -> float:
    total = 0.0
    for sport in sports:
        entry = acts.get(sport)
        if isinstance(entry, dict):
            total += finite(entry.get(field)) or 0
    return total


def _daily(day: HistoryDay, metric: str) -> float | None:
    if metric in DAILY_METRICS:
        return day.values.get(metric)
    group, field, *_ = TOTAL_METRICS[metric]
    acts = day.activities
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
    history: HistoryReader | None = None,
) -> dict[str, Any]:
    names = list(dict.fromkeys(metrics))
    start, end = history_range(today, start_date, end_date, period=period)
    days = (history if history is not None else HistoryView(store)).days(start, end)
    by_day = {day.date: day for day in days}
    days_with_data = sum(1 for day in days if day.has_data)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "period": period,
        "days_with_data": days_with_data,
        "metrics": {name: _trend(name, by_day, start, end, period) for name in names},
    }


def _trend(
    metric: str, by_day: dict[dt.date, HistoryDay], start: dt.date, end: dt.date, period: str
) -> dict[str, Any]:
    if metric in DAILY_METRICS:
        unit, decimals = DAILY_METRICS[metric]
        mean_decimals = decimals
        kind = "daily"
    else:
        _, _, unit, decimals, mean_decimals = TOTAL_METRICS[metric]
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
            "mean": _round(mean, mean_decimals),
            "min": _round(min(vals), decimals),
            "max": _round(max(vals), decimals),
        }
        if kind == "total":
            bucket["total"] = _round(sum(vals), decimals)
        buckets.append(bucket)

    every = [v for _, v in values]
    summary: dict[str, Any] = {
        "n": len(every),
        "mean": _round(sum(every) / len(every), mean_decimals),
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
        summary["first"] = _round(first_value, mean_decimals)
        summary["last"] = _round(last_value, mean_decimals)
        summary["change"] = _round(change, mean_decimals)
        summary["change_pct"] = round(change * 100 / first_value, 1) if first_value != 0 else None
    return {**out, "summary": summary, "buckets": buckets}


# ------------------------------------------------------------- weekly report

STRENGTH_NO_DATA = {"available": False, "reason": "no strength history from Hevy covers this week"}
BASELINE_WEEKS = 4
FLAG_PCT = 30.0
MIN_DAYS_FOR_FLAG = 4
MIN_BASELINE_WEEKS_FOR_FLAG = 2
MIN_VO2MAX_DAYS = 28
MIN_BEST_WEEKS = 4


class _Week:
    """The seven stored days of one ISO week (a missing row is None)."""

    def __init__(self, monday: dt.date, by_day: dict[dt.date, HistoryDay]) -> None:
        self.monday = monday
        self.days: list[tuple[dt.date, HistoryDay | None]] = [
            (monday + dt.timedelta(days=n), by_day.get(monday + dt.timedelta(days=n))) for n in range(7)
        ]
        self.acts: list[Mapping[str, Any] | None] = [day.activities if day else None for _, day in self.days]

    def values(self, column: str) -> list[float]:
        out = []
        for _, day in self.days:
            value = day.values.get(column) if day else None
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


def get_weekly_report(
    store: Store, today: dt.date, *, week: str | None, history: HistoryReader | None = None
) -> dict[str, Any]:
    if week is not None:
        monday, sunday, label = tools.week_of(parse_date(week, "week"))
    else:
        monday, sunday, label = tools.week_of(today - dt.timedelta(days=7))
    if monday > today:
        raise ToolError("week must not be in the future")
    partial = sunday >= today

    baseline_mondays = [monday - dt.timedelta(days=7 * n) for n in range(BASELINE_WEEKS, 0, -1)]
    view = history if history is not None else HistoryView(store)
    by_day = {day.date: day for day in view.days(baseline_mondays[0], sunday)}
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
    strength_section, strength_bests, strength_flags = _strength(view, monday, sunday, baseline_mondays, partial)
    return {
        "week": {
            "start": monday.isoformat(),
            "end": sunday.isoformat(),
            "iso_week": label,
            "partial": partial,
        },
        "baseline_weeks": [tools.week_of(m)[2] for m in baseline_mondays],
        "endurance": endurance,
        "strength": strength_section,
        "sleep": sleep,
        "hrv": hrv,
        "resting_hr": resting,
        "adherence": adherence,
        "new_bests": _new_bests(view, this, monday, final) + strength_bests,
        "flags": _flags(this, final, stats) + strength_flags,
    }


def _strength(
    history: HistoryReader, monday: dt.date, sunday: dt.date, baseline_mondays: list[dt.date], partial: bool
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """The strength section, new e1RM bests and flags of the week from the stored Hevy workouts.

    A week has a section when at least one of its days is covered by the Hevy history; baselines are the
    complete covered weeks among the previous four, and the volume flag needs a final week like the others.
    """
    hist = history.strength(baseline_mondays[0], sunday)
    cov = hist.coverage
    if cov is None:
        return dict(STRENGTH_NO_DATA), [], []
    totals = hist.days
    this = strength.StrengthWeek(monday, totals, cov)
    if this.days == 0:
        return dict(STRENGTH_NO_DATA), [], []
    baselines = [w for m in baseline_mondays if (w := strength.StrengthWeek(m, totals, cov)).complete]

    volume = _stat(this.volume_kg, [w.volume_kg for w in baselines], 1)
    sets = _stat(this.sets, [w.sets for w in baselines], 1)
    groups = {g for g, n in this.muscle_sets.items() if n > 0}
    for group in {g for w in baselines for g in w.muscle_sets}:
        if _mean([w.muscle_sets.get(group, 0) for w in baselines]) > 0:
            groups.add(group)
    by_group: dict[str, Any] = {}
    for group in sorted(groups):
        base_sets = _mean([w.muscle_sets.get(group, 0) for w in baselines]) if baselines else None
        base_volume = _mean([w.muscle_volume.get(group, 0.0) for w in baselines]) if baselines else None
        by_group[group] = {
            "sets": this.muscle_sets.get(group, 0),
            "baseline_sets": None if base_sets is None else round(base_sets, 1),
            "volume_kg": round(this.muscle_volume.get(group, 0.0), 1),
            "baseline_volume_kg": None if base_volume is None else round(base_volume, 1),
        }
    section = {
        "days": this.days,
        "workouts": this.workouts,
        "volume_kg": volume.out(),
        "sets": sets.out(),
        "by_muscle_group": by_group,
    }

    flags: list[dict[str, Any]] = []
    final = this.complete and not partial
    if (
        final
        and volume.baseline_weeks >= MIN_BASELINE_WEEKS_FOR_FLAG
        and volume.change_pct is not None
        and volume.change_pct > FLAG_PCT
    ):
        flags.append(_flag("strength_volume_up", volume))

    earlier = history.strength(None, monday - dt.timedelta(days=1)).days
    bests: list[dict[str, Any]] = []
    for tid, (best, day, title) in this.best.items():
        previous = max(
            (
                entry["best"]["e1rm_kg"]
                for total in earlier.values()
                if (entry := total.exercises.get(tid)) is not None and entry["best"] is not None
            ),
            default=None,
        )
        if previous is not None and best["e1rm_kg"] > previous:
            bests.append(
                {
                    "metric": "e1rm_kg",
                    "exercise": title,
                    "template_id": tid,
                    "value": best["e1rm_kg"],
                    "previous_best": previous,
                    "date": day.isoformat(),
                }
            )
    bests.sort(key=lambda b: (b["exercise"] or "", b["template_id"]))
    return section, bests, flags


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
        (day for _, day in reversed(this.days) if day is not None and day.values.get("acute_load") is not None),
        None,
    )
    if latest is not None:
        out["load"] = {
            "date": latest.date.isoformat(),
            "acute": latest.values.get("acute_load"),
            "chronic": latest.values.get("chronic_load"),
            "acwr_ratio": latest.values.get("acwr_ratio"),
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
    status = next((day.hrv_status for _, day in reversed(this.days) if day is not None and day.hrv_status), None)
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


def _new_bests(history: HistoryReader, this: _Week, monday: dt.date, final: bool) -> list[dict[str, Any]]:
    earlier_days = history.days(None, monday - dt.timedelta(days=1))
    out: list[dict[str, Any]] = []
    for column in ("vo2max_running", "vo2max_cycling"):
        earlier = [v for day in earlier_days if (v := day.values.get(column)) is not None]
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

    by_day = {day.date: day for day in earlier_days}
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

