"""The Monday report text (PR 14): exact output of ``render_weekly`` for a fixture report in both languages."""

from datetime import date, timedelta

import pytest

from coach_mcp.briefing import MAX_MESSAGE_BYTES
from coach_mcp.goals import get_goal_progress
from coach_mcp.messages import LANGUAGES, WEEKLY_LANGUAGES, weekly_texts
from coach_mcp.scheduler import compose_weekly
from coach_mcp.weekly import render_weekly

def stat(week, baseline, weeks=4):
    return {"week": week, "baseline": baseline, "baseline_weeks": weeks, "change": None, "change_pct": None}


REPORT = {
    "week": {"start": "2026-10-05", "end": "2026-10-11", "iso_week": "2026-W41", "partial": False},
    "endurance": {"activity_days": 7, "total": {"count": 4, "duration_min": stat(310.0, 270.4),
                  "distance_km": stat(42.31, 38.0), "training_load": stat(820.4, 700.0)}, "by_sport": {}},
    "strength": {"days": 7, "workouts": 3, "volume_kg": stat(12340.5, 11000.2), "sets": stat(48, 45.5), "by_muscle_group": {}},
    "sleep": {"days": 7, "duration_h": stat(7.2, 7.08), "score": stat(80, 78)},
    "hrv": {"days": 7, "last_night_ms": stat(52.3, 55.0), "min_ms": 45, "status": "BALANCED"},
    "resting_hr": {"days": 7, "bpm": stat(48.0, 47.2)},
    "adherence": {"planned": 0, "done": 5, "skipped": 1, "moved": 0, "overdue_planned": 0, "completion_rate": 0.83,
                  "baseline_completion_rate": 0.9, "baseline_weeks": 4},
    "new_bests": [
        {"metric": "vo2max_running", "value": 53.0, "previous_best": 52.0},
        {"metric": "running_distance_km", "value": 42.31, "previous_best": 40.1, "weeks_compared": 8},
        {"metric": "endurance_duration_min", "value": 310.0, "previous_best": 290.0, "weeks_compared": 8},
        {"metric": "e1rm_kg", "exercise": "Bench Press (Barbell)", "template_id": "T1", "value": 100.0,
         "previous_best": 97.5, "date": "2026-10-07"},
        {"metric": "future_metric", "value": 1, "previous_best": 0},
    ],
    "flags": [
        {"flag": "endurance_load_up", "week": 820.4, "baseline": 600.0, "change": 220.4, "change_pct": 36.7},
        {"flag": "sleep_down", "week": 6.4, "baseline": 7.0, "change": -0.6, "change_pct": -8.6},
        {"flag": "resting_hr_up", "week": 53.0, "baseline": 47.5, "change": 5.5, "change_pct": 11.6},
        {"flag": "future_flag", "week": 1, "baseline": 1, "change": 0, "change_pct": 0},
    ],
}
PROGRESS = {
    "today": "2026-10-12",
    "goals": [
        {"goal_id": 1, "title": "Half marathon\nsub 1:36", "metric": "race_half_marathon", "available": True,
         "current": {"date": "2026-10-11", "value": 5836, "time": "1:37:16"},
         "weeks_4_ago": {"date": "2026-09-14", "value": 5890, "time": "1:38:10"}, "weeks_12_ago": None,
         "trend_per_week": -14.0, "points": 83, "target_value": {"value": 5800, "time": "1:36:40"},
         "projected": {"date": "2026-11-15", "value": 5756, "time": "1:35:56"}, "on_track": True, "reason": None},
        {"goal_id": 2, "title": "VO2max 56", "metric": "vo2max_running", "available": True,
         "current": {"date": "2026-10-11", "value": 52.1}, "weeks_4_ago": None, "weeks_12_ago": None,
         "trend_per_week": 0.35, "points": 15, "target_value": {"value": 56.0},
         "projected": {"date": "2026-12-01", "value": 54.6}, "on_track": False, "reason": None},
        {"goal_id": 3, "title": "5 km", "metric": "race_5k", "available": True,
         "current": {"date": "2026-10-11", "value": 1290, "time": "21:30"}, "weeks_4_ago": None, "weeks_12_ago": None,
         "trend_per_week": -2.333, "points": 20, "target_value": None, "projected": None, "on_track": None,
         "reason": "no_target_date"},
        {"goal_id": 4, "title": "Marathon", "metric": "race_marathon", "available": False, "current": None,
         "weeks_4_ago": None, "weeks_12_ago": None, "trend_per_week": None, "points": 0,
         "target_value": {"value": 12600, "time": "3:30:00"}, "projected": None, "on_track": None,
         "reason": "no_recent_value"},
    ],
    "goals_without_metric": 1,
}


