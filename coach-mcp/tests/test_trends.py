from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

import pytest
from history_helpers import seed_activities, seed_day, sport
from mcp.server.mcpserver.exceptions import ToolError

from conftest import TODAY
from coach_mcp import trends
from coach_mcp.trends import DAILY_METRICS, METRIC_NAMES, TOTAL_METRICS


def get_trends(store, today=TODAY, *, metrics, start_date=None, end_date=None, period="week"):
    return trends.get_trends(
        store, today, metrics=metrics, start_date=start_date, end_date=end_date, period=period
    )


def d(text: str) -> date:
    return date.fromisoformat(text)


def span(first: str, last: str) -> list[date]:
    return [d(first) + timedelta(days=n) for n in range((d(last) - d(first)).days + 1)]


def test_metric_catalogue():
    assert len(DAILY_METRICS) == 14 and len(TOTAL_METRICS) == 32 and len(METRIC_NAMES) == 46
    assert len(set(METRIC_NAMES)) == 46
    assert "running_distance_km" in TOTAL_METRICS and "endurance_training_load" in TOTAL_METRICS
    assert "strength_count" in TOTAL_METRICS and "other_duration_min" in TOTAL_METRICS


# ------------------------------------------------------------ get_trends


def test_trends_day_aggregates(store):
    for day, value in zip(span("2026-10-01", "2026-10-05"), [50, 52, None, 54, 60], strict=True):
        seed_day(store, day, hrv_last_night_ms=value, readiness=70)
    out = get_trends(
        store, metrics=["hrv_last_night_ms"], start_date="2026-10-01", end_date="2026-10-05", period="day"
    )
    assert out["start"] == "2026-10-01" and out["end"] == "2026-10-05" and out["period"] == "day"
    assert out["days_with_data"] == 5  # the day without an HRV value still has a stored row
    assert out["metrics"] == {
        "hrv_last_night_ms": {
            "unit": "ms",
            "kind": "daily",
            "summary": {"n": 4, "mean": 54.0, "min": 50, "max": 60, "first": 50, "last": 60, "change": 10, "change_pct": 20.0},
            "buckets": [
                {"period": "2026-10-01", "value": 50},
                {"period": "2026-10-02", "value": 52},
                {"period": "2026-10-04", "value": 54},  # 10-03 has no value: no bucket
                {"period": "2026-10-05", "value": 60},
            ],
        }
    }


def test_trends_week_aggregates_with_partial_edge_weeks(store):
    today = date(2026, 10, 15)
    for n, day in enumerate(span("2026-09-30", "2026-10-14")):
        seed_day(store, day, resting_hr=40 + n)
    out = get_trends(store, today, metrics=["resting_hr"], start_date="2026-09-30", end_date="2026-10-14")
    assert out["period"] == "week" and out["days_with_data"] == 15
    metric = out["metrics"]["resting_hr"]
    assert metric["unit"] == "bpm" and metric["kind"] == "daily"
    assert metric["buckets"] == [
        {"period": "2026-W40", "start": "2026-09-30", "end": "2026-10-04", "n": 5, "mean": 42.0, "min": 40, "max": 44},
        {"period": "2026-W41", "start": "2026-10-05", "end": "2026-10-11", "n": 7, "mean": 48.0, "min": 45, "max": 51},
        {"period": "2026-W42", "start": "2026-10-12", "end": "2026-10-14", "n": 3, "mean": 53.0, "min": 52, "max": 54},
    ]
    # the summary covers every daily value; change compares the first and the last period mean
    assert metric["summary"] == {
        "n": 15, "mean": 47.0, "min": 40, "max": 54, "first": 42.0, "last": 53.0, "change": 11.0, "change_pct": 26.2,
    }


def test_trends_month_aggregates_over_120_days(store):
    days = span("2026-06-09", "2026-10-06")
    assert len(days) == 120
    for day in days:
        seed_day(store, day, stress_avg=day.day)
        seed_activities(store, day, {"running": sport(1, 30, 5.0, 40)})
    out = get_trends(
        store,
        metrics=["stress_avg", "running_duration_min", "running_count"],
        start_date="2026-06-09",
        end_date="2026-10-06",
        period="month",
    )
    assert out["days_with_data"] == 120
    stress = out["metrics"]["stress_avg"]
    assert [(b["period"], b["start"], b["end"], b["n"], b["mean"], b["min"], b["max"]) for b in stress["buckets"]] == [
        ("2026-06", "2026-06-09", "2026-06-30", 22, 19.5, 9, 30),
        ("2026-07", "2026-07-01", "2026-07-31", 31, 16.0, 1, 31),
        ("2026-08", "2026-08-01", "2026-08-31", 31, 16.0, 1, 31),
        ("2026-09", "2026-09-01", "2026-09-30", 30, 15.5, 1, 30),
        ("2026-10", "2026-10-01", "2026-10-06", 6, 3.5, 1, 6),
    ]
    assert all("total" not in b for b in stress["buckets"])  # daily metrics have no total
    assert stress["summary"]["n"] == 120
    assert stress["summary"]["first"] == 19.5 and stress["summary"]["last"] == 3.5
    assert stress["summary"]["change"] == -16.0 and stress["summary"]["change_pct"] == -82.1
    duration = out["metrics"]["running_duration_min"]
    assert duration["kind"] == "total" and duration["unit"] == "min"
    assert [(b["period"], b["n"], b["total"]) for b in duration["buckets"]] == [
        ("2026-06", 22, 660.0), ("2026-07", 31, 930.0), ("2026-08", 31, 930.0), ("2026-09", 30, 900.0),
        ("2026-10", 6, 180.0),
    ]
    assert out["metrics"]["running_count"]["buckets"][1]["total"] == 31  # a count is an integer


