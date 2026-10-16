"""Daily delivery rules, once, with stub compose / already-sent / record functions, a fake clock and a fake push.

Window 06:30-09:30 Europe/Prague, retry every 15 minutes (the defaults). The briefing and the Monday report are the
two adapters (``test_scheduler.py`` tests them through ``BriefingScheduler`` with the real Store and Garmin fakes).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeClock, FakePush

from coach_mcp.clock import LocalClock
from coach_mcp.config import Settings
from coach_mcp.delivery import Attempt, DailyDelivery, DailyMessage, send_now
from coach_mcp.push import PushMessage

PRAGUE = ZoneInfo("Europe/Prague")
D = date(2026, 10, 7)
START, CUTOFF = time(6, 30), time(9, 30)
RETRY = 900.0
SECRET = "SECRET-TEXT-4242"

pytestmark = pytest.mark.anyio


def local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=PRAGUE)


@dataclass(frozen=True)
class Note:
    message: PushMessage
    waiting: bool = False


class Stub:
    """One daily message with scripted answers; records every call it gets."""

    def __init__(
        self,
        name: str,
        *,
        waits: int = 0,  # the first ``waits`` composes say "waiting"
        compose_fails: tuple[int, ...] = (),  # attempt numbers whose compose raises
        sent: Callable[[date], bool] | bool = False,
        look_fails: tuple[int, ...] = (),  # already-sent looks (1-based) that raise
        record_fails: bool = False,
        due: bool = True,
        crash: str = "retry",
    ) -> None:
        self.name = name
        self._waits = waits
        self._compose_fails = compose_fails
        self._sent = sent
        self._look_fails = look_fails
        self._record_fails = record_fails
        self._due = due
        self._crash = crash
        self.attempts: list[Attempt] = []
        self.looks = 0
        self.recorded: list[tuple[date, Note]] = []

    async def compose(self, attempt: Attempt) -> Note:
        self.attempts.append(attempt)
        if attempt.number in self._compose_fails:
            raise RuntimeError(SECRET)
        return Note(PushMessage(f"{self.name} {attempt.day}", SECRET), waiting=len(self.attempts) <= self._waits)

    def already_sent(self, day: date) -> bool:
        self.looks += 1
        if self.looks in self._look_fails:
            raise OSError(SECRET)
        return self._sent(day) if callable(self._sent) else self._sent

    def record(self, day: date, note: Note) -> None:
        if self._record_fails:
            raise OSError(SECRET)
        self.recorded.append((day, note))

    def message(self) -> DailyMessage[Note]:
        return DailyMessage(
            self.name,
            self.compose,
            self.already_sent,
            self.record,
            due=lambda day: self._due,
            crash=self._crash,  # type: ignore[arg-type]
        )


class Bench:
    def __init__(self, start: datetime, *stubs: Stub, outcomes: list[str] | None = None, retry: float = RETRY):
        self.clock = FakeClock(start, max_sleeps=10_000)
        self.push = FakePush(outcomes, clock=self.clock)
        self.stubs = stubs
        self.delivery = DailyDelivery(
            LocalClock("Europe/Prague", self.clock.now),
            self.push,
            [stub.message() for stub in stubs],
            start=START,
            cutoff=CUTOFF,
            retry_seconds=retry,
        )
        self.waits: list[float] = []

    async def tick(self) -> float:
        wait = await self.delivery.tick()
        self.waits.append(wait)
        return wait

    async def run_until(self, end: datetime) -> None:
        """Tick and sleep the returned wait (at least 1 s, like the loop) until the clock reaches ``end``."""
        while self.clock.now() < end.astimezone(UTC):
            await self.clock.sleep(max(await self.tick(), 1.0))

    def times(self) -> list[str]:
        return [t.astimezone(PRAGUE).strftime("%d %H:%M") for t, _ in self.push.sent]

    def titles(self) -> list[str]:
        return [m.title for _, m in self.push.sent]


def lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "coach_mcp.briefing"]


# ------------------------------------------------------------------ the window


async def test_nothing_before_the_start_then_one_send_and_nothing_more_today():
    a = Stub("a")
    bench = Bench(local(D, 5, 0), a)
    assert await bench.tick() == 5400.0  # 06:30 is 1 h 30 min away
    assert a.attempts == [] and a.looks == 0
    await bench.run_until(local(D, 23, 0))
    assert bench.times() == ["07 06:30"]
    assert a.recorded == [(D, Note(PushMessage(f"a {D}", SECRET)))]
    assert [(x.day, x.number, x.final) for x in a.attempts] == [(D, 1, False)]
    assert a.attempts[0].now == local(D, 6, 30)


async def test_after_a_send_the_wait_is_until_tomorrows_start():
    bench = Bench(local(D, 6, 30), Stub("a"))
    assert await bench.tick() == 24 * 3600.0
    await bench.clock.sleep(3600.0)
    assert await bench.tick() == 23 * 3600.0  # stopped for the day: no look, no compose


async def test_each_local_day_starts_afresh():
    a = Stub("a", waits=1)
    bench = Bench(local(D, 0), a)
    await bench.run_until(local(D + timedelta(days=1), 23, 0))
    assert bench.times() == ["07 06:45", "08 06:30"]  # only the very first compose waits
    assert [(x.day, x.number) for x in a.attempts] == [(D, 1), (D, 2), (D + timedelta(days=1), 1)]


# ------------------------------------------------------------------ outcomes


async def test_already_sent_or_not_due_composes_nothing():
    for stub in (Stub("a", sent=True), Stub("a", due=False)):
        bench = Bench(local(D, 7, 0), stub)
        assert await bench.tick() == 23.5 * 3600
        assert stub.attempts == [] and bench.push.sent == []


async def test_not_due_does_not_look_at_the_store():
    stub = Stub("a", due=False)
    await Bench(local(D, 7, 0), stub).tick()
    assert stub.looks == 0


async def test_waiting_retries_until_the_final_attempt_sends_anyway(caplog):
    caplog.set_level(logging.INFO)
    a = Stub("a", waits=99)
    bench = Bench(local(D, 6, 30), a)
    await bench.run_until(local(D, 23, 0))
    assert bench.times() == ["07 09:30"]
    assert len(a.attempts) == 13 and a.attempts[-1].final and not a.attempts[-2].final
    assert lines(caplog)[:2] == ["a status=waiting attempt=1", "a status=waiting attempt=2"]
    assert lines(caplog)[-1] == "a status=sent attempt=13"


async def test_transient_retries_and_the_last_retry_lands_on_the_cutoff(caplog):
    caplog.set_level(logging.INFO)
    bench = Bench(local(D, 9, 20), Stub("a"), outcomes=["transient"])
    assert await bench.tick() == 600.0  # min(15 min, until the cutoff)
    await bench.clock.sleep(600.0)
    await bench.run_until(local(D, 23, 0))
    assert bench.times() == ["07 09:20", "07 09:30"]
    assert lines(caplog) == [
        "a status=waiting attempt=1",
        "a status=gave_up reason=push_transient",
    ]


async def test_permanent_gives_up_for_the_day_and_the_next_message_is_not_tried(caplog):
    caplog.set_level(logging.INFO)
    a, b = Stub("a"), Stub("b")
    bench = Bench(local(D, 6, 30), a, b, outcomes=["permanent"])
    assert await bench.tick() == 24 * 3600.0
    assert lines(caplog) == ["a status=gave_up reason=push_permanent"]
    assert b.looks == 0 and b.attempts == [] and a.recorded == []


async def test_a_failing_record_is_logged_and_the_message_is_not_sent_again(caplog):
    caplog.set_level(logging.INFO)
    a, b = Stub("a", record_fails=True), Stub("b")
    bench = Bench(local(D, 6, 30), a, b, outcomes=["sent", "transient", "sent"])
    await bench.run_until(local(D, 23, 0))
    assert bench.titles() == [f"a {D}", f"b {D}", f"b {D}"]
    assert len(a.attempts) == 1 and a.looks == 1  # the in-process flag answers; the store is not asked again
    assert lines(caplog)[:2] == ["a status=state_error kind=OSError", "a status=sent attempt=1"]
    assert SECRET not in "\n".join(lines(caplog))


async def test_the_next_message_follows_in_the_same_step_and_counts_its_own_attempts(caplog):
    caplog.set_level(logging.INFO)
    a, b = Stub("a"), Stub("b")
    bench = Bench(local(D, 6, 30), a, b, outcomes=["sent", "transient", "sent"])
    await bench.run_until(local(D, 23, 0))
    assert bench.times() == ["07 06:30", "07 06:30", "07 06:45"]
    assert lines(caplog) == [
        "a status=sent attempt=1",
        "b status=waiting attempt=1",
        "b status=sent attempt=2",
    ]
    assert len(b.recorded) == 1


async def test_a_message_waits_for_the_one_before_it():
    a, b = Stub("a", waits=2), Stub("b")
    bench = Bench(local(D, 6, 30), a, b)
    await bench.run_until(local(D, 23, 0))
    assert bench.times() == ["07 07:00", "07 07:00"]
    assert b.looks == 1


# ------------------------------------------------------------------ late start


async def test_a_late_start_skips_the_day(caplog):
    caplog.set_level(logging.INFO)
    a = Stub("a")
    bench = Bench(local(D, 10, 0), a)
    assert await bench.tick() == 20.5 * 3600
    assert lines(caplog) == ["a status=skipped reason=late_start"] and a.attempts == []
    await bench.run_until(local(D + timedelta(days=1), 7, 0))
    assert bench.times() == ["08 06:30"]


async def test_a_late_start_after_an_earlier_send_skips_only_the_unsent_message(caplog):
    caplog.set_level(logging.INFO)
    a, b = Stub("a", sent=True), Stub("b")
    await Bench(local(D, 10, 0), a, b).tick()
    assert lines(caplog) == ["b status=skipped reason=late_start"]


# ------------------------------------------------------------------ crashes


async def test_retry_mode_logs_a_crash_and_retries_before_the_cutoff(caplog):
    caplog.set_level(logging.INFO)
    a = Stub("a", compose_fails=(1,))
    bench = Bench(local(D, 6, 30), a)
    assert await bench.tick() == RETRY
    await bench.clock.sleep(RETRY)
    await bench.run_until(local(D, 23, 0))
    assert bench.times() == ["07 06:45"]
    assert lines(caplog) == ["a status=crashed kind=RuntimeError", "a status=sent attempt=2"]


async def test_retry_mode_gives_up_after_a_crash_on_the_final_attempt(caplog):
    caplog.set_level(logging.INFO)
    a = Stub("a", compose_fails=(1, 2))
    bench = Bench(local(D, 9, 20), a)
    assert await bench.tick() == 600.0  # min(15 min, until the cutoff)
    await bench.clock.sleep(600.0)
    assert await bench.tick() == 21 * 3600.0  # 09:30, final: the day ends
    assert lines(caplog) == ["a status=crashed kind=RuntimeError"] * 2 and len(a.attempts) == 2


async def test_retry_mode_treats_a_failing_look_like_a_crash(caplog):
    caplog.set_level(logging.INFO)
    a = Stub("a", look_fails=(1, 2))
    bench = Bench(local(D, 9, 20), a)
    assert await bench.tick() == 600.0
    await bench.clock.sleep(600.0)
    assert await bench.tick() == 21 * 3600.0  # the final look failed too: the day ends
    assert lines(caplog) == ["a status=crashed kind=OSError"] * 2 and a.attempts == []


async def test_raise_mode_lets_a_crash_leave_the_step_and_retries_on_the_next():
    a = Stub("a", compose_fails=(1,), crash="raise")
    bench = Bench(local(D, 6, 30), a)
    with pytest.raises(RuntimeError):
        await bench.tick()
    await bench.clock.sleep(RETRY)
    assert await bench.tick() == pytest.approx(24 * 3600.0 - RETRY)
    assert bench.times() == ["07 06:45"]


async def test_raise_mode_ends_the_day_after_a_crash_on_the_final_attempt():
    a = Stub("a", compose_fails=(1, 2), crash="raise")
    bench = Bench(local(D, 9, 20), a)
    with pytest.raises(RuntimeError):
        await bench.tick()
    await bench.clock.sleep(RETRY)  # 09:35: the final attempt
    with pytest.raises(RuntimeError):
        await bench.tick()
    await bench.clock.sleep(RETRY)
    assert bench.clock.now() == local(D, 9, 50).astimezone(UTC)
    assert await bench.tick() == 20 * 3600 + 40 * 60  # until tomorrow's 06:30: no third attempt
    assert len(a.attempts) == 2  # no crash loop until midnight


async def test_raise_mode_a_failing_look_before_the_first_attempt_is_not_a_late_start(caplog):
    caplog.set_level(logging.INFO)
    a = Stub("a", look_fails=(1,), crash="raise")
    bench = Bench(local(D, 9, 20), a)
    with pytest.raises(OSError):
        await bench.tick()
    await bench.clock.sleep(RETRY)  # 09:35, past the cutoff, but the first look was inside the window
    await bench.tick()
    assert bench.times() == ["07 09:35"] and "late_start" not in caplog.text


async def test_raise_mode_a_failing_late_first_look_is_still_a_late_start(caplog):
    caplog.set_level(logging.INFO)
    a = Stub("a", look_fails=(1,), crash="raise")
    bench = Bench(local(D, 10, 0), a)
    with pytest.raises(OSError):
        await bench.tick()  # a failing look does not end the day ...
    await bench.clock.sleep(RETRY)
    await bench.tick()  # ... the next look decides: the day started late
    assert lines(caplog) == ["a status=skipped reason=late_start"] and bench.push.sent == []


async def test_a_crash_never_logs_its_text(caplog):
    caplog.set_level(logging.DEBUG)
    bench = Bench(local(D, 6, 30), Stub("a", compose_fails=(1,), record_fails=True))
    await bench.run_until(local(D, 8, 0))
    assert lines(caplog) and SECRET not in caplog.text


async def test_a_base_exception_is_not_swallowed():
    class Stop(BaseException):
        pass

    async def compose(attempt: Attempt) -> Note:
        raise Stop

    message = DailyMessage("a", compose, lambda day: False, lambda day, note: None)
    clock = FakeClock(local(D, 6, 30))
    delivery = DailyDelivery(
        LocalClock("Europe/Prague", clock.now), FakePush(), [message], start=START, cutoff=CUTOFF, retry_seconds=RETRY
    )
    with pytest.raises(Stop):
        await delivery.tick()


# ------------------------------------------------------------------ send_now (the CLI)


def settings(tmp_path: Any, **overrides: Any) -> Settings:
    return Settings(db_path=tmp_path / "coach.db", telegram_chat_id="123456789", **overrides)


async def note_for(day: date) -> Note:
    return Note(PushMessage(f"n {day}", SECRET))


async def test_send_now_sends_and_prints_the_summary(tmp_path, capsys):
    push = FakePush()
    rc = await send_now(
        settings(tmp_path), "n", note_for, lambda n: f"title={n.message.title}", push=push, today=lambda: D
    )
    assert rc == 0 and [m.title for _, m in push.sent] == [f"n {D}"]
    assert capsys.readouterr().out == f"n status=sent title=n {D}\n"


async def test_send_now_reports_a_failed_push_with_exit_1(tmp_path, capsys):
    push = FakePush(["transient"])
    rc = await send_now(settings(tmp_path), "n", note_for, lambda n: "x=1", push=push, today=lambda: D)
    assert rc == 1 and capsys.readouterr().out == "n status=transient x=1\n"


async def test_send_now_reports_a_crash_with_exit_1_and_prints_nothing(tmp_path, capsys, caplog):
    caplog.set_level(logging.INFO)

    async def broken(day: date) -> Note:
        raise RuntimeError(SECRET)

    rc = await send_now(settings(tmp_path), "n", broken, lambda n: "", push=FakePush(), today=lambda: D)
    assert rc == 1 and capsys.readouterr().out == ""
    assert lines(caplog) == ["n status=crashed kind=RuntimeError"]


async def test_send_now_without_a_chat_exits_2_before_composing(tmp_path, capsys):
    called: list[date] = []

    async def compose(day: date) -> Note:
        called.append(day)
        return await note_for(day)

    unconfigured = Settings(db_path=tmp_path / "c.db")
    rc = await send_now(unconfigured, "n", compose, lambda n: "", push=FakePush(), today=lambda: D)
    assert rc == 2 and called == []
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "push is not configured: set COACH_TELEGRAM_CHAT_ID\n"


async def test_send_now_takes_today_from_the_configured_zone(tmp_path):
    days: list[date] = []

    async def compose(day: date) -> Note:
        days.append(day)
        return await note_for(day)

    for zone in ("Etc/GMT-14", "Etc/GMT+10"):  # UTC+14 and UTC-10: exactly one day apart at any instant
        await send_now(settings(tmp_path, timezone=zone), "n", compose, lambda n: "", push=FakePush())
    assert days[0] - days[1] == timedelta(days=1)
