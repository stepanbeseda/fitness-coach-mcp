"""The English catalogue (the default language): the same scenarios as the exact Czech rendering tests."""

from __future__ import annotations

import pytest

from briefing_helpers import GREEN_SESSION
from coach_mcp import briefing
from coach_mcp.briefing import build_briefing
from coach_mcp.messages import EN, LANGUAGES
from coach_mcp.session_advice import ACTIONS, CLASSES, REASONS, Advice
from coach_mcp.session_advice import _change  # the change shape per (action, class), as a floor can produce it
from garmin_payloads import DAY, GREEN_SNAPSHOT, device_status, garmin_data, yellow_data

GREEN_TITLE = "\U0001f7e2 GREEN · morning briefing 7 Oct"
GREEN_MESSAGE = """\
Verdict: green – full plan, you can add progression.
Readiness: 78 (green)
HRV: 62 ms, baseline 52–68 ms (green)
Sleep: 7 h 30 min, score 84 (green)
Body Battery at wake: 81 (green)
Today's plan:
- run: 10 km lehce, zóna 2 (60 min, 10 km)"""

YELLOW_TITLE = "\U0001f7e1 ⚠️ YELLOW · morning briefing 7 Oct"
YELLOW_MESSAGE = """\
Verdict: yellow – cut volume by 20–30 %, intensity at most RPE 7, cardio in zone 2.
Readiness: 61 (yellow)
HRV: missing
Sleep: 6 h 15 min, score 70 (yellow)
Body Battery at wake: missing
Missing: HRV, Body Battery (the watch may not have synced yet). Without these data the verdict cannot be green.
For full load: readiness at least 70; sleep at least 7 h.
Today in Garmin: Lehký běh 40 min
Alerts:
- Resting HR 58 bpm, 6 above the 7-day average of 52 bpm.
- Yesterday's scheduled workout was not completed: Intervaly 6×800 m."""

YELLOW_ROWS = {
    "resting_hr_elevated": {"value": 0, "evaluated_on": "2026-10-06", "fired_on": None},
    "acwr_out_of_range": {"value": 1, "evaluated_on": "2026-10-06", "fired_on": "2026-10-04"},
    "garmin_auth": {"value": 0, "evaluated_on": "2026-10-06", "fired_on": None},
}


def test_english_is_the_default():
    assert build_briefing(DAY, garmin_data(), [GREEN_SESSION], {}).message.title == GREEN_TITLE


def test_render_green_exact():
    b = build_briefing(DAY, garmin_data(), [GREEN_SESSION], {}, language="en")
    assert (b.message.title, b.message.body) == (GREEN_TITLE, GREEN_MESSAGE)


def test_render_yellow_with_missing_and_alerts_exact():
    b = build_briefing(DAY, yellow_data(), [], YELLOW_ROWS, language="en")
    assert (b.message.title, b.message.body) == (YELLOW_TITLE, YELLOW_MESSAGE)


def test_language_changes_only_the_text():
    sessions = [dict(STRENGTH_DW)]
    en = build_briefing(DAY, yellow_data(), sessions, YELLOW_ROWS, language="en")
    cs = build_briefing(DAY, yellow_data(), sessions, YELLOW_ROWS, language="cs")
    assert en.payload["session_advice"]  # the comparison below covers the advice entries
    assert en.verdict == cs.verdict and en.waiting == cs.waiting and en.alert_updates == cs.alert_updates
    assert {k: v for k, v in en.payload.items() if k != "push"} == {k: v for k, v in cs.payload.items() if k != "push"}


@pytest.mark.parametrize("texts", list(LANGUAGES.values()))
def test_every_catalogue_names_every_key(texts):
    for mapping, keys in [
        (texts.level, EN.level),
        (texts.criterion, EN.criterion),
        (texts.session_type, EN.session_type),
        (texts.status_suffix, EN.status_suffix),
        (texts.advice, EN.advice),
        (texts.acwr, EN.acwr),
        (texts.session_advice, EN.session_advice),
        (texts.advice_reason, EN.advice_reason),
    ]:
        assert set(mapping) == set(keys)
    assert set(EN.session_advice) == {
        "ok",
        "swap",
        "reduce_easy_endurance",
        "reduce_hard_endurance",
        "reduce_strength",
        "reduce_rpe",
        "reduce_mobility_other",
    }
    assert set(EN.advice_reason) == set(REASONS)


def _missing(local: str, utc: str) -> str:
    snapshot = {k: v for k, v in GREEN_SNAPSHOT.items() if k != "sleep"}
    data = garmin_data(snapshot=snapshot, device=device_status(utc, local))
    body = build_briefing(DAY, data, [], {}, language="en").message.body
    return next(ln for ln in body.splitlines() if ln.startswith("Missing:"))


@pytest.mark.parametrize(
    ("local", "utc", "when"),
    [
        ("2026-10-07T05:12", "2026-10-07T03:12:00Z", "today 5:12"),
        ("2026-10-06T22:14", "2026-10-06T20:14:00Z", "yesterday 22:14"),
        ("2026-10-05T22:14", "2026-10-05T20:14:00Z", "5 Oct 22:14"),
    ],
)
def test_missing_line_names_the_last_sync(local, utc, when):
    assert _missing(local, utc) == (
        f"Missing: sleep (last sync with Garmin: {when}). Without these data the verdict cannot be green."
    )