def test_trends_total_metrics_sum_activities_and_zero_on_fetched_empty_days(store):
    seed_activities(store, d("2026-10-01"), {"running": sport(1, 40, 8.0, 50), "cycling": sport(1, 60, 20.0, 70)})
    seed_activities(store, d("2026-10-02"), {})  # fetched, nothing happened: a real zero
    seed_day(store, d("2026-10-03"), readiness=60)  # a row whose activities were never fetched
    seed_activities(store, d("2026-10-04"), {"strength": sport(1, 45, 0, 30), "running": sport(1, 20, 3.0, 15)})
    out = get_trends(
        store,
        metrics=["endurance_duration_min", "strength_count", "endurance_count", "cycling_distance_km", "walking_count"],
        start_date="2026-10-01",
        end_date="2026-10-04",
        period="day",
    )
    values = {name: [(b["period"][-2:], b["value"]) for b in m["buckets"]] for name, m in out["metrics"].items()}
    assert values["endurance_duration_min"] == [("01", 100.0), ("02", 0.0), ("04", 20.0)]  # strength is not endurance
    assert values["strength_count"] == [("01", 0), ("02", 0), ("04", 1)]
    assert values["endurance_count"] == [("01", 2), ("02", 0), ("04", 1)]
    assert values["cycling_distance_km"] == [("01", 20.0), ("02", 0.0), ("04", 0.0)]
    assert values["walking_count"] == [("01", 0), ("02", 0), ("04", 0)]
    assert out["days_with_data"] == 4  # three days with activities fetched, one with recovery only
    assert out["metrics"]["endurance_duration_min"]["summary"]["n"] == 3  # the never-fetched day is not a zero
    assert out["metrics"]["endurance_duration_min"]["summary"]["mean"] == pytest.approx(40.0)


def test_trends_skip_days_without_value_and_omit_empty_buckets(store):
    for day in span("2026-09-14", "2026-09-20"):  # W38
        seed_day(store, day, readiness=60)
    for day in span("2026-09-28", "2026-10-04"):  # W40; W39 has no value at all
        seed_day(store, day, readiness=70)
    out = get_trends(store, metrics=["readiness", "stress_avg"], start_date="2026-09-14", end_date="2026-10-04")
    readiness = out["metrics"]["readiness"]
    assert [b["period"] for b in readiness["buckets"]] == ["2026-W38", "2026-W40"]
    assert readiness["summary"]["first"] == 60.0 and readiness["summary"]["last"] == 70.0
    assert readiness["summary"]["n"] == 14
    assert out["metrics"]["stress_avg"] == {"unit": "level", "kind": "daily", "summary": {"n": 0}, "buckets": []}


def test_trends_change_and_change_pct(store):
    # a total metric: week 1 is a real zero, week 2 has 3 km; week 3 has 1.5 km
    for day in span("2026-09-14", "2026-09-20"):
        seed_activities(store, day, {})
    seed_activities(store, d("2026-09-21"), {"running": sport(1, 20, 3.0, 20)})
    for day in span("2026-09-22", "2026-09-27"):
        seed_activities(store, day, {})
    seed_activities(store, d("2026-09-28"), {"running": sport(1, 10, 1.5, 10)})
    out = get_trends(store, metrics=["running_distance_km"], start_date="2026-09-14", end_date="2026-09-28")
    summary = out["metrics"]["running_distance_km"]["summary"]
    assert [b["total"] for b in out["metrics"]["running_distance_km"]["buckets"]] == [0.0, 3.0, 1.5]
    assert summary["first"] == 0.0 and summary["last"] == 1.5
    assert summary["change"] == 1.5 and summary["change_pct"] is None  # first bucket 0: no percentage
    # a falling series
    for day, value in zip(span("2026-10-01", "2026-10-02"), [80, 60], strict=True):
        seed_day(store, day, hrv_last_night_ms=value)
    out = get_trends(
        store, metrics=["hrv_last_night_ms"], start_date="2026-10-01", end_date="2026-10-02", period="day"
    )
    summary = out["metrics"]["hrv_last_night_ms"]["summary"]
    assert (summary["change"], summary["change_pct"]) == (-20, -25.0)


def test_trends_single_bucket_has_no_change(store):
    for n, day in enumerate(span("2026-10-05", "2026-10-11")):
        seed_day(store, day, resting_hr=50 + n)
    out = get_trends(store, date(2026, 10, 15), metrics=["resting_hr"], start_date="2026-10-05", end_date="2026-10-11")
    summary = out["metrics"]["resting_hr"]["summary"]
    assert len(out["metrics"]["resting_hr"]["buckets"]) == 1
    assert summary == {
        "n": 7, "mean": 53.0, "min": 50, "max": 56, "first": None, "last": None, "change": None, "change_pct": None,
    }


