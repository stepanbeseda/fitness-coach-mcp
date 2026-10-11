"""The strength exercise catalogue search (PR 07). Expected values hold for the catalogue of the pinned
garminconnect release (0.3.17) and are refreshed on a library upgrade."""

import pytest
from garminconnect import exercises as catalogue

from garmin_coach_mcp import exercises as x


def first(query: str) -> tuple[str, str, str, float]:
    match = x.find(query, 5)["matches"][0]
    return match["exercise"], match["category"], match["name"], match["score"]


@pytest.mark.parametrize(
    "query, expected",
    [
        ("Bench Press (Barbell)", ("Barbell Bench Press", "BENCH_PRESS", "BARBELL_BENCH_PRESS", 1.0)),
        ("Lat Pulldown (Cable)", ("Lat Pull-down", "PULL_UP", "LAT_PULLDOWN", 0.875)),
        ("pullup", ("Pull-up", "PULL_UP", "PULL_UP", 1.0)),
        ("Pull Up", ("Pull-up", "PULL_UP", "PULL_UP", 1.0)),
        ("Crunches", ("Crunch", "CRUNCH", "CRUNCH", 1.0)),  # ches
        ("Bench Presses", ("Bench Press", "BENCH_PRESS", "BENCH_PRESS", 1.0)),  # sses
        ("Ab Stretch", ("Abs Stretch", "WARM_UP", "STRETCH_ABS", 1.0)),
        ("PLANK", ("Plank", "PLANK", "PLANK", 1.0)),
        ("Triceps Pushdown", ("Triceps Press-down", "TRICEPS_EXTENSION", "TRICEPS_PRESSDOWN", 0.747)),
        ("Pushes", ("Push-up", "PUSH_UP", "PUSH_UP", 0.721)),  # shes
        ("Boxes", ("Box Jump", "PLYO", "BOX_JUMP", 0.708)),  # xes
    ],
)
def test_e1_first_match(query, expected):
    assert first(query) == expected


@pytest.mark.parametrize(
    "query, expected",
    [
        ("Tbar Row", ("T-Bar Row", "ROW", "T_BAR_ROW", 1.0)),  # a one-character part of the compound
        ("walkoutside", ("Walk", "RUN", "WALK", 0.602)),  # the first split from the left; the last would be Walkout
    ],
)
def test_e8_compound_split(query, expected):
    assert first(query) == expected


def test_e2_rare_words_decide():
    matches = x.find("Romanian Deadlift (Barbell)", 2)["matches"]
    assert [(m["exercise"], m["score"]) for m in matches] == [("Romanian Deadlift", 0.874), ("Barbell Deadlift", 0.699)]


@pytest.mark.parametrize("query", ["xyzzy", "???", "qwertyuiop asdf", "Pec Deck", "", "   "])
def test_e3_nonsense_has_no_matches(query):
    assert x.find(query, 5) == {"query": query, "count": 0, "matches": []}


def test_e4_limit_and_order():
    out = x.find("plank", 20)
    assert out["count"] == 20 and len(out["matches"]) == 20
    scores = [m["score"] for m in out["matches"]]
    assert scores == sorted(scores, reverse=True)
    assert out["matches"][0]["exercise"] == "Plank"
    assert x.find("plank", 1)["count"] == 1


def test_e5_ties_shorter_name_first_then_by_name():
    squat = x.find("Squat", 20)["matches"][3:5]
    assert [(m["exercise"], m["score"]) for m in squat] == [("Jump Squat", 0.591), ("Banded Squat", 0.591)]
    barbell = x.find("Barbell", 20)["matches"][5:7]
    assert [(m["exercise"], m["score"]) for m in barbell] == [("Barbell Burpee", 0.55), ("Barbell Snatch", 0.55)]
    assert len(barbell[0]["exercise"]) == len(barbell[1]["exercise"])


def test_e6_threshold(monkeypatch):
    out = x.find("Lying Leg Curl (Machine)", 1000)
    assert out["count"] == 19
    assert (out["matches"][-1]["exercise"], out["matches"][-1]["score"]) == ("Incline Smith Machine Bench Press", 0.302)
    monkeypatch.setattr(x, "MIN_SCORE", 0.302)
    assert x.find("Lying Leg Curl (Machine)", 1000)["count"] == 19  # a score equal to the threshold is kept
    monkeypatch.setattr(x, "MIN_SCORE", 0.303)
    assert x.find("Lying Leg Curl (Machine)", 1000)["count"] == 18


def test_e7_display_name():
    assert x.display_name("BENCH_PRESS", "BARBELL_BENCH_PRESS") == "Barbell Bench Press"
    assert x.display_name("PULL_UP", "BARBELL_BENCH_PRESS") is None
    assert x.display_name(None, None) is None
    assert x.display_name("PLANK", 5) is None
    assert x.display_name(["PLANK"], {}) is None  # unhashable: a stored step with an odd value must not raise
    assert len(x._DISPLAY) == len(catalogue.EXERCISES)  # no (category, name) pair is lost to a duplicate
