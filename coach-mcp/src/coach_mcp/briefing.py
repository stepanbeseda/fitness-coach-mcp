"""Morning briefing rules and rendering: pure functions, no I/O and no clock.

The verdict (green / yellow / red) comes from four criteria taken from the Garmin
recovery snapshot. A missing input never makes the verdict greener: it caps it at
yellow and is named in the message. Alerts are conditions evaluated once a day; an
alert is *new* when it holds now and did not hold at the previous evaluation, except event alerts
(yesterday's missed workout in Garmin's calendar and yesterday's planned session without an activity), which
are new whenever they hold.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Literal

from .messages import DEFAULT_LANGUAGE, Texts, texts
from .push import PushMessage
from .reconcile import MATCHED_TYPES

if TYPE_CHECKING:
    from .garmin import GarminData

Level = Literal["green", "yellow", "red"]

CRITERIA = ("readiness", "hrv", "sleep", "body_battery")
ALERT_KEYS = (
    "hrv_below_baseline",
    "resting_hr_elevated",
    "acwr_out_of_range",
    "missed_workout",
    "missed_planned_session",
    "garmin_auth",
)
# Event alerts are about a different object each day (yesterday's workouts and planned sessions), so they are
# not edge-triggered: "it held yesterday too" must not hide today's new event.
EVENT_ALERTS = frozenset({"missed_workout", "missed_planned_session"})
# Garmin acwrStatus values that mean "outside the optimal range" (each catalogue in ``messages`` names
# them). Any other value (including "OPTIMAL") never fires; the raw string never reaches the message.
ACWR_OUT = frozenset({"LOW", "HIGH", "VERY_HIGH"})

READINESS_GREEN, READINESS_YELLOW = 70, 40
SLEEP_GREEN_MIN, SLEEP_YELLOW_MIN = 420, 360
BODY_BATTERY_GREEN = 50
HRV_RED_NIGHTS = 3
HRV_STREAK_DAYS = 7
RESTING_HR_DELTA = 5
MAX_MESSAGE_BYTES = 4000

_RANK = {"green": 0, "yellow": 1, "red": 2}
# Coloured circle per verdict (large green / yellow / red circle) and the warning sign for new alerts.
_CIRCLE = {"green": "\U0001f7e2", "yellow": "\U0001f7e1", "red": "\U0001f534"}
_WARNING = "\u26a0\ufe0f"
_WS = re.compile(r"\s+")


# ------------------------------------------------------------------ helpers


def _num(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def _section(snapshot: Mapping[str, Any] | None, name: str) -> Mapping[str, Any] | None:
    """The snapshot section, or None when it is absent, not a dict, an error or unavailable."""
    if not isinstance(snapshot, Mapping):
        return None
    section = snapshot.get(name)
    if not isinstance(section, Mapping) or "error" in section or section.get("available") is False:
        return None
    return section


def fmt(value: float | int, separator: str = ".") -> str:
    """An integer when it is whole, otherwise one decimal with the language's decimal separator."""
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.1f}".replace(".", separator)


def fmt_ratio(value: float | int, separator: str = ".") -> str:
    return f"{float(value):.2f}".replace(".", separator)


def _one_line(text: Any, limit: int) -> str:
    collapsed = _WS.sub(" ", str(text)).strip()
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "…"


def _baseline(section: Mapping[str, Any] | None) -> tuple[float | int, float | int | None] | None:
    """(low, high) from a ``baseline_balanced_ms`` list; ``high`` is None for a one-element list.

    garmin-mcp drops None list items, so a single element is always the lower bound.
    """
    if section is None:
        return None
    raw = section.get("baseline_balanced_ms")
    if not isinstance(raw, list) or not raw:
        return None
    low = _num(raw[0])
    if low is None:
        return None
    return low, _num(raw[1]) if len(raw) > 1 else None


# ------------------------------------------------------------ criteria levels


def readiness_level(score: float | int) -> Level:
    if score >= READINESS_GREEN:
        return "green"
    return "yellow" if score >= READINESS_YELLOW else "red"


def sleep_minutes(duration_h: float | int) -> int:
    return round(duration_h * 60)


def sleep_level(minutes: int) -> Level:
    if minutes >= SLEEP_GREEN_MIN:
        return "green"
    return "yellow" if minutes >= SLEEP_YELLOW_MIN else "red"


def body_battery_level(at_wake: float | int) -> Level:
    return "green" if at_wake >= BODY_BATTERY_GREEN else "yellow"


