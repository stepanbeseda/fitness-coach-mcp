"""The English catalogue (the default language): the same scenarios as the exact Czech rendering tests."""

from __future__ import annotations

import pytest

from briefing_helpers import GREEN_SESSION
from coach_mcp.briefing import build_briefing
from coach_mcp.messages import EN, LANGUAGES
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
Verdict: yellow – cut volume by 20–30 %, intensity at most RPE 7–8, cardio in zone 2.
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
    en = build_briefing(DAY, yellow_data(), [], YELLOW_ROWS, language="en")
    cs = build_briefing(DAY, yellow_data(), [], YELLOW_ROWS, language="cs")
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
    ]:
        assert set(mapping) == set(keys)


def _missing(local: str, utc: str) -> str:
    data = garmin_data(snapshot={k: v for k, v in GREEN_SNAPSHOT.items() if k != "sleep"}, device=device_status(utc, local))
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
