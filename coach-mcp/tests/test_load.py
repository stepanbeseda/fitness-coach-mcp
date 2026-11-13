"""The load signals (PR 38): detraining and load spike, pure rules over view values."""

from __future__ import annotations

from datetime import date, timedelta

from view_values import W45_DAY, W45_END, W45_GOAL, FakeHistory, w45_history

from coach_mcp import load
from coach_mcp.load import GoalRef, load_signals

W45_DETRAINING = {
    "conditions": ["acwr", "running"],
    "start": "2026-10-26",
    "end": "2026-11-08",
    "goal": {
        "id": 1,
        "title": "Run a marathon",
        "metric": "race_marathon",
        "target_value": "3:59:59",
        "target_date": "2027-10-03",
    },
    "acwr_days": 13,
    "acwr_max": 0.7,
    "acwr_last": 0.4,
    "running_km": {"window": 0.0, "week": 0.0, "baseline": 10.2, "usual": 20.4, "baseline_weeks": 4},
}


def detraining(**kwargs):
    return load_signals(w45_history(**kwargs), [W45_GOAL], W45_DAY).detraining


def test_w45_detraining_holds_by_both_conditions():
    signal = detraining()
    assert signal.holds is True
    assert signal.out() == W45_DETRAINING


def test_detraining_by_acwr_alone():
    signal = detraining(window_km=20.4)
    assert signal.conditions == ["acwr"]
    assert signal.running is False
    assert signal.holds is True


def test_detraining_by_running_alone():
    signal = detraining(acwr=0.8, window_km=7.0)
    assert signal.conditions == ["running"]
    assert signal.acwr is False
    assert signal.holds is True


def test_no_endurance_goal_never_holds():
    history = w45_history()
    for goals in ([], [{**W45_GOAL, "status": "achieved"}], [{**W45_GOAL, "metric": None}]):
        assert load_signals(history, goals, W45_DAY).detraining.holds is False


def test_acwr_exactly_at_the_limit_does_not_hold():
    signal = detraining(acwr=0.8, window_km=20.4)
    assert signal.acwr is False
    assert signal.running is False
    assert signal.holds is False


def test_running_exactly_half_does_not_hold():
    half = detraining(acwr=0.8, window_km=10.2)
    assert (half.running, half.holds) == (False, False)
    below = detraining(acwr=0.8, window_km=10.1)
    assert (below.running, below.holds) == (True, True)


def test_acwr_needs_ten_stored_values():
    few = detraining(window_km=20.4, acwr_days=9)
    assert few.acwr is None
    assert few.holds is None
    enough = detraining(window_km=20.4, acwr_days=10)
    assert enough.acwr is True
    assert enough.acwr_days == 10
    assert enough.holds is True


def test_one_condition_unknown_and_the_other_false_is_unknown():
    signal = detraining(acwr=0.8, unfetched=(W45_END - timedelta(days=7),))
    assert signal.running is None
    assert signal.acwr is False
    assert signal.holds is None


def test_history_off_is_unknown():
    signals = load_signals(FakeHistory(), [W45_GOAL], W45_DAY)
    assert signals.detraining.holds is None
    assert signals.load_spike.holds is None
    assert load_signals(FakeHistory(), [], W45_DAY).detraining.holds is False


def test_endurance_goal_picks_the_earliest_target_date():
    def goal(id_, metric, target_value=None, target_date=None, status="active"):
        return {"id": id_, "title": f"g{id_}", "metric": metric, "target_value": target_value,
                "target_date": target_date, "status": status}  # fmt: skip

    goals = [
        goal(1, "race_5k"),
        goal(2, "race_half_marathon", 5800.0, "2027-05-01"),
        {**W45_GOAL, "id": 3},
        goal(4, "vo2max_running", 55.0, "2027-04-01", "achieved"),
        goal(5, None, None, "2026-12-01"),
    ]
    assert load.endurance_goal(goals) == GoalRef(2, "g2", "race_half_marathon", "1:36:40", date(2027, 5, 1))
    assert load.endurance_goal(goals[:1]) == GoalRef(1, "g1", "race_5k", None, None)
    assert load.endurance_goal([goal(6, "vo2max_running", 55.0)]).target_value == 55.0


def test_running_baseline_needs_two_complete_blocks():
    def gap(*offsets):
        return tuple(W45_END - timedelta(days=n) for n in offsets)

    one = detraining(acwr=0.8, unfetched=gap(15, 22, 29))
    assert (one.running, one.baseline_weeks) == (None, 1)
    two = detraining(acwr=0.8, unfetched=gap(15, 22))
    assert (two.running, two.baseline_weeks) == (True, 2)


def test_load_spike_boundaries():
    assert load.load_spike(30, [20, 20]).holds is False
    assert load.load_spike(31, [20, 20]).holds is True
    assert load.load_spike(99, [20]).holds is None
    assert load.load_spike(5, [0, 0]).holds is False
    assert load.load_spike(None, [20, 20]).holds is None


def test_w45_load_spike():
    spike = load_signals(w45_history(), [W45_GOAL], W45_DAY).load_spike
    assert spike.holds is True
    assert spike.out() == {"week": 77, "baseline": 21.8, "baseline_weeks": 4, "ratio": 3.54}


def test_rolling_spike_unknown_until_yesterday_is_covered():
    uncovered = w45_history(cover=(W45_END - timedelta(days=68), W45_END - timedelta(days=1)))
    assert load_signals(uncovered, [], W45_DAY).load_spike.holds is None
    history = w45_history()
    history.coverage = None
    assert load_signals(history, [], W45_DAY).load_spike.holds is None


def test_rolling_spike_uses_only_fully_covered_baseline_windows():
    narrow = load_signals(w45_history(cover=(W45_END - timedelta(days=20), W45_END)), [], W45_DAY).load_spike
    assert (narrow.baseline_weeks, narrow.baseline, narrow.holds) == (2, 22.5, True)
    too_narrow = w45_history(cover=(W45_END - timedelta(days=19), W45_END))
    assert load_signals(too_narrow, [], W45_DAY).load_spike.holds is None


def test_load_signals_reads_up_to_yesterday():
    history = w45_history()
    load_signals(history, [], W45_DAY)
    assert ("days", W45_END - timedelta(days=41), W45_END) in history.calls
    assert ("strength", W45_END - timedelta(days=34), W45_END) in history.calls
