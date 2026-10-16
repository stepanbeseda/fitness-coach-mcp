"""History view: finished day values over the stored history (the read side of the nightly job's tables).

Every reader of the stored history (``trends``, ``correlations``, ``goals``, ``strength.get_strength_progress``
and the reconcile step of ``history``) reads it through ``HistoryView``, which turns the rows of
``history_days``, ``strength_workouts``, ``hevy_templates``, ``hevy_sync`` and ``race_predictions`` into values:

- a day's recovery values (finite numbers or None) and its activity totals parsed from the ``activities`` JSON
  column, with the flag that no later run re-fetches them (``activities_settled``);
- the strength days summed from the stored Hevy workout summaries, with the Hevy coverage and each template's
  primary muscle group;
- the Garmin activities of a day with the Hevy strength workouts merged in (``with_hevy_strength``);
- the race predictions.

It also owns the date-range defaults and limits of the history tools and of ``get_plan``. The JSON parsing of
the stored columns, the coverage rule and the settled rules live only here. The re-fetch windows that the
settled rules invert (``REFETCH_DAYS``, ``MAX_FAILURES``, ``OVERLAP``) are defined here too; the nightly job
(``history``, ``strength``) imports them. Nothing here writes, reads the clock or calls a backend.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from mcp.server.mcpserver.exceptions import ToolError

from .reconcile import DayActivities, count_of
from .store import HISTORY_COLUMNS, RACE_COLUMNS, Store
from .values import finite, finite_float, parse_date, valid_id, whole

# ------------------------------------------------------------- fetch windows (shared with the nightly job)

REFETCH_DAYS = 3  # a day is fetched on D+1, D+2 and D+3 (late watch syncs)
MAX_FAILURES = 3  # item errors after which a day is given up (or no longer re-fetched)
OVERLAP = timedelta(days=3)  # every Hevy listing re-reads the last 3 days (late syncs, since semantics unverified)

# ------------------------------------------------------------------ ranges

DEFAULT_RANGE_DAYS = 90  # history tools: the 90 days ending yesterday
MAX_DAY_RANGE = 92  # get_trends with period "day"
MAX_LONG_RANGE = 731  # every other history tool and period
PLAN_DAYS = 7  # get_plan: the ISO week of today
PLAN_MAX_RANGE_DAYS = 92


def history_range(
    today: date, start_date: str | None, end_date: str | None, *, period: str | None = None
) -> tuple[date, date]:
    """The range of a history tool (``get_trends``, ``get_correlations``, ``get_strength_progress``).

    ``end`` defaults to yesterday and must not be after today; ``start`` defaults to ``end`` - 89 days; the range
    holds at most ``MAX_DAY_RANGE`` days for ``period`` "day", else ``MAX_LONG_RANGE``. The errors are checked in
    this order: end_date format, end after today, start_date format, start after end, too long.
    """
    end = parse_date(end_date, "end_date") if end_date is not None else today - timedelta(days=1)
    if end > today:
        raise ToolError("end_date must not be after today")
    start = parse_date(start_date, "start_date") if start_date is not None else end - timedelta(
        days=DEFAULT_RANGE_DAYS - 1
    )
    if start > end:
        raise ToolError("start_date must be on or before end_date")
    limit = MAX_DAY_RANGE if period == "day" else MAX_LONG_RANGE
    if (end - start).days + 1 > limit:
        where = "" if period is None else f" for period {period}"
        raise ToolError(f"date range too long{where} (max {limit} days)")
    return start, end


def plan_range(today: date, start_date: str | None, end_date: str | None) -> tuple[date, date]:
    """The range of ``get_plan``: ``start`` defaults to the Monday of today's ISO week, ``end`` to ``start`` + 6
    days; at most ``PLAN_MAX_RANGE_DAYS`` days. Errors in this order: start_date format, end_date format, start
    after end, too long."""
    start = parse_date(start_date, "start_date") if start_date is not None else today - timedelta(days=today.weekday())
    end = parse_date(end_date, "end_date") if end_date is not None else start + timedelta(days=PLAN_DAYS - 1)
    if start > end:
        raise ToolError("start_date must be on or before end_date")
    if (end - start).days + 1 > PLAN_MAX_RANGE_DAYS:
        raise ToolError(f"date range too long (max {PLAN_MAX_RANGE_DAYS} days)")
    return start, end


# ------------------------------------------------------------------ Garmin days

NUMERIC_COLUMNS: tuple[str, ...] = tuple(c for c in HISTORY_COLUMNS if c != "hrv_status")


def activities_settled(day: date, fetched_on: date, failures: int) -> bool:
    """No later run re-fetches the activities of ``day``: the inverse of the re-fetch rule of ``history.needs_fetch``.
    """
    return fetched_on >= day + timedelta(days=REFETCH_DAYS) or failures >= MAX_FAILURES


@dataclass(frozen=True)
class HistoryDay:
    """One stored day of ``history_days``.

    ``values`` has every column of ``NUMERIC_COLUMNS``: a finite number or None (never stored, or not a finite
    number). ``activities`` is the per-sport totals object, or None when the activities were never fetched (also
    a given-up day) or the stored text is not a JSON object; a sport entry is passed on as stored.
    """

    date: dt.date
    recovery_fetched_on: dt.date | None
    activities_fetched_on: dt.date | None
    values: Mapping[str, int | float | None]
    hrv_status: str | None
    activities: Mapping[str, Any] | None
    activities_settled: bool  # False when activities is None

    @property
    def has_data(self) -> bool:
        """The recovery or the activities part was fetched (a row that only counts failures has no data)."""
        return self.recovery_fetched_on is not None or self.activities_fetched_on is not None


@dataclass(frozen=True)
class PredictionDay:
    date: dt.date
    values: Mapping[str, int | float | None]  # every column of RACE_COLUMNS: a finite number or None


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _activities(text: Any) -> dict[str, Any] | None:
    if not isinstance(text, str):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _history_day(row: Mapping[str, Any]) -> HistoryDay | None:
    """The value of one ``history_days`` row; None when its date is not a ``YYYY-MM-DD`` text."""
    day = _day(row.get("date"))
    if day is None:
        return None
    fetched_on = _day(row.get("activities_fetched_on"))
    activities = _activities(row.get("activities")) if fetched_on is not None else None
    status = row.get("hrv_status")
    return HistoryDay(
        date=day,
        recovery_fetched_on=_day(row.get("recovery_fetched_on")),
        activities_fetched_on=fetched_on,
        values={column: finite(row.get(column)) for column in NUMERIC_COLUMNS},
        hrv_status=status if isinstance(status, str) and status else None,
        activities=activities,
        activities_settled=(
            activities is not None
            and fetched_on is not None
            and activities_settled(day, fetched_on, row.get("activities_failures") or 0)
        ),
    )


# ------------------------------------------------------------------ Hevy strength

UNKNOWN_GROUP = "unknown"
ASSISTED_TYPES = frozenset({"bodyweight_assisted_reps"})  # weight_kg is assistance: no volume, no e1RM


def sync_dates(state: Mapping[str, Any] | None) -> tuple[date, date, datetime] | None:
    """(covered_from, synced_on, synced_at) of a usable ``hevy_sync`` row, else None."""
    if state is None:
        return None
    try:
        synced_at = datetime.fromisoformat(state["synced_at"])
        if synced_at.tzinfo is None:
            return None
        return date.fromisoformat(state["covered_from"]), date.fromisoformat(state["synced_on"]), synced_at
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Coverage:
    first: date
    through: date

    def covers(self, day: date) -> bool:
        return self.first <= day <= self.through


def coverage(state: Mapping[str, Any] | None) -> Coverage | None:
    """``covered_from`` .. ``synced_on`` - 1; None before the first complete listing."""
    dates = sync_dates(state)
    if dates is None:
        return None
    return Coverage(dates[0], dates[1] - timedelta(days=1))


class Templates:
    def __init__(self, rows: Mapping[str, Mapping[str, Any]]) -> None:
        self._rows = rows

    def _found(self, tid: str) -> Mapping[str, Any] | None:
        row = self._rows.get(tid)
        return row if row is not None and row.get("found") == 1 else None

    def group(self, tid: str) -> str:
        """Primary muscle group of a found template, else ``UNKNOWN_GROUP``."""
        row = self._found(tid)
        group = row.get("primary_muscle_group") if row else None
        return group if isinstance(group, str) and group else UNKNOWN_GROUP

    def title(self, tid: str) -> str | None:
        row = self._found(tid)
        title = row.get("title") if row else None
        return title if isinstance(title, str) and title else None

    def counts_load(self, tid: str) -> bool:
        """False for a found template whose type is assisted bodyweight (its weight is assistance)."""
        row = self._found(tid)
        return not (row is not None and row.get("type") in ASSISTED_TYPES)


def better(candidate: Mapping[str, Any], best: Mapping[str, Any] | None) -> bool:
    """Higher e1RM wins, a tie goes to the heavier set; the first one seen stays otherwise."""
    if best is None:
        return True
    return bool((candidate["e1rm_kg"], candidate["weight_kg"]) > (best["e1rm_kg"], best["weight_kg"]))


@dataclass
class DayStrength:
    workouts: int = 0
    sets: int = 0
    volume_kg: float = 0.0
    muscle_sets: dict[str, int] = field(default_factory=dict)
    muscle_volume: dict[str, float] = field(default_factory=dict)
    exercises: dict[str, dict[str, Any]] = field(default_factory=dict)  # tid -> {title, sets, volume_kg, best}

    def add(
        self, tid: str, *, group: str, title: str | None, sets: int, volume_kg: float, best: dict[str, Any] | None
    ) -> None:
        """Add one exercise entry of a workout (the workout itself is counted by the caller)."""
        self.sets += sets
        self.muscle_sets[group] = self.muscle_sets.get(group, 0) + sets
        self.volume_kg += volume_kg
        self.muscle_volume[group] = self.muscle_volume.get(group, 0.0) + volume_kg
        entry = self.exercises.setdefault(tid, {"title": title, "sets": 0, "volume_kg": 0.0, "best": None})
        entry["sets"] += sets
        entry["volume_kg"] += volume_kg
        if best is not None and better(best, entry["best"]):
            entry["best"] = best


def _entries(text: Any) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return []
    return [e for e in parsed if isinstance(e, dict)] if isinstance(parsed, list) else []


def _valid_best(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    weight, estimate = finite_float(value.get("weight_kg")), finite_float(value.get("e1rm_kg"))
    reps = whole(value.get("reps"))
    if weight is None or estimate is None or reps is None:
        return None
    return {"weight_kg": weight, "reps": reps, "e1rm_kg": estimate}


def day_totals(rows: Sequence[Mapping[str, Any]], templates: Templates) -> dict[date, DayStrength]:
    """Per local day: workouts, working sets and volume (also per primary muscle group) and the best set of
    every template, summed on read from the stored workout summaries."""
    out: dict[date, DayStrength] = {}
    for row in rows:
        try:
            day = date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError):
            continue
        total = out.setdefault(day, DayStrength())
        total.workouts += 1
        for entry in _entries(row.get("exercises")):
            tid, sets = entry.get("template_id"), whole(entry.get("sets"))
            if not valid_id(tid) or sets is None:
                continue
            counts = templates.counts_load(tid)
            total.add(
                tid,
                group=templates.group(tid),
                title=templates.title(tid) or (entry["title"] if isinstance(entry.get("title"), str) else None),
                sets=sets,
                volume_kg=(finite_float(entry.get("volume_kg")) or 0.0) if counts else 0.0,
                best=_valid_best(entry.get("best")) if counts else None,
            )
    return out


@dataclass(frozen=True)
class StrengthHistory:
    """The stored Hevy strength days of a range, the coverage and the muscle group of every template in them."""

    coverage: Coverage | None  # None before the first complete listing
    days: Mapping[date, DayStrength]  # only days with a live workout
    groups: Mapping[str, str]

    def group(self, tid: str) -> str:
        return self.groups.get(tid, UNKNOWN_GROUP)


def with_hevy_strength(
    days: Mapping[str, DayActivities], hevy: Mapping[date, DayStrength], cov: Coverage | None
) -> dict[str, DayActivities]:
    """Garmin day totals with Hevy strength workouts merged in (pure).

    Only days whose Garmin activities were fetched are in ``days`` and none is added, so the other sports of a
    day never get a false ``no_activity``. A day the Hevy history does not cover keeps its Garmin totals. On a
    covered day Hevy's workouts replace the strength entry when there is at least one and not fewer than the
    watch recorded (two watch sessions against one Hevy workout stay as Garmin evidence). Such a day is final
    only when the Garmin side is final and the day lies outside the 3-day re-read of the Hevy listing.
    """
    out = dict(days)
    if cov is None:
        return out
    for day_text, activities in days.items():
        day = date.fromisoformat(day_text)
        if not cov.covers(day):
            continue
        hevy_day = hevy.get(day)
        count = hevy_day.workouts if hevy_day is not None else 0
        if hevy_day is None or count == 0 or count < count_of(activities.totals.get("strength")):
            continue
        entry = {
            "count": count,
            "source": "hevy",
            "sets": hevy_day.sets,
            "volume_kg": round(hevy_day.volume_kg, 1),
        }
        out[day_text] = DayActivities(
            {**activities.totals, "strength": entry},
            activities.final and (cov.through - day).days >= OVERLAP.days,
        )
    return out


# ------------------------------------------------------------------ the view


class HistoryReader(Protocol):
    """What the history tools read; ``HistoryView`` over the Store, a fake of view values in the tests."""

    def days(self, start: date | None, end: date) -> list[HistoryDay]: ...

    def strength(self, start: date | None, end: date) -> StrengthHistory: ...

    def predictions(self, start: date | None, end: date) -> list[PredictionDay]: ...


class HistoryView:
    """Finished day values of the stored history; ranges are inclusive, ``start`` None means from the first day."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def days(self, start: date | None, end: date) -> list[HistoryDay]:
        """The stored days of ``history_days``, oldest first (also rows that only count failures)."""
        out = []
        for row in self._store.history_rows(start, end):
            day = _history_day(row)
            if day is not None:
                out.append(day)
        return out

    def strength(self, start: date | None, end: date) -> StrengthHistory:
        templates = Templates(self._store.hevy_templates())
        totals = day_totals(self._store.strength_workout_rows(start, end), templates)
        groups = {tid: templates.group(tid) for total in totals.values() for tid in total.exercises}
        return StrengthHistory(coverage(self._store.hevy_sync_state()), totals, groups)

    def predictions(self, start: date | None, end: date) -> list[PredictionDay]:
        out = []
        for row in self._store.race_prediction_rows(start, end):
            day = _day(row.get("date"))
            if day is not None:
                out.append(PredictionDay(day, {column: finite(row.get(column)) for column in RACE_COLUMNS}))
        return out

    def activity_days(self, start: date, end: date) -> dict[str, DayActivities]:
        """The fetched Garmin activities per date with the Hevy strength days merged in, for ``reconcile.suggest``.

        A day whose activities were never fetched (also a given-up day) or are unreadable is left out, so it gets
        no suggestion.
        """
        garmin = {
            day.date.isoformat(): DayActivities(day.activities, day.activities_settled)
            for day in self.days(start, end)
            if day.activities is not None
        }
        hevy = self.strength(start, end)
        return with_hevy_strength(garmin, hevy.days, hevy.coverage)
