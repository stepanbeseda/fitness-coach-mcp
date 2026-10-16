"""Criterion 4 of PR 24: the history tools return the same JSON for the same stored data.

``seed`` writes one rich store through the write side (the Store API, plus raw SQL for values the job never
writes but the read side must survive); ``outputs`` calls every reader with many argument sets and returns the
exact ``to_json`` text of each result (or ``{"error": ...}`` for a ToolError). ``history_golden.json`` holds the
texts produced by the code before PR 24 (commit da44156); it is never regenerated. Texts are compared exactly,
so an int that became a float (``100`` against ``100.0``) fails.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fakes import FakeHistorySource
from mcp.server.mcpserver.exceptions import ToolError

from coach_mcp import correlations, goals, strength, tools, trends
from coach_mcp.config import Settings
from coach_mcp.history import HistoryJob
from coach_mcp.store import Store

GOLDEN = Path(__file__).with_name("history_golden.json")
TODAY = date(2026, 10, 7)
FIRST = date(2026, 5, 1)
NOW = "2026-10-07T03:00:00+00:00"
SPORTS = ("running", "cycling", "swimming", "walking", "hiking", "strength", "other")
STATUSES = ("hrv_low", "BALANCED", "UNBALANCED", None)


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def _sport(n: int, k: int) -> dict[str, Any]:
    return {
        "count": 1 + (n + k) % 2,
        "duration_min": round(20 + (n * 7 + k * 11) % 70 + 0.5 * (n % 3), 1),
        "distance_km": round(((n * 3 + k) % 17) * 0.73, 2) if k < 5 else 0.0,
        "training_load": round(15 + (n * 5 + k * 13) % 90 + 0.1 * (n % 7), 1),
    }


def _seed_history(store: Store, conn: sqlite3.Connection) -> None:
    for n, day in enumerate(_days(FIRST, TODAY - timedelta(days=1))):
        if n % 11 != 5:
            values: dict[str, Any] = {
                "readiness": 40 + (n * 7) % 50,
                "hrv_last_night_ms": 45 + (n * 3) % 25 + 0.5,
                "hrv_weekly_avg_ms": 50 + (n % 9),
                "hrv_status": STATUSES[n % 4],
                "sleep_duration_h": round(6 + (n % 5) * 0.37, 2),
                "sleep_score": 60 + (n * 13) % 35,
                "resting_hr": 44 + (n * 5) % 12,
                "body_battery_high": 70 + n % 30,
                "body_battery_low": 5 + n % 20,
                "stress_avg": 20 + (n * 3) % 30,
                "acute_load": 300 + (n * 17) % 400,
                "chronic_load": 350 + (n * 7) % 200,
                "acwr_ratio": round(0.8 + (n % 9) * 0.07, 2),
                "vo2max_running": round(50 + n * 0.03, 1) if n % 2 == 0 else None,
                "vo2max_cycling": 48 + n % 4 if n % 3 == 0 else None,
            }
            if n % 13 == 0:
                values = {k: (None if i % 2 else v) for i, (k, v) in enumerate(values.items())}
            store.upsert_history_recovery(day, values, day + timedelta(days=1), missing="hrv" if n % 17 == 0 else "")
        if n % 9 == 4 and day < date(2026, 7, 15):
            continue  # activities never fetched
        totals = {sport: _sport(n, k) for k, sport in enumerate(SPORTS) if (n + k) % (3 + k % 3) == 0}
        fetched = min(day + timedelta(days=1 + n % 4), TODAY)
        store.upsert_history_activities({day: json.dumps(totals, sort_keys=True, separators=(",", ":"))}, fetched)
    # Rows that both old readers handle the same way: bad JSON, the wrong shape, bad sport entries, non-finite and
    # text values in REAL columns, failure-only rows.
    odd = {
        date(2026, 9, 2): "not json",
        date(2026, 9, 3): "[1, 2]",
        date(2026, 9, 4): '"x"',
        date(2026, 9, 9): '{"running": 7}',
        date(2026, 9, 10): '{"running": {"duration_min": true, "count": 1}}',
        date(2026, 9, 16): '{"running": {"count": 1, "distance_km": "5", "duration_min": 30}}',
    }
    for day, text in odd.items():
        store.upsert_history_activities({day: text}, day + timedelta(days=3))
    for day, column, value in (
        (date(2026, 9, 22), "resting_hr", float("inf")),
        (date(2026, 9, 23), "hrv_last_night_ms", float("-inf")),
        (date(2026, 9, 24), "readiness", "high"),
        (date(2026, 8, 12), "vo2max_running", float("inf")),
    ):
        conn.execute(f"UPDATE history_days SET {column} = ? WHERE date = ?", (value, day.isoformat()))
    conn.commit()
    store.record_history_failure([date(2026, 4, 28)], "recovery")
    store.record_history_failure([date(2026, 4, 29)], "activities")


def _seed_predictions(store: Store) -> None:
    values: dict[date, dict[str, float | None]] = {}
    for n, day in enumerate(_days(date(2026, 6, 1), TODAY - timedelta(days=1))):
        if n % 6 == 5:
            continue
        values[day] = {
            "time_5k_s": 1500 - n * 0.5,
            "time_10k_s": 3150 - n * 0.9 if n % 4 else None,
            "time_half_marathon_s": 6000 - 2 * n,
            "time_marathon_s": 13000 - 3 * n if n < 90 else None,
        }
    store.store_race_predictions(values, TODAY, date(2026, 6, 1))  # the table's CHECKs keep values in (0, 86400]


def _seed_goals(store: Store) -> None:
    store.insert_goal({"title": "Half", "metric": "race_half_marathon", "target_value": 5800.0, "target_date": "2026-11-15"})
    store.insert_goal({"title": "VO2", "metric": "vo2max_running", "target_value": 55.0, "target_date": "2026-12-31"})
    store.insert_goal({"title": "5k", "metric": "race_5k", "target_value": 1380.0})
    store.insert_goal({"title": "Marathon", "metric": "race_marathon", "target_date": "2026-11-01"})
    store.insert_goal({"title": "Feel good"})


def _exercise(tid: str, sets: int, volume: float, best: tuple[float, int, float] | None, title: str | None = None) -> dict[str, Any]:
    return {
        "template_id": tid,
        "title": title,
        "sets": sets,
        "volume_kg": volume,
        "best": None if best is None else {"weight_kg": best[0], "reps": best[1], "e1rm_kg": best[2]},
    }


def _seed_strength(store: Store) -> None:
    on = date(2026, 1, 1)
    store.upsert_hevy_template("T1", found=True, title="Bench Press", type_="weight_reps", group="chest", attempted_on=on)
    store.upsert_hevy_template("T2", found=True, title="Squat", type_="weight_reps", group="quadriceps", attempted_on=on)
    store.upsert_hevy_template("T3", found=True, title="Assisted Pull-up", type_="bodyweight_assisted_reps", group="lats", attempted_on=on)
    store.upsert_hevy_template("T4", found=True, title=None, type_="weight_reps", group=None, attempted_on=on)
    store.upsert_hevy_template("T5", found=False, title=None, type_=None, group=None, attempted_on=on)
    updates: dict[str, tuple[date, str | None, str]] = {}
    for w, monday in enumerate(_days(date(2026, 6, 15), TODAY)[::7]):
        tue, thu, sat = monday + timedelta(days=1), monday + timedelta(days=3), monday + timedelta(days=5)
        bench = (80 + 2.5 * (w % 6), 5, round((80 + 2.5 * (w % 6)) * (1 + 5 / 30), 1))
        entries = [_exercise("T1", 3 + w % 3, 1200.0 + 50 * w, bench, "Stored bench"), _exercise("T4", 2, 75.5, (12.5, 10, 16.7))]
        updates[f"A{w}"] = (tue, None, json.dumps(entries, sort_keys=True, separators=(",", ":")))
        squat = (100 + 5 * (w % 4), 3, round((100 + 5 * (w % 4)) * 1.1, 1))
        entries = [_exercise("T2", 4, 2400.0 + 10 * w, squat), _exercise("T3", 3, 300.0, (20, 8, 25.3))]
        if w % 3 == 0:
            entries.append(_exercise("T5", 2, 40.0, (10, 2, 10.7), "Unknown move"))
        updates[f"B{w}"] = (thu, None, json.dumps(entries, sort_keys=True, separators=(",", ":")))
        if w % 4 == 1:
            updates[f"C{w}"] = (sat, None, json.dumps([_exercise("T1", 2, 800.0, (bench[0] + 5, 1, bench[0] + 5))]))
    updates["BAD"] = (date(2026, 9, 17), None, "not json")
    updates["GONE"] = (date(2026, 9, 18), None, json.dumps([_exercise("T1", 9, 9000.0, (200, 1, 200.0))]))
    store.apply_strength_events(updates, [])
    store.apply_strength_events({}, ["GONE"])
    store.record_hevy_sync(date(2026, 7, 1), datetime(2026, 10, 7, 1, 30, tzinfo=UTC), TODAY)


def _seed_sessions(conn: sqlite3.Connection) -> None:
    kinds = ("running", "cycling", "strength", "swimming", "mobility", "running")
    for n, day in enumerate(_days(date(2026, 8, 24), date(2026, 10, 11))):
        if n % 3 == 2:
            continue
        status = "planned" if day >= date(2026, 9, 30) else ("done", "skipped", "planned", "superseded", "done")[n % 5]
        conn.execute(
            "INSERT INTO sessions (date, type, description, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (day.isoformat(), kinds[n % 6], f"Session {n}", status, NOW, NOW),
        )
    for n, day in enumerate(_days(date(2026, 9, 30), date(2026, 10, 6))):  # the reconcile window: every type daily
        for kind in ("running", "strength", "cycling") if n % 2 else ("strength", "running"):
            status = "done" if (n, kind) == (3, "running") else "planned"
            conn.execute(
                "INSERT INTO sessions (date, type, description, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (day.isoformat(), kind, f"Window {n} {kind}", status, NOW, NOW),
            )
    conn.commit()


def seed(store: Store) -> None:
    conn = sqlite3.connect(store.path)
    try:
        _seed_history(store, conn)
        _seed_predictions(store)
        _seed_goals(store)
        _seed_strength(store)
        _seed_sessions(conn)
    finally:
        conn.close()


DAILY = ["readiness", "hrv_last_night_ms", "hrv_weekly_avg_ms", "sleep_duration_h", "sleep_score", "resting_hr", "body_battery_high"]
DAILY2 = ["body_battery_low", "stress_avg", "acute_load", "chronic_load", "acwr_ratio", "vo2max_running", "vo2max_cycling"]


def _cases() -> list[tuple[str, Callable[[Store], Any]]]:
    totals = [m for m in trends.METRIC_NAMES if m not in DAILY + DAILY2]
    cases: list[tuple[str, Callable[[Store], Any]]] = []

    def add(name: str, call: Callable[[Store], Any]) -> None:
        cases.append((name, call))

    def tr(today: date = TODAY, **kw: Any) -> Callable[[Store], Any]:
        args = {"start_date": None, "end_date": None, "period": "week", **kw}
        return lambda s: trends.get_trends(s, today, **args)

    add("trends_day_daily", tr(metrics=DAILY, start_date="2026-08-20", end_date="2026-10-06", period="day"))
    add("trends_day_daily2", tr(metrics=DAILY2, start_date="2026-08-20", end_date="2026-10-06", period="day"))
    for i in range(0, len(totals), 8):
        add(f"trends_day_totals_{i}", tr(metrics=totals[i : i + 8], start_date="2026-09-08", end_date="2026-10-06", period="day"))
        add(f"trends_week_totals_{i}", tr(metrics=totals[i : i + 8]))
        add(f"trends_month_totals_{i}", tr(metrics=totals[i : i + 8], start_date="2026-04-01", period="month"))
    add("trends_week_default", tr(metrics=DAILY + DAILY2))
    add("trends_month_long", tr(metrics=DAILY + ["readiness"], start_date="2025-10-08", end_date="2026-10-07", period="month"))
    add("trends_day_default", tr(metrics=["readiness", "running_count"], period="day"))
    add("trends_empty_range", tr(metrics=["readiness", "running_count"], start_date="2026-01-01", end_date="2026-01-31"))
    add("trends_other_today", tr(today=date(2026, 9, 15), metrics=["resting_hr", "endurance_training_load"]))
    for name, kw in (
        ("future", {"end_date": "2026-10-08"}),
        ("order", {"start_date": "2026-10-05", "end_date": "2026-10-01"}),
        ("day_too_long", {"start_date": "2026-07-05", "end_date": "2026-10-05", "period": "day"}),
        ("week_too_long", {"start_date": "2024-10-05", "end_date": "2026-10-05"}),
        ("bad_start", {"start_date": "2026-13-01"}),
        ("bad_end", {"end_date": "yesterday"}),
        ("future_and_bad_start", {"start_date": "x", "end_date": "2026-10-09"}),
    ):
        add(f"trends_error_{name}", tr(metrics=["readiness"], **kw))

    for name, today, week in (
        ("default", TODAY, None),
        ("partial", TODAY, "2026-10-07"),
        ("w36", TODAY, "2026-09-01"),
        ("w27_coverage_starts", TODAY, "2026-07-01"),
        ("w26", TODAY, "2026-06-24"),
        ("w19_first", TODAY, "2026-05-06"),
        ("w18_before", TODAY, "2026-04-28"),
        ("monday_after", date(2026, 10, 12), "2026-10-07"),
        ("w38", TODAY, "2026-09-16"),
        ("error_future", TODAY, "2026-10-12"),
        ("error_bad", TODAY, "2026-02-30"),
    ):
        add(f"weekly_{name}", lambda s, t=today, w=week: trends.get_weekly_report(s, t, week=w))

    def co(lag: int = 0, metrics: list[str] | None = None, **kw: Any) -> Callable[[Store], Any]:
        names = metrics or ["readiness", "hrv_last_night_ms", "resting_hr", "running_distance_km", "endurance_training_load", "sleep_score"]
        args = {"start_date": None, "end_date": None, **kw}
        return lambda s: correlations.get_correlations(s, TODAY, metrics=names, lag_days=lag, **args)

    for lag in range(4):
        add(f"correlations_lag{lag}", co(lag))
    add("correlations_range", co(1, start_date="2026-05-01", end_date="2026-08-31"))
    add("correlations_totals", co(0, ["strength_count", "other_duration_min", "vo2max_running", "acwr_ratio"]))
    add("correlations_short", co(0, ["readiness", "resting_hr"], start_date="2026-09-25", end_date="2026-10-06"))
    add("correlations_error_long", co(0, start_date="2024-01-01", end_date="2026-10-06"))
    add("correlations_error_future", co(0, end_date="2026-10-08"))
    add("correlations_error_order", co(0, start_date="2026-10-06", end_date="2026-10-01"))

    for today in (TODAY, date(2026, 9, 1), date(2026, 10, 20), date(2026, 6, 10)):
        add(f"goals_{today.isoformat()}", lambda s, t=today: goals.get_goal_progress(s, t))

    def sp(**kw: Any) -> Callable[[Store], Any]:
        args: dict[str, Any] = {"exercise": None, "start_date": None, "end_date": None, "plateau_weeks": 4, "limit": 10, **kw}
        return lambda s: strength.get_strength_progress(s, TODAY, **args)

    add("strength_default", sp())
    add("strength_bench", sp(exercise="bench"))
    add("strength_id", sp(exercise="T2"))
    add("strength_limit", sp(limit=1))
    add("strength_plateau2", sp(plateau_weeks=2))
    add("strength_plateau8", sp(plateau_weeks=8, start_date="2026-06-01"))
    add("strength_long", sp(start_date="2024-10-07", end_date="2026-10-06"))
    add("strength_before_coverage", sp(start_date="2026-05-01", end_date="2026-06-30"))
    add("strength_error_long", sp(start_date="2024-10-05", end_date="2026-10-06"))
    add("strength_error_future", sp(end_date="2026-10-08"))
    add("strength_error_bad", sp(start_date="yesterday"))

    def plan(**kw: Any) -> Callable[[Store], Any]:
        return lambda s: tools.get_plan(s, TODAY, **kw)

    add("plan_default", plan())
    add("plan_week", plan(start_date="2026-09-28"))
    add("plan_range_superseded", plan(start_date="2026-08-24", end_date="2026-10-11", include_superseded=True))
    add("plan_end_only", plan(end_date="2026-10-11"))
    add("plan_error_order", plan(start_date="2026-10-05", end_date="2026-10-01"))
    add("plan_error_long", plan(start_date="2026-07-01", end_date="2026-10-01"))
    add("plan_error_bad", plan(start_date="2026-1-1"))
    return cases


def _reconcile(store: Store) -> Any:
    job = HistoryJob(Settings(timezone="Europe/Prague"), store, FakeHistorySource(), now=lambda: datetime(2026, 10, 7, 8, tzinfo=UTC))
    counts = job._reconcile(TODAY)
    return {"counts": list(counts), "plan": tools.get_plan(store, TODAY, start_date="2026-09-28", end_date="2026-10-11")}


def outputs(store: Store) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, call in _cases():
        try:
            result = call(store)
        except ToolError as exc:
            result = {"error": str(exc)}
        out[name] = tools.to_json(result)
    out["reconcile"] = tools.to_json(_reconcile(store))
    return out


def write(path: Path, store: Store) -> None:
    seed(store)
    path.write_text(json.dumps(outputs(store), indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def test_history_readers_return_the_golden_json(store: Store) -> None:
    seed(store)
    got = outputs(store)
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert sorted(got) == sorted(expected)
    differing = [name for name in expected if got[name] != expected[name]]
    assert differing == [], differing
