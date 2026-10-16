"""Morning briefing scheduler and the manual ``send-briefing`` and ``send-weekly-report`` commands.

``compose_briefing`` is the one place that reads Garmin, the coach plan and the alert
state and builds the briefing; the tool, the scheduler and the CLI all call it, so
they cannot disagree. The clock and the sleep function are injected. Log lines carry
outcome words, counters and exception class names only, never message content.

On Mondays the scheduler sends a second message, the Monday report (``compose_weekly``), once per
reported ISO week and only after the briefing of that day went out; it follows the briefing's window,
cutoff and retry rules.

A retry first asks garmin-mcp only for the last upload time and reuses the previous full read while nothing new was
uploaded.

The once-a-day rules (window, retries, giving up, the late start) live in ``delivery.py``; this module holds the
two messages' adapters and the loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from . import goals, tools, trends, weekly
from .briefing import Briefing, build_briefing
from .clock import LocalClock
from .config import Settings
from .delivery import Attempt, DailyDelivery, DailyMessage, send_now
from .garmin import GarminData, GarminReader, GarminSource, LastSync, exception_kind
from .messages import DEFAULT_LANGUAGE
from .push import PushMessage, PushSender
from .store import Store

log = logging.getLogger("coach_mcp.briefing")

MAX_SLEEP_SECONDS = 3600.0
# A full read is reused only when it started at least this long after the upload it saw: Garmin works out sleep and
# HRV after an upload, so a read right after one may still lack them.
SETTLE = timedelta(minutes=30)


@dataclass(frozen=True)
class _Read:
    day: date  # the briefing day the read is for: a new day starts with a full read
    data: GarminData
    last_sync: LastSync
    at: datetime  # when the read started, UTC


def reusable(data: GarminData) -> bool:
    """A full read a later retry may reuse: Garmin answered every call and every snapshot section, and the upload
    time is known."""
    if data.auth != "ok" or data.last_sync is None:
        return False
    if not data.calls or any(outcome != "ok" for outcome in data.calls.values()):
        return False
    snapshot = data.snapshot if isinstance(data.snapshot, Mapping) else {}
    return not any(isinstance(section, Mapping) and "error" in section for section in snapshot.values())


async def compose_briefing(
    store: Store, garmin: GarminSource, day: date, language: str = DEFAULT_LANGUAGE, *, data: GarminData | None = None
) -> Briefing:
    """``data``: a full Garmin read to reuse (the scheduler's retry gate); None reads Garmin now."""
    if data is None:
        data = await garmin.fetch(day)
    sessions = tools.sessions_out(store, store.sessions_between(day, day))
    before = day - timedelta(days=1)
    rows = store.sessions_between(before, before)
    yesterday_sessions = tools.sessions_out(store, rows)
    checked_at = store.suggestion_times([row["id"] for row in rows if row["status"] == "planned"])
    return build_briefing(
        day,
        data,
        sessions,
        store.alert_states(),
        yesterday_sessions=yesterday_sessions,
        checked_at=checked_at,
        language=language,
    )


@dataclass(frozen=True)
class Weekly:
    message: PushMessage
    week: str  # ISO label of the reported week, e.g. 2026-W41
    goals: int  # goals with a metric in the message
    waiting: bool = False  # the report never waits for data


def compose_weekly(store: Store, day: date, language: str = DEFAULT_LANGUAGE) -> Weekly:
    """The Monday report of the last complete week before ``day``; reads only the store."""
    report = trends.get_weekly_report(store, day, week=None)
    progress = goals.get_goal_progress(store, day)
    return Weekly(
        weekly.render_weekly(report, progress, language), report["week"]["iso_week"], len(progress["goals"])
    )


class BriefingScheduler:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        garmin: GarminSource,
        push: PushSender,
        *,
        now: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._store = store
        self._garmin = garmin
        self._sleep = sleep
        self._read: _Read | None = None  # today's last clean full Garmin read, for the retry gate
        self._delivery = DailyDelivery(
            LocalClock(settings.timezone, now),
            push,
            [
                DailyMessage(
                    "briefing", self._compose_briefing, self._briefing_sent, self._record_briefing, crash="raise"
                ),
                DailyMessage(
                    "weekly", self._compose_weekly, self._weekly_sent, self._record_weekly, due=self._weekly_due
                ),
            ],
            start=settings.briefing_time,
            cutoff=settings.briefing_cutoff,
            retry_seconds=settings.briefing_retry_minutes * 60.0,
        )

    # ----------------------------------------------------------------- loop

    async def run(self) -> None:
        retry = self._settings.briefing_retry_minutes * 60.0
        while True:
            try:
                delay = await self.tick()
            except Exception as exc:  # BaseException (cancellation) propagates
                log.error("briefing status=crashed kind=%s", exception_kind(exc))
                delay = retry
            await self._sleep(min(max(delay, 1.0), MAX_SLEEP_SECONDS))

    async def tick(self) -> float:
        """One scheduling step; returns the seconds to wait before the next one."""
        return await self._delivery.tick()

    # ------------------------------------------------------- the briefing

    def _briefing_sent(self, day: date) -> bool:
        return self._store.briefing_sent(day)

    async def _compose_briefing(self, attempt: Attempt) -> Briefing:
        data = await self._garmin_data(attempt)
        return await compose_briefing(self._store, self._garmin, attempt.day, self._settings.language, data=data)

    def _record_briefing(self, day: date, briefing: Briefing) -> None:
        self._store.record_briefing_sent(day, briefing.verdict, briefing.alert_updates)

    async def _garmin_data(self, attempt: Attempt) -> GarminData:
        """The full read, or the previous one when nothing was uploaded since it and it had settled."""
        cached = self._read
        if cached is not None and cached.day == attempt.day:
            try:
                current = await self._garmin.sync_check()
            except Exception as exc:  # a failed check is never a reason to skip the read
                log.warning("briefing sync_check=crashed kind=%s", exception_kind(exc))
                current = None
            settled = cached.at - cached.last_sync.utc >= SETTLE
            if current is not None and current.utc == cached.last_sync.utc and settled:
                log.info("briefing garmin=unchanged attempt=%d", attempt.number)
                return cached.data
        started = attempt.now.astimezone(UTC)
        data = await self._garmin.fetch(attempt.day)
        last = data.last_sync
        self._read = _Read(attempt.day, data, last, started) if last is not None and reusable(data) else None
        return data

    # ------------------------------------------------- the Monday report

    def _weekly_due(self, day: date) -> bool:
        return day.weekday() == 0 and self._settings.weekly_push

    def _weekly_sent(self, day: date) -> bool:
        return self._store.weekly_report_sent(tools.week_of(day - timedelta(days=7))[2])

    async def _compose_weekly(self, attempt: Attempt) -> Weekly:
        return compose_weekly(self._store, attempt.day, self._settings.language)

    def _record_weekly(self, day: date, report: Weekly) -> None:
        self._store.record_weekly_report_sent(report.week)


async def send_briefing_command(
    settings: Settings,
    *,
    store: Store | None = None,
    garmin: GarminSource | None = None,
    push: PushSender | None = None,
    today: Callable[[], date] | None = None,
) -> int:
    """Compose today's briefing and push it now; writes no state. Exit codes: 0 sent, 1 failed, 2 not configured."""

    async def compose(day: date) -> Briefing:
        return await compose_briefing(
            store or Store(settings.db_path), garmin or GarminReader(settings), day, settings.language
        )

    def summary(briefing: Briefing) -> str:
        missing = ",".join(briefing.payload["missing"]) or "-"
        new_alerts = sum(1 for alert in briefing.payload["alerts"] if alert["new"])
        return f"verdict={briefing.verdict} missing={missing} new_alerts={new_alerts}"

    return await send_now(settings, "briefing", compose, summary, push=push, today=today)


async def send_weekly_report_command(
    settings: Settings,
    *,
    store: Store | None = None,
    push: PushSender | None = None,
    today: Callable[[], date] | None = None,
) -> int:
    """Push the Monday report of the last complete week now; writes no state and ignores COACH_WEEKLY_PUSH.

    Exit codes: 0 sent, 1 failed, 2 not configured.
    """

    async def compose(day: date) -> Weekly:
        return compose_weekly(store or Store(settings.db_path), day, settings.language)

    def summary(report: Weekly) -> str:
        return f"week={report.week} goals={report.goals}"

    return await send_now(settings, "weekly", compose, summary, push=push, today=today)
