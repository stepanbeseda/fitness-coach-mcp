"""Plan reconciliation: the pure matching, the day helpers of the history job and the nightly step."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, timedelta

import pytest
from fakes import FakeHistorySource
from garmin_payloads import activities_payload, activity
from fakes import FakeStrengthSource
from hevy_payloads import day_workout, events_page, updated
from history_helpers import local, make_job, next_run, run_first, run_job, seed_activities, seed_day
from view_values import sport

from coach_mcp import strength as strength_module
from coach_mcp.history_view import Coverage, DayStrength, with_hevy_strength
from coach_mcp.reconcile import MATCHED_TYPES, RECONCILE_DAYS, DayActivities, Suggestion, suggest

TODAY = date(2026, 10, 7)
NOW = "2026-10-01T00:00:00+00:00"


def s(id_, date_="2026-10-06", type_="running", status="planned"):
    return {"id": id_, "date": date_, "type": type_, "status": status}


def day(totals, final=False):
    return DayActivities(totals, final)


def evidence(sport_="running", count=1, **extra):
    return {"source": "garmin", "sport": sport_, "count": count, **extra}


# ------------------------------------------------------------------ pure


def test_constants():
    assert RECONCILE_DAYS == 7
    assert MATCHED_TYPES == ("running", "cycling", "swimming", "strength")


def test_match_gives_evidence_from_the_day_totals():
    days = {"2026-10-06": day({"running": sport(1, 45.0, 9.5, 60)})}
    assert suggest([s(1)], days) == {
        1: Suggestion("matched", False, evidence(duration_min=45.0, distance_km=9.5))
    }
    final = {"2026-10-06": day({"running": sport(1, 45.0, 9.5, 60)}, final=True)}
    assert suggest([s(1)], final)[1].final is True


def test_no_activity_on_a_fetched_empty_day():
    assert suggest([s(1)], {"2026-10-06": day({})}) == {1: Suggestion("no_activity", False, None)}


def test_final_passes_through_to_no_activity():
    assert suggest([s(1)], {"2026-10-06": day({}, final=True)}) == {1: Suggestion("no_activity", True, None)}


def test_different_sport_is_no_activity():
    totals = {"cycling": sport(1, 60, 25), "walking": sport(1, 30, 2)}
    assert suggest([s(1)], {"2026-10-06": day(totals)}) == {1: Suggestion("no_activity", False, None)}


def test_two_sessions_of_different_sports_on_one_day():
    sessions = [s(1), s(2, type_="strength")]
    only_run = {"2026-10-06": day({"running": sport(1, 45, 9)})}
    got = suggest(sessions, only_run)
    assert (got[1].result, got[2].result) == ("matched", "no_activity")
    both = {"2026-10-06": day({"running": sport(1, 45, 9), "strength": sport(1, 50, 0, 40)})}
    got = suggest(sessions, both)
    assert (got[1].result, got[2].result) == ("matched", "matched")
    assert got[2].evidence == {"source": "garmin", "sport": "strength", "count": 1, "duration_min": 50}


def test_two_sessions_of_one_sport_share_the_count():
    sessions = [s(5), s(3)]  # given out of order: the planned ones are taken in order of id

    def results(count):
        days = {"2026-10-06": day({"running": sport(count, 30, 5)})}
        return {i: x.result for i, x in suggest(sessions, days).items()}

    assert results(1) == {3: "matched", 5: "no_activity"}
    assert results(2) == {3: "matched", 5: "matched"}
    assert results(3) == {3: "matched", 5: "matched"}


def test_done_session_takes_an_activity_first():
    sessions = [s(1, status="done"), s(2)]
    one = suggest(sessions, {"2026-10-06": day({"running": sport(1, 30, 5)})})
    assert one == {2: Suggestion("no_activity", False, None)}
    two = suggest(sessions, {"2026-10-06": day({"running": sport(2, 60, 10)})})
    assert set(two) == {2} and two[2].result == "matched"


@pytest.mark.parametrize("status", ["skipped", "moved", "superseded"])
def test_skipped_moved_superseded_take_nothing(status):
    sessions = [s(1, status=status), s(2)]
    got = suggest(sessions, {"2026-10-06": day({"running": sport(1, 30, 5)})})
    assert set(got) == {2} and got[2].result == "matched"


def test_never_fetched_day_gives_no_suggestion():
    assert suggest([s(1)], {"2026-10-05": day({"running": sport(1, 30, 5)})}) == {}


def test_other_dates_do_not_share_activities():
    sessions = [s(1, "2026-10-05"), s(2, "2026-10-06")]
    days = {"2026-10-05": day({}), "2026-10-06": day({"running": sport(1, 30, 5)})}
    got = suggest(sessions, days)
    assert (got[1].result, got[2].result) == ("no_activity", "matched")


def test_mobility_and_other_never_get_a_suggestion():
    sessions = [s(1, type_="mobility"), s(2, type_="other")]
    assert suggest(sessions, {"2026-10-06": day({"other": sport(1, 30)})}) == {}


@pytest.mark.parametrize("entry", [5, {"count": True}, {"count": -1}, {"count": 1.0}, {"count": "1"}, {}])
def test_unusable_count_counts_as_zero(entry):
    got = suggest([s(1)], {"2026-10-06": day({"running": entry})})
    assert got == {1: Suggestion("no_activity", False, None)}


@pytest.mark.parametrize(
    "entry",
    [
        {"count": 1, "duration_min": "x", "distance_km": True},
        {"count": 1, "duration_min": 0, "distance_km": 0.0},
        {"count": 1, "duration_min": float("nan"), "distance_km": float("inf") * -1},
    ],
)
def test_evidence_keeps_only_positive_numbers(entry):
    got = suggest([s(1)], {"2026-10-06": day({"running": entry})})
    assert got[1].evidence == evidence()


# ------------------------------------------------------- history helpers


def row(date_="2026-10-06", activities=None, fetched_on=None, failures=0):
    return {
        "date": date_,
        "activities": activities,
        "activities_fetched_on": fetched_on,
        "activities_failures": failures,
    }


# --------------------------------------------------------------- the job step


def add_session(store, day_, type_="running", status="planned", description="Run") -> int:
    conn = sqlite3.connect(store.path)
    try:
        cur = conn.execute(
            "INSERT INTO sessions (date, type, description, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (day_, type_, description, status, NOW, NOW),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def session_statuses(store) -> list[tuple[int, str]]:
    conn = sqlite3.connect(store.path)
    try:
        return conn.execute("SELECT id, status FROM sessions ORDER BY id").fetchall()
    finally:
        conn.close()


def count_suggestions(store) -> int:
    conn = sqlite3.connect(store.path)
    try:
        return conn.execute("SELECT count(*) FROM session_suggestions").fetchone()[0]
    finally:
        conn.close()


def history_lines(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "coach_mcp.history"]


def at(store, ids):
    return store.suggestions_for(ids)


@pytest.mark.anyio
async def test_reconcile_step_stores_suggestions_for_the_last_seven_days(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    outside = add_session(store, "2026-09-29")
    old = add_session(store, "2026-09-30")
    run = add_session(store, "2026-10-06")
    strength = add_session(store, "2026-10-06", type_="strength")
    mobility = add_session(store, "2026-10-06", type_="mobility")
    future = add_session(store, "2026-10-07")
    # Today's totals exist in the store (a stray row): the window still ends yesterday.
    seed_activities(store, TODAY, {"running": sport(1, 30, 5)}, fetched_on=TODAY)
    payload = activities_payload(
        [
            activity("2026-09-29", "running", 40, 8),
            activity("2026-09-30", "running", 40, 8),
            activity("2026-10-06", "running", 45, 9.5),
        ]
    )
    source = FakeHistorySource(activities=lambda start, end, index: ("ok", payload))
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=10)
    await run_first(job, clock)

    ids = [outside, old, run, strength, mobility, future]
    got = at(store, ids)
    assert set(got) == {old, run, strength}
    assert (got[old]["result"], got[old]["final"]) == ("matched", 1)  # fetched 10-07 = day + 7
    assert (got[run]["result"], got[run]["final"]) == ("matched", 0)
    assert (got[strength]["result"], got[strength]["final"]) == ("no_activity", 0)
    assert {g["evaluated_on"] for g in got.values()} == {"2026-10-07"}
    assert all(status == "planned" for _, status in session_statuses(store))  # A1: no status changes
    (record,) = history_lines(caplog)
    assert record.getMessage().endswith(" matched=2 no_activity=1")


@pytest.mark.anyio
async def test_reconcile_runs_when_nothing_is_pending(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    seed_day(store, date(2026, 10, 6))
    seed_activities(store, date(2026, 10, 6), {"running": sport(1, 30, 5)})
    sid = add_session(store, "2026-10-06")
    source = FakeHistorySource()
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=1)
    await run_first(job, clock)
    assert source.calls == []
    (record,) = history_lines(caplog)
    assert record.getMessage().startswith("history status=done fetched=0 failed=0 pending=0")
    assert at(store, [sid])[sid]["result"] == "matched"


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["auth", "rate_limited"])
async def test_reconcile_runs_when_the_run_stops(store, caplog, case):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    seed_day(store, date(2026, 10, 5), fetched_on=date(2026, 10, 6))
    seed_activities(store, date(2026, 10, 5), {"running": sport(1, 30, 5)}, fetched_on=date(2026, 10, 6))
    sid = add_session(store, "2026-10-05")
    if case == "auth":
        source = FakeHistorySource(auth="missing")
    else:
        source = FakeHistorySource(activities=lambda start, end, index: ("rate_limited", None))
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=2)
    await run_first(job, clock)
    (record,) = history_lines(caplog)
    assert record.getMessage().startswith(f"history status={case}")
    if case == "auth":
        assert source.calls == []
    else:
        assert len(source.calls) == 1
    got = at(store, [sid])[sid]
    assert (got["result"], got["final"]) == ("matched", 0)


@pytest.mark.anyio
async def test_reconcile_error_is_logged_and_the_run_stands(store, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("SECRET-DB-TEXT")

    monkeypatch.setattr(store, "upsert_suggestions", boom)
    add_session(store, "2026-10-06")
    source = FakeHistorySource()
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=1)
    delay = await run_first(job, clock)
    (record,) = history_lines(caplog)
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert message.startswith("history status=done") and message.endswith("reconcile=error kind=OperationalError")
    assert "SECRET-DB-TEXT" not in caplog.text
    assert len(store.history_rows(None, TODAY)) == 1  # the history itself was stored
    next_due = (clock.now() + timedelta(seconds=delay)).astimezone(local(TODAY, 0).tzinfo)
    assert next_due == local(date(2026, 10, 8), 3, 30)


@pytest.mark.anyio
async def test_late_activity_updates_the_suggestion_next_night(store):
    sid = add_session(store, "2026-10-06")
    flag = {"late": False}

    def activities(start, end, index):
        if flag["late"] and start <= date(2026, 10, 6) <= end:
            return "ok", activities_payload([activity("2026-10-06", "running", 45, 9.5)])
        return "ok", activities_payload([])

    job, clock = make_job(store, FakeHistorySource(activities=activities), local(TODAY, 10, 0), history_backfill_days=3)
    delay = await run_first(job, clock)
    got = at(store, [sid])[sid]
    assert (got["result"], got["final"], got["evaluated_on"]) == ("no_activity", 0, "2026-10-07")
    flag["late"] = True
    await next_run(job, clock, delay)
    got = at(store, [sid])[sid]
    assert (got["result"], got["evaluated_on"]) == ("matched", "2026-10-08")
    assert count_suggestions(store) == 1  # updated, not duplicated


@pytest.mark.anyio
async def test_reconcile_logs_carry_no_values(store, caplog):
    caplog.set_level(logging.DEBUG)
    add_session(store, "2026-10-06")
    payload = activities_payload([activity("2026-10-06", "running", 45, 9.5)])
    source = FakeHistorySource(activities=lambda start, end, index: ("ok", payload))
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=1)
    await run_first(job, clock)
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("coach_mcp")]
    assert lines and all(line.startswith("history status=") for line in lines)
    assert "matched=1 no_activity=0" in lines[0]
    for line in lines:
        assert not any(bit in line for bit in ("2026-", "running", "9.5"))


# ---------------------------------------------------- Hevy strength in the matching

COV = Coverage(date(2026, 9, 1), date(2026, 10, 6))
D6 = date(2026, 10, 6)


def hevy(workouts=1, sets=12, volume=3400.0) -> dict[date, DayStrength]:
    return {D6: DayStrength(workouts=workouts, sets=sets, volume_kg=volume)}


def merged(totals, hevy_days, cov=COV, final=False, day_text="2026-10-06"):
    return with_hevy_strength({day_text: day(totals, final)}, hevy_days, cov)[day_text]


def test_hevy_workout_matches_a_strength_session():
    days = with_hevy_strength({"2026-10-06": day({})}, hevy(), COV)  # Garmin totals fetched, no strength
    assert suggest([s(1, type_="strength")], days) == {
        1: Suggestion(
            "matched",
            False,
            {"count": 1, "sets": 12, "source": "hevy", "sport": "strength", "volume_kg": 3400.0},
        )
    }


def test_uncovered_day_keeps_garmin_behaviour():
    sessions = [s(1, type_="strength")]
    for cov in (None, Coverage(date(2026, 9, 1), date(2026, 10, 5)), Coverage(date(2026, 10, 7), date(2026, 10, 20))):
        days = with_hevy_strength({"2026-10-06": day({})}, hevy(), cov)  # the day is after / before the coverage
        assert suggest(sessions, days) == {1: Suggestion("no_activity", False, None)}, cov


def test_tie_prefers_hevy_more_watch_sessions_keep_garmin():
    one = merged({"strength": sport(1, 50, 0, 40)}, hevy(workouts=1))
    assert one.totals["strength"]["source"] == "hevy" and one.totals["strength"]["count"] == 1
    two = merged({"strength": sport(2, 90, 0, 70)}, hevy(workouts=1))
    assert two.totals["strength"] == sport(2, 90, 0, 70)  # unchanged Garmin evidence, count 2
    got = suggest([s(1, type_="strength")], {"2026-10-06": two})
    assert got[1].evidence == {"source": "garmin", "sport": "strength", "count": 2, "duration_min": 90}
    more_hevy = merged({"strength": sport(1, 50, 0, 40)}, hevy(workouts=2))
    assert more_hevy.totals["strength"]["count"] == 2


def test_covered_day_without_workouts_is_no_activity():
    for hevy_days in ({}, hevy(workouts=0)):
        merged_day = merged({}, hevy_days)
        assert suggest([s(1, type_="strength")], {"2026-10-06": merged_day}) == {1: Suggestion("no_activity", False, None)}
    assert merged({}, hevy(workouts=0)).totals == {}  # a covered day with no workout adds no strength entry
    # without a Hevy workout the watch's own strength activity still matches
    watch = merged({"strength": sport(1, 50, 0, 40)}, {})
    assert suggest([s(1, type_="strength")], {"2026-10-06": watch})[1].evidence["source"] == "garmin"


@pytest.mark.parametrize(("through", "final"), [(D6, False), (date(2026, 10, 8), False), (date(2026, 10, 9), True)])
def test_hevy_day_inside_overlap_is_not_final(through, final):
    # cov.through - day = 0, 2 and 3 days: only a day outside the 3-day re-read of the Hevy listing is final
    cov = Coverage(date(2026, 9, 1), through)
    assert merged({}, hevy(), cov=cov, final=True).final is final
    assert merged({}, hevy(), cov=cov, final=False).final is False  # a Garmin day that can still change stays open


def test_other_sports_and_days_are_untouched():
    days = {"2026-10-06": day({"running": sport(1, 30, 5)}), "2026-10-05": day({"strength": sport(1, 50)}, final=True)}
    out = with_hevy_strength(days, hevy(), COV)
    assert out["2026-10-06"].totals["running"] == sport(1, 30, 5) and out["2026-10-06"].totals["strength"]["source"] == "hevy"
    assert out["2026-10-05"] == days["2026-10-05"]  # no Hevy workout that day
    assert days["2026-10-06"].totals == {"running": sport(1, 30, 5)}  # the input is not changed


def test_day_without_garmin_fetch_gets_no_suggestion_even_with_hevy():
    out = with_hevy_strength({}, hevy(), COV)
    assert out == {}
    assert suggest([s(1, type_="strength")], out) == {}


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"count": 1, "source": "hevy", "sets": 5, "volume_kg": 100.5}, {"source": "hevy", "sets": 5, "volume_kg": 100.5}),
        ({"count": 1, "source": "x", "sets": 5}, {"source": "garmin", "sets": 5}),
        ({"count": 1, "source": 5}, {"source": "garmin"}),
        ({"count": 1, "source": "hevy", "sets": True, "volume_kg": True}, {"source": "hevy"}),
        ({"count": 1, "sets": 5.5, "volume_kg": float("nan")}, {"source": "garmin"}),
        ({"count": 1, "sets": "5", "volume_kg": float("inf")}, {"source": "garmin"}),
    ],
)
def test_evidence_source_comes_from_the_entry(entry, expected):
    got = suggest([s(1, type_="strength")], {"2026-10-06": day({"strength": entry})})
    assert got[1].evidence == {"sport": "strength", "count": 1, **expected}


@pytest.mark.anyio
async def test_the_reconcile_step_uses_the_local_date_just_after_midnight(store):
    # 00:29 local on 10-08 is 22:29 UTC on 10-07: today is 10-08, so a session of 10-07 is inside the window
    session_id = add_session(store, "2026-10-07")
    job, clock = make_job(store, FakeHistorySource(), local(date(2026, 10, 8), 0, 29), history_backfill_days=1)
    await run_first(job, clock)
    assert store.suggestions_for([session_id])[session_id]["evaluated_on"] == "2026-10-08"


@pytest.mark.anyio
async def test_hevy_workout_matches_a_planned_strength_session_in_one_run(store, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")
    session_id = add_session(store, "2026-10-06", type_="strength", description="Legs")
    workout = day_workout("W1", "2026-10-06", sets=4, weight_kg=100, reps=5)
    strength = FakeStrengthSource(events=lambda page, since, index: ("ok", events_page([updated(workout)], page, 1)))
    garmin = FakeHistorySource(activities=lambda start, end, index: ("ok", activities_payload([])))  # no watch strength
    job, clock = make_job(store, garmin, local(TODAY, 10, 0), strength=strength, history_backfill_days=10)
    await run_first(job, clock)
    # the Hevy part ran before the reconcile step, so the suggestion of the same run already uses it
    got = store.suggestions_for([session_id])[session_id]
    assert got["result"] == "matched" and got["final"] == 0
    assert json.loads(got["evidence"]) == {"count": 1, "sets": 4, "source": "hevy", "sport": "strength", "volume_kg": 2000.0}
    assert all(status == "planned" for _, status in session_statuses(store))  # still only a suggestion
    messages = [r.getMessage() for r in history_lines(caplog)]
    assert "part=strength" in messages[0] and messages[1].endswith(" matched=1 no_activity=0")


@pytest.mark.anyio
async def test_a_day_the_hevy_history_does_not_cover_is_matched_by_the_watch_only(store):
    session_id = add_session(store, "2026-10-06", type_="strength")
    down = FakeStrengthSource(events=lambda page, since, index: ("unreachable", None))
    job, clock = make_job(store, FakeHistorySource(), local(TODAY, 10, 0), strength=down, history_backfill_days=10)
    await run_first(job, clock)
    # Hevy was never listed: no sync row, so the day stays "no_activity" exactly as in PR 13
    assert store.hevy_sync_state() is None
    assert store.suggestions_for([session_id])[session_id]["result"] == "no_activity"


@pytest.mark.anyio
async def test_strength_crash_keeps_the_garmin_line_and_reconcile(store, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.history")

    def broken(events, zone):
        raise RuntimeError("SECRET-PARSE-TEXT")

    monkeypatch.setattr(strength_module, "parse_events", broken)
    session_id = add_session(store, "2026-10-06")
    seed_day(store, D6, fetched_on=TODAY, readiness=50)  # nothing for the Garmin part to do: no pacing sleeps
    seed_activities(store, D6, {"running": sport(1, 45, 9)}, fetched_on=TODAY)
    strength = FakeStrengthSource(events=lambda page, since, index: ("ok", events_page([updated(day_workout("W1", "2026-10-05"))], page, 1)))
    job, clock = await run_job(
        store, FakeHistorySource(), local(TODAY, 10, 0), max_sleeps=3, strength=strength,
        settings_overrides={"history_backfill_days": 1},
    )
    assert clock.sleeps == [60.0, 3600.0, 3600.0]  # the crash is retried in an hour
    lines = [r.getMessage() for r in history_lines(caplog)]
    assert lines[0].startswith("history status=done fetched=0") and lines[0].endswith(" matched=1 no_activity=0")
    assert lines[1] == "history status=crashed kind=RuntimeError"
    assert not any("part=strength" in line for line in lines)
    assert store.suggestions_for([session_id])[session_id]["result"] == "matched"  # the reconcile step still ran
    assert "SECRET-PARSE-TEXT" not in caplog.text
