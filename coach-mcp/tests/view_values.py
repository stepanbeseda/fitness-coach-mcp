"""View values for the tests of the history readers (trends, correlations, goals, strength progress).

``FakeHistory`` is a ``HistoryReader`` over values a test adds with its builder methods; those tests never seed the
store's history tables. How stored rows become these values is tested once, in ``test_history_view.py``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from coach_mcp.history_view import (
    NUMERIC_COLUMNS,
    UNKNOWN_GROUP,
    Coverage,
    DayStrength,
    HistoryDay,
    PredictionDay,
    StrengthHistory,
)
from coach_mcp.store import RACE_COLUMNS


def sport(count: int = 1, duration_min: float = 0, distance_km: float = 0, training_load: float = 0) -> dict[str, Any]:
    """One sport entry of a day's activity totals."""
    return {"count": count, "duration_min": duration_min, "distance_km": distance_km, "training_load": training_load}


def best(weight_kg: float, reps: int, e1rm_kg: float) -> dict[str, Any]:
    return {"weight_kg": weight_kg, "reps": reps, "e1rm_kg": e1rm_kg}


def _in(day: date, start: date | None, end: date) -> bool:
    return (start is None or start <= day) and day <= end


class FakeHistory:
    """A ``HistoryReader`` of view values; every builder method adds to (or replaces part of) one day."""

    def __init__(self) -> None:
        self._recovery: dict[date, tuple[date | None, dict[str, Any]]] = {}
        self._activities: dict[date, tuple[date, dict[str, Any] | None, bool]] = {}
        self._rows: set[date] = set()
        self._strength: dict[date, DayStrength] = {}
        self._groups: dict[str, str] = {}
        self._predictions: dict[date, dict[str, float | None]] = {}
        self.coverage: Coverage | None = None
        self.calls: list[tuple[str, date | None, date]] = []

    # ------------------------------------------------------------ Garmin days

    def recovery(self, day: date, *, fetched_on: date | None = None, **values: Any) -> None:
        """Recovery values of ``day`` (columns of ``NUMERIC_COLUMNS`` and ``hrv_status``), fetched the day after."""
        unknown = set(values) - {*NUMERIC_COLUMNS, "hrv_status"}
        assert not unknown, unknown
        known = self._recovery.get(day, (None, {}))[1]
        self._recovery[day] = (fetched_on or day + timedelta(days=1), {**known, **values})
        self._rows.add(day)

    def activities(
        self, day: date, totals: dict[str, Any], *, fetched_on: date | None = None, settled: bool = False
    ) -> None:
        """The activity totals of ``day`` as the view returns them (``{}``: fetched, nothing recorded)."""
        self._activities[day] = (fetched_on or day + timedelta(days=1), totals, settled)
        self._rows.add(day)

    def failures_only(self, day: date) -> None:
        """A stored row that only counts failures: no fetch date, no values."""
        self._rows.add(day)

    def days(self, start: date | None, end: date) -> list[HistoryDay]:
        self.calls.append(("days", start, end))
        out = []
        for day in sorted(d for d in self._rows if _in(d, start, end)):
            recovered_on, values = self._recovery.get(day, (None, {}))
            fetched_on, totals, settled = self._activities.get(day, (None, None, False))
            out.append(
                HistoryDay(
                    date=day,
                    recovery_fetched_on=recovered_on,
                    activities_fetched_on=fetched_on,
                    values={column: values.get(column) for column in NUMERIC_COLUMNS},
                    hrv_status=values.get("hrv_status"),
                    activities=totals,
                    activities_settled=settled if totals is not None else False,
                )
            )
        return out

    # ------------------------------------------------------------ Hevy strength

    def cover(self, first: date, through: date) -> None:
        """The Hevy history covers ``first`` .. ``through`` (the day before the last complete listing)."""
        self.coverage = Coverage(first, through)

    def lift(
        self,
        day: date,
        tid: str,
        sets: int,
        volume_kg: float,
        best_set: dict[str, Any] | None = None,
        *,
        group: str = UNKNOWN_GROUP,
        title: str | None = None,
    ) -> None:
        """One workout on ``day`` with one exercise, as the view sums it (an assisted exercise: volume 0, no best)."""
        self.workout(day, (tid, sets, volume_kg, best_set, group, title))

    def workout(self, day: date, *exercises: tuple[Any, ...]) -> None:
        """One workout; each exercise is ``(tid, sets, volume_kg, best, group, title)`` with an optional 7th element,
        the RPE tuple ``(rpe_avg, rpe_max, rpe_sets)`` of ``DayStrength.add``."""
        total = self._strength.setdefault(day, DayStrength())
        total.workouts += 1
        for tid, sets, volume_kg, best_set, group, title, *rest in exercises:
            self._groups[tid] = group
            total.add(
                tid, group=group, title=title, sets=sets, volume_kg=volume_kg, best=best_set, rpe=rest[0] if rest else None
            )

    def strength(self, start: date | None, end: date) -> StrengthHistory:
        self.calls.append(("strength", start, end))
        days = {day: total for day, total in self._strength.items() if _in(day, start, end)}
        groups = {tid: self._groups[tid] for total in days.values() for tid in total.exercises}
        return StrengthHistory(self.coverage, days, groups)

    # ------------------------------------------------------------ race predictions

    def prediction(self, day: date, **values: float | None) -> None:
        assert set(values) <= set(RACE_COLUMNS), values
        self._predictions[day] = {**self._predictions.get(day, {}), **values}

    def predictions(self, start: date | None, end: date) -> list[PredictionDay]:
        self.calls.append(("predictions", start, end))
        return [
            PredictionDay(day, {column: values.get(column) for column in RACE_COLUMNS})
            for day, values in sorted(self._predictions.items())
            if _in(day, start, end)
        ]