def test_trends_defaults_last_90_days_ending_yesterday(store):
    out = get_trends(store, metrics=["readiness"])
    assert (out["start"], out["end"], out["period"]) == ("2026-07-09", "2026-10-06", "week")
    assert (d(out["end"]) - d(out["start"])).days + 1 == 90
    out = get_trends(store, metrics=["readiness"], end_date="2026-09-30")
    assert (out["start"], out["end"]) == ("2026-07-03", "2026-09-30")
    out = get_trends(store, metrics=["readiness"], end_date="2026-10-07")  # today itself is allowed
    assert out["end"] == "2026-10-07"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"end_date": "2026-10-08"}, "end_date must not be after today"),
        ({"start_date": "2026-10-03", "end_date": "2026-10-02"}, "start_date must be on or before end_date"),
        ({"start_date": "2026-07-05", "end_date": "2026-10-05", "period": "day"}, "date range too long for period day (max 92 days)"),
        ({"start_date": "2024-10-05", "end_date": "2026-10-06", "period": "week"}, "date range too long for period week (max 731 days)"),
        ({"start_date": "2024-10-05", "end_date": "2026-10-06", "period": "month"}, "date range too long for period month (max 731 days)"),
        ({"end_date": "2026-13-01"}, "end_date is not a valid date"),
        ({"start_date": "yesterday"}, "start_date must be YYYY-MM-DD"),
    ],
)
def test_trends_argument_errors(store, arguments, message):
    with pytest.raises(ToolError, match=re.escape(message)):
        get_trends(store, metrics=["readiness"], **arguments)


def test_trends_range_limits_accept_the_boundary(store):
    day = get_trends(store, metrics=["readiness"], start_date="2026-07-07", end_date="2026-10-06", period="day")
    assert (d(day["end"]) - d(day["start"])).days + 1 == 92
    for period in ("week", "month"):
        out = get_trends(store, metrics=["readiness"], start_date="2024-10-06", end_date="2026-10-06", period=period)
        assert (d(out["end"]) - d(out["start"])).days + 1 == 731


def test_trends_empty_store_returns_n_zero(store):
    out = get_trends(store, metrics=["hrv_last_night_ms", "running_distance_km"])
    assert out["days_with_data"] == 0
    for name, kind in (("hrv_last_night_ms", "daily"), ("running_distance_km", "total")):
        assert out["metrics"][name]["kind"] == kind
        assert out["metrics"][name]["summary"] == {"n": 0} and out["metrics"][name]["buckets"] == []


def test_trends_dedupes_metrics(store):
    out = get_trends(store, metrics=["readiness", "hrv_last_night_ms", "readiness"])
    assert list(out["metrics"]) == ["readiness", "hrv_last_night_ms"]


def test_trends_invalid_activities_json_is_skipped(store):
    seed_activities(store, d("2026-10-01"), {"running": sport(1, 30, 5.0, 40)})
    for day, text in ((d("2026-10-02"), "not json"), (d("2026-10-03"), "[1, 2]"), (d("2026-10-04"), '"x"')):
        store.upsert_history_activities({day: text}, TODAY)
    store.upsert_history_activities({d("2026-10-05"): '{"running": 7}'}, TODAY)  # a sport entry of the wrong type
    store.upsert_history_activities({d("2026-10-06"): '{"running": {"duration_min": true}}'}, TODAY)  # not a number
    out = get_trends(
        store, metrics=["running_duration_min"], start_date="2026-10-01", end_date="2026-10-06", period="day"
    )
    assert [(b["period"][-2:], b["value"]) for b in out["metrics"]["running_duration_min"]["buckets"]] == [
        ("01", 30.0),
        ("05", 0.0),
        ("06", 0.0),
    ]


def test_trends_only_include_rows_inside_the_range(store):
    seed_day(store, d("2026-09-30"), readiness=10)
    seed_day(store, d("2026-10-01"), readiness=60)
    seed_day(store, d("2026-10-03"), readiness=90)
    out = get_trends(store, metrics=["readiness"], start_date="2026-10-01", end_date="2026-10-02", period="day")
    assert out["days_with_data"] == 1
    assert [b["value"] for b in out["metrics"]["readiness"]["buckets"]] == [60]


# ---------------------------------------------------------- weekly report

# TODAY (Wed 2026-10-07) is in ISO week 41, so the default report week is W40 (Mon 09-28 .. Sun 10-04) and
# its four baseline weeks are W36 .. W39 (Mondays 08-31, 09-07, 09-14, 09-21).
W36, W37, W38, W39, W40, W41 = (d("2026-08-31") + timedelta(days=7 * n) for n in range(6))
BASELINES = [W36, W37, W38, W39]
BASELINE_LABELS = ["2026-W36", "2026-W37", "2026-W38", "2026-W39"]
STRENGTH_OFF = {"available": False, "reason": "strength history from Hevy is not stored yet; read Hevy directly"}


def report(store, today=TODAY, week=None):
    return trends.get_weekly_report(store, today, week=week)


