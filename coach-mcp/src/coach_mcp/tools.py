"""Validation, date logic and result shaping for the coach memory tools.

One plain function per tool, taking the store and today's date explicitly (the
clock is injected by the caller, never read here). Shape constraints (enum values,
maximum lengths, numeric ranges) are enforced by pydantic in the server signatures
and in ``PlannedSession``; everything else raises ``ToolError``.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Annotated, Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from .store import Store

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MAX_RANGE_DAYS = 92
LATEST_NOTES = 10

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


def parse_date(value: str, name: str) -> dt.date:
    text = value.strip() if isinstance(value, str) else value
    if not isinstance(text, str) or not _DATE_RE.fullmatch(text):
        raise ToolError(f"{name} must be YYYY-MM-DD, got {value!r}")
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise ToolError(f"{name} is not a valid date: {value!r}") from None


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


def goal_out(row: dict[str, Any], today: dt.date) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "target": row["target"],
        "target_date": row["target_date"],
        "target_result": row["target_result"],
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


def sessions_out(store: Store, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    moved = store.moved_to([r["id"] for r in rows if r["status"] == "moved"])
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
                }
            )
        )
    return out


def _session_out(store: Store, row: dict[str, Any]) -> dict[str, Any]:
    return sessions_out(store, [row])[0]


# -------------------------------------------------------------------- tools


def get_coach_context(store: Store, today: dt.date) -> dict[str, Any]:
    start, end, _ = week_of(today)
    return {
        "today": today.isoformat(),
        "week": _week_out(today),
        "goals": [goal_out(g, today) for g in store.list_goals("active")],
        "constraints": [constraint_out(c) for c in store.list_constraints("active")],
        "sessions": sessions_out(store, store.sessions_between(start, end)),
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
    start = parse_date(start_date, "start_date") if start_date is not None else week_of(today)[0]
    end = parse_date(end_date, "end_date") if end_date is not None else start + dt.timedelta(days=6)
    if start > end:
        raise ToolError("start_date must be on or before end_date")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ToolError(f"date range too long (max {MAX_RANGE_DAYS} days)")

    rows = store.sessions_between(start, end, include_superseded=include_superseded)
    # Counts are per status, so superseded rows (listed only on request) never enter them.
    count = {s: sum(1 for r in rows if r["status"] == s) for s in ("planned", "done", "skipped", "moved")}
    finished = count["done"] + count["skipped"]
    adherence = {
        **count,
        "overdue_planned": sum(1 for r in rows if r["status"] == "planned" and r["date"] < today.isoformat()),
        "completion_rate": round(count["done"] / finished, 2) if finished else None,
    }
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "sessions": sessions_out(store, rows),
        "adherence": adherence,
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
) -> dict[str, Any]:
    if goal_id is None:
        if title is None:
            raise ToolError("title is required to create a goal")
        fields: dict[str, Any] = {
            "title": _required_text(title, "title"),
            "target": _optional_text(target, "target"),
            "target_date": _optional_date(target_date, "target_date"),
            "target_result": _optional_text(target_result, "target_result"),
            "status": status or "active",
        }
        new_id = store.insert_goal({k: v for k, v in fields.items() if v is not None})
        return {"goal": goal_out(_must_get(store.get_goal(new_id), "goal", new_id), today), "created": True}

    given = {"title": title, "target": target, "target_date": target_date, "target_result": target_result, "status": status}
    if all(v is None for v in given.values()):
        raise ToolError("nothing to update")
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
    if not store.update_goal(goal_id, fields):
        raise ToolError(f"goal {goal_id} not found")
    return {"goal": goal_out(_must_get(store.get_goal(goal_id), "goal", goal_id), today), "created": False}


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
    note_text = _required_text(note, "note") if note is not None else None

    row = store.get_session(session_id)
    if row is None:
        raise ToolError(f"session {session_id} not found")
    if row["status"] == "superseded":
        raise ToolError(f"session {session_id} was superseded by a later plan and cannot be updated")
    if row["status"] == "moved":
        link = store.moved_to([session_id]).get(session_id)
        if link is None:
            raise ToolError(f"session {session_id} was already moved; update that one")
        copy_id, copy_date = link
        copy = store.get_session(copy_id)
        if copy is not None and copy["status"] == "superseded":
            raise ToolError(
                f"session {session_id} was moved to session {copy_id} on {copy_date}, "
                "which a later plan superseded; nothing left to update"
            )
        raise ToolError(f"session {session_id} was already moved to session {copy_id} on {copy_date}; update that one")
    if status == "moved":
        assert target is not None
        if row["status"] != "planned":
            raise ToolError("only a planned session can be moved")
        if target.isoformat() == row["date"]:
            raise ToolError("new_date must differ from the session date")
        if target < today:
            raise ToolError("new_date must be today or later")

    copy_id, note_id = store.update_session_status(
        session_id, status, target, (today, note_text) if note_text is not None else None
    )
    result: dict[str, Any] = {"session": _session_out(store, _must_get(store.get_session(session_id), "session", session_id))}
    if copy_id is not None:
        result["moved_copy"] = _session_out(store, _must_get(store.get_session(copy_id), "session", copy_id))
    if note_id is not None:
        result["note"] = note_out(_must_get(store.get_note(note_id), "note", note_id))
    return result


def add_note(
    store: Store,
    today: dt.date,
    *,
    text: str,
    date: str | None = None,
    session_id: int | None = None,
) -> dict[str, Any]:
    body = _required_text(text, "text")
    day = parse_date(date, "date") if date is not None else today
    if session_id is not None and store.get_session(session_id) is None:
        raise ToolError(f"session {session_id} not found")
    note_id = store.insert_note(day, body, session_id)
    return {"note": note_out(_must_get(store.get_note(note_id), "note", note_id))}


def _must_get(row: dict[str, Any] | None, kind: str, row_id: int) -> dict[str, Any]:
    if row is None:  # pragma: no cover - a row we just wrote must exist
        raise ToolError(f"{kind} {row_id} not found")
    return row
