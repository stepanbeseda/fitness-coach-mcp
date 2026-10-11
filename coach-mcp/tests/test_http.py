import asyncio
import json
import logging
import sqlite3
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import anyio
import httpx2
import pytest
from fakes import (
    FakeClock,
    FakeGarmin,
    FakeHistorySource,
    FakePush,
    FakeStrengthSource,
    StopLoop,
    fake_garmin_app,
    fake_garmin_running,
)

from conftest import BACKEND_TOKEN, MCP_HEADERS, TODAY, auth_headers, call_tool, payload, running_client
from coach_mcp.__main__ import configure_logging
from coach_mcp.config import Settings
from coach_mcp.garmin import GarminData, GarminReader
from coach_mcp.server import create_app
from coach_mcp.store import SCHEMA_VERSION, Store
from coach_mcp.tools import SEARCH_QUERY_ERROR
from garmin_payloads import (
    GREEN_SNAPSHOT,
    YELLOW_SNAPSHOT,
    activities_payload,
    activity,
    garmin_data,
    history_snapshot,
    yellow_data,
)
from history_helpers import (
    exercise,
    local,
    make_job,
    run_first,
    seed_activities,
    seed_day,
    seed_strength,
    seed_sync,
    seed_template,
    sport,
)
from weather_payloads import WEATHER_T0, open_meteo_payload

EXPECTED_TOOLS = {
    "get_coach_context",
    "get_plan",
    "set_goal",
    "set_constraint",
    "save_plan",
    "update_session",
    "add_note",
    "get_daily_briefing",
    "get_trends",
    "get_weekly_report",
    "get_strength_progress",
    "search_notes",
    "list_goals",
    "list_constraints",
    "get_correlations",
    "get_weather_forecast",
    "get_goal_progress",
}
WRITE_TOOLS = {"set_goal", "set_constraint", "save_plan", "update_session", "add_note"}
# Tools that reach an outside service (garmin-mcp, Open-Meteo); every other tool only touches the local store.
OPEN_WORLD_TOOLS = {"get_daily_briefing", "get_weather_forecast"}

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}},
}


async def rpc(client, method, params=None, id_=2, headers=None):
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return await client.post("/mcp", headers=headers or auth_headers(), json=body)


@pytest.mark.anyio
async def test_healthz_reports_db_state(app, store, monkeypatch):
    async with running_client(app) as client:
        r = await client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {
            "status": "ok",
            "db": "ok",
            "schema_version": SCHEMA_VERSION,
            "scheduler": "off",
            "history": "off",
        }

        def broken():
            raise sqlite3.OperationalError("x")

        monkeypatch.setattr(store, "check", broken)
        r = await client.get("/healthz")
        assert r.status_code == 503
        assert r.json() == {"status": "error", "db": "error"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [MCP_HEADERS, auth_headers("wrong-token-wrong-token-wrong"), {**MCP_HEADERS, "Authorization": BACKEND_TOKEN}],
)
async def test_mcp_requires_bearer(app, headers):
    async with running_client(app) as client:
        r = await client.post("/mcp", headers=headers, json=INIT)
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.anyio
async def test_missing_or_short_backend_token_fails_closed(app, bearer_file):
    async with running_client(app) as client:
        bearer_file.unlink()
        r = await client.post("/mcp", headers=auth_headers(), json=INIT)
        assert r.status_code == 503
        bearer_file.write_text("short")
        r = await client.post("/mcp", headers=auth_headers("short"), json=INIT)
        assert r.status_code == 503


