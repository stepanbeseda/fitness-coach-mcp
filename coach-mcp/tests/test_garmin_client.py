from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime

import anyio
import httpx2
import pytest
from fakes import RawText, fake_garmin_app, fake_garmin_running

from coach_mcp import garmin as garmin_module
from coach_mcp.config import Settings
from coach_mcp.garmin import (
    ALLOWED_TOOLS,
    BRIEFING_TOOLS,
    HISTORY_TOOLS,
    SYNC_TOOL,
    GarminReader,
    LastSync,
    parse_last_sync,
)
from garmin_payloads import (
    DEVICE_STATUS,
    GREEN_SCHEDULED,
    GREEN_SNAPSHOT,
    GREEN_TREND,
    activities_payload,
    activity,
    history_snapshot,
)

D = date(2026, 10, 7)
SYNC_0507 = LastSync(datetime(2026, 10, 7, 3, 7, tzinfo=UTC), datetime(2026, 10, 7, 5, 7))
TOKEN = "garmin-backend-token-0123456789abcdef"
NOT_LOGGED_IN = "Garmin is not logged in. On the server run scripts/garmin-login.sh to sign in."


def reader_settings(tmp_path, token: str | None = TOKEN) -> Settings:
    token_file = tmp_path / "garmin_readonly_token"
    if token is not None:
        token_file.write_text(token + "\n", encoding="utf-8")
    return Settings(garmin_url="http://garmin-mcp:8000/mcp", garmin_token_file=token_file)


def reader_for(app, tmp_path, token: str | None = TOKEN) -> GarminReader:
    return GarminReader(reader_settings(tmp_path, token), transport=httpx2.ASGITransport(app=app))


@pytest.mark.anyio
async def test_fetch_calls_the_four_read_tools_with_dates_and_token(tmp_path):
    app, calls = fake_garmin_app(TOKEN)
    async with fake_garmin_running(app):
        data = await reader_for(app, tmp_path).fetch(D)
    assert data.auth == "ok" and data.auth_problem is False and data.credentials_missing is False
    assert data.calls == {name: "ok" for name in BRIEFING_TOOLS}
    assert calls == [
        ("get_device_status", {}),  # first: an upload during the read then shows up at the next check
        ("get_recovery_snapshot", {"date": "2026-10-07"}),
        ("get_hrv_trend", {"days": 7, "end_date": "2026-10-07"}),
        ("list_scheduled_workouts", {"start_date": "2026-10-06", "end_date": "2026-10-07"}),
    ]
    assert data.snapshot == GREEN_SNAPSHOT
    assert data.hrv_trend == GREEN_TREND
    assert data.scheduled == GREEN_SCHEDULED
    assert data.device == DEVICE_STATUS
    assert data.last_sync == SYNC_0507


@pytest.mark.anyio
async def test_auth_missing_still_reads_and_marks_auth_errors(tmp_path):
    errors = dict.fromkeys(BRIEFING_TOOLS, NOT_LOGGED_IN)
    app, calls = fake_garmin_app(TOKEN, auth="missing", errors=errors)
    async with fake_garmin_running(app):
        data = await reader_for(app, tmp_path).fetch(D)
    assert data.auth == "missing" and data.auth_problem is True
    assert data.calls == dict.fromkeys(BRIEFING_TOOLS, "auth_error")
    assert (data.snapshot, data.hrv_trend, data.scheduled) == (None, None, None)
    assert [name for name, _ in calls] == list(BRIEFING_TOOLS)  # the calls are made even without a login


@pytest.mark.anyio
async def test_wrong_token_marks_calls_error(tmp_path):
    app, calls = fake_garmin_app(TOKEN)
    async with fake_garmin_running(app):
        data = await reader_for(app, tmp_path, token="a-different-token-0123456789abcdef").fetch(D)
    assert data.auth == "ok"  # /healthz needs no token
    assert data.calls == dict.fromkeys(BRIEFING_TOOLS, "error")
    assert data.auth_problem is False
    assert calls == []


