"""Load signals of the morning briefing and the weekly report (PR 38): pure functions, no I/O and no clock.

Two signals, both computed here and nowhere else, so the morning alert and the weekly flag cannot disagree:

- ``detraining``: an endurance goal is active (a goal whose metric is a running metric) and, over the 14 days up
  to ``end``, either every stored daily ACWR is below 0.8 (at least 10 stored values), or the running distance is
  below half of the usual one (the mean of the complete 7-day blocks among the four before the window, times 2);
- ``load_spike``: the Hevy working sets (warm-ups are excluded when the workouts are summarised) of 7 days are more
  than 1.5x the mean of the fully covered 7-day windows among the four before them (at least 2).

An input that is not there (history off, too few stored ACWR values, days whose activities were never fetched,
no Hevy coverage) leaves the condition unknown (None), never false. ``load_signals`` is the one function that
computes the morning signals; PR 40 adds Foster monotony and strain to it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .goals import target_value_out
from .history_view import HistoryDay, HistoryReader, StrengthHistory
from .strength import StrengthWeek
from .values import finite

ENDURANCE_GOAL_METRICS = ("race_5k", "race_10k", "race_half_marathon", "race_marathon", "vo2max_running")
WINDOW_DAYS = 14
ACWR_LOW = 0.8
MIN_ACWR_VALUES = 10
RUNNING_BASELINE_WEEKS = 4
MIN_RUNNING_BASELINE_WEEKS = 2
RUNNING_DROP = 0.5
SPIKE_DAYS = 7
SPIKE_BASELINE_WINDOWS = 4
MIN_SPIKE_BASELINE_WINDOWS = 2
SPIKE_FACTOR = 1.5


@dataclass(frozen=True)
class GoalRef:
    id: int
    title: str
    metric: str
    target_value: str | float | None  # as get_goal_progress shows it: H:MM:SS for a race, a number for VO2max
    target_date: date | None

    def out(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "metric": self.metric,
            "target_value": self.target_value,
            "target_date": None if self.target_date is None else self.target_date.isoformat(),
        }


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def endurance_goal(goals: Iterable[Mapping[str, Any]]) -> GoalRef | None:
    """The active goal with a running metric and the earliest target date (no date last, then the lowest id)."""
    found = []
    for goal in goals:
        if goal.get("status") != "active" or goal.get("metric") not in ENDURANCE_GOAL_METRICS:
            continue
        value = finite(goal.get("target_value"))
        found.append(
            GoalRef(
                int(goal["id"]),
                str(goal.get("title") or ""),
                goal["metric"],
                None if value is None else target_value_out(goal["metric"], value),
                _date(goal.get("target_date")),
            )
        )
    found.sort(key=lambda g: (g.target_date is None, g.target_date or date.min, g.id))
    return found[0] if found else None


@dataclass(frozen=True)
class Detraining:
    holds: bool | None
    goal: GoalRef | None
    start: date
    end: date
    acwr: bool | None  # every stored ACWR of the window below ACWR_LOW; None with fewer than MIN_ACWR_VALUES
    acwr_days: int
    acwr_max: float | None
    acwr_last: float | None
    running: bool | None  # running km of the window below RUNNING_DROP x usual; None when unknown
    running_km: float | None
    baseline_week_km: float | None
    baseline_weeks: int

    @property
    def conditions(self) -> list[str]:
        return [name for name, held in (("acwr", self.acwr), ("running", self.running)) if held is True]

    def out(self) -> dict[str, Any]:
        """The fields of the weekly flag ``detraining`` (without ``flag``); see the spec's contract for PR 39."""
        per_week = WINDOW_DAYS / 7
        km, base = self.running_km, self.baseline_week_km
        return {
            "conditions": self.conditions,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "goal": None if self.goal is None else self.goal.out(),
            "acwr_days": self.acwr_days,
            "acwr_max": self.acwr_max,
            "acwr_last": self.acwr_last,
            "running_km": {
                "window": None if km is None else round(km, 2),
                "week": None if km is None else round(km / per_week, 2),
                "baseline": None if base is None else round(base, 2),
                "usual": None if base is None else round(base * per_week, 2),
                "baseline_weeks": self.baseline_weeks,
            },
        }


def _running_km(acts: Mapping[str, Any]) -> float:
    entry = acts.get("running")
    return (finite(entry.get("distance_km")) or 0.0) if isinstance(entry, Mapping) else 0.0