@pytest.mark.anyio
async def test_initialize_list_and_annotations(app):
    async with running_client(app) as client:
        r = await client.post("/mcp", headers=auth_headers(protocol=None), json=INIT)
        assert r.status_code == 200, r.text
        init = r.json()["result"]
        assert init["serverInfo"]["name"] == "coach-memory"
        assert "get_coach_context" in init["instructions"]
        assert "get_daily_briefing" in init["instructions"]
        assert "get_trends" in init["instructions"]
        assert "get_weekly_report" in init["instructions"]
        assert "get_strength_progress" in init["instructions"]
        for name in ("search_notes", "list_goals", "list_constraints"):
            assert name in init["instructions"]
        assert "get_correlations" in init["instructions"]
        assert "get_weather_forecast" in init["instructions"]
        assert "get_goal_progress" in init["instructions"]

        r = await client.post("/mcp", headers=auth_headers(), json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert r.status_code == 202

        tools = (await rpc(client, "tools/list")).json()["result"]["tools"]
        by_name = {t["name"]: t for t in tools}
        assert set(by_name) == EXPECTED_TOOLS
        for name, tool in by_name.items():
            assert tool["title"] and tool["description"]
            ann = tool["annotations"]
            assert ann["openWorldHint"] is (name in OPEN_WORLD_TOOLS)
            assert ann["readOnlyHint"] is (name not in WRITE_TOOLS)
            if name in WRITE_TOOLS:
                assert ann["destructiveHint"] is False
                assert "only after the athlete confirmed" in tool["description"].lower()
                assert ann["idempotentHint"] is (name == "save_plan")
        assert "distance_km" in json.dumps(by_name["save_plan"]["inputSchema"])
        status_schema = by_name["update_session"]["inputSchema"]["properties"]["status"]
        assert sorted(status_schema["enum"]) == ["done", "moved", "planned", "skipped"]
        assert len(by_name["get_trends"]["inputSchema"]["properties"]["metrics"]["items"]["enum"]) == 46
        corr_schema = by_name["get_correlations"]["inputSchema"]["properties"]
        assert corr_schema["metrics"]["items"]["enum"] == by_name["get_trends"]["inputSchema"]["properties"]["metrics"]["items"]["enum"]
        assert (corr_schema["metrics"]["minItems"], corr_schema["metrics"]["maxItems"]) == (2, 6)
        assert (corr_schema["lag_days"]["minimum"], corr_schema["lag_days"]["maximum"]) == (0, 3)
        goal_schema = by_name["set_goal"]["inputSchema"]["properties"]
        metric_options = next(o for o in goal_schema["metric"]["anyOf"] if "enum" in o)
        assert sorted(metric_options["enum"]) == sorted(
            ["race_5k", "race_10k", "race_half_marathon", "race_marathon", "vo2max_running", ""]
        )
        assert len(json.dumps(tools)) < 40_000


@pytest.mark.anyio
async def test_set_goal_metric_and_target_value_over_http(app, store):
    async with running_client(app) as client:
        made = await call_tool(client, "set_goal", {"title": "VO2max", "metric": "vo2max_running", "target_value": 52})
        assert made["isError"] is False, made
        assert payload(made)["goal"]["target_value"] == 52.0
        assert store.get_goal(1)["target_value"] == 52.0
        text = await call_tool(client, "set_goal", {"title": "10k", "metric": "race_10k", "target_value": "0:45:00"}, id_=3)
        assert payload(text)["goal"]["target_value"] == "45:00"
        for arguments in (
            {"title": "x", "metric": "vo2max_running", "target_value": True},
            {"title": "x", "metric": "race_1k"},
            {"title": "x", "metric": "race_10k", "target_value": "soon"},
        ):
            bad = await call_tool(client, "set_goal", arguments, id_=4)
            assert bad["isError"] is True, arguments
        cleared = await call_tool(client, "set_goal", {"goal_id": 1, "metric": ""}, id_=5)
        assert "metric" not in payload(cleared)["goal"] and store.get_goal(1)["target_value"] is None


@pytest.mark.anyio
async def test_get_goal_progress_over_http(app, store):
    start = TODAY - timedelta(days=83)
    for n in range(83):
        day = start + timedelta(days=n)
        store.store_race_predictions({day: {"time_half_marathon_s": 6000 - 2 * n}}, day + timedelta(days=1), day)
    store.insert_goal(
        {"title": "Half", "metric": "race_half_marathon", "target_value": 5800.0, "target_date": date(2026, 11, 15)}
    )
    store.insert_goal({"title": "Plain"})
    async with running_client(app) as client:
        result = await call_tool(client, "get_goal_progress", {})
        assert result["isError"] is False
        body = payload(result)
    assert body["goals_without_metric"] == 1
    (goal,) = body["goals"]
    assert goal["current"]["time"] == "1:37:16" and goal["trend_per_week"] == -14.0
    assert goal["projected"]["time"] == "1:35:56" and goal["on_track"] is True and goal["days_remaining"] == 39


@pytest.mark.anyio
async def test_tool_error_is_reported_over_http(app):
    async with running_client(app) as client:
        result = await call_tool(client, "get_plan", {"start_date": "2026-13-01"})
        assert result["isError"] is True
        assert "is not a valid date" in result["content"][0]["text"]

        result = await call_tool(client, "update_session", {"session_id": 999, "status": "done"}, id_=3)
        assert result["isError"] is True
        assert "not found" in result["content"][0]["text"]


def _session(**kw):
    return {"date": "2026-10-08", "type": "running", "description": "Easy", **kw}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "sessions",
    [
        [_session(type="yoga")],
        [_session(description="x" * 2001)],
        [_session(description="   ")],
        [_session(rpe=7)],
        [_session() for _ in range(31)],
        [_session(duration_min=0)],
    ],
    ids=["type", "long-description", "blank-description", "extra-field", "31-sessions", "duration-zero"],
)
async def test_argument_validation_over_http(app, db_path, sessions):
    async with running_client(app) as client:
        result = await call_tool(client, "save_plan", {"week": "2026-10-07", "sessions": sessions})
        assert result["isError"] is True
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("set_goal", {"title": "x" * 201}),
        ("set_goal", {"title": "x", "status": "deleted"}),
        ("set_goal", {"goal_id": 0, "title": "x"}),
        ("set_constraint", {"kind": "mood", "title": "x"}),
        ("update_session", {"session_id": 1, "status": "superseded"}),
        ("add_note", {"text": "x" * 2001}),
        ("search_notes", {"query": "knee", "limit": 0}),
        ("search_notes", {"query": "knee", "limit": 51}),
        ("search_notes", {"query": "x" * 201}),
        ("search_notes", {"query": "knee", "session_id": 0}),
    ],
)
async def test_shape_errors_are_tool_errors_not_writes(app, name, arguments):
    async with running_client(app) as client:
        assert (await call_tool(client, name, arguments))["isError"] is True
        ctx = payload(await call_tool(client, "get_coach_context", {}, id_=3))
        assert ctx["goals"] == [] and ctx["constraints"] == [] and ctx["notes"] == []