# ------------------------------------------------------------ the live series of PR 37

LIVE_TODAY = date(2026, 11, 13)
LIVE_STRENGTH: list[tuple[str, str, str, list[tuple[str, float, int, float]]]] = [
    # (template_id, title, muscle group, [(date, weight_kg, reps, e1rm_kg)])  -- live e1RM series, read 2026-11-13
    ("LP", "Lat Pulldown (Cable)", "lats", [("2026-09-29", 100.0, 5, 78.0), ("2026-10-03", 100.0, 5, 76.0), ("2026-10-10", 100.0, 5, 80.0), ("2026-10-17", 100.0, 5, 83.3), ("2026-10-29", 100.0, 5, 86.7), ("2026-11-06", 100.0, 5, 93.3)]),
    ("LR", "Lateral Raise (Dumbbell)", "shoulders", [("2026-10-03", 100.0, 5, 20.0), ("2026-10-10", 100.0, 5, 20.0), ("2026-10-17", 100.0, 5, 20.5), ("2026-11-02", 100.0, 5, 20.0), ("2026-11-06", 100.0, 5, 21.0)]),
    ("SC", "Seated Incline Curl (Dumbbell)", "biceps", [("2026-10-03", 100.0, 5, 40.0), ("2026-10-10", 100.0, 5, 40.0), ("2026-10-17", 100.0, 5, 46.7), ("2026-10-29", 100.0, 5, 46.7), ("2026-11-06", 100.0, 5, 40.0)]),
    ("TP", "Triceps Pushdown", "triceps", [("2026-10-03", 45.0, 12, 63.0), ("2026-10-10", 100.0, 5, 44.3), ("2026-10-17", 100.0, 5, 40.0), ("2026-10-29", 100.0, 5, 42.0), ("2026-11-06", 100.0, 5, 52.0)]),
    ("CR", "Chest Supported Incline Row (Dumbbell)", "upper_back", [("2026-10-10", 100.0, 5, 53.3), ("2026-10-17", 100.0, 5, 53.3), ("2026-10-29", 100.0, 5, 56.0), ("2026-11-06", 100.0, 5, 54.7)]),
    ("IB", "Incline Bench Press (Dumbbell)", "chest", [("2026-09-29", 25.0, 10, 33.3), ("2026-10-03", 50.0, 8, 63.3), ("2026-10-10", 40.0, 12, 56.0), ("2026-10-17", 40.0, 12, 56.0)]),
    ("BP", "Bench Press (Barbell)", "chest", [("2026-10-29", 100.0, 5, 88.7), ("2026-11-06", 100.0, 5, 96.0)]),
    ("BS", "Bulgarian Split Squat (Dumbbell)", "quadriceps", [("2026-10-31", 100.0, 5, 44.3), ("2026-11-08", 100.0, 5, 39.0)]),
    ("PP", "Cable Core Pallof Press", "abdominals", [("2026-10-31", 100.0, 5, 26.7), ("2026-11-08", 100.0, 5, 26.7)]),
    ("PT", "Cable Pull Through", "glutes", [("2026-10-31", 100.0, 5, 42.0), ("2026-11-08", 100.0, 5, 40.0)]),
    ("FP", "Face Pull", "shoulders", [("2026-11-02", 100.0, 5, 50.7), ("2026-11-06", 100.0, 5, 42.0)]),
    ("GS", "Goblet Squat", "quadriceps", [("2026-10-31", 100.0, 5, 33.3), ("2026-11-08", 100.0, 5, 33.3)]),
    ("HT", "Hip Thrust (Barbell)", "glutes", [("2026-10-31", 60.0, 10, 80.0), ("2026-11-08", 90.0, 8, 114.0)]),
    ("SP", "Shoulder Press (Dumbbell)", "shoulders", [("2026-10-29", 100.0, 5, 53.3), ("2026-11-06", 100.0, 5, 56.0)]),
    ("CF", "Single Leg Standing Calf Raise (Dumbbell)", "calves", [("2026-10-31", 17.5, 12, 24.5), ("2026-11-08", 25.0, 12, 35.0)]),
]


