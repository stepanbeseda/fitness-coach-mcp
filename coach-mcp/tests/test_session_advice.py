"""The per-session advice module (PR 41): classifier, matrix, set reduction, floors. Pure, no fixtures."""

from __future__ import annotations

import pytest

from briefing_helpers import GREEN_SESSION
from coach_mcp.session_advice import (
    ACTIONS,
    CLASSES,
    MATRIX,
    NO_FLOORS,
    REASONS,
    classify,
    reduced_sets,
    session_advice,
)

STRENGTH_DW = {
    "id": 2,
    "date": "2026-10-07",
    "weekday": "Wed",
    "type": "strength",
    "description": "bench 3×8 @ 75 kg",
    "intensity": "RPE 8",
    "status": "planned",
}
EASY = GREEN_SESSION
HARD = {
    **GREEN_SESSION,
    "id": 5,
    "description": "Intervaly 6×800 m",
    "intensity": "Z4",
    "duration_min": None,
    "distance_km": None,
}
MOBILITY = {
    **GREEN_SESSION,
    "id": 6,
    "type": "mobility",
    "description": "Mobilita 15 min",
    "duration_min": None,
    "distance_km": None,
}
BY_CLASS = {
    "easy_endurance": EASY,
    "hard_endurance": HARD,
    "strength": STRENGTH_DW,
    "mobility_other": MOBILITY,
}

# The live plan of the athlete (13 sessions, every one planned), copied from the plan table.
LIVE_SESSIONS = [
    {"id": 1, "type": "strength", "intensity": "RPE 7–8", "description": "Síla A – horní tělo + trup (bench 3×8 @ 55 kg, lat pulldown 3×10 @ 62,5 kg, incline row 3×12 @ 20 kg, tlaky na ramena, boky, ruce, face pull, dead bug, side plank)", "status": "planned"},
    {"id": 2, "type": "strength", "intensity": "RPE 7", "description": "Síla B – nohy a hýždě bez osové zátěže + trup (hip thrust 3×10 @ 60 kg, bulharský dřep, goblet dřep, pull through, lýtka, Pallof, bird dog)", "status": "planned"},
    {"id": 3, "type": "mobility", "intensity": "lehce", "description": "Volno, případně procházka nebo 15 min mobility", "status": "planned"},
    {"id": 4, "type": "running", "intensity": "Z2", "description": "Dlouhý běh 9 km Z2 (116–134 bpm)", "status": "planned"},
    {"id": 5, "type": "running", "intensity": "Z2", "description": "Lehký běh 6 km Z2 (116–134 bpm)", "status": "planned"},
    {"id": 6, "type": "strength", "intensity": "RPE 7–8", "description": "Síla C – horní tělo + trup (incline bench 3×8 @ 45 kg, jednoruční přítah kladky, úzký pulldown, dipy, boky, EZ curl, triceps nad hlavou, face pull, side plank, Pallof)", "status": "planned"},
    {"id": 7, "type": "running", "intensity": "Z2, rovinky svižně", "description": "Lehký běh 6 km Z2 + 6× 20 s rovinky", "status": "planned"},
    {"id": 8, "type": "running", "intensity": "Z2", "description": "Lehký běh 5 km Z2 (116–134 bpm)", "status": "planned"},
    {"id": 9, "type": "running", "intensity": "Z2", "description": "Lehký běh 6 km Z2 (116–134 bpm)", "status": "planned"},
    {"id": 10, "type": "strength", "intensity": "RPE 8", "description": "Síla A – horní tělo + trup (bench 3×8 @ 75 kg, lat pulldown 3×8 @ 70 kg, row 3×12 @ 40 kg, shoulder press 3×10 @ 40 kg, boky, ruce, face pull, trup)", "status": "planned"},
    {"id": 11, "type": "strength", "intensity": "RPE 8", "description": "Síla B – nohy a hýždě + trup (hip thrust 3×8 @ 92,5 kg, bulharský dřep 3×10 @ 30 kg, goblet 3×12 @ 25 kg, pull through, lýtka, Pallof, bird dog)", "status": "planned"},
    {"id": 12, "type": "strength", "intensity": "RPE 8", "description": "Síla C – horní tělo + trup (incline bench 3×8 @ 62,5 kg, jednoruční přítah, úzký pulldown, dipy, boky, EZ curl, triceps, face pull, trup)", "status": "planned"},
    {"id": 13, "type": "running", "intensity": "Z2", "description": "Dlouhý běh 8 km Z2 (116–134 bpm)", "status": "planned"},
]
LIVE_CLASSES = {
    1: "strength",
    2: "strength",
    3: "mobility_other",
    4: "easy_endurance",
    5: "easy_endurance",
    6: "strength",
    7: "hard_endurance",
    8: "easy_endurance",
    9: "easy_endurance",
    10: "strength",
    11: "strength",
    12: "strength",
    13: "easy_endurance",
}