def seed_week(store, monday, *, run=None, ride=None, strength=None, walk=None, days=7):
    """Activities of the first ``days`` days of a week are fetched ('{}' when nothing was recorded).

    run / ride / strength / walk are (duration_min, distance_km, training_load) on Tue / Thu / Fri / Sat.
    """
    placed = {1: ("running", run), 3: ("cycling", ride), 4: ("strength", strength), 5: ("walking", walk)}
    for n in range(days):
        name, values = placed.get(n, (None, None))
        seed_activities(store, monday + timedelta(days=n), {name: sport(1, *values)} if values else {})


def seed_daily(store, monday, *, days=7, **columns):
    for n in range(days):
        seed_day(store, monday + timedelta(days=n), **columns)


def flags_of(out):
    return [f["flag"] for f in out["flags"]]


def test_weekly_report_every_section_from_seeded_db(store):
    runs = {W36: (50, 10.0, 60), W37: (60, 12.0, 70), W38: (70, 14.0, 80), W39: (70, 14.0, 80)}
    rides = {W36: (50, 20.0, 40), W37: (60, 24.0, 50), W38: (70, 28.0, 60), W39: (70, 28.0, 60)}
    for monday in BASELINES:
        seed_week(store, monday, run=runs[monday], ride=rides[monday])
    seed_week(store, W40, run=(80, 16.0, 100), ride=(80, 32.0, 80), strength=(45, 0, 30), walk=(30, 2.5, 5))
    # sleep: weekly means 7.0 / 7.5 / 8.0 / 7.5 (baseline 7.5), score 80 every day
    for monday, hours in zip(BASELINES, (7.0, 7.5, 8.0, 7.5), strict=True):
        seed_daily(store, monday, sleep_duration_h=hours, sleep_score=80, hrv_last_night_ms=60, resting_hr=48)
    seed_daily(store, W40, days=5, sleep_duration_h=6.5, sleep_score=70)  # the last two nights are missing
    for n in range(7):
        day = W40 + timedelta(days=n)
        seed_day(store, day, hrv_last_night_ms=48 if n == 6 else 57, hrv_status="LOW" if n == 6 else "BALANCED")
    seed_daily(store, W40, days=4, resting_hr=52)
    seed_day(store, d("2026-10-02"), acute_load=410, chronic_load=375, acwr_ratio=1.09)
    seed_day(store, d("2026-10-03"), acute_load=420, chronic_load=380, acwr_ratio=1.11)  # the latest with a load
    # sessions: W40 two done, one skipped, one planned (overdue); W39 two done; W38 one done, one skipped
    ids = store.replace_planned(
        W40, W40 + timedelta(days=6), W40,
        [{"date": W40 + timedelta(days=n), "type": "running", "description": "x"} for n in (1, 2, 3, 4)],
    )[1]
    for session_id, status in zip(ids, ("done", "done", "skipped"), strict=False):
        store.update_session_status(session_id, status, None, None)
    for monday, statuses in ((W39, ("done", "done")), (W38, ("done", "skipped"))):
        week_ids = store.replace_planned(
            monday, monday + timedelta(days=6), monday,
            [{"date": monday + timedelta(days=n), "type": "running", "description": "x"} for n in range(len(statuses))],
        )[1]
        for session_id, status in zip(week_ids, statuses, strict=True):
            store.update_session_status(session_id, status, None, None)

    out = report(store)
    assert out["week"] == {"start": "2026-09-28", "end": "2026-10-04", "iso_week": "2026-W40", "partial": False}
    assert out["baseline_weeks"] == BASELINE_LABELS
    assert out["strength"] == STRENGTH_OFF
    assert out["endurance"] == {
        "activity_days": 7,
        "total": {
            "count": 3,
            "duration_min": {"week": 190.0, "baseline": 125.0, "baseline_weeks": 4, "change": 65.0, "change_pct": 52.0},
            "distance_km": {"week": 50.5, "baseline": 37.5, "baseline_weeks": 4, "change": 13.0, "change_pct": 34.7},
            "training_load": {"week": 185.0, "baseline": 125.0, "baseline_weeks": 4, "change": 60.0, "change_pct": 48.0},
        },
        "by_sport": {
            "running": {"count": 1, "duration_min": 80.0, "distance_km": 16.0, "training_load": 100.0,
                        "baseline_duration_min": 62.5, "baseline_training_load": 72.5},
            "cycling": {"count": 1, "duration_min": 80.0, "distance_km": 32.0, "training_load": 80.0,
                        "baseline_duration_min": 62.5, "baseline_training_load": 52.5},
            "walking": {"count": 1, "duration_min": 30.0, "distance_km": 2.5, "training_load": 5.0,
                        "baseline_duration_min": 0.0, "baseline_training_load": 0.0},
        },
        "garmin_strength": {"count": 1, "duration_min": 45.0, "training_load": 30.0},
        "load": {"date": "2026-10-03", "acute": 420.0, "chronic": 380.0, "acwr_ratio": 1.11},
    }
    assert out["sleep"] == {
        "days": 5,
        "duration_h": {"week": 6.5, "baseline": 7.5, "baseline_weeks": 4, "change": -1.0, "change_pct": -13.3},
        "score": {"week": 70.0, "baseline": 80.0, "baseline_weeks": 4, "change": -10.0, "change_pct": -12.5},
    }
    assert out["hrv"] == {
        "days": 7,
        "last_night_ms": {"week": 55.7, "baseline": 60.0, "baseline_weeks": 4, "change": -4.3, "change_pct": -7.1},
        "min_ms": 48.0,
        "status": "LOW",
    }
    assert out["resting_hr"] == {
        "days": 4,
        "bpm": {"week": 52.0, "baseline": 48.0, "baseline_weeks": 4, "change": 4.0, "change_pct": 8.3},
    }
    assert out["adherence"] == {
        "planned": 1, "done": 2, "skipped": 1, "moved": 0, "overdue_planned": 1, "completion_rate": 0.67,
        "baseline_completion_rate": 0.75, "baseline_weeks": 2,
    }
    assert out["new_bests"] == [
        {"metric": "running_distance_km", "value": 16.0, "previous_best": 14.0, "weeks_compared": 4},
        {"metric": "cycling_distance_km", "value": 32.0, "previous_best": 28.0, "weeks_compared": 4},
        {"metric": "endurance_duration_min", "value": 190.0, "previous_best": 140.0, "weeks_compared": 4},
        {"metric": "endurance_training_load", "value": 185.0, "previous_best": 140.0, "weeks_compared": 4},
    ]
    assert out["flags"] == [
        {"flag": "endurance_load_up", "week": 185.0, "baseline": 125.0, "change": 60.0, "change_pct": 48.0},
        {"flag": "endurance_time_up", "week": 190.0, "baseline": 125.0, "change": 65.0, "change_pct": 52.0},
        {"flag": "sleep_down", "week": 6.5, "baseline": 7.5, "change": -1.0, "change_pct": -13.3},
        {"flag": "hrv_down", "week": 55.7, "baseline": 60.0, "change": -4.3, "change_pct": -7.1},
    ]  # resting HR is +4 bpm: below its threshold of 5


