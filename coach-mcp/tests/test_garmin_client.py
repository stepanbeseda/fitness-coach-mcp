from __future__ import annotations

import json
import logging
from datetime import date

import anyio
import httpx2
import pytest
from fakes import fake_garmin_app, fake_garmin_running

from coach_mcp import garmin as garmin_module
from coach_mcp.config import Settings
from coach_mcp.garmin import ALLOWED_TOOLS, GarminReader
from garmin_payloads import GREEN_SCHEDULED, GREEN_SNAPSHOT, GREEN_TREND

D = date(2026, 10, 7)
TOKEN = "garmin-backend-token-0123456789abcdef"
NOT_LOGGED_IN = "Garmin is not logged in. On the server run scripts/garmin-login.sh to sign in."


def reader_settings(tmp_path, token: str | None = TOKEN) -> Settings:
    token_file = tmp_path / "garmin_backend_token"
    if token is not None:
        token_file.write_text(token + "\n", encoding="utf-8")
    return Settings(garmin_url="http://garmin-mcp:8000/mcp", garmin_token_file=token_file)


def reader_for(app, tmp_path, token: str | None = TOKEN) -> GarminReader:
    return GarminReader(reader_settings(tmp_path, token), transport=httpx2.ASGITransport(app=app))


@pytest.mark.anyio
async def test_fetch_calls_the_three_read_tools_with_dates_and_token(tmp_path):
    app, calls = fake_garmin_app(TOKEN)
    async with fake_garmin_running(app):
        data = await reader_for(app, tmp_path).fetch(D)
    assert data.auth == "ok" and data.auth_problem is False and data.credentials_missing is False
    assert data.calls == {name: "ok" for name in ALLOWED_TOOLS}
    assert calls == [
        ("get_recovery_snapshot", {"date": "2026-10-07"}),
        ("get_hrv_trend", {"days": 7, "end_date": "2026-10-07"}),
        ("list_scheduled_workouts", {"start_date": "2026-10-06", "end_date": "2026-10-07"}),
    ]
    assert data.snapshot == GREEN_SNAPSHOT
    assert data.hrv_trend == GREEN_TREND
    assert data.scheduled == GREEN_SCHEDULED


@pytest.mark.anyio
async def test_auth_missing_still_reads_and_marks_auth_errors(tmp_path):
    errors = dict.fromkeys(ALLOWED_TOOLS, NOT_LOGGED_IN)
    app, calls = fake_garmin_app(TOKEN, auth="missing", errors=errors)
    async with fake_garmin_running(app):
        data = await reader_for(app, tmp_path).fetch(D)
    assert data.auth == "missing" and data.auth_problem is True
    assert data.calls == dict.fromkeys(ALLOWED_TOOLS, "auth_error")
    assert (data.snapshot, data.hrv_trend, data.scheduled) == (None, None, None)
    assert [name for name, _ in calls] == list(ALLOWED_TOOLS)  # the calls are made even without a login


@pytest.mark.anyio
async def test_wrong_token_marks_calls_error(tmp_path):
    app, calls = fake_garmin_app(TOKEN)
    async with fake_garmin_running(app):
        data = await reader_for(app, tmp_path, token="a-different-token-0123456789abcdef").fetch(D)
    assert data.auth == "ok"  # /healthz needs no token
    assert data.calls == dict.fromkeys(ALLOWED_TOOLS, "error")
    assert data.auth_problem is False
    assert calls == []


@pytest.mark.anyio
async def test_unreachable_backend_returns_no_data(tmp_path):
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down")

    reader = GarminReader(reader_settings(tmp_path), transport=httpx2.MockTransport(handler))
    data = await reader.fetch(D)
    assert data.auth is None
    assert data.calls == dict.fromkeys(ALLOWED_TOOLS, "error")
    assert (data.snapshot, data.hrv_trend, data.scheduled) == (None, None, None)


@pytest.mark.anyio
@pytest.mark.parametrize("token", [None, "short"])
async def test_missing_token_file_returns_no_data_without_requests(tmp_path, token, caplog):
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={"garmin_auth": "ok"})

    caplog.set_level(logging.DEBUG)
    reader = GarminReader(reader_settings(tmp_path, token), transport=httpx2.MockTransport(handler))
    data = await reader.fetch(D)
    assert requests == []
    assert data.auth is None and data.calls == dict.fromkeys(ALLOWED_TOOLS, "error")
    assert data.credentials_missing is True
    assert "garmin status=error kind=no_credentials" in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("body", [{"garmin_auth": "weird"}, {}, ["x"]])
