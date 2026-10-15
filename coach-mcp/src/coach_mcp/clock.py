"""The athlete's local time: the configured zone (``TZ``), now, today, the instant of a local clock time on a day
and the real number of seconds until an instant.

Every module that needs local time uses this one. Local days and clock times come from the zone; durations are
computed between UTC instants, because subtracting two datetimes that share a tzinfo is wall-clock arithmetic in
Python, which is wrong across a DST change. A clock time that does not exist (spring forward) resolves to the next
valid instant (02:30 becomes 03:30), a repeated one (fall back) to its first occurrence.

The source of "now" is injected; tests pass a fake clock, production uses the system clock in the zone.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def local_at(day: date, clock_time: time, zone: ZoneInfo) -> datetime:
    """Local ``day`` at ``clock_time`` in ``zone``."""
    return datetime.combine(day, clock_time, tzinfo=zone)


def local_day(instant: datetime, zone: ZoneInfo) -> date:
    """The local date of an aware ``instant`` in ``zone``."""
    return instant.astimezone(zone).date()


def seconds_until(target: datetime, now: datetime) -> float:
    """Real seconds from ``now`` to ``target`` (negative when it is past), computed in UTC."""
    return (target.astimezone(UTC) - now.astimezone(UTC)).total_seconds()


class LocalClock:
    def __init__(self, timezone: str, now: Callable[[], datetime] | None = None) -> None:
        self.zone = ZoneInfo(timezone)
        self._source = now if now is not None else self._system_now

    def _system_now(self) -> datetime:
        return datetime.now(self.zone)

    def now(self) -> datetime:
        """The current instant, in the zone."""
        return self._source().astimezone(self.zone)

    def today(self) -> date:
        return self.now().date()

    def day_of(self, instant: datetime) -> date:
        return local_day(instant, self.zone)

    def at(self, day: date, clock_time: time) -> datetime:
        return local_at(day, clock_time, self.zone)

    def next_at(self, clock_time: time, after: datetime) -> datetime:
        """The first instant of local ``clock_time`` strictly after ``after``: today's when still ahead, else
        tomorrow's."""
        day = self.day_of(after)
        target = self.at(day, clock_time)
        if seconds_until(target, after) > 0:
            return target
        return self.at(day + timedelta(days=1), clock_time)

    @staticmethod
    def until(target: datetime, now: datetime) -> float:
        return seconds_until(target, now)
