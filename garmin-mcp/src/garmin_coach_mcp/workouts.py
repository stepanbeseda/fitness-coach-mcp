"""Structured workouts: input schema, Garmin JSON builder, summaries, tool logic.

The input schema is deliberately small and strict so the model cannot produce
Garmin payloads that upload fine but silently lose their targets. Garmin field
conventions follow python-garminconnect's typed workout models
(``garminconnect.workout``): custom heart-rate / power / cadence ranges use the
zone target type with ``targetValueOne/Two`` (no ``zoneNumber``), pace ranges are
``pace.zone`` in m/s with the slower pace as the lower value. A strength workout is a list of exercise items
(sets, reps or seconds, weight, rest); each becomes one repeat group of an exercise step and a rest step, in the
shape python-garminconnect's ``StrengthWorkout`` model describes and a real upload to Garmin accepted.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from garminconnect.workout import (
    WEIGHT_UNIT_KILOGRAM,
    CyclingWorkout,
    ExecutableStep,
    RepeatGroup,
    RunningWorkout,
    StrengthWorkout,
    WorkoutSegment,
)
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import exercises
from .formatting import (
    as_dict,
    as_list,
    clean,
    clock,
    km,
    minutes,
    num,
    pace_per_km,
    parse_pace,
    resolve_date,
    today,
)
from .garmin_client import GarminService

MAX_TOTAL_STEPS = 50
WORKOUT_URL = "https://connect.garmin.com/modern/workout/{workout_id}"
GRAMS_PER_KG = 1000.0  # Garmin stores a step weight in grams when the unit tag is kilogram (factor 1000.0)
REPS_SET_SECONDS = 30  # assumed length of a set that ends by repetitions, for the duration estimate
NO_TARGET = "a strength exercise takes no pace, power, heart-rate or cadence target (give reps or seconds, and weight_kg)"
ONLY_EXERCISES = (
    'a strength workout takes only exercise items ({"type":"exercise", ...}); warmup, interval, recovery, rest, '
    "cooldown and repeat steps with their pace, power or heart-rate targets are for running and cycling"
)
NO_EXERCISES = "exercise items are only supported for strength workouts"

# ------------------------------------------------------------------ input schema


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeEnd(_Strict):
    type: Literal["time"]
    seconds: int = Field(ge=10, le=6 * 3600, description="Step length in seconds (10 s - 6 h).")


class DistanceEnd(_Strict):
    type: Literal["distance"]
    meters: int = Field(ge=50, le=300_000, description="Step length in meters (50 m - 300 km).")


class LapButtonEnd(_Strict):
    type: Literal["lap_button"]


EndCondition = Annotated[TimeEnd | DistanceEnd | LapButtonEnd, Field(discriminator="type")]


class NoTarget(_Strict):
    type: Literal["none"] = "none"


class HeartRateZoneTarget(_Strict):
    type: Literal["heart_rate_zone"]
    zone: int = Field(ge=1, le=5, description="Garmin heart-rate zone 1-5 (user's own zones).")


class HeartRateTarget(_Strict):
    type: Literal["heart_rate"]
    min_bpm: int = Field(ge=60, le=220)
    max_bpm: int = Field(ge=60, le=230)

    @model_validator(mode="after")
    def _order(self) -> HeartRateTarget:
        if self.min_bpm >= self.max_bpm:
            raise ValueError("min_bpm must be lower than max_bpm")
        return self


class PaceTarget(_Strict):
    type: Literal["pace"]
    slowest: str = Field(description="Slowest acceptable pace per km as 'M:SS', e.g. '5:30'.")
    fastest: str = Field(description="Fastest acceptable pace per km as 'M:SS', e.g. '5:10'.")

    @model_validator(mode="after")
    def _order(self) -> PaceTarget:
        slow, fast = parse_pace(self.slowest), parse_pace(self.fastest)  # m/s
        if slow >= fast:
            raise ValueError("'slowest' must be a slower pace (more min/km) than 'fastest'")
        return self


class PowerTarget(_Strict):
    type: Literal["power"]
    min_watts: int = Field(ge=30, le=2000)
    max_watts: int = Field(ge=30, le=2500)

    @model_validator(mode="after")
    def _order(self) -> PowerTarget:
        if self.min_watts >= self.max_watts:
            raise ValueError("min_watts must be lower than max_watts")
        return self


class CadenceTarget(_Strict):
    type: Literal["cadence"]
    min: int = Field(ge=20, le=250, description="Steps/min when running, rpm when cycling.")
    max: int = Field(ge=20, le=250)

    @model_validator(mode="after")
    def _order(self) -> CadenceTarget:
        if self.min >= self.max:
            raise ValueError("cadence min must be lower than max")
        return self


Target = Annotated[
    NoTarget | HeartRateZoneTarget | HeartRateTarget | PaceTarget | PowerTarget | CadenceTarget,
    Field(discriminator="type"),
]


class Step(_Strict):
    type: Literal["warmup", "interval", "recovery", "rest", "cooldown"]
    end: EndCondition = Field(description="When the step ends: time, distance or lap button.")
    target: Target = Field(default_factory=NoTarget, description="Intensity target (default none).")
    note: str | None = Field(default=None, max_length=100, description="Short cue shown with the step.")


class Repeat(_Strict):
    type: Literal["repeat"]
    iterations: int = Field(ge=2, le=50)
    steps: list[Step] = Field(min_length=1, max_length=10, description="Steps repeated as a block.")


class Exercise(_Strict):
    type: Literal["exercise"]
    category: str | None = Field(
        default=None,
        max_length=60,
        description="Garmin exercise category from find_strength_exercise, e.g. 'BENCH_PRESS'. "
        "Omit category and name when Garmin has no match.",
    )
    name: str | None = Field(
        default=None,
        max_length=80,
        description="Garmin exercise name from find_strength_exercise, e.g. 'BARBELL_BENCH_PRESS'.",
    )
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=60,
        description="The athlete's own title of the exercise (the Hevy title). Required without category and "
        "name: it is then the note of the step.",
    )
    sets: int = Field(ge=1, le=20, description="Number of sets (1-20).")
    reps: int | None = Field(default=None, ge=1, le=200, description="Repetitions per set (1-200). Give reps or seconds.")
    seconds: int | None = Field(
        default=None, ge=5, le=3600, description="Seconds per set for a hold or a carry (5-3600), instead of reps."
    )
    weight_kg: float | None = Field(
        default=None, ge=0.1, le=500, description="Target weight in kg; omit for body weight."
    )
    rest_seconds: int = Field(ge=0, le=900, description="Rest after each set in seconds (0-900; 0 = no rest step).")
    note: str | None = Field(default=None, max_length=100, description="Short cue shown with the exercise.")

    @model_validator(mode="before")
    @classmethod
    def _no_target(cls, data: Any) -> Any:
        if isinstance(data, dict) and "target" in data:
            raise ValueError(NO_TARGET)
        return data

    @model_validator(mode="after")
    def _check(self) -> Exercise:
        # Four independent checks; the order decides which message wins when several apply.
        if (self.reps is None) == (self.seconds is None):
            raise ValueError("give exactly one of reps and seconds")
        if (self.category is None) != (self.name is None):
            raise ValueError(
                "category and name go together (both from find_strength_exercise); "
                "omit both for an exercise Garmin does not know"
            )
        if self.category is None and self.title is None:
            raise ValueError("an exercise without category and name needs a title")
        if self.category is not None and exercises.display_name(self.category, self.name) is None:
            raise ValueError(
                f"unknown Garmin exercise {self.category}/{self.name}; "
                "take category and name from find_strength_exercise"
            )
        return self


WorkoutItem = Annotated[Step | Repeat | Exercise, Field(discriminator="type")]


def _garmin_steps(item: Step | Repeat | Exercise) -> int:
    """How many Garmin steps an input item becomes."""
    if isinstance(item, Repeat):
        return 1 + len(item.steps)
    if isinstance(item, Exercise):
        return (1 if item.sets > 1 else 0) + 1 + (1 if item.rest_seconds else 0)
    return 1


class WorkoutSpec(_Strict):
    name: str = Field(min_length=1, max_length=80)
    sport: Literal["running", "cycling", "strength"]
    steps: list[WorkoutItem] = Field(min_length=1, max_length=30)
    description: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _check(self) -> WorkoutSpec:
        exercise_count = sum(isinstance(item, Exercise) for item in self.steps)
        if self.sport == "strength":
            if exercise_count != len(self.steps):
                raise ValueError(ONLY_EXERCISES)
        elif exercise_count:
            raise ValueError(NO_EXERCISES)
        total = sum(_garmin_steps(item) for item in self.steps)
        if total > MAX_TOTAL_STEPS:
            raise ValueError(f"too many steps ({total}); Garmin workouts are limited to {MAX_TOTAL_STEPS}")
        for step in _flat_steps([item for item in self.steps if not isinstance(item, Exercise)]):
            if isinstance(step.target, PaceTarget) and self.sport != "running":
                raise ValueError("pace targets are only supported for running workouts")
            if isinstance(step.target, PowerTarget) and self.sport != "cycling":
                raise ValueError("power targets are only supported for cycling workouts")
        return self


def _flat_steps(items: list[Step | Repeat]) -> list[Step]:
    flat: list[Step] = []
    for item in items:
        flat.extend(item.steps if isinstance(item, Repeat) else [item])
    return flat


# ------------------------------------------------------------- Garmin builder

_STEP_TYPES = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2},
    "interval": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
    "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4},
    "rest": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
    "repeat": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
}
_END_LAP = {"conditionTypeId": 1, "conditionTypeKey": "lap.button", "displayOrder": 1, "displayable": True}
_END_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True}
_END_DISTANCE = {"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True}
_END_REPS = {"conditionTypeId": 10, "conditionTypeKey": "reps", "displayOrder": 10, "displayable": True}
_END_ITERATIONS = {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False}
_TARGETS = {
    "none": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1},
    "power": {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone", "displayOrder": 2},
    "cadence": {"workoutTargetTypeId": 3, "workoutTargetTypeKey": "cadence", "displayOrder": 3},
    "heart_rate": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4},
    "pace": {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6},
}
_SPORTS = {
    "running": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "cycling": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
    "strength": {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5},
}
_DEFAULT_SPEED_MPS = {"running": 1000 / 360, "cycling": 25 / 3.6}  # 6:00/km, 25 km/h


def _end_fields(end: TimeEnd | DistanceEnd | LapButtonEnd) -> dict[str, Any]:
    if isinstance(end, TimeEnd):
        return {"endCondition": dict(_END_TIME), "endConditionValue": float(end.seconds)}
    if isinstance(end, DistanceEnd):
        return {"endCondition": dict(_END_DISTANCE), "endConditionValue": float(end.meters)}
    return {"endCondition": dict(_END_LAP), "endConditionValue": None}


def _target_fields(target: Any) -> dict[str, Any]:
    if isinstance(target, HeartRateZoneTarget):
        return {"targetType": dict(_TARGETS["heart_rate"]), "zoneNumber": target.zone}
    if isinstance(target, HeartRateTarget):
        return {"targetType": dict(_TARGETS["heart_rate"]),
                "targetValueOne": float(target.min_bpm), "targetValueTwo": float(target.max_bpm)}
    if isinstance(target, PaceTarget):
        return {"targetType": dict(_TARGETS["pace"]),
                "targetValueOne": round(parse_pace(target.slowest), 6),
                "targetValueTwo": round(parse_pace(target.fastest), 6)}
    if isinstance(target, PowerTarget):
        return {"targetType": dict(_TARGETS["power"]),
                "targetValueOne": float(target.min_watts), "targetValueTwo": float(target.max_watts)}
    if isinstance(target, CadenceTarget):
        return {"targetType": dict(_TARGETS["cadence"]),
                "targetValueOne": float(target.min), "targetValueTwo": float(target.max)}
    return {"targetType": dict(_TARGETS["none"])}


def _step_seconds(step: Step, sport: str) -> float:
    if isinstance(step.end, TimeEnd):
        return float(step.end.seconds)
    if isinstance(step.end, DistanceEnd):
        speed = _DEFAULT_SPEED_MPS[sport]
        if isinstance(step.target, PaceTarget):
            speed = (parse_pace(step.target.slowest) + parse_pace(step.target.fastest)) / 2
        return step.end.meters / speed
    return 0.0


def _exercise_seconds(item: Exercise) -> float:
    return float(item.sets * ((item.seconds if item.seconds is not None else REPS_SET_SECONDS) + item.rest_seconds))


def estimate_duration_seconds(spec: WorkoutSpec) -> int:
    total = 0.0
    for item in spec.steps:
        if isinstance(item, Exercise):
            total += _exercise_seconds(item)
        elif isinstance(item, Repeat):
            total += item.iterations * sum(_step_seconds(s, spec.sport) for s in item.steps)
        else:
            total += _step_seconds(item, spec.sport)
    return int(round(total))


def _exercise_steps(item: Exercise, order: Iterator[int]) -> list[ExecutableStep | RepeatGroup]:
    """One exercise: a repeat group of the exercise step and a rest step, or the plain steps for a single set."""
    group_order = next(order) if item.sets > 1 else None
    fields: dict[str, Any] = {
        "stepOrder": next(order),
        "stepType": dict(_STEP_TYPES["interval"]),
        "targetType": dict(_TARGETS["none"]),
    }
    if item.seconds is not None:
        fields |= {"endCondition": dict(_END_TIME), "endConditionValue": float(item.seconds)}
    else:
        fields |= {"endCondition": dict(_END_REPS), "endConditionValue": float(item.reps or 0)}
    if item.category is not None:
        fields |= {"category": item.category, "exerciseName": item.name}
        description = item.note
    else:  # Garmin does not know the exercise: the step carries the athlete's title as its note
        description = f"{item.title} - {item.note}" if item.note else item.title
    if item.weight_kg is not None:
        fields |= {
            "weightValue": float(round(item.weight_kg * GRAMS_PER_KG)),
            "weightUnit": dict(WEIGHT_UNIT_KILOGRAM),
        }
    if description:
        fields["description"] = description
    steps: list[ExecutableStep | RepeatGroup] = [ExecutableStep(**fields)]
    if item.rest_seconds:
        steps.append(
            ExecutableStep(
                stepOrder=next(order),
                stepType=dict(_STEP_TYPES["rest"]),
                endCondition=dict(_END_TIME),
                endConditionValue=float(item.rest_seconds),
                targetType=dict(_TARGETS["none"]),
            )
        )
    if group_order is None:
        return steps
    return [
        RepeatGroup(
            stepOrder=group_order,
            stepType=dict(_STEP_TYPES["repeat"]),
            numberOfIterations=item.sets,
            workoutSteps=steps,
            endCondition=dict(_END_ITERATIONS),
            endConditionValue=float(item.sets),
            smartRepeat=False,
        )
    ]


def build_garmin_workout(spec: WorkoutSpec) -> dict[str, Any]:
    order = itertools.count(1)

    def executable(step: Step) -> ExecutableStep:
        fields: dict[str, Any] = {
            "stepOrder": next(order),
            "stepType": dict(_STEP_TYPES[step.type]),
            **_end_fields(step.end),
            **_target_fields(step.target),
        }
        if step.note:
            fields["description"] = step.note
        return ExecutableStep(**fields)

    garmin_steps: list[ExecutableStep | RepeatGroup] = []
    for item in spec.steps:
        if isinstance(item, Exercise):
            garmin_steps.extend(_exercise_steps(item, order))
        elif isinstance(item, Repeat):
            group_order = next(order)
            children = [executable(s) for s in item.steps]
            garmin_steps.append(
                RepeatGroup(
                    stepOrder=group_order,
                    stepType=dict(_STEP_TYPES["repeat"]),
                    numberOfIterations=item.iterations,
                    workoutSteps=children,
                    endCondition=dict(_END_ITERATIONS),
                    endConditionValue=float(item.iterations),
                    smartRepeat=False,
                )
            )
        else:
            garmin_steps.append(executable(item))

    model = {"running": RunningWorkout, "cycling": CyclingWorkout, "strength": StrengthWorkout}[spec.sport]
    workout = model(
        workoutName=spec.name,
        estimatedDurationInSecs=estimate_duration_seconds(spec),
        workoutSegments=[
            WorkoutSegment(segmentOrder=1, sportType=dict(_SPORTS[spec.sport]), workoutSteps=garmin_steps)
        ],
        description=spec.description,
    )
    payload: dict[str, Any] = workout.to_dict()
    return payload


# ------------------------------------------------------------------ summaries


def describe_end(step: dict[str, Any]) -> str | None:
    key = as_dict(step.get("endCondition")).get("conditionTypeKey")
    value = step.get("endConditionValue")
    if key == "time":
        return f"time {clock(value)}"
    if key == "distance":
        meters = num(value, 0) or 0
        return f"distance {km(meters)} km" if meters >= 1000 else f"distance {meters} m"
    if key == "lap.button":
        return "lap button"
    return key


def describe_target(step: dict[str, Any]) -> str | None:
    key: str | None = as_dict(step.get("targetType")).get("workoutTargetTypeKey")
    one, two, zone = step.get("targetValueOne"), step.get("targetValueTwo"), step.get("zoneNumber")
    if key in (None, "no.target"):
        return None
    if key == "heart.rate.zone":
        return f"HR zone {zone}" if zone else f"HR {num(one, 0)}-{num(two, 0)} bpm"
    if key == "power.zone":
        return f"power zone {zone}" if zone else f"power {num(one, 0)}-{num(two, 0)} W"
    if key == "pace.zone":
        return f"pace {pace_per_km(one)}-{pace_per_km(two)} /km" if one and two else "pace zone"
    if key == "speed.zone":
        return f"speed {num((one or 0) * 3.6, 1)}-{num((two or 0) * 3.6, 1)} km/h"
    if key == "cadence":
        return f"cadence {num(one, 0)}-{num(two, 0)}"
    return key


def summarize_steps(steps: Any) -> list[dict[str, Any]]:
    out = []
    for step in sorted((s for s in as_list(steps) if isinstance(s, dict)), key=lambda s: s.get("stepOrder") or 0):
        step_type = as_dict(step.get("stepType")).get("stepTypeKey")
        if step.get("type") == "RepeatGroupDTO" or step_type == "repeat":
            out.append({"repeat": step.get("numberOfIterations"), "steps": summarize_steps(step.get("workoutSteps"))})
        else:
            out.append(
                clean(
                    {
                        "type": step_type,
                        "end": describe_end(step),
                        "target": describe_target(step),
                        "note": step.get("description"),
                    }
                )
            )
    return out


def _kg(grams: Any) -> float | int | None:
    g = num(grams, 3)
    if g is None or g <= 0:
        return None
    return num(g / GRAMS_PER_KG, 2)


def _step_key(step: dict[str, Any]) -> Any:
    return as_dict(step.get("stepType")).get("stepTypeKey")


def _end_key(step: dict[str, Any]) -> Any:
    return as_dict(step.get("endCondition")).get("conditionTypeKey")


def _is_repeat(step: dict[str, Any]) -> bool:
    return step.get("type") == "RepeatGroupDTO" or _step_key(step) == "repeat"


def _ordered(steps: Any) -> list[dict[str, Any]]:
    return sorted((s for s in as_list(steps) if isinstance(s, dict)), key=lambda s: s.get("stepOrder") or 0)


def _exercise_summary(step: dict[str, Any], sets: Any, rest: dict[str, Any] | None) -> dict[str, Any]:
    end, value = _end_key(step), step.get("endConditionValue")
    category, name = step.get("category"), step.get("exerciseName")
    return clean(
        {
            "exercise": exercises.display_name(category, name),
            "category": category,
            "name": name,
            "sets": num(sets, 0),
            "reps": num(value, 0) if end == "reps" else None,
            "seconds": num(value, 0) if end == "time" else None,
            "end": describe_end(step) if end not in ("reps", "time") else None,
            "weight_kg": _kg(step.get("weightValue")),
            "rest_seconds": num(rest.get("endConditionValue"), 0) if rest is not None else None,
            "note": step.get("description"),
        }
    )


def _set_block(children: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """(exercise step, rest step or None) when a repeat group is one exercise and an optional timed rest."""
    if not children or len(children) > 2 or _is_repeat(children[0]) or _step_key(children[0]) == "rest":
        return None
    if len(children) == 1:
        return children[0], None
    rest = children[1]
    if _step_key(rest) != "rest" or _end_key(rest) != "time":
        return None
    return children[0], rest


def summarize_strength_steps(steps: Any, *, nested: bool = False) -> list[dict[str, Any]]:
    """Exercise-oriented summary of the steps of a strength workout.

    A repeat group of one exercise and an optional timed rest is one entry with ``sets``; any other group stays
    a ``repeat`` with its steps (no ``sets`` inside), so a workout built in the Garmin app degrades to what
    ``summarize_steps`` would show instead of failing.
    """
    out: list[dict[str, Any]] = []
    for step in _ordered(steps):
        if _is_repeat(step):
            children = _ordered(step.get("workoutSteps"))
            block = _set_block(children)
            if block is None:
                out.append({"repeat": step.get("numberOfIterations"), "steps": summarize_strength_steps(children, nested=True)})
            else:
                out.append(_exercise_summary(block[0], step.get("numberOfIterations"), block[1]))
        elif _step_key(step) == "rest":
            out.append(clean({"type": "rest", "end": describe_end(step), "note": step.get("description")}))
        else:
            out.append(_exercise_summary(step, None if nested else 1, None))
    return out


def summarize_workout(w: dict[str, Any], *, with_steps: bool) -> dict[str, Any]:
    workout_id = w.get("workoutId")
    sport = as_dict(w.get("sportType")).get("sportTypeKey")
    out: dict[str, Any] = {
        "workout_id": workout_id,
        "name": w.get("workoutName"),
        "sport": sport,
        "description": w.get("description"),
        "estimated_duration_min": minutes(w.get("estimatedDurationInSecs"), 0),
        "estimated_distance_km": km(w.get("estimatedDistanceInMeters")) or None,
        # the library list spells the change date updateDate, the detail updatedDate
        "updated": (w.get("updatedDate") or w.get("updateDate") or w.get("createdDate") or "")[:10] or None,
    }
    if with_steps:
        summarize = summarize_strength_steps if sport == "strength_training" else summarize_steps
        segments = [s for s in as_list(w.get("workoutSegments")) if isinstance(s, dict)]
        steps: list[dict[str, Any]] = []
        for segment in segments:
            steps.extend(summarize(segment.get("workoutSteps")))
        out["steps"] = steps
        out["url"] = WORKOUT_URL.format(workout_id=workout_id) if workout_id else None
    return clean(out)


def _schedule_id(payload: Any) -> Any:
    p = as_dict(payload)
    return p.get("workoutScheduleId") or p.get("scheduledWorkoutId") or p.get("id")


def shape_scheduled(entry: dict[str, Any]) -> dict[str, Any]:
    """GraphQL workoutScheduleSummariesScalar entry (or calendar item fallback)."""
    activity_id = entry.get("associatedActivityId")
    return clean(
        {
            "schedule_id": entry.get("scheduledWorkoutId") or entry.get("id"),
            "date": entry.get("scheduleDate") or entry.get("date"),
            "workout_id": entry.get("workoutId"),
            "name": entry.get("workoutName") or entry.get("title"),
            "sport": entry.get("workoutType") or entry.get("sportTypeKey"),
            "completed": activity_id is not None if "associatedActivityId" in entry else None,
            "activity_id": activity_id,
            "training_plan": entry.get("tpPlanName"),
            "estimated_duration_min": minutes(entry.get("estimatedDurationInSecs"), 0),
        }
    )


# ---------------------------------------------------------------------- tools


def _graphql_schedules(client: Any, start: str, end: str) -> list[dict[str, Any]]:
    query = {"query": f'query{{workoutScheduleSummariesScalar(startDate:"{start}", endDate:"{end}")}}'}
    result = client.query_garmin_graphql(query)
    data = as_dict(as_dict(result).get("data"))
    entries = data.get("workoutScheduleSummariesScalar")
    if not isinstance(entries, list):
        raise ValueError("unexpected GraphQL response")
    return [e for e in entries if isinstance(e, dict)]


def _calendar_schedules(client: Any, start: date, end: date) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        data = as_dict(client.get_scheduled_workouts(year, month))
        for item in as_list(data.get("calendarItems")):
            if isinstance(item, dict) and item.get("itemType") == "workout":
                if start.isoformat() <= str(item.get("date") or "") <= end.isoformat():
                    items.append(item)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return items


def _fetch_schedules(client: Any, start: date, end: date) -> list[dict[str, Any]]:
    try:
        return _graphql_schedules(client, start.isoformat(), end.isoformat())
    except Exception:  # GraphQL shape/availability is not guaranteed; the calendar feed is
        return _calendar_schedules(client, start, end)


def _check_schedule_date(value: str, tz: str) -> date:
    d = resolve_date(value, tz, "date")
    now = today(tz)
    if d < now - timedelta(days=1) or d > now + timedelta(days=366):
        raise ToolError("date must be between yesterday and one year ahead")
    return d


async def list_workouts(service: GarminService, limit: int) -> dict[str, Any]:
    if not 1 <= limit <= 100:
        raise ToolError("limit must be between 1 and 100")
    raw = await service.call(lambda c: c.get_workouts(0, limit))
    workouts = [summarize_workout(w, with_steps=False) for w in as_list(raw) if isinstance(w, dict)]
    return {"count": len(workouts), "workouts": workouts}


async def get_workout(service: GarminService, workout_id: int) -> dict[str, Any]:
    if workout_id <= 0:
        raise ToolError("workout_id must be a positive integer")
    raw = await service.call(lambda c: c.get_workout_by_id(workout_id))
    return summarize_workout(as_dict(raw), with_steps=True)


async def create_workout(
    service: GarminService,
    name: str,
    sport: Literal["running", "cycling", "strength"],
    steps: list[Any],
    description: str | None,
    schedule_date: str | None,
) -> dict[str, Any]:
    try:
        spec = WorkoutSpec(name=name, sport=sport, steps=steps, description=description)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'workout'}: {e['msg']}" for e in exc.errors())
        raise ToolError(f"invalid workout: {problems}") from exc
    scheduled_for = _check_schedule_date(schedule_date, service.settings.timezone) if schedule_date else None
    payload = build_garmin_workout(spec)

    summarize = summarize_strength_steps if spec.sport == "strength" else summarize_steps
    created = as_dict(await service.call(lambda c: c.upload_workout(payload)))
    workout_id = created.get("workoutId")
    if not workout_id:
        raise ToolError("Garmin accepted the upload but returned no workoutId; check Garmin Connect before retrying.")
    result: dict[str, Any] = {
        "workout_id": workout_id,
        "name": created.get("workoutName") or spec.name,
        "sport": spec.sport,
        "estimated_duration_min": minutes(payload.get("estimatedDurationInSecs"), 0),
        "steps": summarize(payload["workoutSegments"][0]["workoutSteps"]),
        "url": WORKOUT_URL.format(workout_id=workout_id),
    }
    unmapped = [item.title or "" for item in spec.steps if isinstance(item, Exercise) and item.category is None]
    if unmapped:
        result["unmapped"] = unmapped
    if scheduled_for is not None:
        day = scheduled_for.isoformat()
        try:
            schedule = await service.call(lambda c: c.schedule_workout(workout_id, day))
            result["scheduled"] = clean({"date": day, "schedule_id": _schedule_id(schedule)})
        except ToolError as exc:
            result["scheduled"] = {"date": day, "error": f"workout created but scheduling failed: {exc}"}
    result["next_step"] = "Sync the watch with the Garmin Connect app to receive the workout."
    return clean(result)


async def schedule_workout(service: GarminService, workout_id: int, date_str: str) -> dict[str, Any]:
    if workout_id <= 0:
        raise ToolError("workout_id must be a positive integer")
    d = _check_schedule_date(date_str, service.settings.timezone)

    def run(client: Any) -> dict[str, Any]:
        try:
            existing = [
                e for e in _fetch_schedules(client, d, d)
                if e.get("workoutId") == workout_id and (e.get("scheduleDate") or e.get("date")) == d.isoformat()
            ]
        except Exception:
            existing = []  # the duplicate check is best effort
        if existing:
            return {"already_scheduled": True, "schedule_id": existing[0].get("scheduledWorkoutId") or existing[0].get("id")}
        return {"already_scheduled": False, "schedule_id": _schedule_id(client.schedule_workout(workout_id, d.isoformat()))}

    outcome = await service.call(run)
    return clean({"workout_id": workout_id, "date": d.isoformat(), **outcome})


async def list_scheduled_workouts(service: GarminService, start_date: str | None, end_date: str | None) -> dict[str, Any]:
    tz = service.settings.timezone
    start = resolve_date(start_date, tz, "start_date")
    end = resolve_date(end_date, tz, "end_date") if end_date else start + timedelta(days=13)
    if start > end:
        raise ToolError("start_date must be on or before end_date")
    if (end - start).days + 1 > 62:
        raise ToolError("date range too long (max 62 days)")
    entries = await service.call(lambda c: _fetch_schedules(c, start, end))
    items = sorted((shape_scheduled(e) for e in entries), key=lambda e: e.get("date") or "")
    return {"start": start.isoformat(), "end": end.isoformat(), "count": len(items), "scheduled": items}


async def unschedule_workout(service: GarminService, schedule_id: int) -> dict[str, Any]:
    if schedule_id <= 0:
        raise ToolError("schedule_id must be a positive integer")
    await service.call(lambda c: c.unschedule_workout(schedule_id))
    return {"schedule_id": schedule_id, "unscheduled": True,
            "note": "The workout itself stays in the Garmin workout library."}

