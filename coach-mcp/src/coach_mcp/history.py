"""Nightly history of Garmin daily values: the snapshot job and its pure helpers.

One row per local day in ``history_days``: recovery values (HRV, sleep, resting heart rate, Body Battery,
stress, load, VO2max) from ``get_recovery_snapshot`` and per-sport activity totals from ``list_activities``.
Progress is the stored state, not a cursor: ``needs_fetch`` decides from the dates and failure counters in
the store which days still need a call, so a restart resumes where the last run stopped. The clock and the
sleep function are injected. Log lines carry outcome words, counters and exception class names only, never
dates, values, payload text or arguments. After every run the job stores a suggestion (matched / no_activity)
for the planned sessions of the last seven days (``reconcile``, over the activity days of ``history_view``); it
never changes a session status. The re-fetch window (``REFETCH_DAYS``, ``MAX_FAILURES``) is defined in
``history_view``, whose settled flag is its inverse.
A day whose snapshot had sections in error keeps them in ``recovery_missing``; after the re-fetch window such a
day is fetched again, a few per night, and only its missing sections are written (``repair_sections``).

With a predictions source (``create_app`` passes the Garmin reader) the run then asks Garmin once for the daily
race predictions of the missing range and stores them in ``race_predictions`` (PR 14); a failed call changes no
state, so the next night asks for the whole missed range again.

With a strength source the same run then syncs Hevy workouts (``strength.py``): events since the last
complete listing, exercise templates once; the Garmin part never depends on it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from . import strength
from .clock import LocalClock
from .config import Settings
from .garmin import HistorySource, exception_kind
from .hevy import StrengthSource
from .history_view import MAX_FAILURES, REFETCH_DAYS, HistoryView
from .reconcile import RECONCILE_DAYS, suggest
from .store import Store
from .values import finite

log = logging.getLogger("coach_mcp.history")

STARTUP_DELAY_SECONDS = 60.0
RETRY_SECONDS = 3600.0
RATE_LIMIT_BACKOFF_SECONDS = 3600.0
MAX_BACKOFF_SECONDS = 28800.0
MAX_SLEEP_SECONDS = 3600.0
REPAIR_ATTEMPTS = 3  # answered repair calls that are charged to a day
REPAIR_DAYS_PER_NIGHT = 5  # answered repair calls per local date
OUTAGE_ERRORS = 3
BLOCK_DAYS = 7
ACTIVITY_LIMIT = 100

# The sections of a get_recovery_snapshot payload that the history keeps.
SNAPSHOT_SECTIONS: tuple[str, ...] = (
    "readiness",
    "hrv",
    "sleep",
    "body_battery",
    "resting_hr",
    "stress",
    "training_status",
)
# Section key -> (column, field of the section as the garmin-mcp shaper returns it).
_SECTION_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "readiness": (("readiness", "score"),),  # morning_score wins when numeric, see recovery_values
    "hrv": (
        ("hrv_last_night_ms", "last_night_avg_ms"),
        ("hrv_weekly_avg_ms", "weekly_avg_ms"),
        ("hrv_status", "status"),
    ),
    "sleep": (("sleep_duration_h", "duration_h"), ("sleep_score", "score")),
    "body_battery": (("body_battery_high", "high"), ("body_battery_low", "low")),
    "resting_hr": (("resting_hr", "today"),),
    "stress": (("stress_avg", "avg"),),
    "training_status": (
        ("acute_load", "acute"),
        ("chronic_load", "chronic"),
        ("acwr_ratio", "acwr_ratio"),
        ("vo2max_running", "vo2max_running"),
        ("vo2max_cycling", "vo2max_cycling"),
    ),
}
ALL_SECTIONS = frozenset(SNAPSHOT_SECTIONS)
# Column -> key of its section.
_SECTION_OF: dict[str, str] = {column: key for key, columns in _SECTION_COLUMNS.items() for column, _ in columns}
SPORTS = ("running", "cycling", "swimming", "walking", "hiking", "strength", "other")
# Everything except Garmin's own strength_training, which is reported separately.
ENDURANCE_SPORTS = ("running", "cycling", "swimming", "walking", "hiking", "other")


def recovery_values(payload: dict[str, Any]) -> dict[str, Any]:
    """Columns of ``history_days`` from a ``get_recovery_snapshot`` payload.

    A section that holds ``error`` leaves its columns out (the stored value is kept); a section that is
    missing, ``{"available": false}`` or not an object gives ``None`` for all its columns.
    """
    out: dict[str, Any] = {}
    for key, columns in _SECTION_COLUMNS.items():
        section = payload.get(key)
        if isinstance(section, dict) and "error" in section:
            continue
        if not isinstance(section, dict):
            section = {}
        for column, field in columns:
            value = section.get(field)
            if column == "hrv_status":
                out[column] = value if isinstance(value, str) and 1 <= len(value) <= 40 else None
            else:
                out[column] = finite(value)
        if key == "readiness":
            morning = finite(section.get("morning_score"))
            if morning is not None:
                out["readiness"] = morning
    return out


def snapshot_failed(payload: dict[str, Any]) -> bool:
    """True when every section of the snapshot is an error (the call itself worked, nothing was read)."""
    return all(isinstance(payload.get(key), dict) and "error" in payload[key] for key in SNAPSHOT_SECTIONS)


def section_errors(payload: dict[str, Any]) -> frozenset[str]:
    """The sections of a snapshot payload that hold ``error`` (the test of ``recovery_values``)."""
    return frozenset(
        key for key in SNAPSHOT_SECTIONS if isinstance(payload.get(key), dict) and "error" in payload[key]
    )


def missing_text(sections: Collection[str]) -> str:
    """The stored form of a set of sections: comma list in the order of ``SNAPSHOT_SECTIONS``."""
    return ",".join(key for key in SNAPSHOT_SECTIONS if key in sections)


def sport_of(type_key: str | None) -> str:
    """Sport group of a Garmin activity ``typeKey``; the first matching rule wins."""
    key = type_key or ""
    if key == "strength_training":
        return "strength"
    if "run" in key:
        return "running"
    if "cycl" in key or "bik" in key or "ride" in key:
        return "cycling"
    if "swim" in key:
        return "swimming"
    if "walk" in key:
        return "walking"
    if "hik" in key:
        return "hiking"
    return "other"


def activity_totals(payload: dict[str, Any], days: Sequence[date]) -> dict[date, dict[str, dict[str, float | int]]]:
    """Per day (every day of ``days`` gets an entry) and sport: count, duration_min, distance_km, training_load.

    A row counts for the local date of its ``start_local``; rows without a parsable date or outside ``days``
    are ignored; a missing or non-numeric value counts as 0.
    """
    wanted = set(days)
    sums: dict[date, dict[str, dict[str, float]]] = {day: {} for day in days}
    for row in payload.get("activities") or []:
        if not isinstance(row, dict):
            continue
        try:
            day = date.fromisoformat(str(row["start_local"])[:10])
        except (KeyError, ValueError):
            continue
        if day not in wanted:
            continue
        total = sums[day].setdefault(
            sport_of(row.get("type")), {"count": 0, "duration_min": 0.0, "distance_km": 0.0, "training_load": 0.0}
        )
        total["count"] += 1
        for field in ("duration_min", "distance_km", "training_load"):
            total[field] += finite(row.get(field)) or 0
    return {
        day: {
            sport: {
                "count": int(t["count"]),
                "duration_min": round(t["duration_min"], 1),
                "distance_km": round(t["distance_km"], 2),
                "training_load": round(t["training_load"], 1),
            }
            for sport, t in by_sport.items()
        }
        for day, by_sport in sums.items()
    }


def totals_json(totals: dict[str, dict[str, float | int]]) -> str:
    return json.dumps(totals, sort_keys=True, separators=(",", ":"))


def needs_fetch(day: date, fetched_on: date | None, failures: int, today: date) -> bool:
    """Does one part (recovery or activities) of ``day`` need a call now?

    A day is fetched on D+1, again on D+2 and D+3 (late watch syncs), never more than once per local date;
    a day that never succeeded is given up after ``MAX_FAILURES`` item errors, and a day with data stops being
    re-fetched after ``MAX_FAILURES`` item errors.
    """
    if fetched_on is None:
        return failures < MAX_FAILURES
    return fetched_on < today and fetched_on < day + timedelta(days=REFETCH_DAYS) and failures < MAX_FAILURES


def _fetched_on(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def missing_sections(row: Mapping[str, Any]) -> frozenset[str]:
    """The sections of a stored day that were never read without an error.

    A day without an answered snapshot misses all of them; ``recovery_missing`` NULL on a fetched row (written
    by a caller that did not know) counts as complete; unknown tokens are ignored.
    """
    if _fetched_on(row.get("recovery_fetched_on")) is None:
        return ALL_SECTIONS
    text = row.get("recovery_missing")
    if not isinstance(text, str):
        return frozenset()
    return frozenset(token for token in text.split(",") if token in ALL_SECTIONS)


def repair_sections(row: Mapping[str, Any], day: date, today: date) -> frozenset[str]:
    """The sections of ``day`` to ask for again now; empty when the day is not due (or complete).

    Due only after the regular re-fetch window, never on the date of a regular fetch, below the failure and
    repair limits and at most once per local date. Disjoint from ``needs_fetch`` for a fetched day.
    """
    fetched_on = _fetched_on(row.get("recovery_fetched_on"))
    if fetched_on is None or fetched_on < day + timedelta(days=REFETCH_DAYS) or fetched_on >= today:
        return frozenset()
    if (row.get("recovery_failures") or 0) >= MAX_FAILURES or (row.get("recovery_repairs") or 0) >= REPAIR_ATTEMPTS:
        return frozenset()
    if row.get("recovery_repaired_on") == today.isoformat():
        return frozenset()
    return missing_sections(row)


PREDICTION_KEYS = {
    "5k_seconds": "time_5k_s",
    "10k_seconds": "time_10k_s",
    "half_marathon_seconds": "time_half_marathon_s",
    "marathon_seconds": "time_marathon_s",
}
MAX_PREDICTION_SECONDS = 86400


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def prediction_range(
    state: Mapping[str, Any] | None, first: date, last: date, today: date
) -> tuple[date, date] | None:
    """The range of race predictions to ask Garmin for, or None when nothing is due.

    Everything from ``first`` when the stored range does not reach back that far (or nothing was ever
    synced); else only the last ``REFETCH_DAYS`` days before the previous sync (late watch syncs), and not at
    all when the previous sync was today.
    """
    if state is None:
        return first, last
    covered, synced = _parse_day(state.get("covered_from")), _parse_day(state.get("synced_on"))
    if covered is None or covered > first or synced is None:
        return first, last
    if synced >= today:
        return None
    try:
        return max(first, synced - timedelta(days=REFETCH_DAYS)), last
    except OverflowError:
        return first, last


def prediction_values(
    payload: Mapping[str, Any], start: date, end: date
) -> dict[date, dict[str, float | None]] | None:
    """Columns of ``race_predictions`` per day from a ``get_race_predictions`` payload.

    None when the payload has no list of days (also the ``error`` section: nothing is stored then). A value
    that is not a finite number in (0, 86400] is NULL; a day without any valid value is left out.
    """
    days = payload.get("days")
    if not isinstance(days, list):
        return None
    out: dict[date, dict[str, float | None]] = {}
    for entry in days:
        if not isinstance(entry, dict):
            continue
        day = _parse_day(entry.get("date"))
        if day is None or not start <= day <= end:
            continue
        values: dict[str, float | None] = {}
        for key, column in PREDICTION_KEYS.items():
            value = finite(entry.get(key))
            values[column] = value if value is not None and 0 < value <= MAX_PREDICTION_SECONDS else None
        if any(v is not None for v in values.values()):
            out[day] = values
    return out


class _Stop(Exception):
    """A transient service condition ends the run; carries the run's outcome word."""

    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


