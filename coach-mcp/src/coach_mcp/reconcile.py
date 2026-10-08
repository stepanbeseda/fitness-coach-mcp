"""Plan reconciliation: which past planned sessions have a recorded activity of the same day and sport.

Pure functions, no I/O and no clock. The history job feeds them the stored sessions and activity totals each
night and stores the result as a suggestion; nothing here changes a session status. The module imports nothing
from the package, so ``history`` and ``briefing`` can both import it without a cycle.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

RECONCILE_DAYS = 7  # the window: today - 7 .. today - 1
MATCHED_TYPES = ("running", "cycling", "swimming", "strength")  # session type == history sport of the totals
_COUNTED_STATUSES = ("planned", "done")


@dataclass(frozen=True)
class DayActivities:
    totals: Mapping[str, Any]  # per-sport totals of one day as stored in history_days.activities
    final: bool  # no later history run re-fetches this day


@dataclass(frozen=True)
class Suggestion:
    result: Literal["matched", "no_activity"]
    final: bool
    evidence: dict[str, Any] | None  # only for matched


def _count(entry: Any) -> int:
    count = entry.get("count") if isinstance(entry, dict) else None
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return count
    return 0


def _positive(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _evidence(sport: str, entry: Mapping[str, Any], count: int) -> dict[str, Any]:
    evidence: dict[str, Any] = {"source": "garmin", "sport": sport, "count": count}
    for field in ("duration_min", "distance_km"):
        # activity_totals stores 0.0 for a missing value (e.g. the distance of strength); 0 is not a measurement.
        if _positive(entry.get(field)):
            evidence[field] = entry[field]
    return evidence


def suggest(sessions: Sequence[Mapping[str, Any]], days: Mapping[str, DayActivities]) -> dict[int, Suggestion]:
    """Suggest ``matched`` / ``no_activity`` for the planned sessions that have stored activity totals.

    Only sessions of ``MATCHED_TYPES`` with status ``planned`` or ``done`` are considered, grouped by
    (date, type); every other session gets nothing and consumes nothing. A date missing from ``days``
    (activities never fetched, given up, unreadable) gives no suggestion. The group's activity count is
    ``totals[type]["count"]`` (an int >= 0, otherwise 0); sessions already ``done`` take activities first,
    and the planned ones, in order of id, get ``matched`` while activities remain and ``no_activity``
    afterwards. ``final`` is the day's ``final``. The evidence of a match is the day's totals of the sport
    (count, and duration / distance when positive). Returns ``{session_id: Suggestion}`` for planned sessions.
    """
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for session in sessions:
        if session.get("type") in MATCHED_TYPES and session.get("status") in _COUNTED_STATUSES:
            groups.setdefault((session["date"], session["type"]), []).append(session)
    out: dict[int, Suggestion] = {}
    for (date_, type_), group in groups.items():
        day = days.get(date_)
        if day is None:
            continue
        entry = day.totals.get(type_)
        count = _count(entry)
        available = max(count - sum(1 for s in group if s["status"] == "done"), 0)
        planned = sorted((s for s in group if s["status"] == "planned"), key=lambda s: s["id"])
        for index, session in enumerate(planned):
            if index < available:
                out[session["id"]] = Suggestion("matched", day.final, _evidence(type_, entry, count))
            else:
                out[session["id"]] = Suggestion("no_activity", day.final, None)
    return out