@pytest.mark.anyio
async def test_write_and_read_round_trip_over_http(app):
    async with running_client(app) as client:
        result = await call_tool(client, "set_goal", {"title": "10k", "target_date": "2026-10-17"})
        assert result["isError"] is False
        assert payload(result)["created"] is True
        result = await call_tool(
            client,
            "save_plan",
            {"week": "2026-10-07", "sessions": [_session(garmin_workout_id=424242, distance_km=8.5)]},
            id_=3,
        )
        assert result["isError"] is False, result
        ctx = payload(await call_tool(client, "get_coach_context", {}, id_=4))
        assert ctx["goals"][0]["days_remaining"] == 10
        assert ctx["sessions"][0]["garmin_workout_id"] == "424242" and ctx["sessions"][0]["distance_km"] == 8.5


@pytest.mark.anyio
async def test_search_and_lists_over_http(app):
    async with running_client(app) as client:
        added = payload(await call_tool(client, "add_note", {"text": "Bolí mě koleno"}))["note"]
        found = await call_tool(client, "search_notes", {"query": "boli"}, id_=3)
        assert found["isError"] is False
        assert [n["id"] for n in payload(found)["notes"]] == [added["id"]]
        bad = await call_tool(client, "search_notes", {"query": "low-carb"}, id_=4)
        assert bad["isError"] is True
        text = bad["content"][0]["text"]
        assert SEARCH_QUERY_ERROR in text
        assert "no such column" not in text and "fts5" not in text

        assert (await call_tool(client, "set_goal", {"title": "Old 10k"}, id_=5))["isError"] is False
        assert (await call_tool(client, "set_goal", {"goal_id": 1, "status": "achieved"}, id_=6))["isError"] is False
        result = await call_tool(client, "set_constraint", {"kind": "injury", "title": "Knee"}, id_=7)
        assert result["isError"] is False
        result = await call_tool(client, "set_constraint", {"constraint_id": 1, "status": "resolved"}, id_=8)
        assert result["isError"] is False
        goals = payload(await call_tool(client, "list_goals", {}, id_=9))["goals"]
        assert [(g["title"], g["status"]) for g in goals] == [("Old 10k", "achieved")]
        constraints = payload(await call_tool(client, "list_constraints", {}, id_=10))["constraints"]
        assert [(c["title"], c["status"]) for c in constraints] == [("Knee", "resolved")]
        ctx = payload(await call_tool(client, "get_coach_context", {}, id_=11))
        assert ctx["goals"] == [] and ctx["constraints"] == []


@pytest.mark.anyio
async def test_goal_survives_restart_over_http(app, settings, db_path):
    async with running_client(app) as client:
        result = await call_tool(client, "set_goal", {"title": "Half marathon", "target_date": "2027-04-05"})
        assert result["isError"] is False, result

    second = create_app(settings, Store(db_path), today=lambda: TODAY)
    async with running_client(second) as client:
        ctx = payload(await call_tool(client, "get_coach_context", {}))
    assert [g["title"] for g in ctx["goals"]] == ["Half marathon"]
    assert ctx["goals"][0]["days_remaining"] == 180


@pytest.mark.anyio
async def test_unknown_host_header_is_accepted(app):
    """Requests arrive via the gateway with Host: coach-mcp:8000; no 421."""
    async with running_client(app) as client:
        r = await client.post("/mcp", headers={**auth_headers(), "Host": "coach-mcp:8000"}, json=INIT)
        assert r.status_code == 200


@pytest.mark.anyio
async def test_log_never_contains_arguments_or_stored_text(app, caplog, restore_logging):
    """Tool errors echo caller input; the SDK logs them at INFO unless configure_logging quiets it."""
    configure_logging("INFO")
    caplog.set_level(logging.DEBUG)
    async with running_client(app) as client:
        failed = await call_tool(client, "get_plan", {"start_date": "SECRET-DATE-TEXT"})
        assert failed["isError"] is True and "SECRET-DATE-TEXT" in failed["content"][0]["text"]
        ok = await call_tool(client, "add_note", {"text": "PRIVATE-NOTE-TEXT"}, id_=3)
        assert ok["isError"] is False
        found = await call_tool(client, "search_notes", {"query": "PRIVATEQUERYWORD"}, id_=4)
        assert found["isError"] is False
        bad = await call_tool(client, "search_notes", {"query": "PRIVATE-QUERY-TEXT"}, id_=5)
        assert bad["isError"] is True
    assert "tool=get_plan status=error" in caplog.text and "tool=add_note status=ok" in caplog.text
    assert "tool=search_notes status=ok" in caplog.text
    assert "tool=search_notes status=error kind=ToolError" in caplog.text
    assert "SECRET-DATE-TEXT" not in caplog.text and "PRIVATE-NOTE-TEXT" not in caplog.text
    for secret in ("PRIVATEQUERYWORD", "PRIVATE-QUERY-TEXT", "no such column", "fts5"):
        assert secret not in caplog.text


# ---------------------------------------------------------- daily briefing

PRAGUE = ZoneInfo("Europe/Prague")
GARMIN_TOKEN = "garmin-backend-token-0123456789abcdef"


def push_settings(settings: Settings, **overrides) -> Settings:
    fields = {**settings.__dict__, "telegram_chat_id": "123456789"}
    fields.update(overrides)
    return Settings(**fields)


def at_0630() -> datetime:
    return datetime(2026, 10, 7, 6, 30, tzinfo=PRAGUE)


async def never_wake(_seconds: float) -> None:
    """A scheduler sleep that never returns in time; it ends by raising, so a missing cancel is red, not a hang."""
    await asyncio.sleep(5)
    raise StopLoop


