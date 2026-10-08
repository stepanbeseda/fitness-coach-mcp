"""Nightly history of Garmin daily values: the snapshot job and its pure helpers.

One row per local day in ``history_days``: recovery values (HRV, sleep, resting heart rate, Body Battery,
stress, load, VO2max) from ``get_recovery_snapshot`` and per-sport activity totals from ``list_activities``.
Progress is the stored state, not a cursor: ``needs_fetch`` decides from the dates and failure counters in
the store which days still need a call, so a restart resumes where the last run stopped. The clock and the
sleep function are injected. Log lines carry outcome words, counters and exception class names only, never
dates, values, payload text or arguments. After every run the job stores a suggestion (matched / no_activity)
for the planned sessions of the last seven days (``reconcile``); it never changes a session status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time as dt_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .garmin import HistorySource, exception_kind
from .reconcile import RECONCILE_DAYS, DayActivities, suggest
from .store import Store

log = logging.getLogger("coach_mcp.history")

STARTUP_DELAY_SECONDS = 60.0
RETRY_SECONDS = 3600.0
RATE_LIMIT_BACKOFF_SECONDS = 3600.0
MAX_BACKOFF_SECONDS = 28800.0
MAX_SLEEP_SECONDS = 3600.0
REFETCH_DAYS = 3
MAX_FAILURES = 3
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
SPORTS = ("running", "cycling", "swimming", "walking", "hiking", "strength", "other")
# Everything except Garmin's own strength_training, which is reported separately.
ENDURANCE_SPORTS = ("running", "cycling", "swimming", "walking", "hiking", "other")


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return value


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
                out[column] = _number(value)
        if key == "readiness":
            morning = _number(section.get("morning_score"))
            if morning is not None:
                out["readiness"] = morning
    return out


def snapshot_failed(payload: dict[str, Any]) -> bool:
    """True when every section of the snapshot is an error (the call itself worked, nothing was read)."""
    return all(isinstance(payload.get(key), dict) and "error" in payload[key] for key in SNAPSHOT_SECTIONS)


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
            total[field] += _number(row.get(field)) or 0
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


def activities_settled(day: date, fetched_on: date, failures: int) -> bool:
    """No later run re-fetches the activities of ``day``: the inverse of the re-fetch rule in ``needs_fetch``."""
    return fetched_on >= day + timedelta(days=REFETCH_DAYS) or failures >= MAX_FAILURES


def _fetched_on(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def day_activities(rows: Sequence[Mapping[str, Any]]) -> dict[str, DayActivities]:
    """Stored activity totals per date (``history_days`` rows) for ``reconcile.suggest``.

    A day never fetched (activities NULL, also a given-up day), with invalid JSON or without a fetch date is
    left out, so it gets no suggestion.
    """
    out: dict[str, DayActivities] = {}
    for row in rows:
        fetched_on = _fetched_on(row.get("activities_fetched_on"))
        text = row.get("activities")
        if fetched_on is None or not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        settled = activities_settled(
            date.fromisoformat(row["date"]), fetched_on, row.get("activities_failures") or 0
        )
        out[row["date"]] = DayActivities(parsed, settled)
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


class HistoryJob:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        source: HistorySource,
        *,
        now: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._store = store
        self._source = source
        self._now = now
        self._sleep = sleep
        self._zone = ZoneInfo(settings.timezone)
        self._due: datetime | None = None
        self._backoff = RATE_LIMIT_BACKOFF_SECONDS

    # ----------------------------------------------------------------- time

    def _at(self, day: date, clock_time: dt_time) -> datetime:
        """Local ``day`` at ``clock_time``; a time that does not exist resolves to the next valid instant."""
        return datetime.combine(day, clock_time, tzinfo=self._zone)

    @staticmethod
    def _until(target: datetime, now: datetime) -> float:
        # Both are converted to UTC: subtracting two datetimes that share a tzinfo is wall-clock
        # arithmetic in Python, which is wrong across a DST change.
        return (target.astimezone(UTC) - now.astimezone(UTC)).total_seconds()

    def _today(self, now: datetime) -> date:
        return now.astimezone(self._zone).date()

    def _next_history_time(self, after: datetime) -> datetime:
        day = self._today(after)
        target = self._at(day, self._settings.history_time)
        if self._until(target, after) > 0:
            return target
        return self._at(day + timedelta(days=1), self._settings.history_time)

    def _in_briefing_window(self, now: datetime) -> bool:
        day = self._today(now)
        start = self._at(day, self._settings.briefing_time)
        end = self._at(day, self._settings.briefing_cutoff)
        return self._until(start, now) <= 0 < self._until(end, now)

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
        wait = self._until(self._due, now)
        if wait > 0:
            return wait
        outcome = await self.run_once()
        after = self._now()
        if outcome == "done":
            self._due, self._backoff = self._next_history_time(after), RATE_LIMIT_BACKOFF_SECONDS
        elif outcome == "paused":
            self._due = self._at(self._today(after), self._settings.briefing_cutoff)
        elif outcome in ("rate_limited", "service_error"):  # one shared backoff
            self._due = after + timedelta(seconds=self._backoff)
            self._backoff = min(self._backoff * 2, MAX_BACKOFF_SECONDS)
        else:  # partial, auth, unreachable, no_credentials: try again in an hour, at the latest at history_time
            self._due = min(after + timedelta(seconds=RETRY_SECONDS), self._next_history_time(after))
            self._backoff = RATE_LIMIT_BACKOFF_SECONDS
        return self._until(self._due, after)

    # ------------------------------------------------------------------ run

    async def run_once(self) -> str:
        """One collection run plus the reconcile step; returns its outcome word (and logs exactly one line)."""
        started = time.monotonic()
        run = _Run()
        try:
            outcome = await self._collect(run)
        except _Stop as stop:
            outcome = stop.outcome  # the held errors of the run are dropped, not charged
        level = logging.WARNING if outcome in _WARNING_OUTCOMES else logging.INFO
        # The step needs only the store, so it runs after every run, also after an early return or a stop.
        try:
            matched, no_activity = self._reconcile(self._today(self._now()))
            step = f"matched={matched} no_activity={no_activity}"
        except Exception as exc:  # the run's outcome and scheduling stand; BaseException propagates
            step = f"reconcile=error kind={exception_kind(exc)}"
            level = logging.WARNING
        log.log(
            level,
            "history status=%s fetched=%d failed=%d pending=%d given_up=%d ms=%.0f %s",
            outcome,
            run.fetched,
            run.failed,
            len(run.pending),
            run.given_up,
            (time.monotonic() - started) * 1000,
            step,
        )
        return outcome

    def _reconcile(self, today: date) -> tuple[int, int]:
        """Store a suggestion for every planned session of the last RECONCILE_DAYS days; changes no status.

        Returns (matched, no_activity).
        """
        first, last = today - timedelta(days=RECONCILE_DAYS), today - timedelta(days=1)
        found = suggest(
            self._store.sessions_between(first, last), day_activities(self._store.history_rows(first, last))
        )
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
        today = self._today(self._now())
        first = today - timedelta(days=self._settings.history_backfill_days)
        last = today - timedelta(days=1)
        stored = {row["date"]: row for row in self._store.history_rows(first, last)}
        need_recovery: set[date] = set()
        need_activities: set[date] = set()
        day = last
        while day >= first:
            row = stored.get(day.isoformat(), {})
            fetched = {part: _fetched_on(row.get(f"{part}_fetched_on")) for part in PARTS}
            failures = {part: row.get(f"{part}_failures") or 0 for part in PARTS}
            if needs_fetch(day, fetched["recovery"], failures["recovery"], today):
                need_recovery.add(day)
            if needs_fetch(day, fetched["activities"], failures["activities"], today):
                need_activities.add(day)
            if any(fetched[p] is None and failures[p] >= MAX_FAILURES for p in PARTS):
                run.given_up += 1
            for part in PARTS:  # newest first: the first hit is the newest day that has data
                if run.known_good[part] is None and fetched[part] is not None:
                    run.known_good[part] = day
            day -= timedelta(days=1)
        run.pending.extend(sorted(need_recovery | need_activities, reverse=True))
        if not run.pending:
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
                    await self._fetch_snapshot(d, today, run)
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

    async def _call(self, name: str, args: dict[str, Any], run: _Run) -> tuple[str, dict[str, Any] | None]:
        """One paced backend call -> ("ok", payload) or ("error", None); anything else stops the run."""
        if run.calls:
            await self._sleep(self._settings.history_call_delay_seconds)
            if self._in_briefing_window(self._now()):
                raise _Stop("paused")
        run.calls += 1
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

    async def _fetch_snapshot(self, day: date, today: date, run: _Run) -> None:
        outcome, payload = await self._call("get_recovery_snapshot", {"date": day.isoformat()}, run)
        if outcome != "ok" or payload is None or snapshot_failed(payload):
            await self._item_error("recovery", [day], run)
            return
        self._confirm("recovery", day, run)
        self._store.upsert_history_recovery(day, recovery_values(payload), today)
        run.fetched += 1
