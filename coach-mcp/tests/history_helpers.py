"""Shared helpers for the history tests: fixed zone, seeding the store, running the job on a fake clock."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fakes import FakeClock, FakeHistorySource, StopLoop

from coach_mcp.config import Settings
from coach_mcp.history import HistoryJob, totals_json
from coach_mcp.store import Store

PRAGUE = ZoneInfo("Europe/Prague")


def local(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=PRAGUE)


def seed_day(store: Store, day: date, *, fetched_on: date | None = None, **columns: Any) -> None:
    """Store daily values for ``day`` (fetched the day after unless ``fetched_on`` says otherwise)."""
    store.upsert_history_recovery(day, columns, fetched_on or day + timedelta(days=1))


def seed_activities(
    store: Store, day: date, totals: dict[str, dict[str, float | int]], fetched_on: date | None = None
) -> None:
    store.upsert_history_activities({day: totals_json(totals)}, fetched_on or day + timedelta(days=1))


def sport(count: int = 1, duration_min: float = 0, distance_km: float = 0, training_load: float = 0) -> dict[str, Any]:
    return {"count": count, "duration_min": duration_min, "distance_km": distance_km, "training_load": training_load}


async def run_job(
    store: Store,
    source: FakeHistorySource,
    start: datetime,
    *,
    settings_overrides: dict[str, Any] | None = None,
    stop_when: Any = lambda: False,
    max_sleeps: int = 2000,
) -> tuple[HistoryJob, FakeClock]:
    """Run ``HistoryJob.run()`` from ``start`` on a fake clock until ``stop_when()`` or ``max_sleeps`` sleeps."""
    clock = FakeClock(start, stop_when=stop_when, max_sleeps=max_sleeps)
    if source.clock is None:
        source.clock = clock
    settings = Settings(timezone="Europe/Prague", **(settings_overrides or {}))
    job = HistoryJob(settings, store, source, now=clock.now, sleep=clock.sleep)
    try:
        await job.run()
    except StopLoop:
        pass
    return job, clock


def make_job(
    store: Store,
    source: FakeHistorySource,
    start: datetime,
    *,
    stop_when: Any = lambda: False,
    **settings: Any,
) -> tuple[HistoryJob, FakeClock]:
    """A job on a fake clock for tests that drive ``tick()`` by hand (the clock only moves in ``sleep``)."""
    clock = FakeClock(start, stop_when=stop_when, max_sleeps=100_000)
    if source.clock is None:
        source.clock = clock
    return HistoryJob(Settings(timezone="Europe/Prague", **settings), store, source, now=clock.now, sleep=clock.sleep), clock


async def run_first(job: HistoryJob, clock: FakeClock) -> float:
    """Wait out the 60 s start-up delay, run once; returns the seconds until the next run."""
    assert await job.tick() == 60.0
    await clock.sleep(60.0)
    return await job.tick()


async def next_run(job: HistoryJob, clock: FakeClock, delay: float) -> float:
    """Sleep ``delay`` seconds (the value the last tick returned), run again; returns the next delay."""
    await clock.sleep(delay)
    return await job.tick()
