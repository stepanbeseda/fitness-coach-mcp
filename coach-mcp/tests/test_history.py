from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeHistorySource, StopLoop
from garmin_payloads import (
    HISTORY_ROW,
    HISTORY_SECTIONS,
    activities_payload,
    activity,
    history_snapshot,
)
from history_helpers import local, make_job, next_run, run_first, run_job, seed_activities, seed_day

from coach_mcp import history
from coach_mcp import store as store_module
from coach_mcp.config import Settings
from coach_mcp.history import (
    ALL_SECTIONS,
    activity_totals,
    missing_sections,
    missing_text,
    needs_fetch,
    prediction_range,
    prediction_values,
    recovery_values,
    repair_sections,
    section_errors,
    snapshot_failed,
    sport_of,
)
from coach_mcp.store import HISTORY_COLUMNS, MIGRATIONS, Store

TODAY = date(2026, 10, 7)
START = local(TODAY, 3, 29)  # the first run happens after the 60 s start-up delay: 03:30:00
AFTER_WINDOW = local(TODAY, 10, 0)  # outside 06:30-09:30
ERROR_SECTION = {"error": "x"}

SECTION_COLUMNS = {
    "readiness": ["readiness"],
    "hrv": ["hrv_last_night_ms", "hrv_weekly_avg_ms", "hrv_status"],
    "sleep": ["sleep_duration_h", "sleep_score"],
    "body_battery": ["body_battery_high", "body_battery_low"],
    "resting_hr": ["resting_hr"],
    "stress": ["stress_avg"],
    "training_status": ["acute_load", "chronic_load", "acwr_ratio", "vo2max_running", "vo2max_cycling"],
}


def days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def rows_by_date(store) -> dict[str, dict[str, Any]]:
    return {row["date"]: row for row in store.history_rows(None, date(2030, 1, 1))}


# ------------------------------------------------------------ pure helpers


def test_section_table_matches_the_store_columns():
    assert sorted(sum(SECTION_COLUMNS.values(), [])) == sorted(HISTORY_COLUMNS)
    assert set(SECTION_COLUMNS) == set(HISTORY_SECTIONS) == set(history.SNAPSHOT_SECTIONS)


def test_recovery_values_maps_every_column():
    assert recovery_values(history_snapshot("2026-10-06")) == HISTORY_ROW


@pytest.mark.parametrize(
    ("readiness", "expected"),
    [({"score": 70, "morning_score": 72}, 72), ({"score": 70}, 70), ({"score": 70, "morning_score": None}, 70)],
)
def test_readiness_prefers_morning_score(readiness, expected):
    assert recovery_values(history_snapshot("2026-10-06", readiness=readiness))["readiness"] == expected


@pytest.mark.parametrize("section", HISTORY_SECTIONS)
def test_unavailable_section_gives_nulls(section):
    got = recovery_values(history_snapshot("2026-10-06", **{section: {"available": False}}))
    expected = dict(HISTORY_ROW)
    expected.update(dict.fromkeys(SECTION_COLUMNS[section]))
    assert got == expected
    assert set(got) == set(HISTORY_COLUMNS)


@pytest.mark.parametrize("section", HISTORY_SECTIONS)
def test_missing_section_key_counts_as_unavailable(section):
    payload = history_snapshot("2026-10-06")
    del payload[section]
    expected = dict(HISTORY_ROW)
    expected.update(dict.fromkeys(SECTION_COLUMNS[section]))
    assert recovery_values(payload) == expected


@pytest.mark.parametrize("section", HISTORY_SECTIONS)
def test_error_section_is_left_out(section):
    got = recovery_values(history_snapshot("2026-10-06", **{section: ERROR_SECTION}))
    assert set(got) == set(HISTORY_COLUMNS) - set(SECTION_COLUMNS[section])
    assert all(got[c] == HISTORY_ROW[c] for c in got)


def test_non_numeric_and_bool_values_become_null():
    payload = history_snapshot(
        "2026-10-06",
        readiness={"score": True, "morning_score": "72"},
        hrv={"last_night_avg_ms": "55", "weekly_avg_ms": float("nan"), "status": "BALANCED"},
        sleep={"duration_h": float("inf"), "score": None},
        stress=[1, 2],
    )
    got = recovery_values(payload)
    assert (got["readiness"], got["hrv_last_night_ms"], got["hrv_weekly_avg_ms"]) == (None, None, None)
    assert (got["sleep_duration_h"], got["sleep_score"], got["stress_avg"]) == (None, None, None)
    assert got["hrv_status"] == "BALANCED"  # a valid value next to the invalid ones is kept
    assert got["resting_hr"] == 47


def test_a_bool_does_not_count_as_a_number_even_for_a_plain_field():
    assert recovery_values(history_snapshot("2026-10-06", resting_hr={"today": True}))["resting_hr"] is None
    assert recovery_values(history_snapshot("2026-10-06", resting_hr={"today": 0}))["resting_hr"] == 0


@pytest.mark.parametrize(
    ("status", "expected"),
    [("x" * 41, None), ("x" * 40, "x" * 40), (7, None), ("", None), (None, None), ("BALANCED", "BALANCED")],
)
def test_hrv_status_must_be_short_text(status, expected):
    got = recovery_values(history_snapshot("2026-10-06", hrv={"last_night_avg_ms": 55, "status": status}))
    assert got["hrv_status"] == expected
    assert got["hrv_last_night_ms"] == 55


@pytest.mark.parametrize(
    ("type_key", "sport"),
    [
        ("strength_training", "strength"),
        ("running", "running"),
        ("trail_running", "running"),
        ("treadmill_running", "running"),
        ("virtual_run", "running"),
        ("cycling", "cycling"),
        ("gravel_cycling", "cycling"),
        ("indoor_cycling", "cycling"),
        ("mountain_biking", "cycling"),
        ("virtual_ride", "cycling"),
        ("lap_swimming", "swimming"),
        ("open_water_swimming", "swimming"),
        ("walking", "walking"),
        ("casual_walking", "walking"),
        ("hiking", "hiking"),
        ("yoga", "other"),
        ("indoor_rowing", "other"),
        (None, "other"),
        ("", "other"),
    ],
)
def test_sport_of(type_key, sport):
    assert sport_of(type_key) == sport


def test_strength_rule_is_an_equality_the_others_are_substring_rules():
    assert sport_of("strength_training") == "strength"
    assert sport_of("strength_training_run") == "running"  # not equal to strength_training: first match wins
    assert sport_of("strength") == "other"


def test_activity_totals_per_day_and_sport():
    d1, d2, d3 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)
    payload = activities_payload(
        [
            activity("2026-10-05", "running", 30.04, 5.004, 50.04),
            activity("2026-10-05", "trail_running", 10.06, 1.0, 11.0, hhmm="18:00"),
            activity("2026-10-05", "strength_training", 45, None, 30),
            activity("2026-10-06", "yoga", 20),  # missing distance and load count 0
            activity("2026-10-09", "running", 99, 99, 99),  # outside the days
            {"id": 9, "type": "running", "duration_min": 5},  # no start_local
            {"id": 9, "start_local": "garbage", "type": "running", "duration_min": 5},
        ]
    )
    got = activity_totals(payload, [d1, d2, d3])
    assert got == {
        d1: {
            "running": {"count": 2, "duration_min": 40.1, "distance_km": 6.0, "training_load": 61.0},
            "strength": {"count": 1, "duration_min": 45.0, "distance_km": 0.0, "training_load": 30.0},
        },
        d2: {"other": {"count": 1, "duration_min": 20.0, "distance_km": 0.0, "training_load": 0.0}},
        d3: {},
    }
    assert activity_totals(payload, [d3]) == {d3: {}}


def test_activity_totals_non_numeric_values_count_zero():
    payload = activities_payload(
        [
            {
                "start_local": "2026-10-05T07:00",
                "type": "running",
                "duration_min": "45",
                "distance_km": True,
                "training_load": None,
            }
        ]
    )
    assert activity_totals(payload, [date(2026, 10, 5)]) == {
        date(2026, 10, 5): {"running": {"count": 1, "duration_min": 0.0, "distance_km": 0.0, "training_load": 0.0}}
    }


def test_activity_totals_are_rounded_after_summing():
    payload = activities_payload(
        [activity("2026-10-05", "running", 10.04, 1.004, 1.04), activity("2026-10-05", "running", 10.04, 1.004, 1.04)]
    )
    assert activity_totals(payload, [date(2026, 10, 5)]) == {
        date(2026, 10, 5): {"running": {"count": 2, "duration_min": 20.1, "distance_km": 2.01, "training_load": 2.1}}
    }


def all_errors() -> dict[str, Any]:
    return history_snapshot("x", **dict.fromkeys(HISTORY_SECTIONS, ERROR_SECTION))


OUTAGE_SECTION = {"error": "Garmin Connect request failed: 503 Server Error"}


def outage_snapshot(day: date) -> dict[str, Any]:
    """What garmin-mcp answers for a snapshot while Garmin Connect is down: every section carries the error."""
    return history_snapshot(day.isoformat(), **dict.fromkeys(HISTORY_SECTIONS, OUTAGE_SECTION))


def seed_known_good(store, day: date) -> None:
    """A day both parts of which were fetched today (not pending), so the job has a day to probe with."""
    seed_day(store, day, fetched_on=TODAY, readiness=50)
    seed_activities(store, day, {}, fetched_on=TODAY)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (all_errors(), True),
        (history_snapshot("x", **{**dict.fromkeys(HISTORY_SECTIONS, ERROR_SECTION), "sleep": {"score": 80}}), False),
        (
            history_snapshot("x", **{**dict.fromkeys(HISTORY_SECTIONS, ERROR_SECTION), "hrv": {"available": False}}),
            False,
        ),
        (history_snapshot("x", sleep=ERROR_SECTION), False),
        (history_snapshot("x"), False),
        ({}, False),
    ],
)
def test_snapshot_failed_only_when_every_section_errors(payload, expected):
    assert snapshot_failed(payload) is expected


@pytest.mark.parametrize("missing", HISTORY_SECTIONS)
def test_snapshot_with_one_section_absent_among_errors_is_not_failed(missing):
    payload = all_errors()
    del payload[missing]
    assert snapshot_failed(payload) is False


T = TODAY


@pytest.mark.parametrize(
    ("day", "fetched_on", "failures", "expected"),
    [
        (T - timedelta(days=5), None, 0, True),
        (T - timedelta(days=5), None, 2, True),
        (T - timedelta(days=5), None, 3, False),
        (T - timedelta(days=1), T, 0, False),
        (T - timedelta(days=2), T - timedelta(days=1), 0, True),
        (T - timedelta(days=3), T - timedelta(days=1), 0, True),
        (T - timedelta(days=4), T - timedelta(days=1), 0, False),
        (T - timedelta(days=10), T - timedelta(days=9), 0, True),
        # a day with data stops being re-fetched after MAX_FAILURES confirmed item errors
        (T - timedelta(days=2), T - timedelta(days=1), 2, True),
        (T - timedelta(days=2), T - timedelta(days=1), 3, False),
        (T - timedelta(days=2), T - timedelta(days=1), 5, False),
    ],
)
def test_needs_fetch_rule(day, fetched_on, failures, expected):
    assert needs_fetch(day, fetched_on, failures, T) is expected


# --------------------------------------------------------------- the job


@pytest.mark.anyio
async def test_default_backfill_covers_90_days(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    source = FakeHistorySource()
    job, clock = make_job(store, source, START)
    delay = await run_first(job, clock)

    window = days(date(2026, 7, 9), date(2026, 10, 6))
    assert len(window) == 90
    assert sorted(source.snapshot_days) == window and len(set(source.snapshot_days)) == 90
    covered = [d for lo, hi in source.activity_ranges for d in days(lo, hi)]
    assert len(source.activity_ranges) == 13
    assert sorted(covered) == window  # exactly the window, every day once
    assert all(a["limit"] == 100 for name, a in source.calls if name == "list_activities")

    rows = rows_by_date(store)
    assert sorted(rows) == [d.isoformat() for d in window]
    assert {(r["recovery_fetched_on"], r["activities_fetched_on"]) for r in rows.values()} == {
        ("2026-10-07", "2026-10-07")
    }
    assert rows["2026-10-06"]["readiness"] == 72 and rows["2026-10-06"]["activities"] == "{}"
    assert clock.now() + timedelta(seconds=delay) == local(date(2026, 10, 8), 3, 30)
    lines = [r.getMessage() for r in caplog.records if r.name == "coach_mcp.history"]
    assert len(lines) == 1 and lines[0].startswith(
        "history status=done fetched=103 failed=0 pending=90 given_up=0 ms="
    )


@pytest.mark.anyio
async def test_first_run_backfills_the_configured_range_newest_first(store):
    source = FakeHistorySource()
    job, clock = make_job(store, source, START, history_backfill_days=10)
    await run_first(job, clock)

    def act(first: str, last: str) -> tuple[str, dict[str, Any]]:
        return "list_activities", {"start_date": first, "end_date": last, "limit": 100}

    def snap(day: str) -> tuple[str, dict[str, Any]]:
        return "get_recovery_snapshot", {"date": day}

    assert source.calls == [
        act("2026-09-30", "2026-10-06"),
        *(snap(f"2026-10-0{n}") for n in (6, 5, 4, 3, 2, 1)),
        snap("2026-09-30"),
        act("2026-09-27", "2026-09-29"),
        snap("2026-09-29"),
        snap("2026-09-28"),
        snap("2026-09-27"),
    ]
    assert len(rows_by_date(store)) == 10


@pytest.mark.anyio
async def test_delay_between_backend_calls(store):
    source = FakeHistorySource()
    job, clock = make_job(store, source, START, history_backfill_days=10, history_call_delay_seconds=20)
    await run_first(job, clock)
    assert len(source.calls) == 12
    gaps = [(b - a).total_seconds() for a, b in zip(source.times, source.times[1:], strict=False)]
    assert gaps == [20.0] * 11
    assert clock.sleeps == [60.0, *[20.0] * 11]  # the start-up delay, then one sleep between each pair of calls


@pytest.mark.anyio
async def test_backfill_resumes_after_restart(store):
    window = days(date(2026, 9, 27), date(2026, 10, 6))
    first_source = FakeHistorySource()
    job, clock = make_job(
        store, first_source, START, history_backfill_days=10, stop_when=lambda: len(first_source.calls) >= 6
    )
    with pytest.raises(StopLoop):  # the process dies in the middle of the first run
        await run_first(job, clock)
    assert len(first_source.calls) == 6
    stored_after_crash = [d for d, r in rows_by_date(store).items() if r["recovery_fetched_on"]]
    assert stored_after_crash == [d.isoformat() for d in days(date(2026, 10, 2), date(2026, 10, 6))]

    second_source = FakeHistorySource()
    job, clock = make_job(store, second_source, START, history_backfill_days=10)
    await run_first(job, clock)

    fetched = first_source.snapshot_days + second_source.snapshot_days
    assert len(fetched) == len(set(fetched))  # no day twice
    assert sorted(fetched) == window
    assert second_source.snapshot_days == days(date(2026, 9, 27), date(2026, 10, 1))[::-1]
    # The activity block that was already stored is not asked for again.
    assert second_source.activity_ranges == [(date(2026, 9, 27), date(2026, 9, 29))]
    assert {r["recovery_fetched_on"] for r in rows_by_date(store).values()} == {"2026-10-07"}


@pytest.mark.anyio
async def test_nightly_run_refetches_the_last_three_days(store):
    source = FakeHistorySource()
    job, clock = make_job(store, source, START, history_backfill_days=10)
    delay = await run_first(job, clock)
    first_night = len(source.calls)

    delay = await next_run(job, clock, delay)
    assert source.times[first_night] == local(date(2026, 10, 8), 3, 30)
    assert source.snapshot_days[10:] == [
        date(2026, 10, 7),
        date(2026, 10, 6),
        date(2026, 10, 5),
    ]
    assert source.activity_ranges[2:] == [(date(2026, 10, 5), date(2026, 10, 7))]
    assert len(source.calls) == first_night + 4
    assert clock.now() + timedelta(seconds=delay) == local(date(2026, 10, 9), 3, 30)
    # Day four back is not touched again: its last fetch (10-07) was already on the 3rd day after it.
    assert rows_by_date(store)["2026-10-04"]["recovery_fetched_on"] == "2026-10-07"


@pytest.mark.anyio
async def test_late_sync_value_overwrites_on_refetch(store):
    scores = {"value": 70}

    def recovery(day: date, index: int):
        return "ok", history_snapshot(day.isoformat(), sleep={"duration_h": 7.0, "score": scores["value"]})

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, START, history_backfill_days=2)
    delay = await run_first(job, clock)
    assert rows_by_date(store)["2026-10-06"]["sleep_score"] == 70
    scores["value"] = 90  # the watch synced late
    await next_run(job, clock, delay)
    rows = rows_by_date(store)
    assert rows["2026-10-06"]["sleep_score"] == 90 and rows["2026-10-06"]["recovery_fetched_on"] == "2026-10-08"
    assert rows["2026-10-07"]["sleep_score"] == 90


