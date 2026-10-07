"""Morning briefing scheduler and the manual ``send-briefing`` command.

``compose_briefing`` is the one place that reads Garmin, the coach plan and the alert
state and builds the briefing; the tool, the scheduler and the CLI all call it, so
they cannot disagree. The clock and the sleep function are injected. Log lines carry
outcome words, counters and exception class names only, never message content.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import tools
from .briefing import Briefing, build_briefing
from .config import Settings
from .garmin import GarminReader, GarminSource, exception_kind
from .push import PushSender, TelegramPush
from .store import Store

log = logging.getLogger("coach_mcp.briefing")

MAX_SLEEP_SECONDS = 3600.0


async def compose_briefing(store: Store, garmin: GarminSource, day: date) -> Briefing:
    data = await garmin.fetch(day)
    sessions = tools.sessions_out(store, store.sessions_between(day, day))
    return build_briefing(day, data, sessions, store.alert_states())


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
        if self._store.briefing_sent(day):
            return to_tomorrow
        if self._late_start:
            log.info("briefing status=skipped reason=late_start")
            self._stopped = True
            return to_tomorrow

        retry = min(settings.briefing_retry_minutes * 60.0, max(self._until(cutoff, now), 0.0))
        self._attempts += 1
        try:
            briefing = await compose_briefing(self._store, self._garmin, day)
            if briefing.waiting and not final:
                log.info("briefing status=waiting attempt=%d", self._attempts)
                return retry
            outcome = await self._push.send(briefing.message)
        except Exception:
            if final:  # no more attempts today: do not crash-loop until midnight
                self._stopped = True
            raise

        if outcome == "sent":
            self._stopped = True
            try:
                self._store.record_briefing_sent(day, briefing.verdict, briefing.alert_updates)
            except Exception as exc:
                log.error("briefing status=state_error kind=%s", exception_kind(exc))
            log.info("briefing status=sent attempt=%d", self._attempts)
            return to_tomorrow
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
            store or Store(settings.db_path), garmin or GarminReader(settings), today()
        )
        outcome = await (push or TelegramPush(settings)).send(briefing.message)
    except Exception as exc:
        log.error("briefing status=crashed kind=%s", exception_kind(exc))
        return 1
    missing = ",".join(briefing.payload["missing"]) or "-"
    new_alerts = sum(1 for alert in briefing.payload["alerts"] if alert["new"])
    print(f"briefing status={outcome} verdict={briefing.verdict} missing={missing} new_alerts={new_alerts}")
    return 0 if outcome == "sent" else 1
