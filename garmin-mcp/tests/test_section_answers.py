"""Answers of the tools built from sections, for canned, failing and odd payloads, compared with a recording (PR 25).

`section_answers.json` holds one short hash per scenario. It was recorded from the code before PR 25 with
`RECORD_SECTION_ANSWERS=1`: the refactor leaves every answer, error text and key order as it was. A scenario whose
answer PR 25 changes on purpose is listed in CHANGED with the test that states the new answer.
PR 31 adds a kind to every error: `without_kinds` drops the `error_kind` keys and a raised ToolError subclass is
named ToolError, so the recording shows that the kinds are the only change (test_error_kinds.py and
tests/contract/garmin_answers.json pin them)."""

import copy
import hashlib
import json
import os
from datetime import date
from pathlib import Path

import pytest
from garminconnect import GarminConnectNotFoundError
from mcp.server.mcpserver.exceptions import ToolError

from conftest import AUTH_ERROR, CONN_ERROR, FakeGarmin
from garmin_coach_mcp import activities, body, health, performance
from garmin_coach_mcp.formatting import to_json
from garmin_coach_mcp.garmin_client import GarminService

RECORDING = Path(__file__).with_name("section_answers.json")
DAY = "2026-09-30"

TOOLS = {
    "recovery_snapshot": (lambda s: health.recovery_snapshot(s, DAY),
                          ["get_training_readiness", "get_hrv_data", "get_sleep_data", "get_user_summary",
                           "get_training_status"]),
    "device_status": (lambda s: health.device_status(s), ["get_device_last_used", "get_devices"]),
    "hrv_trend": (lambda s: health.hrv_trend(s, 3, DAY), ["get_hrv_data_range", "get_hrv_data"]),
    "performance_metrics": (lambda s: performance.performance_metrics(s, 12, end=date(2026, 9, 30)),
                            ["get_race_predictions", "get_lactate_threshold", "get_heart_rate_zones",
                             "get_power_zones", "get_endurance_score", "get_hill_score", "get_max_metrics_range"]),
    "race_predictions": (lambda s: performance.race_predictions(s, "2026-09-01", DAY), ["get_race_predictions"]),
    "running_tolerance": (lambda s: performance.running_tolerance(s, 8, DAY), ["get_running_tolerance"]),
    "gear": (lambda s: performance.gear(s), ["get_gear", "get_gear_stats"]),
    "nutrition": (lambda s: body.nutrition(s, DAY),
                  ["get_nutrition_daily_food_log", "get_nutrition_daily_meals", "get_nutrition_daily_settings",
                   "get_hydration_data"]),
    "get_activity": (lambda s: activities.get_activity(s, 20001),
                     ["get_activity", "get_activity_splits", "get_activity_hr_in_timezones", "get_activity_weather",
                      "get_activity_exercise_sets"]),
}
FAILURES = {"conn": CONN_ERROR, "404": GarminConnectNotFoundError("API Error 404"), "auth": AUTH_ERROR}
ODD = {
    "none": None, "empty_dict": {}, "empty_list": [], "text": "x", "nested": {"a": [1, "b", None]},
    "list_of_text": ["x"],
    # values that make a shaper raise: a text where a number is expected, a date that is not one
    "bad_values": [{"secsInZone": "x", "zoneNumber": 1, "calendarDate": "not-a-date", "uuid": "u-1",
                    "generic": {"calendarDate": "not-a-date", "vo2MaxPreciseValue": 50}}],
}
# Spec D5: a shaper that raises now degrades its section instead of failing the whole tool.
CHANGED = {
    "get_activity get_activity_hr_in_timezones odd bad_values":
        "test_activities.py::test_get_activity_odd_zones_payload_is_left_out",
}


def _scenarios() -> list[tuple[str, str, str, str]]:
    rows = [(tool, "", "canned", "") for tool in TOOLS]
    for tool, (_, methods) in TOOLS.items():
        for method in methods:
            rows += [(tool, method, "fail", name) for name in FAILURES]
            rows += [(tool, method, "odd", name) for name in ODD]
    # the per-day HRV fallback (the range call answers None) with failing days
    rows += [("hrv_trend", "get_hrv_data", "fallback_fail", name) for name in FAILURES]
    return rows


def without_kinds(value):
    if isinstance(value, dict):
        return {k: without_kinds(v) for k, v in value.items() if k != "error_kind"}
    if isinstance(value, list):
        return [without_kinds(v) for v in value]
    return value


async def _answer(settings, tool: str, method: str, kind: str, name: str) -> str:
    fake = FakeGarmin()
    service = GarminService(settings, garmin_factory=lambda: fake)
    if kind in ("fail", "fallback_fail"):
        fake.fail[method] = FAILURES[name]
    if kind == "fallback_fail":
        fake.responses["get_hrv_data_range"] = None
    if kind == "odd":
        fake.responses[method] = copy.deepcopy(ODD[name])
    try:
        return to_json(without_kinds(await TOOLS[tool][0](service)))
    except Exception as exc:  # a failure of the whole tool is part of the answer
        return f"raised {'ToolError' if isinstance(exc, ToolError) else type(exc).__name__}: {exc}"


@pytest.mark.anyio
async def test_section_tool_answers_match_the_recording(settings):
    answers = {}
    for tool, method, kind, name in _scenarios():
        text = await _answer(settings, tool, method, kind, name)
        answers[" ".join(p for p in (tool, method, kind, name) if p)] = hashlib.sha256(text.encode()).hexdigest()[:16]
    if os.environ.get("RECORD_SECTION_ANSWERS") == "1":
        RECORDING.write_text(json.dumps(answers, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    recorded = json.loads(RECORDING.read_text(encoding="utf-8"))
    assert set(answers) == set(recorded)
    differ = sorted(k for k in answers if answers[k] != recorded[k])
    assert differ == sorted(CHANGED)