@pytest.mark.anyio
async def test_section_error_on_refetch_keeps_the_stored_value(store):
    state = {"hrv": {"last_night_avg_ms": 55, "weekly_avg_ms": 57, "status": "BALANCED"}, "score": 81}

    def recovery(day: date, index: int):
        return "ok", history_snapshot(day.isoformat(), hrv=state["hrv"], sleep={"duration_h": 7.4, "score": state["score"]})

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, START, history_backfill_days=2)
    delay = await run_first(job, clock)
    state["hrv"], state["score"] = ERROR_SECTION, 60  # hrv fails this time, sleep changed
    await next_run(job, clock, delay)
    row = rows_by_date(store)["2026-10-06"]
    assert (row["hrv_last_night_ms"], row["hrv_weekly_avg_ms"], row["hrv_status"]) == (55, 57, "BALANCED")
    assert row["sleep_score"] == 60
    assert row["recovery_failures"] == 0


@pytest.mark.anyio
async def test_snapshot_of_a_day_is_idempotent(store):
    source = FakeHistorySource()
    job, clock = make_job(store, source, START, history_backfill_days=2)
    delay = await run_first(job, clock)
    before = rows_by_date(store)["2026-10-06"]
    await next_run(job, clock, delay)  # the same payload is stored a second time (re-fetch window)
    rows = rows_by_date(store)
    assert sorted(rows) == ["2026-10-05", "2026-10-06", "2026-10-07"]
    assert source.snapshot_days.count(date(2026, 10, 6)) == 2
    after = rows["2026-10-06"]
    assert {k: after[k] for k in HISTORY_COLUMNS} == {k: before[k] for k in HISTORY_COLUMNS} == HISTORY_ROW
    assert after["activities"] == before["activities"] == "{}"


@pytest.mark.anyio
async def test_missing_section_stores_null_and_does_not_fail_the_day(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")

    def recovery(day: date, index: int):
        return "ok", history_snapshot(day.isoformat(), hrv={"available": False})

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, START, history_backfill_days=1)
    await run_first(job, clock)
    (row,) = store.history_rows(None, TODAY)
    assert (row["hrv_last_night_ms"], row["hrv_weekly_avg_ms"], row["hrv_status"]) == (None, None, None)
    expected = {k: v for k, v in HISTORY_ROW.items() if not k.startswith("hrv")}
    assert {k: row[k] for k in expected} == expected
    assert row["recovery_fetched_on"] == "2026-10-07" and row["recovery_failures"] == 0
    assert "history status=done fetched=2 failed=0" in caplog.text


def rate_limited_from(first_index: int):
    return lambda day, index: ("rate_limited", None) if index >= first_index else ("ok", history_snapshot(day.isoformat()))


@pytest.mark.anyio
async def test_rate_limit_stops_the_run_and_backs_off(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    source = FakeHistorySource(recovery=rate_limited_from(2))  # the 3rd call: act, snapshot, snapshot -> 429
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    delay = await run_first(job, clock)
    assert delay == 3600.0
    assert len(source.calls) == 3
    rows = rows_by_date(store)
    assert rows["2026-10-06"]["recovery_fetched_on"] == "2026-10-07"
    assert rows["2026-10-05"]["recovery_fetched_on"] is None
    assert rows["2026-10-05"]["recovery_failures"] == 0
    assert source.snapshot_days == [date(2026, 10, 6), date(2026, 10, 5)]
    assert "history status=rate_limited fetched=2 failed=0" in caplog.text
    assert [r.levelno for r in caplog.records if r.name == "coach_mcp.history"] == [logging.WARNING]

    delays = [delay]
    for _ in range(4):
        delays.append(await next_run(job, clock, delays[-1]))
    assert delays == [3600.0, 7200.0, 14400.0, 28800.0, 28800.0]


@pytest.mark.anyio
@pytest.mark.parametrize("middle", ["auth_missing", "backend_down", "item_error"])
async def test_backoff_resets_after_any_run_that_is_not_rate_limited(store, middle):
    mode = {"value": "limited"}
    good = date(2026, 10, 4)  # the known-good day that proves the backend works (the item errors get charged)
    seed_known_good(store, good)

    def recovery(day: date, index: int):
        if day == good:
            return "ok", history_snapshot(day.isoformat())
        if mode["value"] == "limited":
            return "rate_limited", None
        return ("error", None) if mode["value"] == "error" else ("ok", history_snapshot(day.isoformat()))

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    delay = await run_first(job, clock)
    assert delay == 3600.0  # the backoff is now 7200 for the next limit
    if middle == "auth_missing":
        source.auth = "missing"
    elif middle == "backend_down":
        source.auth = None
    else:
        mode["value"] = "error"
    delay = await next_run(job, clock, delay)
    assert delay == 3600.0
    source.auth, mode["value"] = "ok", "limited"
    assert await next_run(job, clock, delay) == 3600.0  # not 7200: the interrupting run reset the backoff


@pytest.mark.anyio
async def test_blocks_do_not_span_a_gap_of_fetched_days(store):
    for day in days(date(2026, 10, 1), date(2026, 10, 3)):  # already fetched today: not pending
        store.upsert_history_recovery(day, {"readiness": 70}, TODAY)
        store.upsert_history_activities({day: "{}"}, TODAY)
    source = FakeHistorySource()
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=10)
    await run_first(job, clock)
    assert source.activity_ranges == [
        (date(2026, 10, 4), date(2026, 10, 6)),
        (date(2026, 9, 27), date(2026, 9, 30)),
    ]
    assert sorted(source.snapshot_days) == [*days(date(2026, 9, 27), date(2026, 9, 30)), *days(date(2026, 10, 4), date(2026, 10, 6))]


@pytest.mark.anyio
async def test_backoff_resets_after_a_done_run(store):
    mode = {"limited": True}

    def recovery(day: date, index: int):
        return ("rate_limited", None) if mode["limited"] else ("ok", history_snapshot(day.isoformat()))

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=2)
    delay = await run_first(job, clock)
    assert delay == 3600.0
    mode["limited"] = False
    delay = await next_run(job, clock, delay)  # done
    assert clock.now() + timedelta(seconds=delay) == local(date(2026, 10, 8), 3, 30)
    mode["limited"] = True
    delay = await next_run(job, clock, delay)  # the next night: limited again, back to one hour
    assert delay == 3600.0
    assert await next_run(job, clock, delay) == 7200.0