def test_contract_sets_are_closed_and_ordered():
    assert ACTIONS == ("ok", "reduce", "swap")
    assert CLASSES == ("easy_endurance", "hard_endurance", "strength", "mobility_other")
    assert REASONS == ("verdict",)  # PR 44 extends it
    assert set(MATRIX) == {"yellow", "red"}
    for row in MATRIX.values():
        assert tuple(row) == CLASSES
    assert dict(NO_FLOORS) == {}


MATRIX_ROWS = [
    ("green", "easy_endurance", None),
    ("green", "hard_endurance", None),
    ("green", "strength", None),
    ("green", "mobility_other", None),
    ("yellow", "easy_endurance", "ok"),
    ("yellow", "hard_endurance", "reduce"),
    ("yellow", "strength", "reduce"),
    ("yellow", "mobility_other", "ok"),
    ("red", "easy_endurance", "reduce"),
    ("red", "hard_endurance", "swap"),
    ("red", "strength", "swap"),
    ("red", "mobility_other", "ok"),
]
CHANGES = {
    ("yellow", "hard_endurance"): {"zone_max": 2},
    ("yellow", "strength"): {"rpe_max": 7, "sets": [{"from": "3×8", "to": "2×8"}], "minus_one_set": False},
    ("red", "easy_endurance"): {"zone_max": 2, "max_minutes": 40},
}


@pytest.mark.parametrize(("level", "cls", "expected"), MATRIX_ROWS)
def test_matrix_cells(level, cls, expected):
    session = BY_CLASS[cls]
    advice = session_advice(level, session)
    assert (advice.action if advice else None) == expected
    if advice is None:
        return
    assert advice.cls == cls
    assert advice.reason == "verdict"
    assert advice.session_id == session["id"]
    if advice.action == "ok":
        assert advice.change == {}
    elif advice.action == "swap":
        assert advice.change == {"to": "recovery"}
    else:
        assert advice.change == CHANGES[(level, cls)]


@pytest.mark.parametrize("session", LIVE_SESSIONS, ids=[str(s["id"]) for s in LIVE_SESSIONS])
def test_classify_live_shapes(session):
    assert classify(session) == LIVE_CLASSES[session["id"]]