async def test_unknown_healthz_answer_gives_no_session(tmp_path, body):
    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.url.path)
        return httpx2.Response(200, json=body)

    data = await GarminReader(reader_settings(tmp_path), transport=httpx2.MockTransport(handler)).fetch(D)
    assert requests == ["/healthz"]
    assert data.auth is None


@pytest.mark.anyio
async def test_healthz_error_status_gives_no_session(tmp_path):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, json={"garmin_auth": "ok"})

    data = await GarminReader(reader_settings(tmp_path), transport=httpx2.MockTransport(handler)).fetch(D)
    assert data.auth is None


@pytest.mark.anyio
async def test_non_auth_tool_error_marks_only_that_call(tmp_path, caplog):
    app, _calls = fake_garmin_app(TOKEN, errors={"get_hrv_trend": "SECRET-ERROR-TEXT"})
    caplog.set_level(logging.DEBUG)
    async with fake_garmin_running(app):
        data = await reader_for(app, tmp_path).fetch(D)
    assert data.calls == {"get_recovery_snapshot": "ok", "get_hrv_trend": "error", "list_scheduled_workouts": "ok"}
    assert data.hrv_trend is None and data.snapshot == GREEN_SNAPSHOT and data.scheduled == GREEN_SCHEDULED
    assert data.auth_problem is False
    assert "garmin status=partial" in caplog.text


@pytest.mark.anyio
async def test_only_allowlisted_tools_can_be_called():
    class Boom:
        async def call_tool(self, name, args):
            raise AssertionError("must not be reached")

    with pytest.raises(ValueError):
        await garmin_module._call(Boom(), "create_workout", {})  # type: ignore[arg-type]
    assert ALLOWED_TOOLS == ("get_recovery_snapshot", "get_hrv_trend", "list_scheduled_workouts")


@pytest.mark.anyio
async def test_create_workout_is_never_called_by_a_fetch(tmp_path):
    app, calls = fake_garmin_app(TOKEN)
    async with fake_garmin_running(app):
        await reader_for(app, tmp_path).fetch(D)
    assert "create_workout" not in [name for name, _ in calls]


@pytest.mark.anyio
async def test_non_json_and_non_dict_results_are_errors(tmp_path):
    class Result:
        def __init__(self, text, is_error=False):
            import mcp.types

            self.content = [mcp.types.TextContent(type="text", text=text)]
            self.is_error = is_error

    class Client:
        def __init__(self, outputs):
            self.outputs = outputs

        async def call_tool(self, name, args):
            output = self.outputs.pop(0)
            if isinstance(output, Exception):
                raise output
            return output

    outputs = [
        Result("not json"),
        Result(json.dumps([1, 2])),
        Result("boom", is_error=True),
        Result(NOT_LOGGED_IN, is_error=True),
        RuntimeError("transport"),
        Result(json.dumps({"ok": 1})),
    ]
    client = Client(outputs)
    got = [await garmin_module._call(client, "get_hrv_trend", {}) for _ in range(6)]  # type: ignore[arg-type]
    assert got == [
        ("error", None),
        ("error", None),
        ("error", None),
        ("auth_error", None),
        ("error", None),
        ("ok", {"ok": 1}),
    ]


@pytest.mark.anyio
async def test_a_hung_tool_is_bounded_by_the_overall_timeout(tmp_path, monkeypatch):
    real = anyio.fail_after
    requested: list[float] = []

    def short_deadline(seconds: float):
        requested.append(seconds)
        return real(0.3)

    monkeypatch.setattr(garmin_module.anyio, "fail_after", short_deadline)
    app, _ = fake_garmin_app(TOKEN, hang=frozenset({"get_hrv_trend"}))
    async with fake_garmin_running(app):
        data = await reader_for(app, tmp_path).fetch(D)
    assert requested[0] == 180  # the reader's own overall deadline for the whole MCP session (the SDK adds more)
    assert data.calls == {"get_recovery_snapshot": "ok", "get_hrv_trend": "error", "list_scheduled_workouts": "error"}
    assert data.snapshot == GREEN_SNAPSHOT