EN_TITLE = '📊 Weekly report 2026-W41 · 5 Oct–11 Oct'
EN_BODY = 'Endurance: 4 activities, 5 h 10 min, 42.3 km, load 820 (4-week average 4 h 30 min, load 700)\nStrength (Hevy): 3 workouts, working sets 48, volume 12340 kg (4-week average: sets 45.5, volume 11000 kg)\nSleep: average 7 h 12 min (4-week average 7 h 05 min)\nHRV: average 52.3 ms (4-week average 55 ms)\nResting HR: average 48 bpm (4-week average 47.2 bpm)\nPlan: done 5, skipped 1, moved 0, still planned 0 – completion 83 %\nNew bests: VO2max running 53 (was 52); running 42.3 km in a week (was 40.1 km); endurance time 5 h 10 min in a week (was 4 h 50 min); estimated 1RM Bench Press (Barbell) 100 kg (was 97.5 kg).\nWatch: endurance load 36.7 % above the 4-week average; sleep 36 min a night shorter than the 4-week average; resting HR 5.5 bpm above the 4-week average.\nGoals:\n- Half marathon sub 1:36 (half marathon prediction): now 1:37:16, 4 weeks ago 1:38:10, -14 s a week; at this trend 1:35:56 on 15 Nov, target 1:36:40: on track\n- VO2max 56 (VO2max running): now 52.1, +0.35 a week; at this trend 54.6 on 1 Dec, target 56: behind\n- 5 km (5 km prediction): now 21:30, -2 s a week; no target date\n- Marathon (marathon prediction): not enough data yet (no value in the last 14 days)'
CS_TITLE = '📊 Týdenní report 2026-W41 · 5. 10.–11. 10.'
CS_BODY = 'Vytrvalost: 4 aktivity, 5 h 10 min, 42,3 km, zátěž 820 (průměr 4 týdnů 4 h 30 min, zátěž 700)\nSíla (Hevy): 3 tréninky, pracovní série 48, objem 12340 kg (průměr 4 týdnů: série 45,5, objem 11000 kg)\nSpánek: průměr 7 h 12 min (průměr 4 týdnů 7 h 05 min)\nHRV: průměr 52,3 ms (průměr 4 týdnů 55 ms)\nKlidová tepovka: průměr 48 bpm (průměr 4 týdnů 47,2 bpm)\nPlán: splněno 5, přeskočeno 1, přesunuto 0, zbývá v plánu 0 – plnění 83 %\nNové rekordy: VO2max běh 53 (dříve 52); běh 42,3 km za týden (dříve 40,1 km); vytrvalost 5 h 10 min za týden (dříve 4 h 50 min); odhad 1RM Bench Press (Barbell) 100 kg (dříve 97,5 kg).\nPozor: vytrvalostní zátěž o 36,7 % nad průměrem 4 týdnů; spánek o 36 min za noc kratší než průměr 4 týdnů; klidová tepovka o 5,5 bpm nad průměrem 4 týdnů.\nCíle:\n- Half marathon sub 1:36 (predikce půlmaratonu): teď 1:37:16, před 4 týdny 1:38:10, -14 s za týden; tímto tempem 1:35:56 k 15. 11., cíl 1:36:40: na dobré cestě\n- VO2max 56 (VO2max běh): teď 52,1, +0,35 za týden; tímto tempem 54,6 k 1. 12., cíl 56: zaostává\n- 5 km (predikce 5 km): teď 21:30, -2 s za týden; bez cílového data\n- Marathon (predikce maratonu): zatím málo dat (žádná hodnota za posledních 14 dní)'


def empty_report(**sections):
    base = {"endurance": {"available": False}, "strength": {"available": False, "reason": "x"},
            "sleep": {"available": False}, "hrv": {"available": False}, "resting_hr": {"available": False},
            "adherence": {"available": False}, "new_bests": [], "flags": []}
    return {"week": REPORT["week"], **base, **sections}


def test_english_message_is_exact():
    message = render_weekly(REPORT, PROGRESS, "en")
    assert (message.title, message.body) == (EN_TITLE, EN_BODY)
    assert len(message.text.encode()) <= MAX_MESSAGE_BYTES