@pytest.mark.anyio
async def test_unreachable_backend_returns_no_data(tmp_path):
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down")

    reader = GarminReader(reader_settings(tmp_path), transport=httpx2.MockTransport(handler))
    data = await reader.fetch(D)
    assert data.auth is None
    assert data.calls == dict.fromkeys(BRIEFING_TOOLS, "error")
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
    assert data.auth is None and data.calls == dict.fromkeys(BRIEFING_TOOLS, "error")
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
    assert data.calls == {
        "get_device_status": "ok",
        "get_recovery_snapshot": "ok",
        "get_hrv_trend": "error",
        "list_scheduled_workouts": "ok",
    }
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
    assert BRIEFING_TOOLS == ("get_device_status", "get_recovery_snapshot", "get_hrv_trend", "list_scheduled_workouts")
    assert SYNC_TOOL == "get_device_status" and SYNC_TOOL in ALLOWED_TOOLS
    assert ALLOWED_TOOLS == (*BRIEFING_TOOLS, "list_activities", "get_race_predictions")
    assert set(HISTORY_TOOLS) <= set(ALLOWED_TOOLS)
    assert HISTORY_TOOLS == ("get_recovery_snapshot", "list_activities", "get_race_predictions")


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
    assert data.calls == {
        "get_device_status": "ok",
        "get_recovery_snapshot": "ok",
        "get_hrv_trend": "error",
        "list_scheduled_workouts": "error",
    }
    assert data.snapshot == GREEN_SNAPSHOT


RATE_LIMIT_TEXT = "Garmin Connect rate limit (HTTP 429). Wait a few minutes and retry."
SNAPSHOT_ARGS = {"date": "2026-10-06"}
ACTIVITIES_ARGS = {"start_date": "2026-10-01", "end_date": "2026-10-06", "limit": 100}


@pytest.mark.anyio
async def test_fetch_never_calls_list_activities(tmp_path):
    app, calls = fake_garmin_app(TOKEN)
    async with fake_garmin_running(app):
        await reader_for(app, tmp_path).fetch(D)
    assert "list_activities" not in [name for name, _ in calls]


@pytest.mark.anyio
async def test_history_call_reads_snapshot_and_activities(tmp_path):
    snapshot = history_snapshot("2026-10-06")
    activities = activities_payload([activity("2026-10-05", "running", 40, 7.5, 80)])
    app, calls = fake_garmin_app(TOKEN, snapshot=snapshot, activities=activities)
    async with fake_garmin_running(app):
        reader = reader_for(app, tmp_path)
        got_snapshot = await reader.history_call("get_recovery_snapshot", dict(SNAPSHOT_ARGS))
        got_activities = await reader.history_call("list_activities", dict(ACTIVITIES_ARGS))
    assert got_snapshot == ("ok", snapshot)
    assert got_activities == ("ok", activities)
    assert calls == [("get_recovery_snapshot", SNAPSHOT_ARGS), ("list_activities", ACTIVITIES_ARGS)]


@pytest.mark.anyio
async def test_history_call_reads_race_predictions(tmp_path):
    from fakes import RACE_PREDICTIONS_PAYLOAD

    app, calls = fake_garmin_app(TOKEN)
    args = {"start_date": "2026-09-28", "end_date": "2026-09-30"}
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("get_race_predictions", dict(args))
    assert got == ("ok", RACE_PREDICTIONS_PAYLOAD)
    assert calls == [("get_race_predictions", args)]


@pytest.mark.anyio
async def test_history_call_race_predictions_failures(tmp_path):
    app, _ = fake_garmin_app(TOKEN, auth="missing", errors={"get_race_predictions": NOT_LOGGED_IN})
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("get_race_predictions", {})
    assert got == ("auth_error", None)
    app, _ = fake_garmin_app(TOKEN, errors={"get_race_predictions": "Garmin Connect rate limit (HTTP 429). Wait."})
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("get_race_predictions", {})
    assert got == ("rate_limited", None)


@pytest.mark.anyio
async def test_history_call_tool_429_is_rate_limited(tmp_path):
    app, _ = fake_garmin_app(TOKEN, errors={"get_recovery_snapshot": RATE_LIMIT_TEXT})
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("get_recovery_snapshot", SNAPSHOT_ARGS)
    assert got == ("rate_limited", None)


@pytest.mark.anyio
async def test_history_call_section_429_is_rate_limited(tmp_path):
    snapshot = history_snapshot("2026-10-06", sleep={"error": RATE_LIMIT_TEXT})
    app, _ = fake_garmin_app(TOKEN, snapshot=snapshot)
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("get_recovery_snapshot", SNAPSHOT_ARGS)
    assert got == ("rate_limited", None)


@pytest.mark.anyio
async def test_history_call_section_error_without_429_is_ok(tmp_path):
    snapshot = history_snapshot("2026-10-06", sleep={"error": "Garmin Connect returned HTTP 500"}, hrv={"available": False})
    app, _ = fake_garmin_app(TOKEN, snapshot=snapshot)
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("get_recovery_snapshot", SNAPSHOT_ARGS)
    assert got == ("ok", snapshot)