def hrv_level(last: float | int, low: float | int, nights_below: int | None) -> Level:
    if last >= low:
        return "green"
    return "red" if nights_below is not None and nights_below >= HRV_RED_NIGHTS else "yellow"


def nights_below_baseline(trend: Mapping[str, Any] | None, day: date) -> int | None:
    """Consecutive nights up to ``day`` (at most 7) with the last-night HRV below the baseline.

    None when there is no trend or no complete entry for ``day``; a missing date or value ends the streak.
    """
    if not isinstance(trend, Mapping) or not isinstance(trend.get("days"), list):
        return None
    by_date: dict[str, Mapping[str, Any]] = {
        d["date"]: d for d in trend["days"] if isinstance(d, Mapping) and isinstance(d.get("date"), str)
    }

    def below(on: date) -> bool | None:
        entry = by_date.get(on.isoformat())
        if entry is None:
            return None
        last, base = _num(entry.get("last_night_avg_ms")), _baseline(entry)
        if last is None or base is None:
            return None
        return last < base[0]

    first = below(day)
    if first is None:
        return None
    count = 0
    for offset in range(HRV_STREAK_DAYS):
        if below(day - timedelta(days=offset)) is not True:
            break
        count += 1
    return count


def criteria_from_snapshot(snapshot: Mapping[str, Any] | None, nights_below: int | None) -> dict[str, dict[str, Any]]:
    """The four criteria: ``{key: {"level": Level | None, ...numbers}}``; ``level`` None means missing."""
    out: dict[str, dict[str, Any]] = {key: {"level": None} for key in CRITERIA}

    readiness = _section(snapshot, "readiness")
    if readiness is not None:
        score = _num(readiness.get("morning_score"))
        if score is None:
            score = _num(readiness.get("score"))
        if score is not None:
            out["readiness"] = {"level": readiness_level(score), "score": score}

    hrv = _section(snapshot, "hrv")
    last = _num(hrv.get("last_night_avg_ms")) if hrv is not None else None
    base = _baseline(hrv)
    if last is not None and base is not None:
        out["hrv"] = {
            "level": hrv_level(last, base[0], nights_below),
            "last_night_ms": last,
            "baseline_ms": [base[0]] if base[1] is None else [base[0], base[1]],
            "nights_below": nights_below,
        }

    sleep = _section(snapshot, "sleep")
    duration = _num(sleep.get("duration_h")) if sleep is not None else None
    if sleep is not None and duration is not None:
        minutes = sleep_minutes(duration)
        entry: dict[str, Any] = {"level": sleep_level(minutes), "minutes": minutes}
        score = _num(sleep.get("score"))
        if score is not None:
            entry["score"] = score
        out["sleep"] = entry

    battery = _section(snapshot, "body_battery")
    at_wake = _num(battery.get("at_wake")) if battery is not None else None
    if at_wake is not None:
        out["body_battery"] = {"level": body_battery_level(at_wake), "at_wake": at_wake}
    return out


def decide_verdict(levels: Mapping[str, Level | None]) -> tuple[Level, list[str]]:
    """Worst present level; a missing (None) input caps a would-be green at yellow and is named."""
    missing = [key for key in CRITERIA if levels.get(key) is None]
    present = [level for key in CRITERIA if (level := levels.get(key)) is not None]
    verdict: Level = max(present, key=_RANK.__getitem__) if present else "yellow"
    if missing and verdict == "green":
        verdict = "yellow"
    return verdict, missing


# --------------------------------------------------------------------- alerts


def _acwr(snapshot: Mapping[str, Any] | None) -> tuple[str | None, float | int | None]:
    status = _section(snapshot, "training_status")
    if status is None:
        return None, None
    raw = status.get("acwr_status")
    normalized = raw.strip().upper() if isinstance(raw, str) else None
    return normalized, _num(status.get("acwr_ratio"))


def _scheduled_entries(scheduled: Mapping[str, Any] | None, on: date) -> list[Mapping[str, Any]] | None:
    if not isinstance(scheduled, Mapping) or not isinstance(scheduled.get("scheduled"), list):
        return None
    return [e for e in scheduled["scheduled"] if isinstance(e, Mapping) and e.get("date") == on.isoformat()]


def _resting_hr(snapshot: Mapping[str, Any] | None) -> tuple[float | int, float | int] | None:
    section = _section(snapshot, "resting_hr")
    if section is None:
        return None
    today, avg = _num(section.get("today")), _num(section.get("avg_7d"))
    return (today, avg) if today is not None and avg is not None else None


