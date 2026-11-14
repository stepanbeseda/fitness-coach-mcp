"""Foster session load of the coach plan (PR 40): pure functions, no I/O and no clock.

A done plan session gets a session RPE (sRPE, CR-10): the value the coach recorded with update_session, else for a
strength session the mean RPE of that day's Hevy working sets, else none (unrated). Its minutes are the Garmin
minutes of that day and sport when the day has exactly one done session of that type, else the planned duration.
Load = sRPE x minutes. The week sums the load for strength, endurance (running, cycling, swimming) and in total
(mobility and other too), against the mean of the complete weeks among the previous four. Monotony (mean / population
SD of the 7 daily loads, rest days 0) and strain (weekly load x monotony) need a finished week in which every done
plan session has a load. Unplanned training has no session RPE and is not counted. Weekly only: no morning alert.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from .reconcile import MATCHED_TYPES
from .values import finite, whole

STRENGTH_TYPES = ("strength",)
ENDURANCE_TYPES = ("running", "cycling", "swimming")
GROUPS = (("strength", STRENGTH_TYPES), ("endurance", ENDURANCE_TYPES))  # mobility and other: only in total
MONOTONY_LIMIT = 2.0
MAX_PLANNED_MINUTES = 1440


@dataclass(frozen=True)
class Rated:
    """One done plan session with its session RPE and minutes (each may be unknown) and where they came from."""

    id: int
    day: date
    type: str
    rpe: float | None
    rpe_source: Literal["coach", "hevy"] | None
    minutes: float | None
    minutes_source: Literal["garmin", "plan"] | None

    @property
    def load(self) -> float | None:
        """sRPE x minutes; None when either is unknown."""
        return None if self.rpe is None or self.minutes is None else self.rpe * self.minutes

    def out(self) -> dict[str, Any]:
        load = self.load
        return {
            "id": self.id,
            "date": self.day.isoformat(),
            "type": self.type,
            "rpe": self.rpe,
            "rpe_source": self.rpe_source,
            "minutes": None if self.minutes is None else round(self.minutes, 1),
            "minutes_source": self.minutes_source,
            "load": None if load is None else round(load),
        }


def _rpe(row: Mapping[str, Any], day: date, hevy_rpe: Mapping[date, float]) -> tuple[float | None, Literal["coach", "hevy"] | None]:
    value = whole(row.get("rpe"))
    if value is not None and 1 <= value <= 10:
        return float(value), "coach"
    if row["type"] == "strength" and day in hevy_rpe:
        return round(hevy_rpe[day], 1), "hevy"
    return None, None


def _minutes(
    row: Mapping[str, Any],
    day: date,
    sessions: Sequence[Mapping[str, Any]],
    activities: Mapping[date, Mapping[str, Any] | None],
) -> tuple[float | None, Literal["garmin", "plan"] | None]:
    kind = row["type"]
    if kind in MATCHED_TYPES:
        same = [r for r in sessions if r["status"] == "done" and r["type"] == kind and r["date"] == row["date"]]
        totals = activities.get(day)
        entry = totals.get(kind) if isinstance(totals, Mapping) else None
        garmin = finite(entry.get("duration_min")) if isinstance(entry, dict) else None
        if len(same) == 1 and garmin is not None and garmin > 0:
            return float(garmin), "garmin"
    planned = whole(row.get("duration_min"))
    if planned is not None and 1 <= planned <= MAX_PLANNED_MINUTES:
        return float(planned), "plan"
    return None, None


def rate(
    sessions: Sequence[Mapping[str, Any]],
    activities: Mapping[date, Mapping[str, Any] | None],
    hevy_rpe: Mapping[date, float],
) -> list[Rated]:
    """The done sessions of ``sessions`` (input order) with their session RPE and minutes."""
    out = []
    for row in sessions:
        if row["status"] != "done":
            continue
        day = date.fromisoformat(row["date"])
        rpe, rpe_source = _rpe(row, day, hevy_rpe)
        minutes, minutes_source = _minutes(row, day, sessions, activities)
        out.append(Rated(row["id"], day, row["type"], rpe, rpe_source, minutes, minutes_source))
    return out


def _group(rated: Sequence[Rated]) -> dict[str, Any]:
    counted = [r for r in rated if r.load is not None]
    load = sum(r.load or 0.0 for r in counted)
    minutes = sum(r.minutes or 0.0 for r in counted)
    return {
        "load": round(load),
        "minutes": round(minutes, 1),
        "avg_rpe": round(load / minutes, 1) if counted and minutes > 0 else None,
        "rated": sum(1 for r in rated if r.rpe is not None),
        "unrated": sum(1 for r in rated if r.rpe is None),
        "no_minutes": sum(1 for r in rated if r.rpe is not None and r.minutes is None),
    }


def _complete(rated: Sequence[Rated]) -> bool:
    return bool(rated) and all(r.load is not None for r in rated)


def _week(rated: Sequence[Rated], monday: date) -> list[Rated]:
    return [r for r in rated if monday <= r.day <= monday + timedelta(days=6)]


def _daily(week: Sequence[Rated], monday: date) -> list[float]:
    return [sum(r.load or 0.0 for r in week if r.day == monday + timedelta(days=i)) for i in range(7)]


def _baseline(rated: Sequence[Rated], baseline_mondays: Sequence[date]) -> dict[str, Any]:
    weeks = [w for w in (_week(rated, m) for m in baseline_mondays) if _complete(w)]
    if not weeks:
        return {"total": None, "strength": None, "endurance": None, "weeks": 0}

    def mean_load(types: Sequence[str] | None) -> int:
        loads = [sum(r.load or 0.0 for r in w if types is None or r.type in types) for w in weeks]
        return round(sum(loads) / len(loads))

    return {
        "total": mean_load(None),
        "strength": mean_load(STRENGTH_TYPES),
        "endurance": mean_load(ENDURANCE_TYPES),
        "weeks": len(weeks),
    }


def section(
    sessions: Sequence[Mapping[str, Any]],
    activities: Mapping[date, Mapping[str, Any] | None],
    hevy_rpe: Mapping[date, float],
    monday: date,
    baseline_mondays: Sequence[date],
    *,
    partial: bool,
) -> dict[str, Any]:
    """The ``session_load`` section of the weekly report: this week's load against the baseline, plus monotony."""
    rated = rate(sessions, activities, hevy_rpe)
    week = _week(rated, monday)
    complete = _complete(week)
    monotony: float | None = None
    strain: int | None = None
    if complete and not partial:
        daily = _daily(week, monday)
        spread = statistics.pstdev(daily)  # population SD: the 7 days are the whole week, not a sample
        if spread != 0:
            ratio = statistics.mean(daily) / spread
            monotony = round(ratio, 2)
            strain = round(sum(daily) * ratio)
    groups = {name: _group([r for r in week if r.type in types]) for name, types in GROUPS}
    return {
        "total": _group(week),
        "strength": groups["strength"],
        "endurance": groups["endurance"],
        "complete": complete,
        "sessions": [r.out() for r in week],
        "baseline": _baseline(rated, baseline_mondays),
        "monotony": monotony,
        "strain": strain,
    }


def flags(section: Mapping[str, Any], *, final: bool) -> list[dict[str, Any]]:
    """``load_monotony`` for a finished week whose (rounded) monotony is above ``MONOTONY_LIMIT``."""
    monotony = section["monotony"]
    if final and monotony is not None and monotony > MONOTONY_LIMIT:
        return [
            {
                "flag": "load_monotony",
                "monotony": monotony,
                "limit": MONOTONY_LIMIT,
                "strain": section["strain"],
                "week": section["total"]["load"],
            }
        ]
    return []