def test_weekly_report_lists_a_sport_that_only_the_baseline_has(store):
    for monday in BASELINES:
        for n in range(7):
            seed_activities(store, monday + timedelta(days=n), {"swimming": sport(1, 40, 1.5, 20)} if n == 2 else {})
    seed_week(store, W40, run=(50, 8.0, 60))
    by_sport = report(store)["endurance"]["by_sport"]
    assert set(by_sport) == {"running", "swimming"}  # hiking, walking, cycling and other have neither
    assert by_sport["swimming"] == {
        "count": 0, "duration_min": 0.0, "distance_km": 0.0, "training_load": 0.0,
        "baseline_duration_min": 40.0, "baseline_training_load": 20.0,
    }
    assert by_sport["running"]["baseline_duration_min"] == 0.0 and by_sport["running"]["count"] == 1


def test_weekly_baseline_of_zero_gives_no_percentage_and_no_flag(store):
    for monday in BASELINES:
        seed_week(store, monday)  # complete weeks with nothing recorded
    seed_week(store, W40, run=(60, 10.0, 100))
    out = report(store)
    assert out["endurance"]["total"]["training_load"] == {
        "week": 100.0, "baseline": 0.0, "baseline_weeks": 4, "change": 100.0, "change_pct": None,
    }
    assert out["flags"] == []  # a rise from nothing is not "more than 30 %"
    assert [b["metric"] for b in out["new_bests"]] == [
        "running_distance_km", "endurance_duration_min", "endurance_training_load",
    ]


def test_sleep_days_count_the_nights_with_a_duration(store):
    seed_daily(store, W40, days=7, sleep_score=75)
    seed_daily(store, W40, days=4, sleep_duration_h=7.0)
    out = report(store)["sleep"]
    assert out["days"] == 4
    assert out["duration_h"]["week"] == 7.0 and out["score"]["week"] == 75.0
    seed_day(store, W36, sleep_score=70)  # a baseline week with a score only
    assert report(store)["sleep"]["score"]["baseline_weeks"] == 1
    assert report(store)["sleep"]["duration_h"]["baseline_weeks"] == 0


def test_a_week_with_only_a_score_has_no_duration_stat(store):
    seed_daily(store, W40, days=3, sleep_score=75)
    out = report(store)["sleep"]
    assert out["days"] == 0 and out["duration_h"] is None
    assert out["score"]["week"] == 75.0


def test_new_bests_weeks_compared_counts_the_earlier_complete_weeks(store):
    first = W36 - timedelta(days=14)  # two more weeks before W36
    for monday in (first, first + timedelta(days=7), *BASELINES):
        seed_week(store, monday, run=(60, 10.0, 100), days=6 if monday == W38 else 7)
    seed_week(store, W40, run=(70, 12.0, 120))
    bests = {b["metric"]: b for b in report(store)["new_bests"]}
    assert bests["running_distance_km"]["weeks_compared"] == 5  # six earlier weeks, W38 is incomplete
    assert bests["running_distance_km"]["previous_best"] == 10.0


