"""Shared helpers for the history tests: fixed zone, seeding the store, running the job on a fake clock."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fakes import FakeClock, FakeHistorySource, FakeStrengthSource, StopLoop

from coach_mcp.config import Settings
from coach_mcp.history import HistoryJob, totals_json
from coach_mcp.strength import exercises_json
from coach_mcp.store import Store

PRAGUE = ZoneInfo("Europe/Prague")


def local(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=PRAGUE)


def seed_day(
    store: Store, day: date, *, fetched_on: date | None = None, missing: str | None = None, **columns: Any
) -> None:
    """Store daily values for ``day`` (fetched the day after unless ``fetched_on`` says otherwise).

    ``missing`` is the stored ``recovery_missing`` text; None leaves the column NULL (an old-schema seed).
    """
    store.upsert_history_recovery(day, columns, fetched_on or day + timedelta(days=1), missing=missing)


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
    strength: FakeStrengthSource | None = None,
    predictions: FakeHistorySource | None = None,
) -> tuple[HistoryJob, FakeClock]:
    """Run ``HistoryJob.run()`` from ``start`` on a fake clock until ``stop_when()`` or ``max_sleeps`` sleeps."""
    clock = FakeClock(start, stop_when=stop_when, max_sleeps=max_sleeps)
    if source.clock is None:
        source.clock = clock
    if strength is not None and strength.clock is None:
        strength.clock = clock
    settings = Settings(timezone="Europe/Prague", **(settings_overrides or {}))
    if predictions is not None and predictions.clock is None:
        predictions.clock = clock
    job = HistoryJob(
        settings, store, source, now=clock.now, sleep=clock.sleep, strength=strength, predictions=predictions
    )
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
    strength: FakeStrengthSource | None = None,
    predictions: FakeHistorySource | None = None,
    **settings: Any,
) -> tuple[HistoryJob, FakeClock]:
    """A job on a fake clock for tests that drive ``tick()`` by hand (the clock only moves in ``sleep``)."""
    clock = FakeClock(start, stop_when=stop_when, max_sleeps=100_000)
    if source.clock is None:
        source.clock = clock
    if strength is not None and strength.clock is None:
        strength.clock = clock
    if predictions is not None and predictions.clock is None:
        predictions.clock = clock
    job = HistoryJob(
        Settings(timezone="Europe/Prague", **settings),
        store,
        source,
        now=clock.now,
        sleep=clock.sleep,
        strength=strength,
        predictions=predictions,
    )
    return job, clock


async def run_first(job: HistoryJob, clock: FakeClock) -> float:
    """Wait out the 60 s start-up delay, run once; returns the seconds until the next run."""
    assert await job.tick() == 60.0
    await clock.sleep(60.0)
    return await job.tick()


async def next_run(job: HistoryJob, clock: FakeClock, delay: float) -> float:
    """Sleep ``delay`` seconds (the value the last tick returned), run again; returns the next delay."""
    await clock.sleep(delay)
    return await job.tick()


def seed_strength(
    store: Store,
    workout_id: str,
    day: date,
    exercises: list[dict[str, Any]],
    *,
    updated: str = "2026-10-01T10:00:00.000000Z",
) -> None:
    """Store one workout (``exercises`` are stored summaries, see ``strength.summarize_exercises``)."""
    store.apply_strength_events({workout_id: (day, updated, exercises_json(exercises))}, [])


def exercise(
    template_id: str, sets: int, volume_kg: float, best: tuple[float, int, float] | None = None, title: str | None = None
) -> dict[str, Any]:
    """A stored exercise summary; ``best`` is ``(weight_kg, reps, e1rm_kg)``."""
    return {
        "template_id": template_id,
        "title": title,
        "sets": sets,
        "volume_kg": volume_kg,
        "best": None if best is None else {"weight_kg": best[0], "reps": best[1], "e1rm_kg": best[2]},
    }


def seed_template(store: Store, tid: str, group: str | None, *, title: str | None = None, type_: str = "weight_reps") -> None:
    store.upsert_hevy_template(tid, found=True, title=title, type_=type_, group=group, attempted_on=date(2026, 1, 1))


def seed_sync(store: Store, covered_from: date, synced_on: date) -> None:
    """The ``hevy_sync`` row of a complete listing that ran at 03:30 local time of ``synced_on``."""
    synced_at = datetime(synced_on.year, synced_on.month, synced_on.day, 3, 30, tzinfo=PRAGUE)
    store.record_hevy_sync(covered_from, synced_at.astimezone(UTC), synced_on)
