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
from coach_mcp.garmin import GarminReader, LastSync
from coach_mcp.push import TelegramPush
from coach_mcp.scheduler import (
    SETTLE,
    BriefingScheduler,
    compose_briefing,
    reusable,
    send_briefing_command,
    send_weekly_report_command,
)
from coach_mcp.store import Store
from garmin_payloads import (
    GREEN_SNAPSHOT,
    device_status,
    days_range,
    garmin_data,
    scheduled,
    trend,
)

BOT_TOKEN = "123456789:SECRETtokenSECRETtokenSECRETtoken12"  # 35 characters after the colon
PRAGUE = ZoneInfo("Europe/Prague")
D = date(2026, 10, 7)
M = date(2026, 10, 12)  # a Monday: the Monday report of ISO week 2026-W41 (5-11 Oct)


def local(day: date, hour: int, minute: int = 0, zone: ZoneInfo = PRAGUE) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)


def make_settings(db_path: Path, **overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "db_path": db_path,
        "timezone": "Europe/Prague",
        "telegram_chat_id": "123456789",
        "language": "cs",  # the message assertions below are in Czech
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
async def test_a_briefing_crash_waits_the_full_retry_interval(db_path, caplog):
    caplog.set_level(logging.INFO)

    def data(day: date, index: int):
        return RuntimeError("boom") if index == 0 else garmin_data()

    push, _ = await run_scheduler(db_path, local(D, 9, 20), FakeGarmin(data), stop_pushes=1)
    # the loop logs the crash and waits 15 minutes, past the cutoff (not capped at 09:30 like a transient failure)
    assert [utc(t) for t, _ in push.sent] == [local(D, 9, 35).astimezone(UTC)]
    assert "briefing status=crashed kind=RuntimeError" in caplog.text and "weekly" not in caplog.text


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
async def test_missed_planned_session_reports_each_day_through_the_store(db_path):
    store = Store(db_path)
    d1, d2, d3 = D, D + timedelta(days=1), D + timedelta(days=2)
    day_before = D - timedelta(days=1)
    ids = store.replace_planned(
        day_before,
        d3,
        day_before,
        [
            {"date": day_before, "type": "running", "description": "Intervaly"},
            {"date": d1, "type": "running", "description": "Tempo"},
            {"date": d2, "type": "running", "description": "Lehký"},
        ],
    )[1]
    store.upsert_suggestions(
        [(ids[0], "no_activity", False, None), (ids[1], "no_activity", False, None)]
        + [(ids[2], "matched", False, '{"count":1}')],
        d2,
    )

    async def day_run(day: date, from_hour: int = 0, to_hour: int = 12) -> list[str]:
        push, _ = await run_scheduler(
            db_path, local(day, from_hour), FakeGarmin(garmin_data(scheduled=scheduled([]))),
            until=local(day, to_hour), store=Store(db_path),
        )
        return [msg.body for _, msg in push.sent]

    text = "Včera v plánu bez aktivity v Garminu"
    # 1. yesterday's planned session has no activity
    (message,) = await day_run(d1)
    assert f"{text}: běh – Intervaly." in message
    assert Store(db_path).alert_states()["missed_planned_session"]["fired_on"] == d1.isoformat()
    # 2. a restart on the same day sends nothing
    assert await day_run(d1, from_hour=7, to_hour=23) == []
    # 3. the next day another session has none: it is reported, the old one is not
    (message,) = await day_run(d2)
    assert f"{text}: běh – Tempo." in message and "Intervaly" not in message
    assert Store(db_path).alert_states()["missed_planned_session"]["fired_on"] == d2.isoformat()
    # 4. the day after, yesterday's session was matched: no text
    (message,) = await day_run(d3)
    assert text not in message
    state = Store(db_path).alert_states()["missed_planned_session"]
    assert state["value"] == 0 and state["fired_on"] == d2.isoformat()


@pytest.mark.anyio
async def test_missing_baseline_sends_at_the_briefing_time(db_path):
    garmin = FakeGarmin(garmin_data(snapshot={**GREEN_SNAPSHOT, "hrv": {"last_night_avg_ms": 62}}))
    push, _ = await run_scheduler(db_path, local(D, 0), garmin, stop_pushes=1)
    assert garmin.calls == 1
    (sent_at, msg), = push.sent
    assert utc(sent_at) == local(D, 6, 30).astimezone(UTC)
    assert "Chybí: HRV baseline (Garmin ji počítá až po zhruba 3 týdnech" in msg.body


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
        Settings(db_path=db_path, language="cs"), store=Store(db_path), garmin=garmin, push=FakePush(), today=lambda: D
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
    usage = "usage: python -m coach_mcp [send-briefing | send-weekly-report | backup | restore [PATH]]\n"
    assert capsys.readouterr().err == usage
    assert main(["send-briefing", "extra"]) == 2
    assert main(["send-weekly-report", "x"]) == 2
    monkeypatch.delenv("COACH_TELEGRAM_CHAT_ID", raising=False)
    capsys.readouterr()
    assert main(["send-briefing"]) == 2
    assert "push is not configured" in capsys.readouterr().err
    assert main(["send-weekly-report"]) == 2
    assert capsys.readouterr().err == "push is not configured: set COACH_TELEGRAM_CHAT_ID\n"


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
        M, M, M, [{"date": M, "type": "running", "description": "PRIVATE-SESSION-TEXT", "duration_min": 40}]
    )
    store.insert_goal({"title": "PRIVATE-GOAL-TITLE", "metric": "race_10k", "target_value": 2700.0})
    answers = iter([500, 200, 200])  # the briefing is retried once; the third answer is the Monday message
    clock_ref: dict[str, FakeClock] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(next(answers))

    app, _calls = fake_garmin_app("garmin-backend-token-0123456789abcdef", errors={"get_hrv_trend": "SECRET-ERROR-TEXT"})
    clock = FakeClock(local(M, 0), stop_when=lambda: store.weekly_report_sent("2026-W41"))
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
    assert store.briefing_sent(M)

    text = caplog.text
    assert "briefing status=sent" in text
    assert "weekly status=sent" in text
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
        "PRIVATE-GOAL-TITLE",
        "Týdenní report",
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


