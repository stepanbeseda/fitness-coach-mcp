"""The recommendation rules of the Monday report (PR 39): ``recommend`` over hand-built ``get_weekly_report`` results.

Every expected value is a literal (the items of the rule table, the numbers behind them, the order and the hiding),
not computed from the code under test.
"""

import pytest

from coach_mcp import recommendations, trends
from coach_mcp.recommendations import ITEM_KEYS, RULES, recommend

FINAL = {"start": "2026-11-02", "end": "2026-11-08", "iso_week": "2026-W45", "partial": False}
GOAL = {"id": 1, "title": "Run a marathon", "metric": "race_marathon", "target_value": "3:59:59",
        "target_date": "2027-10-03"}
DETRAINING = {"flag": "detraining", "conditions": ["acwr", "running"], "start": "2026-10-26", "end": "2026-11-08",
              "goal": GOAL, "acwr_days": 13, "acwr_max": 0.7, "acwr_last": 0.4,
              "running_km": {"window": 0.0, "week": 0.0, "baseline": 10.2, "usual": 20.4, "baseline_weeks": 4}}
LOAD_SPIKE = {"flag": "load_spike", "week": 77, "baseline": 21.8, "change": 55.2, "change_pct": 254.0,
              "baseline_weeks": 4, "ratio": 3.54}


def suspect(exercise, day, weight, reps):
    return {"flag": "strength_record_suspect", "week": 160.0, "baseline": 100.0, "change": 60.0, "change_pct": 60.0,
            "exercise": exercise, "template_id": "T9", "date": day, "weight_kg": weight, "reps": reps}


ALONE = {
    "load_spike": LOAD_SPIKE,
    "endurance_load_up": {"flag": "endurance_load_up", "week": 820.4, "baseline": 600.0, "change": 220.4,
                          "change_pct": 36.7},
    "hrv_down": {"flag": "hrv_down", "week": 44.0, "baseline": 55.0, "change": -11.0, "change_pct": -20.0},
    "resting_hr_up": {"flag": "resting_hr_up", "week": 53.0, "baseline": 47.5, "change": 5.5, "change_pct": 11.6},
    "sleep_down": {"flag": "sleep_down", "week": 6.4, "baseline": 7.0, "change": -0.6, "change_pct": -8.6},
    "detraining_running": DETRAINING,
    "detraining_acwr": {**DETRAINING, "conditions": ["acwr"],
                        "running_km": {"window": 30.0, "week": 15.0, "baseline": 12.0, "usual": 24.0,
                                       "baseline_weeks": 4}},
    "endurance_time_up": {"flag": "endurance_time_up", "week": 410.0, "baseline": 270.4, "change": 139.6,
                          "change_pct": 51.6},
    "strength_volume_up": {"flag": "strength_volume_up", "week": 7700.0, "baseline": 2175.0, "change": 5525.0,
                           "change_pct": 254.0},
    "endurance_load_down": {"flag": "endurance_load_down", "week": 120.0, "baseline": 400.0, "change": -280.0,
                            "change_pct": -70.0},
    "strength_record_suspect": suspect("Hip Thrust (Barbell)", "2026-11-08", 140.0, 5),
}


ALONE_ITEMS = {
    "load_spike": [
        {"key": "load_spike", "source": "load_spike", "numbers": {"sets": 77, "baseline_sets": 21.8, "max_sets": 33, "factor": 1.5}},
    ],
    "endurance_load_up": [
        {"key": "endurance_load_up", "source": "endurance_load_up", "numbers": {"pct": 36.7, "max_load": 780, "cap_pct": 30.0, "easy_days": 2}},
    ],
    "hrv_down": [
        {"key": "hrv_down", "source": "hrv_down", "numbers": {"pct": 20.0, "easy_days": 2}},
    ],
    "resting_hr_up": [
        {"key": "resting_hr_up", "source": "resting_hr_up", "numbers": {"bpm": 5.5, "easy_days": 2}},
    ],
    "sleep_down": [
        {"key": "sleep_down", "source": "sleep_down", "numbers": {"minutes": 36, "baseline_h": 7.0}},
    ],
    "detraining_running": [
        {"key": "detraining_running", "source": "detraining", "numbers": {"km_week": 0.0, "baseline_km": 10.2, "runs": 3, "km": 10, "goal": "race_marathon"}},
    ],
    "detraining_acwr": [
        {"key": "detraining_acwr", "source": "detraining", "numbers": {"acwr": 0.4, "acwr_limit": 0.8, "days": 14, "easy_sessions": 3, "goal": "race_marathon"}},
    ],
    "endurance_time_up": [
        {"key": "endurance_time_up", "source": "endurance_time_up", "numbers": {"pct": 51.6, "max_time_min": 352, "cap_pct": 30.0}},
    ],
    "strength_volume_up": [
        {"key": "strength_volume_up", "source": "strength_volume_up", "numbers": {"pct": 254.0}},
    ],
    "endurance_load_down": [
        {"key": "endurance_load_down", "source": "endurance_load_down", "numbers": {"pct": 70.0}},
    ],
    "strength_record_suspect": [
        {"key": "strength_record_suspect", "source": "strength_record_suspect", "numbers": {"records": [{"exercise": "Hip Thrust (Barbell)", "date": "2026-11-08", "weight_kg": 140.0, "reps": 5}], "more": 0}},
    ],
}