@pytest.mark.anyio
@pytest.mark.parametrize("auth", ["missing", "error"])
async def test_auth_problem_skips_the_run_without_calls(store, auth, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    source = FakeHistorySource(auth=auth)
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    assert await run_first(job, clock) == 3600.0
    assert source.calls == [] and source.auth_checks == 1
    assert rows_by_date(store) == {}
    assert "history status=auth fetched=0 failed=0 pending=3" in caplog.text


@pytest.mark.anyio
async def test_auth_error_from_a_call_stops_the_run_without_a_failure(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    source = FakeHistorySource(recovery=lambda day, index: ("auth_error", None))
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    assert await run_first(job, clock) == 3600.0
    assert len(source.snapshot_days) == 1  # the run stopped at the first snapshot
    assert {r["recovery_failures"] for r in rows_by_date(store).values()} == {0}
    assert "history status=auth fetched=1 failed=0" in caplog.text


@pytest.mark.anyio
async def test_retry_is_capped_at_the_next_history_time(store):
    source = FakeHistorySource(auth="missing")
    job, clock = make_job(store, source, local(TODAY, 3, 0), history_backfill_days=3)
    delay = await run_first(job, clock)
    assert clock.now() + timedelta(seconds=delay) == local(TODAY, 3, 30)  # earlier than +1 h


@pytest.mark.anyio
async def test_unreachable_backend_retries_in_an_hour(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    source = FakeHistorySource(auth=None)
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    assert await run_first(job, clock) == 3600.0
    assert source.calls == []
    assert "history status=unreachable" in caplog.text


@pytest.mark.anyio
async def test_unreachable_call_mid_run_counts_no_failure(store):
    source = FakeHistorySource(
        recovery=lambda day, index: ("unreachable", None) if index == 2 else ("ok", history_snapshot(day.isoformat()))
    )
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    assert await run_first(job, clock) == 3600.0
    assert len(source.calls) == 3
    assert {r["recovery_failures"] for r in rows_by_date(store).values()} == {0}
    assert {r["activities_failures"] for r in rows_by_date(store).values()} == {0}


@pytest.mark.anyio
async def test_no_credentials_stops_the_run(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    source = FakeHistorySource(recovery=lambda day, index: ("no_credentials", None))
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    assert await run_first(job, clock) == 3600.0
    assert len(source.snapshot_days) == 1
    assert {r["recovery_failures"] for r in rows_by_date(store).values()} == {0}
    assert "history status=no_credentials" in caplog.text


@pytest.mark.anyio
async def test_item_error_counts_a_failure_and_continues(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    bad = date(2026, 10, 5)
    source = FakeHistorySource(
        recovery=lambda day, index: ("error", None) if day == bad else ("ok", history_snapshot(day.isoformat()))
    )
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    assert await run_first(job, clock) == 3600.0  # partial: try again in an hour
    rows = rows_by_date(store)
    assert rows["2026-10-05"]["recovery_failures"] == 1 and rows["2026-10-05"]["recovery_fetched_on"] is None
    assert rows["2026-10-05"]["readiness"] is None
    assert rows["2026-10-06"]["readiness"] == 72 and rows["2026-10-04"]["readiness"] == 72  # the others went on
    assert "history status=partial fetched=3 failed=1" in caplog.text
    # the ok call after the bad day proved the backend works: the error was charged then, no probe call was needed
    assert source.snapshot_days == [date(2026, 10, 6), bad, date(2026, 10, 4)]


@pytest.mark.anyio
async def test_day_is_given_up_after_three_failures(store):
    bad = date(2026, 10, 5)
    source = FakeHistorySource(
        recovery=lambda day, index: ("error", None) if day == bad else ("ok", history_snapshot(day.isoformat()))
    )
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    delay = await run_first(job, clock)
    for _ in range(2):
        delay = await next_run(job, clock, delay)
    assert source.snapshot_days.count(bad) == 3
    assert rows_by_date(store)["2026-10-05"]["recovery_failures"] == 3
    before = len(source.calls)
    await next_run(job, clock, delay)  # the fourth run: nothing is pending any more
    assert len(source.calls) == before
    row = rows_by_date(store)["2026-10-05"]
    assert row["recovery_fetched_on"] is None and row["readiness"] is None


@pytest.mark.anyio
async def test_activities_error_marks_only_activities_failures(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    good = date(2026, 10, 3)
    seed_known_good(store, good)
    before = rows_by_date(store)[good.isoformat()]
    source = FakeHistorySource(
        activities=lambda start, end, index: (
            ("ok", activities_payload([])) if start == end == good else ("error", None)
        )
    )
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=4)
    assert await run_first(job, clock) == 3600.0
    rows = {d: r for d, r in rows_by_date(store).items() if d != good.isoformat()}
    assert sorted(rows) == ["2026-10-04", "2026-10-05", "2026-10-06"]
    assert {r["activities_failures"] for r in rows.values()} == {1}
    assert {r["recovery_failures"] for r in rows.values()} == {0}
    assert {r["activities"] for r in rows.values()} == {None}
    assert {r["recovery_fetched_on"] for r in rows.values()} == {"2026-10-07"}
    assert rows_by_date(store)[good.isoformat()] == before  # the probe is never stored
    assert source.activity_ranges[-1] == (good, good)  # the activities part was proved by its own call
    assert "history status=partial fetched=3 failed=1" in caplog.text


@pytest.mark.anyio
async def test_truncated_activities_are_refetched_day_by_day(store):
    def activities(start: date, end: date, index: int):
        if start != end:
            return "ok", activities_payload([activity("2026-10-04", "yoga", 99)], truncated=True)
        rows = [activity("2026-10-03", "running", 40, 8.0, 70)] if start == date(2026, 10, 3) else []
        return "ok", activities_payload(rows)

    source = FakeHistorySource(activities=activities)
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=7)
    await run_first(job, clock)
    assert source.activity_ranges == [(date(2026, 9, 30), date(2026, 10, 6)), *((d, d) for d in days(date(2026, 9, 30), date(2026, 10, 6))[::-1])]
    rows = rows_by_date(store)
    assert rows["2026-10-04"]["activities"] == "{}"  # the multi-day answer (a yoga row) was discarded
    assert rows["2026-10-03"]["activities"] == (
        '{"running":{"count":1,"distance_km":8.0,"duration_min":40.0,"training_load":70.0}}'
    )
    assert {r["activities_fetched_on"] for r in rows.values()} == {"2026-10-07"}


@pytest.mark.anyio
async def test_single_day_still_truncated_is_stored_as_returned(store):
    rows = [activity("2026-10-06", "running", 30, 5.0, 40), activity("2026-10-06", "cycling", 60, 20.0, 80)]
    source = FakeHistorySource(activities=lambda start, end, index: ("ok", activities_payload(rows, truncated=True)))
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=1)
    await run_first(job, clock)
    assert source.activity_ranges == [(date(2026, 10, 6), date(2026, 10, 6))]  # no further call for that day
    (row,) = store.history_rows(None, TODAY)
    assert row["activities_fetched_on"] == "2026-10-07"
    assert '"running":{"count":1' in row["activities"] and '"cycling":{"count":1' in row["activities"]


@pytest.mark.anyio
@pytest.mark.parametrize("answer", [{"activities": {}, "count": 0}, {"count": 0}, {"activities": "none"}])
async def test_activities_payload_not_a_list_is_an_item_error(store, answer, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    good = date(2026, 10, 3)
    seed_known_good(store, good)
    source = FakeHistorySource(
        activities=lambda start, end, index: ("ok", activities_payload([])) if start == end == good else ("ok", answer)
    )
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=4)
    assert await run_first(job, clock) == 3600.0
    rows = {d: r for d, r in rows_by_date(store).items() if d != good.isoformat()}
    assert sorted(rows) == ["2026-10-04", "2026-10-05", "2026-10-06"]
    assert {r["activities_failures"] for r in rows.values()} == {1}  # every day of the call
    assert {r["activities"] for r in rows.values()} == {None} and {r["activities_fetched_on"] for r in rows.values()} == {None}
    assert "history status=partial" in caplog.text


@pytest.mark.anyio
async def test_snapshot_with_every_section_in_error_counts_a_failure(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    state = {"broken": True}
    old = date(2026, 10, 1)  # older than D+3: no re-fetch window would pick it up later

    def recovery(day: date, index: int):
        if day == old and state["broken"]:
            return "ok", all_errors()
        return "ok", history_snapshot(day.isoformat())

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=7)
    delay = await run_first(job, clock)
    assert delay == 3600.0
    row = rows_by_date(store)["2026-10-01"]
    assert row["recovery_fetched_on"] is None and row["recovery_failures"] == 1 and row["readiness"] is None
    assert rows_by_date(store)["2026-10-06"]["recovery_fetched_on"] == "2026-10-07"
    assert "history status=partial fetched=7 failed=1" in caplog.text

    state["broken"] = False
    await next_run(job, clock, delay)
    row = rows_by_date(store)["2026-10-01"]
    assert row["recovery_fetched_on"] == "2026-10-07" and row["readiness"] == 72 and row["recovery_failures"] == 0
    assert source.snapshot_days.count(old) == 2
    assert source.snapshot_days.count(date(2026, 10, 6)) == 1  # fetched today already: not asked again


@pytest.mark.anyio
async def test_snapshot_with_some_sections_in_error_is_stored(store):
    source = FakeHistorySource(
        recovery=lambda day, index: ("ok", history_snapshot(day.isoformat(), sleep=ERROR_SECTION))
    )
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=1)
    await run_first(job, clock)
    (row,) = store.history_rows(None, TODAY)
    assert (row["sleep_duration_h"], row["sleep_score"]) == (None, None)
    assert row["readiness"] == 72 and row["hrv_last_night_ms"] == 55 and row["acute_load"] == 410
    assert row["recovery_fetched_on"] == "2026-10-07" and row["recovery_failures"] == 0


@pytest.mark.anyio
async def test_pauses_in_the_briefing_window_and_resumes_at_cutoff(store):
    source = FakeHistorySource()
    job, clock = make_job(store, source, local(TODAY, 6, 28), history_backfill_days=30, history_call_delay_seconds=20)
    delay = await run_first(job, clock)  # first run at 06:29:00
    assert source.times == [local(TODAY, 6, 29, 0), local(TODAY, 6, 29, 20), local(TODAY, 6, 29, 40)]
    assert clock.now() == local(TODAY, 6, 30, 0)  # the third pause for the next call ended inside the window
    assert clock.now() + timedelta(seconds=delay) == local(TODAY, 9, 30)
    await next_run(job, clock, delay)
    assert source.times[3] == local(TODAY, 9, 30, 0)
    assert all(not (local(TODAY, 6, 30) <= t < local(TODAY, 9, 30)) for t in source.times)
    assert len(source.calls) > 4


@pytest.mark.anyio
async def test_a_start_inside_the_window_makes_no_call_and_no_auth_check(store):
    source = FakeHistorySource()
    job, clock = make_job(store, source, local(TODAY, 6, 29, 30), history_backfill_days=30)
    delay = await run_first(job, clock)  # first run at 06:30:30
    assert source.calls == [] and source.auth_checks == 0
    assert clock.now() + timedelta(seconds=delay) == local(TODAY, 9, 30)
    await next_run(job, clock, delay)
    assert source.auth_checks == 1 and source.times[0] == local(TODAY, 9, 30)


@pytest.mark.anyio
async def test_the_window_follows_the_configured_briefing_times(store):
    source = FakeHistorySource()
    job, clock = make_job(
        store,
        source,
        local(TODAY, 7, 59),
        history_backfill_days=3,
        briefing_time=dt.time(8, 0),
        briefing_cutoff=dt.time(8, 30),
    )
    await run_first(job, clock)  # 08:00:00 is inside
    assert source.calls == []


@pytest.mark.anyio
async def test_startup_runs_after_60_seconds_then_waits_for_history_time(store):
    source = FakeHistorySource()
    job, clock = make_job(store, source, local(TODAY, 14, 0), history_backfill_days=1)
    assert await job.tick() == 60.0
    assert source.calls == []
    await clock.sleep(60.0)
    delay = await job.tick()
    assert source.times[0] == local(TODAY, 14, 1)
    assert clock.now() + timedelta(seconds=delay) == local(date(2026, 10, 8), 3, 30)


@pytest.mark.anyio
async def test_today_is_the_local_date_just_after_midnight(store):
    source = FakeHistorySource()
    job, clock = make_job(store, source, local(date(2026, 10, 8), 0, 29), history_backfill_days=1)
    await run_first(job, clock)  # 00:30 local is still 10-07 in UTC
    assert source.snapshot_days == [date(2026, 10, 7)]
    (row,) = store.history_rows(None, date(2026, 10, 8))
    assert row["date"] == "2026-10-07" and row["recovery_fetched_on"] == "2026-10-08"


@pytest.mark.anyio
async def test_nothing_pending_makes_no_calls_and_no_auth_check(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    store.upsert_history_recovery(date(2026, 10, 6), {"readiness": 70}, TODAY)
    store.upsert_history_activities({date(2026, 10, 6): "{}"}, TODAY)
    source = FakeHistorySource()
    job, clock = make_job(store, source, local(TODAY, 6, 29, 30), history_backfill_days=1)  # even inside the window
    delay = await run_first(job, clock)
    assert source.calls == [] and source.auth_checks == 0
    assert clock.now() + timedelta(seconds=delay) == local(date(2026, 10, 8), 3, 30)
    assert "history status=done fetched=0 failed=0 pending=0" in caplog.text


@pytest.mark.anyio
async def test_crash_in_a_run_is_logged_and_retried(store, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    calls: list[int] = []

    def broken(start, end):
        calls.append(1)
        raise sqlite3.OperationalError("SECRET-DB-TEXT")

    monkeypatch.setattr(store, "history_rows", broken)
    source = FakeHistorySource()
    job, clock = await run_job(store, source, AFTER_WINDOW, stop_when=lambda: False, max_sleeps=3)
    assert clock.sleeps == [60.0, 3600.0, 3600.0]
    assert len(calls) == 2  # the loop ran again after the crash
    crashed = [r.getMessage() for r in caplog.records if r.name == "coach_mcp.history"]
    assert crashed == ["history status=crashed kind=OperationalError"] * 2
    assert "SECRET-DB-TEXT" not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("day", "next_day", "hours"),
    [
        (date(2026, 10, 25), date(2026, 10, 26), 24),  # the day Prague leaves summer time (03:00 -> 02:00)
        (date(2026, 10, 24), date(2026, 10, 25), 25),  # the night before: 03:30 to 03:30 is 25 real hours
        (date(2026, 3, 28), date(2026, 3, 29), 23),  # the night summer time starts (02:00 -> 03:00)
    ],
)
async def test_next_history_time_after_dst_change(store, day, next_day, hours):
    source = FakeHistorySource()
    job, clock = make_job(store, source, local(day, 3, 29), history_backfill_days=1)
    delay = await run_first(job, clock)
    assert clock.now() + timedelta(seconds=delay) == local(next_day, 3, 30)
    assert source.times[0] == local(day, 3, 30)
    assert (local(next_day, 3, 30) - source.times[0].astimezone(dt.UTC)).total_seconds() == hours * 3600


@pytest.mark.anyio
async def test_history_logs_carry_no_values_or_dates(store, caplog):
    caplog.set_level(logging.DEBUG)

    def recovery(day: date, index: int):
        return "ok", history_snapshot(
            day.isoformat(), hrv={"last_night_avg_ms": 4242, "weekly_avg_ms": 4242, "status": "SENTINEL"}
        )

    source = FakeHistorySource(recovery=recovery, activities=lambda s, e, i: ("ok", activities_payload([activity("2026-10-06", "running", 4242, 4242, 4242)])))
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    await run_first(job, clock)
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("coach_mcp")]
    assert lines and all(line.startswith("history status=") for line in lines)
    assert not any(bad in line for line in lines for bad in ("4242", "SENTINEL", "2026-", "running"))
    assert store.history_rows(None, TODAY)[-1]["hrv_status"] == "SENTINEL"  # the value was really handled


# --------------------------------------- outage rule: item errors are charged only once the backend works


def history_lines(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "coach_mcp.history"]


@pytest.mark.anyio
async def test_outage_from_run_start_charges_nothing_and_backs_off(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    good = date(2026, 10, 3)
    seed_known_good(store, good)
    source = FakeHistorySource(recovery=lambda day, index: ("ok", outage_snapshot(day)))
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=4)
    delay = await run_first(job, clock)
    assert source.calls == [
        ("list_activities", {"start_date": "2026-10-04", "end_date": "2026-10-06", "limit": 100}),
        ("get_recovery_snapshot", {"date": "2026-10-06"}),
        ("get_recovery_snapshot", {"date": "2026-10-05"}),
        ("get_recovery_snapshot", {"date": "2026-10-04"}),
        ("get_recovery_snapshot", {"date": "2026-10-03"}),  # the probe: the known-good day answers the outage too
    ]
    (record,) = history_lines(caplog)  # the run line is the only record
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("history status=service_error fetched=1 failed=0 pending=3 given_up=0 ms=")
    rows = rows_by_date(store)
    assert {rows[d]["recovery_failures"] for d in ("2026-10-04", "2026-10-05", "2026-10-06")} == {0}
    assert {rows[d]["recovery_fetched_on"] for d in ("2026-10-04", "2026-10-05", "2026-10-06")} == {None}
    assert delay == 3600.0
    assert await next_run(job, clock, delay) == 7200.0  # the backoff of rate_limited, doubled
    assert {r["recovery_failures"] for r in rows_by_date(store).values()} == {0}


@pytest.mark.anyio
async def test_backfill_completes_after_outage_runs(store):
    outage = {"on": True}

    def recovery(day: date, index: int):
        return ("ok", outage_snapshot(day)) if outage["on"] else ("ok", history_snapshot(day.isoformat()))

    def activities(start: date, end: date, index: int):
        return ("error", None) if outage["on"] else ("ok", activities_payload([]))

    source = FakeHistorySource(recovery=recovery, activities=activities)
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=30)
    delay = await run_first(job, clock)
    for _ in range(2):  # three runs inside the outage
        delay = await next_run(job, clock, delay)
    assert len(source.calls) == 3 * 4  # per run: one activities call and three snapshots, no probe (nothing known)
    assert all(r["recovery_failures"] == 0 and r["activities_failures"] == 0 for r in rows_by_date(store).values())

    outage["on"] = False  # Garmin is back
    await next_run(job, clock, delay)
    rows = rows_by_date(store)
    window = [d.isoformat() for d in days(date(2026, 9, 7), date(2026, 10, 6))]
    assert sorted(rows) == window and len(window) == 30
    assert {(r["recovery_fetched_on"], r["activities_fetched_on"]) for r in rows.values()} == {
        ("2026-10-07", "2026-10-07")
    }
    assert {(r["recovery_failures"], r["activities_failures"]) for r in rows.values()} == {(0, 0)}


@pytest.mark.anyio
@pytest.mark.parametrize("stop", ["service_error", "rate_limited", "paused"])
async def test_outage_mid_run_charges_nothing(store, stop):
    good = date(2026, 9, 29)
    back = {"on": False}

    def healthy(day: date) -> tuple[str, Any]:
        return "ok", history_snapshot(day.isoformat())

    if stop == "service_error":
        # 10-06 and 10-05 work, every later call errors, the probe (it asks 10-05, the last ok day) too
        def recovery(day: date, index: int):
            return healthy(day) if back["on"] or index <= 2 else ("ok", outage_snapshot(day))

        start, window = AFTER_WINDOW, 8
    elif stop == "rate_limited":
        # two items error (held), the next call is rate limited
        def recovery(day: date, index: int):
            if back["on"] or day in (date(2026, 10, 6), date(2026, 10, 5)):
                return healthy(day)
            return ("rate_limited", None) if day == date(2026, 10, 2) else ("error", None)

        start, window = AFTER_WINDOW, 8
    else:
        good = date(2026, 10, 3)

        def recovery(day: date, index: int):
            return healthy(day) if back["on"] else ("error", None)

        start, window = local(TODAY, 6, 27), 4
    seed_known_good(store, good)
    source = FakeHistorySource(recovery=recovery)
    delay_seconds = 30 if stop == "paused" else 20
    job, clock = make_job(
        store, source, start, history_backfill_days=window, history_call_delay_seconds=delay_seconds
    )
    delay = await run_first(job, clock)

    if stop == "paused":
        assert source.times == [local(TODAY, 6, 28, s) for s in (0, 30)] + [
            local(TODAY, 6, 29, s) for s in (0, 30)
        ]  # the probe's paced call reached 06:30:00: paused, the call itself was not made
        assert clock.now() == local(TODAY, 6, 30, 0)
        assert clock.now() + timedelta(seconds=delay) == local(TODAY, 9, 30)
        assert good not in source.snapshot_days
    elif stop == "service_error":
        assert delay == 3600.0
        assert source.snapshot_days == [date(2026, 10, n) for n in (6, 5, 4, 3, 2, 5)]  # the last one is the probe
    else:
        assert delay == 3600.0
        assert source.snapshot_days == [date(2026, 10, n) for n in (6, 5, 4, 3, 2)]  # two held errors, then the 429
    assert {r["recovery_failures"] for r in rows_by_date(store).values()} == {0}
    assert {r["activities_failures"] for r in rows_by_date(store).values()} == {0}

    back["on"] = True  # Garmin is back; the next run is on the same job object
    await next_run(job, clock, delay)
    rows = rows_by_date(store)
    pending = [d for d in days(TODAY - timedelta(days=window), date(2026, 10, 6)) if d != good]
    assert {rows[d.isoformat()]["recovery_fetched_on"] for d in pending} == {"2026-10-07"}
    assert {r["recovery_failures"] for r in rows.values()} == {0}
    assert {r["activities_failures"] for r in rows.values()} == {0}


@pytest.mark.anyio
async def test_item_errors_confirmed_by_probe(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    good = date(2026, 9, 29)
    seed_known_good(store, good)
    bad = {date(2026, 10, 6), date(2026, 10, 5), date(2026, 10, 4)}
    source = FakeHistorySource(
        recovery=lambda day, index: ("error", None) if day in bad else ("ok", history_snapshot(day.isoformat()))
    )
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=8, history_call_delay_seconds=20)
    assert await run_first(job, clock) == 3600.0
    assert source.snapshot_days == [
        date(2026, 10, 6), date(2026, 10, 5), date(2026, 10, 4),
        good,  # the probe: no ok snapshot in this run yet, so the stored known-good day
        date(2026, 10, 3), date(2026, 10, 2), date(2026, 10, 1), date(2026, 9, 30),
    ]  # fmt: skip
    assert (source.times[4] - source.times[3]).total_seconds() == 20.0  # the probe is paced like any call
    rows = rows_by_date(store)
    assert {rows[d.isoformat()]["recovery_failures"] for d in bad} == {1}  # each charged once, by the probe
    assert rows[good.isoformat()]["readiness"] == 50  # the probe answer (72) is not stored
    assert {rows[d.isoformat()]["readiness"] for d in days(date(2026, 9, 30), date(2026, 10, 3))} == {72}
    assert "history status=partial fetched=5 failed=3" in caplog.text  # act + 4 stored snapshots; probe not counted


@pytest.mark.anyio
async def test_probe_asks_the_newest_stored_day_when_no_call_worked_yet(store):
    seed_known_good(store, date(2026, 9, 29))  # the oldest day of the window
    seed_known_good(store, date(2026, 10, 1))  # a newer one: the probe asks for this
    bad = {date(2026, 10, 6), date(2026, 10, 5), date(2026, 10, 4)}
    source = FakeHistorySource(
        recovery=lambda day, index: ("error", None) if day in bad else ("ok", history_snapshot(day.isoformat()))
    )
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=8)
    await run_first(job, clock)
    assert source.snapshot_days[:4] == [date(2026, 10, 6), date(2026, 10, 5), date(2026, 10, 4), date(2026, 10, 1)]
    assert {rows_by_date(store)[d.isoformat()]["recovery_failures"] for d in bad} == {1}