_STOP_OUTCOMES = {
    "rate_limited": "rate_limited",
    "auth_error": "auth",
    "unreachable": "unreachable",
    "no_credentials": "no_credentials",
}
_WARNING_OUTCOMES = frozenset({"rate_limited", "service_error", "auth", "unreachable", "no_credentials"})
PARTS = ("recovery", "activities")


def combined_outcome(garmin: str, strength: str) -> str:
    """The run's outcome for scheduling: the Garmin outcome rules (its backoff stays as in PR 05); only when the
    Garmin part is done does the strength outcome decide the next run."""
    return garmin if garmin != "done" else strength


class _Run:
    """State of one collection run: counters plus the item errors that are not charged yet.

    Created at the start of every ``run_once`` from the stored rows, never persisted and never kept across
    runs: a restart or a crash only leaves held errors uncharged, so those days stay pending.
    """

    def __init__(self) -> None:
        self.fetched = 0
        self.failed = 0
        self.calls = 0
        self.pending: list[date] = []
        self.given_up = 0
        # Per part: the day lists of item errors not charged yet (charged once the backend is shown to work).
        self.unconfirmed: dict[str, list[list[date]]] = {part: [] for part in PARTS}
        # Per part: a day for which the backend answered this part correctly, the probe asks for it.
        self.known_good: dict[str, date | None] = {part: None for part in PARTS}
        self.repaired = 0  # days whose missing sections all loaded in this run
        # The regular snapshots of this run that were answered, and the sections read without an error in them:
        # the evidence that a section works (or is down) for the whole run.
        self.snapshots_answered = 0
        self.sections_ok: set[str] = set()


