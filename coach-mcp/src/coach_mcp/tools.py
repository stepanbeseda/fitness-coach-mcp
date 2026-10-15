"""Validation, date logic and result shaping for the coach memory tools.

One plain function per tool, taking the store and today's date explicitly (the
clock is injected by the caller, never read here). Shape constraints (enum values,
maximum lengths, numeric ranges) are enforced by pydantic in the server signatures
and in ``PlannedSession``; everything else raises ``ToolError``.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Annotated, Any, Literal, assert_never

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from . import plan
from .goals import parse_target_value, target_value_out
from .history_view import plan_range
from .reconcile import RECONCILE_DAYS
from .store import Store
from .values import parse_date

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
LATEST_NOTES = 10
SEARCH_LIMIT = 20
SEARCH_LIMIT_MAX = 50
SEARCH_QUERY_ERROR = (
    'invalid search query: use plain words (all must match), "exact phrase", OR, NOT and prefix*; '
    'put a word containing punctuation such as - : , . ? or \' in double quotes, e.g. "low-carb"'
)

SessionType = Literal["running", "cycling", "swimming", "strength", "mobility", "other"]


def to_json(value: Any) -> str:
    """Compact JSON for tool results."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class PlannedSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: Annotated[str, Field(max_length=10, description="YYYY-MM-DD, inside the week and not before today.")]
    type: Annotated[SessionType, Field(description="Sport or kind of session.")]
    description: Annotated[str, Field(max_length=2000, description="What to do, e.g. '10 km easy, HR zone 2'.")]
    intensity: Annotated[str | None, Field(max_length=200, description="Optional, e.g. 'easy' or 'RPE 7'.")] = None
    duration_min: Annotated[int | None, Field(ge=1, le=1440, description="Planned duration in minutes.")] = None
    distance_km: Annotated[float | None, Field(gt=0, le=1000, description="Planned distance in km.")] = None
    garmin_workout_id: Annotated[
        str | int | None, Field(description="workout_id of the linked Garmin workout, if any.")
    ] = None
    hevy_routine_id: Annotated[str | int | None, Field(description="Id of the linked Hevy routine, if any.")] = None


# ------------------------------------------------------------------ helpers


def week_of(day: dt.date) -> tuple[dt.date, dt.date, str]:
    """ISO week (Monday to Sunday) containing ``day`` and its label."""
    start = day - dt.timedelta(days=day.weekday())
    year, week, _ = day.isocalendar()
    return start, start + dt.timedelta(days=6), f"{year}-W{week:02d}"


def _week_out(day: dt.date) -> dict[str, str]:
    start, end, label = week_of(day)
    return {"start": start.isoformat(), "end": end.isoformat(), "iso_week": label}


def _compact(obj: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in obj.items() if v is not None}


def _strip(value: str | None) -> str | None:
    return None if value is None else value.strip()


def _no_nul(text: str, field: str) -> str:
    """SQLite's length() stops at NUL, so a NUL would trip a CHECK instead of giving a clear error."""
    if "\x00" in text:
        raise ToolError(f"{field} must not contain NUL characters")
    return text


def _required_text(value: str, field: str) -> str:
    text = _no_nul(value.strip(), field)
    if not text:
        raise ToolError(f"{field} must not be empty")
    return text


def _optional_text(value: str | None, field: str) -> str | None:
    """Empty after strip counts as absent (create) or as 'clear the field' (update)."""
    text = _strip(value)
    if text is not None:
        _no_nul(text, field)
    return text or None


def _optional_date(value: str | None, name: str) -> str | None:
    text = _strip(value) or None
    return None if text is None else parse_date(text, name).isoformat()


def _workout_id(value: str | int | None, name: str) -> str | None:
    if value is None:
        return None
    text = _no_nul(str(value), name)
    if not 1 <= len(text) <= 64:
        raise ToolError(f"{name} must be 1-64 characters")
    return text


# ------------------------------------------------------------ output shaping


def _target_out(row: dict[str, Any]) -> str | float | None:
    return None if row["target_value"] is None else target_value_out(row["metric"], row["target_value"])


def goal_out(row: dict[str, Any], today: dt.date) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "target": row["target"],
        "target_date": row["target_date"],
        "target_result": row["target_result"],
        "metric": row["metric"],
        "target_value": _target_out(row),
        "days_remaining": None,
    }
    if row["target_date"] is not None:
        out["days_remaining"] = (dt.date.fromisoformat(row["target_date"]) - today).days
    return _compact(out)


def constraint_out(row: dict[str, Any]) -> dict[str, Any]:
    return _compact(
        {"id": row["id"], "kind": row["kind"], "title": row["title"], "status": row["status"], "details": row["details"]}
    )