def test_stale_missed_session_line():
    sessions = [{"id": 1, "date": "2026-10-06", "type": "running", "description": "Intervals", "status": "planned",
                 "suggestion": {"result": "no_activity", "final": False, "evaluated_on": "2026-10-07"}}]
    data = garmin_data(device=device_status("2026-10-07T04:58:12Z", "2026-10-07T06:58"))
    b = build_briefing(DAY, data, [], {}, yesterday_sessions=sessions, checked_at={1: "2026-10-07T01:30:00+00:00"},
                       language="en")
    assert (
        "- Planned yesterday with no activity in Garmin at the nightly check, before the watch's last sync "
        "(may be out of date): run – Intervals."
    ) in b.message.body.splitlines()
    fresh = build_briefing(DAY, data, [], {}, yesterday_sessions=sessions, checked_at={1: "2026-10-07T05:30:00+00:00"},
                           language="en")
    assert "- Planned yesterday with no activity in Garmin: run – Intervals." in fresh.message.body.splitlines()


# ------------------------------------------------------------------ per-session advice (PR 41)

STRENGTH_DW = {
    "id": 2,
    "date": "2026-10-07",
    "weekday": "Wed",
    "type": "strength",
    "description": "bench 3×8 @ 75 kg",
    "intensity": "RPE 8",
    "status": "planned",
}
STRENGTH_GENERIC = {k: v for k, v in {**STRENGTH_DW, "id": 3, "description": "Celé tělo"}.items() if k != "intensity"}
STRENGTH_SINGLE = {k: v for k, v in {**STRENGTH_DW, "id": 4, "description": "deadlift 1×5"}.items() if k != "intensity"}
HARD = {**GREEN_SESSION, "id": 5, "description": "Intervaly 6×800 m", "intensity": "Z4", "duration_min": None, "distance_km": None}
MOBILITY = {**GREEN_SESSION, "id": 6, "type": "mobility", "description": "Mobilita 15 min", "duration_min": None, "distance_km": None}
YELLOW_DAY = garmin_data(snapshot={**GREEN_SNAPSHOT, "readiness": {"score": 60}})
RED_DAY = garmin_data(snapshot={**GREEN_SNAPSHOT, "readiness": {"score": 30}})
SWAP = "swap for recovery: a walk, mobility or rest"


def en_lines(sessions, data):
    body = build_briefing(DAY, data, sessions, {}, language="en").message.body
    after = body.split("Today's plan:\n", 1)[1].splitlines()
    return [line for line in after if line.startswith("- ")]


@pytest.mark.parametrize(
    ("data", "session", "expected"),
    [
        (YELLOW_DAY, STRENGTH_DW, "- strength: bench 3×8 @ 75 kg (RPE 8) → reduce: 2×8 instead of 3×8, at most RPE 7"),
        (YELLOW_DAY, STRENGTH_GENERIC, "- strength: Celé tělo → reduce: 1 set fewer per exercise, at most RPE 7"),
        (YELLOW_DAY, STRENGTH_SINGLE, "- strength: deadlift 1×5 → reduce: at most RPE 7"),
        (YELLOW_DAY, GREEN_SESSION, "- run: 10 km lehce, zóna 2 (60 min, 10 km) → OK"),
        (
            YELLOW_DAY,
            HARD,
            "- run: Intervaly 6×800 m (Z4) → reduce: easy in zone 2 only, no intervals or strides",
        ),
        (YELLOW_DAY, MOBILITY, "- mobility: Mobilita 15 min → OK"),
        (RED_DAY, STRENGTH_DW, f"- strength: bench 3×8 @ 75 kg (RPE 8) → {SWAP}"),
        (RED_DAY, GREEN_SESSION, "- run: 10 km lehce, zóna 2 (60 min, 10 km) → reduce: at most 40 min in zone 1–2"),
        (RED_DAY, HARD, f"- run: Intervaly 6×800 m (Z4) → {SWAP}"),
        (RED_DAY, MOBILITY, "- mobility: Mobilita 15 min → OK"),
    ],
    ids=[
        "yellow-strength",
        "yellow-generic",
        "yellow-single",
        "yellow-easy",
        "yellow-hard",
        "yellow-mobility",
        "red-strength",
        "red-easy",
        "red-hard",
        "red-mobility",
    ],
)
def test_advice_lines_exact_en(data, session, expected):
    assert en_lines([session], data) == [expected]


@pytest.mark.parametrize("texts", list(LANGUAGES.values()))
@pytest.mark.parametrize("description", ["bench 3×8", "Celé tělo", "deadlift 1×5"])
def test_every_action_and_class_renders(texts, description):
    # A floor (PR 44) can put any action on any class: every pair must render in every catalogue.
    for action in ACTIONS:
        for cls in CLASSES:
            advice = Advice(1, action, cls, "verdict", _change(action, cls, {"description": description}))
            text = briefing._advice_text(texts, advice)
            assert text.startswith(" → ")
            assert "{" not in text and "}" not in text
