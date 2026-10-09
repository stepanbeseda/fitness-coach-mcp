from __future__ import annotations

import math
import re
from datetime import date, timedelta
from typing import Any

import pytest
from history_helpers import seed_activities, seed_day, sport
from mcp.server.mcpserver.exceptions import ToolError

from conftest import TODAY
from coach_mcp import correlations

HRV = [50, 52, 49, 55, 58, 54, 60, 57, 62, 59, 65, 63, 61, 66]
RHR = [60, 58, 61, 57, 55, 58, 52, 56, 51, 54, 49, 50, 53, 48]
SLEEP_SCORE_TIES = [70, 75, 75, 80, 70, 85, 90, 75, 80, 85, 90, 95, 70, 80]
STRESS_TIES = [30, 28, 28, 25, 31, 22, 20, 27, 24, 22, 19, 18, 30, 26]
READINESS_LINE = list(range(40, 54))
BATTERY_OUTLIER = [*range(30, 56, 2), 99]
LOAD = [40, 80, 55, 120, 30, 95, 70, 110, 45, 85, 60, 100, 35, 90, 75]
LOAD_HRV = [60, 80.0, 60.0, 72.5, 40.0, 85.0, 52.5, 65.0, 45.0, 77.5, 57.5, 70.0, 50.0, 82.5, 55.0]
SLEEP_A = [78, 72, 80, 70, 66, 81, 64, 79, 74, 60, 83, 71, 77, 73]
RUN_KM = [0, 5.0, 0, 8.0, 10.0, 0, 12.0, 0, 6.0, 15.0, 0, 9.0, 0, 7.5]
RHR_NOISE = [58, 70, 46, 48, 46, 53, 58, 60, 61, 67, 56, 41, 62, 56]


def corr(
    store, today=TODAY, *, metrics, start_date="2026-09-01", end_date="2026-09-30", lag_days=0
) -> dict[str, Any]:
    return correlations.get_correlations(
        store, today, metrics=metrics, start_date=start_date, end_date=end_date, lag_days=lag_days
    )


def d(text: str) -> date:
    return date.fromisoformat(text)


def days(n: int, first: str = "2026-09-01") -> list[date]:
    return [d(first) + timedelta(days=i) for i in range(n)]


def pair(out: dict[str, Any], a: str, b: str) -> dict[str, Any]:
    (found,) = [p for p in out["pairs"] if p["a"] == a and p["b"] == b]
    return found


def seed(store, metric: str, values: list[float], first: str = "2026-09-01") -> None:
    for day, value in zip(days(len(values), first), values, strict=True):
        seed_day(store, day, **{metric: value})


def seed_s1(store, count: int = 14) -> None:
    seed(store, "hrv_last_night_ms", HRV[:count])
    seed(store, "resting_hr", RHR[:count])


def seed_activity_days(store) -> None:
    seed(store, "sleep_score", SLEEP_A)
    for day, km in zip(days(14), RUN_KM, strict=True):
        seed_activities(store, day, {} if km == 0 else {"running": sport(1, 30, km, 40)})


S1_PAIR = {"a": "hrv_last_night_ms", "b": "resting_hr", "n_pairs": 14, "available": True, "pearson": -0.989, "spearman": -0.995}


def test_pearson_and_spearman_match_reference(store):
    seed_s1(store)
    seed(store, "readiness", READINESS_LINE)
    seed(store, "body_battery_high", BATTERY_OUTLIER)
    out = corr(store, metrics=["hrv_last_night_ms", "resting_hr"])
    assert pair(out, "hrv_last_night_ms", "resting_hr") == S1_PAIR
    out = corr(store, metrics=["readiness", "body_battery_high"])
    assert pair(out, "readiness", "body_battery_high") == {
        "a": "readiness", "b": "body_battery_high", "n_pairs": 14, "available": True, "pearson": 0.796, "spearman": 1.0,
    }


def test_spearman_averages_tied_ranks(store):
    seed(store, "sleep_score", SLEEP_SCORE_TIES)
    seed(store, "stress_avg", STRESS_TIES)
    p = pair(corr(store, metrics=["sleep_score", "stress_avg"]), "sleep_score", "stress_avg")
    assert (p["n_pairs"], p["pearson"], p["spearman"]) == (14, -0.989, -0.988)


