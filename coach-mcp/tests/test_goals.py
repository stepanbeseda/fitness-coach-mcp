"""get_goal_progress over seeded histories (PR 14). The expected numbers come from the designer's prototype run."""

from datetime import date, timedelta

import pytest

from coach_mcp import goals
from coach_mcp.goals import RECENT_DAYS, get_goal_progress
from history_helpers import seed_day

TODAY = date(2026, 10, 7)  # a Wednesday; the history never holds today itself


def seed_race(store, column: str, points: list[tuple[date, float]]) -> None:
    for day, value in points:
        store.store_race_predictions({day: {column: value}}, day + timedelta(days=1), day)


def g1_points() -> list[tuple[date, float]]:
    """Half marathon: 2 s faster a day from 2026-07-16 to 2026-10-06 (83 points)."""
    start = TODAY - timedelta(days=83)
    return [(start + timedelta(days=n), 6000 - 2 * n) for n in range(83)]


def goal(store, metric="race_half_marathon", target_value=5800.0, target_date="2026-11-15", **kw) -> int:
    fields = {"title": "Goal", "metric": metric, "target_value": target_value, "target_date": target_date, **kw}
    return store.insert_goal({k: v for k, v in fields.items() if v is not None})


def progress(store, today=TODAY) -> dict:
    return get_goal_progress(store, today)["goals"][0]


@pytest.fixture
def g1(store):
    seed_race(store, "time_half_marathon_s", g1_points())
    goal(store)
    return store


def test_g1_numbers(g1):
    out = progress(g1)
    assert out["goal_id"] == 1 and out["metric"] == "race_half_marathon" and out["unit"] == "s"
    assert out["current"] == {"date": "2026-10-06", "value": 5836, "time": "1:37:16"}
    assert out["weeks_4_ago"] == {"date": "2026-09-09", "value": 5890, "time": "1:38:10"}
    assert out["weeks_12_ago"] is None
    assert out["trend_per_week"] == -14.0 and out["points"] == 83
    assert out["projected"] == {"date": "2026-11-15", "value": 5756, "time": "1:35:56"}
    assert out["target_value"] == {"value": 5800, "time": "1:36:40"}
    assert (out["available"], out["on_track"], out["reason"]) == (True, True, None)
    assert out["days_remaining"] == 39 and out["target_date"] == "2026-11-15"
    assert out["title"] == "Goal"


def test_tie_and_predicate(store):
    seed_race(store, "time_half_marathon_s", g1_points())
    gid = goal(store, target_value=5756.0)
    assert progress(store)["on_track"] is True  # projected 5756 equals the target: a tie is on track
    store.update_goal(gid, {"target_value": 5755.0})
    assert progress(store)["on_track"] is False


def test_reasons(store):
    seed_race(store, "time_half_marathon_s", g1_points())
    gid = goal(store, target_date=None)
    out = progress(store)
    assert (out["available"], out["reason"], out["projected"], out["on_track"]) == (True, "no_target_date", None, None)
    assert out["days_remaining"] is None and out["trend_per_week"] == -14.0
    store.update_goal(gid, {"target_date": "2026-10-06"})  # yesterday
    assert progress(store)["reason"] == "target_date_passed"
    store.update_goal(gid, {"target_date": "2026-10-07"})  # today is not passed
    out = progress(store)
    assert out["projected"]["value"] == 5834 and out["on_track"] is False and out["reason"] is None
    store.update_goal(gid, {"target_date": "2027-04-07"})  # 183 days after the current date
    out = progress(store)
    assert (out["reason"], out["projected"], out["on_track"]) == ("target_date_too_far", None, None)
    assert out["available"] is True
    store.update_goal(gid, {"target_date": "2027-04-06"})  # 182 days
    out = progress(store)
    assert out["projected"]["time"] == "1:31:12" and out["reason"] is None


def test_no_target_value_still_projects(store):
    seed_race(store, "time_half_marathon_s", g1_points())
    goal(store, target_value=None)
    out = progress(store)
    assert out["projected"]["value"] == 5756 and out["on_track"] is None and out["reason"] == "no_target_value"
    assert out["target_value"] is None


def test_weeks_ago_windows(store):
    # 12 weeks ago: the reference date is 2026-07-15, the window 07-09..07-15 (ref - 6 included, ref - 7 not).
    seed_race(store, "time_half_marathon_s", g1_points())
    goal(store)
    seed_race(store, "time_half_marathon_s", [(date(2026, 7, 9), 6100)])
    assert progress(store)["weeks_12_ago"] == {"date": "2026-07-09", "value": 6100, "time": "1:41:40"}