def _block_km(by_day: Mapping[date, HistoryDay], first: date, days: int) -> float | None:
    """Running km of ``days`` days from ``first``; None when one of them has no fetched activities."""
    total = 0.0
    for n in range(days):
        day = by_day.get(first + timedelta(days=n))
        if day is None or day.activities is None:
            return None
        total += _running_km(day.activities)
    return total


def detraining(days: Sequence[HistoryDay], end: date, goal: GoalRef | None) -> Detraining:
    """The detraining rule over the WINDOW_DAYS days up to ``end`` (``days`` must reach 41 days before ``end``)."""
    start = end - timedelta(days=WINDOW_DAYS - 1)
    by_day = {day.date: day for day in days}
    acwr_values = [
        value
        for n in range(WINDOW_DAYS)
        if (day := by_day.get(start + timedelta(days=n))) is not None
        and (value := day.values.get("acwr_ratio")) is not None
    ]
    acwr = None if len(acwr_values) < MIN_ACWR_VALUES else all(v < ACWR_LOW for v in acwr_values)

    km = _block_km(by_day, start, WINDOW_DAYS)
    blocks = [
        total
        for k in range(1, RUNNING_BASELINE_WEEKS + 1)
        if (total := _block_km(by_day, start - timedelta(days=7 * k), 7)) is not None
    ]
    base = sum(blocks) / len(blocks) if len(blocks) >= MIN_RUNNING_BASELINE_WEEKS else None
    if km is None or base is None:
        running = None
    else:
        running = base > 0 and km < RUNNING_DROP * base * WINDOW_DAYS / 7

    if goal is None:
        holds: bool | None = False
    elif acwr is True or running is True:
        holds = True
    elif acwr is False and running is False:
        holds = False
    else:
        holds = None
    return Detraining(
        holds,
        goal,
        start,
        end,
        acwr,
        len(acwr_values),
        max(acwr_values) if acwr_values else None,
        acwr_values[-1] if acwr_values else None,
        running,
        km,
        base,
        len(blocks),
    )


@dataclass(frozen=True)
class LoadSpike:
    holds: bool | None
    sets: int | None
    baseline: float | None  # mean working sets of the baseline windows
    baseline_weeks: int

    @property
    def ratio(self) -> float | None:
        if self.sets is None or not self.baseline:
            return None
        return round(self.sets / self.baseline, 2)

    def out(self) -> dict[str, Any]:
        return {
            "week": self.sets,
            "baseline": None if self.baseline is None else round(self.baseline, 1),
            "baseline_weeks": self.baseline_weeks,
            "ratio": self.ratio,
        }


def load_spike(sets: int | None, baseline_sets: Sequence[int]) -> LoadSpike:
    """``sets`` against the mean of ``baseline_sets`` (the complete windows only)."""
    n = len(baseline_sets)
    baseline = sum(baseline_sets) / n if n else None
    if sets is None or n < MIN_SPIKE_BASELINE_WINDOWS:
        return LoadSpike(None, sets, baseline, n)
    total = sum(baseline_sets)
    return LoadSpike(total > 0 and sets * n > SPIKE_FACTOR * total, sets, baseline, n)


def rolling_spike(hist: StrengthHistory, end: date) -> LoadSpike:
    """The SPIKE_DAYS days up to ``end`` against the SPIKE_BASELINE_WINDOWS windows before them."""
    cov = hist.coverage
    if cov is None:
        return LoadSpike(None, None, None, 0)
    first = end - timedelta(days=SPIKE_DAYS - 1)
    this = StrengthWeek(first, hist.days, cov)
    baselines = [
        w.sets
        for k in range(1, SPIKE_BASELINE_WINDOWS + 1)
        if (w := StrengthWeek(first - timedelta(days=SPIKE_DAYS * k), hist.days, cov)).complete
    ]
    return load_spike(this.sets if this.complete else None, baselines)


@dataclass(frozen=True)
class LoadSignals:
    detraining: Detraining
    load_spike: LoadSpike


def load_signals(history: HistoryReader, goals: Iterable[Mapping[str, Any]], day: date) -> LoadSignals:
    """The morning signals of ``day``: windows end yesterday, the last day the nightly history stored."""
    end = day - timedelta(days=1)
    days = history.days(end - timedelta(days=WINDOW_DAYS - 1 + 7 * RUNNING_BASELINE_WEEKS), end)
    strength = history.strength(end - timedelta(days=SPIKE_DAYS * (1 + SPIKE_BASELINE_WINDOWS) - 1), end)
    return LoadSignals(detraining(days, end, endurance_goal(goals)), rolling_spike(strength, end))
