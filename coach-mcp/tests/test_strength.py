from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hevy_payloads import (
    deleted,
    hevy_exercise,
    hevy_set,
    hevy_workout,
    updated,
)
from history_helpers import exercise, seed_strength, seed_sync, seed_template
from mcp.server.mcpserver.exceptions import ToolError

from coach_mcp import strength
from coach_mcp.history_view import Coverage, Templates, day_totals
from coach_mcp.strength import (
    e1rm,
    exercises_json,
    get_strength_progress,
    listing_due,
    listing_since,
    page_step,
    parse_events,
    since_text,
    summarize_exercises,
    updated_key,
    workout_day,
)

PRAGUE = ZoneInfo("Europe/Prague")
TODAY = date(2026, 10, 7)


def workout(*exercises: dict[str, Any]) -> dict[str, Any]:
    return hevy_workout("W1", "2026-10-05T10:00:00Z", list(exercises))


def one(*sets: dict[str, Any], template_id: str = "T1", title: str | None = "Bench") -> dict[str, Any]:
    return workout(hevy_exercise(template_id, title, list(sets)))


# --------------------------------------------------------------------- e1RM


def test_e1rm_rule():
    assert e1rm(100, 1) == 100.0
    assert e1rm(100, 5) == pytest.approx(116.6666667)
    assert e1rm(100, 12) == pytest.approx(140.0)
    assert e1rm(100, 13) is None
    assert e1rm(0, 5) is None
    assert e1rm(-10, 5) is None
    assert e1rm(100, 0) is None
    assert e1rm(float("nan"), 5) is None


# ------------------------------------------------------------------ summary


def test_summary_excludes_warmups_and_counts_other_set_types():
    sets = [
        hevy_set(40, 10, "warmup"),
        hevy_set(100, 5, "normal"),
        hevy_set(80, 8, "dropset"),
        hevy_set(100, 3, "failure"),
        {"weight_kg": 100, "reps": 5},  # no type
    ]
    (entry,) = summarize_exercises(one(*sets))
    assert entry["sets"] == 4
    assert entry["volume_kg"] == 100 * 5 + 80 * 8 + 100 * 3 + 100 * 5  # the warm-up (400 kg) is not in it
    (other,) = summarize_exercises(one(hevy_set(100, 5, "something-new")))
    assert other["sets"] == 1  # an unknown type is a working set


def test_summary_volume_needs_weight_and_reps():
    sets = [
        hevy_set(None, 10),  # bodyweight: a set, no volume
        hevy_set(50, None, duration_seconds=60),  # duration set: a set, no volume
        hevy_set(20, 5.0),  # whole float reps count
        hevy_set(20, 5.5),  # fractional reps do not
        hevy_set(True, 5),  # a bool is not a weight
        hevy_set(0, 5),  # zero weight: no volume
    ]
    (entry,) = summarize_exercises(one(*sets))
    assert entry["sets"] == 6
    assert entry["volume_kg"] == 100.0
    assert entry["best"] == {"weight_kg": 20.0, "reps": 5, "e1rm_kg": round(20 * (1 + 5 / 30), 1)}


def test_summary_best_set_and_ties():
    def best(*sets):
        return summarize_exercises(one(*sets))[0]["best"]

    assert best(hevy_set(100, 5), hevy_set(110, 3)) == {"weight_kg": 110.0, "reps": 3, "e1rm_kg": 121.0}
    # 50 x 6 and 60 x 1 both estimate 60.0: the heavier set wins, whatever the order
    assert best(hevy_set(50, 6), hevy_set(60, 1)) == {"weight_kg": 60.0, "reps": 1, "e1rm_kg": 60.0}
    assert best(hevy_set(60, 1), hevy_set(50, 6)) == {"weight_kg": 60.0, "reps": 1, "e1rm_kg": 60.0}
    # a 13-rep set never counts for the best, only for sets and volume
    (entry,) = summarize_exercises(one(hevy_set(200, 13)))
    assert entry["best"] is None and entry["sets"] == 1 and entry["volume_kg"] == 2600.0
    assert best(hevy_set(200, 13), hevy_set(50, 10)) == {"weight_kg": 50.0, "reps": 10, "e1rm_kg": round(50 * 4 / 3, 1)}