def test_weeks_ago_window_lower_edge(store):
    seed_race(store, "time_half_marathon_s", g1_points() + [(date(2026, 7, 8), 6100)])
    goal(store)
    assert progress(store)["weeks_12_ago"] is None


def test_weeks_ago_window_upper_edge(store):
    # No point on 09-03..09-09; the next one is 09-10 (ref + 1) and must not count; 09-02 (ref - 7) neither.
    points = [p for p in g1_points() if not date(2026, 9, 3) <= p[0] <= date(2026, 9, 9)]
    seed_race(store, "time_half_marathon_s", points)
    goal(store)
    assert progress(store)["weeks_4_ago"] is None
    seed_race(store, "time_half_marathon_s", [(date(2026, 9, 3), 5900)])  # ref - 6
    assert progress(store)["weeks_4_ago"]["date"] == "2026-09-03"


def test_vo2max_goal(store):
    for n in range(15):
        seed_day(store, date(2026, 8, 25) + timedelta(days=3 * n), vo2max_running=50 + 0.15 * n)
    gid = goal(store, metric="vo2max_running", target_value=53.0, target_date="2026-12-01")
    out = progress(store)
    assert out["unit"] == "ml/kg/min" and "time" not in out["current"]
    assert out["current"] == {"date": "2026-10-06", "value": 52.1}
    assert out["weeks_4_ago"] == {"date": "2026-09-09", "value": round(50 + 0.15 * 5, 1)}
    assert out["weeks_4_ago"]["value"] == 50.8
    assert out["trend_per_week"] == 0.35 and out["points"] == 15
    assert out["projected"] == {"date": "2026-12-01", "value": 54.9}
    assert out["on_track"] is True and out["target_value"] == {"value": 53.0}
    store.update_goal(gid, {"target_value": 55.0})
    assert progress(store)["on_track"] is False  # higher is better: 54.9 < 55


def seed_5k(store, points):
    seed_race(store, "time_5k_s", points)


def test_too_few_points(store):
    seed_5k(store, [(date(2026, 8, 31) + timedelta(days=3 * n), 1300 - n) for n in range(13)])
    goal(store, metric="race_5k", target_value=1250.0, target_date="2026-11-01")
    out = progress(store)
    assert (out["available"], out["reason"], out["points"]) == (False, "too_few_values", 13)
    assert out["current"]["date"] == "2026-10-06"
    assert out["weeks_4_ago"] == {"date": "2026-09-09", "value": 1297, "time": "21:37"}
    assert out["trend_per_week"] is None and out["projected"] is None and out["on_track"] is None
    seed_5k(store, [(date(2026, 8, 28), 1301)])
    out = progress(store)
    assert out["available"] is True and out["points"] == 14 and out["trend_per_week"] == -2.3


def test_span_must_be_28_days(store):
    seed_5k(store, [(date(2026, 9, 9) + timedelta(days=n), 1300) for n in range(13)] + [(date(2026, 10, 6), 1290)])
    goal(store, metric="race_5k", target_value=1250.0, target_date="2026-11-01")
    out = progress(store)
    assert (out["available"], out["reason"], out["points"]) == (False, "too_few_values", 14)  # span 27 days
    seed_5k(store, [(date(2026, 9, 8), 1300)])  # 15 points now, span 28
    assert progress(store)["available"] is True


def test_span_of_exactly_28_days_with_14_points(store):
    seed_5k(store, [(date(2026, 9, 8) + timedelta(days=n), 1300) for n in range(13)] + [(date(2026, 10, 6), 1290)])
    goal(store, metric="race_5k", target_value=1250.0, target_date="2026-11-01")
    out = progress(store)
    assert (out["available"], out["points"]) == (True, 14)


def test_current_value_must_be_recent(store):
    seed_5k(store, [(date(2026, 8, 1) + timedelta(days=2 * n), 1300) for n in range(27)])  # last 2026-09-22
    goal(store, metric="race_5k", target_value=1250.0, target_date="2026-11-01")
    out = progress(store)
    assert (out["available"], out["reason"], out["current"], out["points"]) == (False, "no_recent_value", None, 0)
    assert out["weeks_4_ago"]["date"] == "2026-09-08"  # computed in every case, also when not available
    seed_5k(store, [(date(2026, 9, 23), 1300)])  # 14 days old
    out = progress(store)
    assert RECENT_DAYS == 14 and out["available"] is True and out["current"]["date"] == "2026-09-23"
    assert out["trend_per_week"] == 0.0 and out["points"] == 28