def test_garmin_strength_and_other_sports_are_kept_apart(store):
    for n in range(7):
        totals = {"other": sport(1, 30, 0, 10)} if n == 1 else {}
        seed_activities(store, W40 + timedelta(days=n), totals)
    out = report(store)["endurance"]
    assert "garmin_strength" not in out  # "other" is endurance, only strength_training is Garmin strength
    assert out["total"]["count"] == 1 and set(out["by_sport"]) == {"other"}
    seed_activities(store, W40 + timedelta(days=2), {"strength": sport(1, 45, 0, 30)})
    out = report(store)["endurance"]
    assert out["garmin_strength"] == {"count": 1, "duration_min": 45.0, "training_load": 30.0}
    assert out["total"]["count"] == 1  # strength is not endurance


def test_weekly_report_marks_sections_without_data(store):
    assert report(store) == {
        "week": {"start": "2026-09-28", "end": "2026-10-04", "iso_week": "2026-W40", "partial": False},
        "baseline_weeks": BASELINE_LABELS,
        "endurance": {"available": False},
        "strength": STRENGTH_OFF,
        "sleep": {"available": False},
        "hrv": {"available": False},
        "resting_hr": {"available": False},
        "adherence": {"available": False},
        "new_bests": [],
        "flags": [],
    }


def test_weekly_report_sections_are_independent(store):
    seed_week(store, W40, run=(40, 7.0, 50))  # activities only
    out = report(store)
    assert out["endurance"]["activity_days"] == 7 and out["endurance"]["total"]["count"] == 1
    assert out["endurance"]["total"]["duration_min"] == {
        "week": 40.0, "baseline": None, "baseline_weeks": 0, "change": None, "change_pct": None,
    }
    assert out["endurance"]["by_sport"]["running"]["baseline_duration_min"] is None
    assert "garmin_strength" not in out["endurance"] and "load" not in out["endurance"]
    for key in ("sleep", "hrv", "resting_hr", "adherence"):
        assert out[key] == {"available": False}
    assert out["flags"] == [] and out["new_bests"] == []


@pytest.mark.parametrize(
    ("today", "iso_week", "start"),
    [
        (d("2026-10-12"), "2026-W41", "2026-10-05"),  # Monday: the week that just ended
        (d("2026-10-07"), "2026-W40", "2026-09-28"),
        (d("2026-10-11"), "2026-W40", "2026-09-28"),  # Sunday: the current week is not complete yet
    ],
)
def test_weekly_report_default_is_last_complete_week(store, today, iso_week, start):
    out = report(store, today)
    assert out["week"]["iso_week"] == iso_week and out["week"]["start"] == start
    assert out["week"]["partial"] is False
    assert out["baseline_weeks"] == [
        f"2026-W{int(iso_week[-2:]) - n:02d}" for n in (4, 3, 2, 1)
    ]


def test_weekly_report_explicit_week_and_future_week(store):
    out = report(store, week="2026-09-16")  # any date in the week
    assert (out["week"]["iso_week"], out["week"]["start"], out["week"]["end"]) == ("2026-W38", "2026-09-14", "2026-09-20")
    out = report(store, week="2026-10-11")  # Sunday of the running week
    assert out["week"]["iso_week"] == "2026-W41" and out["week"]["partial"] is True
    assert report(store, week="2026-10-05")["week"]["partial"] is True
    with pytest.raises(ToolError, match="week must not be in the future"):
        report(store, week="2026-10-12")
    with pytest.raises(ToolError, match="week is not a valid date"):
        report(store, week="2026-02-30")


def test_weekly_report_future_week_is_an_error(store):
    with pytest.raises(ToolError, match="week must not be in the future"):
        report(store, week="2026-10-19")


def test_weekly_report_partial_current_week_has_no_load_flags(store):
    for monday in (W37, W38, W39, W40):
        seed_week(store, monday, run=(60, 10.0, 100))
    seed_week(store, W41, run=(60, 10.0, 200))  # every day fetched, load +100 %
    running = report(store, today=d("2026-10-08"), week="2026-10-08")  # Thursday: the week is not over
    assert running["week"]["partial"] is True
    assert running["endurance"]["total"]["training_load"]["change_pct"] == 100.0  # still reported ...
    assert running["flags"] == [] and running["new_bests"] == []  # ... but no flag and no record yet
    over = report(store, today=d("2026-10-12"), week="2026-10-08")
    assert over["week"]["partial"] is False
    assert flags_of(over) == ["endurance_load_up"]
    assert over["new_bests"] == [
        {"metric": "endurance_training_load", "value": 200.0, "previous_best": 100.0, "weeks_compared": 4}
    ]


def test_weekly_baseline_uses_available_weeks_and_counts_them(store):
    seed_daily(store, W38, days=3, resting_hr=50)  # a week with a few values still counts
    seed_daily(store, W39, resting_hr=46)
    seed_daily(store, W40, resting_hr=48)
    out = report(store)["resting_hr"]
    assert out == {
        "days": 7,
        "bpm": {"week": 48.0, "baseline": 48.0, "baseline_weeks": 2, "change": 0.0, "change_pct": 0.0},
    }
    seed_daily(store, W36, resting_hr=44)
    seed_daily(store, W37, resting_hr=44)
    out = report(store)["resting_hr"]["bpm"]
    assert (out["baseline"], out["baseline_weeks"]) == (46.0, 4)