def test_lag_pairs_a_with_b_lag_days_later(store):
    seed(store, "acute_load", LOAD)
    seed(store, "hrv_last_night_ms", LOAD_HRV)
    both = {"start_date": "2026-09-01", "end_date": "2026-09-15"}
    p = pair(corr(store, metrics=["acute_load", "hrv_last_night_ms"], **both), "acute_load", "hrv_last_night_ms")
    assert (p["n_pairs"], p["pearson"], p["spearman"]) == (15, 0.738, 0.754)
    out = corr(store, metrics=["acute_load", "hrv_last_night_ms"], lag_days=1, **both)
    p = pair(out, "acute_load", "hrv_last_night_ms")
    assert out["lag_days"] == 1
    assert (p["n_pairs"], p["pearson"], p["spearman"]) == (14, -1.0, -1.0)
    out = corr(store, metrics=["hrv_last_night_ms", "acute_load"], lag_days=1, **both)
    p = pair(out, "hrv_last_night_ms", "acute_load")
    assert (p["n_pairs"], p["pearson"], p["spearman"]) == (14, -0.572, -0.631)
    out = corr(store, metrics=["acute_load", "hrv_last_night_ms"], lag_days=2, **both)
    assert pair(out, "acute_load", "hrv_last_night_ms") == {
        "a": "acute_load", "b": "hrv_last_night_ms", "n_pairs": 13, "available": False, "reason": "too_few_pairs",
    }


def test_lag_stays_inside_the_range(store):
    seed(store, "acute_load", LOAD)
    seed(store, "hrv_last_night_ms", LOAD_HRV)
    metrics = ["acute_load", "hrv_last_night_ms"]
    out = corr(store, metrics=metrics, lag_days=1, start_date="2026-09-01", end_date="2026-09-14")
    p = pair(out, *metrics)
    assert (p["n_pairs"], p["available"], p["reason"]) == (13, False, "too_few_pairs")
    out = corr(store, metrics=metrics, lag_days=1, start_date="2026-08-31", end_date="2026-09-15")
    assert pair(out, *metrics)["n_pairs"] == 14


def test_constant_series_has_no_coefficient(store):
    seed(store, "hrv_last_night_ms", HRV)
    seed(store, "sleep_duration_h", [7.0] * 14)
    seed(store, "acwr_ratio", [1.1] * 14)
    out = corr(store, metrics=["hrv_last_night_ms", "sleep_duration_h", "acwr_ratio"])
    base = {"n_pairs": 14, "available": False, "reason": "constant"}
    assert out["pairs"] == [
        {"a": "hrv_last_night_ms", "b": "sleep_duration_h", **base, "constant": ["sleep_duration_h"]},
        {"a": "hrv_last_night_ms", "b": "acwr_ratio", **base, "constant": ["acwr_ratio"]},
        {"a": "sleep_duration_h", "b": "acwr_ratio", **base, "constant": ["sleep_duration_h", "acwr_ratio"]},
    ]


def test_min_pairs_boundary(store):
    seed_s1(store, 13)
    out = corr(store, metrics=["hrv_last_night_ms", "resting_hr"])
    assert pair(out, "hrv_last_night_ms", "resting_hr") == {
        "a": "hrv_last_night_ms", "b": "resting_hr", "n_pairs": 13, "available": False, "reason": "too_few_pairs",
    }
    seed_day(store, d("2026-09-14"), hrv_last_night_ms=HRV[13], resting_hr=RHR[13])
    out = corr(store, metrics=["hrv_last_night_ms", "resting_hr"])
    assert pair(out, "hrv_last_night_ms", "resting_hr") == S1_PAIR
    assert out["min_pairs"] == correlations.MIN_PAIRS == 14


def test_days_missing_a_value_are_skipped(store):
    seed_s1(store)
    seed(store, "hrv_last_night_ms", [40, 45, 70], first="2026-09-15")
    seed(store, "resting_hr", [70, 65], first="2026-09-18")
    seed_day(store, d("2026-09-20"), sleep_score=80)
    out = corr(store, metrics=["hrv_last_night_ms", "resting_hr"])
    assert pair(out, "hrv_last_night_ms", "resting_hr") == S1_PAIR