def test_trend_too_steep(store):
    seed_5k(store, [(date(2026, 9, 7) + timedelta(days=n), 1600 - 10 * n) for n in range(30)])
    gid = goal(store, metric="race_5k", target_value=1200.0, target_date="2027-02-13")  # 130 days after 10-06
    out = progress(store)
    assert out["projected"]["value"] == 10 and out["on_track"] is True and out["reason"] is None
    store.update_goal(gid, {"target_date": "2027-02-14"})  # 131 days: projected exactly 0
    out = progress(store)
    assert (out["reason"], out["projected"], out["on_track"]) == ("trend_too_steep", None, None)
    assert out["available"] is True and out["trend_per_week"] == -70.0


def test_each_race_metric_reads_its_own_column(store):
    store.store_race_predictions(
        {date(2026, 10, 6): {"time_5k_s": 1290, "time_10k_s": 2700, "time_half_marathon_s": 5970,
                             "time_marathon_s": 12310}},
        TODAY, date(2026, 10, 6),
    )
    expected = {"race_5k": 1290, "race_10k": 2700, "race_half_marathon": 5970, "race_marathon": 12310}
    for metric, value in expected.items():
        store.insert_goal({"title": metric, "metric": metric})
    values = {g["metric"]: g["current"]["value"] for g in get_goal_progress(store, TODAY)["goals"]}
    assert values == expected


def test_vo2max_skips_null_missing_and_zero(store):
    seed_day(store, date(2026, 10, 6), vo2max_running=None)
    seed_day(store, date(2026, 10, 5), vo2max_running=0)
    seed_day(store, date(2026, 10, 4), readiness=70)
    seed_day(store, date(2026, 10, 3), vo2max_running=51.5)
    store.insert_goal({"title": "V", "metric": "vo2max_running"})
    out = get_goal_progress(store, TODAY)["goals"][0]
    assert out["current"] == {"date": "2026-10-03", "value": 51.5}


def test_only_active_goals_with_a_metric(store):
    achieved = store.insert_goal({"title": "Done", "metric": "race_5k"})
    store.update_goal(achieved, {"status": "achieved"})
    store.insert_goal({"title": "Plain 1"})
    store.insert_goal({"title": "Plain 2"})
    store.insert_goal({"title": "Active", "metric": "race_10k"})
    result = get_goal_progress(store, TODAY)
    assert result["today"] == "2026-10-07" and result["goals_without_metric"] == 2
    assert [g["title"] for g in result["goals"]] == ["Active"]


def test_order_follows_the_goal_list(store):
    store.insert_goal({"title": "No date", "metric": "race_5k"})
    store.insert_goal({"title": "Late", "metric": "race_10k", "target_date": "2027-01-01"})
    store.insert_goal({"title": "Early", "metric": "race_marathon", "target_date": "2026-12-01"})
    store.insert_goal({"title": "Early 2", "metric": "vo2max_running", "target_date": "2026-12-01"})
    assert [g["title"] for g in get_goal_progress(store, TODAY)["goals"]] == ["Early", "Early 2", "Late", "No date"]


def test_no_history_at_all(store):
    store.insert_goal({"title": "Empty", "metric": "race_5k", "target_value": 1200.0})
    out = progress(store)
    assert (out["available"], out["reason"], out["points"]) == (False, "no_recent_value", 0)
    assert out["current"] is None and out["weeks_4_ago"] is None and out["weeks_12_ago"] is None
    assert out["trend_per_week"] is None and out["projected"] is None and out["on_track"] is None


def test_no_goals_at_all(store):
    assert get_goal_progress(store, TODAY) == {"today": "2026-10-07", "goals_without_metric": 0, "goals": []}


def test_parse_target_value_and_clock_helpers():
    assert goals.clock(5836) == "1:37:16" and goals.clock(59) == "0:59" and goals.clock(3600) == "1:00:00"
    assert goals.clock(1289.6) == "21:30"
    assert goals.target_value_out("race_5k", 1290.0) == "21:30" and goals.target_value_out("vo2max_running", 55.5) == 55.5