# ------------------------------------------------------------ the Monday report (PR 14)


def weekly_titles(push: FakePush) -> list[str]:
    return [msg.title for _, msg in push.sent if "Týdenní report" in msg.title]


MONDAY_TITLE = "\U0001f4ca Týdenní report 2026-W41 · 5. 10.–11. 10."


@pytest.mark.anyio
async def test_monday_report_follows_the_briefing_once(db_path):
    push, _ = await run_scheduler(db_path, local(M, 0), FakeGarmin(garmin_data()), stop_pushes=2)
    assert [utc(t) for t, _ in push.sent] == [local(M, 6, 30).astimezone(UTC)] * 2
    assert "ranní briefing" in push.sent[0][1].title
    assert push.sent[1][1].title == MONDAY_TITLE
    assert "Pro tento týden nejsou uložená data" in push.sent[1][1].body
    store = Store(db_path)
    assert store.weekly_report_sent("2026-W41") and store.briefing_sent(M)


@pytest.mark.anyio
async def test_no_monday_report_on_other_days(db_path):
    push, _ = await run_scheduler(db_path, local(D, 0), FakeGarmin(garmin_data()), until=local(D, 12))
    assert len(push.sent) == 1 and weekly_titles(push) == []
    assert Store(db_path).weekly_report_sent("2026-W40") is False


@pytest.mark.anyio
async def test_weekly_push_off_sends_only_the_briefing(db_path):
    push, _ = await run_scheduler(
        db_path, local(M, 0), FakeGarmin(garmin_data()), until=local(M, 12), weekly_push=False
    )
    assert len(push.sent) == 1 and weekly_titles(push) == []


@pytest.mark.anyio
async def test_consecutive_mondays_each_send_their_own_week(db_path):
    first, _ = await run_scheduler(db_path, local(M, 0), FakeGarmin(garmin_data()), stop_pushes=2)
    nxt = M + timedelta(days=7)
    second, _ = await run_scheduler(db_path, local(nxt, 0), FakeGarmin(garmin_data()), stop_pushes=2)
    assert "Týdenní report 2026-W41 ·" in weekly_titles(first)[0]
    assert "Týdenní report 2026-W42 ·" in weekly_titles(second)[0]
    store = Store(db_path)
    assert store.weekly_report_sent("2026-W41") and store.weekly_report_sent("2026-W42")