async def wait_for(condition, seconds: float = 2.0) -> None:
    with anyio.fail_after(seconds):
        while not condition():
            await anyio.sleep(0.01)


@pytest.mark.anyio
async def test_daily_briefing_over_http(settings, store):
    # the canonical alert rows of the YELLOW fixture (spec 4.3)
    with sqlite3.connect(settings.db_path) as conn:
        conn.executemany(
            "INSERT INTO alert_state (key, value, evaluated_on, fired_on) VALUES (?, ?, ?, ?)",
            [
                ("resting_hr_elevated", 0, "2026-10-06", None),
                ("acwr_out_of_range", 1, "2026-10-06", "2026-10-04"),
                ("garmin_auth", 0, "2026-10-06", None),
            ],
        )
    garmin = FakeGarmin(yellow_data())
    app = create_app(settings, store, today=lambda: TODAY, garmin=garmin)
    async with running_client(app) as client:
        result = await call_tool(client, "get_daily_briefing", {})
    assert result["isError"] is False
    briefing = payload(result)
    assert briefing["date"] == "2026-10-07" and briefing["verdict"] == "yellow"
    assert briefing["missing"] == ["hrv", "body_battery"]
    assert briefing["criteria"]["readiness"] == {"level": "yellow", "score": 61}
    assert briefing["criteria"]["sleep"] == {"level": "yellow", "minutes": 375, "score": 70}
    assert [(a["key"], a["holds"], a["new"]) for a in briefing["alerts"]] == [
        ("hrv_below_baseline", None, False),
        ("resting_hr_elevated", True, True),
        ("acwr_out_of_range", True, False),
        ("missed_workout", True, True),
        ("missed_planned_session", False, False),
        ("garmin_auth", False, False),
    ]
    assert briefing["garmin_today"] == ["Lehký běh 40 min"]
    assert list(briefing["push"]) == ["text"]
    assert briefing["push"]["text"].split("\n")[0] == "\U0001f7e1 \u26a0\ufe0f YELLOW · morning briefing 7 Oct"  # English by default
    assert garmin.days == [TODAY]


@pytest.mark.anyio
async def test_daily_briefing_changes_no_state(settings, store, db_path):
    app = create_app(settings, store, today=lambda: TODAY, garmin=FakeGarmin(yellow_data()))
    async with running_client(app) as client:
        await call_tool(client, "get_daily_briefing", {})
        await call_tool(client, "get_daily_briefing", {}, id_=3)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM briefing_runs").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM alert_state").fetchone()[0] == 0


@pytest.mark.anyio
async def test_daily_briefing_with_unreachable_garmin_is_still_a_result(settings, store):
    app = create_app(
        settings, store, today=lambda: TODAY, garmin=FakeGarmin(GarminData(auth=None, calls={"get_hrv_trend": "error"}))
    )
    async with running_client(app) as client:
        result = await call_tool(client, "get_daily_briefing", {})
    assert result["isError"] is False
    briefing = payload(result)
    assert briefing["verdict"] == "yellow" and briefing["garmin"]["auth"] == "unreachable"
    assert briefing["missing"] == ["readiness", "hrv", "sleep", "body_battery"]


@pytest.mark.anyio
async def test_scheduler_off_without_push_config(settings, store):
    garmin, push = FakeGarmin(garmin_data()), FakePush()
    app = create_app(settings, store, today=lambda: TODAY, garmin=garmin, push=push, now=at_0630, sleep=never_wake)
    async with running_client(app) as client:
        assert (await client.get("/healthz")).json()["scheduler"] == "off"
        await anyio.sleep(0.05)
    assert garmin.calls == 0 and push.sent == []


@pytest.mark.anyio
async def test_scheduler_on_sends_from_lifespan(settings, store, caplog):
    caplog.set_level(logging.DEBUG)
    garmin, push = FakeGarmin(garmin_data()), FakePush()
    app = create_app(push_settings(settings), store, garmin=garmin, push=push, now=at_0630, sleep=never_wake)
    async with running_client(app) as client:
        await wait_for(lambda: len(push.sent) == 1)
        assert (await client.get("/healthz")).json()["scheduler"] == "on"
    await anyio.sleep(0.05)  # let the task's done-callback run
    # a normal shutdown is a cancellation, not a "stopped" scheduler, and the callback raised nothing
    assert "briefing status=stopped" not in caplog.text and "Exception in callback" not in caplog.text
    assert garmin.days == [TODAY]
    assert push.sent[0][1].title == "\U0001f7e2 GREEN · morning briefing 7 Oct"
    assert store.briefing_sent(TODAY)


@pytest.mark.anyio
async def test_scheduler_that_dies_is_reported_stopped(settings, store, caplog):
    caplog.set_level(logging.DEBUG)
    before_start = datetime(2026, 10, 7, 5, 0, tzinfo=PRAGUE)

    async def dying_sleep(_seconds: float) -> None:
        raise RuntimeError("boom")  # outside run()'s try: the scheduler task dies

    app = create_app(
        push_settings(settings), store, garmin=FakeGarmin(garmin_data()), push=FakePush(),
        now=lambda: before_start, sleep=dying_sleep,
    )
    with anyio.fail_after(10):
        async with running_client(app) as client:
            await wait_for(lambda: "briefing status=stopped kind=RuntimeError" in caplog.text)
            body = (await client.get("/healthz")).json()
            assert body["scheduler"] == "stopped" and body["status"] == "ok"
            assert (await client.get("/healthz")).status_code == 200
            left_at = time.monotonic()
        assert time.monotonic() - left_at < 2  # the dead task is not awaited again on shutdown
    assert "boom" not in caplog.text