def test_activity_baseline_uses_only_complete_weeks(store):
    seed_week(store, W36, run=(60, 10.0, 60))
    seed_week(store, W37, run=(60, 10.0, 100))
    seed_week(store, W38, run=(60, 10.0, 500), days=6)  # one day was never fetched: not a weekly total
    seed_week(store, W39, run=(60, 10.0, 140))
    seed_week(store, W40, run=(60, 10.0, 120))
    out = report(store)["endurance"]
    assert out["total"]["training_load"] == {
        "week": 120.0, "baseline": 100.0, "baseline_weeks": 3, "change": 20.0, "change_pct": 20.0,
    }
    assert out["by_sport"]["running"]["baseline_training_load"] == 100.0
    assert out["by_sport"]["running"]["baseline_duration_min"] == 60.0


def test_activity_baseline_is_none_without_a_complete_baseline_week(store):
    for monday in BASELINES:
        seed_week(store, monday, run=(60, 10.0, 100), days=6)
    seed_week(store, W40, run=(60, 10.0, 120))
    out = report(store)["endurance"]
    assert out["total"]["training_load"]["baseline"] is None and out["total"]["training_load"]["baseline_weeks"] == 0
    assert out["by_sport"]["running"]["baseline_training_load"] is None


def test_incomplete_report_week_is_reported_but_never_flagged_or_a_record(store):
    for monday in BASELINES:
        seed_week(store, monday, run=(60, 10.0, 100))
    seed_week(store, W40, run=(120, 20.0, 300), days=6)
    out = report(store)
    assert out["endurance"]["activity_days"] == 6
    assert out["endurance"]["total"]["training_load"]["change_pct"] == 200.0
    assert out["flags"] == [] and out["new_bests"] == []


@pytest.mark.parametrize(
    ("load", "expected"),
    [(130, []), (131, ["endurance_load_up"]), (70, []), (69, ["endurance_load_down"]), (100, [])],
)
def test_weekly_flags_thresholds_load(store, load, expected):
    for monday in BASELINES:
        seed_week(store, monday, run=(60, 10.0, 100))
    seed_week(store, W40, run=(60, 10.0, load))
    assert flags_of(report(store)) == expected


@pytest.mark.parametrize(("duration", "expected"), [(78, []), (79, ["endurance_time_up"]), (30, [])])
def test_weekly_flags_thresholds_time(store, duration, expected):
    for monday in BASELINES:
        seed_week(store, monday, run=(60, 10.0, 100))
    seed_week(store, W40, run=(duration, 10.0, 100))
    assert flags_of(report(store)) == expected


def test_weekly_load_flags_need_two_complete_baseline_weeks(store):
    for monday in (W36, W37, W38):
        seed_week(store, monday, run=(60, 10.0, 100), days=6)  # incomplete: not counted
    seed_week(store, W39, run=(60, 10.0, 100))
    seed_week(store, W40, run=(120, 20.0, 300))
    out = report(store)
    assert out["endurance"]["total"]["training_load"]["baseline_weeks"] == 1
    assert out["flags"] == []
    seed_week(store, W38, run=(60, 10.0, 100))  # a second complete baseline week
    assert flags_of(report(store)) == ["endurance_load_up", "endurance_time_up"]


@pytest.mark.parametrize(
    ("column", "flag", "baseline", "week_value", "week_days", "baseline_weeks", "expected"),
    [
        ("sleep_duration_h", "sleep_down", 7.5, 7.0, 4, 4, True),  # -0.5 h
        ("sleep_duration_h", "sleep_down", 7.5, 7.1, 4, 4, False),  # -0.4 h
        ("sleep_duration_h", "sleep_down", 7.5, 6.0, 3, 4, False),  # only 3 nights
        ("sleep_duration_h", "sleep_down", 7.5, 6.0, 7, 1, False),  # a single baseline week
        ("sleep_duration_h", "sleep_down", 7.5, 8.5, 7, 4, False),  # up is not a flag
        ("hrv_last_night_ms", "hrv_down", 100.0, 95.0, 4, 4, True),  # -5 %
        ("hrv_last_night_ms", "hrv_down", 100.0, 95.1, 4, 4, False),  # -4.9 %
        ("hrv_last_night_ms", "hrv_down", 100.0, 80.0, 3, 4, False),
        ("hrv_last_night_ms", "hrv_down", 100.0, 80.0, 7, 1, False),
        ("resting_hr", "resting_hr_up", 48.0, 53.0, 4, 4, True),  # +5 bpm
        ("resting_hr", "resting_hr_up", 48.0, 52.9, 4, 4, False),  # +4.9 bpm
        ("resting_hr", "resting_hr_up", 48.0, 60.0, 3, 4, False),
        ("resting_hr", "resting_hr_up", 48.0, 60.0, 7, 1, False),
        ("resting_hr", "resting_hr_up", 48.0, 40.0, 7, 4, False),  # down is not a flag
    ],
)
def test_weekly_flags_thresholds(store, column, flag, baseline, week_value, week_days, baseline_weeks, expected):
    for monday in BASELINES[4 - baseline_weeks :]:
        seed_daily(store, monday, **{column: baseline})
    seed_daily(store, W40, days=week_days, **{column: week_value})
    out = report(store)
    assert (flag in flags_of(out)) is expected
    assert len(out["flags"]) == int(expected)