@pytest.mark.anyio
async def test_restart_after_both_messages_sends_nothing(db_path):
    await run_scheduler(db_path, local(M, 0), FakeGarmin(garmin_data()), stop_pushes=2)
    push, _ = await run_scheduler(db_path, local(M, 7), FakeGarmin(garmin_data()), until=local(M, 23))
    assert push.sent == []


@pytest.mark.anyio
async def test_restart_after_the_briefing_sends_the_report(db_path):
    Store(db_path).record_briefing_sent(M, "green", {})
    garmin = FakeGarmin(garmin_data())
    push, _ = await run_scheduler(db_path, local(M, 7), garmin, until=local(M, 12))
    assert [msg.title for _, msg in push.sent] == [MONDAY_TITLE]
    assert [utc(t) for t, _ in push.sent] == [local(M, 7).astimezone(UTC)]
    assert garmin.calls == 0  # the briefing is not composed again


@pytest.mark.anyio
async def test_late_start_sends_no_monday_report(db_path, caplog):
    caplog.set_level(logging.INFO)
    Store(db_path).record_briefing_sent(M, "green", {})
    push, _ = await run_scheduler(db_path, local(M, 10), FakeGarmin(garmin_data()), until=local(M, 23))
    assert push.sent == []
    assert "weekly status=skipped reason=late_start" in caplog.text
    caplog.clear()
    push, _ = await run_scheduler(
        db_path.with_name("fresh.db"), local(M, 10), FakeGarmin(garmin_data()), until=local(M, 23)
    )
    assert push.sent == []
    assert "briefing status=skipped reason=late_start" in caplog.text and "weekly status" not in caplog.text


@pytest.mark.anyio
async def test_transient_monday_failure_retries_like_the_briefing(db_path, caplog):
    caplog.set_level(logging.INFO)
    clock_push = FakePush(["sent", "transient", "sent"])
    push, _ = await run_scheduler(db_path, local(M, 0), FakeGarmin(garmin_data()), clock_push, stop_pushes=3)
    assert [utc(t) for t, _ in push.sent] == [local(M, 6, 30).astimezone(UTC)] * 2 + [local(M, 6, 45).astimezone(UTC)]
    assert [m.title for _, m in push.sent][1:] == [MONDAY_TITLE] * 2
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM briefing_runs").fetchone()[0] == 1
    assert Store(db_path).weekly_report_sent("2026-W41")
    # the report counts its own attempts, not the briefing's
    assert "weekly status=waiting attempt=1" in caplog.text and "weekly status=sent attempt=2" in caplog.text
    assert "briefing status=sent attempt=1" in caplog.text


@pytest.mark.anyio
async def test_permanent_monday_failure_gives_up(db_path, caplog):
    caplog.set_level(logging.INFO)
    push, _ = await run_scheduler(
        db_path, local(M, 0), FakeGarmin(garmin_data()), FakePush(["sent", "permanent"]), until=local(M, 23)
    )
    assert len(push.sent) == 2
    assert Store(db_path).weekly_report_sent("2026-W41") is False
    assert "weekly status=gave_up reason=push_permanent" in caplog.text


@pytest.mark.anyio
async def test_monday_report_gives_up_at_the_cutoff(db_path, caplog):
    caplog.set_level(logging.INFO)
    push, _ = await run_scheduler(
        db_path, local(M, 0), FakeGarmin(garmin_data()), FakePush(["sent", "transient"]), until=local(M + timedelta(days=1), 0)
    )
    assert len(push.sent) == 14  # the briefing and 13 attempts: 06:30, 06:45, ..., 09:30
    times = [utc(t) for t, _ in push.sent]
    assert times[1] == local(M, 6, 30).astimezone(UTC) and times[-1] == local(M, 9, 30).astimezone(UTC)
    assert "weekly status=gave_up reason=push_transient" in caplog.text
    assert Store(db_path).weekly_report_sent("2026-W41") is False


@pytest.mark.anyio
async def test_no_monday_report_after_a_briefing_that_gave_up(db_path):
    push, _ = await run_scheduler(
        db_path, local(M, 0), FakeGarmin(garmin_data()), FakePush(["permanent"]), until=local(M, 23)
    )
    assert len(push.sent) == 1 and "ranní briefing" in push.sent[0][1].title
    assert Store(db_path).weekly_report_sent("2026-W41") is False