CONFIRM_ITEMS = {
    1: [
        {"key": "unconfirmed_sessions", "source": "adherence.planned_matched", "numbers": {"sessions": 1}},
    ],
    2: [
        {"key": "unconfirmed_sessions", "source": "adherence.planned_matched", "numbers": {"sessions": 2}},
    ],
    5: [
        {"key": "unconfirmed_sessions", "source": "adherence.planned_matched", "numbers": {"sessions": 5}},
    ],
}

W45_ITEMS = [
    {"key": "load_spike", "source": "load_spike", "numbers": {"sets": 77, "baseline_sets": 21.8, "max_sets": 33, "factor": 1.5}},
    {"key": "detraining_running", "source": "detraining", "numbers": {"km_week": 0.0, "baseline_km": 10.2, "runs": 3, "km": 10, "goal": "race_marathon"}},
    {"key": "strength_record_suspect", "source": "strength_record_suspect", "numbers": {"records": [{"exercise": "Hip Thrust", "date": "2026-11-08", "weight_kg": 140.0, "reps": 5}, {"exercise": "Calf Raise", "date": "2026-11-08", "weight_kg": 100.0, "reps": 10}], "more": 0}},
]

HIDDEN_BEFORE_CAP_ITEMS = [
    {"key": "load_spike", "source": "load_spike", "numbers": {"sets": 77, "baseline_sets": 21.8, "max_sets": 33, "factor": 1.5}},
    {"key": "detraining_running", "source": "detraining", "numbers": {"km_week": 0.0, "baseline_km": 10.2, "runs": 3, "km": 10, "goal": "race_marathon"}},
    {"key": "strength_record_suspect", "source": "strength_record_suspect", "numbers": {"records": [{"exercise": "Hip Thrust (Barbell)", "date": "2026-11-08", "weight_kg": 140.0, "reps": 5}], "more": 0}},
]

CAP_ITEMS = [
    {"key": "load_spike", "source": "load_spike", "numbers": {"sets": 77, "baseline_sets": 21.8, "max_sets": 33, "factor": 1.5}},
    {"key": "hrv_down", "source": "hrv_down", "numbers": {"pct": 20.0, "easy_days": 2}},
    {"key": "sleep_down", "source": "sleep_down", "numbers": {"minutes": 36, "baseline_h": 7.0}},
]

FIVE_SUSPECTS_ITEMS = [
    {"key": "strength_record_suspect", "source": "strength_record_suspect", "numbers": {"records": [{"exercise": "Hip Thrust", "date": "2026-11-08", "weight_kg": 140.0, "reps": 5}, {"exercise": None, "date": "2026-11-07", "weight_kg": 62.5, "reps": 8}, {"exercise": "Calf Raise", "date": "2026-11-06", "weight_kg": 100.0, "reps": 10}], "more": 2}},
]

PARTIAL_ITEMS = [
    {"key": "unconfirmed_sessions", "source": "adherence.planned_matched", "numbers": {"sessions": 2}},
]

ACWR_FALLBACK_ITEMS = [
    {"key": "detraining_acwr", "source": "detraining", "numbers": {"acwr": 0.4, "acwr_limit": 0.8, "days": 14, "easy_sessions": 3, "goal": "race_marathon"}},
]

HIDING_ITEMS = [
    {"key": "endurance_load_up", "source": "endurance_load_up", "numbers": {"pct": 36.7, "max_load": 780, "cap_pct": 30.0, "easy_days": 2}},
    {"key": "hrv_down", "source": "hrv_down", "numbers": {"pct": 20.0, "easy_days": 2}},
]