@pytest.mark.anyio
async def test_lifespan_exit_cancels_a_running_fetch(settings, store):
    class HangingGarmin:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def fetch(self, day):
            self.started.set()
            await asyncio.sleep(5)
            raise AssertionError("fetch was not cancelled")

    garmin, push = HangingGarmin(), FakePush()
    app = create_app(push_settings(settings), store, garmin=garmin, push=push, now=at_0630, sleep=never_wake)
    with anyio.fail_after(10):
        async with running_client(app):
            await asyncio.wait_for(garmin.started.wait(), 2)
            left_at = time.monotonic()
        # a swallowed cancellation would keep the shutdown waiting for the scheduler task
        assert time.monotonic() - left_at < 2
    assert push.sent == []


@pytest.mark.anyio
async def test_lifespan_exit_cancels_a_fetch_hanging_inside_the_sdk_client(settings, store, tmp_path):
    token_file = tmp_path / "garmin_readonly_token"
    token_file.write_text(GARMIN_TOKEN, encoding="utf-8")
    fake, calls = fake_garmin_app(GARMIN_TOKEN, hang=frozenset({"get_recovery_snapshot"}))
    config = push_settings(settings, garmin_token_file=token_file)
    reader = GarminReader(config, transport=httpx2.ASGITransport(app=fake))
    push = FakePush()
    app = create_app(config, store, garmin=reader, push=push, now=at_0630, sleep=never_wake)
    with anyio.fail_after(10):
        async with fake_garmin_running(fake):
            async with running_client(app):
                await wait_for(lambda: [name for name, _ in calls] == ["get_device_status", "get_recovery_snapshot"])
                left_at = time.monotonic()
            assert time.monotonic() - left_at < 2
    assert push.sent == []


@pytest.mark.anyio
async def test_push_and_tool_report_the_same_briefing(settings, store):
    # HRV is present, so the scheduler sends at 06:30 instead of waiting for the watch to sync.
    snapshot = {**YELLOW_SNAPSHOT, "hrv": GREEN_SNAPSHOT["hrv"]}
    garmin, push = FakeGarmin(yellow_data(snapshot=snapshot)), FakePush()
    app = create_app(push_settings(settings), store, garmin=garmin, push=push, now=at_0630, sleep=never_wake)
    async with running_client(app) as client:
        await wait_for(lambda: len(push.sent) == 1 and store.briefing_sent(TODAY))
        tool = payload(await call_tool(client, "get_daily_briefing", {}))
    (_, sent), = push.sent
    assert tool["push"] == {"text": sent.text}
    assert tool["verdict"] == "yellow"
    assert [a["key"] for a in tool["alerts"] if a["new"]] == ["resting_hr_elevated", "acwr_out_of_range", "missed_workout"]
    for text in ("Resting HR 58 bpm", "acute to chronic load ratio 1.62", "Intervaly 6×800 m"):
        assert text in sent.text
    # the alert state the push wrote keeps the already announced alerts "new" for the day
    assert store.alert_states()["acwr_out_of_range"]["fired_on"] == "2026-10-07"


# ------------------------------------------------------------ stored history


@pytest.mark.anyio
async def test_trends_and_weekly_report_over_http(app, store):
    for n in range(1, 8):
        day = date(2026, 9, 28) + timedelta(days=n - 1)  # Monday 09-28 .. Sunday 10-04
        seed_day(store, day, hrv_last_night_ms=50 + n, sleep_duration_h=7.0, resting_hr=48)
        seed_activities(store, day, {"running": sport(1, 40, 8.0, 50)} if n % 2 else {})
    async with running_client(app) as client:
        result = await call_tool(client, "get_trends", {"metrics": ["hrv_last_night_ms", "running_distance_km"]})
        assert result["isError"] is False
        trend = payload(result)
        assert trend["start"] == "2026-07-09" and trend["end"] == "2026-10-06" and trend["period"] == "week"
        assert trend["days_with_data"] == 7
        assert trend["metrics"]["hrv_last_night_ms"]["summary"]["n"] == 7
        assert trend["metrics"]["running_distance_km"]["buckets"][0]["total"] == 32.0

        result = await call_tool(client, "get_weekly_report", {}, id_=3)
        assert result["isError"] is False
        report = payload(result)
        assert report["week"]["iso_week"] == "2026-W40" and report["week"]["partial"] is False
        assert report["endurance"]["total"]["count"] == 4
        assert report["strength"]["available"] is False

        bad = await call_tool(client, "get_trends", {"metrics": ["readiness"], "end_date": "2026-10-09"}, id_=4)
        assert bad["isError"] is True and "end_date must not be after today" in bad["content"][0]["text"]
        for arguments in ({"metrics": []}, {"metrics": ["steps"]}, {"metrics": ["readiness"] * 9}):
            invalid = await call_tool(client, "get_trends", arguments, id_=5)
            assert invalid["isError"] is True, arguments
        future = await call_tool(client, "get_weekly_report", {"week": "2026-10-12"}, id_=6)
        assert future["isError"] is True and "future" in future["content"][0]["text"]