def test_summary_merges_a_template_used_twice():
    w = workout(
        hevy_exercise("T1", "Bench", [hevy_set(100, 5), hevy_set(100, 5)]),
        hevy_exercise("T2", "Row", [hevy_set(60, 10)], index=1),
        hevy_exercise("T1", "Bench again", [hevy_set(110, 3)], index=2),
    )
    entries = summarize_exercises(w)
    assert [e["template_id"] for e in entries] == ["T1", "T2"]  # order of first appearance
    assert entries[0] == {
        "template_id": "T1",
        "title": "Bench",  # the first title seen
        "sets": 3,
        "volume_kg": 1330.0,
        "best": {"weight_kg": 110.0, "reps": 3, "e1rm_kg": 121.0},
    }


def test_summary_drops_warmup_only_and_template_less_exercises():
    w = workout(
        hevy_exercise("T1", "Bench", [hevy_set(40, 10, "warmup")]),
        {"title": "No template", "sets": [hevy_set(50, 5)]},
        hevy_exercise("x" * 65, "Too long id", [hevy_set(50, 5)]),
        "not a dict",
        {**hevy_exercise("T2", "Row", [hevy_set(60, 10)]), "sets": [hevy_set(60, 10), "not a set"]},
    )
    assert [e["template_id"] for e in summarize_exercises(w)] == ["T2"]
    assert summarize_exercises({"exercises": "nope"}) == []
    assert summarize_exercises({}) == []
    assert summarize_exercises(workout({"exercise_template_id": "T3", "title": "Sets missing"})) == []


def test_summary_title_is_cut_to_200():
    (entry,) = summarize_exercises(one(hevy_set(50, 5), title="  " + "t" * 250 + "  "))
    assert entry["title"] == "t" * 200
    (entry,) = summarize_exercises(one(hevy_set(50, 5), title=None))
    assert entry["title"] is None


def test_exercises_json_is_canonical():
    text = exercises_json([{"b": 1, "a": 2}])
    assert text == '[{"a":2,"b":1}]'


def test_workout_day_in_the_athletes_zone():
    assert workout_day("2026-10-04T22:30:00Z", PRAGUE) == date(2026, 10, 5)  # 00:30 local
    assert workout_day("2026-10-05T08:00:00+02:00", PRAGUE) == date(2026, 10, 5)
    assert workout_day("2026-10-05T23:30:00", PRAGUE) == date(2026, 10, 6)  # no offset: UTC
    assert workout_day("x", PRAGUE) is None
    assert workout_day(None, PRAGUE) is None
    assert workout_day(20261005, PRAGUE) is None


EXTREME = ["9999-12-31T23:00:00-05:00", "0001-01-01T00:30:00+01:00", "9999-12-31T23:59:59", "0001-01-01T00:00:00+14:00"]


@pytest.mark.parametrize("text", EXTREME)
def test_extreme_timestamps_are_unusable_not_a_crash(text):
    # Converting them to another zone leaves the calendar (OverflowError): a skip, never an exception.
    assert workout_day(text, PRAGUE) is None
    if text != "9999-12-31T23:59:59":  # (valid in UTC, only the local day overflows)
        assert updated_key(text) is None


def test_an_extreme_start_time_skips_only_that_event():
    events = [
        wk("W1", "2026-10-05T10:00:00Z"),
        wk("EDGE", "9999-12-31T23:00:00-05:00"),
        wk("W2", "2026-10-06T10:00:00Z"),
    ]
    batch = parse_events(events, PRAGUE)
    assert list(batch.updates) == ["W1", "W2"] and batch.skipped == 1