@pytest.mark.anyio
async def test_trailing_item_error_is_charged_after_probe(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    bad = date(2026, 10, 4)
    source = FakeHistorySource(
        recovery=lambda day, index: ("error", None) if day == bad else ("ok", history_snapshot(day.isoformat()))
    )
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    assert await run_first(job, clock) == 3600.0
    # 10-06 and 10-05 are ok, 10-04 is the last pending day: the end-of-run probe asks the last ok day, 10-05
    assert source.snapshot_days == [date(2026, 10, 6), date(2026, 10, 5), bad, date(2026, 10, 5)]
    assert rows_by_date(store)[bad.isoformat()]["recovery_failures"] == 1
    assert "history status=partial fetched=3 failed=1" in caplog.text


@pytest.mark.anyio
async def test_trailing_item_error_with_failed_probe_is_service_error(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    bad, probed = date(2026, 10, 4), date(2026, 10, 5)
    asked: list[date] = []

    def recovery(day: date, index: int):
        asked.append(day)
        if day == bad or (day == probed and asked.count(probed) == 2):  # the second call for 10-05 is the probe
            return "ok", outage_snapshot(day)
        return "ok", history_snapshot(day.isoformat())

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    assert await run_first(job, clock) == 3600.0
    assert source.snapshot_days == [date(2026, 10, 6), probed, bad, probed]
    rows = rows_by_date(store)
    assert rows.get(bad.isoformat(), {}).get("recovery_failures", 0) == 0
    assert rows[probed.isoformat()]["recovery_fetched_on"] == "2026-10-07" and rows[probed.isoformat()]["readiness"] == 72
    assert "history status=service_error fetched=3 failed=0" in caplog.text


@pytest.mark.anyio
async def test_no_known_good_day_counts_as_outage(store):
    source = FakeHistorySource(recovery=lambda day, index: ("ok", outage_snapshot(day)))
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    assert await run_first(job, clock) == 3600.0
    assert [name for name, _ in source.calls] == ["list_activities", *["get_recovery_snapshot"] * 3]  # no probe
    assert {r["recovery_failures"] for r in rows_by_date(store).values()} == {0}


@pytest.mark.anyio
async def test_end_of_run_without_known_good_day_is_service_error(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    source = FakeHistorySource(recovery=lambda day, index: ("ok", outage_snapshot(day)))
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=2)
    assert await run_first(job, clock) == 3600.0  # two held errors at the end of the run, nothing to probe with
    assert [name for name, _ in source.calls] == ["list_activities", "get_recovery_snapshot", "get_recovery_snapshot"]
    assert {r["recovery_failures"] for r in rows_by_date(store).values()} == {0}
    assert "history status=service_error fetched=1 failed=0" in caplog.text


@pytest.mark.anyio
async def test_empty_store_stall_resolves_when_a_new_day_succeeds(store):
    broken = set(days(date(2026, 10, 4), date(2026, 10, 6)))
    source = FakeHistorySource(
        recovery=lambda day, index: ("error", None) if day in broken else ("ok", history_snapshot(day.isoformat()))
    )
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    delay = await run_first(job, clock)
    delays = [delay]
    for _ in range(3):  # the runs of 10-07 at 10:01, 11:01, 13:01 and 17:01: three broken newest dates, no proof
        delay = await next_run(job, clock, delay)
        delays.append(delay)
    assert delays == [3600.0, 7200.0, 14400.0, 28800.0]
    assert {r["recovery_failures"] for r in rows_by_date(store).values()} == {0}  # four runs, no proof, no charge
    assert {r["recovery_fetched_on"] for r in rows_by_date(store).values()} == {None}

    before = len(source.calls)
    await next_run(job, clock, delay)  # about 01:03 on 10-08: yesterday (10-07) is new and works
    assert local(date(2026, 10, 8), 1, 0) < source.times[before] < local(date(2026, 10, 8), 1, 10)  # after the 8 h wait
    asked = [(n, a) for n, a in source.calls[before:] if n == "get_recovery_snapshot"]
    assert [a["date"] for _, a in asked] == ["2026-10-07", "2026-10-06", "2026-10-05", "2026-10-07"]  # last = probe
    rows = rows_by_date(store)
    assert rows["2026-10-07"]["recovery_fetched_on"] == "2026-10-08" and rows["2026-10-07"]["recovery_failures"] == 0
    assert (rows["2026-10-06"]["recovery_failures"], rows["2026-10-05"]["recovery_failures"]) == (1, 1)


@pytest.mark.anyio
async def test_activities_errors_are_confirmed_only_by_activities(store):
    good = date(2026, 9, 22)
    seed_known_good(store, good)
    source = FakeHistorySource(activities=lambda start, end, index: ("error", None))
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=15)
    assert await run_first(job, clock) == 3600.0
    assert len(source.snapshot_days) == 14  # the recovery part works and goes on
    rows = rows_by_date(store)
    assert {rows[d.isoformat()]["recovery_fetched_on"] for d in days(date(2026, 9, 23), date(2026, 10, 6))} == {
        "2026-10-07"
    }
    assert source.activity_ranges.count((good, good)) == 1  # one probe, of the activities part, at the end
    assert source.activity_ranges[-1] == (good, good)
    assert len(source.activity_ranges) == 3  # two blocks and the probe
    assert {r["activities_failures"] for r in rows.values()} == {0}


@pytest.mark.anyio
async def test_probe_is_not_stored_and_not_counted(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    bad, probed = date(2026, 10, 4), date(2026, 10, 5)
    asked: list[date] = []

    def recovery(day: date, index: int):
        asked.append(day)
        if day == bad:
            return "error", None
        if day == probed and asked.count(probed) == 2:  # the probe answers differently from the stored fetch
            return "ok", history_snapshot(day.isoformat(), readiness={"score": 60})
        return "ok", history_snapshot(day.isoformat())

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    await run_first(job, clock)
    assert source.snapshot_days == [date(2026, 10, 6), probed, bad, probed]
    rows = rows_by_date(store)
    assert rows[probed.isoformat()]["readiness"] == 72  # not overwritten by the probe's 60
    assert rows[bad.isoformat()]["recovery_failures"] == 1
    (record,) = history_lines(caplog)
    assert record.getMessage().startswith("history status=partial fetched=3 failed=1")  # act + 2 snapshots


@pytest.mark.anyio
async def test_refetch_errors_stop_after_three_failures(store):
    bad = date(2026, 10, 5)
    for day in days(date(2026, 10, 4), date(2026, 10, 6)):  # data from yesterday: inside the re-fetch window
        seed_day(store, day, fetched_on=date(2026, 10, 6), readiness=60)
        seed_activities(store, day, {}, fetched_on=TODAY)
    source = FakeHistorySource(
        recovery=lambda day, index: ("error", None) if day == bad else ("ok", history_snapshot(day.isoformat()))
    )
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    delay = await run_first(job, clock)  # 10-06 ok, 10-05 held, 10-04 ok confirms: charged
    for _ in range(2):  # runs 2 and 3: the bad day alone, confirmed by the probe of a day fetched today
        delay = await next_run(job, clock, delay)
    assert source.snapshot_days.count(bad) == 3
    row = rows_by_date(store)[bad.isoformat()]
    assert row["recovery_failures"] == 3 and row["readiness"] == 60 and row["recovery_fetched_on"] == "2026-10-06"
    before = len(source.calls)
    await next_run(job, clock, delay)
    assert len(source.calls) == before  # the fourth run on the same date does not call it again


@pytest.mark.anyio
async def test_service_error_shares_the_rate_limit_backoff(store):
    mode = {"value": "limited"}

    def recovery(day: date, index: int):
        if mode["value"] == "limited":
            return "rate_limited", None
        return ("ok", outage_snapshot(day)) if mode["value"] == "outage" else ("ok", history_snapshot(day.isoformat()))

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=2)
    delay = await run_first(job, clock)
    assert delay == 3600.0  # rate_limited
    mode["value"] = "outage"
    delay = await next_run(job, clock, delay)
    assert delay == 7200.0  # service_error continues the same backoff
    mode["value"] = "ok"
    delay = await next_run(job, clock, delay)  # done: the backoff is reset
    assert clock.now() + timedelta(seconds=delay) == local(date(2026, 10, 8), 3, 30)
    mode["value"] = "outage"
    delay = await next_run(job, clock, delay)
    assert delay == 3600.0
    assert await next_run(job, clock, delay) == 7200.0


@pytest.mark.anyio
async def test_run_log_counts_given_up_days(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    for day in (date(2026, 10, 4), date(2026, 10, 5)):
        store.record_history_failure([day] * 3, "recovery")
    for day in days(date(2026, 10, 4), date(2026, 10, 6)):
        seed_activities(store, day, {}, fetched_on=TODAY)
    seed_day(store, date(2026, 10, 6), fetched_on=TODAY, readiness=50)
    source = FakeHistorySource()
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    await run_first(job, clock)
    assert source.calls == []
    (record,) = history_lines(caplog)
    assert record.getMessage().startswith("history status=done fetched=0 failed=0 pending=0 given_up=2 ms=")
    assert "2026" not in record.getMessage()


@pytest.mark.anyio
async def test_history_time_inside_window_runs_at_cutoff_with_one_warning(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    source = FakeHistorySource()
    # 07:00 is inside the default briefing window 06:30-09:30; nothing refuses it, the run waits for the cutoff.
    await run_job(
        store,
        source,
        local(TODAY, 5, 0),
        settings_overrides={"history_time": dt.time(7, 0), "history_backfill_days": 2},
        stop_when=lambda: len(source.calls) >= 4,
    )
    warnings = [r for r in history_lines(caplog) if "config_warning" in r.getMessage()]
    assert [(r.levelno, r.getMessage()) for r in warnings] == [
        (logging.WARNING, "history status=config_warning reason=time_in_briefing_window")
    ]
    assert len(source.calls) == 4 and source.times[:3] == [local(TODAY, 5, 1, s) for s in (0, 15, 30)]
    # the run due at 2026-10-08 07:00 is paused: its first call is at the cutoff, none inside the window
    assert source.times[3] == local(date(2026, 10, 8), 9, 30)
    assert all(not (local(d, 6, 30) <= t < local(d, 9, 30)) for t in source.times for d in (TODAY, date(2026, 10, 8)))


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("history_time", "warns"),
    [("03:30", False), ("06:29", False), ("06:30", True), ("09:29", True), ("09:30", False)],
)
async def test_config_warning_only_for_a_time_inside_the_window(store, caplog, history_time, warns):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    await run_job(
        store,
        FakeHistorySource(),
        AFTER_WINDOW,
        settings_overrides={"history_time": dt.time.fromisoformat(history_time)},
        max_sleeps=1,  # the warning comes before the loop; stop at the first sleep
    )
    lines = [r.getMessage() for r in history_lines(caplog) if "config_warning" in r.getMessage()]
    assert lines == (["history status=config_warning reason=time_in_briefing_window"] if warns else [])


# ------------------------------------------------------- strength (Hevy) part

from fakes import FakeStrengthSource  # noqa: E402
from hevy_payloads import day_workout, deleted, events_page, hevy_template, updated  # noqa: E402
from history_helpers import seed_strength  # noqa: E402

from coach_mcp import strength as strength_module  # noqa: E402
from coach_mcp.history import combined_outcome  # noqa: E402
from coach_mcp.store import Store  # noqa: E402

FIRST_DAY = "2026-09-27"  # TODAY - 10 days: the first day of a backfill of 10 days
FIRST_SINCE = "2026-09-26T22:00:00Z"  # its local midnight (CEST) as UTC


def pages_of(*pages: list[dict[str, Any]], count: bool = True):
    """An ``events`` function answering page n with ``pages[n - 1]`` (empty past the end)."""

    def events(page: int, since: str, index: int):
        batch = pages[page - 1] if page <= len(pages) else []
        return "ok", events_page(batch, page, len(pages) if count else None)

    return events


def wo(id: str, day: str = "2026-10-05", template_id: str = "T1", **kwargs: Any) -> dict[str, Any]:
    return updated(day_workout(id, day, template_id, **kwargs))


def strength_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "coach_mcp.history" and "part=strength" in r.getMessage()]


def garmin_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "coach_mcp.history" and "part=strength" not in r.getMessage()]


def stored_workouts(store) -> list[dict[str, Any]]:
    """The live workouts without the wall-clock column."""
    return [{k: v for k, v in r.items() if k != "updated_at"} for r in store.strength_workout_rows(None, date(2030, 1, 1))]


def all_workouts(db_path) -> dict[str, tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return {r[0]: r[1:] for r in conn.execute("SELECT workout_id, date, deleted, hevy_updated_at, exercises FROM strength_workouts")}
    finally:
        conn.close()


def sync_without_clock(store) -> dict[str, Any] | None:
    return store.hevy_sync_state()


@pytest.mark.anyio
async def test_strength_first_run_lists_the_backfill_range(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    first_page = [wo(f"W{n:02d}", day=f"2026-09-{28 + n % 3}", template_id=f"T{n % 3 + 1}") for n in range(10)]
    second_page = [wo(f"W{n:02d}", template_id=f"T{n % 3 + 1}") for n in range(10, 13)]
    strength = FakeStrengthSource(events=pages_of(first_page, second_page))
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=strength, history_backfill_days=10)
    await run_first(job, clock)

    assert strength.calls[0] == ("get-workout-events", {"page": 1, "page_size": 10, "since": FIRST_SINCE})
    assert strength.calls[1] == ("get-workout-events", {"page": 2, "page_size": 10, "since": FIRST_SINCE})
    assert strength.pages == [1, 2]
    assert len(store.strength_workout_rows(None, TODAY)) == 13
    assert strength.template_ids == ["T1", "T2", "T3"]  # once each, in id order, after the listing
    assert [name for name, _ in strength.calls] == ["get-workout-events"] * 2 + ["get-exercise-template"] * 3
    state = store.hevy_sync_state()
    assert (state["covered_from"], state["synced_on"]) == (FIRST_DAY, "2026-10-07")
    # the sync point is the instant the listing started, i.e. before the pause that precedes its first call
    assert datetime.fromisoformat(state["synced_at"]) == strength.times[0] - timedelta(seconds=15)
    lines = strength_lines(caplog)
    assert len(lines) == 1 and lines[0].startswith("history status=done part=strength workouts=13 deleted=0 templates=3 failed=0 skipped=0 ms=")
    assert {t["found"] for t in store.hevy_templates().values()} == {1}


@pytest.mark.anyio
async def test_strength_calls_are_paced_like_garmin_calls(store):
    garmin = FakeHistorySource()
    strength = FakeStrengthSource(events=pages_of([wo("W1"), wo("W2", template_id="T2")]))
    job, clock = make_job(
        store, garmin, AFTER_WINDOW, strength=strength, history_backfill_days=3, history_call_delay_seconds=20
    )
    await run_first(job, clock)
    assert garmin.times and len(strength.calls) == 3
    merged = sorted([*garmin.times, *strength.times])
    assert [b - a for a, b in zip(merged, merged[1:], strict=False)] == [timedelta(seconds=20)] * (len(merged) - 1)
    assert max(garmin.times) < min(strength.times)  # Garmin first, then Hevy, with the same delay in between


@pytest.mark.anyio
async def test_strength_runs_once_a_day(store):
    garmin = FakeHistorySource(auth="missing")  # the Garmin part fails, so the run is retried an hour later
    strength = FakeStrengthSource(events=pages_of([wo("W1")]))
    job, clock = make_job(store, garmin, AFTER_WINDOW, strength=strength, history_backfill_days=3)
    delay = await run_first(job, clock)
    assert delay == 3600.0 and len(strength.calls) == 2  # the Hevy part ran although Garmin did not
    garmin.auth = "ok"
    delay = await next_run(job, clock, delay)  # the same day: nothing to list, no template to fetch
    assert len(strength.calls) == 2
    assert clock.now() + timedelta(seconds=delay) == local(date(2026, 10, 8), 3, 30)
    synced_at = datetime.fromisoformat(store.hevy_sync_state()["synced_at"])
    await next_run(job, clock, delay)  # the next day: one listing page since the overlap, no template call
    assert [name for name, _ in strength.calls[2:]] == ["get-workout-events"]
    assert strength.calls[2][1]["since"] == strength_module.since_text(synced_at - timedelta(days=3))
    assert store.hevy_sync_state()["synced_on"] == "2026-10-08"


@pytest.mark.anyio
async def test_strength_rereading_events_is_idempotent(store):
    strength = FakeStrengthSource(events=pages_of([wo("W1", "2026-10-05"), wo("W2", "2026-10-06", sets=4)]))
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=strength, history_backfill_days=3)
    delay = await run_first(job, clock)
    before = stored_workouts(store)
    assert [r["date"] for r in before] == ["2026-10-05", "2026-10-06"]
    await next_run(job, clock, delay)  # the next day reads the same page again
    assert strength.pages == [1, 1]
    assert stored_workouts(store) == before


def evolving(events: dict[str, list[dict[str, Any]]]):
    """An ``events`` function whose single page is ``events["now"]``; a test changes it between runs."""
    return lambda page, since, index: ("ok", events_page(events["now"], page, 1))


@pytest.mark.anyio
async def test_edited_workout_replaces_its_day(store):
    events = {"now": [wo("W1", sets=3, updated_at="2026-10-05T11:00:00Z")]}
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=FakeStrengthSource(events=evolving(events)), history_backfill_days=3)
    delay = await run_first(job, clock)
    (row,) = stored_workouts(store)
    assert json.loads(row["exercises"])[0]["sets"] == 3
    events["now"] = [wo("W1", sets=5, weight_kg=110, updated_at="2026-10-06T08:00:00Z")]
    await next_run(job, clock, delay)
    (row,) = stored_workouts(store)
    assert json.loads(row["exercises"])[0]["sets"] == 5 and json.loads(row["exercises"])[0]["volume_kg"] == 2750.0
    assert row["date"] == "2026-10-05" and row["hevy_updated_at"] == "2026-10-06T08:00:00.000000Z"


@pytest.mark.anyio
async def test_moved_workout_changes_its_day(store):
    events = {"now": [wo("W1", "2026-10-05", updated_at="2026-10-05T11:00:00Z")]}
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=FakeStrengthSource(events=evolving(events)), history_backfill_days=3)
    delay = await run_first(job, clock)
    assert [r["date"] for r in stored_workouts(store)] == ["2026-10-05"]
    events["now"] = [wo("W1", "2026-10-06", updated_at="2026-10-06T08:00:00Z")]
    await next_run(job, clock, delay)
    assert [r["date"] for r in stored_workouts(store)] == ["2026-10-06"]  # one row, not two
    assert store.strength_workout_rows(date(2026, 10, 5), date(2026, 10, 5)) == []


@pytest.mark.anyio
async def test_older_update_does_not_overwrite_a_newer_one(store):
    events = {"now": [wo("W1", sets=5, updated_at="2026-10-06T08:00:00Z")]}
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=FakeStrengthSource(events=evolving(events)), history_backfill_days=3)
    delay = await run_first(job, clock)
    events["now"] = [wo("W1", sets=3, updated_at="2026-10-05T11:00:00Z")]  # a stale copy of the same workout
    await next_run(job, clock, delay)
    (row,) = stored_workouts(store)
    assert json.loads(row["exercises"])[0]["sets"] == 5


