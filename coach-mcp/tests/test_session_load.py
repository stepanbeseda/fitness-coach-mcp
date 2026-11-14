"""Foster session load: the rating of done plan sessions, the weekly section, baseline, monotony and the flag."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from coach_mcp.session_load import flags, rate, section

MON = date(2026, 11, 2)  # ISO week 45
BASE = [MON - timedelta(weeks=n) for n in (4, 3, 2, 1)]


def row(sid: int, day: date | str, type_: str = "running", status: str = "done", **kw: Any) -> dict[str, Any]:
    return {
        "id": sid,
        "date": day if isinstance(day, str) else day.isoformat(),
        "type": type_,
        "status": status,
        "duration_min": kw.pop("duration_min", None),
        "rpe": kw.pop("rpe", None),
        **kw,
    }


def week(monday: date, **kw: Any):
    """Mon-Sat one done running of 60 planned minutes, coach RPE 5 (load 300 each), Sunday free."""
    return [row(100 + i + 10 * (monday - MON).days // 7, monday + timedelta(days=i), duration_min=60, rpe=5) for i in range(6)]


def sect(sessions, activities=None, hevy=None, monday=MON, baseline=BASE, partial=False):
    return section(sessions, activities or {}, hevy or {}, monday, baseline, partial=partial)


def test_coach_rpe_beats_hevy():
    (r,) = rate([row(1, MON, "strength", rpe=9, duration_min=60)], {}, {MON: 7.0})
    assert (r.rpe, r.rpe_source) == (9.0, "coach")


def test_strength_day_takes_the_hevy_mean():
    (r,) = rate([row(1, MON, "strength", duration_min=60)], {}, {MON: 7.94})
    assert (r.rpe, r.rpe_source) == (7.9, "hevy")


def test_hevy_rates_strength_only_and_a_day_without_hevy_is_unrated():
    (lift,) = rate([row(1, MON, "strength", duration_min=60)], {}, {})
    (run,) = rate([row(2, MON, "running", duration_min=60)], {}, {MON: 8.0})
    assert (lift.rpe, lift.rpe_source, run.rpe, run.rpe_source) == (None, None, None, None)


@pytest.mark.parametrize("type_", ["running", "cycling", "swimming"])
def test_endurance_is_rated_by_the_coach_only(type_):
    (r,) = rate([row(1, MON, type_, rpe=6 if type_ == "running" else 5, duration_min=30)], {}, {})
    assert (r.rpe, r.rpe_source) == (6.0 if type_ == "running" else 5.0, "coach")


def test_garmin_minutes_need_exactly_one_done_session_of_the_type():
    acts = {MON: {"running": {"duration_min": 47.5}, "strength": {"duration_min": 120}, "other": {"duration_min": 30}}}
    sessions = [
        row(1, MON, "running", duration_min=45),
        row(2, MON, "running", status="planned", duration_min=30),
        row(3, MON, "strength", duration_min=60),
        row(4, MON, "strength", duration_min=60),
        row(5, MON, "mobility", duration_min=20),
    ]
    by_id = {r.id: r for r in rate(sessions, acts, {})}
    assert (by_id[1].minutes, by_id[1].minutes_source) == (47.5, "garmin")
    assert (by_id[3].minutes, by_id[3].minutes_source) == (60.0, "plan")
    assert (by_id[4].minutes, by_id[4].minutes_source) == (60.0, "plan")
    assert (by_id[5].minutes, by_id[5].minutes_source) == (20.0, "plan")


@pytest.mark.parametrize(
    "acts",
    [{}, {MON: None}, {MON: {"cycling": {"duration_min": 50}}}, {MON: {"running": {"duration_min": 0}}}],
    ids=["no-day", "day-none", "no-sport", "zero"],
)
def test_planned_minutes_are_the_fallback(acts):
    (r,) = rate([row(1, MON, "running", duration_min=45)], acts, {})
    assert (r.minutes, r.minutes_source) == (45.0, "plan")


def test_a_rated_session_without_minutes_has_no_load_and_makes_the_week_incomplete():
    sessions = [row(1, MON, "running", rpe=7)]
    (r,) = rate(sessions, {}, {})
    assert r.minutes is None and r.load is None
    out = sect(sessions)
    total = out["total"]
    assert (total["rated"], total["no_minutes"], total["load"], total["avg_rpe"]) == (1, 1, 0, None)
    assert out["complete"] is False


def test_section_of_week_45():
    sessions = [
        row(1, "2026-11-02", "strength", duration_min=60),
        row(2, "2026-11-03", "running", rpe=6, duration_min=45),
        row(3, "2026-11-05", "strength", duration_min=60),
        row(4, "2026-11-06", "running", status="skipped", duration_min=45),
        row(5, "2026-11-07", "mobility", status="planned", duration_min=20),
    ]
    acts = {MON: {"strength": {"duration_min": 58.0}}, date(2026, 11, 3): {"running": {"duration_min": 47.5}}}
    out = sect(sessions, acts, {MON: 8.0})
    assert out["strength"] == {"load": 464, "minutes": 58.0, "avg_rpe": 8.0, "rated": 1, "unrated": 1, "no_minutes": 0}
    assert out["endurance"] == {"load": 285, "minutes": 47.5, "avg_rpe": 6.0, "rated": 1, "unrated": 0, "no_minutes": 0}
    assert out["total"] == {"load": 749, "minutes": 105.5, "avg_rpe": 7.1, "rated": 2, "unrated": 1, "no_minutes": 0}
    assert (out["complete"], out["monotony"], out["strain"]) == (False, None, None)
    assert out["baseline"] == {"total": None, "strength": None, "endurance": None, "weeks": 0}
    assert out["sessions"] == [
        {"id": 1, "date": "2026-11-02", "type": "strength", "rpe": 8.0, "rpe_source": "hevy", "minutes": 58.0,
         "minutes_source": "garmin", "load": 464},
        {"id": 2, "date": "2026-11-03", "type": "running", "rpe": 6.0, "rpe_source": "coach", "minutes": 47.5,
         "minutes_source": "garmin", "load": 285},
        {"id": 3, "date": "2026-11-05", "type": "strength", "rpe": None, "rpe_source": None, "minutes": 60.0,
         "minutes_source": "plan", "load": None},
    ]
    assert list(out) == ["total", "strength", "endurance", "complete", "sessions", "baseline", "monotony", "strain"]


def test_monotony_and_strain():
    six = week(MON)
    out = sect(six)
    assert (out["monotony"], out["strain"], out["complete"]) == (2.45, 4409, True)
    seven = six + [row(200, MON + timedelta(days=6), duration_min=60, rpe=4)]
    out = sect(seven)
    assert (out["monotony"], out["strain"]) == (13.88, 28316)  # a sample SD would give 12.85
    flat = six + [row(200, MON + timedelta(days=6), duration_min=60, rpe=5)]
    out = sect(flat)
    assert (out["monotony"], out["strain"]) == (None, None)
    unrated = six + [row(201, MON + timedelta(days=1), "strength", duration_min=60)]
    out = sect(unrated)
    assert (out["complete"], out["monotony"]) == (False, None)
    assert sect(six, partial=True)["monotony"] is None


def test_the_flag_is_a_rounded_value_above_two_in_a_final_week():
    base = {"monotony": 2.45, "strain": 4409, "total": {"load": 1800}}
    assert flags(base, final=True) == [
        {"flag": "load_monotony", "monotony": 2.45, "limit": 2.0, "strain": 4409, "week": 1800}
    ]
    assert flags({**base, "monotony": 2.0}, final=True) == []
    assert len(flags({**base, "monotony": 2.01}, final=True)) == 1
    assert flags({**base, "monotony": None}, final=True) == []
    assert flags(base, final=False) == []


def test_baseline_counts_complete_weeks_only():
    prev4, prev3, prev2, prev1 = BASE
    sessions = [
        row(1, prev4, "strength", rpe=6, duration_min=100),  # 600
        row(2, prev4 + timedelta(days=1), "running", rpe=4, duration_min=100),  # 400
        row(3, prev3, "strength", rpe=5, duration_min=100),  # 500
        row(4, prev2, "running", rpe=5, duration_min=60),
        row(5, prev2 + timedelta(days=1), "strength", duration_min=60),  # unrated: the week is incomplete
    ]
    assert sect(sessions)["baseline"] == {"total": 750, "strength": 550, "endurance": 200, "weeks": 2}


def test_mobility_and_other_count_in_the_total_only():
    sessions = [row(1, MON, "mobility", rpe=4, duration_min=30), row(2, MON + timedelta(days=1), "other", rpe=3, duration_min=20)]
    out = sect(sessions)
    assert out["total"]["load"] == 180
    for name in ("strength", "endurance"):
        assert (out[name]["load"], out[name]["rated"], out[name]["unrated"]) == (0, 0, 0)
