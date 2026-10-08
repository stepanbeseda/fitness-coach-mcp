"""Scheduler, manual send and logging rules, driven by a fake clock, fake Garmin and fake push (no network)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx2
import pytest
from fakes import FakeClock, FakeGarmin, FakePush, StopLoop, fake_garmin_app, fake_garmin_running

from coach_mcp.__main__ import configure_logging, main
from coach_mcp.briefing import ALERT_KEYS, EVENT_ALERTS
from coach_mcp.config import Settings
from coach_mcp.garmin import GarminReader
from coach_mcp.push import TelegramPush
from coach_mcp.scheduler import BriefingScheduler, compose_briefing, send_briefing_command
from coach_mcp.store import Store
from garmin_payloads import (
    GREEN_SNAPSHOT,
    days_range,
    garmin_data,
    scheduled,
    trend,
)

BOT_TOKEN = "123456789:SECRETtokenSECRETtokenSECRETtoken12"  # 35 characters after the colon
PRAGUE = ZoneInfo("Europe/Prague")
D = date(2026, 10, 7)


def local(day: date, hour: int, minute: int = 0, zone: ZoneInfo = PRAGUE) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)


def make_settings(db_path: Path, **overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "db_path": db_path,
        "timezone": "Europe/Prague",
        "telegram_chat_id": "123456789",
    }
    fields.update(overrides)
    return Settings(**fields)


async def run_scheduler(
    db_path: Path,
    start: datetime,
    garmin: FakeGarmin,
    push: FakePush | None = None,
    *,
    until: datetime | None = None,
    stop_pushes: int | None = None,
    store: Store | None = None,
    **settings: Any,
) -> tuple[FakePush, FakeClock]:
    """Run the scheduler loop from ``start`` until ``stop_pushes`` pushes or the clock passes ``until``."""
    state: dict[str, Any] = {}

    def stop() -> bool:
        if stop_pushes is not None and len(state["push"].sent) >= stop_pushes:
            return True
        return until is not None and state["clock"].now() >= until.astimezone(UTC)

    clock = FakeClock(start, stop_when=stop)
    push = push or FakePush(clock=clock)
    if push._clock is None:
        push._clock = clock
    state.update(clock=clock, push=push)
    scheduler = BriefingScheduler(
        make_settings(db_path, **settings),
        store or Store(db_path),
        garmin,
        push,
        now=clock.now,
        sleep=clock.sleep,
    )
    with pytest.raises(StopLoop):
        await scheduler.run()
    return push, clock


def utc(instant: datetime | None) -> datetime:
    assert instant is not None
    return instant.astimezone(UTC)


# ------------------------------------------------------------------ timing


@pytest.mark.anyio
async def test_sends_at_configured_time(db_path):
    garmin = FakeGarmin(garmin_data())
    push, _ = await run_scheduler(db_path, local(D, 0), garmin, stop_pushes=1)
    assert [utc(t) for t, _ in push.sent] == [local(D, 6, 30).astimezone(UTC)]
    assert garmin.days == [D]
    assert Store(db_path).briefing_sent(D)


@pytest.mark.anyio
async def test_custom_time_from_settings(db_path):
    import datetime as dt

    push, _ = await run_scheduler(
        db_path, local(D, 0), FakeGarmin(garmin_data()), stop_pushes=1, briefing_time=dt.time(5, 45)
    )
    assert [utc(t) for t, _ in push.sent] == [local(D, 5, 45).astimezone(UTC)]


@pytest.mark.anyio
async def test_sends_at_configured_time_in_configured_zone(db_path):
    new_york = ZoneInfo("America/New_York")
    start = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)
    garmin = FakeGarmin(garmin_data())
    push, _ = await run_scheduler(db_path, start, garmin, stop_pushes=1, timezone="America/New_York")
    (sent_at, _msg), = push.sent
    assert utc(sent_at) == datetime(2026, 10, 7, 10, 30, tzinfo=UTC)
    assert utc(sent_at).astimezone(new_york).strftime("%H:%M") == "06:30"
    assert garmin.days == [D]  # the briefing is for the local date in the configured zone


@pytest.mark.anyio
async def test_retries_while_sleep_or_hrv_missing_then_sends_when_synced(db_path):
    def data(day: date, index: int):
        if index < 2:
            snap = {k: v for k, v in GREEN_SNAPSHOT.items() if k != "sleep"}
            return garmin_data(snapshot=snap)
        return garmin_data()

    garmin = FakeGarmin(data)
    push, _ = await run_scheduler(db_path, local(D, 0), garmin, stop_pushes=1)
    assert garmin.calls == 3
    (sent_at, msg), = push.sent
    assert utc(sent_at) == local(D, 7, 0).astimezone(UTC)
    assert "Chybí" not in msg.body and msg.body.startswith("Verdikt: zelená")


@pytest.mark.anyio
async def test_retries_while_hrv_missing(db_path):
    def data(day: date, index: int):
        if index < 1:
            return garmin_data(snapshot={k: v for k, v in GREEN_SNAPSHOT.items() if k != "hrv"})
        return garmin_data()

    push, _ = await run_scheduler(db_path, local(D, 0), FakeGarmin(data), stop_pushes=1)
    assert utc(push.sent[0][0]) == local(D, 6, 45).astimezone(UTC)


@pytest.mark.anyio
async def test_other_missing_data_does_not_wait(db_path):
    snap = {k: v for k, v in GREEN_SNAPSHOT.items() if k != "body_battery"}
    garmin = FakeGarmin(garmin_data(snapshot=snap))
    push, _ = await run_scheduler(db_path, local(D, 0), garmin, stop_pushes=1)
    assert garmin.calls == 1
    assert utc(push.sent[0][0]) == local(D, 6, 30).astimezone(UTC)
    assert "Chybí: Body Battery" in push.sent[0][1].body


@pytest.mark.anyio
async def test_sends_at_cutoff_with_missing_data(db_path):
    garmin = FakeGarmin(garmin_data(snapshot={k: v for k, v in GREEN_SNAPSHOT.items() if k != "hrv"}))
    push, _ = await run_scheduler(db_path, local(D, 0), garmin, stop_pushes=1)
    assert garmin.calls == 13  # 06:30, 06:45, ..., 09:15 waiting, 09:30 final
    (sent_at, msg), = push.sent
    assert utc(sent_at) == local(D, 9, 30).astimezone(UTC)
    assert "Chybí: HRV" in msg.body
    assert Store(db_path).briefing_sent(D)


@pytest.mark.anyio
async def test_auth_problem_sends_without_waiting(db_path):
    garmin = FakeGarmin(garmin_data(auth="missing", snapshot=None, hrv_trend=None, scheduled=None))
    push, _ = await run_scheduler(db_path, local(D, 0), garmin, stop_pushes=1)
    assert garmin.calls == 1
    (sent_at, msg), = push.sent
    assert utc(sent_at) == local(D, 6, 30).astimezone(UTC)
    assert "Garmin není přihlášený: na serveru spusť scripts/garmin-login.sh." in msg.body


@pytest.mark.anyio
async def test_start_after_cutoff_skips_until_next_day(db_path, caplog):
    caplog.set_level(logging.DEBUG)
    garmin = FakeGarmin(garmin_data())
    push, _ = await run_scheduler(db_path, local(D, 10, 0), garmin, stop_pushes=1)
    assert [utc(t) for t, _ in push.sent] == [local(D + timedelta(days=1), 6, 30).astimezone(UTC)]
    assert garmin.days == [D + timedelta(days=1)]
    assert "briefing status=skipped reason=late_start" in caplog.text


@pytest.mark.anyio
async def test_start_inside_window_runs_immediately(db_path):
    push, _ = await run_scheduler(db_path, local(D, 8, 0), FakeGarmin(garmin_data()), stop_pushes=1)
    assert [utc(t) for t, _ in push.sent] == [local(D, 8, 0).astimezone(UTC)]


@pytest.mark.anyio
async def test_already_sent_today_is_not_sent_again_after_restart(db_path):
    first, _ = await run_scheduler(db_path, local(D, 6, 0), FakeGarmin(garmin_data()), stop_pushes=1)
    assert len(first.sent) == 1
    garmin = FakeGarmin(garmin_data())
    second, _ = await run_scheduler(db_path, local(D, 7, 30), garmin, stop_pushes=1, store=Store(db_path))
    assert [utc(t) for t, _ in second.sent] == [local(D + timedelta(days=1), 6, 30).astimezone(UTC)]
    assert garmin.days == [D + timedelta(days=1)]  # nothing was even fetched on the sent day


@pytest.mark.anyio
async def test_transient_push_failure_retries_permanent_stops_for_the_day(db_path, tmp_path):
    push, _ = await run_scheduler(
        db_path, local(D, 0), FakeGarmin(garmin_data()), FakePush(["transient", "sent"]), stop_pushes=2
    )
    assert [utc(t) for t, _ in push.sent] == [local(D, 6, 30).astimezone(UTC), local(D, 6, 45).astimezone(UTC)]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM briefing_runs").fetchone()[0] == 1

    other_db = tmp_path / "permanent.db"
    permanent = FakePush(["permanent"])
    push, _ = await run_scheduler(other_db, local(D, 0), FakeGarmin(garmin_data()), permanent, stop_pushes=2)
    assert [utc(t) for t, _ in push.sent] == [
        local(D, 6, 30).astimezone(UTC),
        local(D + timedelta(days=1), 6, 30).astimezone(UTC),
    ]
    with sqlite3.connect(other_db) as conn:
        assert conn.execute("SELECT count(*) FROM briefing_runs").fetchone()[0] == 0


@pytest.mark.anyio
async def test_state_written_only_after_successful_push(db_path, caplog):
    caplog.set_level(logging.DEBUG)
    push = FakePush(["transient"])
    garmin = FakeGarmin(garmin_data(auth="missing"))
    await run_scheduler(db_path, local(D, 0), garmin, push, stop_pushes=13)
    assert len(push.sent) == 13  # 06:30 ... 09:30, then it gives up for the day
    assert utc(push.sent[-1][0]) == local(D, 9, 30).astimezone(UTC)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM briefing_runs").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM alert_state").fetchone()[0] == 0
    assert "briefing status=gave_up reason=push_transient" in caplog.text


@pytest.mark.anyio
async def test_failing_state_write_does_not_cause_a_second_push(db_path, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    store = Store(db_path)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(store, "record_briefing_sent", broken)
    push, _ = await run_scheduler(
        db_path, local(D, 0), FakeGarmin(garmin_data()), stop_pushes=2, store=store
    )
    # the push of the first day is not repeated; the second one is the next morning's
    assert [utc(t) for t, _ in push.sent] == [
        local(D, 6, 30).astimezone(UTC),
        local(D + timedelta(days=1), 6, 30).astimezone(UTC),
    ]
    assert "briefing status=state_error kind=OperationalError" in caplog.text
    assert "disk full" not in caplog.text


@pytest.mark.anyio
async def test_the_briefing_day_is_the_local_date_not_the_utc_date(db_path):
    import datetime as dt

    # 01:00 Prague on 2026-10-07 is 23:00 UTC on 2026-10-06.
    garmin = FakeGarmin(garmin_data())
    push, _ = await run_scheduler(
        db_path,
        local(date(2026, 10, 6), 23, 0),
        garmin,
        stop_pushes=1,
        briefing_time=dt.time(1, 0),
        briefing_cutoff=dt.time(3, 0),
    )
    assert [utc(t) for t, _ in push.sent] == [local(D, 1, 0).astimezone(UTC)]
    assert garmin.days == [D]


@pytest.mark.anyio
async def test_no_push_after_giving_up_on_the_same_day(db_path):
    push = FakePush(["transient"])
    await run_scheduler(db_path, local(D, 9, 20), FakeGarmin(garmin_data()), push, stop_pushes=3)
    # 09:20 and 09:30 (transient, then final gives up); the third push is the next morning
    assert [utc(t) for t, _ in push.sent][:2] == [local(D, 9, 20).astimezone(UTC), local(D, 9, 30).astimezone(UTC)]
    assert utc(push.sent[2][0]) == local(D + timedelta(days=1), 6, 30).astimezone(UTC)


@pytest.mark.anyio
async def test_crash_in_a_tick_does_not_stop_the_loop(db_path, caplog):
    caplog.set_level(logging.DEBUG)

    def data(day: date, index: int):
        return RuntimeError("boom") if index == 0 else garmin_data()

    push, _ = await run_scheduler(db_path, local(D, 0), FakeGarmin(data), stop_pushes=1)
    assert utc(push.sent[0][0]) == local(D, 6, 45).astimezone(UTC)
    assert "briefing status=crashed kind=RuntimeError" in caplog.text
    assert "boom" not in caplog.text


@pytest.mark.anyio
async def test_crash_after_the_cutoff_stops_for_the_day(db_path):
    garmin = FakeGarmin(RuntimeError("always"))
    await run_scheduler(db_path, local(D, 9, 20), garmin, until=local(D, 23, 0))
    assert garmin.calls == 2  # 09:20 and the final attempt at 09:35; no crash loop until midnight


@pytest.mark.anyio
async def test_failing_push_server_does_not_crash(db_path, tmp_path):
    clock_ref: dict[str, FakeClock] = {}
    times: list[datetime] = []
    answers = iter([500, "connect", 200])

    def handler(request: httpx2.Request) -> httpx2.Response:
        times.append(clock_ref["clock"].now())
        answer = next(answers)
        if answer == "connect":
            raise httpx2.ConnectError("down")
        return httpx2.Response(answer)

    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN + "\n", encoding="utf-8")
    settings = make_settings(db_path, telegram_token_file=token_file)
    clock = FakeClock(local(D, 0), stop_when=lambda: len(times) >= 3 and Store(db_path).briefing_sent(D))
    clock_ref["clock"] = clock
    scheduler = BriefingScheduler(
        settings,
        Store(db_path),
        FakeGarmin(garmin_data()),
        TelegramPush(settings, transport=httpx2.MockTransport(handler)),
        now=clock.now,
        sleep=clock.sleep,
    )
    with pytest.raises(StopLoop):
        await scheduler.run()
    assert [t.astimezone(PRAGUE).strftime("%H:%M") for t in times] == ["06:30", "06:45", "07:00"]


@pytest.mark.anyio
async def test_dst_fall_back_and_spring_forward(db_path, tmp_path):
    # Fall back: 2026-10-25 has 25 hours. 06:30 CEST on the 24th -> 06:30 CET on the 25th.
    push, _ = await run_scheduler(db_path, local(date(2026, 10, 24), 0), FakeGarmin(garmin_data()), stop_pushes=2)
    first, second = (utc(t) for t, _ in push.sent)
    assert first == datetime(2026, 10, 24, 4, 30, tzinfo=UTC)  # 06:30 CEST
    assert second == datetime(2026, 10, 25, 5, 30, tzinfo=UTC)  # 06:30 CET
    assert second - first == timedelta(hours=25)
    assert second.astimezone(PRAGUE).strftime("%H:%M") == "06:30"

    # Spring forward: 02:30 does not exist on 2027-03-28; the push goes out at the next valid instant.
    import datetime as dt

    spring_db = tmp_path / "spring.db"
    push, _ = await run_scheduler(
        spring_db,
        local(date(2027, 3, 28), 0),
        FakeGarmin(garmin_data()),
        stop_pushes=1,
        briefing_time=dt.time(2, 30),
        briefing_cutoff=dt.time(9, 30),
    )
    (sent_at, _msg), = push.sent
    assert utc(sent_at) == datetime(2027, 3, 28, 1, 30, tzinfo=UTC)
    assert sent_at.astimezone(PRAGUE).strftime("%H:%M") == "03:30"


@pytest.mark.anyio
async def test_dst_delays_are_computed_in_utc(db_path):
    """tick() returns the real number of seconds, also across a clock change (a wall-clock subtraction would not)."""
    day = date(2026, 10, 24)
    store = Store(db_path)
    store.record_briefing_sent(day, "green", {})
    clock = FakeClock(local(day, 7, 0))
    scheduler = BriefingScheduler(
        make_settings(db_path), store, FakeGarmin(garmin_data()), FakePush(), now=clock.now, sleep=clock.sleep
    )
    # 07:00 CEST (05:00 UTC) on the 24th -> 06:30 CET (05:30 UTC) on the 25th is 24 h 30 min, not 23 h 30 min.
    assert await scheduler.tick() == 24.5 * 3600

    # Spring forward: 22:00 CET on 2027-03-27 (21:00 UTC) to 06:30 CEST on the 28th (04:30 UTC) is 7 h 30 min.
    store.record_briefing_sent(date(2027, 3, 27), "green", {})
    clock = FakeClock(local(date(2027, 3, 27), 22, 0))  # 21:00 UTC
    scheduler = BriefingScheduler(
        make_settings(db_path), store, FakeGarmin(garmin_data()), FakePush(), now=clock.now, sleep=clock.sleep
    )
    assert await scheduler.tick() == 7.5 * 3600


# ------------------------------------------------------------ alert state

ALERT_TEXT = {
    "hrv_below_baseline": "HRV pod baseline",
    "resting_hr_elevated": "Klidová tepovka",
    "acwr_out_of_range": "Poměr akutní a chronické zátěže",
    "missed_workout": "Včera naplánovaný trénink",
    "garmin_auth": "Garmin není přihlášený: na serveru",
}


def alert_data(key: str, day: date, holds: bool):
    """Green data in which the alert ``key`` holds (or does not) on ``day``."""
    snapshot = {**GREEN_SNAPSHOT}
    hrv_trend = trend(days_range(day - timedelta(days=6), day, 60))
    sched = scheduled([])
    auth = "ok"
    if key == "hrv_below_baseline":
        days = days_range(day - timedelta(days=6), day, 60)
        if holds:
            for offset in range(3):
                days[(day - timedelta(days=offset)).isoformat()] = 40
        hrv_trend = trend(days)
        snapshot["hrv"] = {"last_night_avg_ms": 40 if holds else 60, "baseline_balanced_ms": [52, 68]}
    elif key == "resting_hr_elevated":
        snapshot["resting_hr"] = {"today": 60 if holds else 50, "avg_7d": 50}
    elif key == "acwr_out_of_range":
        snapshot["training_status"] = {"acwr_ratio": 1.7, "acwr_status": "HIGH" if holds else "OPTIMAL"}
    elif key == "missed_workout":
        sched = scheduled([{"date": (day - timedelta(days=1)).isoformat(), "name": "Run", "completed": not holds}])
    else:
        auth = "missing" if holds else "ok"
    return garmin_data(auth=auth, snapshot=snapshot, hrv_trend=hrv_trend, scheduled=sched)


# Event alerts (yesterday's missed workout) are reported on every day they hold; see the test below.
STATE_ALERT_KEYS = tuple(k for k in ALERT_KEYS if k not in EVENT_ALERTS)


@pytest.mark.anyio
@pytest.mark.parametrize("key", STATE_ALERT_KEYS)
async def test_alert_fires_once_then_not_again(db_path, key):
    async def day_run(day: date, holds: bool, from_hour: int = 0, to_hour: int = 12) -> list[str]:
        garmin = FakeGarmin(lambda d, i: alert_data(key, d, holds))
        push, _ = await run_scheduler(
            db_path, local(day, from_hour), garmin, until=local(day, to_hour), store=Store(db_path)
        )
        return [msg.body for _, msg in push.sent]

    text = ALERT_TEXT[key]
    d1 = D
    # 1. the condition holds: the push carries the alert and the state remembers it
    (message,) = await day_run(d1, True)
    assert text in message and "Upozornění:" in message
    assert Store(db_path).alert_states()[key] == {
        "value": 1,
        "evaluated_on": d1.isoformat(),
        "fired_on": d1.isoformat(),
    }
    # 2. a restart the same day sends nothing
    assert await day_run(d1, True, from_hour=7, to_hour=23) == []
    # 3. the next day it still holds: no repeat
    (message,) = await day_run(d1 + timedelta(days=1), True)
    assert text not in message
    assert Store(db_path).alert_states()[key] == {
        "value": 1,
        "evaluated_on": (d1 + timedelta(days=1)).isoformat(),
        "fired_on": d1.isoformat(),
    }
    # 4. it stops holding
    (message,) = await day_run(d1 + timedelta(days=2), False)
    assert text not in message
    assert Store(db_path).alert_states()[key]["value"] == 0
    # 5. it holds again: the alert is back
    (message,) = await day_run(d1 + timedelta(days=3), True)
    assert text in message
    assert Store(db_path).alert_states()[key]["fired_on"] == (d1 + timedelta(days=3)).isoformat()


@pytest.mark.anyio
async def test_missed_workout_reports_each_newly_missed_session(db_path):
    def missed(on: date, name: str, completed: bool = False):
        entry = {"date": on.isoformat(), "name": name, "completed": completed}
        return garmin_data(scheduled=scheduled([entry]))

    async def day_run(day: date, data, from_hour: int = 0, to_hour: int = 12) -> list[str]:
        push, _ = await run_scheduler(
            db_path, local(day, from_hour), FakeGarmin(data), until=local(day, to_hour), store=Store(db_path)
        )
        return [msg.body for _, msg in push.sent]

    d1, d2, d3 = D, D + timedelta(days=1), D + timedelta(days=2)
    text = "Včera naplánovaný trénink nebyl splněn"
    # 1. yesterday's session was missed
    (message,) = await day_run(d1, missed(d1 - timedelta(days=1), "Intervaly 6×800 m"))
    assert f"{text}: Intervaly 6×800 m." in message
    # 2. a restart on the same day sends nothing
    assert await day_run(d1, missed(d1 - timedelta(days=1), "Intervaly 6×800 m"), from_hour=7, to_hour=23) == []
    # 3. the next day another session was missed: it is reported, the old one is not
    (message,) = await day_run(d2, missed(d1, "Tempo 10 km"))
    assert f"{text}: Tempo 10 km." in message and "Intervaly" not in message
    assert Store(db_path).alert_states()["missed_workout"]["fired_on"] == d2.isoformat()
    # 4. nothing missed the day after: no text
    (message,) = await day_run(d3, missed(d2, "Tempo 10 km", completed=True))
    assert text not in message
    assert Store(db_path).alert_states()["missed_workout"]["value"] == 0


@pytest.mark.anyio
async def test_missing_baseline_sends_at_the_briefing_time(db_path):
    garmin = FakeGarmin(garmin_data(snapshot={**GREEN_SNAPSHOT, "hrv": {"last_night_avg_ms": 62}}))
    push, _ = await run_scheduler(db_path, local(D, 0), garmin, stop_pushes=1)
    assert garmin.calls == 1
    (sent_at, msg), = push.sent
    assert utc(sent_at) == local(D, 6, 30).astimezone(UTC)
    assert "Chybí: HRV baseline (Garmin ji počítá až po zhruba 3 týdnech" in msg.body


@pytest.mark.anyio
async def test_store_error_before_the_first_attempt_is_not_a_late_start(db_path, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    store = Store(db_path)
    real = store.briefing_sent
    calls = {"n": 0}

    def flaky(day):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("locked")
        return real(day)

    monkeypatch.setattr(store, "briefing_sent", flaky)
    push, _ = await run_scheduler(db_path, local(D, 9, 20), FakeGarmin(garmin_data()), stop_pushes=1, store=store)
    assert "briefing status=crashed kind=OperationalError" in caplog.text
    assert "late_start" not in caplog.text
    assert [utc(t) for t, _ in push.sent] == [local(D, 9, 35).astimezone(UTC)]


@pytest.mark.anyio
async def test_store_error_on_a_late_first_look_is_still_a_late_start(db_path, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    store = Store(db_path)
    real = store.briefing_sent
    calls = {"n": 0}

    def flaky(day):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("locked")
        return real(day)

    monkeypatch.setattr(store, "briefing_sent", flaky)
    garmin = FakeGarmin(garmin_data())
    push, _ = await run_scheduler(db_path, local(D, 10, 0), garmin, stop_pushes=1, store=store)
    assert "briefing status=crashed kind=OperationalError" in caplog.text
    assert "briefing status=skipped reason=late_start" in caplog.text
    # nothing was sent on the day the service started after the cutoff
    assert [utc(t) for t, _ in push.sent] == [local(D + timedelta(days=1), 6, 30).astimezone(UTC)]
    assert garmin.days == [D + timedelta(days=1)]


@pytest.mark.anyio
async def test_unknown_condition_does_not_change_alert_state(db_path):
    store = Store(db_path)
    store.record_briefing_sent(D - timedelta(days=1), "green", {"acwr_out_of_range": (True, True)})
    before = store.alert_states()["acwr_out_of_range"]
    snapshot = {**GREEN_SNAPSHOT, "training_status": {"acwr_status": "NONE"}}
    garmin = FakeGarmin(garmin_data(snapshot=snapshot))
    await run_scheduler(db_path, local(D, 0), garmin, stop_pushes=1)
    assert Store(db_path).alert_states()["acwr_out_of_range"] == before


# ------------------------------------------------------------------ the CLI


@pytest.mark.anyio
async def test_send_briefing_command_pushes_without_writing_state(db_path, capsys):
    store = Store(db_path)
    push = FakePush()
    rc = await send_briefing_command(
        make_settings(db_path),
        store=store,
        garmin=FakeGarmin(garmin_data(snapshot={**GREEN_SNAPSHOT, "resting_hr": {"today": 60, "avg_7d": 50}})),
        push=push,
        today=lambda: D,
    )
    assert rc == 0
    assert len(push.sent) == 1 and "Klidová tepovka" in push.sent[0][1].body
    out = capsys.readouterr()
    assert out.out == "briefing status=sent verdict=green missing=- new_alerts=1\n"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM briefing_runs").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM alert_state").fetchone()[0] == 0


@pytest.mark.anyio
async def test_send_briefing_command_reports_missing_and_failures(db_path, capsys):
    snap = {k: v for k, v in GREEN_SNAPSHOT.items() if k not in ("hrv", "body_battery")}
    rc = await send_briefing_command(
        make_settings(db_path),
        store=Store(db_path),
        garmin=FakeGarmin(garmin_data(snapshot=snap)),
        push=FakePush(["transient"]),
        today=lambda: D,
    )
    assert rc == 1
    assert capsys.readouterr().out == "briefing status=transient verdict=yellow missing=hrv,body_battery new_alerts=0\n"
    rc = await send_briefing_command(
        make_settings(db_path),
        store=Store(db_path),
        garmin=FakeGarmin(RuntimeError("x")),
        push=FakePush(),
        today=lambda: D,
    )
    assert rc == 1
    assert capsys.readouterr().out == ""


@pytest.mark.anyio
async def test_send_briefing_command_uses_the_configured_zone_for_today(db_path):
    garmin = FakeGarmin(garmin_data())
    rc = await send_briefing_command(make_settings(db_path), store=Store(db_path), garmin=garmin, push=FakePush())
    assert rc == 0 and len(garmin.days) == 1 and isinstance(garmin.days[0], date)


@pytest.mark.anyio
async def test_send_briefing_command_without_push_config_exits_2(db_path, capsys):
    garmin = FakeGarmin(garmin_data())
    rc = await send_briefing_command(
        Settings(db_path=db_path), store=Store(db_path), garmin=garmin, push=FakePush(), today=lambda: D
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "push is not configured: set COACH_TELEGRAM_CHAT_ID\n"
    assert garmin.calls == 0


@pytest.mark.anyio
async def test_send_briefing_command_with_unusable_token_reports_permanent(db_path, tmp_path, capsys):
    settings = make_settings(db_path, telegram_token_file=tmp_path / "missing")
    rc = await send_briefing_command(settings, store=Store(db_path), garmin=FakeGarmin(garmin_data()), today=lambda: D)
    assert rc == 1
    assert capsys.readouterr().out.startswith("briefing status=permanent verdict=green")


def test_main_dispatch_usage(capsys, monkeypatch, restore_logging):
    assert main(["bogus"]) == 2
    assert capsys.readouterr().err == "usage: python -m coach_mcp [send-briefing | backup | restore [PATH]]\n"
    assert main(["send-briefing", "extra"]) == 2
    monkeypatch.delenv("COACH_TELEGRAM_CHAT_ID", raising=False)
    capsys.readouterr()
    assert main(["send-briefing"]) == 2
    assert "push is not configured" in capsys.readouterr().err


# ------------------------------------------------------------------ privacy


@pytest.mark.anyio
async def test_logs_never_contain_text_chat_id_token_or_values(db_path, tmp_path, caplog, restore_logging):
    configure_logging("DEBUG")
    caplog.set_level(logging.DEBUG)
    telegram_token = tmp_path / "telegram_bot_token"
    telegram_token.write_text(BOT_TOKEN + "\n", encoding="utf-8")
    garmin_token = tmp_path / "garmin_token"
    garmin_token.write_text("garmin-backend-token-0123456789abcdef\n", encoding="utf-8")
    settings = make_settings(
        db_path,
        telegram_chat_id="918273645",
        telegram_token_file=telegram_token,
        garmin_token_file=garmin_token,
    )
    store = Store(db_path)
    store.replace_planned(
        D, D, D, [{"date": D, "type": "running", "description": "PRIVATE-SESSION-TEXT", "duration_min": 40}]
    )
    answers = iter([500, 200])
    clock_ref: dict[str, FakeClock] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(next(answers))

    app, _calls = fake_garmin_app("garmin-backend-token-0123456789abcdef", errors={"get_hrv_trend": "SECRET-ERROR-TEXT"})
    clock = FakeClock(local(D, 0), stop_when=lambda: store.briefing_sent(D))
    clock_ref["clock"] = clock
    scheduler = BriefingScheduler(
        settings,
        store,
        GarminReader(settings, transport=httpx2.ASGITransport(app=app)),
        TelegramPush(settings, transport=httpx2.MockTransport(handler)),
        now=clock.now,
        sleep=clock.sleep,
    )
    async with fake_garmin_running(app):
        with pytest.raises(StopLoop):
            await scheduler.run()
    assert store.briefing_sent(D)

    text = caplog.text
    assert "briefing status=sent" in text
    assert "push status=transient code=500" in text
    assert "garmin status=partial" in text
    for secret in (
        "SECRETtoken",
        "918273645",
        "api.telegram.org",
        "/bot",
        "sendMessage",
        "PRIVATE-SESSION-TEXT",
        "SECRET-ERROR-TEXT",
        "Readiness",
        "ranní briefing",
        "garmin-backend-token",
    ):
        assert secret not in text, secret


@pytest.mark.anyio
async def test_compose_briefing_reads_plan_and_alert_rows(db_path):
    store = Store(db_path)
    store.replace_planned(D, D, D, [{"date": D, "type": "running", "description": "Easy", "duration_min": 40}])
    store.record_briefing_sent(
        D - timedelta(days=1), "green", {"resting_hr_elevated": (True, True)}
    )
    garmin = FakeGarmin(garmin_data(snapshot={**GREEN_SNAPSHOT, "resting_hr": {"today": 60, "avg_7d": 50}}))
    briefing = await compose_briefing(store, garmin, D)
    assert [p["description"] for p in briefing.payload["planned"]] == ["Easy"]
    rhr = next(a for a in briefing.payload["alerts"] if a["key"] == "resting_hr_elevated")
    assert rhr == {"key": "resting_hr_elevated", "holds": True, "new": False}  # held yesterday: not new
