"""Measurable goals: the metric catalogue, target parsing and the progress arithmetic (PR 14).

``get_goal_progress`` reads only what the nightly history stored (race predictions, running VO2max) and
computes the current value, the values 4 and 12 weeks ago, a least-squares trend and the value at the target
date at that trend. Reasons are codes, not prose, so the Monday message can translate them.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .history_view import HistoryReader, HistoryView
from .store import Store
from .values import finite

RECENT_DAYS = 14  # the current value is at most this old
TREND_DAYS = 84  # the trend window: 12 weeks up to the current value
MIN_TREND_POINTS = 14
MIN_TREND_SPAN_DAYS = 28
AGO_TOLERANCE_DAYS = 6  # "4 / 12 weeks ago" is the latest value in the 7 days up to the reference date
MAX_PROJECTION_DAYS = 182  # 26 weeks
SERIES_DAYS = TREND_DAYS + RECENT_DAYS - 1

_TIME_HMS = re.compile(r"([0-9]{1,2}):([0-5][0-9]):([0-5][0-9])")
_TIME_MS = re.compile(r"([0-9]{1,2}):([0-5][0-9])")
_VO2MAX_TEXT = re.compile(r"[0-9]{1,3}(\.[0-9]{1,2})?")


@dataclass(frozen=True)
class MetricSpec:
    source: str  # "race_predictions" or "history_days"
    column: str
    lower_is_better: bool
    low: float  # inclusive bounds of a target_value
    high: float
    unit: str


GOAL_METRICS: dict[str, MetricSpec] = {
    "race_5k": MetricSpec("race_predictions", "time_5k_s", True, 600, 7200, "s"),
    "race_10k": MetricSpec("race_predictions", "time_10k_s", True, 1200, 14400, "s"),
    "race_half_marathon": MetricSpec("race_predictions", "time_half_marathon_s", True, 2700, 28800, "s"),
    "race_marathon": MetricSpec("race_predictions", "time_marathon_s", True, 6300, 43200, "s"),
    "vo2max_running": MetricSpec("history_days", "vo2max_running", False, 20, 95, "ml/kg/min"),
}


def clock(seconds: float) -> str:
    """``H:MM:SS`` from one hour up, else ``M:SS``; whole seconds."""
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _number_text(value: float) -> str:
    return f"{value:g}"


def parse_target_value(metric: str, value: Any) -> float:
    """The stored target: seconds for a race metric, ml/kg/min for VO2max. Raises ``ToolError``."""
    spec = GOAL_METRICS[metric]
    if spec.unit == "s":
        text = value.strip() if isinstance(value, str) else None
        hms = _TIME_HMS.fullmatch(text) if text is not None else None
        ms = _TIME_MS.fullmatch(text) if text is not None and hms is None else None
        if hms is not None:
            seconds = int(hms[1]) * 3600 + int(hms[2]) * 60 + int(hms[3])
        elif ms is not None:
            seconds = int(ms[1]) * 60 + int(ms[2])
        else:
            raise ToolError(f"target_value for {metric} must be a time H:MM:SS or MM:SS, e.g. 1:45:00")
        number = float(seconds)
    else:
        message = f"target_value for {metric} must be a number, e.g. 55"
        if isinstance(value, bool):
            raise ToolError(message)
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str) and _VO2MAX_TEXT.fullmatch(value.strip()):
            number = float(value.strip())
        else:
            raise ToolError(message)
        if finite(number) is None:
            raise ToolError(message)
        number = round(number, 1)
    if not spec.low <= number <= spec.high:
        low, high = (clock(spec.low), clock(spec.high)) if spec.unit == "s" else (_number_text(spec.low), _number_text(spec.high))
        raise ToolError(f"target_value for {metric} must be between {low} and {high}")
    return number


def target_value_out(metric: str, value: float) -> str | float:
    """A stored target as the model entered it: a time text for a race metric, a number for VO2max."""
    return clock(value) if GOAL_METRICS[metric].unit == "s" else value


# ----------------------------------------------------------------- progress


def _series(history: HistoryReader, metric: str, today: dt.date) -> list[tuple[dt.date, float]]:
    spec = GOAL_METRICS[metric]
    start = today - dt.timedelta(days=SERIES_DAYS)
    days = history.predictions(start, today) if spec.source == "race_predictions" else history.days(start, today)
    points: list[tuple[dt.date, float]] = []
    for day in days:
        value = day.values.get(spec.column)
        if value is not None and value > 0:
            points.append((day.date, float(value)))
    points.sort()
    return points


def _ago(points: list[tuple[dt.date, float]], ref: dt.date) -> tuple[dt.date, float] | None:
    found = [p for p in points if ref - dt.timedelta(days=AGO_TOLERANCE_DAYS) <= p[0] <= ref]
    return found[-1] if found else None


def _slope_per_day(points: list[tuple[dt.date, float]]) -> float:
    first = points[0][0]
    xs = [(day - first).days for day, _ in points]
    ys = [value for _, value in points]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sum((x - mean_x) ** 2 for x in xs)


def _value_out(spec: MetricSpec, value: float) -> int | float:
    return round(value) if spec.unit == "s" else round(value, 1)


def _point_out(spec: MetricSpec, point: tuple[dt.date, float] | None) -> dict[str, Any] | None:
    if point is None:
        return None
    out: dict[str, Any] = {"date": point[0].isoformat(), "value": _value_out(spec, point[1])}
    if spec.unit == "s":
        out["time"] = clock(point[1])
    return out


def _goal_progress(history: HistoryReader, goal: dict[str, Any], today: dt.date) -> dict[str, Any]:
    metric = goal["metric"]
    spec = GOAL_METRICS[metric]
    target = goal["target_value"]
    target_date = dt.date.fromisoformat(goal["target_date"]) if goal["target_date"] else None
    target_out: dict[str, Any] | None = None
    if target is not None:
        target_out = {"value": _value_out(spec, target)}
        if spec.unit == "s":
            target_out["time"] = clock(target)

    points = _series(history, metric, today)
    current = points[-1] if points and points[-1][0] >= today - dt.timedelta(days=RECENT_DAYS) else None
    out: dict[str, Any] = {
        "goal_id": goal["id"],
        "title": goal["title"],
        "metric": metric,
        "unit": spec.unit,
        "target_value": target_out,
        "target_date": goal["target_date"],
        "days_remaining": None if target_date is None else (target_date - today).days,
        "available": False,
        "current": _point_out(spec, current),
        "weeks_4_ago": _point_out(spec, _ago(points, today - dt.timedelta(days=28))),
        "weeks_12_ago": _point_out(spec, _ago(points, today - dt.timedelta(days=84))),
        "trend_per_week": None,
        "points": 0,
        "projected": None,
        "on_track": None,
        "reason": None,
    }
    if current is None:
        out["reason"] = "no_recent_value"
        return out
    window = [p for p in points if p[0] >= current[0] - dt.timedelta(days=TREND_DAYS - 1)]
    out["points"] = len(window)
    if len(window) < MIN_TREND_POINTS or (window[-1][0] - window[0][0]).days < MIN_TREND_SPAN_DAYS:
        out["reason"] = "too_few_values"
        return out
    slope = _slope_per_day(window)
    out["available"] = True
    out["trend_per_week"] = round(slope * 7, 1 if spec.unit == "s" else 2)
    if target_date is None:
        out["reason"] = "no_target_date"
        return out
    if target_date < today:
        out["reason"] = "target_date_passed"
        return out
    ahead = (target_date - current[0]).days
    if ahead > MAX_PROJECTION_DAYS:
        out["reason"] = "target_date_too_far"
        return out
    projected = current[1] + slope * ahead
    if spec.lower_is_better and projected <= 0:
        out["reason"] = "trend_too_steep"
        return out
    out["projected"] = _point_out(spec, (target_date, projected))
    if target is None:
        out["reason"] = "no_target_value"
        return out
    out["on_track"] = projected <= target if spec.lower_is_better else projected >= target
    return out


def get_goal_progress(store: Store, today: dt.date, *, history: HistoryReader | None = None) -> dict[str, Any]:
    active = store.list_goals("active")
    measurable = [g for g in active if g["metric"] is not None]
    view = history if history is not None else HistoryView(store)
    return {
        "today": today.isoformat(),
        "goals_without_metric": len(active) - len(measurable),
        "goals": [_goal_progress(view, g, today) for g in measurable],
    }