class _StrengthRun:
    def __init__(self) -> None:
        self.workouts = 0  # updated events applied
        self.deleted = 0  # delete events applied
        self.skipped = 0  # events that could not be used (no valid id, unparsable start time, unknown type)
        self.templates = 0  # templates resolved (found or not found)
        self.failed = 0  # failed listings and failed template calls


class HistoryJob:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        source: HistorySource,
        *,
        now: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        strength: StrengthSource | None = None,
        predictions: HistorySource | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._source = source
        self._strength = strength
        self._predictions = predictions
        self._now = now
        self._sleep = sleep
        self._clock = LocalClock(settings.timezone, now)
        self._due: datetime | None = None
        self._backoff = RATE_LIMIT_BACKOFF_SECONDS

    # ----------------------------------------------------------------- time

    def _in_briefing_window(self, now: datetime) -> bool:
        clock = self._clock
        day = clock.day_of(now)
        start = clock.at(day, self._settings.briefing_time)
        end = clock.at(day, self._settings.briefing_cutoff)
        return clock.until(start, now) <= 0 < clock.until(end, now)

    # ----------------------------------------------------------------- loop

    async def run(self) -> None:
        if self._settings.briefing_time <= self._settings.history_time < self._settings.briefing_cutoff:
            # Accepted: the run due at history_time is paused and starts at the cutoff. Said once per start.
            log.warning("history status=config_warning reason=time_in_briefing_window")
        while True:
            try:
                delay = await self.tick()
            except Exception as exc:  # BaseException (cancellation) propagates
                log.error("history status=crashed kind=%s", exception_kind(exc))
                delay = RETRY_SECONDS
            await self._sleep(min(max(delay, 1.0), MAX_SLEEP_SECONDS))

    async def tick(self) -> float:
        """One step of the loop; returns the seconds until the next one."""
        now = self._now()
        if self._due is None:
            self._due = now + timedelta(seconds=STARTUP_DELAY_SECONDS)
        wait = self._clock.until(self._due, now)
        if wait > 0:
            return wait
        outcome = await self.run_once()
        after = self._now()
        if outcome == "done":
            self._due = self._clock.next_at(self._settings.history_time, after)
            self._backoff = RATE_LIMIT_BACKOFF_SECONDS
        elif outcome == "paused":
            self._due = self._clock.at(self._clock.day_of(after), self._settings.briefing_cutoff)
        elif outcome in ("rate_limited", "service_error"):  # one shared backoff
            self._due = after + timedelta(seconds=self._backoff)
            self._backoff = min(self._backoff * 2, MAX_BACKOFF_SECONDS)
        else:  # partial, auth, unreachable, no_credentials: try again in an hour, at the latest at history_time
            next_run = self._clock.next_at(self._settings.history_time, after)
            self._due = min(after + timedelta(seconds=RETRY_SECONDS), next_run)
            self._backoff = RATE_LIMIT_BACKOFF_SECONDS
        return self._clock.until(self._due, after)

    # ------------------------------------------------------------------ run

    async def run_once(self) -> str:
        """One collection run, the race predictions, the Hevy part and the reconcile step; returns its outcome word.

        Logs one line for the predictions part (with a predictions source, when it had a call due), one for the
        Hevy part (with a strength source) and one for the Garmin part and the reconcile step. The Hevy part
        runs before the reconcile step, so last night's workouts are used by the matching.
        """
        started = time.monotonic()
        run = _Run()
        try:
            outcome = await self._collect(run)
        except _Stop as stop:
            outcome = stop.outcome  # the held errors of the run are dropped, not charged
        level = logging.WARNING if outcome in _WARNING_OUTCOMES else logging.INFO
        predictions_outcome: str | None = None
        predictions_crash: Exception | None = None
        predictions_seconds = 0.0
        if self._predictions is not None and outcome in ("done", "partial"):
            predictions_started = time.monotonic()
            try:
                predictions_outcome = await self._run_predictions(run)  # logs its own line when it had work
            except Exception as exc:  # BaseException (cancellation) propagates at once
                predictions_crash = exc
            predictions_seconds = time.monotonic() - predictions_started
        strength_outcome: str | None = None
        strength_crash: Exception | None = None
        strength_seconds = 0.0
        if self._strength is not None:
            strength_started = time.monotonic()
            try:
                strength_outcome = await self._run_strength(run)  # logs its own line
            except Exception as exc:  # BaseException (cancellation) propagates at once
                strength_crash = exc
            strength_seconds = time.monotonic() - strength_started
        # The step needs only the store, so it runs after every run, also after an early return or a stop.
        try:
            matched, no_activity = self._reconcile(self._clock.today())
            step = f"matched={matched} no_activity={no_activity}"
        except Exception as exc:  # the run's outcome and scheduling stand; BaseException propagates
            step = f"reconcile=error kind={exception_kind(exc)}"
            level = logging.WARNING
        log.log(
            level,
            "history status=%s fetched=%d failed=%d pending=%d given_up=%d ms=%.0f repaired=%d %s",
            outcome,
            run.fetched,
            run.failed,
            len(run.pending),
            run.given_up,
            (time.monotonic() - started - strength_seconds - predictions_seconds) * 1000,
            run.repaired,
            step,
        )
        crash = predictions_crash or strength_crash
        if crash is not None:
            raise crash  # after the reconcile step and the Garmin line: run() logs "crashed" and retries
        scheduled = outcome
        if outcome == "done" and predictions_outcome in ("rate_limited", "paused"):
            scheduled = predictions_outcome  # the only two outcomes of the predictions part that steer scheduling
        return scheduled if strength_outcome is None else combined_outcome(scheduled, strength_outcome)

    def _reconcile(self, today: date) -> tuple[int, int]:
        """Store a suggestion for every planned session of the last RECONCILE_DAYS days; changes no status.

        Returns (matched, no_activity).
        """
        first, last = today - timedelta(days=RECONCILE_DAYS), today - timedelta(days=1)
        days = HistoryView(self._store).activity_days(first, last)
        found = suggest(self._store.sessions_between(first, last), days)
        self._store.upsert_suggestions(
            [
                (
                    sid,
                    s.result,
                    s.final,
                    None if s.evidence is None else json.dumps(s.evidence, sort_keys=True, separators=(",", ":")),
                )
                for sid, s in found.items()
            ],
            today,
        )
        matched = sum(1 for s in found.values() if s.result == "matched")
        return matched, len(found) - matched

    async def _collect(self, run: _Run) -> str:
        today = self._clock.today()
        first = today - timedelta(days=self._settings.history_backfill_days)
        last = today - timedelta(days=1)
        stored = {row["date"]: row for row in self._store.history_rows(first, last)}
        need_recovery: set[date] = set()
        need_activities: set[date] = set()
        prior: dict[date, frozenset[str]] = {}  # sections never read without an error, per day to fetch
        repairs: list[tuple[date, frozenset[str]]] = []  # newest first, like the loop
        answered_today = 0
        day = last
        while day >= first:
            row = stored.get(day.isoformat(), {})
            fetched = {part: _fetched_on(row.get(f"{part}_fetched_on")) for part in PARTS}
            failures = {part: row.get(f"{part}_failures") or 0 for part in PARTS}
            if needs_fetch(day, fetched["recovery"], failures["recovery"], today):
                need_recovery.add(day)
                prior[day] = missing_sections(row)
            due = repair_sections(row, day, today)
            if due:
                repairs.append((day, due))
            if row.get("recovery_repaired_on") == today.isoformat():
                answered_today += 1
            if needs_fetch(day, fetched["activities"], failures["activities"], today):
                need_activities.add(day)
            if any(fetched[p] is None and failures[p] >= MAX_FAILURES for p in PARTS):
                run.given_up += 1
            for part in PARTS:  # newest first: the first hit is the newest day that has data
                if run.known_good[part] is None and fetched[part] is not None:
                    run.known_good[part] = day
            day -= timedelta(days=1)
        run.pending.extend(sorted(need_recovery | need_activities, reverse=True))
        budget = max(0, REPAIR_DAYS_PER_NIGHT - answered_today)
        if not run.pending and not (repairs and budget):
            return "done"
        if self._in_briefing_window(self._now()):
            return "paused"
        auth = await self._source.auth_state()
        if auth is None:
            return "unreachable"
        if auth != "ok":
            return "auth"

        for block in self._blocks(run.pending):
            act = [d for d in block if d in need_activities]
            if act:
                await self._fetch_activities(act, today, run)
            for d in block:
                if d in need_recovery:
                    await self._fetch_snapshot(d, today, run, prior[d])
        await self._repairs(repairs, budget, today, run)
        for part in PARTS:  # errors held at the end of the loop: the backend must show it works
            if run.unconfirmed[part]:
                await self._probe(part, run)
        return "partial" if run.failed else "done"

    @staticmethod
    def _blocks(pending: list[date]) -> list[list[date]]:
        """Newest-first runs of contiguous dates, at most ``BLOCK_DAYS`` each."""
        blocks: list[list[date]] = []
        for d in pending:
            if blocks and len(blocks[-1]) < BLOCK_DAYS and blocks[-1][-1] - d == timedelta(days=1):
                blocks[-1].append(d)
            else:
                blocks.append([d])
        return blocks

    async def _pace(self, run: _Run) -> None:
        """The configured delay before every backend call of the run but the first; the briefing window stops it."""
        if run.calls:
            await self._sleep(self._settings.history_call_delay_seconds)
            if self._in_briefing_window(self._now()):
                raise _Stop("paused")
        run.calls += 1

    async def _call(self, name: str, args: dict[str, Any], run: _Run) -> tuple[str, dict[str, Any] | None]:
        """One paced backend call -> ("ok", payload) or ("error", None); anything else stops the run."""
        await self._pace(run)
        outcome, payload = await self._source.history_call(name, args)
        if outcome in _STOP_OUTCOMES:
            raise _Stop(_STOP_OUTCOMES[outcome])
        if outcome == "ok" and payload is not None:
            return "ok", payload
        return "error", None

    # An item error is not charged to its days at once: during an outage of Garmin Connect (or of the server's
    # internet) every call fails the same way and every day would be given up. It is held until the backend
    # answers the same part correctly (``_confirm``); three held errors, or the end of the run, ask the backend
    # for a day that is known to work (``_probe``).

    def _charge(self, part: str, run: _Run) -> None:
        for days in run.unconfirmed[part]:
            self._store.record_history_failure(days, part)
            run.failed += 1
        run.unconfirmed[part].clear()

    def _confirm(self, part: str, good_day: date, run: _Run) -> None:
        """A call of ``part`` worked: the held errors of that part belonged to their days."""
        run.known_good[part] = good_day
        self._charge(part, run)

    async def _item_error(self, part: str, days: list[date], run: _Run) -> None:
        run.unconfirmed[part].append(days)
        if len(run.unconfirmed[part]) >= OUTAGE_ERRORS:
            await self._probe(part, run)

    async def _probe(self, part: str, run: _Run) -> None:
        """Ask for the known-good day of ``part``; success charges the held errors, failure stops the run."""
        good = run.known_good[part]
        if good is None:
            raise _Stop("service_error")
        if part == "recovery":
            outcome, payload = await self._call("get_recovery_snapshot", {"date": good.isoformat()}, run)
            works = outcome == "ok" and payload is not None and not snapshot_failed(payload)
        else:
            outcome, payload = await self._call(
                "list_activities",
                {"start_date": good.isoformat(), "end_date": good.isoformat(), "limit": ACTIVITY_LIMIT},
                run,
            )
            works = outcome == "ok" and payload is not None and isinstance(payload.get("activities"), list)
        if not works:
            raise _Stop("service_error")
        self._charge(part, run)  # the probe result itself is neither stored nor counted

    async def _fetch_activities(self, days: list[date], today: date, run: _Run) -> None:
        """One ``list_activities`` call over the days in ``days`` (a single day or a short block)."""
        lo, hi = min(days), max(days)
        outcome, payload = await self._call(
            "list_activities",
            {"start_date": lo.isoformat(), "end_date": hi.isoformat(), "limit": ACTIVITY_LIMIT},
            run,
        )
        if payload is not None and not isinstance(payload.get("activities"), list):
            outcome = "error"
        if outcome != "ok" or payload is None:
            await self._item_error("activities", days, run)
            return
        self._confirm("activities", hi, run)
        if payload.get("truncated") is True and lo != hi:
            # More activities than the limit in the range: ask day by day instead of storing a cut-off list.
            for day in days:
                await self._fetch_activities([day], today, run)
            return
        totals = activity_totals(payload, days)
        self._store.upsert_history_activities({d: totals_json(t) for d, t in totals.items()}, today)
        run.fetched += 1

    async def _fetch_snapshot(self, day: date, today: date, run: _Run, prior: frozenset[str]) -> None:
        """Fetch and store the snapshot of ``day``; ``prior`` = its sections never read without an error."""
        outcome, payload = await self._call("get_recovery_snapshot", {"date": day.isoformat()}, run)
        if outcome != "ok" or payload is None or snapshot_failed(payload):
            await self._item_error("recovery", [day], run)
            return
        errors = section_errors(payload)
        run.snapshots_answered += 1
        run.sections_ok |= ALL_SECTIONS - errors
        self._confirm("recovery", day, run)
        self._store.upsert_history_recovery(day, recovery_values(payload), today, missing=missing_text(prior & errors))
        run.fetched += 1

    # A day that still lacks a section after the re-fetch window is asked for again (a repair), a few per night.
    # Call-level errors follow the held-error rule above; a section that fails for every day of the run is not
    # asked for and nothing is charged for it.

    async def _repairs(
        self, candidates: list[tuple[date, frozenset[str]]], budget: int, today: date, run: _Run
    ) -> None:
        """Repair up to ``budget`` days, newest first, skipping days whose sections all failed in this run."""
        calls = 0
        for day, missing in candidates:
            if calls >= budget:
                break
            if run.snapshots_answered and not missing & run.sections_ok:
                continue  # every missing section failed in this run's regular snapshots: that part is down
            calls += 1
            await self._repair(day, missing, today, run)

    async def _repair(self, day: date, missing: frozenset[str], today: date, run: _Run) -> None:
        outcome, payload = await self._call("get_recovery_snapshot", {"date": day.isoformat()}, run)
        if outcome != "ok" or payload is None or snapshot_failed(payload):
            await self._item_error("recovery", [day], run)  # held; charged only when the backend shows it works
            return
        self._confirm("recovery", day, run)
        errors = section_errors(payload)
        rest = missing & errors
        values = {c: v for c, v in recovery_values(payload).items() if _SECTION_OF[c] in missing - errors}
        # An answer is charged only when the still-missing sections loaded for another day of this run.
        charged = bool(rest) and rest <= run.sections_ok
        self._store.repair_history_recovery(day, values, missing_text(rest), today, charged=charged)
        if not rest:
            run.repaired += 1

    # ---------------------------------------------------------- predictions

    async def _run_predictions(self, run: _Run) -> str | None:
        """The race-prediction part: one call for the missing range; None when nothing was due (no line)."""
        assert self._predictions is not None
        started = time.monotonic()
        today = self._clock.today()
        first, last = today - timedelta(days=self._settings.history_backfill_days), today - timedelta(days=1)
        state = self._store.prediction_sync_state()
        asked = prediction_range(state, first, last, today)
        if asked is None:
            return None
        days = 0
        try:
            if self._in_briefing_window(self._now()):
                raise _Stop("paused")
            await self._pace(run)
            args = {"start_date": asked[0].isoformat(), "end_date": asked[1].isoformat()}
            outcome, payload = await self._predictions.history_call("get_race_predictions", args)
            if outcome in _STOP_OUTCOMES:
                raise _Stop(_STOP_OUTCOMES[outcome])
            values = prediction_values(payload, *asked) if outcome == "ok" and payload is not None else None
            if values is None:
                status = "error"
            else:
                stored = _parse_day(state["covered_from"]) if state is not None else None
                covered_from = first if stored is None or stored > first else stored
                self._store.store_race_predictions(values, today, covered_from)
                status, days = "done", len(values)
        except _Stop as stop:
            status = stop.outcome
        log.log(
            logging.WARNING if status in _WARNING_OUTCOMES or status == "error" else logging.INFO,
            "history status=%s part=predictions days=%d ms=%.0f",
            status,
            days,
            (time.monotonic() - started) * 1000,
        )
        return status

    # ------------------------------------------------------------- strength

    async def _run_strength(self, run: _Run) -> str:
        """The Hevy part of the run: one log line, returns its outcome word."""
        started = time.monotonic()
        srun = _StrengthRun()
        try:
            outcome = await self._collect_strength(srun, run)
        except _Stop as stop:
            outcome = stop.outcome
        log.log(
            logging.WARNING if outcome in _WARNING_OUTCOMES else logging.INFO,
            "history status=%s part=strength workouts=%d deleted=%d templates=%d failed=%d skipped=%d ms=%.0f",
            outcome,
            srun.workouts,
            srun.deleted,
            srun.templates,
            srun.failed,
            srun.skipped,
            (time.monotonic() - started) * 1000,
        )
        return outcome

    async def _strength_call(self, name: str, args: dict[str, Any], run: _Run) -> tuple[str, dict[str, Any] | None]:
        """One paced Hevy call -> ("ok", payload) or ("error", None); anything else stops the part."""
        assert self._strength is not None
        await self._pace(run)
        outcome, payload = await self._strength.history_call(name, args)
        if outcome in _STOP_OUTCOMES:
            raise _Stop(_STOP_OUTCOMES[outcome])
        if outcome == "ok" and payload is not None:
            return "ok", payload
        return "error", None

    async def _collect_strength(self, srun: _StrengthRun, run: _Run) -> str:
        today = self._clock.today()
        first = today - timedelta(days=self._settings.history_backfill_days)
        state = self._store.hevy_sync_state()
        due = strength.listing_due(state, first, today)
        todo = self._templates_to_fetch(today)
        if not due and not todo:
            return "done"  # no call, no window check
        if self._in_briefing_window(self._now()):
            return "paused"
        if due:
            since, full = strength.listing_since(state, first, self._clock.zone)
            started = self._now()
            if not await self._list_events(strength.since_text(since), srun, run):
                srun.failed += 1
                return "service_error"  # the sync point is not advanced, nothing is given up
            covered_from = first if full or state is None else date.fromisoformat(state["covered_from"])
            self._store.record_hevy_sync(covered_from, started, self._clock.day_of(started))
            todo = self._templates_to_fetch(today)
        return await self._fetch_templates(todo, today, srun, run)

    async def _list_events(self, since: str, srun: _StrengthRun, run: _Run) -> bool:
        """Read every events page since ``since`` and apply it; True only for a complete listing."""
        page = 1
        while True:
            args = {"page": page, "page_size": strength.EVENTS_PAGE_SIZE, "since": since}
            outcome, payload = await self._strength_call("get-workout-events", args, run)
            if outcome != "ok" or payload is None:
                return False
            step = strength.page_step(payload, page)
            if step == "failed":
                return False
            batch = strength.parse_events(payload["events"], self._clock.zone)
            self._store.apply_strength_events(batch.updates, batch.deletes)
            srun.workouts += len(batch.updates)
            srun.deleted += len(batch.deletes)
            srun.skipped += batch.skipped
            if step == "complete":
                return True
            if page >= strength.MAX_EVENT_PAGES:
                return False
            page += 1

    def _templates_to_fetch(self, today: date) -> list[str]:
        """Templates of stored workouts that are not resolved: never tried first, then the longest ago."""
        known = self._store.hevy_templates()
        todo: list[tuple[bool, str, str]] = []
        for tid in self._store.strength_template_ids():
            row = known.get(tid)
            if row is None:
                todo.append((False, "", tid))
            elif row["found"] is None and row["attempted_on"] != today.isoformat():
                todo.append((row["attempted_on"] is not None, row["attempted_on"] or "", tid))
        return [tid for _, _, tid in sorted(todo)]

    async def _fetch_templates(self, todo: list[str], today: date, srun: _StrengthRun, run: _Run) -> str:
        errors = consecutive = 0
        for tid in todo:
            outcome, payload = await self._strength_call("get-exercise-template", {"exercise_template_id": tid}, run)
            value = payload.get("exercise_template", ...) if outcome == "ok" and payload is not None else ...
            if value is None:
                self._store.upsert_hevy_template(
                    tid, found=False, title=None, type_=None, group=None, attempted_on=today
                )
            elif isinstance(value, dict):
                title, kind, group = value.get("title"), value.get("type"), value.get("primary_muscle_group")
                self._store.upsert_hevy_template(
                    tid,
                    found=True,
                    title=title.strip()[: strength.MAX_TITLE] if isinstance(title, str) and title.strip() else None,
                    type_=kind if isinstance(kind, str) and len(kind) <= 40 else None,
                    group=group if isinstance(group, str) and 1 <= len(group) <= 40 else None,
                    attempted_on=today,
                )
            else:
                self._store.record_hevy_template_failure(tid, today)
                errors += 1
                srun.failed += 1
                consecutive += 1
                if consecutive >= OUTAGE_ERRORS:
                    return "service_error"
                continue
            srun.templates += 1
            consecutive = 0
        return "partial" if errors else "done"