@pytest.mark.anyio
async def test_deleted_workout_leaves_its_day_and_stays_deleted(store, db_path):
    events = {"now": [wo("W1", "2026-10-05"), wo("W2", "2026-10-05", template_id="T2")]}
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=FakeStrengthSource(events=evolving(events)), history_backfill_days=3)
    delay = await run_first(job, clock)
    assert [r["workout_id"] for r in store.strength_workout_rows(None, TODAY)] == ["W1", "W2"]
    events["now"] = [deleted("W1")]
    delay = await next_run(job, clock, delay)
    assert [r["workout_id"] for r in store.strength_workout_rows(None, TODAY)] == ["W2"]
    assert store.strength_template_ids() == {"T2"}
    events["now"] = [wo("W1", "2026-10-05", updated_at="2030-01-01T00:00:00Z")]  # an old "updated" read again later
    await next_run(job, clock, delay)
    assert [r["workout_id"] for r in store.strength_workout_rows(None, TODAY)] == ["W2"]
    assert all_workouts(db_path)["W1"][1] == 1  # the tombstone is still there


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("outcome", "status"),
    [("unreachable", "unreachable"), ("no_credentials", "no_credentials"), ("rate_limited", "rate_limited"), ("auth_error", "auth")],
)
async def test_strength_failure_does_not_touch_the_garmin_part(tmp_path, caplog, outcome, status):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")

    async def run(strength: FakeStrengthSource, name: str):
        caplog.clear()
        db = Store(tmp_path / name)
        job, clock = make_job(db, FakeHistorySource(), AFTER_WINDOW, strength=strength, history_backfill_days=3)
        first = await run_first(job, clock)
        garmin = [re.sub(r" ms=\d+", "", line) for line in garmin_lines(caplog)]
        rows = [{k: v for k, v in r.items() if k != "updated_at"} for r in db.history_rows(None, date(2030, 1, 1))]
        line = strength_lines(caplog)[0]
        second = await next_run(job, clock, first)
        return db, rows, garmin, line, first, second

    good_db, good_rows, good_garmin, good_line, good_first, _ = await run(FakeStrengthSource(), "good.db")
    assert "history status=done part=strength" in good_line and good_first > 3600
    bad_db, bad_rows, bad_garmin, bad_line, bad_first, bad_second = await run(
        FakeStrengthSource(events=lambda page, since, index: (outcome, None)), "bad.db"
    )
    assert bad_rows == good_rows and bad_garmin == good_garmin  # the Garmin part is exactly as without a Hevy problem
    assert bad_line.startswith(f"history status={status} part=strength workouts=0 deleted=0 templates=0 failed=0 skipped=0 ms=")
    assert bad_db.hevy_sync_state() is None
    assert bad_first == 3600.0
    assert bad_second == (7200.0 if outcome == "rate_limited" else 3600.0)  # the shared backoff doubles
    assert good_db.hevy_sync_state() is not None


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["rate_limited", "unreachable"])
async def test_strength_warning_levels(store, caplog, outcome):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    strength = FakeStrengthSource(events=lambda page, since, index: (outcome, None))
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=strength, history_backfill_days=1)
    await run_first(job, clock)
    levels = {r.getMessage().split(" ")[1]: r.levelno for r in caplog.records if r.name == "coach_mcp.history"}
    assert levels == {"status=done": logging.INFO, f"status={outcome}": logging.WARNING}


@pytest.mark.anyio
async def test_garmin_stop_does_not_stop_the_strength_part(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    garmin = FakeHistorySource(auth="missing")
    strength = FakeStrengthSource(events=pages_of([wo("W1")]))
    job, clock = make_job(store, garmin, AFTER_WINDOW, strength=strength, history_backfill_days=3)
    delay = await run_first(job, clock)
    assert garmin.calls == [] and delay == 3600.0  # Garmin: auth problem, the run outcome is "auth"
    assert garmin_lines(caplog)[0].startswith("history status=auth fetched=0")
    assert strength_lines(caplog)[0].startswith("history status=done part=strength workouts=1")
    assert len(store.strength_workout_rows(None, TODAY)) == 1 and store.hevy_sync_state() is not None


def full_page(page: int) -> list[dict[str, Any]]:
    return [wo(f"P{page}-{n}") for n in range(strength_module.EVENTS_PAGE_SIZE)]


def listing_case(name: str):
    if name == "empty page claiming more":
        return lambda page, since, index: ("ok", events_page(full_page(1) if page == 1 else [], page, 3))
    if name == "page echo mismatch":
        return lambda page, since, index: ("ok", events_page(full_page(page), 2, 3))
    if name == "page cap":
        return lambda page, since, index: ("ok", events_page(full_page(page), page))  # always a full page, no count
    if name == "error on page 2":
        return lambda page, since, index: ("ok", events_page(full_page(1), 1, 3)) if page == 1 else ("error", None)
    raise AssertionError(name)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "stored"),
    [("empty page claiming more", 10), ("page echo mismatch", 0), ("page cap", 20), ("error on page 2", 10)],
)
async def test_strength_listing_failures(store, caplog, monkeypatch, name, stored):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    monkeypatch.setattr(strength_module, "MAX_EVENT_PAGES", 2)
    strength = FakeStrengthSource(events=listing_case(name))
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=strength, history_backfill_days=3)
    delay = await run_first(job, clock)
    assert delay == 3600.0 and clock.now() + timedelta(seconds=delay) < local(date(2026, 10, 8), 3, 30)  # the shared backoff
    assert len(store.strength_workout_rows(None, TODAY)) == stored  # events of the pages before the failure stay
    assert store.hevy_sync_state() is None  # the sync point did not advance
    assert strength.template_ids == []  # no template call after a failed listing
    (line,) = strength_lines(caplog)
    assert line.startswith("history status=service_error part=strength") and " failed=1 " in line
    calls = len(strength.calls)
    await next_run(job, clock, delay)  # nothing was given up: the next run lists from page 1 again
    assert strength.pages[calls:][:1] == [1] and len(strength.calls) > calls


@pytest.mark.anyio
async def test_strength_listing_ends_at_the_empty_page_past_the_end(store):
    def events(page: int, since: str, index: int):
        return "ok", events_page(full_page(1) if page == 1 else [], page)  # no page_count: hevy-mcp's empty last page

    strength = FakeStrengthSource(events=events)
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=strength, history_backfill_days=3)
    await run_first(job, clock)
    assert strength.pages == [1, 2]
    assert len(store.strength_workout_rows(None, TODAY)) == 10 and store.hevy_sync_state() is not None


def template_down(down: dict[str, bool], broken: set[str] | None = None):
    def template(tid: str, index: int):
        if down["on"] or tid in (broken or set()):
            return "error", None
        return "ok", {"exercise_template": hevy_template(tid, f"T {tid}", "chest")}

    return template


@pytest.mark.anyio
async def test_template_outage_stops_after_three_and_gives_nothing_up(store):
    for n in range(1, 6):
        seed_strength(store, f"W{n}", date(2026, 10, 5), [{"template_id": f"T{n}", "title": None, "sets": 1, "volume_kg": 0.0, "best": None}])
    down = {"on": True}
    strength = FakeStrengthSource(template=template_down(down))
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=strength, history_backfill_days=3)
    delay = await run_first(job, clock)
    runs = 1
    while clock.now().astimezone(PRAGUE_ZONE).date() < TODAY + timedelta(days=3) and runs < 40:
        before = len(strength.template_ids)
        delay = await next_run(job, clock, delay)
        runs += 1
        assert len(strength.template_ids) - before <= 3  # the outage bound: three wasted calls per run
    assert runs > 3 and len(strength.template_ids) > 5  # it kept trying on the later days
    rows = store.hevy_templates()
    assert set(rows) == {f"T{n}" for n in range(1, 6)}
    assert {r["found"] for r in rows.values()} == {None}  # nothing was ever marked found 0 or 1
    assert all(r["attempted_on"] is not None for r in rows.values())
    down["on"] = False  # the fourth day: Hevy is back
    for _ in range(4):
        delay = await next_run(job, clock, delay)
    assert {r["found"] for r in store.hevy_templates().values()} == {1}


PRAGUE_ZONE = ZoneInfo("Europe/Prague")


@pytest.mark.anyio
async def test_a_broken_template_does_not_block_the_others(store):
    events = {"now": [wo(f"W{n}", template_id=f"T{n}") for n in range(1, 5)]}
    strength = FakeStrengthSource(events=evolving(events), template=template_down({"on": False}, broken={"T2"}))
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=strength, history_backfill_days=3)
    delay = await run_first(job, clock)
    assert strength.template_ids == ["T1", "T2", "T3", "T4"]  # the broken one did not stop the rest
    assert delay == 3600.0  # partial: try again in an hour
    rows = store.hevy_templates()
    assert (rows["T1"]["found"], rows["T2"]["found"], rows["T3"]["found"], rows["T4"]["found"]) == (1, None, 1, 1)
    assert rows["T2"]["attempted_on"] == "2026-10-07"
    delay = await next_run(job, clock, delay)  # the same day: T2 was tried today, nothing else is open
    assert len(strength.template_ids) == 4
    events["now"] = [wo("W5", template_id="T5")]
    await next_run(job, clock, delay)  # the next day: the new template first, the one that failed yesterday last
    assert strength.template_ids[4:] == ["T5", "T2"]


@pytest.mark.anyio
async def test_template_null_is_cached_as_not_found(store):
    strength = FakeStrengthSource(events=pages_of([wo("W1")]), template=lambda tid, index: ("ok", {"exercise_template": None}))
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=strength, history_backfill_days=3)
    delay = await run_first(job, clock)
    row = store.hevy_templates()["T1"]
    assert (row["found"], row["title"], row["primary_muscle_group"], row["attempted_on"]) == (0, None, None, "2026-10-07")
    await next_run(job, clock, delay)
    assert strength.template_ids == ["T1"]  # not requested again


@pytest.mark.anyio
@pytest.mark.parametrize(
    "answer",
    [("error", None), ("ok", {}), ("ok", {"exercise_template": "a string"}), ("ok", {"exercise_template": ["x"]}), ("ok", {"other": 1})],
)
async def test_template_answers_that_are_not_a_template_or_null_are_failures(store, caplog, answer):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    strength = FakeStrengthSource(events=pages_of([wo("W1")]), template=lambda tid, index: answer)
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=strength, history_backfill_days=3)
    assert await run_first(job, clock) == 3600.0
    row = store.hevy_templates()["T1"]
    assert row["found"] is None and row["attempted_on"] == "2026-10-07"
    assert strength_lines(caplog)[0].startswith("history status=partial part=strength workouts=1 deleted=0 templates=0 failed=1")


@pytest.mark.anyio
async def test_template_fields_are_validated(store):
    def template(tid: str, index: int):
        return "ok", {
            "exercise_template": {
                "id": tid,
                "title": "  " + "t" * 250,
                "type": "x" * 41,
                "primary_muscle_group": "",
            }
        }

    strength = FakeStrengthSource(events=pages_of([wo("W1"), wo("W2", template_id="T2")]), template=template)
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=strength, history_backfill_days=3)
    await run_first(job, clock)
    for tid in ("T1", "T2"):
        row = store.hevy_templates()[tid]
        assert (row["found"], row["title"], row["type"], row["primary_muscle_group"]) == (1, "t" * 200, None, None)

    def valid(tid: str, index: int):
        return "ok", {"exercise_template": {"id": tid, "title": 5, "type": "x" * 40, "primary_muscle_group": "g" * 40}}

    other = Store(store.path.parent / "other.db")
    strength = FakeStrengthSource(events=pages_of([wo("W1")]), template=valid)
    job, clock = make_job(other, FakeHistorySource(), AFTER_WINDOW, strength=strength, history_backfill_days=3)
    await run_first(job, clock)
    row = other.hevy_templates()["T1"]
    assert (row["title"], row["type"], row["primary_muscle_group"]) == (None, "x" * 40, "g" * 40)


@pytest.mark.anyio
async def test_strength_resumes_after_a_restart_without_refetching_templates(store, tmp_path):
    events = pages_of([wo(f"W{n}", template_id=f"T{n}") for n in range(1, 7)])

    def limited(tid: str, index: int):
        # call 0 is the listing; calls 1-3 resolve T1-T3, the fourth template call is rate limited
        return ("rate_limited", None) if index >= 4 else ("ok", {"exercise_template": hevy_template(tid, f"T {tid}", "chest")})

    first = FakeStrengthSource(events=events, template=limited)
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=first, history_backfill_days=3)
    delay = await run_first(job, clock)
    assert delay == 3600.0 and first.template_ids == ["T1", "T2", "T3", "T4"]
    assert set(store.hevy_templates()) == {"T1", "T2", "T3"}
    # the process restarts: a new job on the same store, Hevy works again
    second = FakeStrengthSource(events=events)
    again = HistoryJob_for(store, second, clock)
    await again.tick()  # the 60 s start-up delay
    await clock.sleep(60.0)
    await again.tick()
    assert second.pages == [] and second.template_ids == ["T4", "T5", "T6"]  # the listing was complete; T1-T3 are cached

    # an uninterrupted run reaches the same stored state
    clean_store = Store(tmp_path / "clean.db")
    clean = FakeStrengthSource(events=events)
    clean_job, clean_clock = make_job(clean_store, FakeHistorySource(), AFTER_WINDOW, strength=clean, history_backfill_days=3)
    await run_first(clean_job, clean_clock)

    def state(db: Store):
        templates = {k: {c: v for c, v in r.items() if c != "updated_at"} for k, r in db.hevy_templates().items()}
        return stored_workouts(db), templates, db.hevy_sync_state()

    assert state(store) == state(clean_store)


def HistoryJob_for(store, strength, clock):
    from coach_mcp.config import Settings
    from coach_mcp.history import HistoryJob

    settings = Settings(timezone="Europe/Prague", history_backfill_days=3)
    return HistoryJob(settings, store, FakeHistorySource(clock=clock), now=clock.now, sleep=clock.sleep, strength=strength)


@pytest.mark.anyio
async def test_strength_restart_during_the_listing_repeats_it(store):
    cut = {"on": True}

    def events(page: int, since: str, index: int):
        if page == 2 and cut["on"]:
            return "unreachable", None
        return "ok", events_page(full_page(page) if page == 1 else [wo("LAST")], page, 2)

    strength = FakeStrengthSource(events=events)
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=strength, history_backfill_days=3)
    await run_first(job, clock)
    assert strength.pages == [1, 2] and store.hevy_sync_state() is None and strength.template_ids == []
    cut["on"] = False
    second = FakeStrengthSource(events=events)
    again = HistoryJob_for(store, second, clock)
    await again.tick()
    await clock.sleep(60.0)
    await again.tick()
    assert second.pages == [1, 2]  # an interrupted listing starts again from the first page
    assert len(store.strength_workout_rows(None, TODAY)) == 11 and store.hevy_sync_state() is not None


@pytest.mark.anyio
async def test_longer_backfill_relists_from_the_new_first_day(store):
    first = FakeStrengthSource(events=pages_of([wo("W1")]))
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=first, history_backfill_days=10)
    await run_first(job, clock)
    assert first.calls[0][1]["since"] == FIRST_SINCE
    assert store.hevy_sync_state()["covered_from"] == FIRST_DAY

    longer = FakeStrengthSource(events=pages_of([wo("W1")]))
    again = HistoryJob_for(store, longer, clock)
    again._settings = Settings(timezone="Europe/Prague", history_backfill_days=20)
    await again.tick()
    await clock.sleep(60.0)
    await again.tick()
    # a full listing from the local midnight of the new first day, although the last listing was today
    assert longer.calls[0] == ("get-workout-events", {"page": 1, "page_size": 10, "since": "2026-09-16T22:00:00Z"})
    assert store.hevy_sync_state()["covered_from"] == "2026-09-17"

    shorter = FakeStrengthSource(events=pages_of([wo("W1")]))
    again = HistoryJob_for(store, shorter, clock)
    again._settings = Settings(timezone="Europe/Prague", history_backfill_days=5)
    await again.tick()
    await clock.sleep(60.0)
    await again.tick()
    assert shorter.calls == []  # a shorter range needs nothing, and covered_from does not move
    assert store.hevy_sync_state()["covered_from"] == "2026-09-17"


@pytest.mark.anyio
async def test_shorter_backfill_keeps_covered_from_on_the_next_listing(store):
    first = FakeStrengthSource(events=pages_of([wo("W1")]))
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=first, history_backfill_days=10)
    delay = await run_first(job, clock)
    shorter = HistoryJob_for(store, FakeStrengthSource(events=pages_of([wo("W1")])), clock)
    shorter._settings = Settings(timezone="Europe/Prague", history_backfill_days=5)
    shorter._due = clock.now()
    await clock.sleep(delay)  # the next day
    await shorter.tick()
    state = store.hevy_sync_state()
    assert state["synced_on"] == "2026-10-08" and state["covered_from"] == FIRST_DAY  # not moved to a later day


