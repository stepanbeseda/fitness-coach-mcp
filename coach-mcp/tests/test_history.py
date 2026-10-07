from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from datetime import date, timedelta
from typing import Any

import pytest
from fakes import FakeHistorySource, StopLoop
from garmin_payloads import (
    HISTORY_ROW,
    HISTORY_SECTIONS,
    activities_payload,
    activity,
    history_snapshot,
)
from history_helpers import local, make_job, next_run, run_first, run_job

from coach_mcp import history
from coach_mcp.history import (
    activity_totals,
    needs_fetch,
    recovery_values,
    snapshot_failed,
    sport_of,
)
from coach_mcp.store import HISTORY_COLUMNS

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


def all_errors() -> dict[str, Any]:
    return history_snapshot("x", **dict.fromkeys(HISTORY_SECTIONS, ERROR_SECTION))


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
    ],
)
def test_needs_fetch_rule(day, fetched_on, failures, expected):
    assert needs_fetch(day, fetched_on, failures, T) is expected


def test_needs_fetch_ignores_failures_once_a_fetch_succeeded():
    # The counter is reset by a success; a stale value must not stop a re-fetch inside the window.
    assert needs_fetch(T - timedelta(days=2), T - timedelta(days=1), 5, T) is True


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
    assert len(lines) == 1 and lines[0].startswith("history status=done fetched=103 failed=0 pending=90 ms=")


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
    source = FakeHistorySource(activities=lambda start, end, index: ("error", None))
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    assert await run_first(job, clock) == 3600.0
    rows = rows_by_date(store)
    assert {r["activities_failures"] for r in rows.values()} == {1}
    assert {r["recovery_failures"] for r in rows.values()} == {0}
    assert {r["activities"] for r in rows.values()} == {None}
    assert {r["recovery_fetched_on"] for r in rows.values()} == {"2026-10-07"}
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
    source = FakeHistorySource(activities=lambda start, end, index: ("ok", answer))
    job, clock = make_job(store, source, AFTER_WINDOW, history_backfill_days=3)
    assert await run_first(job, clock) == 3600.0
    rows = rows_by_date(store)
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
