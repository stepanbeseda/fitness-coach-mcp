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
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import goals, tools, trends, weekly
from .briefing import Briefing, build_briefing
from .config import Settings
from .garmin import GarminData, GarminReader, GarminSource, LastSync, exception_kind
from .messages import DEFAULT_LANGUAGE
from .push import PushMessage, PushSender, TelegramPush
from .store import Store

log = logging.getLogger("coach_mcp.briefing")

MAX_SLEEP_SECONDS = 3600.0
# A full read is reused only when it started at least this long after the upload it saw: Garmin works out sleep and
# HRV after an upload, so a read right after one may still lack them.
SETTLE = timedelta(minutes=30)


@dataclass(frozen=True)
class _Read:
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
        self._push = push
        self._now = now
        self._sleep = sleep
        self._zone = ZoneInfo(settings.timezone)
        self._day: date | None = None
        self._attempts = 0
        self._window_seen = False
        self._late_start = False
        self._stopped = False
        self._briefed = False  # today's briefing went out in this process (also when recording it failed)
        self._weekly_attempts = 0
        self._read: _Read | None = None  # today's last clean full Garmin read, for the retry gate

    # ----------------------------------------------------------------- time

    def _at(self, day: date, clock_time: time) -> datetime:
        """Local ``day`` at ``clock_time``; a time that does not exist resolves to the next valid instant."""
        return datetime.combine(day, clock_time, tzinfo=self._zone)

    @staticmethod
    def _until(target: datetime, now: datetime) -> float:
        # Both are converted to UTC: subtracting two datetimes that share a tzinfo is wall-clock
        # arithmetic in Python, which is wrong across a DST change.
        return (target.astimezone(UTC) - now.astimezone(UTC)).total_seconds()

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
        settings = self._settings
        now = self._now().astimezone(self._zone)
        day = now.date()
        if day != self._day:
            self._day, self._attempts, self._window_seen, self._stopped = day, 0, False, False
            self._late_start = False
            self._briefed, self._weekly_attempts = False, 0
            self._read = None
        start = self._at(day, settings.briefing_time)
        cutoff = self._at(day, settings.briefing_cutoff)
        to_tomorrow = self._until(self._at(day + timedelta(days=1), settings.briefing_time), now)

        if self._stopped:
            return to_tomorrow
        if self._until(start, now) > 0:
            return self._until(start, now)
        final = self._until(cutoff, now) <= 0
        # Remember, before anything that can raise, whether the first look inside today's window was
        # already past the cutoff: a store error on that look must neither turn a late start into a
        # late send (retry is "final" too) nor an ordinary start into a "late start".
        if not self._window_seen:
            self._window_seen = True
            self._late_start = final
        if self._briefed or self._store.briefing_sent(day):
            return await self._weekly_step(day, now, cutoff, final, to_tomorrow)
        if self._late_start:
            log.info("briefing status=skipped reason=late_start")
            self._stopped = True
            return to_tomorrow

        retry = min(settings.briefing_retry_minutes * 60.0, max(self._until(cutoff, now), 0.0))
        self._attempts += 1
        try:
            data = await self._garmin_data(day, now)
            briefing = await compose_briefing(self._store, self._garmin, day, settings.language, data=data)
            if briefing.waiting and not final:
                log.info("briefing status=waiting attempt=%d", self._attempts)
                return retry
            outcome = await self._push.send(briefing.message)
        except Exception:
            if final:  # no more attempts today: do not crash-loop until midnight
                self._stopped = True
            raise

        if outcome == "sent":
            self._briefed = True
            try:
                self._store.record_briefing_sent(day, briefing.verdict, briefing.alert_updates)
            except Exception as exc:
                log.error("briefing status=state_error kind=%s", exception_kind(exc))
            log.info("briefing status=sent attempt=%d", self._attempts)
            return await self._weekly_step(day, now, cutoff, final, to_tomorrow)
        if outcome == "permanent":
            log.warning("briefing status=gave_up reason=push_permanent")
            self._stopped = True
            return to_tomorrow
        if final:
            log.warning("briefing status=gave_up reason=push_transient")
            self._stopped = True
            return to_tomorrow
        log.info("briefing status=waiting attempt=%d", self._attempts)
        return retry

    async def _garmin_data(self, day: date, now: datetime) -> GarminData:
        """The full read, or the previous one when nothing was uploaded since it and it had settled."""
        cached = self._read
        if cached is not None:
            try:
                current = await self._garmin.sync_check()
            except Exception as exc:  # a failed check is never a reason to skip the read
                log.warning("briefing sync_check=crashed kind=%s", exception_kind(exc))
                current = None
            settled = cached.at - cached.last_sync.utc >= SETTLE
            if current is not None and current.utc == cached.last_sync.utc and settled:
                log.info("briefing garmin=unchanged attempt=%d", self._attempts)
                return cached.data
        started = now.astimezone(UTC)
        data = await self._garmin.fetch(day)
        last = data.last_sync
        self._read = _Read(data, last, started) if last is not None and reusable(data) else None
        return data

    async def _weekly_step(self, day: date, now: datetime, cutoff: datetime, final: bool, to_tomorrow: float) -> float:
        """The Monday report, after the briefing of ``day`` went out; returns the seconds to wait."""
        settings = self._settings
        retry = min(settings.briefing_retry_minutes * 60.0, max(self._until(cutoff, now), 0.0))
        try:
            due = (
                day.weekday() == 0
                and settings.weekly_push
                and not self._store.weekly_report_sent(tools.week_of(day - timedelta(days=7))[2])
            )
            if not due:
                self._stopped = True
                return to_tomorrow
            if self._late_start:
                log.info("weekly status=skipped reason=late_start")
                self._stopped = True
                return to_tomorrow
            self._weekly_attempts += 1
            report = compose_weekly(self._store, day, settings.language)
            outcome = await self._push.send(report.message)
        except Exception as exc:  # BaseException (cancellation) propagates
            log.error("weekly status=crashed kind=%s", exception_kind(exc))
            if final:
                self._stopped = True
                return to_tomorrow
            return retry
        if outcome == "sent":
            self._stopped = True
            try:
                self._store.record_weekly_report_sent(report.week)
            except Exception as exc:
                log.error("weekly status=state_error kind=%s", exception_kind(exc))
            log.info("weekly status=sent attempt=%d", self._weekly_attempts)
            return to_tomorrow
        if outcome == "permanent":
            log.warning("weekly status=gave_up reason=push_permanent")
            self._stopped = True
            return to_tomorrow
        if final:
            log.warning("weekly status=gave_up reason=push_transient")
            self._stopped = True
            return to_tomorrow
        log.info("weekly status=waiting attempt=%d", self._weekly_attempts)
        return retry