def test_an_extreme_updated_at_keeps_the_workout_without_a_key():
    batch = parse_events([wk("W1", "2026-10-05T10:00:00Z", "9999-12-31T23:00:00-05:00")], PRAGUE)
    assert batch.skipped == 0 and batch.updates["W1"][1] is None and batch.updates["W1"][0] == date(2026, 10, 5)


def test_updated_key_is_fixed_width_utc():
    assert updated_key("2026-10-05T10:00:00+02:00") == "2026-10-05T08:00:00.000000Z"
    assert updated_key("2026-10-05T08:00:00.5Z") == "2026-10-05T08:00:00.500000Z"
    assert updated_key("2026-10-05T08:00:00") == "2026-10-05T08:00:00.000000Z"
    assert updated_key("nope") is None and updated_key(None) is None
    assert updated_key("2026-10-05T08:00:00Z") < updated_key("2026-10-05T08:00:01Z") < updated_key("2026-10-05T10:00:00Z")


# ------------------------------------------------------------------- events


def wk(id: str, start: str, updated_at: str | None = None, sets: int = 1) -> dict[str, Any]:
    return updated(hevy_workout(id, start, [hevy_exercise("T1", "Bench", [hevy_set(100, 5)] * sets)], updated_at=updated_at))


def test_parse_events_rules():
    events: list[Any] = [
        wk("W1", "2026-10-05T10:00:00Z", "2026-10-05T11:00:00Z", sets=2),
        deleted("W2"),
        {"type": "weird", "id": "W9"},  # unknown type
        updated({"start_time": "2026-10-05T10:00:00Z"}),  # no id
        updated(hevy_workout("x" * 65, "2026-10-05T10:00:00Z", [])),  # 65-char id
        "not a dict",
        wk("W3", "garbage"),  # unparsable start
        {"type": "updated", "workout": "not a dict"},
        {"type": "deleted", "id": "x" * 65},  # delete with an invalid id
        {"type": "deleted"},
    ]
    batch = parse_events(events, PRAGUE)
    assert batch.skipped == 8
    assert batch.deletes == ["W2"]
    assert list(batch.updates) == ["W1"]
    day, key, text = batch.updates["W1"]
    assert (day, key) == (date(2026, 10, 5), "2026-10-05T11:00:00.000000Z")
    assert json.loads(text)[0]["sets"] == 2


def test_parse_events_delete_overrides_an_update_of_the_same_id():
    batch = parse_events([wk("W1", "2026-10-05T10:00:00Z"), deleted("W1"), deleted("W1"), wk("W2", "2026-10-05T10:00:00Z")], PRAGUE)
    assert list(batch.updates) == ["W2"] and batch.deletes == ["W1"]
    batch = parse_events([deleted("W1"), wk("W1", "2026-10-05T10:00:00Z")], PRAGUE)  # the order does not matter
    assert batch.updates == {} and batch.deletes == ["W1"]


def test_parse_events_later_updated_at_wins_equal_or_unparsable_keys_keep_the_first():
    old, new = "2026-10-01T10:00:00Z", "2026-10-02T10:00:00Z"
    day_of = lambda b, i: b.updates[i][0]  # noqa: E731
    first_new = parse_events([wk("W1", "2026-10-05T10:00:00Z", new), wk("W1", "2026-10-06T10:00:00Z", old)], PRAGUE)
    assert day_of(first_new, "W1") == date(2026, 10, 5)
    last_new = parse_events([wk("W1", "2026-10-05T10:00:00Z", old), wk("W1", "2026-10-06T10:00:00Z", new)], PRAGUE)
    assert day_of(last_new, "W1") == date(2026, 10, 6)
    equal = parse_events([wk("W1", "2026-10-05T10:00:00Z", new), wk("W1", "2026-10-06T10:00:00Z", new)], PRAGUE)
    assert day_of(equal, "W1") == date(2026, 10, 5)
    unparsable = parse_events([wk("W1", "2026-10-05T10:00:00Z", None), wk("W1", "2026-10-06T10:00:00Z", new)], PRAGUE)
    assert day_of(unparsable, "W1") == date(2026, 10, 5)
    later_unparsable = parse_events([wk("W1", "2026-10-05T10:00:00Z", new), wk("W1", "2026-10-06T10:00:00Z", "bad")], PRAGUE)
    assert day_of(later_unparsable, "W1") == date(2026, 10, 5)
    assert first_new.skipped == 0