def test_new_bests_vo2max_and_weekly_totals(store):
    runs = {W36: (50, 10.0, 60), W37: (60, 12.0, 70), W38: (70, 14.0, 80), W39: (70, 14.0, 80)}
    for monday in BASELINES:
        seed_week(store, monday, run=runs[monday])
        seed_daily(store, monday, vo2max_running=50.0, vo2max_cycling=50.0)  # 28 earlier days with a value
    seed_week(store, W40, run=(80, 16.0, 100), ride=(30, 5.0, 10))
    seed_daily(store, W40, vo2max_running=51.0, vo2max_cycling=49.0)
    seed_day(store, d("2026-10-02"), vo2max_running=51.5)
    assert report(store)["new_bests"] == [
        {"metric": "vo2max_running", "value": 51.5, "previous_best": 50.0},
        {"metric": "running_distance_km", "value": 16.0, "previous_best": 14.0, "weeks_compared": 4},
        {"metric": "cycling_distance_km", "value": 5.0, "previous_best": 0.0, "weeks_compared": 4},
        {"metric": "endurance_duration_min", "value": 110.0, "previous_best": 70.0, "weeks_compared": 4},
        {"metric": "endurance_training_load", "value": 110.0, "previous_best": 80.0, "weeks_compared": 4},
    ]


def test_new_bests_must_exceed_the_earlier_best(store):
    for monday in BASELINES:
        seed_week(store, monday, run=(70, 14.0, 80))
        seed_daily(store, monday, vo2max_running=50.0)
    seed_week(store, W40, run=(70, 14.0, 80))  # equal: not a record
    seed_daily(store, W40, vo2max_running=50.0)
    assert report(store)["new_bests"] == []
    seed_week(store, W40, run=(70, 14.0, 0))  # the same distance with no load: still none
    seed_week(store, W40, run=(0, 0.0, 0))  # a week without any distance never sets a record
    assert report(store)["new_bests"] == []


def test_new_bests_need_enough_history(store):
    for monday in (W37, W38, W39):  # only three earlier complete weeks
        seed_week(store, monday, run=(50, 10.0, 60))
    seed_week(store, W40, run=(90, 18.0, 120))
    for n in range(27):  # only 27 earlier days with a VO2max value
        seed_day(store, d("2026-09-01") + timedelta(days=n), vo2max_running=50.0)
    seed_day(store, d("2026-10-01"), vo2max_running=55.0)
    assert report(store)["new_bests"] == []
    seed_week(store, W36, run=(50, 10.0, 60))  # the fourth earlier complete week
    assert [b["metric"] for b in report(store)["new_bests"]] == [
        "running_distance_km", "endurance_duration_min", "endurance_training_load",
    ]
    seed_day(store, d("2026-09-28") - timedelta(days=28), vo2max_running=50.0)  # the 28th earlier day
    assert report(store)["new_bests"][0] == {"metric": "vo2max_running", "value": 55.0, "previous_best": 50.0}


def test_weekly_adherence_from_sessions(store):
    def session(day: date) -> dict[str, Any]:
        return {"date": day, "type": "running", "description": "x"}

    ids = store.replace_planned(
        W40, W40 + timedelta(days=6), W40, [session(W40 + timedelta(days=n)) for n in range(5)]
    )[1]
    for session_id, status, new_date in (
        (ids[0], "done", None), (ids[1], "done", None), (ids[2], "skipped", None), (ids[3], "moved", d("2026-10-03")),
    ):
        store.update_session_status(session_id, status, new_date, None)
    # ids[4] (10-02) and the moved copy (10-03) are planned: replace both, they are superseded and hidden
    store.replace_planned(d("2026-10-02"), d("2026-10-04"), d("2026-10-02"), [session(d("2026-10-04"))])
    for monday, statuses in ((W39, ("done", "done")), (W38, ("done", "skipped")), (W36, ("planned",))):
        week_ids = store.replace_planned(
            monday, monday + timedelta(days=6), monday, [session(monday + timedelta(days=n)) for n in range(len(statuses))]
        )[1]
        for session_id, status in zip(week_ids, statuses, strict=True):
            if status != "planned":
                store.update_session_status(session_id, status, None, None)
    assert report(store)["adherence"] == {
        "planned": 1, "done": 2, "skipped": 1, "moved": 1, "overdue_planned": 1, "completion_rate": 0.67,
        "baseline_completion_rate": 0.75, "baseline_weeks": 2,  # W36 has no finished session: not counted
    }


def test_weekly_adherence_unavailable_when_every_session_is_superseded(store):
    store.replace_planned(W40, W40 + timedelta(days=6), W40, [{"date": W40, "type": "running", "description": "x"}])
    store.replace_planned(W40, W40 + timedelta(days=6), W40, [])
    assert report(store)["adherence"] == {"available": False}


def test_weekly_adherence_without_finished_baseline_sessions(store):
    store.replace_planned(
        W40, W40 + timedelta(days=6), W40, [{"date": W40, "type": "running", "description": "x"}]
    )
    out = report(store)["adherence"]
    assert out["planned"] == 1 and out["completion_rate"] is None
    assert out["baseline_completion_rate"] is None and out["baseline_weeks"] == 0