@pytest.mark.anyio
async def test_monday_report_waits_for_a_briefing_that_waits_for_sleep(db_path):
    def data(day: date, index: int):
        if index < 2:
            return garmin_data(snapshot={k: v for k, v in GREEN_SNAPSHOT.items() if k != "sleep"})
        return garmin_data()

    push, _ = await run_scheduler(db_path, local(M, 0), FakeGarmin(data), stop_pushes=2)
    assert [utc(t) for t, _ in push.sent] == [local(M, 7, 0).astimezone(UTC)] * 2
    assert "ranní briefing" in push.sent[0][1].title and push.sent[1][1].title == MONDAY_TITLE


@pytest.mark.anyio
async def test_briefing_sent_at_the_cutoff_gets_one_final_report_attempt(db_path, caplog):
    caplog.set_level(logging.INFO)
    garmin = FakeGarmin(garmin_data(snapshot={k: v for k, v in GREEN_SNAPSHOT.items() if k != "hrv"}))
    push, _ = await run_scheduler(
        db_path, local(M, 0), garmin, FakePush(["sent", "transient"]), until=local(M + timedelta(days=1), 0)
    )
    assert [utc(t) for t, _ in push.sent] == [local(M, 9, 30).astimezone(UTC)] * 2
    assert "weekly status=gave_up reason=push_transient" in caplog.text


@pytest.mark.anyio
async def test_a_crash_sending_the_report_is_logged_as_weekly_and_retried(db_path, caplog):
    caplog.set_level(logging.INFO)
    push = FakePush(["sent", RuntimeError("boom"), "sent"])
    push, _ = await run_scheduler(db_path, local(M, 0), FakeGarmin(garmin_data()), push, stop_pushes=3)
    assert [utc(t) for t, _ in push.sent] == [local(M, 6, 30).astimezone(UTC)] * 2 + [local(M, 6, 45).astimezone(UTC)]
    assert [m.title for _, m in push.sent][1:] == [MONDAY_TITLE] * 2
    assert "weekly status=crashed kind=RuntimeError" in caplog.text and "boom" not in caplog.text
    assert "briefing status=crashed" not in caplog.text
    assert Store(db_path).weekly_report_sent("2026-W41")


@pytest.mark.anyio
async def test_a_recorded_week_is_not_sent_again(db_path):
    Store(db_path).record_weekly_report_sent("2026-W41")
    push, _ = await run_scheduler(db_path, local(M, 0), FakeGarmin(garmin_data()), until=local(M, 23))
    assert len(push.sent) == 1 and "ranní briefing" in push.sent[0][1].title


@pytest.mark.anyio
async def test_the_monday_report_has_goals_and_the_week_numbers(db_path):
    store = Store(db_path)
    store.insert_goal({"title": "Plain goal"})
    push, _ = await run_scheduler(db_path, local(M, 0), FakeGarmin(garmin_data()), stop_pushes=2, store=store)
    assert "Cíle bez měřitelné metriky: 1." in push.sent[1][1].body


@pytest.mark.anyio
async def test_send_weekly_report_command_pushes_without_writing_state(db_path, capsys):
    push = FakePush()
    rc = await send_weekly_report_command(make_settings(db_path), store=Store(db_path), push=push, today=lambda: M)
    assert rc == 0 and [m.title for _, m in push.sent] == [MONDAY_TITLE]
    assert capsys.readouterr().out == "weekly status=sent week=2026-W41 goals=0\n"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM weekly_reports").fetchone()[0] == 0
    # it ignores COACH_WEEKLY_PUSH and reports the last complete week on any weekday
    rc = await send_weekly_report_command(
        make_settings(db_path, weekly_push=False), store=Store(db_path), push=FakePush(), today=lambda: D
    )
    assert rc == 0 and capsys.readouterr().out == "weekly status=sent week=2026-W40 goals=0\n"


@pytest.mark.anyio
async def test_send_weekly_report_command_counts_goals(db_path, capsys):
    store = Store(db_path)
    store.insert_goal({"title": "G", "metric": "race_5k"})
    store.insert_goal({"title": "Plain"})
    rc = await send_weekly_report_command(make_settings(db_path), store=store, push=FakePush(), today=lambda: M)
    assert rc == 0 and capsys.readouterr().out == "weekly status=sent week=2026-W41 goals=1\n"


@pytest.mark.anyio
async def test_send_weekly_report_command_failures(db_path, capsys):
    rc = await send_weekly_report_command(
        make_settings(db_path), store=Store(db_path), push=FakePush(["transient"]), today=lambda: M
    )
    assert rc == 1 and capsys.readouterr().out == "weekly status=transient week=2026-W41 goals=0\n"