@pytest.mark.anyio
async def test_history_call_login_error_is_auth_error(tmp_path):
    app, _ = fake_garmin_app(TOKEN, auth="missing", errors={"list_activities": NOT_LOGGED_IN})
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("list_activities", ACTIVITIES_ARGS)
    assert got == ("auth_error", None)


@pytest.mark.anyio
async def test_history_call_other_tool_error_is_error(tmp_path, caplog):
    app, _ = fake_garmin_app(TOKEN, errors={"list_activities": "SECRET-ERROR-TEXT"})
    caplog.set_level(logging.DEBUG)
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("list_activities", ACTIVITIES_ARGS)
    assert got == ("error", None)
    # (the fake garmin-mcp server logs the error text itself; only the coach's own loggers are measured)
    assert [r for r in caplog.records if r.name.startswith("coach_mcp")] == []


@pytest.mark.anyio
@pytest.mark.parametrize("answer", [RawText("not json"), ["a", "list"], "a json string", 7])
async def test_history_call_non_json_or_list_is_error(tmp_path, answer):
    app, _ = fake_garmin_app(TOKEN, snapshot=answer)
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("get_recovery_snapshot", SNAPSHOT_ARGS)
    assert got == ("error", None)


@pytest.mark.anyio
async def test_history_call_unreachable_backend(tmp_path):
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down")

    reader = GarminReader(reader_settings(tmp_path), transport=httpx2.MockTransport(handler))
    assert await reader.history_call("get_recovery_snapshot", SNAPSHOT_ARGS) == ("unreachable", None)


@pytest.mark.anyio
async def test_history_call_wrong_token_is_unreachable_not_a_crash(tmp_path):
    app, calls = fake_garmin_app(TOKEN)
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path, token="a-different-token-0123456789abcdef").history_call(
            "get_recovery_snapshot", SNAPSHOT_ARGS
        )
    assert got == ("unreachable", None) and calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("token", [None, "short"])
async def test_history_call_without_token_makes_no_request(tmp_path, token):
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={})

    reader = GarminReader(reader_settings(tmp_path, token), transport=httpx2.MockTransport(handler))
    assert await reader.history_call("get_recovery_snapshot", SNAPSHOT_ARGS) == ("no_credentials", None)
    assert requests == []


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["get_hrv_trend", "list_scheduled_workouts", "create_workout", "schedule_workout"])
async def test_history_call_refuses_tools_outside_history_tools(tmp_path, name):
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={})

    reader = GarminReader(reader_settings(tmp_path), transport=httpx2.MockTransport(handler))
    with pytest.raises(ValueError, match="not allowed"):
        await reader.history_call(name, {})
    assert requests == []  # refused before any I/O, token or not


@pytest.mark.anyio
async def test_history_call_refuses_a_write_tool_even_with_a_live_backend(tmp_path):
    app, calls = fake_garmin_app(TOKEN)
    async with fake_garmin_running(app):
        with pytest.raises(ValueError):
            await reader_for(app, tmp_path).history_call("create_workout", {})
    assert calls == []


@pytest.mark.anyio
async def test_auth_state_reads_healthz_without_token(tmp_path):
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.url.path, request.headers.get("authorization")))
        return httpx2.Response(200, json={"status": "ok", "garmin_auth": "missing"})

    reader = GarminReader(reader_settings(tmp_path, None), transport=httpx2.MockTransport(handler))
    assert await reader.auth_state() == "missing"
    assert seen == [("/healthz", None)]


@pytest.mark.anyio
async def test_auth_state_is_none_when_unreachable(tmp_path):
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down")

    reader = GarminReader(reader_settings(tmp_path), transport=httpx2.MockTransport(handler))
    assert await reader.auth_state() is None


# ---- the cheap retry check (PR 17)


class RecordingTransport(httpx2.AsyncBaseTransport):
    """Delegates to the inner transport and records the request paths."""

    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self.inner = inner
        self.paths: list[str] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.paths.append(request.url.path)
        return await self.inner.handle_async_request(request)