def test_activity_totals_zero_only_when_fetched(store):
    seed_activity_days(store)
    seed(store, "sleep_score", [75, 68, 82], first="2026-09-15")  # no activities fetched on these days
    out = corr(store, metrics=["running_distance_km", "sleep_score"])
    p = pair(out, "running_distance_km", "sleep_score")
    assert (p["n_pairs"], p["pearson"], p["spearman"]) == (14, -0.962, -0.942)


def test_pairs_follow_request_order_and_dedupe(store):
    out = corr(store, metrics=["readiness", "sleep_score", "stress_avg", "readiness"])
    assert [(p["a"], p["b"]) for p in out["pairs"]] == [
        ("readiness", "sleep_score"), ("readiness", "stress_avg"), ("sleep_score", "stress_avg"),
    ]


def test_empty_store_is_unavailable_with_caveat(store):
    out = corr(store, metrics=["readiness", "sleep_score"])
    assert out["pairs"] == [
        {"a": "readiness", "b": "sleep_score", "n_pairs": 0, "available": False, "reason": "too_few_pairs"}
    ]
    assert out["caveat"] == correlations.CAVEAT and "not a cause" in correlations.CAVEAT
    assert out["min_pairs"] == 14


def test_defaults_last_90_days_ending_yesterday(store):
    out = correlations.get_correlations(
        store, TODAY, metrics=["readiness", "sleep_score"], start_date=None, end_date=None, lag_days=0
    )
    assert (out["start"], out["end"], out["lag_days"]) == ("2026-07-09", "2026-10-06", 0)
    out = corr(store, metrics=["readiness", "sleep_score"], start_date="2026-09-01", end_date="2026-10-07")
    assert out["end"] == "2026-10-07"
    out = corr(store, metrics=["readiness", "sleep_score"], start_date="2024-10-06", end_date="2026-10-06")
    assert (out["start"], out["end"]) == ("2024-10-06", "2026-10-06")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"metrics": ["readiness"]}, "at least two different metrics are needed"),
        ({"metrics": ["readiness", "readiness"]}, "at least two different metrics are needed"),
        (
            {"metrics": ["readiness", "sleep_score", "stress_avg", "resting_hr", "acute_load", "acwr_ratio", "vo2max_running"]},
            "at most 6 different metrics",
        ),
        ({"metrics": ["readiness", "steps"]}, "unknown metric: steps"),
        ({"lag_days": -1}, "lag_days must be 0 to 3"),
        ({"lag_days": 4}, "lag_days must be 0 to 3"),
        ({"end_date": "2026-10-08"}, "end_date must not be after today"),
        ({"start_date": "2026-09-03", "end_date": "2026-09-02"}, "start_date must be on or before end_date"),
        ({"start_date": "2024-10-05", "end_date": "2026-10-06"}, "date range too long (max 731 days)"),
        ({"end_date": "2026-13-01"}, "end_date is not a valid date"),
        ({"start_date": "yesterday"}, "start_date must be YYYY-MM-DD"),
    ],
)
def test_argument_errors(store, kwargs, message):
    arguments = {"metrics": ["readiness", "sleep_score"], **kwargs}
    with pytest.raises(ToolError, match=re.escape(message)):
        corr(store, **arguments)


def test_upper_bounds_are_accepted(store):
    out = corr(store, metrics=["readiness", "sleep_score"], lag_days=3)
    assert out["lag_days"] == 3
    six = ["readiness", "sleep_score", "stress_avg", "resting_hr", "acute_load", "hrv_last_night_ms"]
    assert len(corr(store, metrics=six)["pairs"]) == 15


def test_too_few_pairs_wins_over_constant(store):
    seed(store, "hrv_last_night_ms", HRV[:13])
    seed(store, "sleep_duration_h", [7.0] * 13)
    out = corr(store, metrics=["hrv_last_night_ms", "sleep_duration_h"])
    assert out["pairs"] == [
        {"a": "hrv_last_night_ms", "b": "sleep_duration_h", "n_pairs": 13, "available": False, "reason": "too_few_pairs"}
    ]


def test_negative_zero_is_normalized(store):
    seed(store, "hrv_last_night_ms", HRV)
    seed(store, "resting_hr", RHR_NOISE)
    p = pair(corr(store, metrics=["hrv_last_night_ms", "resting_hr"]), "hrv_last_night_ms", "resting_hr")
    assert p["pearson"] == 0.0
    assert math.copysign(1.0, p["pearson"]) == 1.0
    assert p["spearman"] == 0.002