# ------------------------------------------------------------------- paging

FULL = [{}] * strength.EVENTS_PAGE_SIZE


@pytest.mark.parametrize(
    ("payload", "page", "expected"),
    [
        ({"events": "x"}, 1, "failed"),
        ({"page": 1}, 1, "failed"),  # events missing
        ({"events": [], "page": 2}, 1, "failed"),
        ({"events": [], "page": True}, 1, "failed"),  # a bool is not the page number (True == 1)
        ({"events": [{}], "page": 1, "page_count": "3"}, 1, "failed"),
        ({"events": [{}], "page": 1, "page_count": -1}, 1, "failed"),
        ({"events": [{}], "page": 1, "page_count": True}, 1, "failed"),
        ({"events": [{}], "page": 1, "page_count": 3.0}, 1, "failed"),
        ({"events": [{}], "page": 1, "page_count": None}, 1, "failed"),
        ({"events": [], "page": 1, "page_count": 0}, 1, "complete"),
        ({"events": [{}], "page": 2, "page_count": 2}, 2, "complete"),
        ({"events": [], "page": 3, "page_count": 2}, 3, "complete"),
        ({"events": [], "page": 1, "page_count": 3}, 1, "failed"),  # more pages announced but this one is empty
        ({"events": [{}], "page": 1, "page_count": 3}, 1, "next"),
        ({"events": FULL[:9]}, 1, "complete"),
        ({"events": [], "page": 1}, 1, "complete"),  # the empty page past the end
        ({"events": FULL, "page": 1}, 1, "next"),
        ({"events": FULL}, 1, "next"),
    ],
)
def test_page_step_table(payload, page, expected):
    assert page_step(payload, page) == expected


# ------------------------------------------------------------- sync window

FIRST = date(2026, 7, 10)


def state(covered_from: date = FIRST, synced_on: date = TODAY, synced_at: str | None = None) -> dict[str, str]:
    at = synced_at or datetime(synced_on.year, synced_on.month, synced_on.day, 1, 30, tzinfo=UTC).isoformat(timespec="seconds")
    return {"covered_from": covered_from.isoformat(), "synced_at": at, "synced_on": synced_on.isoformat()}


def test_listing_due_and_since():
    # never completed: a full listing from the local midnight of the first day
    since, full = listing_since(None, FIRST, PRAGUE)
    assert (since, full) == (datetime(2026, 7, 9, 22, 0, tzinfo=UTC), True)
    assert listing_due(None, FIRST, TODAY) is True
    assert since_text(since) == "2026-07-09T22:00:00Z"

    # synced today: nothing to do
    assert listing_due(state(), FIRST, TODAY) is False
    # synced yesterday: due, from the stored instant minus the 3-day overlap
    yesterday = state(synced_on=TODAY - timedelta(days=1))
    assert listing_due(yesterday, FIRST, TODAY) is True
    since, full = listing_since(yesterday, FIRST, PRAGUE)
    assert full is False and since == datetime(2026, 10, 6, 1, 30, tzinfo=UTC) - timedelta(days=3)
    # the range grew (first earlier than covered_from): due even if synced today, and a full listing
    grown = state(covered_from=date(2026, 7, 20))
    assert listing_due(grown, FIRST, TODAY) is True
    assert listing_since(grown, FIRST, PRAGUE) == (datetime(2026, 7, 9, 22, 0, tzinfo=UTC), True)
    # the range shrank: covered_from stays earlier than first, no full listing
    assert listing_due(state(covered_from=date(2026, 6, 1)), FIRST, TODAY) is False
    assert listing_since(state(covered_from=date(2026, 6, 1), synced_on=TODAY - timedelta(days=1)), FIRST, PRAGUE)[1] is False


