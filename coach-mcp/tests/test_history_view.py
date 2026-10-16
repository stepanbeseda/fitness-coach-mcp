"""The history view (PR 24): range defaults and limits, Garmin days from ``history_days`` (activities JSON edge
cases, the settled flag, values), Hevy coverage and strength days, the merged activity days and the predictions.

Everything that turns stored rows into view values is tested here, against a temporary Store; the readers' own
tests build view values (``view_values.FakeHistory``).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fakes import FakeHistorySource, FakeStrengthSource
from hevy_payloads import day_workout, events_page, hevy_template, updated
from history_helpers import exercise, local, make_job, run_first, seed_activities, seed_day, seed_strength, seed_sync, seed_template
from mcp.server.mcpserver.exceptions import ToolError
from view_values import sport

from conftest import TODAY
from coach_mcp import trends
from coach_mcp.history import needs_fetch
from coach_mcp.history_view import (
    NUMERIC_COLUMNS,
    Coverage,
    HistoryView,
    Templates,
    activities_settled,
    coverage,
    day_totals,
    history_range,
    plan_range,
)
from coach_mcp.store import Store
from coach_mcp.strength import exercises_json


def d(text: str) -> date:
    return date.fromisoformat(text)


def raw(store: Store, day: str, **columns: Any) -> None:
    """A row the nightly job never writes (raw SQL)."""
    names = ["date", *columns, "updated_at"]
    conn = sqlite3.connect(store.path)
    try:
        conn.execute(
            f"INSERT INTO history_days ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
            (day, *columns.values(), "2026-10-07T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------ ranges


def test_history_range_defaults_end_yesterday_and_90_days():
    assert history_range(TODAY, None, None) == (d("2026-07-09"), d("2026-10-06"))
    assert history_range(TODAY, None, "2026-09-30") == (d("2026-07-03"), d("2026-09-30"))
    assert history_range(TODAY, "2026-09-01", None) == (d("2026-09-01"), d("2026-10-06"))
    assert history_range(TODAY, " 2026-09-01 ", "2026-10-07") == (d("2026-09-01"), TODAY)  # today is allowed


def test_history_range_accepts_a_single_day():
    assert history_range(TODAY, "2026-10-01", "2026-10-01") == (d("2026-10-01"), d("2026-10-01"))
    assert history_range(TODAY, "2026-10-06", None, period="day") == (d("2026-10-06"), d("2026-10-06"))


@pytest.mark.parametrize(
    ("period", "start_ok", "start_long", "message"),
    [
        ("day", "2026-07-07", "2026-07-06", "date range too long for period day (max 92 days)"),
        ("week", "2024-10-06", "2024-10-05", "date range too long for period week (max 731 days)"),
        ("month", "2024-10-06", "2024-10-05", "date range too long for period month (max 731 days)"),
        (None, "2024-10-06", "2024-10-05", "date range too long (max 731 days)"),
    ],
)
def test_history_range_limits_accept_the_maximum_and_reject_one_more(period, start_ok, start_long, message):
    assert history_range(TODAY, start_ok, "2026-10-06", period=period) == (d(start_ok), d("2026-10-06"))
    with pytest.raises(ToolError) as caught:
        history_range(TODAY, start_long, "2026-10-06", period=period)
    assert str(caught.value) == message


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (None, "2026-10-08", "end_date must not be after today"),
        ("2026-10-06", "2026-10-05", "start_date must be on or before end_date"),
        ("2026-13-01", None, "start_date is not a valid date: '2026-13-01'"),
        ("yesterday", None, "start_date must be YYYY-MM-DD, got 'yesterday'"),
        (None, "2026-1-1", "end_date must be YYYY-MM-DD, got '2026-1-1'"),
        ("yesterday", "2026-10-08", "end_date must not be after today"),  # the end is checked first
        ("yesterday", "2026-1-1", "end_date must be YYYY-MM-DD, got '2026-1-1'"),
        ("2026-10-08", None, "start_date must be on or before end_date"),  # a default end of yesterday
    ],
)
def test_history_range_errors_in_order(start, end, message):
    with pytest.raises(ToolError) as caught:
        history_range(TODAY, start, end, period="day")
    assert str(caught.value) == message


def test_plan_range_defaults_to_the_iso_week_of_today():
    assert plan_range(TODAY, None, None) == (d("2026-10-05"), d("2026-10-11"))
    assert plan_range(d("2026-10-05"), None, None) == (d("2026-10-05"), d("2026-10-11"))  # a Monday
    assert plan_range(d("2026-10-11"), None, None) == (d("2026-10-05"), d("2026-10-11"))  # a Sunday
    assert plan_range(TODAY, "2026-09-30", None) == (d("2026-09-30"), d("2026-10-06"))  # start + 6
    assert plan_range(TODAY, None, "2026-10-20") == (d("2026-10-05"), d("2026-10-20"))
    assert plan_range(TODAY, "2026-12-01", "2026-12-01") == (d("2026-12-01"), d("2026-12-01"))  # the future is fine


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("2026-07-01", "2026-10-01", "date range too long (max 92 days)"),  # 93 days
        (None, "2026-10-04", "start_date must be on or before end_date"),  # before the default Monday
        ("2026-1-1", "x", "start_date must be YYYY-MM-DD, got '2026-1-1'"),  # the start is checked first
        ("2026-10-01", "2026-02-30", "end_date is not a valid date: '2026-02-30'"),
    ],
)
def test_plan_range_errors(start, end, message):
    with pytest.raises(ToolError) as caught:
        plan_range(TODAY, start, end)
    assert str(caught.value) == message
    assert plan_range(TODAY, "2026-07-02", "2026-10-01") == (d("2026-07-02"), d("2026-10-01"))  # 92 days


# ------------------------------------------------------------------ Garmin days


def test_recovery_values_are_finite_numbers_or_none(store):
    seed_day(store, d("2026-10-01"), readiness=70, hrv_last_night_ms=55.5, hrv_status="BALANCED", resting_hr=None)
    raw(store, "2026-10-02", recovery_fetched_on="2026-10-03", readiness=float("inf"), resting_hr=float("-inf"),
        sleep_score="high", stress_avg=True, hrv_status="")
    first, second = HistoryView(store).days(d("2026-10-01"), d("2026-10-02"))
    assert set(first.values) == set(NUMERIC_COLUMNS) and len(NUMERIC_COLUMNS) == 14
    assert first.values["readiness"] == 70.0 and isinstance(first.values["readiness"], float)  # a REAL column
    assert (first.values["hrv_last_night_ms"], first.values["resting_hr"], first.hrv_status) == (55.5, None, "BALANCED")
    assert first.recovery_fetched_on == d("2026-10-02") and first.activities_fetched_on is None
    assert {k: second.values[k] for k in ("readiness", "resting_hr", "sleep_score")} == dict.fromkeys(
        ("readiness", "resting_hr", "sleep_score"), None
    )
    assert second.values["stress_avg"] == 1  # SQLite stores True as the integer 1 in a REAL column
    assert second.hrv_status is None


ACTIVITY_CASES = [
    # (date, activities text, activities_fetched_on, expected activities)
    ("2026-09-21", '{"running":{"count":1}}', "2026-09-22", {"running": {"count": 1}}),
    ("2026-09-22", "{}", "2026-09-23", {}),  # fetched, nothing recorded: a real zero
    ("2026-09-23", '{"running": 7}', "2026-09-24", {"running": 7}),  # a sport entry is passed on as stored
    ("2026-09-24", "not json", "2026-09-25", None),
    ("2026-09-25", "{", "2026-09-26", None),
    ("2026-09-26", "[]", "2026-09-27", None),  # valid JSON of the wrong shape: a list, a text, a number, null
    ("2026-09-27", "[1, 2]", "2026-09-28", None),
    ("2026-09-28", '"x"', "2026-09-29", None),
    ("2026-09-29", "5", "2026-09-30", None),
    ("2026-09-30", "null", "2026-10-01", None),
    ("2026-10-01", '{"running":{"count":1}}', None, None),  # no fetch date: never fetched (PR 24 kept rule)
    ("2026-10-02", '{"running":{"count":1}}', "02.10.2026", None),  # an unreadable fetch date
    ("2026-10-03", b'{"running":{"count":1}}', "2026-10-04", None),  # a BLOB, not text
    ("2026-10-04", None, "2026-10-05", None),  # missing
]


def test_activities_json_edge_cases(store):
    for day, text, fetched_on, _ in ACTIVITY_CASES:
        raw(store, day, activities=text, activities_fetched_on=fetched_on)
    seed_day(store, d("2026-10-05"), readiness=60)  # recovery only
    days = {day.date.isoformat(): day for day in HistoryView(store).days(d("2026-09-21"), d("2026-10-05"))}
    for day, _, _, expected in ACTIVITY_CASES:
        assert days[day].activities == expected, day
        if expected is None:
            assert days[day].activities_settled is False, day
    assert days["2026-10-05"].activities is None and days["2026-10-05"].has_data is True
    assert days["2026-10-01"].has_data is False and days["2026-10-02"].has_data is False  # no readable fetch date
    assert days["2026-10-04"].has_data is True  # fetched; the text is missing
    merged = HistoryView(store).activity_days(d("2026-09-21"), d("2026-10-05"))
    assert sorted(merged) == ["2026-09-21", "2026-09-22", "2026-09-23"]


@pytest.mark.parametrize("failures", [0, 3])
def test_an_unreadable_activities_text_is_never_settled(store, failures):
    # the rule alone would say settled (fetched 4 days later, or given up); without a readable object it is not
    raw(store, "2026-10-01", activities="{", activities_fetched_on="2026-10-05", activities_failures=failures)
    (got,) = HistoryView(store).days(d("2026-10-01"), d("2026-10-01"))
    assert (got.activities, got.activities_fetched_on, got.activities_settled) == (None, d("2026-10-05"), False)
    assert HistoryView(store).activity_days(d("2026-10-01"), d("2026-10-01")) == {}


@pytest.mark.parametrize(
    ("offset", "failures", "settled"),
    [(1, 0, False), (2, 0, False), (3, 0, True), (4, 0, True), (1, 2, False), (1, 3, True), (2, 3, True)],
)
def test_settled_flag_from_the_fetch_date_and_failures(store, offset, failures, settled):
    day = d("2026-10-01")
    seed_activities(store, day, {"running": sport()}, fetched_on=day + timedelta(days=offset))
    for _ in range(failures):
        store.record_history_failure([day], "activities")
    (got,) = HistoryView(store).days(day, day)
    assert got.activities_settled is settled
    assert HistoryView(store).activity_days(day, day)["2026-10-01"].final is settled


def test_activities_settled_is_the_inverse_of_the_refetch_rule():
    day = d("2026-10-01")
    for offset in range(1, 5):
        fetched = day + timedelta(days=offset)
        for failures in range(4):
            assert activities_settled(day, fetched, failures) is (
                not needs_fetch(day, fetched, failures, fetched + timedelta(days=1))
            )


def test_days_are_the_stored_rows_of_the_range_oldest_first(store):
    seed_day(store, d("2026-09-30"), readiness=10)
    seed_day(store, d("2026-10-03"), readiness=30)
    seed_day(store, d("2026-10-01"), readiness=20)
    store.record_history_failure([d("2026-10-02")], "recovery")  # a row that only counts failures
    raw(store, "2026-10-02 ", readiness=99, recovery_fetched_on="2026-10-03")  # an unreadable date: skipped
    view = HistoryView(store)
    assert [x.date.isoformat() for x in view.days(d("2026-10-01"), d("2026-10-03"))] == [
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
    ]
    assert [x.date.isoformat() for x in view.days(None, d("2026-10-01"))] == ["2026-09-30", "2026-10-01"]
    failure_only = view.days(d("2026-10-02"), d("2026-10-02"))[0]
    assert failure_only.has_data is False and failure_only.values["readiness"] is None


def test_predictions_are_finite_values_of_the_range(store):
    store.store_race_predictions(
        {
            d("2026-10-01"): {"time_5k_s": 1290, "time_10k_s": None},
            d("2026-10-02"): {"time_half_marathon_s": 5970.5},
            d("2026-10-04"): {"time_marathon_s": 12310},
        },
        TODAY,
        d("2026-10-01"),
    )
    got = HistoryView(store).predictions(d("2026-10-01"), d("2026-10-03"))
    assert [p.date.isoformat() for p in got] == ["2026-10-01", "2026-10-02"]
    assert got[0].values == {"time_5k_s": 1290.0, "time_10k_s": None, "time_half_marathon_s": None, "time_marathon_s": None}
    assert got[1].values["time_half_marathon_s"] == 5970.5
    assert [p.date.isoformat() for p in HistoryView(store).predictions(None, TODAY)][-1] == "2026-10-04"


def test_predictions_that_are_not_finite_numbers_are_none(store, monkeypatch):
    # the table's CHECK keeps such values out of the database; the guard is for a store that returns them anyway
    rows = [{"date": "2026-10-01", "time_5k_s": float("inf"), "time_10k_s": float("nan"), "time_half_marathon_s": "slow",
             "time_marathon_s": 5970.5}]
    monkeypatch.setattr(store, "race_prediction_rows", lambda start, end: rows)
    (got,) = HistoryView(store).predictions(d("2026-10-01"), d("2026-10-01"))
    assert got.values == {"time_5k_s": None, "time_10k_s": None, "time_half_marathon_s": None, "time_marathon_s": 5970.5}


# ------------------------------------------------------------------ Hevy coverage and strength days


def state(covered_from: str, synced_on: str, synced_at: str | None = None) -> dict[str, str]:
    return {"covered_from": covered_from, "synced_at": synced_at or f"{synced_on}T01:30:00+00:00", "synced_on": synced_on}


def test_coverage_through_is_the_day_before_the_sync():
    assert coverage(None) is None
    cov = coverage(state("2026-09-01", "2026-10-07"))
    assert cov == Coverage(d("2026-09-01"), d("2026-10-06"))
    assert cov.covers(d("2026-09-01")) and cov.covers(d("2026-10-06"))
    assert not cov.covers(d("2026-08-31")) and not cov.covers(d("2026-10-07"))


@pytest.mark.parametrize(
    "bad",
    [
        {"covered_from": "x", "synced_at": "2026-10-06T01:30:00+00:00", "synced_on": "2026-10-06"},
        {"covered_from": "2026-07-10", "synced_at": "2026-10-06T01:30:00", "synced_on": "2026-10-06"},  # naive
        {"covered_from": "2026-07-10"},
        {"covered_from": "2026-07-10", "synced_at": None, "synced_on": "2026-10-06"},
    ],
)
def test_an_unusable_sync_state_has_no_coverage(bad):
    assert coverage(bad) is None


def row(day: str, *entries: dict[str, Any], workout_id: str = "W") -> dict[str, Any]:
    return {"workout_id": workout_id, "date": day, "exercises": exercises_json(entries)}


def found(group: str | None, type_: str = "weight_reps", title: str | None = None) -> dict[str, Any]:
    return {"found": 1, "primary_muscle_group": group, "type": type_, "title": title}


def test_day_totals_groups_by_primary_muscle_group():
    templates = Templates(
        {
            "A": found("chest", title="Bench"),
            "B": {"found": 0, "primary_muscle_group": None, "type": None, "title": None},
            "C": {"found": None, "primary_muscle_group": None, "type": None, "title": None},
            "D": found(None),
            "E": found("quadriceps"),
        }
    )
    rows = [
        row("2026-10-05", exercise("A", 3, 1500.0, (100, 5, 116.7)), exercise("B", 2, 100.0), workout_id="W1"),
        row("2026-10-05", exercise("C", 1, 10.0), exercise("D", 4, 40.0), exercise("E", 5, 500.0), workout_id="W2"),
        row("2026-10-06", exercise("A", 2, 800.0), exercise("Z", 1, 5.0)),  # Z: no template row at all
    ]
    totals = day_totals(rows, templates)
    mon, tue = totals[d("2026-10-05")], totals[d("2026-10-06")]
    assert (mon.workouts, mon.sets, mon.volume_kg) == (2, 15, 2150.0)
    assert mon.muscle_sets == {"chest": 3, "unknown": 7, "quadriceps": 5}
    assert mon.muscle_volume == {"chest": 1500.0, "unknown": 150.0, "quadriceps": 500.0}
    assert (tue.workouts, tue.sets, tue.volume_kg) == (1, 3, 805.0)
    assert tue.muscle_sets == {"chest": 2, "unknown": 1}
    assert mon.exercises["A"] == {
        "title": "Bench",
        "sets": 3,
        "volume_kg": 1500.0,
        "best": {"weight_kg": 100.0, "reps": 5, "e1rm_kg": 116.7},
    }
    assert isinstance(mon.exercises["A"]["best"]["weight_kg"], float)  # a stored 100 is read as 100.0


def test_day_totals_best_of_a_day_across_workouts_and_title_fallback():
    templates = Templates({"A": found("chest")})  # found, but without a title
    rows = [
        row("2026-10-05", exercise("A", 1, 100.0, (100, 5, 116.7), title="Stored title"), workout_id="W1"),
        row("2026-10-05", exercise("A", 1, 110.0, (110, 3, 121.0)), workout_id="W2"),
        row("2026-10-05", exercise("A", 1, 90.0, (90, 6, 108.0)), workout_id="W3"),
        row("2026-10-05", exercise("A", 1, 90.0, (115, 1, 121.0)), workout_id="W4"),  # same e1RM, heavier: wins
        row("2026-10-05", exercise("A", 1, 90.0, (115, 2, 121.0)), workout_id="W5"),  # a full tie: the first stays
    ]
    entry = day_totals(rows, templates)[d("2026-10-05")].exercises["A"]
    assert entry["best"] == {"weight_kg": 115.0, "reps": 1, "e1rm_kg": 121.0}
    assert (entry["sets"], entry["volume_kg"], entry["title"]) == (5, 480.0, "Stored title")


def test_assisted_template_counts_sets_but_no_volume_or_best():
    templates = Templates({"A": found("lats", type_="bodyweight_assisted_reps", title="Assisted pull-up")})
    totals = day_totals([row("2026-10-05", exercise("A", 3, 600.0, (40, 8, 50.7)))], templates)
    day = totals[d("2026-10-05")]
    assert (day.sets, day.volume_kg, day.muscle_sets, day.muscle_volume) == (3, 0.0, {"lats": 3}, {"lats": 0.0})
    assert day.exercises["A"] == {"title": "Assisted pull-up", "sets": 3, "volume_kg": 0.0, "best": None}
    normal = day_totals([row("2026-10-05", exercise("A", 3, 600.0, (40, 8, 50.7)))], Templates({"A": found("lats")}))
    assert normal[d("2026-10-05")].volume_kg == 600.0 and normal[d("2026-10-05")].exercises["A"]["best"] is not None


def test_a_template_that_was_not_found_gives_no_group_title_or_assisted_type():
    templates = Templates(
        {"A": {"found": 0, "primary_muscle_group": "lats", "type": "bodyweight_assisted_reps", "title": "Assisted"}}
    )
    assert (templates.group("A"), templates.title("A"), templates.counts_load("A")) == ("unknown", None, True)
    totals = day_totals([row("2026-10-05", exercise("A", 3, 600.0, (40, 8, 50.7)))], templates)
    assert totals[d("2026-10-05")].muscle_sets == {"unknown": 3}
    assert totals[d("2026-10-05")].volume_kg == 600.0


@pytest.mark.parametrize(
    ("best", "expected"),
    [
        ({"weight_kg": 40, "reps": 8, "e1rm_kg": 50.7}, {"weight_kg": 40.0, "reps": 8, "e1rm_kg": 50.7}),
        ({"weight_kg": 40, "reps": 8.0, "e1rm_kg": 50.7}, {"weight_kg": 40.0, "reps": 8, "e1rm_kg": 50.7}),
        ({"weight_kg": 40, "e1rm_kg": 50.7}, None),  # reps missing
        ({"weight_kg": 40, "reps": None, "e1rm_kg": 50.7}, None),
        ({"weight_kg": 40, "reps": 8.5, "e1rm_kg": 50.7}, None),
        ({"weight_kg": 40, "reps": True, "e1rm_kg": 50.7}, None),
        ({"weight_kg": 40, "reps": "8", "e1rm_kg": 50.7}, None),
        ({"reps": 8, "e1rm_kg": 50.7}, None),  # weight missing
        ({"weight_kg": float("inf"), "reps": 8, "e1rm_kg": 50.7}, None),
        ({"weight_kg": 40, "reps": 8}, None),  # e1RM missing
        ({"weight_kg": 40, "reps": 8, "e1rm_kg": True}, None),
        ([40, 8, 50.7], None),
    ],
)
def test_a_best_set_needs_a_weight_whole_reps_and_an_estimate(best, expected):
    entry = {"template_id": "A", "sets": 1, "volume_kg": 10.0, "best": best}
    totals = day_totals([{"date": "2026-10-05", "exercises": exercises_json([entry])}], Templates({}))
    assert totals[d("2026-10-05")].exercises["A"]["best"] == expected


def test_invalid_exercises_json_counts_the_workout_only():
    rows = [
        {"date": "2026-10-05", "exercises": "not json"},
        {"date": "2026-10-05", "exercises": "{}"},
        {"date": "2026-10-05", "exercises": '[1, {"template_id": 5, "sets": 2}, {"template_id": "A"}]'},
        {"date": "2026-10-05", "exercises": '[{"template_id": "A", "sets": 2, "volume_kg": "9", "best": {"weight_kg": 1}}]'},
        {"date": "garbage", "exercises": "[]"},
        {"exercises": "[]"},
    ]
    totals = day_totals(rows, Templates({}))
    assert list(totals) == [d("2026-10-05")]
    day = totals[d("2026-10-05")]
    assert (day.workouts, day.sets, day.volume_kg) == (4, 2, 0.0)
    assert day.exercises == {"A": {"title": None, "sets": 2, "volume_kg": 0.0, "best": None}}


def test_strength_through_the_store(store):
    seed_template(store, "T1", "chest", title="Bench")
    store.upsert_hevy_template("T5", found=False, title=None, type_=None, group=None, attempted_on=d("2026-01-01"))
    seed_strength(store, "W1", d("2026-10-01"), [exercise("T1", 3, 1500.0, (100, 5, 116.7)), exercise("T5", 2, 40.0)])
    seed_strength(store, "W2", d("2026-10-03"), [exercise("T9", 1, 10.0)])  # no template row
    seed_strength(store, "W3", d("2026-10-04"), [exercise("T1", 9, 9000.0)])
    store.apply_strength_events({}, ["W3"])  # deleted in Hevy
    seed_strength(store, "W4", d("2026-09-30"), [exercise("T1", 1, 100.0)])  # before the range
    view = HistoryView(store)
    assert view.strength(d("2026-10-01"), d("2026-10-06")).coverage is None  # never synced
    seed_sync(store, d("2026-09-01"), d("2026-10-05"))
    got = view.strength(d("2026-10-01"), d("2026-10-06"))
    assert got.coverage == Coverage(d("2026-09-01"), d("2026-10-04"))
    assert sorted(got.days) == [d("2026-10-01"), d("2026-10-03")]
    assert got.days[d("2026-10-01")].muscle_sets == {"chest": 3, "unknown": 2}
    assert dict(got.groups) == {"T1": "chest", "T5": "unknown", "T9": "unknown"}
    assert got.group("T1") == "chest" and got.group("never-seen") == "unknown"
    assert sorted(view.strength(None, d("2026-10-01")).days) == [d("2026-09-30"), d("2026-10-01")]


def test_activity_days_merge_hevy_only_on_covered_days(store):
    for day in ("2026-10-01", "2026-10-02", "2026-10-04", "2026-10-05", "2026-10-06"):
        seed_activities(store, d(day), {"strength": sport(1, 40)}, fetched_on=d(day) + timedelta(days=3))
    for n, day in enumerate(("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-05", "2026-10-06")):
        seed_strength(store, f"W{n}", d(day), [exercise("T1", 4, 400.0)])
    seed_sync(store, d("2026-10-02"), d("2026-10-06"))  # covers 10-02 .. 10-05
    got = HistoryView(store).activity_days(d("2026-10-01"), d("2026-10-06"))
    assert sorted(got) == ["2026-10-01", "2026-10-02", "2026-10-04", "2026-10-05", "2026-10-06"]  # 10-03 not fetched
    hevy = {"count": 1, "source": "hevy", "sets": 4, "volume_kg": 400.0}
    assert got["2026-10-02"].totals["strength"] == hevy and got["2026-10-05"].totals["strength"] == hevy
    for day in ("2026-10-01", "2026-10-06"):  # before and after the coverage: the watch's own record
        assert got[day].totals["strength"] == sport(1, 40), day
    assert got["2026-10-04"].totals["strength"] == sport(1, 40)  # covered, but no Hevy workout
    # final: the Garmin day is settled, and a Hevy day only 3 or more days before the end of the coverage
    assert (got["2026-10-01"].final, got["2026-10-02"].final, got["2026-10-05"].final) == (True, True, False)


def test_activity_days_round_the_hevy_volume_to_one_decimal(store):
    seed_activities(store, d("2026-10-01"), {"strength": sport(1, 40)}, fetched_on=d("2026-10-04"))
    seed_strength(store, "W1", d("2026-10-01"), [exercise("T1", 4, 1145.26)])  # 80 x 8.3 + 62.5 x 7.7 and a bit
    seed_sync(store, d("2026-09-01"), d("2026-10-06"))
    got = HistoryView(store).activity_days(d("2026-10-01"), d("2026-10-01"))
    assert got["2026-10-01"].totals["strength"]["volume_kg"] == 1145.3


@pytest.mark.anyio
async def test_the_job_stores_the_primary_group_and_the_weekly_report_counts_it(store):
    """Through the job: a template's secondary groups are read, but only the primary one is stored and counted."""
    template = hevy_template("T1", "Bench Press (Barbell)", "chest")
    template["secondary_muscle_groups"] = ["triceps", "shoulders"]
    source = FakeStrengthSource(
        events=lambda page, since, index: ("ok", events_page([updated(day_workout("W1", "2026-10-05"))], page, 1)),
        template=lambda tid, index: ("ok", {"exercise_template": template}),
    )
    job, clock = make_job(store, FakeHistorySource(), local(TODAY, 10, 0), strength=source, history_backfill_days=5)
    await run_first(job, clock)
    assert store.hevy_templates()["T1"]["primary_muscle_group"] == "chest"
    assert trends.get_weekly_report(store, TODAY, week="2026-10-05")["strength"] == {
        "days": 2,
        "workouts": 1,
        "volume_kg": {"week": 1500.0, "baseline": None, "baseline_weeks": 0, "change": None, "change_pct": None},
        "sets": {"week": 3.0, "baseline": None, "baseline_weeks": 0, "change": None, "change_pct": None},
        "by_muscle_group": {"chest": {"sets": 3, "baseline_sets": None, "volume_kg": 1500.0, "baseline_volume_kg": None}},
    }


def test_sync_time_is_ignored_for_coverage(store):
    store.record_hevy_sync(d("2026-09-01"), datetime(2026, 10, 6, 23, 59, tzinfo=UTC), d("2026-10-07"))
    assert HistoryView(store).strength(d("2026-10-01"), d("2026-10-06")).coverage == Coverage(d("2026-09-01"), d("2026-10-06"))
