"""Strength history from Hevy: pure rules for summarising workouts, reading workout events and evaluating
event pages (used by the history job), and the weekly sums and ``get_strength_progress`` over the strength days
of the history view (``history_view``, which sums the stored summaries); no clock, no network; raw sets are
never stored. A day whose best e1RM jumps far from its neighbours is marked suspect (a likely entry error) and
counts neither for a best nor for the progress summary.
"""

from __future__ import annotations

import json
from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as dt_time, timedelta
from statistics import median
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .clock import local_at, local_day
from .history_view import OVERLAP, Coverage, DayStrength, HistoryReader, HistoryView, better, history_range, sync_dates
from .store import Store
from .values import finite_float, valid_id, whole

EVENTS_PAGE_SIZE = 10  # hevy-mcp's maximum page_size
MAX_EVENT_PAGES = 200
MAX_E1RM_REPS = 12  # Epley is not used above 12 reps
MAX_TITLE = 200
SUSPECT_UP_PCT = 35.0  # a day this far above the median of its neighbours is suspect
SUSPECT_DOWN_PCT = 30.0
SUSPECT_WINDOW = 3  # sessions in the reference median
SUSPECT_MIN_FOLLOWING = 2  # the first session is judged only against this many later ones

# ------------------------------------------------------------ write side: summaries


def e1rm(weight_kg: float, reps: int) -> float | None:
    """Epley estimated one-rep max (unrounded); None unless weight > 0 and 1 <= reps <= ``MAX_E1RM_REPS``."""
    if not weight_kg > 0 or not 1 <= reps <= MAX_E1RM_REPS:
        return None
    return weight_kg if reps == 1 else weight_kg * (1 + reps / 30)


class _Acc:
    """Running totals of one template inside one workout."""

    def __init__(self) -> None:
        self.sets = 0
        self.volume = 0.0
        self.title: str | None = None
        self.best: tuple[float, float, int] | None = None  # (e1rm, weight, reps)

    def add_set(self, raw: Mapping[str, Any]) -> None:
        self.sets += 1
        weight, reps = finite_float(raw.get("weight_kg")), whole(raw.get("reps"))
        if weight is not None and weight > 0 and reps is not None and reps >= 1:
            self.volume += weight * reps
        if weight is None or reps is None:
            return
        estimate = e1rm(weight, reps)
        # Highest e1RM; a tie goes to the heavier set, then to the first one seen.
        if estimate is not None and (self.best is None or (estimate, weight) > self.best[:2]):
            self.best = (estimate, weight, reps)