async def send_briefing_command(
    settings: Settings,
    *,
    store: Store | None = None,
    garmin: GarminSource | None = None,
    push: PushSender | None = None,
    today: Callable[[], date] | None = None,
) -> int:
    """Compose today's briefing and push it now; writes no state. Exit codes: 0 sent, 1 failed, 2 not configured."""
    if not settings.push_enabled:
        print("push is not configured: set COACH_TELEGRAM_CHAT_ID", file=sys.stderr)
        return 2
    if today is None:
        zone = ZoneInfo(settings.timezone)

        def today() -> date:
            return datetime.now(zone).date()

    try:
        briefing = await compose_briefing(
            store or Store(settings.db_path), garmin or GarminReader(settings), today(), settings.language
        )
        outcome = await (push or TelegramPush(settings)).send(briefing.message)
    except Exception as exc:
        log.error("briefing status=crashed kind=%s", exception_kind(exc))
        return 1
    missing = ",".join(briefing.payload["missing"]) or "-"
    new_alerts = sum(1 for alert in briefing.payload["alerts"] if alert["new"])
    print(f"briefing status={outcome} verdict={briefing.verdict} missing={missing} new_alerts={new_alerts}")
    return 0 if outcome == "sent" else 1


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
    if not settings.push_enabled:
        print("push is not configured: set COACH_TELEGRAM_CHAT_ID", file=sys.stderr)
        return 2
    if today is None:
        zone = ZoneInfo(settings.timezone)

        def today() -> date:
            return datetime.now(zone).date()

    try:
        report = compose_weekly(store or Store(settings.db_path), today(), settings.language)
        outcome = await (push or TelegramPush(settings)).send(report.message)
    except Exception as exc:
        log.error("weekly status=crashed kind=%s", exception_kind(exc))
        return 1
    print(f"weekly status={outcome} week={report.week} goals={report.goals}")
    return 0 if outcome == "sent" else 1