def note_out(row: dict[str, Any]) -> dict[str, Any]:
    return _compact({"id": row["id"], "date": row["date"], "text": row["text"], "session_id": row["session_id"]})


def suggestion_out(row: dict[str, Any]) -> dict[str, Any]:
    return _compact(
        {
            "result": row["result"],
            "final": bool(row["final"]),
            "evidence": json.loads(row["evidence"]) if row["evidence"] is not None else None,
            "evaluated_on": row["evaluated_on"],
        }
    )


def sessions_out(store: Store, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    moved = store.moved_to([r["id"] for r in rows if r["status"] == "moved"])
    suggested = store.suggestions_for([r["id"] for r in rows if r["status"] == "planned"])
    out = []
    for r in rows:
        link = moved.get(r["id"]) if r["status"] == "moved" else None
        out.append(
            _compact(
                {
                    "id": r["id"],
                    "date": r["date"],
                    "weekday": _WEEKDAYS[dt.date.fromisoformat(r["date"]).weekday()],
                    "type": r["type"],
                    "description": r["description"],
                    "status": r["status"],
                    "intensity": r["intensity"],
                    "duration_min": r["duration_min"],
                    "distance_km": r["distance_km"],
                    "garmin_workout_id": r["garmin_workout_id"],
                    "hevy_routine_id": r["hevy_routine_id"],
                    "moved_from_id": r["moved_from_id"],
                    "moved_to": {"id": link[0], "date": link[1]} if link else None,
                    "suggestion": (
                        suggestion_out(suggested[r["id"]])
                        if r["status"] == "planned" and r["id"] in suggested
                        else None
                    ),
                }
            )
        )
    return out


def _session_out(store: Store, row: dict[str, Any]) -> dict[str, Any]:
    return sessions_out(store, [row])[0]


# -------------------------------------------------------------------- tools


def adherence(rows: list[dict[str, Any]], today: dt.date) -> dict[str, Any]:
    """Counts per status, overdue planned sessions and the completion rate of ``rows``."""
    # Counts are per status, so superseded rows (listed only on request) never enter them.
    count = {s: sum(1 for r in rows if r["status"] == s) for s in ("planned", "done", "skipped", "moved")}
    finished = count["done"] + count["skipped"]
    return {
        **count,
        "overdue_planned": sum(1 for r in rows if r["status"] == "planned" and r["date"] < today.isoformat()),
        "completion_rate": round(count["done"] / finished, 2) if finished else None,
    }


def get_coach_context(store: Store, today: dt.date) -> dict[str, Any]:
    start, end, _ = week_of(today)
    return {
        "today": today.isoformat(),
        "week": _week_out(today),
        "goals": [goal_out(g, today) for g in store.list_goals("active")],
        "constraints": [constraint_out(c) for c in store.list_constraints("active")],
        "sessions": sessions_out(store, store.sessions_between(start, end)),
        "to_confirm": [
            s
            for s in sessions_out(
                store,
                store.sessions_between(today - dt.timedelta(days=RECONCILE_DAYS), today - dt.timedelta(days=1)),
            )
            if "suggestion" in s
        ],
        "notes": [note_out(n) for n in store.latest_notes(LATEST_NOTES)],
    }


def get_plan(
    store: Store,
    today: dt.date,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    include_superseded: bool = False,
) -> dict[str, Any]:
    start, end = plan_range(today, start_date, end_date)
    rows = store.sessions_between(start, end, include_superseded=include_superseded)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "sessions": sessions_out(store, rows),
        "adherence": adherence(rows, today),
    }


def set_goal(
    store: Store,
    today: dt.date,
    *,
    goal_id: int | None = None,
    title: str | None = None,
    target: str | None = None,
    target_date: str | None = None,
    target_result: str | None = None,
    status: str | None = None,
    metric: str | None = None,
    target_value: str | float | None = None,
) -> dict[str, Any]:
    if goal_id is None:
        if title is None:
            raise ToolError("title is required to create a goal")
        metric = metric or None  # an empty string counts as absent
        target_value = None if target_value == "" else target_value
        if target_value is not None and metric is None:
            raise ToolError("target_value needs a metric")
        parsed = None if metric is None or target_value is None else parse_target_value(metric, target_value)
        fields: dict[str, Any] = {
            "title": _required_text(title, "title"),
            "target": _optional_text(target, "target"),
            "target_date": _optional_date(target_date, "target_date"),
            "target_result": _optional_text(target_result, "target_result"),
            "status": status or "active",
            "metric": metric,
            "target_value": parsed,
        }
        new_id = store.insert_goal({k: v for k, v in fields.items() if v is not None})
        return {"goal": goal_out(_must_get(store.get_goal(new_id), "goal", new_id), today), "created": True}

    given = {
        "title": title,
        "target": target,
        "target_date": target_date,
        "target_result": target_result,
        "status": status,
        "metric": metric,
        "target_value": target_value,
    }
    if all(v is None for v in given.values()):
        raise ToolError("nothing to update")
    stored = store.get_goal(goal_id)
    if stored is None:
        raise ToolError(f"goal {goal_id} not found")
    fields = {}
    if title is not None:
        fields["title"] = _required_text(title, "title")
    if target is not None:
        fields["target"] = _optional_text(target, "target")
    if target_date is not None:
        fields["target_date"] = _optional_date(target_date, "target_date")
    if target_result is not None:
        fields["target_result"] = _optional_text(target_result, "target_result")
    if status is not None:
        fields["status"] = status
    fields.update(_metric_fields(stored, metric, target_value))
    if not store.update_goal(goal_id, fields):
        raise ToolError(f"goal {goal_id} not found")
    return {"goal": goal_out(_must_get(store.get_goal(goal_id), "goal", goal_id), today), "created": False}


