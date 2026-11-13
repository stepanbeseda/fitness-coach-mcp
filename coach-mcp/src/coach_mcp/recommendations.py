"""Recommendations of the Monday report (PR 39): a pure rule table over the ``get_weekly_report`` result.

Only rules and numbers live here; the sentences are ``messages.WeeklyTexts.recommendation`` (one per item key, in
every catalogue) and ``weekly.render_weekly`` formats the numbers. ``recommend`` reads only the keys it uses: an
unknown flag is ignored, a flag without the numbers a rule needs gives no recommendation. The flags themselves need
a final week (``trends._flags``), and so do the rules over them; only the unconfirmed-sessions rule also holds for a
partial week. An item is ``{"key", "source", "numbers"}``: no prose, so the coach in claude.ai and the Telegram
message say the same thing.

A later signal (wave 2: deload, muscle gap) adds one ``Rule`` row with a free priority and one sentence per
catalogue; numbers named ``*_h`` / ``*_min`` render as a duration, other numbers through ``fmt``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Replaced by `from .load import ACWR_LOW, SPIKE_FACTOR, WINDOW_DAYS` after the rebase onto PR 38 (spec §11).
ACWR_LOW = 0.8
SPIKE_FACTOR = 1.5
WINDOW_DAYS = 14

MAX_RECOMMENDATIONS = 3  # sentences in the Monday report and items in get_weekly_report
MAX_RECORDS = 3  # suspect exercise-days named in the one record-check sentence, then "+N"
CAP_PCT = 30.0  # the cap of a load or time increase; equal to trends.FLAG_PCT (a test pins it; trends imports us)
EASY_DAYS = 2  # easy days in the load-up, HRV and resting-HR sentences (cs "dny" agrees with 2-4)
RETURN_SESSIONS = 3  # easy runs / sessions after detraining (cs "běhy" / "tréninky" agree with 2-4)
PLANNED_MATCHED = "adherence.planned_matched"  # the source of the one rule that does not read a flag (PR 36)

Numbers = dict[str, Any]
Params = Callable[[Mapping[str, Any], Sequence[Mapping[str, Any]]], tuple[str, Numbers] | None]


@dataclass(frozen=True)
class Rule:
    name: str  # the rule (the flag name of a flag rule); what ``hides`` refers to
    source: str  # a ``get_weekly_report`` flag name, or PLANNED_MATCHED
    priority: int  # lower comes first; steps of 10 leave room for later rules
    hides: tuple[str, ...]  # rules with the same cause, dropped when this one fires
    params: Params  # (report, the flags of ``source``) -> (item key, numbers), or None: no recommendation


def _get(mapping: Mapping[str, Any], key: str) -> Any:
    """``mapping.get(key)`` typed as Any: what ``_numbers`` accepted is then a number for the type checker too."""
    return mapping.get(key)


def _numbers(*values: Any) -> bool:
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)


def _pct(flag: Mapping[str, Any]) -> float | None:
    pct = _get(flag, "change_pct")
    return abs(float(pct)) if _numbers(pct) else None


def _load_spike(_: Mapping[str, Any], flags: Sequence[Mapping[str, Any]]) -> tuple[str, Numbers] | None:
    week, baseline = _get(flags[0], "week"), _get(flags[0], "baseline")
    if not _numbers(week, baseline):
        return None
    return "load_spike", {"sets": week, "baseline_sets": baseline, "max_sets": round(baseline * SPIKE_FACTOR),
                          "factor": SPIKE_FACTOR}


def _endurance_load_up(_: Mapping[str, Any], flags: Sequence[Mapping[str, Any]]) -> tuple[str, Numbers] | None:
    pct, baseline = _pct(flags[0]), _get(flags[0], "baseline")
    if pct is None or not _numbers(baseline):
        return None
    return "endurance_load_up", {"pct": pct, "max_load": round(baseline * (1 + CAP_PCT / 100)), "cap_pct": CAP_PCT,
                                 "easy_days": EASY_DAYS}


def _hrv_down(_: Mapping[str, Any], flags: Sequence[Mapping[str, Any]]) -> tuple[str, Numbers] | None:
    pct = _pct(flags[0])
    return None if pct is None else ("hrv_down", {"pct": pct, "easy_days": EASY_DAYS})


def _resting_hr_up(_: Mapping[str, Any], flags: Sequence[Mapping[str, Any]]) -> tuple[str, Numbers] | None:
    change = _get(flags[0], "change")
    return None if not _numbers(change) else ("resting_hr_up", {"bpm": abs(change), "easy_days": EASY_DAYS})


def _sleep_down(_: Mapping[str, Any], flags: Sequence[Mapping[str, Any]]) -> tuple[str, Numbers] | None:
    change, baseline = _get(flags[0], "change"), _get(flags[0], "baseline")
    if not _numbers(change, baseline):
        return None
    return "sleep_down", {"minutes": round(abs(change) * 60), "baseline_h": baseline}


def _detraining(_: Mapping[str, Any], flags: Sequence[Mapping[str, Any]]) -> tuple[str, Numbers] | None:
    flag = flags[0]
    goal = flag.get("goal")
    metric = goal.get("metric") if isinstance(goal, Mapping) else None
    if not isinstance(metric, str):
        return None
    conditions = flag.get("conditions") or []
    running = flag.get("running_km")
    running = running if isinstance(running, Mapping) else {}
    week, baseline = _get(running, "week"), _get(running, "baseline")
    if "running" in conditions and _numbers(week, baseline) and math.floor(baseline) >= 1:
        return "detraining_running", {"km_week": week, "baseline_km": baseline, "runs": RETURN_SESSIONS,
                                      "km": math.floor(baseline), "goal": metric}
    acwr = flag.get("acwr_last")
    if "acwr" in conditions and _numbers(acwr):
        return "detraining_acwr", {"acwr": acwr, "acwr_limit": ACWR_LOW, "days": WINDOW_DAYS,
                                   "easy_sessions": RETURN_SESSIONS, "goal": metric}
    return None


def _endurance_time_up(_: Mapping[str, Any], flags: Sequence[Mapping[str, Any]]) -> tuple[str, Numbers] | None:
    pct, baseline = _pct(flags[0]), _get(flags[0], "baseline")
    if pct is None or not _numbers(baseline):
        return None
    return "endurance_time_up", {"pct": pct, "max_time_min": round(baseline * (1 + CAP_PCT / 100)),
                                 "cap_pct": CAP_PCT}


def _pct_only(key: str) -> Params:
    def params(_: Mapping[str, Any], flags: Sequence[Mapping[str, Any]]) -> tuple[str, Numbers] | None:
        pct = _pct(flags[0])
        return None if pct is None else (key, {"pct": pct})

    return params


def _record_suspects(_: Mapping[str, Any], flags: Sequence[Mapping[str, Any]]) -> tuple[str, Numbers] | None:
    records = [
        {"exercise": f.get("exercise"), "date": f.get("date"), "weight_kg": f.get("weight_kg"), "reps": f.get("reps")}
        for f in flags
        if isinstance(f.get("date"), str) and _numbers(f.get("weight_kg"), f.get("reps"))
    ]
    if not records:
        return None
    return "strength_record_suspect", {"records": records[:MAX_RECORDS], "more": max(0, len(records) - MAX_RECORDS)}


def _unconfirmed(report: Mapping[str, Any], _: Sequence[Mapping[str, Any]]) -> tuple[str, Numbers] | None:
    adherence = report.get("adherence")
    count = adherence.get("planned_matched") if isinstance(adherence, Mapping) else None
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return None
    return "unconfirmed_sessions", {"sessions": count}


# Safety first. Wave 2 (deload, muscle gap) inserts its rows at a free priority between these.
RULES: tuple[Rule, ...] = (
    Rule("load_spike", "load_spike", 10, ("strength_volume_up",), _load_spike),
    Rule("endurance_load_up", "endurance_load_up", 20, ("endurance_time_up",), _endurance_load_up),
    Rule("hrv_down", "hrv_down", 30, ("resting_hr_up",), _hrv_down),
    Rule("resting_hr_up", "resting_hr_up", 40, (), _resting_hr_up),
    Rule("sleep_down", "sleep_down", 50, (), _sleep_down),
    Rule("detraining", "detraining", 60, ("endurance_load_down",), _detraining),
    Rule("endurance_time_up", "endurance_time_up", 70, (), _endurance_time_up),
    Rule("strength_volume_up", "strength_volume_up", 80, (), _pct_only("strength_volume_up")),
    Rule("endurance_load_down", "endurance_load_down", 90, (), _pct_only("endurance_load_down")),
    Rule("strength_record_suspect", "strength_record_suspect", 100, (), _record_suspects),
    Rule("unconfirmed_sessions", PLANNED_MATCHED, 110, (), _unconfirmed),
)
ITEM_KEYS = (
    "load_spike", "endurance_load_up", "hrv_down", "resting_hr_up", "sleep_down", "detraining_running",
    "detraining_acwr", "endurance_time_up", "strength_volume_up", "endurance_load_down", "strength_record_suspect",
    "unconfirmed_sessions",
)  # every key an item can carry: each has a sentence in every catalogue (test)


def recommend(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The recommendations of one ``get_weekly_report`` result: the rules that hold, by priority, without the rules
    a holding rule hides, at most MAX_RECOMMENDATIONS."""
    flags = [f for f in report.get("flags") or [] if isinstance(f, Mapping)]
    week = report.get("week")
    final = isinstance(week, Mapping) and week.get("partial") is False
    fired: list[tuple[Rule, tuple[str, Numbers]]] = []
    for rule in sorted(RULES, key=lambda r: r.priority):
        if rule.source == PLANNED_MATCHED:
            out = rule.params(report, [])
        else:
            matching = [f for f in flags if f.get("flag") == rule.source]
            out = rule.params(report, matching) if final and matching else None
        if out is not None:
            fired.append((rule, out))
    hidden = {name for rule, _ in fired for name in rule.hides}
    items = [{"key": key, "source": rule.source, "numbers": numbers}
             for rule, (key, numbers) in fired if rule.name not in hidden]
    return items[:MAX_RECOMMENDATIONS]