@pytest.mark.anyio
async def test_strength_pauses_in_the_briefing_window(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    strength = FakeStrengthSource(events=pages_of([wo("W1")]))
    job, clock = make_job(store, FakeHistorySource(), local(TODAY, 6, 29, 30), strength=strength, history_backfill_days=3)
    delay = await run_first(job, clock)  # starts at 06:30:30, inside the window
    assert strength.calls == []
    assert strength_lines(caplog)[0].startswith("history status=paused part=strength")
    assert clock.now() + timedelta(seconds=delay) == local(TODAY, 9, 30)
    await next_run(job, clock, delay)
    assert strength.times[0] >= local(TODAY, 9, 30) and len(strength.calls) == 2


@pytest.mark.anyio
async def test_strength_pause_mid_listing(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    yesterday = date(2026, 10, 6)
    seed_day(store, yesterday, fetched_on=TODAY, readiness=50)  # nothing for the Garmin part to do
    seed_activities(store, yesterday, {}, fetched_on=TODAY)
    strength = FakeStrengthSource(events=lambda page, since, index: ("ok", events_page(full_page(page), page)))
    job, clock = make_job(store, FakeHistorySource(), local(TODAY, 6, 28, 50), strength=strength, history_backfill_days=1)
    delay = await run_first(job, clock)  # 06:29:50: the first call is not paced, the pause before page 2 ends inside
    assert strength.pages == [1]
    assert strength_lines(caplog)[0].startswith("history status=paused part=strength workouts=10")
    assert store.hevy_sync_state() is None and len(store.strength_workout_rows(None, TODAY)) == 10
    assert clock.now() + timedelta(seconds=delay) == local(TODAY, 9, 30)


_OUTCOMES = ["done", "partial", "paused", "rate_limited", "service_error", "auth", "unreachable", "no_credentials"]


def test_combined_outcome_table():
    assert combined_outcome("done", "done") == "done"
    for strength_outcome in _OUTCOMES:
        assert combined_outcome("done", strength_outcome) == strength_outcome
    for garmin_outcome in _OUTCOMES[1:]:
        assert combined_outcome(garmin_outcome, "done") == garmin_outcome
        assert combined_outcome(garmin_outcome, "rate_limited") == garmin_outcome


@pytest.mark.anyio
async def test_without_a_strength_source_there_is_no_strength_line(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, history_backfill_days=1)
    await run_first(job, clock)
    assert len(garmin_lines(caplog)) == 1 and strength_lines(caplog) == []


@pytest.mark.anyio
async def test_strength_logs_carry_no_values_or_dates(store, caplog):
    caplog.set_level(logging.DEBUG)
    sentinel = day_workout("W-4242", "2026-10-05", "T-4242", weight_kg=4242, title="SENTINEL", updated_at="2026-10-05T11:00:00Z")
    strength = FakeStrengthSource(events=pages_of([updated(sentinel)]), template=lambda tid, i: ("ok", {"exercise_template": hevy_template(tid, "SENTINEL", "chest")}))
    job, clock = make_job(store, FakeHistorySource(), AFTER_WINDOW, strength=strength, history_backfill_days=3)
    await run_first(job, clock)
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("coach_mcp")]
    assert any("part=strength" in line for line in lines)
    assert lines and all(line.startswith("history status=") for line in lines)
    assert not any(bad in line for line in lines for bad in ("4242", "SENTINEL", "2026-", "chest"))
    assert store.hevy_templates()["T-4242"]["title"] == "SENTINEL"  # the values were really handled


def strength_event_line(caplog) -> str:
    (line,) = strength_lines(caplog)
    return line


@pytest.mark.anyio
async def test_skipped_events_are_counted_in_the_strength_line(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    page = [
        wo("W1"),
        {"type": "weird", "id": "W9"},  # unknown type
        updated(day_workout("W2", "garbage")),  # unparsable start
        {"type": "deleted"},  # delete without an id
    ]
    job, clock = make_job(
        store, FakeHistorySource(), AFTER_WINDOW, strength=FakeStrengthSource(events=pages_of(page)), history_backfill_days=3
    )
    await run_first(job, clock)
    line = strength_event_line(caplog)
    assert line.startswith("history status=done part=strength workouts=1 deleted=0 templates=1 failed=0 skipped=3 ms=")
    assert [r["workout_id"] for r in store.strength_workout_rows(None, TODAY)] == ["W1"]  # the listing still completed
    assert store.hevy_sync_state() is not None


@pytest.mark.anyio
async def test_extreme_timestamps_skip_the_event_and_do_not_crash_the_run(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    page = [
        wo("W1"),
        updated(day_workout("EDGE1", "2026-10-05") | {"start_time": "9999-12-31T23:00:00-05:00"}),
        updated(day_workout("EDGE2", "2026-10-05") | {"start_time": "0001-01-01T00:30:00+01:00"}),
        updated(day_workout("EDGE3", "2026-10-05") | {"start_time": "9999-12-31T23:59:59"}),
        updated(day_workout("EDGE4", "2026-10-05") | {"start_time": "99999-01-01T00:00:00Z"}),
    ]
    job, clock = make_job(
        store, FakeHistorySource(), AFTER_WINDOW, strength=FakeStrengthSource(events=pages_of(page)), history_backfill_days=3
    )
    delay = await run_first(job, clock)  # a crash would give 3600 and a "crashed" line
    assert delay > 3600.0 and not [r for r in caplog.records if "crashed" in r.getMessage()]
    assert strength_event_line(caplog).startswith("history status=done part=strength workouts=1 deleted=0 templates=1 failed=0 skipped=4")
    assert [r["workout_id"] for r in store.strength_workout_rows(None, TODAY)] == ["W1"]
    assert store.hevy_sync_state() is not None


@pytest.mark.anyio
async def test_strength_line_comes_before_the_garmin_line(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    job, clock = make_job(
        store, FakeHistorySource(), AFTER_WINDOW, strength=FakeStrengthSource(events=pages_of([wo("W1")])), history_backfill_days=1
    )
    await run_first(job, clock)
    messages = [r.getMessage() for r in caplog.records if r.name == "coach_mcp.history"]
    assert "part=strength" in messages[0] and messages[1].startswith("history status=done fetched=")


# ------------------------------------------------------- history repair (PR 18)

REPAIR_VERSION = next(i + 1 for i, script in enumerate(MIGRATIONS) if "recovery_missing" in script)
HRV_COLUMNS = ("hrv_last_night_ms", "hrv_weekly_avg_ms", "hrv_status")


def set_repair_state(store, day: date, *, repairs: int = 0, failures: int = 0, repaired_on: str | None = None) -> None:
    conn = sqlite3.connect(store.path)
    try:
        with conn:
            conn.execute(
                "UPDATE history_days SET recovery_repairs = ?, recovery_failures = ?, recovery_repaired_on = ? "
                "WHERE date = ?",
                (repairs, failures, repaired_on, day.isoformat()),
            )
    finally:
        conn.close()


def seed_window(store, first: date, last: date, *, skip: tuple[date, ...] = ()) -> None:
    """Every day of first..last (but ``skip``) complete, both parts fetched on TODAY: nothing is pending."""
    for day in days(first, last):
        if day not in skip:
            seed_day(store, day, fetched_on=TODAY, missing="", **HISTORY_ROW)
            seed_activities(store, day, {}, fetched_on=TODAY)


def seed_candidate(store, day: date, missing: str = "hrv", *, fetched_on: date | None = None) -> None:
    """A day past its re-fetch window whose ``missing`` sections hold no value; activities fetched on TODAY."""
    values = {c: None for key in missing.split(",") for c in SECTION_COLUMNS[key]}
    seed_day(store, day, fetched_on=fetched_on or day + timedelta(days=3), missing=missing, **{**HISTORY_ROW, **values})
    seed_activities(store, day, {}, fetched_on=TODAY)


def asked(source: FakeHistorySource, day: date) -> int:
    return source.snapshot_days.count(day)


def row_for(**overrides: Any) -> dict[str, Any]:
    """A stored row of the day TODAY-10 that is due for a repair of HRV (one field changes per test case)."""
    row = {
        "recovery_fetched_on": (TODAY - timedelta(days=7)).isoformat(),
        "recovery_missing": "hrv",
        "recovery_failures": 0,
        "recovery_repairs": 0,
        "recovery_repaired_on": None,
    }
    row.update(overrides)
    return row


def test_section_errors_lists_only_error_sections():
    payload = history_snapshot("2026-10-06", hrv=ERROR_SECTION, sleep=ERROR_SECTION, stress={"available": False})
    del payload["readiness"]
    assert section_errors(payload) == {"hrv", "sleep"}
    assert section_errors(history_snapshot("2026-10-06")) == frozenset()
    assert section_errors({"hrv": "error", "sleep": {"error": None}}) == {"sleep"}  # only a dict with the key counts


def test_missing_text_uses_the_canonical_order():
    assert missing_text({"training_status", "hrv", "readiness"}) == "readiness,hrv,training_status"
    assert missing_text(frozenset()) == ""
    assert missing_text(ALL_SECTIONS) == ",".join(history.SNAPSHOT_SECTIONS)
    assert len(missing_text(ALL_SECTIONS)) == 66  # the longest value; the column allows 200


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({}, ALL_SECTIONS),
        ({"recovery_fetched_on": None, "recovery_missing": "hrv"}, ALL_SECTIONS),
        ({"recovery_fetched_on": "2026-10-05", "recovery_missing": "hrv,sleep"}, {"hrv", "sleep"}),
        ({"recovery_fetched_on": "2026-10-05", "recovery_missing": ""}, frozenset()),
        ({"recovery_fetched_on": "2026-10-05", "recovery_missing": None}, frozenset()),
        ({"recovery_fetched_on": "2026-10-05", "recovery_missing": "hrv,bogus"}, {"hrv"}),
        ({"recovery_fetched_on": "2026-10-05", "recovery_missing": 7}, frozenset()),
    ],
)
def test_missing_sections_of_a_row(row, expected):
    assert missing_sections(row) == expected


@pytest.mark.parametrize(
    ("overrides", "due"),
    [
        ({"recovery_fetched_on": (TODAY - timedelta(days=8)).isoformat()}, False),  # day + 2: still re-fetched
        ({}, True),  # day + 3
        ({"recovery_fetched_on": (TODAY - timedelta(days=9)).isoformat()}, False),  # day + 1
        ({"recovery_fetched_on": TODAY.isoformat()}, False),  # fetched tonight
        ({"recovery_fetched_on": (TODAY - timedelta(days=1)).isoformat()}, True),
        ({"recovery_fetched_on": None}, False),
        ({"recovery_failures": 2}, True),
        ({"recovery_failures": 3}, False),
        ({"recovery_repairs": 2}, True),
        ({"recovery_repairs": 3}, False),
        ({"recovery_repaired_on": TODAY.isoformat()}, False),
        ({"recovery_repaired_on": (TODAY - timedelta(days=1)).isoformat()}, True),
        ({"recovery_missing": ""}, False),
        ({"recovery_missing": "hrv,sleep"}, True),
    ],
)
def test_repair_sections_rule(overrides, due):
    # day = TODAY - 10; the default fetched_on is day + 3.
    row = row_for(**overrides)
    got = repair_sections(row, TODAY - timedelta(days=10), TODAY)
    assert got == (missing_sections(row) if due else frozenset())
    assert bool(got) == due


def test_repair_and_refetch_rules_are_disjoint():
    day = TODAY - timedelta(days=10)
    for offset in range(1, 10):
        for failures in range(0, 4):
            fetched_on = day + timedelta(days=offset)
            row = row_for(recovery_fetched_on=fetched_on.isoformat(), recovery_failures=failures)
            regular = needs_fetch(day, fetched_on, failures, TODAY)
            repair = bool(repair_sections(row, day, TODAY))
            assert not (regular and repair), (offset, failures)
    # The rules do meet: every fetched date is covered by one of them unless a limit stopped it.
    assert needs_fetch(day, day + timedelta(days=2), 0, TODAY) and not repair_sections(
        row_for(recovery_fetched_on=(day + timedelta(days=2)).isoformat()), day, TODAY
    )
    assert repair_sections(row_for(recovery_fetched_on=(day + timedelta(days=3)).isoformat()), day, TODAY)


@pytest.mark.anyio
async def test_first_fetch_records_every_error_section(store):
    source = FakeHistorySource(
        recovery=lambda day, index: ("ok", history_snapshot(day.isoformat(), sleep=ERROR_SECTION, hrv=ERROR_SECTION))
    )
    job, clock = make_job(store, source, START, history_backfill_days=1)
    await run_first(job, clock)
    row = rows_by_date(store)["2026-10-06"]
    assert row["recovery_missing"] == "hrv,sleep"
    assert all(row[c] is None for c in (*HRV_COLUMNS, "sleep_duration_h", "sleep_score"))
    expected = {k: v for k, v in HISTORY_ROW.items() if k not in (*HRV_COLUMNS, "sleep_duration_h", "sleep_score")}
    assert {k: row[k] for k in expected} == expected
    # The invariant: a section that is listed as missing has no value in any of its columns.
    for key in row["recovery_missing"].split(","):
        assert all(row[c] is None for c in SECTION_COLUMNS[key])


@pytest.mark.anyio
async def test_refetch_narrows_missing_sections(store):
    state = {"night": 1}

    def recovery(day: date, index: int):
        if state["night"] == 1:
            return "ok", history_snapshot(day.isoformat(), hrv=ERROR_SECTION)
        return "ok", history_snapshot(day.isoformat(), sleep=ERROR_SECTION)  # night 2: sleep fails, HRV loads

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, START, history_backfill_days=2)
    delay = await run_first(job, clock)
    row = rows_by_date(store)["2026-10-06"]
    assert row["recovery_missing"] == "hrv" and row["hrv_last_night_ms"] is None and row["sleep_score"] == 81
    state["night"] = 2
    await next_run(job, clock, delay)
    row = rows_by_date(store)["2026-10-06"]
    assert row["recovery_missing"] == ""  # sleep was read on night 1: its error now keeps the stored value
    assert (row["hrv_last_night_ms"], row["hrv_weekly_avg_ms"], row["hrv_status"]) == (55, 57, "BALANCED")
    assert row["sleep_score"] == 81


@pytest.mark.anyio
async def test_complete_fetch_stores_empty_missing(store):
    job, clock = make_job(store, FakeHistorySource(), START, history_backfill_days=2)
    await run_first(job, clock)
    assert {r["recovery_missing"] for r in rows_by_date(store).values()} == {""}


@pytest.mark.anyio
async def test_section_error_on_backfill_is_not_repaired_the_same_date(store):
    bad = date(2026, 10, 1)
    state = {"cut": True}

    def recovery(day: date, index: int):
        if state["cut"] and index >= 7:  # the 09-30 call, after the activities block and six snapshots
            return "rate_limited", None
        return "ok", history_snapshot(day.isoformat(), **({"hrv": ERROR_SECTION} if day == bad else {}))

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, START, history_backfill_days=7)
    delay = await run_first(job, clock)
    assert source.snapshot_days == [date(2026, 10, n) for n in (6, 5, 4, 3, 2, 1)] + [date(2026, 9, 30)]
    assert rows_by_date(store)["2026-10-01"]["recovery_missing"] == "hrv"
    assert delay == 3600.0
    # The retry an hour later, still on the same local date: 10-01 was fetched tonight, so it is not asked again.
    state["cut"] = False
    await next_run(job, clock, delay)
    assert asked(source, bad) == 1
    assert source.snapshot_days[7:] == [date(2026, 9, 30)]


async def nights(job, clock, delay: float, count: int) -> float:
    for _ in range(count):
        delay = await next_run(job, clock, delay)
    return delay


@pytest.mark.anyio
async def test_section_error_day_is_repaired_after_the_refetch_window(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    bad = date(2026, 10, 1)
    state = {"broken": True}

    def recovery(day: date, index: int):
        if day == bad and state["broken"]:
            return "ok", history_snapshot(day.isoformat(), hrv=ERROR_SECTION)
        if day == bad:
            return "ok", history_snapshot(day.isoformat(), sleep={"duration_h": 6.0, "score": 60})
        return "ok", history_snapshot(day.isoformat())

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, START, history_backfill_days=14)
    delay = await run_first(job, clock)
    row = rows_by_date(store)["2026-10-01"]
    assert row["recovery_missing"] == "hrv" and row["recovery_fetched_on"] == "2026-10-07"
    assert row["recovery_repairs"] == 0 and asked(source, bad) == 1

    delay = await next_run(job, clock, delay)  # 10-08: the day is past its window, HRV still fails
    assert asked(source, bad) == 2
    row = rows_by_date(store)["2026-10-01"]
    assert (row["recovery_repairs"], row["recovery_repaired_on"], row["recovery_missing"]) == (1, "2026-10-08", "hrv")
    assert row["recovery_failures"] == 0

    state["broken"] = False
    delay = await next_run(job, clock, delay)  # 10-09: HRV loads
    assert asked(source, bad) == 3
    row = rows_by_date(store)["2026-10-01"]
    assert (row["hrv_last_night_ms"], row["hrv_weekly_avg_ms"], row["hrv_status"]) == (55, 57, "BALANCED")
    assert row["sleep_score"] == 81 and row["sleep_duration_h"] == 7.4  # a stored section is not overwritten
    assert row["recovery_missing"] == "" and row["recovery_fetched_on"] == "2026-10-07"
    assert row["recovery_repairs"] == 1  # an answer that repaired the day is not charged
    assert " repaired=1 " in history_lines(caplog)[-1].getMessage()

    await next_run(job, clock, delay)  # 10-10: complete, not asked
    assert asked(source, bad) == 3


