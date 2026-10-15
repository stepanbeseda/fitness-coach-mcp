"""The local clock: zone, now, today, local clock times, next occurrence and real seconds, DST transition days included.

The DST rules are tested here only (PR 26): the scheduler, the history job and the inbound commands take their
local time from ``LocalClock``, and ``test_local_time_exists_once`` keeps it that way.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from coach_mcp import clock as clock_module
from coach_mcp.clock import LocalClock, local_at, local_day, seconds_until

PRAGUE = ZoneInfo("Europe/Prague")
SRC = Path(clock_module.__file__).parent


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=PRAGUE)


def fixed(instant: datetime) -> LocalClock:
    return LocalClock("Europe/Prague", lambda: instant)


# ------------------------------------------------------------------ now, today, day_of


def test_now_is_the_source_instant_in_the_zone():
    clock = fixed(utc(2026, 10, 6, 22, 30))  # 00:30 CEST on 2026-10-07
    now = clock.now()
    assert now == utc(2026, 10, 6, 22, 30)
    assert now.tzinfo == PRAGUE and now.utcoffset() == timedelta(hours=2)
    assert (now.hour, now.minute) == (0, 30)


def test_today_is_the_local_date_not_the_utc_date():
    assert fixed(utc(2026, 10, 6, 22, 30)).today() == date(2026, 10, 7)
    assert fixed(utc(2026, 10, 6, 21, 59)).today() == date(2026, 10, 6)  # 23:59 CEST
    assert LocalClock("America/New_York", lambda: utc(2026, 10, 7, 3, 0)).today() == date(2026, 10, 6)


def test_day_of_converts_any_aware_instant_to_the_local_date():
    clock = fixed(utc(2026, 1, 1, 0, 0))  # the source does not matter
    assert clock.day_of(utc(2026, 10, 6, 22, 30)) == date(2026, 10, 7)
    assert clock.day_of(datetime(2026, 10, 7, 1, 0, tzinfo=ZoneInfo("America/New_York"))) == date(2026, 10, 7)
    assert local_day(utc(2026, 10, 6, 22, 30), PRAGUE) == date(2026, 10, 7)


def test_the_default_source_is_the_system_clock_in_the_zone():
    clock = LocalClock("Europe/Prague")
    before = datetime.now(UTC)
    now = clock.now()
    assert now.tzinfo == PRAGUE
    assert abs((now - before).total_seconds()) < 60


# ------------------------------------------------------------------ DST transition days (Europe/Prague)
# 2026-03-29 and 2027-03-28: 02:00 CET -> 03:00 CEST (23-hour day); 2026-10-25: 03:00 CEST -> 02:00 CET (25 hours).

AT_ROWS = [
    # (day, clock time, the UTC instant)
    (date(2026, 10, 24), time(6, 30), utc(2026, 10, 24, 4, 30)),  # CEST
    (date(2026, 10, 25), time(6, 30), utc(2026, 10, 25, 5, 30)),  # CET, after the fall back
    (date(2026, 10, 25), time(2, 30), utc(2026, 10, 25, 0, 30)),  # repeated hour: its first occurrence (CEST)
    (date(2027, 3, 28), time(2, 30), utc(2027, 3, 28, 1, 30)),  # does not exist: the next valid instant, 03:30 CEST
    (date(2027, 3, 28), time(6, 30), utc(2027, 3, 28, 4, 30)),  # CEST
]


@pytest.mark.parametrize(("day", "clock_time", "expected"), AT_ROWS)
def test_at_on_dst_transition_days(day, clock_time, expected):
    # Compared as UTC instants: Python never finds a gap or fold time equal to an instant of another zone (PEP 495).
    assert fixed(utc(2026, 1, 1, 0, 0)).at(day, clock_time).astimezone(UTC) == expected
    assert local_at(day, clock_time, PRAGUE).astimezone(UTC) == expected


def test_a_time_that_does_not_exist_reads_as_the_next_valid_local_time():
    instant = local_at(date(2027, 3, 28), time(2, 30), PRAGUE).astimezone(UTC)
    assert instant.astimezone(PRAGUE).strftime("%H:%M %Z") == "03:30 CEST"


UNTIL_ROWS = [
    # (target, now, real seconds)
    (local(date(2026, 10, 25), 6, 30), local(date(2026, 10, 24), 7, 0), 24.5 * 3600),  # not 23 h 30 min
    (local(date(2027, 3, 28), 6, 30), local(date(2027, 3, 27), 22, 0), 7.5 * 3600),  # not 8 h 30 min
    (local(date(2026, 10, 25), 3, 0), local(date(2026, 10, 25), 1, 0), 3 * 3600),  # one tzinfo: wall clock says 2 h
    (local(date(2026, 10, 7), 6, 0), local(date(2026, 10, 7), 6, 30), -1800.0),  # past: negative
    (utc(2026, 10, 7, 4, 30), local(date(2026, 10, 7), 6, 30), 0.0),  # the same instant in two zones
]


@pytest.mark.parametrize(("target", "now", "seconds"), UNTIL_ROWS)
def test_until_counts_real_seconds(target, now, seconds):
    assert LocalClock.until(target, now) == seconds
    assert seconds_until(target, now) == seconds


NEXT_ROWS = [
    # (after, clock time, the next occurrence, real hours from the previous occurrence)
    (local(date(2026, 10, 25), 3, 30), time(3, 30), local(date(2026, 10, 26), 3, 30), 24),  # the fall-back day
    (local(date(2026, 10, 24), 3, 30), time(3, 30), local(date(2026, 10, 25), 3, 30), 25),  # the night before it
    (local(date(2026, 3, 28), 3, 30), time(3, 30), local(date(2026, 3, 29), 3, 30), 23),  # the spring-forward night
    (local(date(2026, 10, 24), 7, 0), time(6, 30), local(date(2026, 10, 25), 6, 30), 25),  # tomorrow's briefing
]


@pytest.mark.parametrize(("after", "clock_time", "expected", "hours"), NEXT_ROWS)
def test_next_at_across_dst_changes(after, clock_time, expected, hours):
    clock = fixed(utc(2026, 1, 1, 0, 0))
    found = clock.next_at(clock_time, after)
    assert found == expected
    previous = clock.at(clock.day_of(after), clock_time)
    assert seconds_until(found, previous) == hours * 3600


def test_next_at_is_today_while_the_time_is_still_ahead():
    clock = fixed(utc(2026, 1, 1, 0, 0))
    assert clock.next_at(time(3, 30), local(date(2026, 10, 7), 3, 29)) == local(date(2026, 10, 7), 3, 30)
    # strictly after: at the very instant it is tomorrow's
    assert clock.next_at(time(3, 30), local(date(2026, 10, 7), 3, 30)) == local(date(2026, 10, 8), 3, 30)
    # the local date decides, not the UTC date: 00:30 CEST on 10-08 is 22:30 UTC on 10-07
    assert clock.next_at(time(3, 30), utc(2026, 10, 7, 22, 30)) == local(date(2026, 10, 8), 3, 30)
    assert clock.next_at(time(0, 15), utc(2026, 10, 7, 22, 30)) == local(date(2026, 10, 9), 0, 15)


# ------------------------------------------------------------------ one place for local time


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _is_attr(node: ast.AST, owner: str, attr: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == owner
    )


def _offenders(src: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(src.glob("*.py")):
        if path.name == "clock.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ("_at", "_until", "_today"):
                found.append(f"{path.name}: def {node.name}")
        for call in _calls(tree):
            func = call.func
            if isinstance(func, ast.Name) and func.id == "ZoneInfo" and path.name != "config.py":
                found.append(f"{path.name}:{call.lineno} ZoneInfo(...)")
            if _is_attr(func, "datetime", "combine") and any(k.arg == "tzinfo" for k in call.keywords):
                found.append(f"{path.name}:{call.lineno} datetime.combine(..., tzinfo=...)")
            if _is_attr(func, "datetime", "now") and not (
                len(call.args) == 1 and isinstance(call.args[0], ast.Name) and call.args[0].id == "UTC"
            ):
                found.append(f"{path.name}:{call.lineno} datetime.now(<not UTC>)")
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "date"
                and isinstance(func.value, ast.Call)
                and isinstance(func.value.func, ast.Attribute)
                and func.value.func.attr == "astimezone"
                and not (
                    len(func.value.args) == 1
                    and isinstance(func.value.args[0], ast.Name)
                    and func.value.args[0].id == "UTC"
                )
            ):
                found.append(f"{path.name}:{call.lineno} .astimezone(<zone>).date()")
    return sorted(found)


def test_local_time_exists_once():
    assert _offenders(SRC) == []


def test_the_guard_finds_each_shape(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from datetime import datetime\n"
        "from zoneinfo import ZoneInfo\n"
        "def _at(day, t, zone):\n"
        "    return datetime.combine(day, t, tzinfo=zone)\n"
        "def f(zone, now):\n"
        "    return ZoneInfo('Europe/Prague'), datetime.now(zone), now.astimezone(zone).date()\n",
        encoding="utf-8",
    )
    (tmp_path / "clock.py").write_text("from datetime import datetime\nx = datetime.now(None)\n", encoding="utf-8")
    (tmp_path / "config.py").write_text("from zoneinfo import ZoneInfo\nZoneInfo('UTC')\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text(
        "from datetime import UTC, datetime\nx = datetime.now(UTC), datetime.now(UTC).astimezone(UTC).date()\n",
        encoding="utf-8",
    )
    assert _offenders(tmp_path) == [
        "bad.py: def _at",
        "bad.py:4 datetime.combine(..., tzinfo=...)",
        "bad.py:6 .astimezone(<zone>).date()",
        "bad.py:6 ZoneInfo(...)",
        "bad.py:6 datetime.now(<not UTC>)",
    ]