def report(flags, *, partial=False, matched=None):
    adherence = {"planned": 3, "done": 2} if matched is None else {"planned": 3, "done": 2, "planned_matched": matched}
    return {"week": {**FINAL, "partial": partial}, "adherence": adherence, "flags": flags}


def keys(items):
    return [item["key"] for item in items]


def every_source():
    """One flag of every source, in reverse priority order, and one unconfirmed session."""
    flags = [ALONE[k] for k in ("strength_record_suspect", "strength_volume_up", "endurance_load_down",
                                "endurance_time_up", "detraining_running", "sleep_down", "resting_hr_up", "hrv_down",
                                "endurance_load_up", "load_spike")]
    return report(flags, matched=1)


@pytest.mark.parametrize("key", list(ALONE_ITEMS))
def test_each_source_alone(key):
    assert recommend(report([ALONE[key]])) == ALONE_ITEMS[key]


def test_unconfirmed_sessions_alone():
    assert recommend(report([], matched=1)) == CONFIRM_ITEMS[1]
    assert recommend(report([], matched=5)) == CONFIRM_ITEMS[5]


def test_priority_order(monkeypatch):
    monkeypatch.setattr(recommendations, "MAX_RECOMMENDATIONS", 99)
    assert keys(recommend(every_source())) == [
        "load_spike", "endurance_load_up", "hrv_down", "sleep_down", "detraining_running",
        "strength_record_suspect", "unconfirmed_sessions",
    ]
    only_hidden_side = report([ALONE[k] for k in ("strength_record_suspect", "endurance_load_down",
                                                  "strength_volume_up", "endurance_time_up", "sleep_down",
                                                  "resting_hr_up")], matched=1)
    assert keys(recommend(only_hidden_side)) == [
        "resting_hr_up", "sleep_down", "endurance_time_up", "strength_volume_up", "endurance_load_down",
        "strength_record_suspect", "unconfirmed_sessions",
    ]


def test_rules_table():
    assert [(r.name, r.source, r.priority, r.hides) for r in RULES] == [
        ("load_spike", "load_spike", 10, ("strength_volume_up",)),
        ("endurance_load_up", "endurance_load_up", 20, ("endurance_time_up",)),
        ("hrv_down", "hrv_down", 30, ("resting_hr_up",)),
        ("resting_hr_up", "resting_hr_up", 40, ()),
        ("sleep_down", "sleep_down", 50, ()),
        ("detraining", "detraining", 60, ("endurance_load_down",)),
        ("endurance_time_up", "endurance_time_up", 70, ()),
        ("strength_volume_up", "strength_volume_up", 80, ()),
        ("endurance_load_down", "endurance_load_down", 90, ()),
        ("strength_record_suspect", "strength_record_suspect", 100, ()),
        ("unconfirmed_sessions", "adherence.planned_matched", 110, ()),
    ]
    assert ITEM_KEYS == (
        "load_spike", "endurance_load_up", "hrv_down", "resting_hr_up", "sleep_down", "detraining_running",
        "detraining_acwr", "endurance_time_up", "strength_volume_up", "endurance_load_down",
        "strength_record_suspect", "unconfirmed_sessions",
    )


def test_priority_not_table_order(monkeypatch):
    monkeypatch.setattr(recommendations, "MAX_RECOMMENDATIONS", 99)
    monkeypatch.setattr(recommendations, "RULES", tuple(reversed(recommendations.RULES)))
    assert keys(recommend(every_source())) == [
        "load_spike", "endurance_load_up", "hrv_down", "sleep_down", "detraining_running",
        "strength_record_suspect", "unconfirmed_sessions",
    ]


@pytest.mark.parametrize(
    ("hider", "hidden", "left"),
    [
        ("load_spike", "strength_volume_up", "load_spike"),
        ("detraining_running", "endurance_load_down", "detraining_running"),
        ("endurance_load_up", "endurance_time_up", "endurance_load_up"),
        ("hrv_down", "resting_hr_up", "hrv_down"),
    ],
)
def test_hiding_pair(hider, hidden, left):
    assert keys(recommend(report([ALONE[hider], ALONE[hidden]]))) == [left]
    assert keys(recommend(report([ALONE[hidden], ALONE[hider]]))) == [left]


def test_hiding_pairs_together():
    assert recommend(report([ALONE["endurance_load_up"], ALONE["endurance_time_up"], ALONE["hrv_down"],
                             ALONE["resting_hr_up"]])) == HIDING_ITEMS


