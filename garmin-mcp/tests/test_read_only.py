"""Read-only token of coach-mcp: reads work, every write tool is refused before it runs (PR 12).

Dates are computed from now (no fixed dates in this file).
"""

import datetime as dt
import json
import logging
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from mcp.types import ToolAnnotations

from conftest import BACKEND_TOKEN, READONLY_TOKEN, auth_headers, running_client
from garmin_coach_mcp.access import READ_ONLY_REFUSAL, ReadOnlyGuard, request_role
from garmin_coach_mcp.auth import ROLE_FULL, ROLE_KEY, ROLE_READ_ONLY
from garmin_coach_mcp.server import asgi_app, build_server, create_app
from test_http import EXPECTED_TOOLS, INIT, LOCAL_TOOLS, WRITE_TOOLS, rpc

READ_TOOLS = EXPECTED_TOOLS - WRITE_TOOLS
READ_ARGS = {
    "get_activity": {"activity_id": 20001},
    "get_workout": {"workout_id": 9001},
    "find_strength_exercise": {"query": "bench press"},
}
NEXT_WEEK = (dt.datetime.now(ZoneInfo("Europe/Prague")).date() + dt.timedelta(days=7)).isoformat()
WRITE_ARGS = {
    "create_workout": {
        "name": "Easy",
        "sport": "running",
        "steps": [{"type": "interval", "end": {"type": "time", "seconds": 1800},
                   "target": {"type": "heart_rate_zone", "zone": 2}}],
    },
    "schedule_workout": {"workout_id": 9001, "date": NEXT_WEEK},
    "unschedule_workout": {"schedule_id": 31337},
    "log_weight": {"weight_kg": 77.8},
}


def ro_headers():
    return auth_headers(READONLY_TOKEN)


def result_of(response):
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_arg_tables_cover_the_tool_split():
    assert set(WRITE_ARGS) == WRITE_TOOLS
    assert set(READ_ARGS) <= READ_TOOLS


@pytest.mark.anyio
async def test_read_only_token_lists_every_tool(app):
    async with running_client(app) as client:
        tools = result_of(await rpc(client, "tools/list", headers=ro_headers()))["tools"]
        assert {t["name"] for t in tools} == EXPECTED_TOOLS


@pytest.mark.anyio
@pytest.mark.parametrize("name", sorted(READ_TOOLS))
async def test_read_only_token_calls_every_read_tool(app, fake, name):
    async with running_client(app) as client:
        r = await rpc(client, "tools/call", {"name": name, "arguments": READ_ARGS.get(name, {})}, headers=ro_headers())
        result = result_of(r)
        assert result["isError"] is False, result
        assert bool(fake.calls) is (name not in LOCAL_TOOLS), "the tool body did not reach the Garmin client"


@pytest.mark.anyio
@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
async def test_read_only_token_is_refused_every_write_tool_before_it_runs(app, fake, name):
    async with running_client(app) as client:
        r = await rpc(client, "tools/call", {"name": name, "arguments": WRITE_ARGS[name]}, headers=ro_headers())
        result = result_of(r)
        assert result["isError"] is True
        assert result["content"][0]["text"] == READ_ONLY_REFUSAL
    assert fake.calls == []
    assert fake.uploaded == [] and fake.scheduled == [] and fake.unscheduled == [] and fake.weigh_ins == []


@pytest.mark.anyio
@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
async def test_gateway_token_calls_every_write_tool(app, fake, name):
    async with running_client(app) as client:
        r = await rpc(client, "tools/call", {"name": name, "arguments": WRITE_ARGS[name]}, headers=auth_headers(BACKEND_TOKEN))
        result = result_of(r)
        assert result["isError"] is False, result
        assert result["content"][0]["text"] != READ_ONLY_REFUSAL
    assert fake.calls


@pytest.mark.anyio
async def test_tool_missing_from_the_map_counts_as_write(settings, service):
    ran = []
    server = build_server(service, settings)

    async def unmapped_probe() -> str:
        ran.append(True)
        return json.dumps({"ran": True})

    # Registered past the tool() helper: annotated read-only, but absent from the guard's map.
    server.add_tool(unmapped_probe, name="unmapped_probe", annotations=ToolAnnotations(read_only_hint=True))
    app = asgi_app(server, settings)
    async with running_client(app) as client:
        for name in ("unmapped_probe", "no_such_tool"):
            result = result_of(await rpc(client, "tools/call", {"name": name, "arguments": {}}, headers=ro_headers()))
            assert result["isError"] is True and result["content"][0]["text"] == READ_ONLY_REFUSAL
        assert ran == []
        result = result_of(await rpc(client, "tools/call", {"name": "unmapped_probe", "arguments": {}}))
        assert result["isError"] is False and ran == [True]