def _yesterday_matchable(yesterday_sessions: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [s for s in yesterday_sessions if s.get("status") == "planned" and s.get("type") in MATCHED_TYPES]


def _suggested(session: Mapping[str, Any]) -> str | None:
    suggestion = session.get("suggestion")
    return suggestion.get("result") if isinstance(suggestion, Mapping) else None


def missed_session_condition(yesterday_sessions: Sequence[Mapping[str, Any]]) -> bool | None:
    """Yesterday's planned (running/cycling/swimming/strength) sessions: True when one has no activity,
    False when all are matched (or there are none), None when one has no suggestion yet."""
    results = [_suggested(s) for s in _yesterday_matchable(yesterday_sessions)]
    if "no_activity" in results:
        return True
    if all(r == "matched" for r in results):
        return False
    return None


def _type_name(t: Texts, kind: Any) -> str:
    return t.session_type.get(kind, t.session_type["other"])


def _missed_session_names(t: Texts, yesterday_sessions: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        f"{_type_name(t, s.get('type'))} – {_one_line(s.get('description', ''), 80)}"
        for s in _yesterday_matchable(yesterday_sessions)
        if _suggested(s) == "no_activity"
    ]


def alert_conditions(
    day: date,
    data: GarminData,
    nights_below: int | None,
    yesterday_sessions: Sequence[Mapping[str, Any]] = (),
) -> dict[str, bool | None]:
    """``{key: True | False | None}`` in ``ALERT_KEYS`` order; None means the condition is unknown."""
    out: dict[str, bool | None] = {}

    out["hrv_below_baseline"] = None if nights_below is None else nights_below >= HRV_RED_NIGHTS

    rhr = _resting_hr(data.snapshot)
    out["resting_hr_elevated"] = None if rhr is None else rhr[0] - rhr[1] >= RESTING_HR_DELTA

    status, _ratio = _acwr(data.snapshot)
    if status in ACWR_OUT:
        out["acwr_out_of_range"] = True
    elif status == "OPTIMAL":
        out["acwr_out_of_range"] = False
    else:
        out["acwr_out_of_range"] = None

    yesterday = _scheduled_entries(data.scheduled, day - timedelta(days=1))
    if yesterday is None:
        out["missed_workout"] = None
    elif any(e.get("completed") is False for e in yesterday):
        out["missed_workout"] = True
    elif all(e.get("completed") is True for e in yesterday):  # also true for an empty list
        out["missed_workout"] = False
    else:
        out["missed_workout"] = None

    out["missed_planned_session"] = missed_session_condition(yesterday_sessions)

    if data.auth_problem:
        out["garmin_auth"] = True
    elif data.auth == "ok":
        out["garmin_auth"] = False
    else:
        out["garmin_auth"] = None
    return out


def is_new(holds: bool | None, row: Mapping[str, Any] | None, day: date, *, event: bool = False) -> bool:
    """Holds now and (an event alert, or never seen, or did not hold last time, or was already announced today)."""
    if event:
        return holds is True
    return holds is True and (row is None or row["value"] == 0 or row["fired_on"] == day.isoformat())


# -------------------------------------------------------------------- briefing


@dataclass(frozen=True)
class Briefing:
    payload: dict[str, Any]
    message: PushMessage
    verdict: Level
    waiting: bool
    alert_updates: dict[str, tuple[bool, bool]]


def _garmin_today(t: Texts, scheduled: Mapping[str, Any] | None, day: date) -> list[str]:
    entries = _scheduled_entries(scheduled, day) or []
    return [_one_line(e.get("name") or t.untitled, 80) for e in entries]


def _missed_names(t: Texts, scheduled: Mapping[str, Any] | None, day: date) -> list[str]:
    entries = _scheduled_entries(scheduled, day - timedelta(days=1)) or []
    return [_one_line(e.get("name") or t.untitled, 80) for e in entries if e.get("completed") is False]


def hrv_night_present(snapshot: Mapping[str, Any] | None) -> bool:
    """Last night's HRV value is there (the baseline may still be missing)."""
    section = _section(snapshot, "hrv")
    return section is not None and _num(section.get("last_night_avg_ms")) is not None


def _missing_reason(t: Texts, data: GarminData, missing: Sequence[str]) -> str:
    if data.auth_problem:
        return t.reason_auth
    if data.credentials_missing:
        return t.reason_no_token
    if data.auth is None:
        return t.reason_unreachable
    if data.snapshot is None:
        return t.reason_no_data
    if list(missing) == ["hrv"] and hrv_night_present(data.snapshot):
        return t.reason_baseline_pending
    return t.reason_not_synced


def _hrv_line(t: Texts, entry: Mapping[str, Any]) -> str:
    sep = t.decimal_separator
    last = fmt(entry["last_night_ms"], sep)
    low = fmt(entry["baseline_ms"][0], sep)
    high = entry["baseline_ms"][1] if len(entry["baseline_ms"]) > 1 else None
    baseline = t.baseline_from.format(low=low) if high is None else t.baseline_range.format(low=low, high=fmt(high, sep))
    below = ""
    if entry["level"] != "green":
        below = t.below_baseline
        nights = entry["nights_below"]
        if nights is not None and nights >= 1:
            below += t.in_a_row.format(nights=t.nights(nights))
    return f"{t.hrv}: {last} ms, {baseline}{below} ({t.level[entry['level']]})"


def _session_line(t: Texts, session: Mapping[str, Any]) -> str:
    sep = t.decimal_separator
    parts = []
    if session.get("duration_min") is not None:
        parts.append(f"{fmt(session['duration_min'], sep)} min")
    if session.get("distance_km") is not None:
        parts.append(f"{fmt(session['distance_km'], sep)} km")
    if session.get("intensity"):
        parts.append(_one_line(session["intensity"], 120))
    details = f" ({', '.join(parts)})" if parts else ""
    kind = _type_name(t, session.get("type"))
    status = t.status_suffix.get(session.get("status"), "")
    return f"- {kind}: {_one_line(session.get('description', ''), 120)}{details}{status}"


def _alert_text(
    t: Texts, key: str, data: GarminData, day: date, nights_below: int | None, missed_sessions: Sequence[str] = ()
) -> str:
    sep = t.decimal_separator
    if key == "hrv_below_baseline":
        return t.alert_hrv.format(nights=t.nights(nights_below or 0))
    if key == "resting_hr_elevated":
        rhr = _resting_hr(data.snapshot)
        assert rhr is not None
        return t.alert_resting_hr.format(
            today=fmt(rhr[0], sep), delta=fmt(rhr[0] - rhr[1], sep), avg=fmt(rhr[1], sep)
        )
    if key == "acwr_out_of_range":
        status, ratio = _acwr(data.snapshot)
        word = t.acwr[status or ""]
        if ratio is None:
            return t.alert_acwr.format(word=word)
        return t.alert_acwr_ratio.format(ratio=fmt_ratio(ratio, sep), word=word)
    if key == "missed_workout":
        return t.alert_missed_workout.format(names=", ".join(_missed_names(t, data.scheduled, day)))
    if key == "missed_planned_session":
        return t.alert_missed_session.format(sessions="; ".join(missed_sessions))
    return t.alert_garmin_auth


def _guard_bytes(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` UTF-8 bytes. Telegram accepts at most 4096 characters per message, and a
    UTF-8 byte count is never smaller than the character count (UTF-16 units included), so a byte limit
    below 4096 always fits."""
    raw = text.encode()
    if len(raw) <= limit:
        return text
    return raw[: limit - 3].decode(errors="ignore") + "…"


def render(
    day: date,
    verdict: Level,
    criteria: Mapping[str, Mapping[str, Any]],
    missing: Sequence[str],
    data: GarminData,
    planned: Sequence[Mapping[str, Any]],
    garmin_today: Sequence[str],
    new_alerts: Sequence[str],
    nights_below: int | None,
    missed_sessions: Sequence[str] = (),
    language: str = DEFAULT_LANGUAGE,
) -> PushMessage:
    t = texts(language)
    sep = t.decimal_separator
    lines = [t.verdict.format(level=t.level[verdict], advice=t.advice[verdict])]

    def level_of(key: str) -> str:
        return t.level[criteria[key]["level"]]

    if "readiness" in missing:
        lines.append(f"{t.readiness}: {t.missing_value}")
    else:
        lines.append(f"{t.readiness}: {fmt(criteria['readiness']['score'], sep)} ({level_of('readiness')})")
    lines.append(f"{t.hrv}: {t.missing_value}" if "hrv" in missing else _hrv_line(t, criteria["hrv"]))
    if "sleep" in missing:
        lines.append(f"{t.sleep}: {t.missing_value}")
    else:
        sleep = criteria["sleep"]
        minutes = sleep["minutes"]
        score = t.sleep_score.format(score=fmt(sleep["score"], sep)) if "score" in sleep else ""
        lines.append(
            t.sleep_value.format(h=minutes // 60, m=f"{minutes % 60:02d}", score=score, level=level_of("sleep"))
        )
    if "body_battery" in missing:
        lines.append(f"{t.body_battery}: {t.missing_value}")
    else:
        at_wake = fmt(criteria["body_battery"]["at_wake"], sep)
        lines.append(f"{t.body_battery}: {at_wake} ({level_of('body_battery')})")

    if missing:
        night = hrv_night_present(data.snapshot)
        names = ", ".join(t.hrv_baseline if key == "hrv" and night else t.criterion[key] for key in missing)
        lines.append(t.missing_line.format(names=names, reason=_missing_reason(t, data, missing)))

    # Present criteria that are not green; a green verdict has none, so the line is only for yellow / red.
    items = []
    for key in CRITERIA:
        if key in missing or criteria[key]["level"] == "green":
            continue
        if key == "readiness":
            items.append(t.target_readiness.format(value=READINESS_GREEN))
        elif key == "hrv":
            items.append(t.target_hrv.format(value=fmt(criteria["hrv"]["baseline_ms"][0], sep)))
        elif key == "sleep":
            items.append(t.target_sleep)
        else:
            items.append(t.target_body_battery.format(value=BODY_BATTERY_GREEN))
    if items:
        lines.append(t.full_load.format(items="; ".join(items)))

    if planned:
        lines.append(t.today_plan)
        lines.extend(_session_line(t, s) for s in planned)
    elif garmin_today:
        lines.append(t.today_garmin.format(names=", ".join(garmin_today)))
    else:
        lines.append(t.today_nothing)

    if new_alerts:
        lines.append(t.alerts)
        lines.extend(f"- {_alert_text(t, key, data, day, nights_below, missed_sessions)}" for key in new_alerts)

    alerting = bool(new_alerts)
    title = t.title.format(
        circle=_CIRCLE[verdict],
        warning=f" {_WARNING}" if alerting else "",
        level=t.level[verdict].upper(),
        day=t.day_title(day),
    )
    # Title, blank line and body together must fit MAX_MESSAGE_BYTES.
    body = _guard_bytes("\n".join(lines), MAX_MESSAGE_BYTES - len(title.encode()) - 2)
    return PushMessage(title=title, body=body)


def build_briefing(
    day: date,
    data: GarminData,
    sessions: Sequence[Mapping[str, Any]],
    alert_rows: Mapping[str, Mapping[str, Any]],
    yesterday_sessions: Sequence[Mapping[str, Any]] = (),
    language: str = DEFAULT_LANGUAGE,
) -> Briefing:
    t = texts(language)
    nights = nights_below_baseline(data.hrv_trend, day)
    criteria = criteria_from_snapshot(data.snapshot, nights)
    verdict, missing = decide_verdict({key: criteria[key]["level"] for key in CRITERIA})
    conditions = alert_conditions(day, data, nights, yesterday_sessions)
    new = {
        key: is_new(holds, alert_rows.get(key), day, event=key in EVENT_ALERTS) for key, holds in conditions.items()
    }
    new_alerts = [key for key in ALERT_KEYS if new[key]]
    garmin_today = _garmin_today(t, data.scheduled, day)
    planned = list(sessions)

    payload_criteria: dict[str, Any] = {}
    for key in CRITERIA:
        entry = criteria[key]
        payload_criteria[key] = (
            {"level": "missing"} if entry["level"] is None else {k: v for k, v in entry.items()}
        )
    message = render(
        day,
        verdict,
        criteria,
        missing,
        data,
        planned,
        garmin_today,
        new_alerts,
        nights,
        _missed_session_names(t, yesterday_sessions),
        language,
    )
    payload = {
        "date": day.isoformat(),
        "verdict": verdict,
        "criteria": payload_criteria,
        "missing": missing,
        "planned": planned,
        "garmin_today": garmin_today,
        "alerts": [{"key": key, "holds": conditions[key], "new": new[key]} for key in ALERT_KEYS],
        "garmin": {
            "auth": "no_credentials" if data.credentials_missing else data.auth or "unreachable",
            "calls": dict(data.calls),
        },
        "push": {"text": message.text},
    }
    # Wait only for what can still arrive during the morning: last night's sleep and HRV value (the
    # HRV baseline does not appear within hours, so a missing baseline never waits).
    sleep_or_hrv_missing = "sleep" in missing or not hrv_night_present(data.snapshot)
    return Briefing(
        payload=payload,
        message=message,
        verdict=verdict,
        waiting=sleep_or_hrv_missing and not data.auth_problem,
        alert_updates={key: (holds, new[key]) for key, holds in conditions.items() if holds is not None},
    )