EDGE_ROWS = [
    ("running", "RPE 7–8", "Běh", "hard_endurance"),  # unreadable counts as hard
    ("running", None, "Běh 10 km", "hard_endurance"),
    ("running", "Z2", "Běh 6 km", "easy_endurance"),  # a zone alone
    ("running", "easy", "Run", "easy_endurance"),
    ("running", None, "Intervaly 6×800 m", "hard_endurance"),
    ("cycling", "Z1–Z2", "Kolo", "easy_endurance"),
    ("swimming", None, "Tempo 1 km", "hard_endurance"),
    ("running", None, "zóna 3", "hard_endurance"),
    ("running", None, "zóně 1–2 lehce", "easy_endurance"),
    ("running", "Z2-3", "Běh", "hard_endurance"),
    ("cycling", "zone 4", "Ride", "hard_endurance"),
    ("running", None, "Fartlek 5 km", "hard_endurance"),
    ("running", None, "Praha běh 6 km", "hard_endurance"),  # no "prah" false hit: unreadable
    ("other", "tempo", "Lezení", "mobility_other"),
    ("mobility", None, "Jóga", "mobility_other"),
    ("running", None, "6 km recovery", "easy_endurance"),
    ("running", "Strides", "Easy 5 km + strides", "hard_endurance"),  # a hard word beats an easy one
    ("unknown", None, "x", "mobility_other"),
    ("running", "lehce", "Běh", "easy_endurance"),  # "lehce" as an intensity
    # Known limit (conservative side): spaces around the dash make "Z2 - 5" a zone range 2-5, so it reads as hard.
    ("running", None, "Běh Z2 - 5 km", "hard_endurance"),
    ("running", "Z2", "Běh 3×2 km", "easy_endurance"),  # a km scheme is not a zone
    ("running", "Z2", "Intervaly 4 km", "hard_endurance"),  # hard stem "interval" beats zone 2
    ("running", "Z2", "Tempo 4 km", "hard_endurance"),  # hard stem "tempo" beats zone 2
    ("running", "Z2", "Závod 5 km", "hard_endurance"),  # hard stem "závod" beats zone 2
    ("running", "Z2", "Race 5 km", "hard_endurance"),  # hard stem "race" beats zone 2
    ("running", "Z2", "Fartlek 5 km", "hard_endurance"),  # hard stem "fartlek" beats zone 2
    ("running", "Z2", "Threshold 3×1 km", "hard_endurance"),  # hard stem "threshold" beats zone 2
    ("running", "Z2", "Práh 4 km", "hard_endurance"),  # hard stem "práh" beats zone 2
    ("running", "Z2", "Prahový běh 4 km", "hard_endurance"),  # hard stem "prahov" beats zone 2
    ("running", "Z2", "Sprint 6×100 m", "hard_endurance"),  # hard stem "sprint" beats zone 2
    ("running", "Z2", "Kopce 5×1 min", "hard_endurance"),  # hard stem "kopc" beats zone 2
    ("running", "Z2", "Hill repeats", "hard_endurance"),  # hard stem "hill" beats zone 2
    ("running", None, "Lehký běh 6 km", "easy_endurance"),  # easy stem "lehk" without a zone
    ("running", None, "Regenerace", "easy_endurance"),  # easy stem "regener" without a zone
    ("running", None, "Volně 5 km", "easy_endurance"),  # easy stem "volně" without a zone
    ("running", None, "Pohodově 5 km", "easy_endurance"),  # easy stem "pohodov" without a zone
    ("running", None, "Z4 3 km, Z1 vyklus", "hard_endurance"),  # a zone above 2 anywhere, not only the last
]


@pytest.mark.parametrize(("kind", "intensity", "description", "expected"), EDGE_ROWS)
def test_classify_edge_shapes(kind, intensity, description, expected):
    session = {"type": kind, "description": description}
    if intensity is not None:
        session["intensity"] = intensity
    assert classify(session) == expected


def test_classify_without_a_type_is_mobility_other():
    assert classify({"description": "Běh", "intensity": "Z2"}) == "mobility_other"
    assert session_advice("red", {"id": 9, "status": "planned", "description": "x"}).action == "ok"


def _pairs(*pairs):
    return [{"from": a, "to": b} for a, b in pairs]


SCHEME_ROWS = [
    ("bench 3×8 @ 75 kg", _pairs(("3×8", "2×8")), False),
    ("Lehký běh 6 km Z2 + 6× 20 s rovinky", [], True),
    ("3x10 dřep, 3 x 12 výpady", _pairs(("3×10", "2×10"), ("3×12", "2×12")), False),
    ("4 × 8–10 přítah", _pairs(("4×8–10", "3×8–10")), False),
    ("1×5 deadlift", [], False),
    ("deadlift 1×5, bench 3×5", _pairs(("3×5", "2×5")), False),
    ("6×800 m", [], True),
    ("4x4 min", [], True),
    ("Celé tělo", [], True),
    ("12,5x3", [], True),
    ("3×8-10 bench", _pairs(("3×8–10", "2×8–10")), False),  # the hyphen becomes an en dash
    ("bench 3 × 8 – 10 @ 60", _pairs(("3×8–10", "2×8–10")), False),  # whitespace inside the range goes
    ("3×2 km", [], True),  # a km unit is not reps
    ("výpady 3×2 h", [], True),  # nor an h unit
    # The unit guard holds for a rep range too: the range is not cut back to its first number.
    ("4×8–10 min výpady", [], True),
    ("3×30–45 s plank", [], True),
    ("4 × 8 – 10 min", [], True),
    (
        "5×5, 5×5, 5×5, 4×6, 3×8, 3×10",
        _pairs(("5×5", "4×5"), ("4×6", "3×6"), ("3×8", "2×8"), ("3×10", "2×10")),
        False,
    ),
    # Known limit: the Czech preposition "s" after the reps reads as seconds, so the text is generic.
    ("3×10 s jednoručkou", [], True),
    (
        "5×5, 4×6, 3×8, 3×10, 2×12",  # at most four pairs: the fifth is cut
        _pairs(("5×5", "4×5"), ("4×6", "3×6"), ("3×8", "2×8"), ("3×10", "2×10")),
        False,
    ),
    ("dřep 3×8, bench 3×8", _pairs(("3×8", "2×8")), False),  # distinct pairs only
    ("3×8, 3×8,5", _pairs(("3×8", "2×8")), False),
    # The live "Síla A" (session 10): three pairs in text order.
    (LIVE_SESSIONS[9]["description"], _pairs(("3×8", "2×8"), ("3×12", "2×12"), ("3×10", "2×10")), False),
]