def test_unusable_sync_state_counts_as_never_synced():
    for bad in (
        {"covered_from": "x", "synced_at": "2026-10-06T01:30:00+00:00", "synced_on": "2026-10-06"},
        {"covered_from": "2026-07-10", "synced_at": "2026-10-06T01:30:00", "synced_on": "2026-10-06"},  # naive
        {"covered_from": "2026-07-10"},
    ):
        assert listing_due(bad, FIRST, TODAY) is True
        assert listing_since(bad, FIRST, PRAGUE)[1] is True


def test_since_text_has_no_offset_or_fraction():
    assert since_text(datetime(2026, 7, 9, 22, 0, 5, 123, tzinfo=PRAGUE)) == "2026-07-09T20:00:05Z"


# --------------------------------------------------------------- read side


def row(day: str, *entries: dict[str, Any], workout_id: str = "W") -> dict[str, Any]:
    return {"workout_id": workout_id, "date": day, "exercises": exercises_json(entries)}


def found(group: str | None, type_: str = "weight_reps", title: str | None = None) -> dict[str, Any]:
    return {"found": 1, "primary_muscle_group": group, "type": type_, "title": title}


def test_week_sums_only_covered_days_and_keeps_the_best_per_template():
    templates = Templates({"A": found("chest", title="Bench")})
    rows = [
        row("2026-10-05", exercise("A", 3, 1000.0, (100, 5, 116.7)), workout_id="W1"),
        row("2026-10-07", exercise("A", 3, 1100.0, (105, 5, 122.5)), workout_id="W2"),
        row("2026-10-09", exercise("A", 3, 1200.0, (105, 5, 122.5)), workout_id="W3"),
        row("2026-10-12", exercise("A", 9, 9999.0, (200, 1, 200.0)), workout_id="W4"),  # next week
    ]
    totals = day_totals(rows, templates)
    week = strength.StrengthWeek(date(2026, 10, 5), totals, Coverage(date(2026, 10, 6), date(2026, 10, 20)))
    assert week.days == 6 and week.complete is False  # Monday is not covered
    assert (week.workouts, week.sets, week.volume_kg) == (2, 6, 2300.0)  # Monday's workout is left out
    best, day, title = week.best["A"]
    assert (best["e1rm_kg"], day, title) == (122.5, date(2026, 10, 7), "Bench")  # a tie keeps the earlier day
    full = strength.StrengthWeek(date(2026, 10, 5), totals, Coverage(date(2026, 9, 1), date(2026, 10, 11)))
    assert full.days == 7 and full.complete is True and full.sets == 9


# --------------------------------------------------------- get_strength_progress


def seed_series(store, tid: str, points: list[tuple[str, float]], *, prefix: str = "") -> None:
    for n, (day, estimate) in enumerate(points):
        seed_strength(store, f"{prefix}{tid}-{n}", date.fromisoformat(day), [exercise(tid, 3, 1500.0, (100, 5, estimate), title="Stored")])


def progress(store, **kwargs: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"exercise": None, "start_date": None, "end_date": None, "plateau_weeks": 4, "limit": 10}
    return get_strength_progress(store, TODAY, **{**args, **kwargs})


def synced(store) -> None:
    seed_sync(store, date(2026, 7, 1), TODAY)


