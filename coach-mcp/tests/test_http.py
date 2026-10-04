import json
import sqlite3

import pytest

from conftest import BACKEND_TOKEN, MCP_HEADERS, TODAY, auth_headers, call_tool, payload, running_client
from coach_mcp.server import create_app
from coach_mcp.store import Store

EXPECTED_TOOLS = {
    "get_coach_context",
    "get_plan",
    "set_goal",
    "set_constraint",
    "save_plan",
    "update_session",
    "add_note",
}
WRITE_TOOLS = {"set_goal", "set_constraint", "save_plan", "update_session", "add_note"}

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
        assert r.json() == {"status": "ok", "db": "ok", "schema_version": 1}

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

        r = await client.post("/mcp", headers=auth_headers(), json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert r.status_code == 202

        tools = (await rpc(client, "tools/list")).json()["result"]["tools"]
        by_name = {t["name"]: t for t in tools}
        assert set(by_name) == EXPECTED_TOOLS
        for name, tool in by_name.items():
            assert tool["title"] and tool["description"]
            ann = tool["annotations"]
            assert ann["openWorldHint"] is False
            assert ann["readOnlyHint"] is (name not in WRITE_TOOLS)
            if name in WRITE_TOOLS:
                assert ann["destructiveHint"] is False
                assert "only after the athlete confirmed" in tool["description"].lower()
                assert ann["idempotentHint"] is (name == "save_plan")
        assert "distance_km" in json.dumps(by_name["save_plan"]["inputSchema"])
        status_schema = by_name["update_session"]["inputSchema"]["properties"]["status"]
        assert sorted(status_schema["enum"]) == ["done", "moved", "planned", "skipped"]
        assert len(json.dumps(tools)) < 40_000


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
