"""garmin-mcp's answers, read by the coach (PR 31).

`contract/garmin_answers.json` is a byte-identical copy of garmin-mcp/tests/contract/garmin_answers.json (what
garmin-mcp answers the six tools the coach calls, for its canned payloads; `scripts/check.sh` compares the copies).
A fake garmin-mcp replays each recorded result over MCP, and the coach's own readers must find every metric they
read: a key renamed in a garmin-mcp shaper turns this file red once the copy is updated.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fakes import fake_garmin_app, fake_garmin_running
from mcp.types import CallToolResult, TextContent

from coach_mcp import briefing, history, scheduler
from coach_mcp.config import Settings
from coach_mcp.garmin import ALLOWED_TOOLS, BRIEFING_TOOLS, ERROR_KIND_KEY, HISTORY_TOOLS, GarminReader

GOLDEN = Path(__file__).parent / "contract" / "garmin_answers.json"
RECORDS: dict[str, dict[str, Any]] = json.loads(GOLDEN.read_text(encoding="utf-8"))
DAY = date(2026, 9, 30)  # the day garmin-mcp recorded the answers for
TOKEN = "garmin-backend-token-0123456789abcdef"


def result_of(record: dict[str, Any]) -> CallToolResult:
    """The recorded result as garmin-mcp sends it (`to_json` text: compact, not ASCII-escaped)."""
    answer = json.dumps(record["answer"], ensure_ascii=False, separators=(",", ":"))
    text = record["text"] if record["is_error"] else answer
    return CallToolResult(
        content=[TextContent(type="text", text=text)], is_error=record["is_error"], meta=record["meta"]
    )


def scenario(name: str) -> dict[str, CallToolResult]:
    """Every tool answers its record of scenario ``name`` (``ok``, ``rate_limited`` ...) when it has one."""
    return {tool: result_of(RECORDS[f"{tool} {name}"]) for tool in ALLOWED_TOOLS if f"{tool} {name}" in RECORDS}


def reader(app, tmp_path) -> GarminReader:
    token_file = tmp_path / "garmin_readonly_token"
    token_file.write_text(TOKEN, encoding="utf-8")
    settings = Settings(garmin_url="http://garmin-mcp:8000/mcp", garmin_token_file=token_file)
    return GarminReader(settings, transport=httpx2.ASGITransport(app=app))


def test_the_contract_covers_the_tools_the_coach_calls():
    assert {r["tool"] for r in RECORDS.values()} == set(ALLOWED_TOOLS)


def test_every_recorded_error_carries_the_kind_key_the_coach_reads():
    errors = [r for r in RECORDS.values() if r["is_error"]]
    assert errors and all(isinstance((r["meta"] or {}).get(ERROR_KIND_KEY), str) for r in errors)
    sections = [
        v for r in RECORDS.values() if not r["is_error"] for v in r["answer"].values()
        if isinstance(v, dict) and "error" in v
    ]
    assert sections and all(isinstance(v.get("error_kind"), str) for v in sections)


@pytest.mark.anyio
async def test_the_briefing_reads_every_metric_with_the_coachs_arguments(tmp_path):
    app, calls = fake_garmin_app(TOKEN, results=scenario("ok"))
    async with fake_garmin_running(app):
        data = await reader(app, tmp_path).fetch(DAY)
    assert calls == [(t, RECORDS[f"{t} ok"]["arguments"]) for t in BRIEFING_TOOLS]  # the arguments match
    assert data.calls == dict.fromkeys(BRIEFING_TOOLS, "ok")
    assert data.last_sync is not None and scheduler.reusable(data)
    made = briefing.build_briefing(DAY, data, [], {})
    assert made.payload["missing"] == []
    holds = {a["key"]: a["holds"] for a in made.payload["alerts"]}
    assert {key for key, value in holds.items() if value is None} == set()  # every alert input was read
    assert made.payload["garmin_today"] == ["Z2 kolo"]
    assert briefing.hrv_night_present(data.snapshot)


def test_the_history_reads_every_column_of_the_snapshot():
    snapshot = RECORDS["get_recovery_snapshot ok"]["answer"]
    values = history.recovery_values(snapshot)
    assert {column for column, value in values.items() if value is None} == set()
    assert history.section_errors(snapshot) == frozenset()
    assert values["readiness"] == snapshot["readiness"]["morning_score"]  # morning_score wins over score


def test_the_snapshot_keys_of_the_coach_are_garmin_mcps():
    snapshot = RECORDS["get_recovery_snapshot ok"]["answer"]
    assert {key: frozenset(snapshot[key]) for key in history.SNAPSHOT_SECTIONS} == history.SNAPSHOT_KEYS


def test_an_empty_day_and_a_degraded_snapshot_are_not_drift():
    empty = RECORDS["get_recovery_snapshot no_data"]["answer"]
    assert history.drifted_sections(empty) == frozenset() and history.section_errors(empty) == frozenset()
    degraded = RECORDS["get_recovery_snapshot degraded"]["answer"]
    assert history.drifted_sections(degraded) == frozenset()
    assert history.section_errors(degraded) == {"hrv", "sleep", "training_status"}


def test_the_history_reads_activities_and_race_predictions():
    activities = RECORDS["list_activities ok"]["answer"]
    days = [date(2026, 9, d) for d in range(24, 31)]
    totals = history.activity_totals(activities, days)
    assert totals[date(2026, 9, 29)]["running"] == {
        "count": 1, "duration_min": 50.1, "distance_km": 10.02, "training_load": 143.0
    }
    assert set(totals[date(2026, 9, 28)]) == {"strength"} and set(totals[date(2026, 9, 27)]) == {"cycling"}
    assert RECORDS["list_activities truncated"]["answer"]["truncated"] is True
    predictions = history.prediction_values(RECORDS["get_race_predictions ok"]["answer"], days[0], days[-1])
    assert predictions is not None and list(predictions) == [DAY]
    assert None not in predictions[DAY].values()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ok", "ok"),
        ("rate_limited", "rate_limited"),  # whole-tool errors and, for the snapshot, error sections
        ("not_logged_in", "auth_error"),
        ("session_expired", "auth_error"),
        ("garmin_unavailable", None),  # error, or ok with every section in error (the snapshot)
    ],
)
async def test_the_history_client_classifies_every_recorded_answer(tmp_path, name, expected):
    app, _ = fake_garmin_app(TOKEN, results=scenario(name))
    async with fake_garmin_running(app):
        client = reader(app, tmp_path)
        got = {t: (await client.history_call(t, RECORDS[f"{t} {name}"]["arguments"]))[0] for t in HISTORY_TOOLS}
    if expected is not None:
        assert got == dict.fromkeys(HISTORY_TOOLS, expected)
    else:
        assert got == {"get_recovery_snapshot": "ok", "list_activities": "error", "get_race_predictions": "error"}


@pytest.mark.anyio
async def test_the_briefing_client_marks_a_recorded_login_error_as_auth(tmp_path):
    app, _ = fake_garmin_app(TOKEN, auth="missing", results=scenario("not_logged_in"))
    async with fake_garmin_running(app):
        data = await reader(app, tmp_path).fetch(DAY)
    assert data.calls == dict.fromkeys(BRIEFING_TOOLS, "auth_error")