def test_strength_progress_series_and_summary(store):
    seed_template(store, "T1", "chest", title="Bench Press (Barbell)")
    seed_series(
        store,
        "T1",
        [("2026-09-14", 100.0), ("2026-09-21", 105.0), ("2026-09-28", 110.0), ("2026-10-02", 108.0), ("2026-10-05", 112.0)],
    )
    synced(store)
    got = progress(store)
    assert (got["start"], got["end"], got["plateau_weeks"], got["available"]) == ("2026-07-09", "2026-10-06", 4, True)
    assert got["coverage"] == {"from": "2026-07-01", "through": "2026-10-06"} and got["matched"] == 1
    (ex,) = got["exercises"]
    assert ex["template_id"] == "T1" and ex["title"] == "Bench Press (Barbell)" and ex["muscle_group"] == "chest"
    assert ex["sessions"] == 5
    assert [p["date"] for p in ex["e1rm_kg"]] == ["2026-09-14", "2026-09-21", "2026-09-28", "2026-10-02", "2026-10-05"]
    assert [p["e1rm_kg"] for p in ex["e1rm_kg"]] == [100.0, 105.0, 110.0, 108.0, 112.0]
    assert ex["e1rm_kg"][0] == {"date": "2026-09-14", "e1rm_kg": 100.0, "weight_kg": 100.0, "reps": 5}
    assert ex["best"] == {"date": "2026-10-05", "e1rm_kg": 112.0, "weight_kg": 100.0, "reps": 5}
    assert (ex["first_e1rm_kg"], ex["last_e1rm_kg"], ex["change_kg"], ex["change_pct"]) == (100.0, 112.0, 12.0, 12.0)
    assert ex["weeks_since_best"] == 0 and ex["plateau"] is False


def test_strength_progress_best_on_a_tie_is_the_earliest_and_weeks_since_best(store):
    seed_template(store, "T1", "chest", title="Bench")
    seed_series(store, "T1", [("2026-08-03", 110.0), ("2026-09-01", 110.0), ("2026-09-29", 100.0)])
    synced(store)
    (ex,) = progress(store)["exercises"]
    assert ex["best"]["date"] == "2026-08-03"
    assert ex["weeks_since_best"] == (date(2026, 10, 6) - date(2026, 8, 3)).days // 7 == 9
    assert ex["change_kg"] == -10.0 and ex["change_pct"] == -9.1


def test_strength_progress_single_session_has_no_change_pct(store):
    seed_template(store, "T1", "chest", title="Bench")
    seed_series(store, "T1", [("2026-10-05", 100.0)])
    synced(store)
    (ex,) = progress(store)["exercises"]
    assert (ex["sessions"], ex["change_kg"], ex["change_pct"], ex["plateau"]) == (1, 0.0, None, False)


def test_strength_progress_range_arguments_and_exercises_without_points(store):
    seed_template(store, "T1", "chest", title="Bench")
    seed_series(store, "T1", [("2026-09-14", 100.0), ("2026-10-05", 112.0)])
    seed_strength(store, "assist", date(2026, 10, 5), [exercise("T9", 3, 0.0, None, title="No best")])  # no point
    synced(store)
    got = progress(store, start_date="2026-09-20", end_date="2026-10-05")
    assert (got["start"], got["end"]) == ("2026-09-20", "2026-10-05")
    assert [e["template_id"] for e in got["exercises"]] == ["T1"]
    assert [p["date"] for p in got["exercises"][0]["e1rm_kg"]] == ["2026-10-05"]


def test_plateau_flag_branches(store):
    seed_template(store, "T1", "chest", title="Bench")
    synced(store)

    case = {"n": 0}

    def plateau(points: list[tuple[str, float]], weeks: int = 4) -> bool:
        case["n"] += 1
        ids = [f"c{case['n']}-{n}" for n in range(len(points))]
        for workout_id, (day, estimate) in zip(ids, points, strict=True):
            seed_strength(store, workout_id, date.fromisoformat(day), [exercise("T1", 3, 1500.0, (100, 5, estimate))])
        (ex,) = progress(store, plateau_weeks=weeks)["exercises"]
        store.apply_strength_events({}, ids)  # a tombstone takes the case out of the next one
        return ex["plateau"]

    # the window of 4 weeks ending 2026-10-06 starts 2026-09-09
    assert plateau([("2026-09-01", 100.0), ("2026-09-15", 100.0), ("2026-09-30", 110.0)]) is False  # improved inside
    assert plateau([("2026-09-01", 110.0), ("2026-09-15", 100.0), ("2026-09-30", 110.0)]) is True  # equal
    assert plateau([("2026-09-01", 110.0), ("2026-09-15", 100.0), ("2026-09-30", 90.0)]) is True  # lower
    assert plateau([("2026-09-01", 110.0), ("2026-09-30", 100.0)]) is False  # a single session in the window
    assert plateau([("2026-09-15", 100.0), ("2026-09-30", 100.0)]) is False  # nothing before the window
    assert plateau([("2026-09-08", 110.0), ("2026-09-09", 100.0), ("2026-09-30", 100.0)]) is True  # window starts 09-09
    assert plateau([("2026-09-08", 110.0), ("2026-09-08", 100.0)]) is False
    # plateau_weeks changes the result: 2 weeks start 2026-09-23, 6 weeks start 2026-08-26
    series = [("2026-09-01", 100.0), ("2026-09-15", 105.0), ("2026-09-25", 104.0), ("2026-10-02", 103.0)]
    assert plateau(series, weeks=2) is True
    assert plateau(series, weeks=6) is False