@pytest.mark.anyio
async def test_repair_stops_at_the_limit(store):
    bad = date(2026, 10, 1)
    source = FakeHistorySource(
        recovery=lambda day, index: (
            "ok",
            history_snapshot(day.isoformat(), **({"hrv": ERROR_SECTION} if day == bad else {})),
        )
    )
    job, clock = make_job(store, source, START, history_backfill_days=14)
    delay = await run_first(job, clock)
    assert asked(source, bad) == 1
    for night, attempts in ((8, 1), (9, 2), (10, 3)):
        delay = await next_run(job, clock, delay)
        assert asked(source, bad) == 1 + attempts, night
        row = rows_by_date(store)["2026-10-01"]
        assert row["recovery_repairs"] == attempts and row["recovery_repaired_on"] == f"2026-10-{night:02d}"
    await nights(job, clock, delay, 2)  # 10-11 and 10-12: the limit holds
    assert asked(source, bad) == 4
    row = rows_by_date(store)["2026-10-01"]
    assert row["recovery_missing"] == "hrv" and row["recovery_repairs"] == 3 and row["hrv_last_night_ms"] is None


@pytest.mark.anyio
async def test_complete_day_is_not_repaired(store):
    old_days = days(date(2026, 9, 30), date(2026, 10, 3))  # past their window, fetched on day + 3
    seed_window(store, date(2026, 9, 30), date(2026, 10, 5), skip=tuple(old_days))
    for day in old_days:
        store.upsert_history_recovery(day, HISTORY_ROW, day + timedelta(days=3), missing=None if day.day == 30 else "")
        seed_activities(store, day, {}, fetched_on=TODAY)
    rows = rows_by_date(store)
    assert rows["2026-09-30"]["recovery_missing"] is None  # an old-schema seed counts as complete
    assert [rows[d.isoformat()]["recovery_missing"] for d in old_days[1:]] == ["", "", ""]
    source = FakeHistorySource()
    job, clock = make_job(store, source, START, history_backfill_days=7)
    await run_first(job, clock)
    assert source.snapshot_days == [date(2026, 10, 6)]  # yesterday only


@pytest.mark.anyio
async def test_repair_writes_only_missing_sections(store):
    day = date(2026, 10, 1)
    seed_candidate(store, day, "hrv")
    seed_window(store, date(2026, 9, 30), date(2026, 10, 5), skip=(day,))
    before = rows_by_date(store)["2026-10-01"]
    assert all(before[c] is None for c in HRV_COLUMNS) and before["stress_avg"] == 28

    def recovery(d: date, index: int):
        return "ok", history_snapshot(
            d.isoformat(),
            readiness={"score": 11},
            hrv={"last_night_avg_ms": 66, "weekly_avg_ms": 67, "status": "LOW"},
            sleep={"duration_h": 3.0, "score": 30},
            body_battery={"high": 1, "low": 0},
            resting_hr={"today": 99},
            stress=ERROR_SECTION,
            training_status={"acute": 1, "chronic": 1, "acwr_ratio": 1.0, "vo2max_running": 1, "vo2max_cycling": 1},
        )

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, START, history_backfill_days=7)
    await run_first(job, clock)
    assert asked(source, day) == 1
    after = rows_by_date(store)["2026-10-01"]
    changed = {k for k in before if before[k] != after[k]}
    assert changed - {"updated_at"} == {*HRV_COLUMNS, "recovery_missing", "recovery_repaired_on"}
    assert (after["hrv_last_night_ms"], after["hrv_weekly_avg_ms"], after["hrv_status"]) == (66, 67, "LOW")
    assert after["stress_avg"] == 28 and after["recovery_missing"] == ""
    assert after["recovery_fetched_on"] == before["recovery_fetched_on"]  # not a regular fetch


def candidates_setup(store, count: int, *, first: date = date(2026, 9, 23)) -> list[date]:
    """``count`` repair candidates (TODAY-4, TODAY-5, ...) and the rest of the 14-day window complete."""
    cands = [TODAY - timedelta(days=4 + n) for n in range(count)]
    for day in cands:
        seed_candidate(store, day)
    seed_window(store, first, date(2026, 10, 5), skip=tuple(cands))
    return cands


@pytest.mark.anyio
async def test_repairs_per_night_are_capped_newest_first(store, tmp_path):
    cands = candidates_setup(store, 7)
    source = FakeHistorySource()
    job, clock = make_job(store, source, START, history_backfill_days=14)
    delay = await run_first(job, clock)
    assert [d for d in source.snapshot_days if d in cands] == cands[:5]  # five, newest first
    assert all(asked(source, d) == 0 for d in cands[5:])
    await next_run(job, clock, delay)
    assert [d for d in source.snapshot_days if d in cands] == cands[:5] + cands[5:]

    # An earlier run tonight already made one repair call: four are left.
    other = Store(tmp_path / "second.db")
    cands = candidates_setup(other, 7)
    seed_day(other, TODAY - timedelta(days=12), fetched_on=TODAY, missing="", **HISTORY_ROW)
    set_repair_state(other, TODAY - timedelta(days=12), repaired_on=TODAY.isoformat())
    source = FakeHistorySource()
    job, clock = make_job(other, source, START, history_backfill_days=14)
    await run_first(job, clock)
    assert [d for d in source.snapshot_days if d in cands] == cands[:4]


@pytest.mark.anyio
async def test_outage_with_repair_days_charges_nothing(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    cands = candidates_setup(store, 3)
    outage = {"on": True}
    source = FakeHistorySource(
        recovery=lambda day, index: ("ok", outage_snapshot(day) if outage["on"] else history_snapshot(day.isoformat()))
    )
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=14)
    delay = await run_first(job, clock)
    delay = await nights(job, clock, delay, 2)  # three runs inside the outage, all on 10-07
    messages = [r.getMessage() for r in history_lines(caplog)]
    assert len(messages) == 3
    assert all(m.startswith("history status=service_error ") and " given_up=0 " in m for m in messages)
    assert all(" repaired=0 " in m for m in messages)
    rows = rows_by_date(store)
    for day in cands:
        row = rows[day.isoformat()]
        assert (row["recovery_failures"], row["recovery_repairs"], row["recovery_missing"]) == (0, 0, "hrv")
        assert row["recovery_repaired_on"] is None
    assert rows["2026-10-06"]["recovery_failures"] == 0

    outage["on"] = False  # Garmin is back
    await next_run(job, clock, delay)
    rows = rows_by_date(store)
    for day in cands:
        row = rows[day.isoformat()]
        assert row["recovery_missing"] == "" and row["hrv_last_night_ms"] == 55 and row["recovery_failures"] == 0
    assert " repaired=3 " in history_lines(caplog)[-1].getMessage()


@pytest.mark.anyio
async def test_section_down_for_every_day_is_not_asked_and_not_charged(store):
    down = {"hrv": True}
    c1, c2, c3 = date(2026, 10, 2), date(2026, 10, 1), date(2026, 9, 30)
    seed_candidate(store, c1)
    seed_candidate(store, c2)
    seed_candidate(store, c3, "hrv,sleep")
    seed_window(store, date(2026, 9, 23), date(2026, 10, 5), skip=(c1, c2, c3))

    def recovery(day: date, index: int):
        return "ok", history_snapshot(day.isoformat(), **({"hrv": ERROR_SECTION} if down["hrv"] else {}))

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, START, history_backfill_days=14)
    delay = await run_first(job, clock)
    assert source.snapshot_days == [date(2026, 10, 6), c3]  # the regular day, then the only candidate with sleep
    mid = rows_by_date(store)
    assert mid[c3.isoformat()]["recovery_missing"] == "hrv"  # sleep was repaired, HRV is still down
    assert mid[c3.isoformat()]["sleep_score"] == 81 and mid[c3.isoformat()]["recovery_repairs"] == 0  # not charged
    assert mid[c3.isoformat()]["recovery_repaired_on"] == "2026-10-07"

    seen = len(source.calls)
    delay = await next_run(job, clock, delay)  # 10-08: HRV is down for the regular days too
    night_two = [a["date"] for n, a in source.calls[seen:] if n == "get_recovery_snapshot"]
    assert night_two == ["2026-10-07", "2026-10-06", "2026-10-05"]
    seen = len(source.calls)
    delay = await next_run(job, clock, delay)  # 10-09
    night_three = [a["date"] for n, a in source.calls[seen:] if n == "get_recovery_snapshot"]
    assert night_three == ["2026-10-08", "2026-10-07", "2026-10-06"]
    rows = rows_by_date(store)
    assert {rows[d.isoformat()]["recovery_repairs"] for d in (c1, c2, c3)} == {0}
    assert {rows[d.isoformat()]["recovery_failures"] for d in (c1, c2, c3)} == {0}

    down["hrv"] = False
    await next_run(job, clock, delay)  # 10-10: the days that reached D + 4 during the outage join the candidates
    rows = rows_by_date(store)
    assert {rows[d]["recovery_missing"] for d in ("2026-10-02", "2026-10-01", "2026-09-30", "2026-10-06")} == {""}
    assert all(rows[d.isoformat()]["hrv_last_night_ms"] == 55 for d in (c1, c2, c3))
    assert rows["2026-10-06"]["hrv_last_night_ms"] == 55  # was down in all three regular fetches


@pytest.mark.anyio
async def test_repair_without_regular_evidence_is_not_charged(store):
    day = date(2026, 10, 1)
    seed_candidate(store, day)
    seed_window(store, date(2026, 9, 23), date(2026, 10, 6), skip=(day,))
    source = FakeHistorySource(
        recovery=lambda d, index: ("ok", history_snapshot(d.isoformat(), hrv=ERROR_SECTION))
    )
    job, clock = make_job(store, source, START, history_backfill_days=14)
    assert await job.run_once() == "done"
    assert source.snapshot_days == [day]
    row = rows_by_date(store)["2026-10-01"]
    assert (row["recovery_repairs"], row["recovery_repaired_on"], row["recovery_missing"]) == (0, "2026-10-07", "hrv")
    assert await job.run_once() == "done"  # the same date: the day was asked tonight
    assert source.snapshot_days == [day]


@pytest.mark.anyio
async def test_repair_call_error_is_held_and_charged_after_confirmation(store, tmp_path):
    day = date(2026, 10, 1)
    seed_candidate(store, day)
    seed_window(store, date(2026, 9, 23), date(2026, 10, 5), skip=(day,))
    source = FakeHistorySource(
        recovery=lambda d, index: ("error", None) if d == day else ("ok", history_snapshot(d.isoformat()))
    )
    job, clock = make_job(store, source, START, history_backfill_days=14)
    delay = await run_first(job, clock)
    assert source.snapshot_days == [date(2026, 10, 6), day, date(2026, 10, 6)]  # yesterday, the repair, the probe
    row = rows_by_date(store)["2026-10-01"]
    assert row["recovery_failures"] == 1 and row["recovery_repairs"] == 0 and row["recovery_missing"] == "hrv"
    assert row["recovery_repaired_on"] is None  # an error is no answered repair
    assert delay == 3600.0  # partial: retried in an hour

    # A day with three charged errors is not asked again, the older candidate is.
    other = Store(tmp_path / "second.db")
    broken, older = date(2026, 10, 3), date(2026, 10, 1)
    seed_candidate(other, broken)
    seed_candidate(other, older)
    set_repair_state(other, broken, failures=3)
    seed_window(other, date(2026, 9, 23), date(2026, 10, 6), skip=(broken, older))
    source = FakeHistorySource()
    job, clock = make_job(other, source, START, history_backfill_days=14)
    await run_first(job, clock)
    assert source.snapshot_days == [older]


@pytest.mark.anyio
@pytest.mark.parametrize(("auth", "status"), [("missing", "auth"), ("error", "auth"), (None, "unreachable")])
async def test_repair_only_run_checks_auth_and_the_window(store, auth, status):
    day = date(2026, 10, 1)
    seed_candidate(store, day)
    seed_window(store, date(2026, 9, 23), date(2026, 10, 6), skip=(day,))
    source = FakeHistorySource(auth=auth)
    job, clock = make_job(store, source, START, history_backfill_days=14)
    assert await job.run_once() == status
    assert source.calls == [] and source.auth_checks == 1

    source = FakeHistorySource()
    job, clock = make_job(store, source, local(TODAY, 6, 31), history_backfill_days=14)
    assert await job.run_once() == "paused"
    assert source.calls == [] and source.auth_checks == 0

    source = FakeHistorySource()
    job, clock = make_job(store, source, START, history_backfill_days=14)
    assert await job.run_once() == "done"
    assert source.snapshot_days == [day] and source.auth_checks == 1


@pytest.mark.anyio
async def test_legacy_row_is_asked_once(db_path, monkeypatch):
    day = date(2026, 10, 1)
    with monkeypatch.context() as m:
        m.setattr(store_module, "MIGRATIONS", MIGRATIONS[: REPAIR_VERSION - 1])
        old = Store(db_path)
        values = {k: (None if k in HRV_COLUMNS else v) for k, v in HISTORY_ROW.items()}
        old.upsert_history_recovery(day, values, day + timedelta(days=3))
        old.upsert_history_activities({day: "{}"}, TODAY)
    store = Store(db_path)  # the migration classifies the row
    seed_window(store, date(2026, 9, 23), date(2026, 10, 5), skip=(day,))
    before = rows_by_date(store)["2026-10-01"]
    assert before["recovery_missing"] == "hrv"
    source = FakeHistorySource(
        recovery=lambda d, index: ("ok", history_snapshot(d.isoformat(), hrv={"available": False}))
    )
    job, clock = make_job(store, source, START, history_backfill_days=14)
    delay = await run_first(job, clock)
    assert asked(source, day) == 1
    after = rows_by_date(store)["2026-10-01"]
    assert after["recovery_missing"] == "" and after["recovery_repairs"] == 0
    assert {k: after[k] for k in before if k not in ("recovery_missing", "recovery_repaired_on", "updated_at")} == {
        k: before[k] for k in before if k not in ("recovery_missing", "recovery_repaired_on", "updated_at")
    }
    await next_run(job, clock, delay)
    assert asked(source, day) == 1  # complete: not asked on the next night


@pytest.mark.anyio
@pytest.mark.parametrize("answer", [{"count": 0}, {"activities": None, "count": 0}, {"activities": {}, "count": 0}])
async def test_activities_probe_answer_without_a_list_is_an_outage(store, answer):
    good = date(2026, 9, 29)
    seed_known_good(store, good)
    probe = (good, good)

    def activities(start: date, end: date, index: int):
        return ("ok", answer) if (start, end) == probe else ("error", None)

    source = FakeHistorySource(activities=activities)
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=8)
    assert await job.run_once() == "service_error"
    assert source.activity_ranges[-1] == probe  # held errors, then the probe
    assert {r["activities_failures"] for r in rows_by_date(store).values()} == {0}


@pytest.mark.anyio
async def test_run_log_counts_a_day_given_up_only_for_activities(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    a, b = date(2026, 10, 4), date(2026, 10, 5)
    seed_day(store, a, fetched_on=TODAY, readiness=50)
    store.record_history_failure([a] * 3, "activities")  # only the activities part is given up
    store.record_history_failure([b] * 3, "recovery")
    store.record_history_failure([b] * 3, "activities")  # both parts given up: the day counts once
    seed_day(store, date(2026, 10, 6), fetched_on=TODAY, readiness=50)
    seed_activities(store, date(2026, 10, 6), {}, fetched_on=TODAY)
    source = FakeHistorySource()
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    await run_first(job, clock)
    assert source.calls == []
    (record,) = history_lines(caplog)
    assert record.getMessage().startswith("history status=done fetched=0 failed=0 pending=0 given_up=2 ms=")


@pytest.mark.anyio
async def test_repair_is_charged_only_when_every_missing_section_works_elsewhere(store):
    day = date(2026, 10, 2)
    seed_candidate(store, day, "hrv,sleep")
    seed_window(store, date(2026, 9, 23), date(2026, 10, 5), skip=(day,))

    def recovery(d: date, index: int):
        # HRV is down for every day of the run; sleep loads for the regular day but not for the candidate.
        sections = {"hrv": ERROR_SECTION, **({"sleep": ERROR_SECTION} if d == day else {})}
        return "ok", history_snapshot(d.isoformat(), **sections)

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, START, history_backfill_days=14)
    await job.run_once()
    assert source.snapshot_days == [date(2026, 10, 6), day]  # sleep works elsewhere, so the candidate is asked
    row = rows_by_date(store)["2026-10-02"]
    # HRV never loaded in this run: the answer proves nothing about this day, so it is not charged.
    assert row["recovery_repairs"] == 0 and row["recovery_missing"] == "hrv,sleep"
    assert row["recovery_repaired_on"] == "2026-10-07"