def seed_live_strength(history: FakeHistory) -> None:
    """The live series (one workout per point, 3 sets of 1500 kg) and the live coverage 2026-07-11 .. 2026-11-11."""
    for tid, title, group, points in LIVE_STRENGTH:
        for day, weight, reps, estimate in points:
            history.lift(date.fromisoformat(day), tid, 3, 1500.0, best(weight, reps, estimate), group=group, title=title)
    history.cover(date(2026, 7, 11), date(2026, 11, 11))

W45_DAY = date(2026, 11, 9)  # the Monday after ISO week 2026-W45 (2-8 Nov)
W45_END = W45_DAY - timedelta(days=1)
W45_GOAL = {"id": 1, "title": "Run a marathon", "metric": "race_marathon", "target_value": 14399.0,
            "target_date": "2027-10-03", "status": "active"}
W45_SETS = (20, 22, 23, 22, 77)  # Hevy working sets of the five weeks up to W45, one workout per week


def w45_acwr(offset: int, *, quiet: bool, acwr: float | None, acwr_days: int | None) -> float | None:
    """The stored ACWR ``offset`` days before the end of the window (None: not stored)."""
    if acwr_days is not None:
        return (0.5 if acwr is None else acwr) if offset < acwr_days else None
    if offset == 3:  # one missing day, as on the owner's 2026-11-05
        return None
    if quiet:
        return 1.0
    if acwr is not None:
        return acwr
    return 0.4 if offset == 0 else 0.5 if offset <= 2 else 0.7


def w45_history(*, end: date = W45_END, quiet: bool = False, acwr: float | None = None, acwr_days: int | None = None,
                window_km: float = 0.0, base_km: float = 10.2, sets: tuple[int, ...] = W45_SETS,
                unfetched: tuple[date, ...] = (), cover: tuple[date, date] | None = None) -> FakeHistory:
    """W45 of the coach review: 42 days up to ``end``; one run of ``base_km`` a week before the 14-day window and
    ``window_km`` on its last day (quiet: one 10.2 km run a week also in the window, ACWR 1.0, 22 sets in the last
    week); one strength workout a week, 4 days before ``end`` and every 7 days before."""
    h = FakeHistory()
    for offset in range(41, -1, -1):
        day = end - timedelta(days=offset)
        weekly = offset % 7 == 4
        if day not in unfetched:
            if offset < 14:
                km = (10.2 if weekly else 0.0) if quiet else (window_km if offset == 0 else 0.0)
            else:
                km = base_km if weekly else 0.0
            h.activities(day, {"running": sport(distance_km=km)} if km else {})
        if offset < 14 and (value := w45_acwr(offset, quiet=quiet, acwr=acwr, acwr_days=acwr_days)) is not None:
            h.recovery(day, acwr_ratio=value)
    h.cover(*(cover or (end - timedelta(days=68), end)))
    weeks = sets[:-1] + (22,) if quiet else sets
    for k, count in enumerate(reversed(weeks)):
        h.lift(end - timedelta(days=4 + 7 * k), "T1", count, count * 100.0)
    return h


# The alert lines (after the heading) that the W45 signals render in each language; literal, never from the catalogue.
W45_LINES = {
    "en": (
        '- Endurance training is fading while you work toward "Run a marathon" (3:59:59, by 3 Oct 2027): '
        "ACWR below 0.8 on all 13 stored days of the last 14 (latest 0.40); "
        "running 0 km in 14 days against the usual 20.4 km.",
        "- Strength load spike: 77 working sets in the last 7 days, 3.5\u00d7 the average of 21.8 over the previous 4 weeks.",
    ),
    "cs": (
        "- Vytrvalostní trénink upadá, přestože máš cíl \u201eRun a marathon\u201c (3:59:59, do 3. 10. 2027): "
        "ACWR pod 0,8 ve všech 13 uložených dnech z posledních 14 (naposledy 0,40); "
        "běh 0 km za 14 dní proti obvyklým 20,4 km.",
        "- Skok v posilování: pracovní série za posledních 7 dní 77, 3,5\u00d7 průměr 21,8 z předchozích 4 týdnů.",
    ),
}