def _metric_fields(stored: dict[str, Any], metric: str | None, target_value: str | float | None) -> dict[str, Any]:
    """Columns metric / target_value of an update of ``stored`` (rules of D6: no silent reinterpretation)."""
    if metric == "":
        if target_value not in (None, ""):
            raise ToolError("an empty metric clears target_value; do not give both")
        return {"metric": None, "target_value": None}
    if metric is None and target_value is None:
        return {}
    effective = metric if metric is not None else stored["metric"]
    fields: dict[str, Any] = {} if metric is None else {"metric": metric}
    if target_value == "":
        fields["target_value"] = None
    elif target_value is not None:
        if effective is None:
            raise ToolError("target_value needs a metric")
        fields["target_value"] = parse_target_value(effective, target_value)
    elif metric != stored["metric"] and stored["target_value"] is not None:
        raise ToolError("changing the metric needs a new target_value (or an empty string to clear it)")
    return fields


def set_constraint(
    store: Store,
    today: dt.date,
    *,
    constraint_id: int | None = None,
    kind: str | None = None,
    title: str | None = None,
    details: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    if constraint_id is None:
        if kind is None or title is None:
            raise ToolError("kind and title are required to create a constraint")
        fields: dict[str, Any] = {
            "kind": kind,
            "title": _required_text(title, "title"),
            "details": _optional_text(details, "details"),
            "status": status or "active",
        }
        new_id = store.insert_constraint({k: v for k, v in fields.items() if v is not None})
        row = _must_get(store.get_constraint(new_id), "constraint", new_id)
        return {"constraint": constraint_out(row), "created": True}

    if kind is None and title is None and details is None and status is None:
        raise ToolError("nothing to update")
    fields = {}
    if kind is not None:
        fields["kind"] = kind
    if title is not None:
        fields["title"] = _required_text(title, "title")
    if details is not None:
        fields["details"] = _optional_text(details, "details")
    if status is not None:
        fields["status"] = status
    if not store.update_constraint(constraint_id, fields):
        raise ToolError(f"constraint {constraint_id} not found")
    row = _must_get(store.get_constraint(constraint_id), "constraint", constraint_id)
    return {"constraint": constraint_out(row), "created": False}


def save_plan(store: Store, today: dt.date, *, week: str, sessions: list[PlannedSession]) -> dict[str, Any]:
    start, end, _ = week_of(parse_date(week, "week"))
    rows: list[dict[str, Any]] = []
    for i, s in enumerate(sessions):
        day = parse_date(s.date, f"sessions[{i}].date")
        if not start <= day <= end:
            raise ToolError(f"sessions[{i}].date {day} is outside the week {start}..{end}")
        if day < today:
            raise ToolError(f"sessions[{i}].date {day} is in the past; mark past sessions with update_session")
        rows.append(
            {
                "date": day,
                "type": s.type,
                "description": _required_text(s.description, f"sessions[{i}].description"),
                "intensity": _optional_text(s.intensity, f"sessions[{i}].intensity"),
                "duration_min": s.duration_min,
                "distance_km": s.distance_km,
                "garmin_workout_id": _workout_id(s.garmin_workout_id, f"sessions[{i}].garmin_workout_id"),
                "hevy_routine_id": _workout_id(s.hevy_routine_id, f"sessions[{i}].hevy_routine_id"),
            }
        )
    superseded, new_ids = store.replace_planned(start, end, today, rows)
    return {
        "week": _week_out(start),
        "superseded": superseded,
        "created_ids": new_ids,
        "sessions": sessions_out(store, store.sessions_between(start, end)),
    }


def update_session(
    store: Store,
    today: dt.date,
    *,
    session_id: int,
    status: str,
    new_date: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    if status != "moved" and new_date is not None:
        raise ToolError("new_date is only allowed with status 'moved'")
    if status == "moved" and new_date is None:
        raise ToolError("new_date is required for status 'moved'")
    target = parse_date(new_date, "new_date") if new_date is not None else None
    outcome = plan.mark_session(store, today, session_id, status, new_date=target, note=note)
    if isinstance(outcome, plan.Refusal):
        raise refusal_error(outcome, "note")
    result: dict[str, Any] = {"session": _session_out(store, outcome.session)}
    if outcome.copy is not None:
        result["moved_copy"] = _session_out(store, outcome.copy)
    if outcome.note is not None:
        result["note"] = note_out(outcome.note)
    return result


def add_note(
    store: Store,
    today: dt.date,
    *,
    text: str,
    date: str | None = None,
    session_id: int | None = None,
) -> dict[str, Any]:
    checked = plan.note_text(text)  # before the date, so a bad text is named first, as before
    if isinstance(checked, plan.Refusal):
        raise refusal_error(checked, "text")
    day = parse_date(date, "date") if date is not None else today
    outcome = plan.add_note(store, day, checked, session_id=session_id)
    if isinstance(outcome, plan.Refusal):
        raise refusal_error(outcome, "text")
    return {"note": note_out(outcome.note)}


def refusal_error(refusal: plan.Refusal, field: str) -> ToolError:
    """The English tool error of a Plan refusal; ``field`` names the note argument (``note`` or ``text``)."""
    sid = refusal.session_id
    match refusal.reason:
        case plan.Reason.NOT_FOUND:
            text = f"session {sid} not found"
        case plan.Reason.SUPERSEDED:
            text = f"session {sid} was superseded by a later plan and cannot be updated"
        case plan.Reason.MOVED:
            text = f"session {sid} was already moved; update that one"
        case plan.Reason.MOVED_TO:
            text = (
                f"session {sid} was already moved to session {refusal.copy_id} on {refusal.copy_day}; "
                "update that one"
            )
        case plan.Reason.COPY_SUPERSEDED:
            text = (
                f"session {sid} was moved to session {refusal.copy_id} on {refusal.copy_day}, "
                "which a later plan superseded; nothing left to update"
            )
        case plan.Reason.FUTURE:
            text = f"session {sid} is planned for {refusal.day}, after today, and cannot be marked done yet"
        case plan.Reason.NOT_PLANNED:
            text = "only a planned session can be moved"
        case plan.Reason.SAME_DATE:
            text = "new_date must differ from the session date"
        case plan.Reason.PAST_DATE:
            text = "new_date must be today or later"
        case plan.Reason.TEXT_EMPTY:
            text = f"{field} must not be empty"
        case plan.Reason.TEXT_NUL:
            text = f"{field} must not contain NUL characters"
        case plan.Reason.TEXT_TOO_LONG:
            text = f"{field} must be at most {plan.NOTE_MAX} characters"
        case _:  # pragma: no cover - mypy proves every reason is handled
            assert_never(refusal.reason)
    return ToolError(text)


def search_notes(
    store: Store,
    today: dt.date,
    *,
    query: str,
    start_date: str | None = None,
    end_date: str | None = None,
    session_id: int | None = None,
    limit: int = SEARCH_LIMIT,
) -> dict[str, Any]:
    text = _required_text(query, "query")
    start = parse_date(start_date, "start_date") if start_date is not None else None
    end = parse_date(end_date, "end_date") if end_date is not None else None
    if start is not None and end is not None and start > end:
        raise ToolError("start_date must be on or before end_date")
    if session_id is not None and store.get_session(session_id) is None:
        raise ToolError(f"session {session_id} not found")
    try:
        rows = store.search_notes(text, start, end, session_id, limit + 1)
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode != sqlite3.SQLITE_ERROR:
            raise
        raise ToolError(SEARCH_QUERY_ERROR) from None
    return {"notes": [note_out(r) for r in rows[:limit]], "more": len(rows) > limit}


def list_goals(store: Store, today: dt.date) -> dict[str, Any]:
    return {
        "goals": [
            {**goal_out(g, today), "created_at": g["created_at"], "updated_at": g["updated_at"]}
            for g in store.list_goals()
        ]
    }


def list_constraints(store: Store, today: dt.date) -> dict[str, Any]:
    return {
        "constraints": [
            {**constraint_out(c), "created_at": c["created_at"], "updated_at": c["updated_at"]}
            for c in store.list_constraints()
        ]
    }


def _must_get(row: dict[str, Any] | None, kind: str, row_id: int) -> dict[str, Any]:
    if row is None:  # pragma: no cover - a row we just wrote must exist
        raise ToolError(f"{kind} {row_id} not found")
    return row