@pytest.mark.anyio
async def test_send_weekly_report_command_without_push_config_exits_2(db_path, capsys):
    push = FakePush()
    rc = await send_weekly_report_command(
        Settings(db_path=db_path, language="cs"), store=Store(db_path), push=push, today=lambda: M
    )
    assert rc == 2 and push.sent == []
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "push is not configured: set COACH_TELEGRAM_CHAT_ID\n"


@pytest.mark.anyio
async def test_no_monday_report_on_the_tuesday_after_a_monday_that_gave_up(db_path):
    tuesday = M + timedelta(days=1)
    assert Store(db_path).weekly_report_sent("2026-W41") is False  # Monday gave up: W41 is still unsent
    push, _ = await run_scheduler(db_path, local(tuesday, 0), FakeGarmin(garmin_data()), until=local(tuesday, 12))
    assert len(push.sent) == 1 and "ranní briefing" in push.sent[0][1].title
    assert Store(db_path).weekly_report_sent("2026-W41") is False


# --------------------------------------------- the retry gate (PR 17)
# Defaults: attempts at 06:30, 06:45, ..., 09:30 (13), sleep missing all morning unless a test says otherwise.

NO_SLEEP = {k: v for k, v in GREEN_SNAPSHOT.items() if k != "sleep"}
SYNC_0500 = LastSync(datetime(2026, 10, 7, 3, 0, tzinfo=UTC), datetime(2026, 10, 7, 5, 0))
DEVICE_0500 = device_status("2026-10-07T03:00:00Z", "2026-10-07T05:00")


def no_sleep(device: Any = DEVICE_0500, **overrides: Any):
    return garmin_data(snapshot=NO_SLEEP, device=device, **overrides)


def sync_at(local_hour: int, local_minute: int) -> tuple[LastSync, dict[str, Any]]:
    """The LastSync and device payload of an upload on D at the given Prague wall time (UTC+2 in October)."""
    wall = datetime(2026, 10, 7, local_hour, local_minute)
    moment = (wall - timedelta(hours=2)).replace(tzinfo=UTC)
    return LastSync(moment, wall), device_status(moment.strftime("%Y-%m-%dT%H:%M:00Z"), wall.strftime("%Y-%m-%dT%H:%M"))


@pytest.mark.anyio
async def test_a_retry_without_a_new_upload_reads_only_the_sync_time(db_path, caplog):
    caplog.set_level(logging.INFO)
    garmin = FakeGarmin(no_sleep(), sync=SYNC_0500)
    push, _ = await run_scheduler(db_path, local(D, 0), garmin, stop_pushes=1)
    assert garmin.calls == 1 and garmin.sync_calls == 12
    (sent_at, msg), = push.sent
    assert utc(sent_at) == local(D, 9, 30).astimezone(UTC)
    assert "(poslední synchronizace s Garminem: dnes 5:00)" in msg.body
    assert caplog.text.count("briefing garmin=unchanged") == 12


@pytest.mark.anyio
async def test_a_new_upload_triggers_a_full_read(db_path):
    fresh_last, fresh_device = sync_at(7, 5)

    def data(day: date, index: int):
        return no_sleep() if index == 0 else garmin_data(device=fresh_device)

    garmin = FakeGarmin(data, sync=lambda index: SYNC_0500 if index < 2 else fresh_last)
    push, _ = await run_scheduler(db_path, local(D, 0), garmin, stop_pushes=1)
    assert garmin.calls == 2 and garmin.sync_calls == 3
    (sent_at, msg), = push.sent
    assert utc(sent_at) == local(D, 7, 15).astimezone(UTC)
    assert "Chybí" not in msg.body


@pytest.mark.anyio
async def test_an_upload_that_had_not_settled_is_read_again(db_path):
    last, device = sync_at(6, 20)  # the first read (06:30) and the second (06:45) start within 30 minutes of it
    garmin = FakeGarmin(no_sleep(device), sync=last)
    await run_scheduler(db_path, local(D, 0), garmin, stop_pushes=1)
    assert garmin.calls == 3  # 06:30, 06:45, 07:00; the 07:00 read is 40 minutes after the upload
    assert garmin.sync_calls == 12