def test_strength_progress_filters_orders_and_limits(store):
    seed_template(store, "A1", "chest", title="Bench Press")
    seed_template(store, "B2", "quadriceps", title="Squat")
    seed_template(store, "C3", "triceps", title="Bench Dip")
    seed_series(store, "A1", [("2026-09-14", 100.0), ("2026-09-21", 105.0), ("2026-09-28", 110.0)])
    seed_series(store, "B2", [("2026-09-14", 150.0), ("2026-09-21", 155.0)])
    seed_series(store, "C3", [("2026-09-14", 60.0)])
    synced(store)

    def ids(**kwargs: Any) -> list[str]:
        return [e["template_id"] for e in progress(store, **kwargs)["exercises"]]

    assert ids() == ["A1", "B2", "C3"]  # most trained first
    assert ids(limit=2) == ["A1", "B2"] and progress(store, limit=2)["matched"] == 3
    assert ids(exercise="B2") == ["B2"]
    assert ids(exercise="b2") == []  # an id matches exactly; "b2" is not part of a title either
    assert ids(exercise="bench") == ["A1", "C3"] and ids(exercise="BENCH") == ["A1", "C3"]
    assert ids(exercise="  squat ") == ["B2"]
    assert ids(exercise="   ") == ["A1", "B2", "C3"]
    got = progress(store, exercise="deadlift")
    assert got["available"] is True and got["exercises"] == [] and got["matched"] == 0


def test_strength_progress_orders_equal_sessions_by_title_then_id(store):
    seed_template(store, "Z1", "chest", title="Apple")
    seed_template(store, "A9", "chest", title="Banana")
    seed_template(store, "A1", "chest", title="Banana")
    for tid in ("Z1", "A9", "A1"):
        seed_series(store, tid, [("2026-09-14", 100.0)])
    synced(store)
    assert [e["template_id"] for e in progress(store)["exercises"]] == ["Z1", "A1", "A9"]


def test_strength_progress_without_sync_is_unavailable(store):
    seed_template(store, "T1", "chest", title="Bench")
    seed_series(store, "T1", [("2026-09-14", 100.0)])
    assert progress(store) == {
        "start": "2026-07-09",
        "end": "2026-10-06",
        "plateau_weeks": 4,
        "available": False,
        "reason": "strength history from Hevy has not been collected yet",
        "coverage": None,
        "exercises": [],
    }


def test_strength_progress_date_errors(store):
    synced(store)
    with pytest.raises(ToolError, match="end_date must not be after today"):
        progress(store, end_date="2026-10-08")
    with pytest.raises(ToolError, match="start_date must be on or before end_date"):
        progress(store, start_date="2026-10-06", end_date="2026-10-05")
    with pytest.raises(ToolError, match=r"date range too long \(max 731 days\)"):
        progress(store, start_date="2024-10-05", end_date="2026-10-06")  # 732 days
    assert progress(store, start_date="2024-10-06", end_date="2026-10-06")["available"] is True  # 731 days
    with pytest.raises(ToolError, match="start_date"):
        progress(store, start_date="yesterday")