@pytest.mark.parametrize(("description", "pairs", "generic"), SCHEME_ROWS)
def test_reduced_sets(description, pairs, generic):
    assert reduced_sets(description) == (pairs, generic)


def test_only_planned_sessions_get_advice():
    for status in ("done", "skipped", "moved", "superseded"):
        assert session_advice("red", {**STRENGTH_DW, "status": status}) is None
    assert session_advice("red", {k: v for k, v in STRENGTH_DW.items() if k != "status"}) is None
    planned = session_advice("red", {**STRENGTH_DW, "status": "planned"})
    assert planned is not None and planned.action == "swap"


def test_floors_replace_the_action_when_at_least_as_strong():
    # "floor" is a test-only reason; tests are outside mypy.
    swap = {"strength": ("swap", "floor")}
    advice = session_advice("green", STRENGTH_DW, floors=swap)
    assert (advice.action, advice.reason, advice.change) == ("swap", "floor", {"to": "recovery"})
    advice = session_advice("yellow", STRENGTH_DW, floors=swap)
    assert (advice.action, advice.reason) == ("swap", "floor")
    # A tie: the floor's reason wins (the ruling of PR 41/44).
    advice = session_advice("red", STRENGTH_DW, floors=swap)
    assert (advice.action, advice.reason, advice.change) == ("swap", "floor", {"to": "recovery"})
    advice = session_advice("yellow", STRENGTH_DW, floors={"strength": ("reduce", "floor")})
    assert (advice.action, advice.reason) == ("reduce", "floor")
    assert advice.change == CHANGES[("yellow", "strength")]
    # A reduce floor applies on green too: a missing cell counts as "ok", the floor is stronger.
    advice = session_advice("green", STRENGTH_DW, floors={"strength": ("reduce", "floor")})
    assert (advice.action, advice.reason, advice.change) == ("reduce", "floor", CHANGES[("yellow", "strength")])
    # A weaker floor is ignored.
    advice = session_advice("red", STRENGTH_DW, floors={"strength": ("reduce", "floor")})
    assert (advice.action, advice.reason) == ("swap", "verdict")
    # An "ok" floor never applies: not on a green day, not on a tie with an "ok" cell.
    assert session_advice("green", STRENGTH_DW, floors={"strength": ("ok", "floor")}) is None
    advice = session_advice("yellow", MOBILITY, floors={"mobility_other": ("ok", "floor")})
    assert (advice.action, advice.reason) == ("ok", "verdict")
    # A reduce floor reaches a class whose cell is "ok" and changes nothing for it.
    advice = session_advice("yellow", MOBILITY, floors={"mobility_other": ("reduce", "floor")})
    assert (advice.action, advice.reason, advice.change) == ("reduce", "floor", {})
    # A floor for another class leaves the session alone.
    advice = session_advice("yellow", STRENGTH_DW, floors={"hard_endurance": ("swap", "floor")})
    assert (advice.action, advice.reason) == ("reduce", "verdict")


def test_advice_payload_shape():
    assert session_advice("yellow", STRENGTH_DW).payload() == {
        "session_id": 2,
        "action": "reduce",
        "class": "strength",
        "reason": "verdict",
        "change": {"rpe_max": 7, "sets": [{"from": "3×8", "to": "2×8"}], "minus_one_set": False},
    }


def test_payload_does_not_share_change_lists_with_the_advice():
    advice = session_advice("yellow", STRENGTH_DW)
    payload = advice.payload()
    payload["change"]["sets"].append({"from": "x", "to": "y"})
    payload["change"]["rpe_max"] = 1
    assert advice.change["sets"] == [{"from": "3×8", "to": "2×8"}]
    assert advice.change["rpe_max"] == 7