@pytest.mark.anyio
async def test_correlations_over_http(app, store):
    hrv = [50, 52, 49, 55, 58, 54, 60, 57, 62, 59, 65, 63, 61, 66]
    rhr = [60, 58, 61, 57, 55, 58, 52, 56, 51, 54, 49, 50, 53, 48]
    for n, (h, r) in enumerate(zip(hrv, rhr, strict=True)):
        seed_day(store, date(2026, 9, 1) + timedelta(days=n), hrv_last_night_ms=h, resting_hr=r)
    async with running_client(app) as client:
        arguments = {
            "metrics": ["hrv_last_night_ms", "resting_hr"],
            "start_date": "2026-09-01",
            "end_date": "2026-09-30",
            "lag_days": 0,
        }
        result = await call_tool(client, "get_correlations", arguments)
        assert result["isError"] is False
        out = payload(result)
        assert out["pairs"] == [
            {"a": "hrv_last_night_ms", "b": "resting_hr", "n_pairs": 14, "available": True,
             "pearson": -0.989, "spearman": -0.995}
        ]
        assert "not a cause" in out["caveat"]
        for n, bad in enumerate(
            (
                {"metrics": ["readiness"]},
                {"metrics": ["readiness"] * 7},
                {"metrics": ["readiness", "steps"]},
                {"metrics": ["readiness", "sleep_score"], "lag_days": 4},
            ),
            start=3,
        ):
            invalid = await call_tool(client, "get_correlations", bad, id_=n)
            assert invalid["isError"] is True, bad
        lagged = await call_tool(
            client,
            "get_correlations",
            {**arguments, "start_date": "2026-09-02", "lag_days": 1},
            id_=9,
        )
        assert lagged["isError"] is False
        out = payload(lagged)
        assert (out["start"], out["end"], out["lag_days"]) == ("2026-09-02", "2026-09-30", 1)
        assert out["pairs"] == [
            {"a": "hrv_last_night_ms", "b": "resting_hr", "n_pairs": 12, "available": False,
             "reason": "too_few_pairs"}
        ]
        same = await call_tool(client, "get_correlations", {"metrics": ["readiness", "readiness"]}, id_=8)
        assert same["isError"] is True and "at least two different metrics" in same["content"][0]["text"]


def history_settings(settings: Settings, **overrides) -> Settings:
    return Settings(**{**settings.__dict__, "history_enabled": True, **overrides})


def history_clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 7, 14, 0, tzinfo=PRAGUE))


def history_sleep_for(source: FakeHistorySource, clock: FakeClock):
    """Wakes up on the fake clock until the first backend call, then never (ends by raising, never hangs)."""

    async def sleep(seconds: float) -> None:
        if source.calls:
            await never_wake(seconds)
        await clock.sleep(seconds)
        await asyncio.sleep(0)

    return sleep


@pytest.mark.anyio
async def test_reconcile_suggestions_reach_the_tools_over_http(app, store):
    run_id, strength_id = store.replace_planned(
        date(2026, 10, 5),
        date(2026, 10, 11),
        date(2026, 10, 5),
        [
            {"date": date(2026, 10, 5), "type": "running", "description": "Easy run"},
            {"date": date(2026, 10, 6), "type": "strength", "description": "Legs"},
        ],
    )[1]
    payload_ = activities_payload([activity("2026-10-05", "running", 45, 9.5)])
    source = FakeHistorySource(activities=lambda start, end, index: ("ok", payload_))
    job, clock = make_job(store, source, local(TODAY, 10, 0), history_backfill_days=3)
    await run_first(job, clock)
    async with running_client(app) as client:
        plan = payload(await call_tool(client, "get_plan", {}))
        by_id = {s["id"]: s for s in plan["sessions"]}
        assert by_id[run_id]["suggestion"]["result"] == "matched"
        assert by_id[run_id]["suggestion"]["evidence"]["sport"] == "running"
        assert by_id[strength_id]["suggestion"]["result"] == "no_activity"
        context = payload(await call_tool(client, "get_coach_context", {}))
        assert sorted(s["id"] for s in context["to_confirm"]) == sorted([run_id, strength_id])
        marked = await call_tool(client, "update_session", {"session_id": run_id, "status": "done"})
        assert marked["isError"] is False
        plan = payload(await call_tool(client, "get_plan", {}))
        by_id = {s["id"]: s for s in plan["sessions"]}
        assert by_id[run_id]["status"] == "done" and "suggestion" not in by_id[run_id]
        assert "suggestion" in by_id[strength_id]


@pytest.mark.anyio
async def test_history_job_on_from_lifespan(settings, store, caplog):
    caplog.set_level(logging.DEBUG)
    clock = history_clock()
    source = FakeHistorySource(clock=clock)
    app = create_app(
        history_settings(settings, history_backfill_days=2),
        store,
        today=lambda: TODAY,
        now=clock.now,
        history_source=source,
        history_sleep=history_sleep_for(source, clock),
    )
    async with running_client(app) as client:
        await wait_for(lambda: source.calls)
        assert (await client.get("/healthz")).json()["history"] == "on"
        assert source.auth_checks == 1
    await anyio.sleep(0.05)  # let the task's done-callback run
    assert "history status=stopped" not in caplog.text and "Exception in callback" not in caplog.text
    assert source.calls[0][0] == "list_activities"