def summarize_exercises(workout: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Per-exercise summaries of a Hevy workout, one entry per template id, in order of first appearance.

    Warm-up sets are skipped; every other set (also dropset, failure, missing or unknown type) is a working
    set. Volume is the sum of weight x reps over working sets that have both; the best set has the highest
    Epley e1RM. An exercise with only warm-ups, or without a valid template id, is dropped.
    """
    exercises = workout.get("exercises")
    if not isinstance(exercises, list):
        return []
    accs: dict[str, _Acc] = {}
    for exercise in exercises:
        if not isinstance(exercise, dict) or not valid_id(exercise.get("exercise_template_id")):
            continue
        acc = accs.setdefault(exercise["exercise_template_id"], _Acc())
        title = exercise.get("title")
        if acc.title is None and isinstance(title, str) and title.strip():
            acc.title = title.strip()[:MAX_TITLE]
        sets = exercise.get("sets")
        for raw in sets if isinstance(sets, list) else []:
            if isinstance(raw, dict) and raw.get("type") != "warmup":
                acc.add_set(raw)
    out: list[dict[str, Any]] = []
    for template_id, acc in accs.items():
        if acc.sets == 0:
            continue
        best = None
        if acc.best is not None:
            estimate, weight, reps = acc.best
            best = {"weight_kg": round(weight, 2), "reps": reps, "e1rm_kg": round(estimate, 1)}
        out.append(
            {
                "template_id": template_id,
                "title": acc.title,
                "sets": acc.sets,
                "volume_kg": round(acc.volume, 1),
                "best": best,
            }
        )
    return out


def exercises_json(entries: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(entries, sort_keys=True, separators=(",", ":"))


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except (OverflowError, ValueError):
        return None


def workout_day(start_time: Any, zone: ZoneInfo) -> date | None:
    """Local date of a Hevy ``start_time`` (ISO text; a time without offset counts as UTC); None if unparsable."""
    instant = _instant(start_time)
    if instant is None:
        return None
    try:
        return local_day(instant, zone)
    except (OverflowError, ValueError):  # an offset at the edge of the calendar: one bad event is a skip
        return None


def updated_key(value: Any) -> str | None:
    """A fixed-width UTC text of an ``updated_at`` (text order = time order); None when unparsable."""
    instant = _instant(value)
    if instant is None:
        return None
    try:
        return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except (OverflowError, ValueError):
        return None


@dataclass
class EventBatch:
    updates: dict[str, tuple[date, str | None, str]]  # id -> (day, updated key, exercises JSON)
    deletes: list[str]
    skipped: int


def parse_events(events: Sequence[Any], zone: ZoneInfo) -> EventBatch:
    """Updated workouts and deleted ids of one events page; anything unusable is counted as skipped.

    A delete is terminal: an id deleted anywhere in the batch is not updated. Of two updates of one id the
    one with the later ``updated_at`` wins when both have one, otherwise the first seen stays.
    """
    updates: dict[str, tuple[date, str | None, str]] = {}
    deletes: list[str] = []
    skipped = 0
    for event in events:
        kind = event.get("type") if isinstance(event, dict) else None
        if kind == "deleted" and valid_id(event.get("id")):
            if event["id"] not in deletes:
                deletes.append(event["id"])
            continue
        workout = event.get("workout") if kind == "updated" else None
        day = workout_day(workout.get("start_time"), zone) if isinstance(workout, dict) else None
        if not isinstance(workout, dict) or not valid_id(workout.get("id")) or day is None:
            skipped += 1
            continue
        candidate = (day, updated_key(workout.get("updated_at")), exercises_json(summarize_exercises(workout)))
        known = updates.get(workout["id"])
        if known is None or (known[1] is not None and candidate[1] is not None and candidate[1] > known[1]):
            updates[workout["id"]] = candidate
    for workout_id in deletes:
        updates.pop(workout_id, None)
    return EventBatch(updates, deletes, skipped)


def page_step(payload: Mapping[str, Any], page: int) -> Literal["next", "complete", "failed"]:
    """What to do after the events page ``page`` (see the table in the PR 09 spec, 4.3)."""
    events = payload.get("events")
    if not isinstance(events, list):
        return "failed"
    if "page" in payload and (isinstance(payload["page"], bool) or payload["page"] != page):
        return "failed"
    if "page_count" in payload:
        count = payload["page_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return "failed"
        if page >= count:
            return "complete"
        return "next" if events else "failed"
    return "complete" if len(events) < EVENTS_PAGE_SIZE else "next"


# ------------------------------------------------------------- write side: sync window


def listing_due(state: Mapping[str, Any] | None, first: date, today: date) -> bool:
    """A listing is needed: never completed, the range grew (first < covered_from), or not yet today."""
    dates = sync_dates(state)
    if dates is None:
        return True
    covered_from, synced_on, _ = dates
    return first < covered_from or synced_on < today


def listing_since(state: Mapping[str, Any] | None, first: date, zone: ZoneInfo) -> tuple[datetime, bool]:
    """(since, full): local midnight of ``first`` (as UTC) when never completed or the range grew; otherwise the
    stored ``synced_at`` minus ``OVERLAP``."""
    dates = sync_dates(state)
    if dates is None or first < dates[0]:
        return local_at(first, dt_time(0), zone).astimezone(UTC), True
    return dates[2].astimezone(UTC) - OVERLAP, False


def since_text(instant: datetime) -> str:
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------ suspect records


def suspect_changes(values: Sequence[float]) -> dict[int, tuple[float, float]]:
    """Index -> (reference e1RM, change %) of the suspect points of one exercise's day values, oldest first."""
    out: dict[int, tuple[float, float]] = {}
    for i, value in enumerate(values):
        if i > 0:
            reference = median(values[max(0, i - SUSPECT_WINDOW) : i])
        elif len(values) > SUSPECT_MIN_FOLLOWING:
            reference = median(values[1 : 1 + SUSPECT_WINDOW])
        else:
            continue
        if not reference > 0:
            continue
        pct = round((value - reference) * 100 / reference, 1)
        if pct > SUSPECT_UP_PCT or pct < -SUSPECT_DOWN_PCT:
            out[i] = (reference, pct)
    return out


@dataclass(frozen=True)
class SuspectDay:
    """One exercise-day whose best e1RM is far from its neighbours."""

    template_id: str
    title: str | None
    day: date
    weight_kg: float
    reps: int
    e1rm_kg: float
    reference: float
    change_pct: float

    def item(self) -> dict[str, Any]:
        return {
            "exercise": self.title,
            "template_id": self.template_id,
            "date": self.day.isoformat(),
            "weight_kg": self.weight_kg,
            "reps": self.reps,
            "e1rm_kg": self.e1rm_kg,
            "reference_e1rm_kg": round(self.reference, 1),
            "change_pct": self.change_pct,
        }

    def flag(self) -> dict[str, Any]:
        return {
            "flag": "strength_record_suspect",
            "week": self.e1rm_kg,
            "baseline": round(self.reference, 1),
            "change": round(self.e1rm_kg - self.reference, 1),
            "change_pct": self.change_pct,
            "exercise": self.title,
            "template_id": self.template_id,
            "date": self.day.isoformat(),
            "weight_kg": self.weight_kg,
            "reps": self.reps,
        }


def classify_suspects(totals: Mapping[date, DayStrength]) -> dict[tuple[str, date], SuspectDay]:
    """The suspect days of every exercise in ``totals``, keyed by (template id, day)."""
    per_template: dict[str, list[tuple[date, dict[str, Any], str | None]]] = {}
    for day in sorted(totals):
        for tid, entry in totals[day].exercises.items():
            if entry["best"] is not None:
                per_template.setdefault(tid, []).append((day, entry["best"], entry["title"]))
    out: dict[tuple[str, date], SuspectDay] = {}
    for tid, days in per_template.items():
        for i, (reference, pct) in suspect_changes([best["e1rm_kg"] for _, best, _ in days]).items():
            day, best, title = days[i]
            out[(tid, day)] = SuspectDay(tid, title, day, best["weight_kg"], best["reps"], best["e1rm_kg"], reference, pct)
    return out


def suspect_days(history: HistoryReader) -> dict[tuple[str, date], SuspectDay]:
    """The suspect days of the whole stored series, independent of any requested range."""
    return classify_suspects(history.strength(None, date.max).days)


def check_order(suspects: Iterable[SuspectDay]) -> list[SuspectDay]:
    """Newest day first, then exercise title, then template id."""
    return sorted(suspects, key=lambda s: (-s.day.toordinal(), s.title or "", s.template_id))


# --------------------------------------------------------------- read side: weeks


class StrengthWeek:
    """The covered days of one ISO week and their sums."""

    def __init__(
        self,
        monday: date,
        totals: Mapping[date, DayStrength],
        cov: Coverage,
        *,
        exclude: Container[tuple[str, date]] = frozenset(),
    ) -> None:
        """``exclude``: (template id, day) pairs left out of ``best`` only; every sum counts them."""
        self.monday = monday
        self.days = 0
        self.workouts = 0
        self.sets = 0
        self.volume_kg = 0.0
        self.muscle_sets: dict[str, int] = {}
        self.muscle_volume: dict[str, float] = {}
        self.best: dict[str, tuple[dict[str, Any], date, str | None]] = {}
        for n in range(7):
            day = monday + timedelta(days=n)
            if not cov.covers(day):
                continue
            self.days += 1
            total = totals.get(day)
            if total is None:
                continue
            self.workouts += total.workouts
            self.sets += total.sets
            self.volume_kg += total.volume_kg
            for group, count in total.muscle_sets.items():
                self.muscle_sets[group] = self.muscle_sets.get(group, 0) + count
            for group, volume in total.muscle_volume.items():
                self.muscle_volume[group] = self.muscle_volume.get(group, 0.0) + volume
            for tid, entry in total.exercises.items():
                if (tid, day) in exclude:
                    continue
                known = self.best.get(tid)
                if entry["best"] is not None and better(entry["best"], known[0] if known else None):
                    self.best[tid] = (entry["best"], day, entry["title"])

    @property
    def complete(self) -> bool:
        return self.days == 7


# -------------------------------------------------------------- get_strength_progress


def _point(day: date, best: Mapping[str, Any], suspect: bool) -> dict[str, Any]:
    point: dict[str, Any] = {
        "date": day.isoformat(),
        "e1rm_kg": best["e1rm_kg"],
        "weight_kg": best["weight_kg"],
        "reps": best["reps"],
    }
    if suspect:
        point["suspect"] = True
    return point


def _exercise_progress(
    tid: str, title: str | None, group: str, points: list[dict[str, Any]], end: date, plateau_weeks: int
) -> dict[str, Any]:
    """The summary values come from the points that are not suspect; ``sessions`` and ``e1rm_kg`` keep all."""
    valid = [p for p in points if not p.get("suspect")]
    summary: dict[str, Any] = {
        "best": None,
        "first_e1rm_kg": None,
        "last_e1rm_kg": None,
        "change_kg": None,
        "change_pct": None,
        "weeks_since_best": None,
        "plateau": False,
    }
    if valid:
        best = valid[0]
        for point in valid:
            if point["e1rm_kg"] > best["e1rm_kg"]:
                best = point
        first, last = valid[0]["e1rm_kg"], valid[-1]["e1rm_kg"]
        change = last - first
        window_start = end - timedelta(days=7 * plateau_weeks - 1)
        inside = [p["e1rm_kg"] for p in valid if date.fromisoformat(p["date"]) >= window_start]
        before = [p["e1rm_kg"] for p in valid if date.fromisoformat(p["date"]) < window_start]
        summary = {
            "best": best,
            "first_e1rm_kg": first,
            "last_e1rm_kg": last,
            "change_kg": round(change, 1),
            "change_pct": round(change * 100 / first, 1) if len(valid) > 1 and first != 0 else None,
            "weeks_since_best": (end - date.fromisoformat(best["date"])).days // 7,
            "plateau": len(inside) >= 2 and bool(before) and max(inside) <= max(before),
        }
    return {
        "template_id": tid,
        "title": title,
        "muscle_group": group,
        "sessions": len(points),
        "e1rm_kg": points,
        **summary,
    }


def get_strength_progress(
    store: Store,
    today: date,
    *,
    exercise: str | None,
    start_date: str | None,
    end_date: str | None,
    plateau_weeks: int,
    limit: int,
    history: HistoryReader | None = None,
) -> dict[str, Any]:
    start, end = history_range(today, start_date, end_date)
    view = history if history is not None else HistoryView(store)
    hist = view.strength(start, end)
    cov = hist.coverage
    if cov is None:
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "plateau_weeks": plateau_weeks,
            "available": False,
            "reason": "strength history from Hevy has not been collected yet",
            "coverage": None,
            "exercises": [],
            "to_check": [],
        }
    suspects = suspect_days(view)
    totals = hist.days
    series: dict[str, list[dict[str, Any]]] = {}
    titles: dict[str, str | None] = {}
    for day in sorted(totals):
        for tid, entry in totals[day].exercises.items():
            if titles.get(tid) is None:
                titles[tid] = entry["title"]
            if entry["best"] is not None:
                series.setdefault(tid, []).append(_point(day, entry["best"], (tid, day) in suspects))
    wanted = (exercise or "").strip()
    if wanted:
        series = {
            tid: points
            for tid, points in series.items()
            if tid == wanted or wanted.casefold() in (titles.get(tid) or "").casefold()
        }
    ordered = sorted(series, key=lambda tid: (-len(series[tid]), titles.get(tid) or "", tid))
    to_check = check_order(s for (tid, day), s in suspects.items() if tid in series and start <= day <= end)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "plateau_weeks": plateau_weeks,
        "available": True,
        "coverage": {"from": cov.first.isoformat(), "through": cov.through.isoformat()},
        "matched": len(ordered),
        "exercises": [
            _exercise_progress(tid, titles.get(tid), hist.group(tid), series[tid], end, plateau_weeks)
            for tid in ordered[:limit]
        ],
        "to_check": [s.item() for s in to_check],
    }