def test_guard_decision_table():
    guard = ReadOnlyGuard()
    guard.register("r", ToolAnnotations(read_only_hint=True))
    guard.register("w", ToolAnnotations(read_only_hint=False))
    guard.register("n", ToolAnnotations())  # no hint: a write
    guard.register("x", None)
    for name in ("r", "w", "n", "x", "unknown"):
        assert guard.allows(ROLE_FULL, name) is True
    assert guard.allows(ROLE_READ_ONLY, "r") is True
    assert guard.allows(None, "r") is True
    for role in (ROLE_READ_ONLY, None):
        for name in ("w", "n", "x", "unknown"):
            assert guard.allows(role, name) is False


def test_request_role_reads_only_known_roles_from_the_scope():
    def ctx(request):
        return SimpleNamespace(request=request)

    assert request_role(ctx(SimpleNamespace(scope={ROLE_KEY: ROLE_FULL}))) == ROLE_FULL
    assert request_role(ctx(SimpleNamespace(scope={ROLE_KEY: ROLE_READ_ONLY}))) == ROLE_READ_ONLY
    assert request_role(ctx(SimpleNamespace(scope={ROLE_KEY: "admin"}))) is None
    assert request_role(ctx(SimpleNamespace(scope={}))) is None
    assert request_role(ctx(SimpleNamespace(scope="not a dict"))) is None
    assert request_role(ctx(None)) is None
    assert request_role(SimpleNamespace()) is None


@pytest.mark.anyio
async def test_missing_or_short_read_only_token_is_not_accepted(settings, service, readonly_file, caplog):
    readonly_file.unlink()
    caplog.set_level(logging.WARNING, logger="garmin_coach_mcp.auth")
    app = create_app(settings, service)
    assert "read-only token file missing or too short" in caplog.text
    async with running_client(app) as client:
        r = await client.post("/mcp", headers=ro_headers(), json=INIT)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        assert (await client.post("/mcp", headers=auth_headers(), json=INIT)).status_code == 200
        readonly_file.write_text("short\n", encoding="utf-8")
        assert (await client.post("/mcp", headers=auth_headers("short"), json=INIT)).status_code == 401
        assert (await client.post("/mcp", headers=auth_headers(), json=INIT)).status_code == 200


@pytest.mark.anyio
async def test_read_only_token_appearing_later_is_picked_up(settings, service, readonly_file):
    readonly_file.unlink()
    app = create_app(settings, service)
    async with running_client(app) as client:
        assert (await client.post("/mcp", headers=ro_headers(), json=INIT)).status_code == 401
        readonly_file.write_text(READONLY_TOKEN + "\n", encoding="utf-8")
        assert (await client.post("/mcp", headers=ro_headers(), json=INIT)).status_code == 200


@pytest.mark.anyio
async def test_gateway_token_missing_fails_closed_for_the_read_only_token_too(app, bearer_file):
    async with running_client(app) as client:
        bearer_file.unlink()
        assert (await client.post("/mcp", headers=ro_headers(), json=INIT)).status_code == 503


@pytest.mark.anyio
async def test_equal_tokens_count_as_read_only(app, fake, readonly_file):
    readonly_file.write_text(BACKEND_TOKEN + "\n", encoding="utf-8")
    async with running_client(app) as client:
        args = {"name": "unschedule_workout", "arguments": WRITE_ARGS["unschedule_workout"]}
        result = result_of(await rpc(client, "tools/call", args, headers=auth_headers(BACKEND_TOKEN)))
        assert result["isError"] is True and result["content"][0]["text"] == READ_ONLY_REFUSAL
        read = {"name": "get_sleep", "arguments": {}}
        assert result_of(await rpc(client, "tools/call", read, headers=auth_headers(BACKEND_TOKEN)))["isError"] is False
    assert fake.unscheduled == []


@pytest.mark.anyio
async def test_refusal_is_logged_without_arguments(app, caplog):
    caplog.set_level(logging.INFO)
    async with running_client(app) as client:
        await rpc(client, "tools/call", {"name": "unschedule_workout", "arguments": {"schedule_id": 987654}},
                  headers=ro_headers())
        await rpc(client, "tools/call", {"name": "evil\nname", "arguments": {}}, headers=ro_headers())
    refused = [r.getMessage() for r in caplog.records if r.name == "garmin_coach_mcp.access"]
    assert refused == [
        "tool=unschedule_workout status=refused reason=read_only",
        "tool=<unregistered> status=refused reason=read_only",
    ]
    assert "987654" not in caplog.text and "evil" not in caplog.text