@pytest.mark.anyio
async def test_a_read_exactly_the_settle_time_after_the_upload_is_reused(db_path):
    assert SETTLE == timedelta(minutes=30)
    last, device = sync_at(6, 0)
    garmin = FakeGarmin(no_sleep(device), sync=last)
    await run_scheduler(db_path, local(D, 0), garmin, stop_pushes=1)
    assert garmin.calls == 1 and garmin.sync_calls == 12
    # one second short of the settle time is not enough
    short = LastSync(last.utc + timedelta(seconds=1), last.local + timedelta(seconds=1))
    short_device = device_status(short.utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "2026-10-07T06:00")
    garmin = FakeGarmin(no_sleep(short_device), sync=short)
    await run_scheduler(db_path.with_name("other.db"), local(D, 0), garmin, stop_pushes=1)
    assert garmin.calls == 2


@pytest.mark.anyio
@pytest.mark.parametrize("sync", [None, RuntimeError("x")], ids=["unknown", "crash"])
async def test_a_failed_check_falls_back_to_the_full_read(db_path, caplog, sync):
    caplog.set_level(logging.INFO)
    garmin = FakeGarmin(no_sleep(), sync=sync)
    push, _ = await run_scheduler(db_path, local(D, 0), garmin, stop_pushes=1)
    assert garmin.calls == 13 and garmin.sync_calls == 12
    assert [utc(t) for t, _ in push.sent] == [local(D, 9, 30).astimezone(UTC)]
    assert "briefing garmin=unchanged" not in caplog.text
    assert ("briefing sync_check=crashed kind=RuntimeError" in caplog.text) is isinstance(sync, Exception)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "data",
    [
        garmin_data(snapshot={**NO_SLEEP, "body_battery": {"error": "x"}}, device=DEVICE_0500),
        no_sleep(calls={"get_device_status": "ok", "get_recovery_snapshot": "error", "get_hrv_trend": "ok",
                        "list_scheduled_workouts": "ok"}),
        no_sleep(calls={}),
        no_sleep(auth=None),
        garmin_data(snapshot=NO_SLEEP),  # no device at all: the old behaviour
    ],
    ids=["section-error", "call-error", "no-calls", "unreachable", "no-device"],
)
async def test_a_read_that_is_not_clean_is_never_reused(db_path, data):
    garmin = FakeGarmin(data, sync=SYNC_0500)
    await run_scheduler(db_path, local(D, 0), garmin, stop_pushes=1)
    assert garmin.calls == 13


@pytest.mark.anyio
async def test_an_auth_problem_still_sends_at_once(db_path):
    garmin = FakeGarmin(no_sleep(auth="missing"), sync=SYNC_0500)
    push, _ = await run_scheduler(db_path, local(D, 0), garmin, stop_pushes=1)
    assert garmin.calls == 1 and garmin.sync_calls == 0
    assert utc(push.sent[0][0]) == local(D, 6, 30).astimezone(UTC)


@pytest.mark.anyio
async def test_the_first_attempt_never_asks_for_the_sync_time(db_path):
    garmin = FakeGarmin(garmin_data(device=DEVICE_0500), sync=SYNC_0500)
    push, _ = await run_scheduler(db_path, local(D, 0), garmin, stop_pushes=1)
    assert utc(push.sent[0][0]) == local(D, 6, 30).astimezone(UTC)
    assert garmin.calls == 1 and garmin.sync_calls == 0


@pytest.mark.anyio
async def test_the_next_day_starts_with_a_full_read(db_path):
    next_day = D + timedelta(days=1)
    checks_at_fetch: list[int] = []
    box: dict[str, FakeGarmin] = {}

    def data(day: date, index: int):
        checks_at_fetch.append(box["garmin"].sync_calls)
        return no_sleep() if day == D else garmin_data(device=device_status("2026-10-08T04:00:00Z", "2026-10-08T06:00"))

    garmin = box["garmin"] = FakeGarmin(data, sync=SYNC_0500)
    push, _ = await run_scheduler(db_path, local(D, 0), garmin, stop_pushes=2)
    assert garmin.days[0] == D and garmin.days[-1] == next_day
    assert garmin.calls == 2 and len(push.sent) == 2  # day D: 1 full read (12 checks), then the day after
    assert checks_at_fetch == [0, 12]  # the first attempt of the next day fetched before any check
    assert garmin.sync_calls == 12


