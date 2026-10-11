"""Pure briefing logic: criteria, verdict, alerts and the exact Czech rendering (day = 2026-10-07).

The English catalogue has its own rendering tests in ``test_briefing_en.py``."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any

import pytest

from briefing_helpers import GREEN_SESSION
from coach_mcp import briefing
from coach_mcp.briefing import (
    ALERT_KEYS,
    alert_conditions,
    criteria_from_snapshot,
    decide_verdict,
    is_new,
    nights_below_baseline,
)
from coach_mcp.briefing import build_briefing as _build_briefing
from coach_mcp.garmin import LastSync
from coach_mcp.messages import CS, EN
from garmin_payloads import (
    DAY,
    device_status,
    GREEN_SCHEDULED,
    GREEN_SNAPSHOT,
    GREEN_TREND,
    YELLOW_TREND,
    days_range,
    garmin_data,
    scheduled,
    trend,
    yellow_data,
)

YESTERDAY = "2026-10-06"
# Every rendering assertion in this module is about the Czech catalogue.
build_briefing = partial(_build_briefing, language="cs")


def snapshot(**sections: Any) -> dict[str, Any]:
    """The green snapshot with some sections replaced (a ``None`` removes the section)."""
    out = {**GREEN_SNAPSHOT, **sections}
    return {k: v for k, v in out.items() if v is not None}


# ------------------------------------------------------------------ levels


@pytest.mark.parametrize(
    ("score", "level"), [(100, "green"), (70, "green"), (69, "yellow"), (69.5, "yellow"), (40, "yellow"), (39, "red"), (0, "red")]
)
def test_readiness_levels(score, level):
    assert briefing.readiness_level(score) == level
    levels = criteria_from_snapshot(snapshot(readiness={"score": score}), 0)
    assert levels["readiness"]["level"] == level


@pytest.mark.parametrize(
    ("minutes", "level"), [(480, "green"), (420, "green"), (419, "yellow"), (360, "yellow"), (359, "red")]
)
def test_sleep_levels(minutes, level):
    assert briefing.sleep_level(minutes) == level
    assert criteria_from_snapshot(snapshot(sleep={"duration_h": minutes / 60}), 0)["sleep"]["level"] == level


def test_sleep_is_judged_in_whole_minutes():
    entry = criteria_from_snapshot(snapshot(sleep={"duration_h": 6.999}), 0)["sleep"]
    assert entry["minutes"] == 420 and entry["level"] == "green"
    entry = criteria_from_snapshot(snapshot(sleep={"duration_h": 5.99}), 0)["sleep"]
    assert entry["minutes"] == 359 and entry["level"] == "red"


@pytest.mark.parametrize(("value", "level"), [(100, "green"), (50, "green"), (49, "yellow"), (0, "yellow")])
def test_body_battery_levels(value, level):
    assert briefing.body_battery_level(value) == level
    assert criteria_from_snapshot(snapshot(body_battery={"at_wake": value}), 0)["body_battery"]["level"] == level


@pytest.mark.parametrize(
    ("last", "nights", "level"),
    [
        (52, 0, "green"),  # last == low
        (60, None, "green"),
        (51, 0, "yellow"),  # the trend disagrees with the snapshot
        (51, 1, "yellow"),
        (51, 2, "yellow"),
        (51, 3, "red"),
        (51, 6, "red"),
        (51, None, "yellow"),
    ],
)
def test_hrv_levels(last, nights, level):
    assert briefing.hrv_level(last, 52, nights) == level
    snap = snapshot(hrv={"last_night_avg_ms": last, "baseline_balanced_ms": [52, 68]})
    assert criteria_from_snapshot(snap, nights)["hrv"]["level"] == level


def test_verdict_green_yellow_red():
    green = dict.fromkeys(briefing.CRITERIA, "green")
    assert decide_verdict(green) == ("green", [])
    assert decide_verdict({**green, "sleep": "yellow"}) == ("yellow", [])
    assert decide_verdict({**green, "sleep": "yellow", "readiness": "red"}) == ("red", [])
    assert decide_verdict({**green, "hrv": "red"}) == ("red", [])


def test_verdict_missing_inputs_cap_at_yellow_and_are_named():
    green = dict.fromkeys(briefing.CRITERIA, "green")
    for key in briefing.CRITERIA:
        assert decide_verdict({**green, key: None}) == ("yellow", [key]), key
    # A missing input never hides a red one.
    assert decide_verdict({**green, "hrv": None, "readiness": "red"}) == ("red", ["hrv"])
    # A key absent from the mapping counts as missing; names come in the fixed order.
    assert decide_verdict({}) == ("yellow", ["readiness", "hrv", "sleep", "body_battery"])
    assert decide_verdict(dict.fromkeys(briefing.CRITERIA)) == ("yellow", list(briefing.CRITERIA))
    assert decide_verdict({"body_battery": None, "sleep": None, "hrv": "green", "readiness": "green"}) == (
        "yellow",
        ["sleep", "body_battery"],
    )


def test_criteria_from_snapshot_sections():
    full = criteria_from_snapshot(GREEN_SNAPSHOT, 0)
    assert {k: v["level"] for k, v in full.items()} == dict.fromkeys(briefing.CRITERIA, "green")

    def levels(snap):
        return {k: v["level"] for k, v in criteria_from_snapshot(snap, 0).items()}

    # error, available: false, a missing field and a non-dict section all count as missing
    assert levels(snapshot(readiness={"error": "x"}))["readiness"] is None
    assert levels(snapshot(readiness={"available": False}))["readiness"] is None
    # the marker wins even when a usable value sits next to it
    assert levels(snapshot(readiness={"error": "x", "score": 78}))["readiness"] is None
    assert levels(snapshot(readiness={"available": False, "score": 78}))["readiness"] is None
    assert levels(snapshot(readiness={"available": True, "score": 78}))["readiness"] == "green"
    assert levels(snapshot(readiness={"status": "n/a"}))["readiness"] is None
    assert levels(snapshot(readiness="78"))["readiness"] is None
    assert levels(snapshot(readiness=None))["readiness"] is None
    assert levels(snapshot(sleep={"score": 80}))["sleep"] is None
    assert levels(snapshot(body_battery=[81]))["body_battery"] is None
    assert levels(snapshot(readiness={"score": "high"}))["readiness"] is None
    assert levels(snapshot(readiness={"score": True}))["readiness"] is None
    assert levels(None) == dict.fromkeys(briefing.CRITERIA)
    assert levels("nonsense") == dict.fromkeys(briefing.CRITERIA)  # type: ignore[arg-type]
    # morning_score is preferred over score, and score is the fallback
    assert criteria_from_snapshot(snapshot(readiness={"score": 30, "morning_score": 75}), 0)["readiness"] == {
        "level": "green",
        "score": 75,
    }
    assert criteria_from_snapshot(snapshot(readiness={"score": 30, "morning_score": None}), 0)["readiness"] == {
        "level": "red",
        "score": 30,
    }
    # a sleep score is optional
    assert criteria_from_snapshot(snapshot(sleep={"duration_h": 8}), 0)["sleep"] == {"level": "green", "minutes": 480}


@pytest.mark.parametrize(
    ("baseline", "expected"),
    [([52], [52]), ([52, 68], [52, 68]), ([52, None], [52])],
)
def test_hrv_baseline_lists(baseline, expected):
    snap = snapshot(hrv={"last_night_avg_ms": 48, "baseline_balanced_ms": baseline})
    entry = criteria_from_snapshot(snap, 0)["hrv"]
    assert entry["baseline_ms"] == expected and entry["level"] == "yellow"


@pytest.mark.parametrize("baseline", [[], None, "x", [None], [None, 68], ["52"], {"low": 52}])
def test_hrv_unusable_baseline_makes_hrv_missing(baseline):
    snap = snapshot(hrv={"last_night_avg_ms": 48, "baseline_balanced_ms": baseline})
    assert criteria_from_snapshot(snap, 0)["hrv"] == {"level": None}


def test_hrv_missing_last_night_makes_hrv_missing():
    snap = snapshot(hrv={"baseline_balanced_ms": [52, 68]})
    assert criteria_from_snapshot(snap, 0)["hrv"] == {"level": None}


# ------------------------------------------------------------------ streak


def test_nights_below_baseline():
    def count(days, day=DAY, low=52, high=68):
        return nights_below_baseline(trend(days, low=low, high=high), day)

    assert count({"2026-10-05": 50, "2026-10-06": 49, "2026-10-07": 51}) == 3
    assert count({"2026-10-05": 60, "2026-10-06": 49, "2026-10-07": 51}) == 2
    # a gap (missing date) ends the streak
    assert count({"2026-10-04": 50, "2026-10-06": 49, "2026-10-07": 51}) == 2
    # a missing value ends the streak
    assert count({"2026-10-05": 50, "2026-10-06": None, "2026-10-07": 51}) == 1
    # today above the baseline -> 0, even when earlier nights were below
    assert count({"2026-10-05": 50, "2026-10-06": 49, "2026-10-07": 60}) == 0
    # exactly on the lower bound is not below
    assert count({"2026-10-07": 52}) == 0
    # today missing -> unknown
    assert count({"2026-10-05": 50, "2026-10-06": 49}) is None
    assert count({"2026-10-06": 50, "2026-10-07": None}) is None
    # at most 7 nights are counted
    assert count(days_range(date(2026, 9, 20), DAY, 40)) == 7
    assert nights_below_baseline(None, DAY) is None
    assert nights_below_baseline({"days": "x"}, DAY) is None
    assert nights_below_baseline({}, DAY) is None


def test_nights_below_baseline_missing_baseline_ends_the_streak():
    days = [
        {"date": "2026-10-05", "last_night_avg_ms": 40},  # no baseline: ends the streak
        {"date": "2026-10-06", "last_night_avg_ms": 40, "baseline_balanced_ms": [52, 68]},
        {"date": "2026-10-07", "last_night_avg_ms": 40, "baseline_balanced_ms": [52]},
    ]
    assert nights_below_baseline({"days": days}, DAY) == 2
    assert nights_below_baseline({"days": days[:2]}, DAY) is None
    no_baseline_today = [{"date": "2026-10-07", "last_night_avg_ms": 40}]
    assert nights_below_baseline({"days": no_baseline_today}, DAY) is None


# ------------------------------------------------------------------ alerts


def conditions(**overrides: Any) -> dict[str, bool | None]:
    yesterday_sessions = overrides.pop("yesterday_sessions", ())
    data = garmin_data(**overrides)
    return alert_conditions(DAY, data, nights_below_baseline(data.hrv_trend, DAY), yesterday_sessions)


def ys(type_="running", status="planned", result=None, description="Intervaly 6×800 m", id_=1) -> dict[str, Any]:
    """A ``sessions_out``-like dict dated yesterday, with a suggestion when ``result`` is given."""
    out: dict[str, Any] = {
        "id": id_,
        "date": YESTERDAY,
        "type": type_,
        "description": description,
        "status": status,
    }
    if result is not None:
        out["suggestion"] = {"result": result, "final": False, "evaluated_on": "2026-10-07"}
    return out


ALERT_CASES = [
    # hrv_below_baseline: nights_below >= 3 holds, 0-2 does not, unknown without today's entry
    ("hrv_below_baseline", {"hrv_trend": trend({"2026-10-05": 50, "2026-10-06": 50, "2026-10-07": 50})}, True),
    ("hrv_below_baseline", {"hrv_trend": trend({"2026-10-06": 50, "2026-10-07": 50})}, False),
    ("hrv_below_baseline", {"hrv_trend": GREEN_TREND}, False),
    ("hrv_below_baseline", {"hrv_trend": YELLOW_TREND}, None),
    ("hrv_below_baseline", {"hrv_trend": None}, None),
    # resting_hr_elevated: today - avg_7d >= 5
    ("resting_hr_elevated", {"snapshot": snapshot(resting_hr={"today": 57, "avg_7d": 52})}, True),
    ("resting_hr_elevated", {"snapshot": snapshot(resting_hr={"today": 56, "avg_7d": 52})}, False),
    ("resting_hr_elevated", {"snapshot": snapshot(resting_hr={"today": 40, "avg_7d": 52})}, False),
    ("resting_hr_elevated", {"snapshot": snapshot(resting_hr={"today": 57})}, None),
    ("resting_hr_elevated", {"snapshot": snapshot(resting_hr={"avg_7d": 52})}, None),
    ("resting_hr_elevated", {"snapshot": snapshot(resting_hr={"error": "x"})}, None),
    ("resting_hr_elevated", {"snapshot": snapshot(resting_hr=None)}, None),
    ("resting_hr_elevated", {"snapshot": None}, None),
    # acwr_out_of_range
    *[
        ("acwr_out_of_range", {"snapshot": snapshot(training_status={"acwr_status": status})}, holds)
        for status, holds in [
            ("HIGH", True),
            (" high ", True),
            ("LOW", True),
            ("VERY_HIGH", True),
            ("OPTIMAL", False),
            (" Optimal", False),
            ("NONE", None),
            ("Höch<script>", None),
            ("", None),
            (42, None),
            (None, None),
        ]
    ],
    ("acwr_out_of_range", {"snapshot": snapshot(training_status={"available": False})}, None),
    ("acwr_out_of_range", {"snapshot": snapshot(training_status=None)}, None),
    # missed_workout
    ("missed_workout", {"scheduled": scheduled([{"date": YESTERDAY, "name": "x", "completed": False}])}, True),
    (
        "missed_workout",
        {"scheduled": scheduled([{"date": YESTERDAY, "completed": True}, {"date": YESTERDAY, "completed": False}])},
        True,
    ),
    (
        "missed_workout",
        {"scheduled": scheduled([{"date": YESTERDAY, "completed": False}, {"date": YESTERDAY, "name": "no flag"}])},
        True,
    ),
    ("missed_workout", {"scheduled": scheduled([{"date": YESTERDAY, "name": "x", "completed": True}])}, False),
    ("missed_workout", {"scheduled": scheduled([])}, False),
    # only today's entry (not yesterday's) with completed false does not count
    ("missed_workout", {"scheduled": scheduled([{"date": "2026-10-07", "completed": False}])}, False),
    ("missed_workout", {"scheduled": scheduled([{"date": YESTERDAY, "name": "x"}])}, None),
    ("missed_workout", {"scheduled": scheduled([{"date": YESTERDAY, "completed": None}])}, None),
    # mixed True and absent completed
    (
        "missed_workout",
        {"scheduled": scheduled([{"date": YESTERDAY, "completed": True}, {"date": YESTERDAY, "name": "x"}])},
        None,
    ),
    ("missed_workout", {"scheduled": None}, None),
    ("missed_workout", {"scheduled": {"count": 0}}, None),
    # missed_planned_session: yesterday's planned running / cycling / swimming / strength sessions
    ("missed_planned_session", {"yesterday_sessions": [ys(result="no_activity")]}, True),
    (
        "missed_planned_session",
        {"yesterday_sessions": [ys(result="matched"), ys("strength", result="no_activity", id_=2)]},
        True,
    ),
    ("missed_planned_session", {"yesterday_sessions": []}, False),
    ("missed_planned_session", {}, False),
    ("missed_planned_session", {"yesterday_sessions": [ys(result="matched")]}, False),
    ("missed_planned_session", {"yesterday_sessions": [ys("mobility")]}, False),  # type filter
    ("missed_planned_session", {"yesterday_sessions": [ys(status="done", result="no_activity")]}, False),
    ("missed_planned_session", {"yesterday_sessions": [ys()]}, None),
    ("missed_planned_session", {"yesterday_sessions": [ys(result="matched"), ys("cycling", id_=2)]}, None),
    # Garmin's calendar unknown does not make this condition unknown (it reads the store)
    ("missed_planned_session", {"scheduled": None, "yesterday_sessions": [ys(result="no_activity")]}, True),
    # garmin_auth
    ("garmin_auth", {"auth": "missing"}, True),
    ("garmin_auth", {"auth": "error"}, True),
    ("garmin_auth", {"auth": "ok", "calls": {"get_hrv_trend": "auth_error", "get_recovery_snapshot": "ok"}}, True),
    ("garmin_auth", {"auth": "ok"}, False),
    ("garmin_auth", {"auth": None, "calls": {"get_hrv_trend": "error"}}, None),
]


@pytest.mark.parametrize(("key", "overrides", "expected"), ALERT_CASES)
def test_alert_conditions(key, overrides, expected):
    result = conditions(**overrides)
    assert list(result) == list(ALERT_KEYS)
    assert result[key] is expected


def test_alert_conditions_cover_every_key_with_all_three_outcomes():
    for key in ALERT_KEYS:
        outcomes = {expected for k, _, expected in ALERT_CASES if k == key}
        assert outcomes == {True, False, None}, key


def test_is_new():
    today = DAY.isoformat()
    assert is_new(True, None, DAY) is True  # never seen
    assert is_new(True, {"value": 0, "evaluated_on": YESTERDAY, "fired_on": None}, DAY) is True
    assert is_new(True, {"value": 1, "evaluated_on": YESTERDAY, "fired_on": "2026-10-04"}, DAY) is False
    assert is_new(True, {"value": 1, "evaluated_on": YESTERDAY, "fired_on": None}, DAY) is False
    # already announced today stays new, so the tool and a manual send repeat the morning's push
    assert is_new(True, {"value": 1, "evaluated_on": today, "fired_on": today}, DAY) is True
    assert is_new(False, None, DAY) is False
    assert is_new(False, {"value": 0, "evaluated_on": YESTERDAY, "fired_on": None}, DAY) is False
    assert is_new(None, None, DAY) is False
    assert is_new(None, {"value": 0, "evaluated_on": YESTERDAY, "fired_on": None}, DAY) is False
    # an event alert (yesterday's missed workout) ignores the previous evaluation
    held_yesterday = {"value": 1, "evaluated_on": YESTERDAY, "fired_on": YESTERDAY}
    assert is_new(True, held_yesterday, DAY, event=True) is True
    assert is_new(True, held_yesterday, DAY) is False
    assert is_new(False, None, DAY, event=True) is False
    assert is_new(None, None, DAY, event=True) is False


def test_missed_workout_on_consecutive_days_is_reported_each_day():
    rows = {"missed_workout": {"value": 1, "evaluated_on": YESTERDAY, "fired_on": YESTERDAY}}
    sched = scheduled([{"date": YESTERDAY, "name": "Tempo 10 km", "completed": False}])
    b = build_briefing(DAY, garmin_data(scheduled=sched), [], rows)
    alert = next(a for a in b.payload["alerts"] if a["key"] == "missed_workout")
    assert alert == {"key": "missed_workout", "holds": True, "new": True}
    assert "- Včera naplánovaný trénink nebyl splněn: Tempo 10 km." in b.message.body.splitlines()
    # a state alert in the same situation stays quiet
    state_rows = {"resting_hr_elevated": {"value": 1, "evaluated_on": YESTERDAY, "fired_on": YESTERDAY}}
    data = garmin_data(snapshot=snapshot(resting_hr={"today": 60, "avg_7d": 50}))
    quiet = build_briefing(DAY, data, [], state_rows)
    assert next(a for a in quiet.payload["alerts"] if a["key"] == "resting_hr_elevated")["new"] is False


# --------------------------------------------------------------- rendering

GREEN_MESSAGE = """\
Verdikt: zelená – plný plán, můžeš přidat progresi.
Readiness: 78 (zelená)
HRV: 62 ms, baseline 52–68 ms (zelená)
Spánek: 7 h 30 min, skóre 84 (zelená)
Body Battery ráno: 81 (zelená)
Dnes v plánu:
- běh: 10 km lehce, zóna 2 (60 min, 10 km)"""

YELLOW_MESSAGE = """\
Verdikt: žlutá – sniž objem o 20–30 %, intenzitu nejvýš RPE 7–8, kardio v zóně 2.
Readiness: 61 (žlutá)
HRV: chybí
Spánek: 6 h 15 min, skóre 70 (žlutá)
Body Battery ráno: chybí
Chybí: HRV, Body Battery (hodinky možná ještě nesynchronizovaly). Bez těchto dat nemůže být verdikt zelený.
Pro plnou zátěž: readiness aspoň 70; spánek aspoň 7 h.
Dnes v Garminu: Lehký běh 40 min
Upozornění:
- Klidová tepovka 58 bpm, o 6 víc než 7denní průměr 52 bpm.
- Včera naplánovaný trénink nebyl splněn: Intervaly 6×800 m."""

GREEN_TITLE = "\U0001f7e2 ZELENÁ · ranní briefing 7. 10."
YELLOW_TITLE = "\U0001f7e1 \u26a0\ufe0f ŽLUTÁ · ranní briefing 7. 10."

YELLOW_ROWS = {
    "resting_hr_elevated": {"value": 0, "evaluated_on": YESTERDAY, "fired_on": None},
    "acwr_out_of_range": {"value": 1, "evaluated_on": YESTERDAY, "fired_on": "2026-10-04"},
    "garmin_auth": {"value": 0, "evaluated_on": YESTERDAY, "fired_on": None},
}


def test_missed_planned_session_on_consecutive_days_is_reported_each_day():
    rows = {"missed_planned_session": {"value": 1, "evaluated_on": YESTERDAY, "fired_on": YESTERDAY}}
    b = build_briefing(DAY, garmin_data(), [], rows, yesterday_sessions=[ys(result="no_activity")])
    alert = next(a for a in b.payload["alerts"] if a["key"] == "missed_planned_session")
    assert alert == {"key": "missed_planned_session", "holds": True, "new": True}
    assert "- Včera v plánu bez aktivity v Garminu: běh – Intervaly 6×800 m." in b.message.body.splitlines()


def test_render_missed_planned_session_text_exact():
    sched = scheduled([{"date": YESTERDAY, "name": "Tempo", "completed": False}])
    sessions = [
        ys(result="no_activity"),
        ys("strength", result="no_activity", description="Celé tělo", id_=2),
        ys("cycling", result="matched", id_=3),
    ]
    b = build_briefing(DAY, garmin_data(scheduled=sched), [], {}, yesterday_sessions=sessions)
    tail = b.message.body.split("Upozornění:\n")[1].splitlines()
    assert tail == [
        "- Včera naplánovaný trénink nebyl splněn: Tempo.",
        "- Včera v plánu bez aktivity v Garminu: běh – Intervaly 6×800 m; síla – Celé tělo.",
    ]
    assert "\u26a0\ufe0f" in b.message.title


def test_render_green_exact():
    b = build_briefing(DAY, garmin_data(), [GREEN_SESSION], {})
    assert b.verdict == "green"
    assert b.message.title == GREEN_TITLE
    assert b.message.body == GREEN_MESSAGE
    assert b.message.text == GREEN_TITLE + "\n\n" + GREEN_MESSAGE
    assert b.waiting is False
    assert b.payload["missing"] == []
    assert b.payload["planned"] == [GREEN_SESSION]
    assert b.payload["push"] == {"text": b.message.text}
    assert all(a["new"] is False for a in b.payload["alerts"])
    assert "Pro plnou zátěž" not in b.message.body and "Upozornění" not in b.message.body


def test_render_yellow_with_missing_and_alerts_exact():
    b = build_briefing(DAY, yellow_data(), [], YELLOW_ROWS)
    assert b.verdict == "yellow"
    assert b.message.title == YELLOW_TITLE
    assert b.message.body == YELLOW_MESSAGE
    assert b.waiting is True
    assert b.payload == {
        "date": "2026-10-07",
        "verdict": "yellow",
        "criteria": {
            "readiness": {"level": "yellow", "score": 61},
            "hrv": {"level": "missing"},
            "sleep": {"level": "yellow", "minutes": 375, "score": 70},
            "body_battery": {"level": "missing"},
        },
        "missing": ["hrv", "body_battery"],
        "planned": [],
        "garmin_today": ["Lehký běh 40 min"],
        "alerts": [
            {"key": "hrv_below_baseline", "holds": None, "new": False},
            {"key": "resting_hr_elevated", "holds": True, "new": True},
            {"key": "acwr_out_of_range", "holds": True, "new": False},
            {"key": "missed_workout", "holds": True, "new": True},
            {"key": "missed_planned_session", "holds": False, "new": False},
            {"key": "garmin_auth", "holds": False, "new": False},
        ],
        "garmin": {
            "auth": "ok",
            "calls": {
                "get_device_status": "ok",
                "get_recovery_snapshot": "ok",
                "get_hrv_trend": "ok",
                "list_scheduled_workouts": "ok",
            },
        },
        "push": {"text": YELLOW_TITLE + "\n\n" + YELLOW_MESSAGE},
    }
    # only known conditions are written back; the unknown HRV streak is not
    assert b.alert_updates == {
        "resting_hr_elevated": (True, True),
        "acwr_out_of_range": (True, False),
        "missed_workout": (True, True),
        "missed_planned_session": (False, False),
        "garmin_auth": (False, False),
    }


def test_render_red_title_line():
    b = build_briefing(DAY, garmin_data(snapshot=snapshot(readiness={"score": 30})), [], {})
    assert b.verdict == "red"
    assert b.message.title == "\U0001f534 ČERVENÁ · ranní briefing 7. 10."  # red alone, no warning
    assert b.message.text.splitlines()[0] == b.message.title
    assert b.message.text.splitlines()[1] == ""
    assert b.message.body.splitlines()[0] == (
        "Verdikt: červená – jen regenerace: lehké kardio v zóně 1–2 na 20–40 min, mobilita, nebo volno."
    )
    assert "Pro plnou zátěž: readiness aspoň 70." in b.message.body


def test_render_new_alert_adds_warning_to_title():
    data = garmin_data(snapshot=snapshot(resting_hr={"today": 60, "avg_7d": 50}))
    b = build_briefing(DAY, data, [], {})
    assert b.verdict == "green"
    assert b.message.title == "\U0001f7e2 \u26a0\ufe0f ZELENÁ · ranní briefing 7. 10."
    known = {"resting_hr_elevated": {"value": 1, "evaluated_on": YESTERDAY, "fired_on": YESTERDAY}}
    quiet = build_briefing(DAY, data, [], known)
    assert quiet.message.title == GREEN_TITLE


def test_render_alert_texts_exact():
    data = garmin_data(
        auth="missing",
        snapshot=snapshot(
            resting_hr={"today": 58.5, "avg_7d": 52},
            training_status={"acwr_ratio": 1.62, "acwr_status": "VERY_HIGH"},
        ),
        hrv_trend=trend({"2026-10-05": 40, "2026-10-06": 40, "2026-10-07": 40}),
        scheduled=scheduled(
            [
                {"date": YESTERDAY, "name": "Intervaly", "completed": False},
                {"date": YESTERDAY, "completed": False},
                {"date": YESTERDAY, "name": "Hotovo", "completed": True},
            ]
        ),
    )
    text = build_briefing(DAY, data, [], {}).message.body
    tail = text.split("Upozornění:\n")[1].splitlines()
    assert tail == [
        "- HRV pod baseline 3 noci po sobě.",
        "- Klidová tepovka 58,5 bpm, o 6,5 víc než 7denní průměr 52 bpm.",
        "- Poměr akutní a chronické zátěže 1,62 je mimo optimální rozsah Garminu (velmi vysoký).",
        "- Včera naplánovaný trénink nebyl splněn: Intervaly, bez názvu.",
        "- Garmin není přihlášený: na serveru spusť scripts/garmin-login.sh.",
    ]


def test_render_acwr_without_ratio_and_word_variants():
    def alert_line(status, ratio=None):
        ts: dict[str, Any] = {"acwr_status": status}
        if ratio is not None:
            ts["acwr_ratio"] = ratio
        text = build_briefing(DAY, garmin_data(snapshot=snapshot(training_status=ts)), [], {}).message.body
        return [line for line in text.splitlines() if "zátěže" in line]

    assert alert_line("HIGH") == ["- Poměr akutní a chronické zátěže je mimo optimální rozsah Garminu (vysoký)."]
    assert alert_line("low", 0.6) == [
        "- Poměr akutní a chronické zátěže 0,60 je mimo optimální rozsah Garminu (nízký)."
    ]
    # the raw Garmin string never reaches the message
    assert alert_line("Höch<script>", 1.9) == []


def test_render_hrv_variants():
    def line(last, baseline, nights):
        """The HRV line when the trend has ``nights`` nights below the baseline ending on the day."""
        snap = snapshot(hrv={"last_night_avg_ms": last, "baseline_balanced_ms": baseline})
        days = days_range(date(2026, 10, 1), date(2026, 10, 7), 60)
        for offset in range(nights):
            days[f"2026-10-0{7 - offset}"] = 40
        text = build_briefing(DAY, garmin_data(snapshot=snap, hrv_trend=trend(days)), [], {}).message.body
        return next(ln for ln in text.splitlines() if ln.startswith("HRV"))

    assert line(48, [52], 0) == "HRV: 48 ms, baseline od 52 ms, pod baseline (žlutá)"
    assert line(45, [52, 68], 2) == "HRV: 45 ms, baseline 52–68 ms, pod baseline 2 noci po sobě (žlutá)"
    assert line(62, [52, 68], 0) == "HRV: 62 ms, baseline 52–68 ms (zelená)"
    assert line(62, [52], 0) == "HRV: 62 ms, baseline od 52 ms (zelená)"
    assert line(45, [52, 68], 3) == "HRV: 45 ms, baseline 52–68 ms, pod baseline 3 noci po sobě (červená)"
    assert line(45, [52, 68], 1) == "HRV: 45 ms, baseline 52–68 ms, pod baseline 1 noc po sobě (žlutá)"
    # unknown streak (no trend entry for today): only "pod baseline"
    snap = snapshot(hrv={"last_night_avg_ms": 45, "baseline_balanced_ms": [52, 68]})
    text = build_briefing(DAY, garmin_data(snapshot=snap, hrv_trend=None), [], {}).message.body
    assert "HRV: 45 ms, baseline 52–68 ms, pod baseline (žlutá)" in text.splitlines()
    assert "Pro plnou zátěž: HRV zpět aspoň na 52 ms." in text


def session(**fields: Any) -> dict[str, Any]:
    return {**GREEN_SESSION, **fields}


def plan_lines(sessions, data=None):
    text = build_briefing(DAY, data or garmin_data(), sessions, {}).message.body
    return text.split("\n")[5:]


def test_render_plan_variants():
    assert plan_lines([session(status="done")]) == ["Dnes v plánu:", "- běh: 10 km lehce, zóna 2 (60 min, 10 km) – splněno"]
    assert plan_lines([session(status="skipped")])[1].endswith(" – přeskočeno")
    assert plan_lines([session(status="moved")])[1].endswith(" – přesunuto")
    assert plan_lines([session(status="planned")])[1].endswith(")")
    # details: present parts only, in the order duration, distance, intensity
    only_intensity = session(duration_min=None, distance_km=None, intensity="RPE 7")
    assert plan_lines([only_intensity])[1] == "- běh: 10 km lehce, zóna 2 (RPE 7)"
    no_details = session(duration_min=None, distance_km=None)
    assert plan_lines([no_details])[1] == "- běh: 10 km lehce, zóna 2"
    all_parts = session(duration_min=45, distance_km=7.5, intensity="easy")
    assert plan_lines([all_parts])[1] == "- běh: 10 km lehce, zóna 2 (45 min, 7,5 km, easy)"
    # every session type has its Czech word
    for kind, word in [
        ("running", "běh"),
        ("cycling", "kolo"),
        ("swimming", "plavání"),
        ("strength", "síla"),
        ("mobility", "mobilita"),
        ("other", "jiné"),
    ]:
        assert plan_lines([session(type=kind)])[1].startswith(f"- {word}: ")
    # a long description with a newline is collapsed and cut at 120 characters plus an ellipsis
    long = session(description=("a" * 100 + "\n  " + "b" * 200), duration_min=None, distance_km=None)
    line = plan_lines([long])[1]
    assert line == "- běh: " + "a" * 100 + " " + "b" * 19 + "…"
    assert len(line) == len("- běh: ") + 121
    # two sessions keep their order
    two = plan_lines([session(description="first"), session(description="second", type="strength")])
    assert [x[:12] for x in two[1:]] == ["- běh: first", "- síla: seco"]
    # Garmin fallback and nothing planned
    fallback = garmin_data(scheduled=scheduled([{"date": "2026-10-07", "name": "Jízda"}, {"date": "2026-10-07"}]))
    assert plan_lines([], fallback) == ["Dnes v Garminu: Jízda, bez názvu"]
    assert plan_lines([], garmin_data(scheduled=scheduled([{"date": "2026-10-06", "completed": True}]))) == [
        "Dnes v plánu: nic."
    ]
    assert plan_lines([], garmin_data(scheduled=None)) == ["Dnes v plánu: nic."]
    # the coach plan wins over the Garmin calendar
    assert plan_lines([session()], fallback)[0] == "Dnes v plánu:"
    # a long Garmin name is cut at 80 characters
    named = garmin_data(scheduled=scheduled([{"date": "2026-10-07", "name": "x" * 100}]))
    assert plan_lines([], named) == ["Dnes v Garminu: " + "x" * 80 + "…"]


def test_render_sleep_without_score():
    snap = snapshot(sleep={"duration_h": 8})
    text = build_briefing(DAY, garmin_data(snapshot=snap), [], {}).message.body
    assert "Spánek: 8 h 00 min (zelená)" in text.splitlines()


def test_missing_baseline_is_named_and_does_not_wait():
    snap = snapshot(hrv={"last_night_avg_ms": 62})
    b = build_briefing(DAY, garmin_data(snapshot=snap), [], {})
    assert b.waiting is False and b.verdict == "yellow" and b.payload["missing"] == ["hrv"]
    lines = b.message.body.splitlines()
    assert "HRV: chybí" in lines
    assert (
        "Chybí: HRV baseline (Garmin ji počítá až po zhruba 3 týdnech nošení hodinek). "
        "Bez těchto dat nemůže být verdikt zelený."
    ) in lines
    # no night value at all: still waiting and still called "HRV"
    nothing = build_briefing(DAY, garmin_data(snapshot=snapshot(hrv=None)), [], {})
    assert nothing.waiting is True and "Chybí: HRV (" in nothing.message.body
    # a baseline without last night's value is not "night present": still waiting, still called "HRV"
    only_baseline = build_briefing(
        DAY, garmin_data(snapshot=snapshot(hrv={"baseline_balanced_ms": [52, 68]})), [], {}
    )
    assert only_baseline.waiting is True and "Chybí: HRV (" in only_baseline.message.body
    # an unavailable HRV section waits too
    assert build_briefing(DAY, garmin_data(snapshot=snapshot(hrv={"available": False})), [], {}).waiting is True


def test_render_missing_reason():
    def reason(data):
        text = build_briefing(DAY, data, [], {}).message.body
        return next(ln for ln in text.splitlines() if ln.startswith("Chybí:"))

    sync = reason(garmin_data(snapshot=snapshot(sleep=None)))
    assert sync == (
        "Chybí: spánek (hodinky možná ještě nesynchronizovaly). Bez těchto dat nemůže být verdikt zelený."
    )
    assert "(Garmin není přihlášený)" in reason(garmin_data(auth="missing", snapshot=None))
    # the coach has no garmin-mcp token: its own configuration problem, not a Garmin login
    no_token = garmin_data(auth=None, snapshot=None, hrv_trend=None, scheduled=None, credentials_missing=True)
    assert "(chybí interní token pro garmin-mcp (secrets/garmin_readonly_token))" in reason(no_token)
    assert build_briefing(DAY, no_token, [], {}).payload["garmin"]["auth"] == "no_credentials"
    assert alert_conditions(DAY, no_token, None)["garmin_auth"] is None
    # last night's HRV is there but Garmin has no baseline yet
    assert reason(garmin_data(snapshot=snapshot(hrv={"last_night_avg_ms": 62}))) == (
        "Chybí: HRV baseline (Garmin ji počítá až po zhruba 3 týdnech nošení hodinek). "
        "Bez těchto dat nemůže být verdikt zelený."
    )
    # with another key missing too, the generic reason applies and the name stays "HRV baseline"
    two = reason(garmin_data(snapshot=snapshot(hrv={"last_night_avg_ms": 62}, sleep=None)))
    assert two.startswith("Chybí: HRV baseline, spánek (hodinky možná ještě nesynchronizovaly)")
    assert "(Garmin není přihlášený)" in reason(garmin_data(snapshot=snapshot(sleep=None), calls={"get_recovery_snapshot": "auth_error"}))
    assert "(garmin-mcp neodpovídá)" in reason(garmin_data(auth=None, snapshot=None))
    assert "(data z Garminu se nepodařilo načíst)" in reason(garmin_data(snapshot=None))
    # all four names, in order
    assert reason(garmin_data(snapshot={})).startswith("Chybí: readiness, HRV, spánek, Body Battery (")
    # a green verdict never carries a "Chybí" line
    assert "Chybí" not in build_briefing(DAY, garmin_data(), [], {}).message.body


def test_missing_data_never_green_and_waiting_flag():
    no_sleep = build_briefing(DAY, garmin_data(snapshot=snapshot(sleep=None)), [], {})
    assert (no_sleep.verdict, no_sleep.waiting) == ("yellow", True)
    no_hrv = build_briefing(DAY, garmin_data(snapshot=snapshot(hrv=None)), [], {})
    assert (no_hrv.verdict, no_hrv.waiting) == ("yellow", True)
    no_battery = build_briefing(DAY, garmin_data(snapshot=snapshot(body_battery=None)), [], {})
    assert (no_battery.verdict, no_battery.waiting) == ("yellow", False)
    no_readiness = build_briefing(DAY, garmin_data(snapshot=snapshot(readiness=None)), [], {})
    assert (no_readiness.verdict, no_readiness.waiting) == ("yellow", False)
    # an auth problem never waits, even with sleep and HRV missing
    auth = build_briefing(DAY, garmin_data(auth="missing", snapshot=None), [], {})
    assert (auth.verdict, auth.waiting) == ("yellow", False)
    # garmin-mcp unreachable keeps waiting (transient)
    down = build_briefing(DAY, garmin_data(auth=None, snapshot=None, hrv_trend=None, scheduled=None), [], {})
    assert down.waiting is True
    assert down.payload["garmin"]["auth"] == "unreachable"


def test_message_byte_limit():
    big = [session(description="x" * 120, status="planned") for _ in range(30)]
    b = build_briefing(DAY, garmin_data(), big, {})
    text = b.message.text
    assert len(text.encode()) <= 4000 and b.message.body.endswith("…")
    assert text.startswith(GREEN_TITLE + "\n\n")
    # 2-byte characters: the cut lands inside a character and is dropped, the result still decodes
    wide = [session(description="é" * 120, status="planned") for _ in range(30)]
    b = build_briefing(DAY, garmin_data(), wide, {})
    text = b.message.text
    assert len(text.encode()) <= 4000 and b.message.body.endswith("…")
    assert text.startswith(GREEN_TITLE + "\n\n")
    assert "�" not in text
    # A new alert adds the warning sign to the title (7 more bytes); the limit still covers the whole text.
    alerting = build_briefing(
        DAY, garmin_data(snapshot=snapshot(resting_hr={"today": 60, "avg_7d": 50})), big, {}
    )
    text = alerting.message.text
    assert len(text.encode()) <= 4000 and alerting.message.body.endswith("…")
    assert text.startswith("\U0001f7e2 \u26a0\ufe0f ")
    small = build_briefing(DAY, garmin_data(), [session()], {})
    assert not small.message.body.endswith("…")


def test_nights_plural():
    assert [CS.nights(n) for n in (1, 2, 3, 4, 5)] == ["1 noc", "2 noci", "3 noci", "4 noci", "5 nocí"]
    assert [EN.nights(n) for n in (1, 2)] == ["1 night", "2 nights"]


def test_build_briefing_payload_shape():
    b = build_briefing(DAY, garmin_data(), [], {})
    assert list(b.payload) == [
        "date",
        "verdict",
        "criteria",
        "missing",
        "planned",
        "garmin_today",
        "alerts",
        "garmin",
        "push",
    ]
    assert [a["key"] for a in b.payload["alerts"]] == list(ALERT_KEYS)
    assert b.payload["criteria"]["hrv"] == {
        "level": "green",
        "last_night_ms": 62,
        "baseline_ms": [52, 68],
        "nights_below": 0,
    }
    assert b.payload["garmin"]["auth"] == "ok"
    # all conditions known and false: updates carry every key with (False, False)
    assert b.alert_updates == dict.fromkeys(ALERT_KEYS, (False, False))
    unknown = build_briefing(DAY, garmin_data(auth=None, snapshot=None, hrv_trend=None, scheduled=None), [], {})
    assert unknown.alert_updates == {"missed_planned_session": (False, False)}
    # the new condition reads the coach store, not Garmin, so it is known even when Garmin is not
    assert all(a["holds"] is None for a in unknown.payload["alerts"] if a["key"] != "missed_planned_session")


def test_green_scheduled_fixture_has_no_missed_workout():
    assert GREEN_SCHEDULED["scheduled"][0]["completed"] is True


# ------------------------------------------------- sync-aware briefing (PR 17)

PRAGUE_OFFSET = timedelta(hours=2)  # Europe/Prague in early October (DST ends on 2026-10-25)


def ds(local: str) -> dict[str, Any]:
    """A get_device_status payload whose last upload happened at ``local`` (wall time, YYYY-MM-DDTHH:MM)."""
    wall = datetime.fromisoformat(local)
    return device_status((wall - PRAGUE_OFFSET).strftime("%Y-%m-%dT%H:%M:00Z"), local)


def missing_line(data, language="cs"):
    text = _build_briefing(DAY, data, [], {}, language=language).message.body
    return next(ln for ln in text.splitlines() if ln.startswith(("Chybí:", "Missing:")))


NO_SLEEP = snapshot(sleep=None)


def test_missing_line_names_the_last_sync():
    line = missing_line(garmin_data(snapshot=NO_SLEEP, device=ds("2026-10-07T05:12")))
    assert line == "Chybí: spánek (poslední synchronizace s Garminem: dnes 5:12). Bez těchto dat nemůže být verdikt zelený."


@pytest.mark.parametrize(
    ("local", "when"),
    [
        ("2026-10-07T05:12", "dnes 5:12"),
        ("2026-10-07T00:00", "dnes 0:00"),
        ("2026-10-06T22:14", "včera 22:14"),
        ("2026-10-06T00:05", "včera 0:05"),
        ("2026-10-05T22:14", "5. 10. 22:14"),
        ("2026-10-08T00:05", "8. 10. 0:05"),  # a future date uses the day form
    ],
)
def test_sync_time_is_relative_to_the_briefing_day(local, when):
    line = missing_line(garmin_data(snapshot=NO_SLEEP, device=ds(local)))
    assert line.startswith(f"Chybí: spánek (poslední synchronizace s Garminem: {when})")


def test_other_missing_reasons_win_over_the_sync_time():
    device = ds("2026-10-06T22:14")
    assert "(Garmin není přihlášený)" in missing_line(garmin_data(auth="missing", snapshot=None, device=device))
    assert "(Garmin není přihlášený)" in missing_line(
        garmin_data(snapshot=NO_SLEEP, device=device, calls={"get_device_status": "auth_error"})
    )
    assert "(garmin-mcp neodpovídá)" in missing_line(garmin_data(auth=None, snapshot=None, device=device))
    no_token = garmin_data(auth=None, snapshot=None, credentials_missing=True, device=device)
    assert "(chybí interní token" in missing_line(no_token)
    assert "(data z Garminu se nepodařilo načíst)" in missing_line(garmin_data(snapshot=None, device=device))
    only_baseline = garmin_data(snapshot=snapshot(hrv={"last_night_avg_ms": 62}), device=device)
    assert "(Garmin ji počítá až po zhruba 3 týdnech nošení hodinek)" in missing_line(only_baseline)
    # with another key missing too the sync time applies
    two = garmin_data(snapshot=snapshot(hrv={"last_night_avg_ms": 62}, sleep=None), device=device)
    assert "(poslední synchronizace s Garminem: včera 22:14)" in missing_line(two)


@pytest.mark.parametrize(
    "device",
    [
        None,
        {"last_upload": {"time_utc": "9999-12-31T23:59:59Z", "time_local": "9999-12-31T23:59"}},
        {"last_upload": {"available": False}},
        {"last_upload": {"error": "x"}},
    ],
    ids=["no-device", "year-9999", "unavailable", "error"],
)
def test_unknown_sync_time_keeps_the_old_wording(device):
    line = missing_line(garmin_data(snapshot=NO_SLEEP, device=device))
    assert line == "Chybí: spánek (hodinky možná ještě nesynchronizovaly). Bez těchto dat nemůže být verdikt zelený."


MISSED = {
    "scheduled": scheduled([{"date": YESTERDAY, "name": "Tempo 10 km", "completed": False}]),
}


def test_missed_alerts_are_held_back_until_a_sync_after_yesterday_ended():
    sessions = [ys(result="no_activity")]
    before_midnight = conditions(device=ds("2026-10-06T23:59"), yesterday_sessions=sessions, **MISSED)
    assert before_midnight["missed_workout"] is None and before_midnight["missed_planned_session"] is None
    at_midnight = conditions(device=ds("2026-10-07T00:00"), yesterday_sessions=sessions, **MISSED)
    assert at_midnight["missed_workout"] is True and at_midnight["missed_planned_session"] is True
    unknown = conditions(yesterday_sessions=sessions, **MISSED)  # no device: as before PR 17
    assert unknown["missed_workout"] is True and unknown["missed_planned_session"] is True
    garbled = {"last_upload": {"time_utc": "9999-12-31T23:59:59Z", "time_local": "9999-12-31T23:59"}}
    still = conditions(device=garbled, yesterday_sessions=sessions, **MISSED)
    assert still["missed_workout"] is True and still["missed_planned_session"] is True


def test_the_hold_back_leaves_the_other_alerts_alone():
    data = garmin_data(device=ds("2026-10-06T23:59"), snapshot=snapshot(resting_hr={"today": 60, "avg_7d": 50}))
    out = alert_conditions(DAY, data, nights_below_baseline(data.hrv_trend, DAY), [ys(result="no_activity")])
    assert out["resting_hr_elevated"] is True and out["garmin_auth"] is False
    assert out["missed_workout"] is None and out["missed_planned_session"] is None


def test_a_held_back_condition_that_does_not_hold_is_unknown_too():
    sessions = [ys(result="matched")]
    done = scheduled([{"date": YESTERDAY, "name": "Tempo", "completed": True}])
    held = conditions(device=ds("2026-10-06T21:40"), yesterday_sessions=sessions, scheduled=done)
    assert held["missed_workout"] is None and held["missed_planned_session"] is None
    b = build_briefing(DAY, garmin_data(device=ds("2026-10-06T21:40"), scheduled=done), [], {}, yesterday_sessions=sessions)
    assert "missed_workout" not in b.alert_updates and "missed_planned_session" not in b.alert_updates
    synced = build_briefing(DAY, garmin_data(device=ds("2026-10-07T06:00"), scheduled=done), [], {}, yesterday_sessions=sessions)
    assert synced.alert_updates["missed_workout"] == (False, False)
    assert synced.alert_updates["missed_planned_session"] == (False, False)


def test_a_held_back_briefing_shows_neither_alert_but_the_sync_time():
    data = garmin_data(device=ds("2026-10-06T21:40"), snapshot=NO_SLEEP, **MISSED)
    b = build_briefing(DAY, data, [], {}, yesterday_sessions=[ys(result="no_activity")])
    assert "Včera" not in b.message.body and "Upozornění" not in b.message.body
    for key in ("missed_workout", "missed_planned_session"):
        assert next(a for a in b.payload["alerts"] if a["key"] == key) == {"key": key, "holds": None, "new": False}
    assert "(poslední synchronizace s Garminem: včera 21:40)" in b.message.body
    synced = garmin_data(device=ds("2026-10-07T06:58"), snapshot=NO_SLEEP, **MISSED)
    shown = build_briefing(DAY, synced, [], {}, yesterday_sessions=[ys(result="no_activity")]).message.body
    assert "Včera naplánovaný trénink nebyl splněn: Tempo 10 km." in shown
    assert "Včera v plánu bez aktivity v Garminu" in shown


def test_the_hold_back_is_not_repeated_the_next_morning():
    # day 1: the watch last synced before midnight, so nothing is reported and nothing is recorded
    day1 = build_briefing(DAY, garmin_data(device=ds("2026-10-06T21:40"), **MISSED), [], {},
                          yesterday_sessions=[ys(result="no_activity")])
    assert "missed_workout" not in day1.alert_updates and "missed_planned_session" not in day1.alert_updates
    # day 2: a sync after midnight; the events of its own yesterday fire (events, not states)
    day2 = DAY + timedelta(days=1)
    rows = {"missed_workout": {"value": 1, "evaluated_on": YESTERDAY, "fired_on": YESTERDAY}}
    sched = scheduled([{"date": DAY.isoformat(), "name": "Tempo 10 km", "completed": False}])
    data = garmin_data(device=ds("2026-10-08T06:10"), scheduled=sched)
    b = _build_briefing(day2, data, [], rows, yesterday_sessions=[ys(result="no_activity")], language="cs")
    fired = {a["key"]: a["new"] for a in b.payload["alerts"]}
    assert fired["missed_workout"] is True and fired["missed_planned_session"] is True


STALE_LINE = (
    "- Včera v plánu bez aktivity v Garminu při noční kontrole, před poslední synchronizací hodinek "
    "(nemusí platit): běh – Intervaly 6×800 m."
)
PLAIN_LINE = "- Včera v plánu bez aktivity v Garminu: běh – Intervaly 6×800 m."
SYNCED_0658 = device_status("2026-10-07T04:58:12Z", "2026-10-07T06:58")


def alert_lines(device, checked_at, sessions=None, language="cs"):
    sessions = sessions if sessions is not None else [ys(result="no_activity")]
    b = _build_briefing(
        DAY, garmin_data(device=device), [], {}, yesterday_sessions=sessions, checked_at=checked_at, language=language
    )
    return b.message.body.splitlines()


def test_a_check_that_ran_before_the_last_sync_is_labelled_stale():
    assert STALE_LINE in alert_lines(SYNCED_0658, {1: "2026-10-07T01:30:00+00:00"})
    assert STALE_LINE not in alert_lines(SYNCED_0658, {1: "2026-10-07T01:30:00+00:00"}, language="en")


def test_a_check_at_or_after_the_last_sync_is_plain():
    equal = alert_lines(SYNCED_0658, {1: "2026-10-07T04:58:12+00:00"})
    assert PLAIN_LINE in equal and STALE_LINE not in equal
    after = alert_lines(SYNCED_0658, {1: "2026-10-07T05:00:00+00:00"})
    assert PLAIN_LINE in after and STALE_LINE not in after
    other_zone = alert_lines(SYNCED_0658, {1: "2026-10-07T07:00:00+02:00"})  # 05:00 UTC
    assert PLAIN_LINE in other_zone


@pytest.mark.parametrize("checked_at", [{}, {2: "2026-10-07T05:00:00+00:00"}, {1: "garbage"}, {1: "2026-10-07T01:30:00"}])
def test_an_unusable_check_time_is_stale(checked_at):
    lines = alert_lines(SYNCED_0658, checked_at)
    assert STALE_LINE in lines and PLAIN_LINE not in lines


def test_an_unknown_sync_time_never_labels_the_line():
    assert PLAIN_LINE in alert_lines(None, {1: "2026-10-07T01:30:00+00:00"})
    assert PLAIN_LINE in alert_lines(None, {})


def test_the_stale_label_only_touches_the_missed_session_line():
    sessions = [ys(result="no_activity")]
    b = _build_briefing(
        DAY,
        garmin_data(device=SYNCED_0658, **MISSED),
        [],
        {},
        yesterday_sessions=sessions,
        checked_at={1: "2026-10-07T01:30:00+00:00"},
        language="cs",
    )
    lines = b.message.body.splitlines()
    assert "- Včera naplánovaný trénink nebyl splněn: Tempo 10 km." in lines and STALE_LINE in lines


def test_only_no_activity_sessions_count_for_the_stale_label():
    sessions = [ys(result="no_activity", id_=1), ys("cycling", result="matched", description="Kolo", id_=2)]
    # the matched session was checked before the sync, the no_activity one after it
    checked = {1: "2026-10-07T05:30:00+00:00", 2: "2026-10-07T01:30:00+00:00"}
    lines = alert_lines(SYNCED_0658, checked, sessions)
    assert PLAIN_LINE in lines and STALE_LINE not in lines


def test_one_stale_session_among_fresh_ones_labels_the_line():
    sessions = [ys(result="no_activity", id_=1), ys("strength", result="no_activity", description="Celé tělo", id_=2)]
    stale_second = alert_lines(SYNCED_0658, {1: "2026-10-07T05:30:00+00:00", 2: "2026-10-07T01:30:00+00:00"}, sessions)
    assert any(ln.startswith("- Včera v plánu bez aktivity v Garminu při noční kontrole") for ln in stale_second)
    stale_first = alert_lines(SYNCED_0658, {1: "2026-10-07T01:30:00+00:00", 2: "2026-10-07T05:30:00+00:00"}, sessions)
    assert any(ln.startswith("- Včera v plánu bez aktivity v Garminu při noční kontrole") for ln in stale_first)
    fresh = alert_lines(SYNCED_0658, {1: "2026-10-07T05:30:00+00:00", 2: "2026-10-07T05:31:00+00:00"}, sessions)
    assert not any("při noční kontrole" in ln for ln in fresh)


def test_missed_check_predates_sync_unit():
    last = LastSync(datetime(2026, 10, 7, 4, 58, 12, tzinfo=UTC), datetime(2026, 10, 7, 6, 58))
    sessions = [ys(result="no_activity")]
    assert briefing.missed_check_predates_sync(sessions, {1: "2026-10-07T01:30:00+00:00"}, last) is True
    assert briefing.missed_check_predates_sync(sessions, {1: "2026-10-07T05:30:00+00:00"}, last) is False
    assert briefing.missed_check_predates_sync(sessions, {1: "2026-10-07T01:30:00+00:00"}, None) is False
    assert briefing.missed_check_predates_sync([ys(result="matched")], {}, last) is False
    assert briefing.missed_check_predates_sync([ys(status="done", result="no_activity")], {}, last) is False
    assert briefing.missed_check_predates_sync([ys("mobility", result="no_activity")], {}, last) is False
