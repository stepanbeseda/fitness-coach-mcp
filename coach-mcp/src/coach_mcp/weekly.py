"""The Monday report: last week's numbers and the goal progress as one Telegram message (PR 14).

``render_weekly`` is pure: it turns the ``get_weekly_report`` and ``get_goal_progress`` results into a
``PushMessage`` with the words of the language catalogue (``messages.WEEKLY_LANGUAGES``). It reads only the keys
it uses and skips a section that is missing or ``available: false``, an unknown best and an unknown flag, so a
later addition to ``trends.py`` cannot break the push.
"""

from __future__ import annotations

from datetime import date
from typing import Any, TypeGuard

from .briefing import MAX_MESSAGE_BYTES, _guard_bytes, _one_line, fmt
from .messages import WeeklyTexts, texts, weekly_texts
from .push import PushMessage


def _duration(w: WeeklyTexts, minutes: float) -> str:
    hours, rest = divmod(round(minutes), 60)
    return w.hours_minutes.format(h=hours, m=f"{rest:02d}") if hours else w.minutes_only.format(m=rest)


def _available(section: Any) -> TypeGuard[dict[str, Any]]:
    return isinstance(section, dict) and section.get("available") is not False


def _stat(section: Any, key: str) -> dict[str, Any] | None:
    value = section.get(key) if isinstance(section, dict) else None
    return value if isinstance(value, dict) and value.get("week") is not None else None


def _goal_value(metric: str, value: dict[str, Any], sep: str) -> str:
    return value["time"] if metric.startswith("race_") else fmt(value["value"], sep)


def _trend(metric: str, per_week: float, sep: str) -> str:
    if metric.startswith("race_"):
        return f"{round(per_week):+d} s"
    return f"{per_week:+.2f}".replace(".", sep)


def _goal_lines(w: WeeklyTexts, progress: dict[str, Any], day_title: Any, sep: str) -> list[str]:
    lines = [w.goals]
    for goal in progress["goals"]:
        metric = goal["metric"]
        title, name = _one_line(goal["title"], 60), w.goal_metric[metric]
        reason = w.goal_reasons.get(goal.get("reason") or "", "")
        if not goal["available"]:
            lines.append(w.goal_unavailable.format(title=title, metric=name, reason=reason))
            continue
        ago = "" if goal["weeks_4_ago"] is None else w.goal_ago.format(value=_goal_value(metric, goal["weeks_4_ago"], sep))
        trend = w.goal_trend.format(change=_trend(metric, goal["trend_per_week"], sep))
        projected = goal["projected"]
        if projected is None:
            outlook = w.goal_reason.format(reason=reason)
        else:
            day = day_title(date.fromisoformat(projected["date"]))
            value = _goal_value(metric, projected, sep)
            if goal["on_track"] is None:
                outlook = w.goal_projection.format(projected=value, date=day)
            else:
                template = w.goal_on_track if goal["on_track"] else w.goal_behind
                outlook = template.format(
                    projected=value, date=day, target=_goal_value(metric, goal["target_value"], sep)
                )
        lines.append(
            w.goal_line.format(
                title=title,
                metric=name,
                current=_goal_value(metric, goal["current"], sep),
                ago=ago,
                trend=trend,
                outlook=outlook,
            )
        )
    return lines


def render_weekly(report: dict[str, Any], progress: dict[str, Any], language: str) -> PushMessage:
    w, t = weekly_texts(language), texts(language)
    sep = t.decimal_separator
    lines: list[str] = []
    endurance = report.get("endurance")
    if _available(endurance):
        total = endurance["total"]
        duration, load = total["duration_min"], total["training_load"]
        average = ""
        if duration["baseline"] is not None and load["baseline"] is not None:
            average = w.endurance_average.format(
                time=_duration(w, duration["baseline"]), load=fmt(round(load["baseline"]), sep)
            )
        lines.append(
            w.endurance.format(
                activities=w.activities(total["count"]),
                time=_duration(w, duration["week"]),
                km=fmt(total["distance_km"]["week"], sep),
                load=fmt(round(load["week"]), sep),
                average=average,
            )
        )
    strength = report.get("strength")
    if _available(strength):
        sets, volume = strength["sets"], strength["volume_kg"]
        average = ""
        if sets["baseline"] is not None and volume["baseline"] is not None:
            average = w.strength_average.format(
                sets=fmt(sets["baseline"], sep), volume=fmt(round(volume["baseline"]), sep)
            )
        lines.append(
            w.strength.format(
                workouts=w.workouts(strength["workouts"]),
                sets=fmt(sets["week"], sep),
                volume=fmt(round(volume["week"]), sep),
                average=average,
            )
        )
    sleep = _stat(report.get("sleep"), "duration_h")
    if sleep:
        average = "" if sleep["baseline"] is None else w.average.format(value=_duration(w, sleep["baseline"] * 60))
        lines.append(w.sleep.format(time=_duration(w, sleep["week"] * 60), average=average))
    hrv = _stat(report.get("hrv"), "last_night_ms")
    if hrv:
        average = "" if hrv["baseline"] is None else w.average.format(value=f"{fmt(hrv['baseline'], sep)} ms")
        lines.append(w.hrv.format(value=fmt(hrv["week"], sep), average=average))
    resting = _stat(report.get("resting_hr"), "bpm")
    if resting:
        average = "" if resting["baseline"] is None else w.average.format(value=f"{fmt(resting['baseline'], sep)} bpm")
        lines.append(w.resting_hr.format(value=fmt(resting["week"], sep), average=average))
    plan = report.get("adherence")
    if _available(plan):
        rate = plan.get("completion_rate")
        lines.append(
            w.plan.format(
                done=plan["done"],
                skipped=plan["skipped"],
                moved=plan["moved"],
                planned=plan["planned"],
                rate="" if rate is None else w.plan_rate.format(rate=round(rate * 100)),
            )
        )
    bests = []
    for best in report.get("new_bests") or []:
        template = w.best.get(best.get("metric"))
        if template is None:
            continue
        value, previous = best["value"], best["previous_best"]
        if best["metric"] == "endurance_duration_min":
            value, previous = _duration(w, value), _duration(w, previous)
        else:
            value, previous = fmt(value, sep), fmt(previous, sep)
        exercise = _one_line(best.get("exercise") or t.untitled, 60)
        bests.append(template.format(value=value, previous=previous, exercise=exercise))
    if bests:
        lines.append(w.new_bests.format(items="; ".join(bests)))
    flags = []
    for flag in report.get("flags") or []:
        template = w.flag.get(flag.get("flag"))
        if template is None:
            continue
        pct, change = flag.get("change_pct"), flag.get("change")
        flags.append(
            template.format(
                pct=fmt(abs(pct), sep) if pct is not None else "?",
                minutes=round(abs(change) * 60) if change is not None else "?",
                bpm=fmt(abs(change), sep) if change is not None else "?",
            )
        )
    if flags:
        lines.append(w.flags.format(items="; ".join(flags)))
    if not lines:
        lines.append(w.no_data)
    if progress.get("goals"):
        lines.extend(_goal_lines(w, progress, t.day_title, sep))
    elif progress.get("goals_without_metric"):
        lines.append(w.goals_without_metric.format(n=progress["goals_without_metric"]))
    start, end = (date.fromisoformat(report["week"][key]) for key in ("start", "end"))
    title = w.title.format(week=report["week"]["iso_week"], days=f"{t.day_title(start)}–{t.day_title(end)}")
    body = _guard_bytes("\n".join(lines), MAX_MESSAGE_BYTES - len(title.encode()) - 2)
    return PushMessage(title, body)