def test_hidden_rules_are_dropped_before_the_cap():
    flags = [LOAD_SPIKE, ALONE["strength_volume_up"], DETRAINING, ALONE["endurance_load_down"],
             ALONE["strength_record_suspect"]]
    assert recommend(report(flags)) == HIDDEN_BEFORE_CAP_ITEMS
    assert keys(HIDDEN_BEFORE_CAP_ITEMS) == ["load_spike", "detraining_running", "strength_record_suspect"]


def test_cap_of_three():
    flags = [ALONE[k] for k in ("sleep_down", "endurance_load_down", "hrv_down", "load_spike", "strength_volume_up",
                                "endurance_time_up")]
    assert recommend(report(flags)) == CAP_ITEMS
    assert recommendations.MAX_RECOMMENDATIONS == 3


def test_suspects_merge_into_one():
    five = [suspect("Hip Thrust", "2026-11-08", 140.0, 5), suspect(None, "2026-11-07", 62.5, 8),
            suspect("Calf Raise", "2026-11-06", 100.0, 10), suspect("Squat", "2026-11-05", 120.0, 3),
            suspect("Row", "2026-11-04", 80.0, 8)]
    items = recommend(report(five))
    assert items == FIVE_SUSPECTS_ITEMS
    assert len(items) == 1 and len(items[0]["numbers"]["records"]) == 3 and items[0]["numbers"]["more"] == 2
    two = recommend(report(five[:2]))
    assert len(two) == 1 and len(two[0]["numbers"]["records"]) == 2 and two[0]["numbers"]["more"] == 0


def test_unknown_flag_is_ignored():
    unknown = {"flag": "deload", "week": 1, "baseline": 1, "change": 0, "change_pct": 0}
    assert recommend(report([unknown])) == []
    assert recommend(report([unknown, ALONE["hrv_down"]])) == ALONE_ITEMS["hrv_down"]


@pytest.mark.parametrize(
    ("rep", "expected"),
    [
        (report([]), []),
        (report([LOAD_SPIKE, ALONE["hrv_down"]], partial=True, matched=2), PARTIAL_ITEMS),
        (report([LOAD_SPIKE, ALONE["hrv_down"]], partial=True), []),
        ({"flags": [ALONE["hrv_down"]], "adherence": {"planned": 3, "done": 2}}, []),
        ({"week": FINAL, "adherence": {"available": False}, "flags": []}, []),
        (report([], matched=0), []),
    ],
    ids=["empty", "partial-with-matched", "partial", "no-week-key", "adherence-unavailable", "matched-zero"],
)
def test_empty_or_partial_week(rep, expected):
    assert recommend(rep) == expected


def test_detraining_variants():
    assert keys(recommend(report([DETRAINING]))) == ["detraining_running"]
    assert keys(recommend(report([ALONE["detraining_acwr"]]))) == ["detraining_acwr"]
    slow = {**DETRAINING, "running_km": {**DETRAINING["running_km"], "baseline": 0.6}}
    assert recommend(report([slow])) == ACWR_FALLBACK_ITEMS
    running_only = {**slow, "conditions": ["running"]}
    assert keys(recommend(report([running_only, ALONE["endurance_load_down"]]))) == ["endurance_load_down"]
    assert recommend(report([{**DETRAINING, "goal": None}])) == []


@pytest.mark.parametrize(
    "flag",
    [
        {**ALONE["hrv_down"], "change_pct": None},
        {**LOAD_SPIKE, "baseline": None},
        suspect("Squat", "2026-11-08", None, 5),
    ],
    ids=["hrv-without-pct", "spike-without-baseline", "suspect-without-weight"],
)
def test_missing_numbers_give_no_item(flag):
    assert recommend(report([flag])) == []


def test_w45_synthetic():
    flags = [
        {"flag": "endurance_load_down", "week": 0.0, "baseline": 310.0, "change": -310.0, "change_pct": -100.0},
        DETRAINING,
        {"flag": "strength_volume_up", "week": 7700.0, "baseline": 2175.0, "change": 5525.0, "change_pct": 254.0},
        LOAD_SPIKE,
        suspect("Hip Thrust", "2026-11-08", 140.0, 5), suspect("Calf Raise", "2026-11-08", 100.0, 10),
    ]
    assert recommend(report(flags, matched=1)) == W45_ITEMS


def test_cap_pct_is_the_flag_threshold():
    assert recommendations.CAP_PCT == trends.FLAG_PCT
