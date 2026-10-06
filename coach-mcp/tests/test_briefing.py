"""Pure briefing logic: criteria, verdict, alerts and the exact Czech rendering (day = 2026-10-07)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from briefing_helpers import GREEN_SESSION
from coach_mcp import briefing
from coach_mcp.briefing import (
    ALERT_KEYS,
    alert_conditions,
    build_briefing,
    criteria_from_snapshot,
    decide_verdict,
    is_new,
    nights_below_baseline,
    nights_cs,
)
from garmin_payloads import (
    DAY,
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
    data = garmin_data(**overrides)
    return alert_conditions(DAY, data, nights_below_baseline(data.hrv_trend, DAY))


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

YELLOW_ROWS = {
    "resting_hr_elevated": {"value": 0, "evaluated_on": YESTERDAY, "fired_on": None},
    "acwr_out_of_range": {"value": 1, "evaluated_on": YESTERDAY, "fired_on": "2026-10-04"},
    "garmin_auth": {"value": 0, "evaluated_on": YESTERDAY, "fired_on": None},
}


def test_render_green_exact():
    b = build_briefing(DAY, garmin_data(), [GREEN_SESSION], {})
    assert b.verdict == "green"
    assert b.message.title == "ZELENÁ · ranní briefing 7. 10."
    assert b.message.message == GREEN_MESSAGE
    assert b.message.priority == 3
    assert b.message.tags == ("green_circle",)
    assert b.waiting is False
    assert b.payload["missing"] == []
    assert b.payload["planned"] == [GREEN_SESSION]
    assert b.payload["push"] == {
        "title": b.message.title,
        "message": GREEN_MESSAGE,
        "priority": 3,
        "tags": ["green_circle"],
    }
    assert all(a["new"] is False for a in b.payload["alerts"])
    assert "Pro plnou zátěž" not in b.message.message and "Upozornění" not in b.message.message


def test_render_yellow_with_missing_and_alerts_exact():
    b = build_briefing(DAY, yellow_data(), [], YELLOW_ROWS)
    assert b.verdict == "yellow"
    assert b.message.title == "ŽLUTÁ · ranní briefing 7. 10."
    assert b.message.message == YELLOW_MESSAGE
    assert b.message.priority == 4
    assert b.message.tags == ("yellow_circle", "warning")
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
            {"key": "garmin_auth", "holds": False, "new": False},
        ],
        "garmin": {
            "auth": "ok",
            "calls": {"get_recovery_snapshot": "ok", "get_hrv_trend": "ok", "list_scheduled_workouts": "ok"},
        },
        "push": {
            "title": "ŽLUTÁ · ranní briefing 7. 10.",
            "message": YELLOW_MESSAGE,
            "priority": 4,
            "tags": ["yellow_circle", "warning"],
        },
    }
    # only known conditions are written back; the unknown HRV streak is not
    assert b.alert_updates == {
        "resting_hr_elevated": (True, True),
        "acwr_out_of_range": (True, False),
        "missed_workout": (True, True),
        "garmin_auth": (False, False),
    }


def test_render_red_has_priority_4_and_red_circle():
    b = build_briefing(DAY, garmin_data(snapshot=snapshot(readiness={"score": 30})), [], {})
    assert b.verdict == "red"
    assert b.message.title == "ČERVENÁ · ranní briefing 7. 10."
    assert b.message.priority == 4  # red alone, no new alert
    assert b.message.tags == ("red_circle",)
    assert b.message.message.splitlines()[0] == (
        "Verdikt: červená – jen regenerace: lehké kardio v zóně 1–2 na 20–40 min, mobilita, nebo volno."
    )
    assert "Pro plnou zátěž: readiness aspoň 70." in b.message.message


def test_render_new_alert_alone_raises_priority_and_adds_warning():
    data = garmin_data(snapshot=snapshot(resting_hr={"today": 60, "avg_7d": 50}))
    b = build_briefing(DAY, data, [], {})
    assert b.verdict == "green"
    assert (b.message.priority, b.message.tags) == (4, ("green_circle", "warning"))
    known = {"resting_hr_elevated": {"value": 1, "evaluated_on": YESTERDAY, "fired_on": YESTERDAY}}
    quiet = build_briefing(DAY, data, [], known)
    assert (quiet.message.priority, quiet.message.tags) == (3, ("green_circle",))


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
    text = build_briefing(DAY, data, [], {}).message.message
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
        text = build_briefing(DAY, garmin_data(snapshot=snapshot(training_status=ts)), [], {}).message.message
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
        text = build_briefing(DAY, garmin_data(snapshot=snap, hrv_trend=trend(days)), [], {}).message.message
        return next(ln for ln in text.splitlines() if ln.startswith("HRV"))

    assert line(48, [52], 0) == "HRV: 48 ms, baseline od 52 ms, pod baseline (žlutá)"
    assert line(45, [52, 68], 2) == "HRV: 45 ms, baseline 52–68 ms, pod baseline 2 noci po sobě (žlutá)"
    assert line(62, [52, 68], 0) == "HRV: 62 ms, baseline 52–68 ms (zelená)"
    assert line(62, [52], 0) == "HRV: 62 ms, baseline od 52 ms (zelená)"
    assert line(45, [52, 68], 3) == "HRV: 45 ms, baseline 52–68 ms, pod baseline 3 noci po sobě (červená)"
    assert line(45, [52, 68], 1) == "HRV: 45 ms, baseline 52–68 ms, pod baseline 1 noc po sobě (žlutá)"
    # unknown streak (no trend entry for today): only "pod baseline"
    snap = snapshot(hrv={"last_night_avg_ms": 45, "baseline_balanced_ms": [52, 68]})
    text = build_briefing(DAY, garmin_data(snapshot=snap, hrv_trend=None), [], {}).message.message
    assert "HRV: 45 ms, baseline 52–68 ms, pod baseline (žlutá)" in text.splitlines()
    assert "Pro plnou zátěž: HRV zpět aspoň na 52 ms." in text


def session(**fields: Any) -> dict[str, Any]:
    return {**GREEN_SESSION, **fields}


def plan_lines(sessions, data=None):
    text = build_briefing(DAY, data or garmin_data(), sessions, {}).message.message
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
    text = build_briefing(DAY, garmin_data(snapshot=snap), [], {}).message.message
    assert "Spánek: 8 h 00 min (zelená)" in text.splitlines()


def test_render_missing_reason():
    def reason(data):
        text = build_briefing(DAY, data, [], {}).message.message
        return next(ln for ln in text.splitlines() if ln.startswith("Chybí:"))

    sync = reason(garmin_data(snapshot=snapshot(sleep=None)))
    assert sync == (
        "Chybí: spánek (hodinky možná ještě nesynchronizovaly). Bez těchto dat nemůže být verdikt zelený."
    )
    assert "(Garmin není přihlášený)" in reason(garmin_data(auth="missing", snapshot=None))
    assert "(Garmin není přihlášený)" in reason(garmin_data(snapshot=snapshot(sleep=None), calls={"get_recovery_snapshot": "auth_error"}))
    assert "(garmin-mcp neodpovídá)" in reason(garmin_data(auth=None, snapshot=None))
    assert "(data z Garminu se nepodařilo načíst)" in reason(garmin_data(snapshot=None))
    # all four names, in order
    assert reason(garmin_data(snapshot={})).startswith("Chybí: readiness, HRV, spánek, Body Battery (")
    # a green verdict never carries a "Chybí" line
    assert "Chybí" not in build_briefing(DAY, garmin_data(), [], {}).message.message


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
    message = build_briefing(DAY, garmin_data(), big, {}).message.message
    raw = message.encode()
    assert len(raw) <= 4000 and message.endswith("…")
    # 2-byte characters: the cut lands inside a character and is dropped, the result still decodes
    wide = [session(description="é" * 120, status="planned") for _ in range(30)]
    message = build_briefing(DAY, garmin_data(), wide, {}).message.message
    assert len(message.encode()) <= 4000 and message.endswith("…")
    assert "�" not in message
    small = build_briefing(DAY, garmin_data(), [session()], {}).message.message
    assert not small.endswith("…")


def test_nights_plural():
    assert [nights_cs(n) for n in (1, 2, 4, 5)] == ["1 noc", "2 noci", "4 noci", "5 nocí"]
    assert nights_cs(3) == "3 noci"


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
    assert unknown.alert_updates == {}
    assert all(a["holds"] is None for a in unknown.payload["alerts"])


def test_green_scheduled_fixture_has_no_missed_workout():
    assert GREEN_SCHEDULED["scheduled"][0]["completed"] is True