@pytest.mark.anyio
async def test_history_job_that_dies_is_reported_stopped(settings, store, caplog):
    caplog.set_level(logging.DEBUG)

    async def dying_sleep(_seconds: float) -> None:
        raise RuntimeError("boom")  # outside run()'s try: the history task dies

    app = create_app(
        history_settings(settings),
        store,
        today=lambda: TODAY,
        now=lambda: datetime(2026, 10, 7, 14, 0, tzinfo=PRAGUE),
        history_source=FakeHistorySource(),
        history_sleep=dying_sleep,
    )
    with anyio.fail_after(10):
        async with running_client(app) as client:
            await wait_for(lambda: "history status=stopped kind=RuntimeError" in caplog.text)
            response = await client.get("/healthz")
            assert response.status_code == 200
            assert response.json()["history"] == "stopped" and response.json()["status"] == "ok"
            left_at = time.monotonic()
        assert time.monotonic() - left_at < 2  # the dead task is not awaited again on shutdown
    assert "boom" not in caplog.text


@pytest.mark.anyio
async def test_history_off_makes_no_calls(settings, store):
    clock = history_clock()
    source = FakeHistorySource(clock=clock)
    app = create_app(
        history_settings(settings, history_enabled=False),
        store,
        today=lambda: TODAY,
        now=clock.now,
        history_source=source,
        history_sleep=history_sleep_for(source, clock),
    )
    async with running_client(app) as client:
        assert (await client.get("/healthz")).json()["history"] == "off"
        await anyio.sleep(0.05)
    assert source.calls == [] and source.auth_checks == 0


@pytest.mark.anyio
async def test_lifespan_exit_cancels_a_running_history_call(settings, store):
    class HangingSource(FakeHistorySource):
        def __init__(self, clock: FakeClock) -> None:
            super().__init__(clock=clock)
            self.started = asyncio.Event()

        async def history_call(self, name, args):
            self.started.set()
            await asyncio.sleep(5)
            raise AssertionError("history call was not cancelled")

    clock = history_clock()
    source = HangingSource(clock)
    app = create_app(
        history_settings(settings),
        store,
        today=lambda: TODAY,
        now=clock.now,
        history_source=source,
        history_sleep=history_sleep_for(source, clock),
    )
    with anyio.fail_after(10):
        async with running_client(app):
            await asyncio.wait_for(source.started.wait(), 2)
            left_at = time.monotonic()
        # a swallowed cancellation would keep the shutdown waiting for the history task
        assert time.monotonic() - left_at < 2
    assert rows_of(store) == []


@pytest.mark.anyio
async def test_strength_progress_and_weekly_strength_over_http(app, store):
    seed_template(store, "T1", "chest", title="Bench Press (Barbell)")
    seed_template(store, "T2", "quadriceps", title="Squat (Barbell)")
    for n, day in enumerate(("2026-09-16", "2026-09-23", "2026-09-30")):  # Wednesdays
        seed_strength(store, f"B{n}", date.fromisoformat(day), [exercise("T1", 3, 1500.0, (100, 5, 100.0 + 5 * n))])
    seed_strength(store, "S0", date(2026, 9, 30), [exercise("T2", 4, 3200.0, (160, 5, 186.7))])
    seed_sync(store, date(2026, 9, 1), TODAY)
    async with running_client(app) as client:
        result = await call_tool(client, "get_strength_progress", {})
        assert result["isError"] is False
        progress = payload(result)
        assert progress["available"] is True and progress["matched"] == 2
        first, second = progress["exercises"]
        assert (first["template_id"], first["sessions"], first["last_e1rm_kg"]) == ("T1", 3, 110.0)
        assert (second["template_id"], second["muscle_group"]) == ("T2", "quadriceps")

        narrowed = payload(await call_tool(client, "get_strength_progress", {"exercise": "squat", "plateau_weeks": 2, "limit": 1}, id_=3))
        assert narrowed["plateau_weeks"] == 2 and [e["template_id"] for e in narrowed["exercises"]] == ["T2"]
        for arguments in ({"plateau_weeks": 1}, {"plateau_weeks": 13}, {"limit": 0}, {"limit": 21}, {"exercise": "x" * 101}):
            invalid = await call_tool(client, "get_strength_progress", arguments, id_=4)
            assert invalid["isError"] is True, arguments
        bad = await call_tool(client, "get_strength_progress", {"end_date": "2026-10-09"}, id_=5)
        assert bad["isError"] is True and "end_date must not be after today" in bad["content"][0]["text"]

        report = payload(await call_tool(client, "get_weekly_report", {}, id_=6))
        assert report["strength"].get("available") is not False  # a section with data has no "available" key
        assert report["strength"]["workouts"] == 2 and report["strength"]["days"] == 7
        assert set(report["strength"]["by_muscle_group"]) == {"chest", "quadriceps"}


def strength_sleep_for(strength: FakeStrengthSource, clock: FakeClock):
    """Wakes up on the fake clock until the first Hevy call, then never."""

    async def sleep(seconds: float) -> None:
        if strength.calls:
            await never_wake(seconds)
        await clock.sleep(seconds)
        await asyncio.sleep(0)

    return sleep