@pytest.mark.anyio
async def test_a_repair_answer_is_not_evidence_for_other_days(store):
    newer, older = date(2026, 10, 3), date(2026, 10, 2)
    seed_candidate(store, newer, "hrv,sleep")
    seed_candidate(store, older, "hrv")
    seed_window(store, date(2026, 9, 23), date(2026, 10, 5), skip=(newer, older))

    def recovery(d: date, index: int):
        # HRV fails for the regular day, but Garmin serves it when the newer candidate is repaired.
        return "ok", history_snapshot(d.isoformat(), **({} if d == newer else {"hrv": ERROR_SECTION}))

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, START, history_backfill_days=14)
    await job.run_once()
    rows = rows_by_date(store)
    assert rows["2026-10-03"]["recovery_missing"] == "" and rows["2026-10-03"]["hrv_last_night_ms"] == 55
    # The newer candidate's HRV loaded, but a repair answer does not show that HRV works in this run.
    assert source.snapshot_days == [date(2026, 10, 6), newer]
    assert rows["2026-10-02"]["recovery_missing"] == "hrv" and rows["2026-10-02"]["recovery_repaired_on"] is None


@pytest.mark.anyio
async def test_an_answered_repair_charges_the_held_errors_of_the_run(store):
    first, second = date(2026, 10, 3), date(2026, 10, 2)
    seed_candidate(store, first)
    seed_candidate(store, second)
    seed_window(store, date(2026, 9, 23), date(2026, 10, 5), skip=(first, second))
    yesterday = date(2026, 10, 6)

    def recovery(d: date, index: int):
        if d == yesterday:
            return "error", None  # held: the backend is not yet shown to work
        if d == second:
            return "rate_limited", None  # ends the run before the final probe
        return "ok", history_snapshot(d.isoformat())

    source = FakeHistorySource(recovery=recovery)
    job, clock = make_job(store, source, START, history_backfill_days=14)
    assert await job.run_once() == "rate_limited"
    assert source.snapshot_days == [yesterday, first, second]
    # The answered repair of `first` showed that the call works, so yesterday's error is charged to it.
    assert rows_by_date(store)["2026-10-06"]["recovery_failures"] == 1


# -------------------------------------------------- race predictions part (PR 14)


def prediction_payload(start: date, end: date, entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"start": start.isoformat(), "end": end.isoformat(), "count": len(entries), "days": entries}


def prediction_day(day: str, **seconds: Any) -> dict[str, Any]:
    return {"date": day, **seconds}


def two_days(start: date, end: date, index: int):
    entries = [
        prediction_day("2026-09-29", **{"5k_seconds": 1300, "10k_seconds": 2720}),
        prediction_day("2026-10-05", **{"5k_seconds": 1290, "10k_seconds": 2700, "marathon_seconds": 12310}),
    ]
    return "ok", prediction_payload(start, end, [e for e in entries if start <= date.fromisoformat(e["date"]) <= end])


def prediction_lines(caplog) -> list[logging.LogRecord]:
    return [r for r in history_lines(caplog) if "part=predictions" in r.getMessage()]


def stored_predictions(store) -> dict[str, dict[str, Any]]:
    return {r["date"]: r for r in store.race_prediction_rows(None, date(2030, 1, 1))}


@pytest.mark.anyio
async def test_first_run_asks_for_the_whole_range_once_after_the_garmin_calls(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    source = FakeHistorySource(predictions=two_days)
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=10, predictions=source)
    await run_first(job, clock)
    assert source.prediction_ranges == [(date(2026, 9, 27), date(2026, 10, 6))]
    names = [n for n, _ in source.calls]
    assert names.count("get_race_predictions") == 1 and names[-1] == "get_race_predictions"
    assert len(names) == 10 + 2 + 1
    assert source.calls[-1][1] == {"start_date": "2026-09-27", "end_date": "2026-10-06"}
    rows = stored_predictions(store)
    assert sorted(rows) == ["2026-09-29", "2026-10-05"]
    assert (rows["2026-10-05"]["time_5k_s"], rows["2026-10-05"]["time_marathon_s"]) == (1290, 12310)
    assert rows["2026-09-29"]["time_half_marathon_s"] is None
    assert store.prediction_sync_state() == {"covered_from": "2026-09-27", "synced_on": "2026-10-07"}
    (line,) = prediction_lines(caplog)
    assert line.levelno == logging.INFO and line.getMessage().startswith("history status=done part=predictions days=2 ms=")
    plain = [m for m in garmin_lines(caplog) if "part=" not in m]  # the Garmin line is separate and unchanged
    assert len(plain) == 1 and plain[0].startswith("history status=done fetched=")


@pytest.mark.anyio
async def test_second_run_the_same_day_makes_no_predictions_call(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    source = FakeHistorySource(predictions=two_days)
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=10, predictions=source)
    await run_first(job, clock)
    assert await job.run_once() == "done"
    assert len(source.prediction_ranges) == 1 and len(prediction_lines(caplog)) == 1  # synced_on == today: nothing due


@pytest.mark.anyio
async def test_next_night_refetches_the_last_three_days_only(store):
    source = FakeHistorySource(predictions=two_days)
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=10, predictions=source)
    delay = await run_first(job, clock)
    await next_run(job, clock, delay)  # 2026-10-08 03:30
    assert source.prediction_ranges[1] == (date(2026, 10, 4), date(2026, 10, 7))
    assert store.prediction_sync_state() == {"covered_from": "2026-09-27", "synced_on": "2026-10-08"}
    assert sorted(stored_predictions(store)) == ["2026-09-29", "2026-10-05"]  # nothing was removed


@pytest.mark.anyio
async def test_a_longer_backfill_asks_for_the_older_range(store):
    source = FakeHistorySource(predictions=two_days)
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=10, predictions=source)
    await run_first(job, clock)
    longer, clock2 = make_job(
        store, source, local(date(2026, 10, 8), 3, 29), history_backfill_days=20, predictions=source
    )
    await run_first(longer, clock2)
    assert source.prediction_ranges[1] == (date(2026, 9, 18), date(2026, 10, 7))
    assert store.prediction_sync_state() == {"covered_from": "2026-09-18", "synced_on": "2026-10-08"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "answer",
    [("error", None), ("ok", {"start": "2026-09-27", "end": "2026-10-06", "error": "unexpected Garmin response"})],
    ids=["call-error", "error-section"],
)
async def test_a_failed_predictions_call_changes_no_state_and_retries_the_whole_range(store, caplog, answer):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    failing = {"on": True}

    def predictions(start, end, index):
        return answer if failing["on"] else two_days(start, end, index)

    source = FakeHistorySource(predictions=predictions)
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=10, predictions=source)
    delay = await run_first(job, clock)
    assert stored_predictions(store) == {} and store.prediction_sync_state() is None
    (line,) = prediction_lines(caplog)
    assert line.levelno == logging.WARNING and line.getMessage().startswith("history status=error part=predictions days=0 ms=")
    assert clock.now() + timedelta(seconds=delay) == local(date(2026, 10, 8), 3, 30)  # as for done
    failing["on"] = False
    await next_run(job, clock, delay)
    assert source.prediction_ranges[1] == (date(2026, 9, 28), date(2026, 10, 7))  # the full range again
    assert store.prediction_sync_state() == {"covered_from": "2026-09-28", "synced_on": "2026-10-08"}


@pytest.mark.anyio
async def test_predictions_rate_limit_backs_off_an_hour_and_retries(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    answers = iter([("rate_limited", None)])
    source = FakeHistorySource(predictions=lambda s, e, i: next(answers, None) or two_days(s, e, i))
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=3, predictions=source)
    delay = await run_first(job, clock)
    assert delay == 3600.0
    (line,) = prediction_lines(caplog)
    assert line.levelno == logging.WARNING and line.getMessage().startswith("history status=rate_limited part=predictions")
    assert store.prediction_sync_state() is None
    await next_run(job, clock, delay)
    assert len(source.prediction_ranges) == 2 and store.prediction_sync_state() is not None
    assert len(source.snapshot_days) == 3  # the Garmin part was done and not repeated


@pytest.mark.anyio
async def test_predictions_auth_error_is_scheduled_like_done(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    source = FakeHistorySource(predictions=lambda s, e, i: ("auth_error", None))
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=3, predictions=source)
    delay = await run_first(job, clock)
    assert clock.now() + timedelta(seconds=delay) == local(date(2026, 10, 8), 3, 30)
    (line,) = prediction_lines(caplog)
    assert line.getMessage().startswith("history status=auth part=predictions days=0")
    assert line.levelno == logging.WARNING


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["unreachable", "no_credentials"])
async def test_predictions_unreachable_or_no_credentials_change_nothing(store, caplog, outcome):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    source = FakeHistorySource(predictions=lambda s, e, i: (outcome, None))
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=3, predictions=source)
    delay = await run_first(job, clock)
    assert clock.now() + timedelta(seconds=delay) == local(date(2026, 10, 8), 3, 30)
    assert store.prediction_sync_state() is None
    (line,) = prediction_lines(caplog)
    assert line.getMessage().startswith(f"history status={outcome} part=predictions days=0")


@pytest.mark.anyio
async def test_predictions_run_in_the_briefing_window_pause_until_the_cutoff(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    yesterday = date(2026, 10, 6)
    seed_day(store, yesterday, fetched_on=TODAY, readiness=50)
    seed_activities(store, yesterday, {}, fetched_on=TODAY)  # the Garmin part has nothing to do
    source = FakeHistorySource(predictions=two_days)
    job, clock = make_job(store, source, local(TODAY, 6, 59), history_backfill_days=1, predictions=source)
    delay = await run_first(job, clock)  # 07:00:00
    assert source.calls == [] and source.auth_checks == 0
    (line,) = prediction_lines(caplog)
    assert line.levelno == logging.INFO and line.getMessage().startswith("history status=paused part=predictions days=0")
    assert clock.now() + timedelta(seconds=delay) == local(TODAY, 9, 30)
    await next_run(job, clock, delay)
    assert source.times == [local(TODAY, 9, 30)] and source.prediction_ranges == [(yesterday, yesterday)]
    assert store.prediction_sync_state() == {"covered_from": "2026-10-06", "synced_on": "2026-10-07"}


@pytest.mark.anyio
async def test_no_predictions_call_after_a_garmin_part_that_stopped(store):
    source = FakeHistorySource(recovery=rate_limited_from(0), predictions=two_days)
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=3, predictions=source)
    await run_first(job, clock)
    assert source.prediction_ranges == []
    missing = FakeHistorySource(auth="missing", predictions=two_days)
    job, clock = make_job(Store(store.path.with_name("other.db")), missing, local(TODAY, 10, 0),
                          history_backfill_days=3, predictions=missing)
    await run_first(job, clock)
    assert missing.prediction_ranges == []


@pytest.mark.anyio
async def test_predictions_run_after_a_partial_garmin_part(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    bad = date(2026, 10, 5)
    source = FakeHistorySource(
        recovery=lambda day, index: ("error", None) if day == bad else ("ok", history_snapshot(day.isoformat())),
        predictions=two_days,
    )
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3, predictions=source)
    assert await run_first(job, clock) == 3600.0  # partial keeps its hourly retry
    assert len(source.prediction_ranges) == 1 and store.prediction_sync_state() is not None
    assert "history status=partial fetched=3 failed=1" in caplog.text


@pytest.mark.anyio
async def test_prediction_values_are_validated_and_replace_whole_days(store):
    entries = [
        prediction_day("2026-09-20", **{"5k_seconds": 1290}),  # outside the range
        prediction_day("2026-09-28", **{"5k_seconds": 0, "10k_seconds": -5, "half_marathon_seconds": 86401,
                                        "marathon_seconds": True}),
        prediction_day("2026-09-29", **{"5k_seconds": 1290, "10k_seconds": "1290", "half_marathon_seconds": 86400,
                                        "marathon_seconds": None}),
        prediction_day("2026-09-30", **{"5k_seconds": 86400, "10k_seconds": 1}),
        prediction_day("2026-10-01", **{"5k_seconds": float("nan"), "10k_seconds": 2700.5}),
        prediction_day("2026-13-01", **{"5k_seconds": 1}),
        {"5k_seconds": 1},
        "junk",
        prediction_day("2026-10-05", **{"5k_seconds": 1300, "10k_seconds": 2700}),
    ]
    later = {"on": False}

    def predictions(start, end, index):
        if later["on"]:
            return "ok", prediction_payload(start, end, [prediction_day("2026-10-05", **{"5k_seconds": 1280})])
        return "ok", prediction_payload(start, end, entries)

    source = FakeHistorySource(predictions=predictions)
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=10, predictions=source)
    delay = await run_first(job, clock)
    rows = stored_predictions(store)
    assert sorted(rows) == ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-05"]
    assert [rows["2026-09-29"][c] for c in ("time_5k_s", "time_10k_s", "time_half_marathon_s", "time_marathon_s")] == [
        1290, None, 86400, None]
    assert (rows["2026-09-30"]["time_5k_s"], rows["2026-09-30"]["time_10k_s"]) == (86400, 1)
    assert (rows["2026-10-01"]["time_5k_s"], rows["2026-10-01"]["time_10k_s"]) == (None, 2700.5)
    later["on"] = True
    await next_run(job, clock, delay)
    row = stored_predictions(store)["2026-10-05"]
    assert (row["time_5k_s"], row["time_10k_s"]) == (1280, None)  # a later answer replaces all four columns


@pytest.mark.anyio
async def test_a_store_error_in_the_predictions_part_still_logs_and_runs_the_strength_part(store, caplog, monkeypatch):
    from fakes import FakeStrengthSource

    caplog.set_level(logging.INFO, logger="coach_mcp.history")

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk")

    monkeypatch.setattr(store, "store_race_predictions", boom)
    source = FakeHistorySource(predictions=two_days)
    strength = FakeStrengthSource()
    await run_job(
        store, source, local(TODAY, 10, 0), settings_overrides={"history_backfill_days": 3},
        stop_when=lambda: "history status=crashed" in caplog.text, strength=strength, predictions=source,
    )
    assert "history status=crashed kind=OperationalError" in caplog.text
    assert any(line.startswith("history status=done fetched=") for line in garmin_lines(caplog))
    assert strength.calls  # the strength part still ran
    assert "disk" not in caplog.text


@pytest.mark.anyio
async def test_without_a_predictions_source_there_is_no_predictions_call(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    source = FakeHistorySource(predictions=two_days)
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=3)
    await run_first(job, clock)
    assert source.prediction_ranges == [] and prediction_lines(caplog) == []


@pytest.mark.anyio
async def test_prediction_logs_carry_no_dates_or_values(store, caplog):
    caplog.set_level(logging.DEBUG)
    source = FakeHistorySource(predictions=two_days)
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=10, predictions=source)
    await run_first(job, clock)
    (line,) = prediction_lines(caplog)
    assert not any(bad in line.getMessage() for bad in ("2026-", "1290", "12310", "2700"))


def test_prediction_range_table():
    first, last, today = date(2026, 9, 27), date(2026, 10, 6), date(2026, 10, 7)

    def state(covered: Any, synced: Any) -> dict[str, Any]:
        return {"covered_from": covered, "synced_on": synced}

    assert prediction_range(None, first, last, today) == (first, last)
    assert prediction_range(state("2026-09-28", "2026-10-06"), first, last, today) == (first, last)  # covers less
    assert prediction_range(state("2026-09-27", "2026-10-07"), first, last, today) is None  # synced today
    assert prediction_range(state("2026-09-27", "2026-10-08"), first, last, today) is None  # clock went back
    assert prediction_range(state("2026-09-27", "2026-10-06"), first, last, today) == (date(2026, 10, 3), last)
    assert prediction_range(state("2026-09-01", "2026-10-06"), first, last, today) == (date(2026, 10, 3), last)
    assert prediction_range(state("2026-09-27", "2026-09-30"), first, last, today) == (date(2026, 9, 27), last)
    assert prediction_range(state("2026-09-27", "2026-08-01"), first, last, today) == (first, last)  # 30+ days ago
    for bad in ("garbage", None, 5, ""):
        assert prediction_range(state(bad, "2026-10-06"), first, last, today) == (first, last)
        assert prediction_range(state("2026-09-27", bad), first, last, today) == (first, last)
    assert prediction_range(state("0001-01-01", "0001-01-02"), first, last, today) == (first, last)
    year_one = prediction_range(state("2026-09-27", "0001-01-02"), date(1, 1, 1), last, today)
    assert year_one == (date(1, 1, 1), last)  # synced - 3 days underflows the calendar: ask for everything


def test_prediction_values_helper():
    start, end = date(2026, 10, 1), date(2026, 10, 7)
    assert prediction_values({"error": "x"}, start, end) is None
    assert prediction_values({"days": "x"}, start, end) is None
    assert prediction_values({"days": []}, start, end) == {}
    got = prediction_values(
        {"days": [prediction_day("2026-10-01", **{"5k_seconds": 1}), prediction_day("2026-10-07", **{"5k_seconds": 2}),
                  prediction_day("2026-09-30", **{"5k_seconds": 3}), prediction_day("2026-10-08", **{"5k_seconds": 4})]},
        start, end,
    )
    assert sorted(got) == [date(2026, 10, 1), date(2026, 10, 7)]  # both ends of the range are inside
    assert got[date(2026, 10, 7)] == {
        "time_5k_s": 2, "time_10k_s": None, "time_half_marathon_s": None, "time_marathon_s": None}