@pytest.mark.anyio
async def test_sync_check_calls_only_the_device_status_tool(tmp_path, caplog):
    app, calls = fake_garmin_app(TOKEN)
    transport = RecordingTransport(httpx2.ASGITransport(app=app))
    reader = GarminReader(reader_settings(tmp_path), transport=transport)
    caplog.set_level(logging.DEBUG)
    async with fake_garmin_running(app):
        last = await reader.sync_check()
    assert last == SYNC_0507
    assert calls == [("get_device_status", {})]
    assert transport.paths and not any(path.endswith("/healthz") for path in transport.paths)
    assert "garmin sync_check status=ok" in caplog.text
    for private in ("05:07", "5:07", "03:07", "Example Watch"):  # R6: the log carries outcome words only
        assert private not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"errors": {"get_device_status": "SECRET-ERROR-TEXT"}}, "error"),
        ({"device": {"last_upload": {"available": False}}}, "unknown"),
        ({"device": {"last_upload": {"time_utc": "9999-12-31T23:59:59Z", "time_local": "9999-12-31T23:59"}}}, "unknown"),
        ({"errors": {"get_device_status": "Garmin is not logged in. Run scripts/garmin-login.sh"}}, "auth_error"),
    ],
    ids=["tool-error", "unavailable", "year-9999", "login-error"],
)
async def test_sync_check_is_none_when_the_tool_gives_no_time(tmp_path, caplog, kwargs, status):
    app, calls = fake_garmin_app(TOKEN, **kwargs)
    caplog.set_level(logging.DEBUG)
    async with fake_garmin_running(app):
        assert await reader_for(app, tmp_path).sync_check() is None
    assert [name for name, _ in calls] == ["get_device_status"]
    assert f"garmin sync_check status={status}" in caplog.text
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name == "coach_mcp.garmin")
    assert "SECRET-ERROR-TEXT" not in ours


@pytest.mark.anyio
async def test_sync_check_unreachable_backend(tmp_path, caplog):
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down")

    caplog.set_level(logging.DEBUG)
    reader = GarminReader(reader_settings(tmp_path), transport=httpx2.MockTransport(handler))
    assert await reader.sync_check() is None
    assert "garmin sync_check status=unreachable" in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("token", [None, "short"])
async def test_sync_check_without_a_token_makes_no_request(tmp_path, token, caplog):
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={})

    caplog.set_level(logging.DEBUG)
    reader = GarminReader(reader_settings(tmp_path, token), transport=httpx2.MockTransport(handler))
    assert await reader.sync_check() is None
    assert requests == []
    assert "garmin sync_check status=no_credentials" in caplog.text


def _status(utc: object = "2026-10-07T03:12:00Z", local: object = "2026-10-07T05:12") -> dict:
    return {"last_upload": {"time_utc": utc, "time_local": local}}


def _sync(utc: datetime, local: datetime) -> LastSync:
    return LastSync(utc, local)


PARSE_ROWS = {
    "ok": (_status(), _sync(datetime(2026, 10, 7, 3, 12, tzinfo=UTC), datetime(2026, 10, 7, 5, 12))),
    "none": (None, None),
    "list": ([_status()], None),
    "unavailable": ({"last_upload": {"available": False}}, None),
    "error": ({"last_upload": {"error": "x"}}, None),
    "section-not-a-dict": ({"last_upload": "2026-10-07T03:12:00Z"}, None),
    "no-local": ({"last_upload": {"time_utc": "2026-10-07T03:12:00Z"}}, None),
    "int": (_status(utc=1790744292000), None),
    "bad-format": (_status(utc="2026-10-07 03:12:00"), None),
    "bad-local-format": (_status(local="2026-10-07 05:12"), None),
    "9999": (_status(utc="9999-12-31T23:59:59Z", local="9999-12-31T23:59"), None),
    "0001": (_status(utc="0001-01-01T00:00:00Z", local="0001-01-01T00:00"), None),
    "1999": (_status(utc="1999-12-31T23:59:59Z", local="2000-01-01T00:59"), None),
    "1999-local": (_status(utc="2000-01-01T00:00:00Z", local="1999-12-31T23:59"), None),
    "2100-local": (
        _status(utc="2099-12-31T23:59:59Z", local="2100-01-01T00:59"),
        _sync(datetime(2099, 12, 31, 23, 59, 59, tzinfo=UTC), datetime(2100, 1, 1, 0, 59)),
    ),
    "2101": (_status(utc="2101-01-01T00:00:00Z", local="2101-01-01T01:00"), None),
    "2101-local": (_status(utc="2100-12-31T23:59:59Z", local="2101-01-01T00:59"), None),
    "2000-low": (
        _status(utc="2000-01-01T00:00:00Z", local="2000-01-01T01:00"),
        _sync(datetime(2000, 1, 1, tzinfo=UTC), datetime(2000, 1, 1, 1, 0)),
    ),
    "2100-utc": (
        _status(utc="2100-01-01T00:00:00Z", local="2100-01-01T01:00"),
        _sync(datetime(2100, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, 1, 0)),
    ),
}


@pytest.mark.parametrize(("device", "expected"), list(PARSE_ROWS.values()), ids=list(PARSE_ROWS))
def test_parse_last_sync_table(device, expected):
    assert parse_last_sync(device) == expected