def test_reusable_table():
    assert reusable(garmin_data(device=DEVICE_0500)) is True
    assert reusable(garmin_data(device=DEVICE_0500, auth=None)) is False
    assert reusable(garmin_data(device=DEVICE_0500, auth="error")) is False
    assert reusable(garmin_data(device=DEVICE_0500, auth="missing")) is False
    assert reusable(garmin_data()) is False  # the upload time is unknown
    assert reusable(garmin_data(device={"last_upload": {"available": False}})) is False
    assert reusable(garmin_data(device=DEVICE_0500, calls={})) is False
    erred = {"get_device_status": "ok", "get_recovery_snapshot": "ok", "get_hrv_trend": "ok",
             "list_scheduled_workouts": "auth_error"}
    assert reusable(garmin_data(device=DEVICE_0500, calls=erred)) is False
    erred["list_scheduled_workouts"] = "error"
    assert reusable(garmin_data(device=DEVICE_0500, calls=erred)) is False
    assert reusable(garmin_data(device=DEVICE_0500, snapshot={**GREEN_SNAPSHOT, "stress": {"error": "x"}})) is False
    unavailable = {**GREEN_SNAPSHOT, "stress": {"available": False}}
    assert reusable(garmin_data(device=DEVICE_0500, snapshot=unavailable)) is True
    assert reusable(garmin_data(device=DEVICE_0500, snapshot=None)) is True  # no sections to inspect


@pytest.mark.anyio
async def test_the_gate_through_the_reader_makes_one_snapshot_read_and_twelve_checks(db_path, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    token_file = tmp_path / "garmin_readonly_token"
    token_file.write_text("garmin-backend-token-0123456789abcdef\n", encoding="utf-8")
    settings = make_settings(db_path, garmin_token_file=token_file)
    app, calls = fake_garmin_app("garmin-backend-token-0123456789abcdef", snapshot=NO_SLEEP)
    clock = FakeClock(local(D, 0), stop_when=lambda: len(push.sent) >= 1)
    push = FakePush(clock=clock)
    reader = GarminReader(settings, transport=httpx2.ASGITransport(app=app))
    scheduler = BriefingScheduler(settings, Store(db_path), reader, push, now=clock.now, sleep=clock.sleep)
    async with fake_garmin_running(app):
        with pytest.raises(StopLoop):
            await scheduler.run()
    names = [name for name, _ in calls]
    assert names.count("get_recovery_snapshot") == 1 and names.count("get_device_status") == 13
    assert [utc(t) for t, _ in push.sent] == [local(D, 9, 30).astimezone(UTC)]
    assert "(poslední synchronizace s Garminem: dnes 5:07)" in push.sent[0][1].body
    assert "garmin sync_check status=ok" in caplog.text
    for private in ("5:07", "05:07", "03:07"):
        assert private not in caplog.text


@pytest.mark.anyio
async def test_compose_briefing_reuses_a_given_read_and_labels_a_stale_check(db_path):
    store = Store(db_path)
    day_before = D - timedelta(days=1)
    ids = store.replace_planned(day_before, day_before, day_before,
                                [{"date": day_before, "type": "running", "description": "Intervaly"}])[1]
    store.upsert_suggestions([(ids[0], "no_activity", False, None)], D)
    data = garmin_data(device=device_status("2026-10-07T04:58:12Z", "2026-10-07T06:58"))
    stale = "při noční kontrole, před poslední synchronizací hodinek (nemusí platit): běh – Intervaly."

    def set_checked(value: str) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE session_suggestions SET updated_at = ? WHERE session_id = ?", (value, ids[0]))
            conn.commit()
        finally:
            conn.close()

    garmin = FakeGarmin(RuntimeError("fetch must not be called"))
    set_checked("2026-10-07T01:30:00+00:00")
    briefing = await compose_briefing(store, garmin, D, "cs", data=data)
    assert garmin.calls == 0 and stale in briefing.message.body
    set_checked("2026-10-07T05:30:00+00:00")  # after the 04:58:12Z sync
    briefing = await compose_briefing(store, garmin, D, "cs", data=data)
    assert garmin.calls == 0 and stale not in briefing.message.body
    assert "Včera v plánu bez aktivity v Garminu: běh – Intervaly." in briefing.message.body
    # without a given read it fetches
    other = FakeGarmin(data)
    await compose_briefing(store, other, D, "cs")
    assert other.calls == 1