@pytest.mark.anyio
async def test_create_app_runs_the_strength_part(settings, store):
    clock = history_clock()
    source = FakeHistorySource(clock=clock)
    strength = FakeStrengthSource(clock=clock)
    app = create_app(
        history_settings(settings, history_backfill_days=1),
        store,
        today=lambda: TODAY,
        now=clock.now,
        history_source=source,
        strength_source=strength,
        history_sleep=strength_sleep_for(strength, clock),
    )
    async with running_client(app):
        await wait_for(lambda: strength.calls)
    assert [name for name, _ in strength.calls] == ["get-workout-events"]
    assert strength.calls[0][1]["since"] == "2026-10-05T22:00:00Z"  # local midnight of the first backfill day (10-06)
    assert source.calls  # the Garmin part ran first


@pytest.mark.anyio
async def test_create_app_runs_the_predictions_part(settings, store):
    clock = history_clock()
    source = FakeHistorySource(clock=clock)

    async def sleep(seconds: float) -> None:
        if source.prediction_ranges:
            await never_wake(seconds)
        await clock.sleep(seconds)
        await asyncio.sleep(0)

    app = create_app(
        history_settings(settings, history_backfill_days=1),
        store,
        today=lambda: TODAY,
        now=clock.now,
        history_source=source,
        history_sleep=sleep,
    )
    async with running_client(app):
        await wait_for(lambda: source.prediction_ranges)
    assert [n for n, _ in source.calls] == ["list_activities", "get_recovery_snapshot", "get_race_predictions"]
    assert source.prediction_ranges == [(date(2026, 10, 6), date(2026, 10, 6))]


def rows_of(store):
    return store.history_rows(None, date(2030, 1, 1))


# ----------------------------------------------------------------- weather


def weather_app(settings: Settings, store: Store, handler, **overrides):
    home = Settings(**{**settings.__dict__, **overrides})
    return create_app(
        home, store, today=lambda: TODAY, now=lambda: WEATHER_T0, weather_transport=httpx2.MockTransport(handler)
    )


@pytest.mark.anyio
async def test_weather_forecast_over_http(settings, store):
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx2.Response(503)
        return httpx2.Response(200, json=open_meteo_payload())

    app = weather_app(settings, store, handler, home_latitude=50.09, home_longitude=14.42)
    async with running_client(app) as client:
        # The SDK prefixes every ToolError text with "Error executing tool ...", so the texts are checked with `in`.
        failed = await call_tool(client, "get_weather_forecast", {})
        assert failed["isError"] is True
        assert "Weather service answered HTTP 503." in failed["content"][0]["text"]

        # The service keeps running: another tool and /healthz still answer.
        assert (await call_tool(client, "get_coach_context", {}, id_=3))["isError"] is False
        assert (await client.get("/healthz")).status_code == 200

        ok = await call_tool(client, "get_weather_forecast", {}, id_=4)
        assert ok["isError"] is False
        data = payload(ok)
        assert data["location"] == {"latitude": 50.09, "longitude": 14.42, "source": "home"}
        assert len(data["days"]) == 7 and data["days"][0]["sunrise"] == "07:12"
        assert data["best_outdoor_windows"][0]["start"] == "07:12"
        assert len(seen) == 2

        # The schema rejects an out-of-range argument before any request.
        bad = await call_tool(client, "get_weather_forecast", {"latitude": 91, "longitude": 0}, id_=5)
        assert bad["isError"] is True
        assert len(seen) == 2
        for arguments in ({"latitude": True, "longitude": 0}, {"latitude": 48.2, "longitude": "16.4"}):
            wrong = await call_tool(client, "get_weather_forecast", arguments, id_=8)
            assert wrong["isError"] is True
        assert len(seen) == 2

        away = await call_tool(client, "get_weather_forecast", {"latitude": 48.2082, "longitude": 16.3738}, id_=6)
        assert away["isError"] is False and payload(away)["location"]["source"] == "arguments"
        assert seen[2].url.params["latitude"] == "48.21" and seen[2].url.params["longitude"] == "16.37"

        whole = await call_tool(client, "get_weather_forecast", {"latitude": 48, "longitude": 16}, id_=9)  # JSON ints
        assert whole["isError"] is False and payload(whole)["location"]["latitude"] == 48.0

    app = weather_app(settings, store, handler)  # no home location
    async with running_client(app) as client:
        none = await call_tool(client, "get_weather_forecast", {}, id_=7)
        assert none["isError"] is True
        assert (
            "No home location configured: set COACH_HOME_LATITUDE and COACH_HOME_LONGITUDE in .env, "
            "or give latitude and longitude."
        ) in none["content"][0]["text"]
        assert len(seen) == 4


@pytest.mark.anyio
async def test_weather_log_has_no_coordinates(settings, store, caplog, restore_logging):
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.params["latitude"] == "47.07":
            raise httpx2.ConnectError("x")
        return httpx2.Response(200, json=open_meteo_payload())

    for name in ("httpx2", "httpcore2", "mcp"):
        logging.getLogger(name).setLevel(logging.NOTSET)
    configure_logging("INFO")
    caplog.set_level(logging.DEBUG)
    app = weather_app(settings, store, handler)
    async with running_client(app) as client:
        ok = await call_tool(client, "get_weather_forecast", {"latitude": 48.2082, "longitude": 16.3738})
        assert ok["isError"] is False
        failed = await call_tool(client, "get_weather_forecast", {"latitude": 47.0712, "longitude": 15.4395}, id_=3)
        assert failed["isError"] is True
    assert "tool=get_weather_forecast status=ok" in caplog.text
    assert "weather status=error reason=unreachable" in caplog.text
    for secret in ("48.21", "16.37", "48.2082", "47.07", "15.44"):
        assert secret not in caplog.text, secret
