"""Canned hevy-mcp payloads for the strength tests.

The shapes follow hevy-mcp's projections (sparse: a key whose value is unknown is left out, hevy-mcp drops
nulls and its output schemas reject ``null``), so every builder omits a key whose argument is None.
"""

from __future__ import annotations

from typing import Any


def hevy_set(weight_kg: float | None = None, reps: int | float | None = None, type: str = "normal", **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"type": type, "weight_kg": weight_kg, "reps": reps, **extra}
    return {k: v for k, v in out.items() if v is not None}


def hevy_exercise(template_id: str, title: str | None, sets: list[dict[str, Any]], index: int = 0) -> dict[str, Any]:
    out: dict[str, Any] = {
        "index": index,
        "title": title,
        "exercise_template_id": template_id,
        "sets": [{"index": n, **s} for n, s in enumerate(sets)],
    }
    return {k: v for k, v in out.items() if v is not None}


def hevy_workout(
    id: str,
    start_time: str,
    exercises: list[dict[str, Any]],
    updated_at: str | None = None,
    end_time: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": id,
        "title": "Workout",
        "start_time": start_time,
        "end_time": end_time,
        "updated_at": updated_at,
        "duration": "1h 0m 0s",
        "exercises": exercises,
    }
    return {k: v for k, v in out.items() if v is not None}


def updated(workout: dict[str, Any]) -> dict[str, Any]:
    return {"type": "updated", "workout": workout}


def deleted(id: str, deleted_at: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "deleted", "id": id, "deleted_at": deleted_at}
    return {k: v for k, v in out.items() if v is not None}


def events_page(events: list[dict[str, Any]], page: int = 1, page_count: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"events": events, "page": page, "page_count": page_count}
    return {k: v for k, v in out.items() if v is not None}


def hevy_template(id: str, title: str | None, group: str | None, type: str = "weight_reps") -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": id,
        "title": title,
        "type": type,
        "primary_muscle_group": group,
        "secondary_muscle_groups": [],
        "is_custom": False,
    }
    return {k: v for k, v in out.items() if v is not None}


def day_workout(id: str, day: str, template_id: str = "T1", sets: int = 3, weight_kg: float = 100, reps: int = 5,
                updated_at: str = "2026-10-01T10:00:00Z", title: str = "Bench Press (Barbell)") -> dict[str, Any]:
    """A workout starting at 10:00 UTC of ``day`` with ``sets`` identical normal sets of one exercise."""
    return hevy_workout(
        id,
        f"{day}T10:00:00Z",
        [hevy_exercise(template_id, title, [hevy_set(weight_kg, reps) for _ in range(sets)])],
        updated_at=updated_at,
        end_time=f"{day}T11:00:00Z",
    )
