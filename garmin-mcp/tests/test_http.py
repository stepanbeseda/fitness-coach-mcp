import copy
import json
from datetime import timedelta

import pytest

import fixtures as fx
from conftest import BACKEND_TOKEN, MCP_HEADERS, auth_headers, running_client
from garmin_coach_mcp.formatting import today
from test_workouts import STRENGTH_SPEC

EXPECTED_TOOLS = {
    "get_recovery_snapshot",
    "get_training_readiness",
    "get_hrv_trend",
    "get_sleep",
    "get_body_battery",
    "get_daily_summary",
    "get_training_status",
    "get_device_status",
    "list_activities",
    "get_activity",
    "list_workouts",
    "get_workout",
    "find_strength_exercise",
    "create_workout",
    "schedule_workout",
    "list_scheduled_workouts",
    "unschedule_workout",
    "get_performance_metrics",
    "get_race_predictions",
    "get_running_tolerance",
    "get_personal_records",
    "get_gear",
    "get_body_composition",
    "get_nutrition",
    "log_weight",
}
WRITE_TOOLS = {"create_workout", "schedule_workout", "unschedule_workout", "log_weight"}
LOCAL_TOOLS = {"find_strength_exercise"}  # answer from the bundled catalogue: no Garmin call, not open world

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
async def test_healthz_without_auth_reports_token_state(app, tokens_dir):
    async with running_client(app) as client:
        r = await client.get("/healthz")
        assert r.status_code == 200 and r.json() == {"status": "ok", "garmin_auth": "ok"}
        (tokens_dir / "garmin_tokens.json").unlink()
        assert (await client.get("/healthz")).json()["garmin_auth"] == "missing"


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
async def test_initialize_list_and_call(app, fake):
    async with running_client(app) as client:
        r = await client.post("/mcp", headers=auth_headers(protocol=None), json=INIT)
        assert r.status_code == 200, r.text
        init = r.json()["result"]
        assert init["serverInfo"]["name"] == "garmin-coach"
        assert "get_recovery_snapshot" in init["instructions"]
        assert "find_strength_exercise" in init["instructions"]

        r = await client.post("/mcp", headers=auth_headers(), json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert r.status_code == 202

        tools = (await rpc(client, "tools/list")).json()["result"]["tools"]
        by_name = {t["name"]: t for t in tools}
        assert set(by_name) == EXPECTED_TOOLS
        for name, tool in by_name.items():
            assert tool["title"] and tool["description"]
            ann = tool["annotations"]
            assert ann["openWorldHint"] is (name not in LOCAL_TOOLS)
            assert ann["readOnlyHint"] is (name not in WRITE_TOOLS)
        assert by_name["unschedule_workout"]["annotations"]["destructiveHint"] is True
        assert by_name["create_workout"]["annotations"]["destructiveHint"] is False
        assert by_name["log_weight"]["annotations"]["destructiveHint"] is False
        assert "only after the athlete confirmed" in by_name["log_weight"]["description"]
        schema = json.dumps(by_name["create_workout"]["inputSchema"])
        assert "heart_rate_zone" in schema and "lap_button" in schema and "repeat" in schema
        assert "exercise" in schema and "weight_kg" in schema
        assert len(json.dumps(tools)) < 40_000

        r = await rpc(client, "tools/call", {"name": "get_recovery_snapshot", "arguments": {"date": "2026-09-30"}}, id_=3)
        result = r.json()["result"]
        assert result["isError"] is False
        payload = json.loads(result["content"][0]["text"])
        assert payload["readiness"]["score"] == 58 and payload["hrv"]["last_night_avg_ms"] == 48


@pytest.mark.anyio
async def test_device_status_over_http(app, fake):
    async with running_client(app) as client:
        r = await rpc(client, "tools/call", {"name": "get_device_status", "arguments": {}}, id_=9)
        result = r.json()["result"]
        assert result["isError"] is False, result
        payload = json.loads(result["content"][0]["text"])
        assert payload["last_upload"]["time_utc"] == "2026-09-30T04:58:12Z"
        assert payload["primary_device"]["registered_devices"] == 1
    # the two library calls run concurrently, so their order is not fixed
    assert sorted(name for name, _ in fake.calls if name.startswith("get_device")) == [
        "get_device_last_used",
        "get_devices",
    ]


@pytest.mark.anyio
async def test_performance_tools_over_http(app, fake):
    async with running_client(app) as client:
        r = await rpc(client, "tools/call", {"name": "get_performance_metrics", "arguments": {"weeks": 8}}, id_=10)
        result = r.json()["result"]
        assert result["isError"] is False, result
        payload = json.loads(result["content"][0]["text"])
        assert payload["window"]["weeks"] == 8
        assert payload["race_predictions"]["5k"]["time"] == "21:30"

        r = await rpc(client, "tools/call", {"name": "get_performance_metrics", "arguments": {"weeks": 1}}, id_=11)
        assert r.json()["result"]["isError"] is True

        r = await rpc(client, "tools/call", {"name": "get_gear", "arguments": {}}, id_=12)
        result = r.json()["result"]
        assert result["isError"] is False, result
        assert json.loads(result["content"][0]["text"])["count"] == 3


@pytest.mark.anyio
async def test_race_predictions_over_http(app, fake):
    fake.responses["get_race_predictions"] = fx.RACE_PREDICTIONS_DAILY

    async def call(client, arguments, id_):
        r = await rpc(client, "tools/call", {"name": "get_race_predictions", "arguments": arguments}, id_=id_)
        return r.json()["result"]

    async with running_client(app) as client:
        result = await call(client, {"start_date": "2026-09-28", "end_date": "2026-09-30"}, 40)
        assert result["isError"] is False, result
        payload = json.loads(result["content"][0]["text"])
        assert payload["count"] == 3 and payload["start"] == "2026-09-28"
        assert ("get_race_predictions", ("2026-09-28", "2026-09-30", "daily")) in fake.calls

        edge = await call(client, {"start_date": "2025-10-01", "end_date": "2026-10-01"}, 41)  # 366 days
        assert edge["isError"] is False, edge
        assert (await call(client, {"start_date": "2025-09-30", "end_date": "2026-10-01"}, 42))["isError"] is True


@pytest.mark.anyio
async def test_running_tolerance_and_activity_signals_over_http(app, fake):
    async def call(client, name, arguments, id_):
        r = await rpc(client, "tools/call", {"name": name, "arguments": arguments}, id_=id_)
        return r.json()["result"]

    async with running_client(app) as client:
        result = await call(client, "get_running_tolerance", {"weeks": 4, "end_date": "2026-09-30"}, 30)
        assert result["isError"] is False, result
        assert json.loads(result["content"][0]["text"])["count"] == 3
        assert ("get_running_tolerance", ("2026-09-03", "2026-09-30", "weekly")) in fake.calls

        assert (await call(client, "get_running_tolerance", {"weeks": 27}, 31))["isError"] is True
        edge = await call(client, "get_running_tolerance", {"weeks": 26, "end_date": "2026-09-30"}, 32)
        assert edge["isError"] is False

        fake.responses["get_activity"] = fx.ACTIVITY_STRENGTH
        fake.responses["get_activity_exercise_sets"] = fx.EXERCISE_SETS
        result = await call(client, "get_activity", {"activity_id": 20003}, 33)
        assert result["isError"] is False, result
        payload = json.loads(result["content"][0]["text"])
        assert payload["exercise_sets"]["count"] == 4 and payload["weather"]["temp_c"] == 15.0
        assert payload["active_sets"] == 4


@pytest.mark.anyio
async def test_body_tools_over_http(app, fake):
    async with running_client(app) as client:
        args = {"start_date": "2026-09-01", "end_date": "2026-09-30"}
        r = await rpc(client, "tools/call", {"name": "get_body_composition", "arguments": args}, id_=20)
        result = r.json()["result"]
        assert result["isError"] is False, result
        payload = json.loads(result["content"][0]["text"])
        assert payload["count"] == 5 and payload["summary"]["change_kg"] == -1.3

        r = await rpc(client, "tools/call", {"name": "get_nutrition", "arguments": {"date": "2026-09-30"}}, id_=21)
        result = r.json()["result"]
        assert result["isError"] is False, result
        payload = json.loads(result["content"][0]["text"])
        assert payload["calories"]["consumed"] == 2150 and payload["hydration"]["intake_ml"] == 1750

        r = await rpc(client, "tools/call", {"name": "log_weight", "arguments": {"weight_kg": 19.5}}, id_=22)
        assert r.json()["result"]["isError"] is True
        assert fake.weigh_ins == []
        r = await rpc(client, "tools/call", {"name": "log_weight", "arguments": {"weight_kg": 77.8, "date": "2026-09-30"}}, id_=23)
        result = r.json()["result"]
        assert result["isError"] is False, result
        assert json.loads(result["content"][0]["text"])["entry"]["weight_kg"] == 77.8
        assert fake.weigh_ins[0][:2] == (77.8, "kg")


@pytest.mark.anyio
async def test_tool_error_is_reported_to_model(app, tokens_dir):
    (tokens_dir / "garmin_tokens.json").unlink()
    async with running_client(app) as client:
        r = await rpc(client, "tools/call", {"name": "get_sleep", "arguments": {}})
        result = r.json()["result"]
        assert result["isError"] is True
        assert "scripts/garmin-login.sh" in result["content"][0]["text"]


@pytest.mark.anyio
async def test_create_workout_via_mcp_validates_schema(app, fake):
    async with running_client(app) as client:
        bad = {"name": "x", "sport": "running", "steps": [{"type": "sprint", "end": {"type": "time", "seconds": 60}}]}
        result = (await rpc(client, "tools/call", {"name": "create_workout", "arguments": bad})).json()["result"]
        assert result["isError"] is True
        assert fake.uploaded == []

        good = {"name": "Easy", "sport": "running",
                "steps": [{"type": "interval", "end": {"type": "time", "seconds": 1800},
                           "target": {"type": "heart_rate_zone", "zone": 2}}]}
        result = (await rpc(client, "tools/call", {"name": "create_workout", "arguments": good}, id_=4)).json()["result"]
        assert result["isError"] is False, result
        assert json.loads(result["content"][0]["text"])["workout_id"] == 424242
        assert fake.uploaded[0]["workoutSegments"][0]["workoutSteps"][0]["zoneNumber"] == 2


async def _call(client, name, arguments, id_=50):
    r = await rpc(client, "tools/call", {"name": name, "arguments": arguments}, id_=id_)
    return r.json()["result"]


@pytest.mark.anyio
async def test_find_strength_exercise_over_http(app, fake):
    async with running_client(app) as client:
        result = await _call(client, "find_strength_exercise", {"query": "Bench Press (Barbell)", "limit": 2})
        assert result["isError"] is False, result
        payload = json.loads(result["content"][0]["text"])
        assert payload["count"] == 2 and payload["matches"][0]["name"] == "BARBELL_BENCH_PRESS"

        default = await _call(client, "find_strength_exercise", {"query": "bench press"})
        assert json.loads(default["content"][0]["text"])["count"] == 5  # the default limit
        many = await _call(client, "find_strength_exercise", {"query": "plank", "limit": 20})
        assert json.loads(many["content"][0]["text"])["count"] == 20

        for arguments in ({"query": "plank", "limit": 21}, {"query": "plank", "limit": 0},
                          {"query": "p"}, {"query": "p" * 81}):
            assert (await _call(client, "find_strength_exercise", arguments))["isError"] is True, arguments
        assert (await _call(client, "find_strength_exercise", {"query": "p" * 80}))["isError"] is False
        short = await _call(client, "find_strength_exercise", {"query": "ab"})  # the minimum length of 2
        assert short["isError"] is False and json.loads(short["content"][0]["text"])["query"] == "ab"
    assert fake.calls == []  # a catalogue lookup makes no Garmin call


@pytest.mark.anyio
async def test_create_strength_workout_via_mcp(app, fake):
    day = (today("Europe/Prague") + timedelta(days=2)).isoformat()
    async with running_client(app) as client:
        result = await _call(client, "create_workout", {**STRENGTH_SPEC, "schedule_date": day})
        assert result["isError"] is False, result
        assert fake.uploaded[0] == fx.STRENGTH_UPLOAD
        payload = json.loads(result["content"][0]["text"])
        assert payload["unmapped"] == ["Hevy: Zercher Carry"] and payload["scheduled"]["date"] == day

        fake.responses["get_workout_by_id"] = fx.STRENGTH_WORKOUT_DETAIL
        detail = json.loads((await _call(client, "get_workout", {"workout_id": 9002}))["content"][0]["text"])
        assert detail["steps"][0]["sets"] == 3 and detail["steps"][3]["seconds"] == 45

        fake.responses["get_workouts"] = [fx.STRENGTH_WORKOUT_ROW]
        listed = json.loads((await _call(client, "list_workouts", {}))["content"][0]["text"])
        assert listed["workouts"][0]["sport"] == "strength_training"

        with_target = copy.deepcopy(STRENGTH_SPEC)
        with_target["steps"][0]["target"] = {"type": "heart_rate_zone", "zone": 2}
        unknown = copy.deepcopy(STRENGTH_SPEC)
        unknown["steps"][0]["name"] = "BARBELL_BENCH_PRES"
        interval = copy.deepcopy(STRENGTH_SPEC)
        interval["steps"].append({"type": "interval", "end": {"type": "time", "seconds": 60}})
        for bad, message in ((with_target, "takes no pace, power"), (unknown, "unknown Garmin exercise"),
                             (interval, "only exercise items")):
            result = await _call(client, "create_workout", bad)
            assert result["isError"] is True and message in result["content"][0]["text"], result
    assert len(fake.uploaded) == 1


@pytest.mark.anyio
async def test_unknown_host_header_is_accepted(app):
    """Requests arrive via the gateway with Host: garmin-mcp:8000; no 421."""
    async with running_client(app) as client:
        r = await client.post("/mcp", headers={**auth_headers(), "Host": "garmin-mcp:8000"}, json=INIT)
        assert r.status_code == 200