def test_czech_message_is_exact():
    message = render_weekly(REPORT, PROGRESS, "cs")
    assert (message.title, message.body) == (CS_TITLE, CS_BODY)


def test_unknown_bests_and_flags_are_skipped():
    assert "future" not in render_weekly(REPORT, PROGRESS, "en").text
    only_unknown = dict(REPORT, new_bests=[{"metric": "future_metric", "value": 1, "previous_best": 0}],
                        flags=[{"flag": "future_flag", "change": 0, "change_pct": 0}])
    body = render_weekly(only_unknown, PROGRESS, "en").body
    assert "New bests" not in body and "Watch" not in body


def test_empty_report_has_a_no_data_line_and_the_goal_count():
    progress = {"today": "2026-10-12", "goals": [], "goals_without_metric": 2}
    message = render_weekly(empty_report(), progress, "en")
    assert message.title == EN_TITLE
    assert message.body == (
        "No stored data for this week (the nightly history is off or still loading).\n"
        "Goals without a measurable metric: 2."
    )
    assert "Goals:" not in message.body
    none = render_weekly(empty_report(), {"today": "x", "goals": [], "goals_without_metric": 0}, "cs")
    assert none.body == "Pro tento týden nejsou uložená data (noční historie je vypnutá nebo se ještě načítá)."


def test_a_long_message_is_cut_to_the_limit():
    goal = dict(PROGRESS["goals"][0], title="Ž" * 300)
    progress = {"today": "x", "goals": [dict(goal) for _ in range(60)], "goals_without_metric": 0}
    message = render_weekly(REPORT, progress, "cs")
    assert len(message.text.encode()) <= MAX_MESSAGE_BYTES
    assert message.body.endswith("…")
    assert message.text.encode().decode("utf-8")  # no character was cut in half


def test_missing_baselines_and_rates_leave_their_parts_out():
    report = dict(REPORT)
    report["sleep"] = {"days": 7, "duration_h": stat(7.2, None), "score": stat(80, None)}
    report["hrv"] = {"days": 7, "last_night_ms": stat(52.3, None)}
    report["adherence"] = dict(REPORT["adherence"], completion_rate=None)
    report["endurance"] = {"activity_days": 7, "total": dict(REPORT["endurance"]["total"],
                           duration_min=stat(310.0, None), training_load=stat(820.4, 700.0))}
    body = render_weekly(report, PROGRESS, "en").body
    assert "Sleep: average 7 h 12 min\n" in body and "HRV: average 52.3 ms\n" in body
    assert "completion" not in body
    assert "Endurance: 4 activities, 5 h 10 min, 42.3 km, load 820\n" in body


def test_both_catalogues_have_the_same_languages_and_fields():
    assert set(WEEKLY_LANGUAGES) == set(LANGUAGES)
    en, cs = weekly_texts("en"), weekly_texts("cs")
    assert vars(en).keys() == vars(cs).keys()
    assert set(en.best) == set(cs.best) and set(en.flag) == set(cs.flag)
    assert set(en.goal_metric) == set(cs.goal_metric) and set(en.goal_reasons) == set(cs.goal_reasons)
    assert en.activities(1) == "1 activity" and en.activities(2) == "2 activities"
    assert cs.activities(1) == "1 aktivita" and cs.activities(3) == "3 aktivity" and cs.activities(5) == "5 aktivit"


@pytest.mark.parametrize("reason", ["no_recent_value", "too_few_values", "no_target_date", "target_date_passed",
                                    "target_date_too_far", "trend_too_steep"])
def test_every_goal_reason_has_a_text_in_every_language(reason):
    for language in WEEKLY_LANGUAGES:
        assert weekly_texts(language).goal_reasons[reason]


def test_compose_weekly_reads_the_store(store):
    start = date(2026, 10, 12) - timedelta(days=83)
    for n in range(83):
        day = start + timedelta(days=n)
        store.store_race_predictions({day: {"time_half_marathon_s": 6000 - 2 * n}}, day + timedelta(days=1), day)
    store.insert_goal({"title": "Half marathon sub 1:36", "metric": "race_half_marathon", "target_value": 5800.0,
                       "target_date": "2026-11-15"})
    weekly = compose_weekly(store, date(2026, 10, 12), "en")
    assert weekly.week == "2026-W41" and weekly.goals == 1
    assert "- Half marathon sub 1:36 (half marathon prediction): now 1:37:16" in weekly.message.body
    assert get_goal_progress(store, date(2026, 10, 12))["goals"][0]["available"] is True
