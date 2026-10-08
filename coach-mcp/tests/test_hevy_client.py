from __future__ import annotations

import json
import logging

import anyio
import httpx2
import pytest
from fakes import RawText, fake_garmin_running, fake_hevy_app
from hevy_payloads import events_page, hevy_template

from coach_mcp import hevy as hevy_module
from coach_mcp.config import Settings
from coach_mcp.hevy import ALLOWED_TOOLS, HISTORY_TOOLS, HevyReader

TOKEN = "hevy-backend-token-0123456789abcdef"
EVENTS_ARGS = {"page": 1, "page_size": 10, "since": "2026-07-09T22:00:00Z"}
TEMPLATE_ARGS = {"exercise_template_id": "79D0BB3A"}
RETRY_EXHAUSTED_TEXT = (
    "[get-workout-events] Error: Unable to complete the request after 4 attempts to the Hevy API due to "
    "transient failures. Please try again shortly."
)
RETRY_EXHAUSTED = {"error": {"outcome": "terminal_failure", "code": "HEVY_RETRY_EXHAUSTED", "status": 429}}


def reader_settings(tmp_path, token: str | None = TOKEN) -> Settings:
    token_file = tmp_path / "hevy_backend_token"
    if token is not None:
        token_file.write_text(token + "\n", encoding="utf-8")
    return Settings(hevy_url="http://hevy-mcp:3000/mcp", hevy_token_file=token_file)


def reader_for(app, tmp_path, token: str | None = TOKEN) -> HevyReader:
    return HevyReader(reader_settings(tmp_path, token), transport=httpx2.ASGITransport(app=app))


@pytest.mark.anyio
async def test_events_call_passes_arguments_and_returns_structured_content(tmp_path):
    page = events_page([{"type": "deleted", "id": "W1"}], page=1, page_count=3)
    app, calls = fake_hevy_app(TOKEN, events=page)
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("get-workout-events", dict(EVENTS_ARGS))
    assert got == ("ok", page)  # the dict of the structured content, not the bare list of the text
    assert calls == [("get-workout-events", EVENTS_ARGS)]


@pytest.mark.anyio
async def test_template_call_returns_the_template(tmp_path):
    payload = {"exercise_template": hevy_template("79D0BB3A", "Bench Press (Barbell)", "chest")}
    app, calls = fake_hevy_app(TOKEN, template=payload)
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("get-exercise-template", dict(TEMPLATE_ARGS))
    assert got == ("ok", payload)
    assert calls == [("get-exercise-template", TEMPLATE_ARGS)]


@pytest.mark.anyio
async def test_template_null_is_ok_with_null(tmp_path):
    app, _ = fake_hevy_app(TOKEN, template={"exercise_template": None})
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("get-exercise-template", dict(TEMPLATE_ARGS))
    assert got == ("ok", {"exercise_template": None})


@pytest.mark.anyio
async def test_retry_exhausted_429_is_rate_limited(tmp_path):
    app, _ = fake_hevy_app(TOKEN, errors={"get-workout-events": (RETRY_EXHAUSTED_TEXT, RETRY_EXHAUSTED)})
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("get-workout-events", dict(EVENTS_ARGS))
    assert got == ("rate_limited", None)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "text",
    ["Rate limited by Hevy. Please wait and retry.", "Rate limited by Hevy (HTTP 429). Please wait about 5 seconds before retrying."],
)
async def test_rate_limit_texts_are_rate_limited(tmp_path, text):
    app, _ = fake_hevy_app(TOKEN, errors={"get-workout-events": text})
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("get-workout-events", dict(EVENTS_ARGS))
    assert got == ("rate_limited", None)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        "The Hevy API key is invalid or has expired. Check HEVY_API_KEY.",
        ("Something else", {"error": {"status": 401}}),
        ("Something else", {"error": {"status": 403}}),
    ],
)
async def test_invalid_api_key_is_auth_error(tmp_path, error):
    app, _ = fake_hevy_app(TOKEN, errors={"get-exercise-template": error})
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("get-exercise-template", dict(TEMPLATE_ARGS))
    assert got == ("auth_error", None)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        ("Hevy API experienced an error. Please retry later.", {"error": {"status": 503}}),
        (RETRY_EXHAUSTED_TEXT, {"error": {"status": 500}}),
        (RETRY_EXHAUSTED_TEXT, {"error": {"status": True}}),  # a bool is not a status
        ("boom", {"error": "not a dict"}),
        ("boom", None),
    ],
)
async def test_other_tool_error_is_error(tmp_path, error):
    app, _ = fake_hevy_app(TOKEN, errors={"get-workout-events": error})
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("get-workout-events", dict(EVENTS_ARGS))
    assert got == ("error", None)


@pytest.mark.anyio
async def test_missing_structured_content_is_error(tmp_path):
    app, _ = fake_hevy_app(TOKEN, events=RawText(json.dumps([{"type": "deleted", "id": "W1"}])))
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("get-workout-events", dict(EVENTS_ARGS))
    assert got == ("error", None)


@pytest.mark.anyio
async def test_unreachable_backend(tmp_path):
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down")

    reader = HevyReader(reader_settings(tmp_path), transport=httpx2.MockTransport(handler))
    assert await reader.history_call("get-workout-events", dict(EVENTS_ARGS)) == ("unreachable", None)


@pytest.mark.anyio
async def test_wrong_token_is_unreachable_not_a_crash(tmp_path):
    app, calls = fake_hevy_app(TOKEN)
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path, token="a-different-token-0123456789abcdef").history_call(
            "get-workout-events", dict(EVENTS_ARGS)
        )
    assert got == ("unreachable", None) and calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("token", [None, "short"])
async def test_missing_or_short_token_makes_no_request(tmp_path, token):
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={})

    reader = HevyReader(reader_settings(tmp_path, token), transport=httpx2.MockTransport(handler))
    assert await reader.history_call("get-workout-events", dict(EVENTS_ARGS)) == ("no_credentials", None)
    assert requests == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "name",
    [
        "create-workout",
        "update-workout",
        "replace-workout-exercises",
        "create-routine",
        "update-routine",
        "create-exercise-template",
        "feedback",
        "get-workouts",
        "get_recovery_snapshot",
    ],
)
async def test_only_allowlisted_tools_can_be_called(tmp_path, name):
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={})

    reader = HevyReader(reader_settings(tmp_path), transport=httpx2.MockTransport(handler))
    with pytest.raises(ValueError, match="not allowed"):
        await reader.history_call(name, {})
    assert requests == []  # refused before any I/O

    app, calls = fake_hevy_app(TOKEN)
    async with fake_garmin_running(app):
        with pytest.raises(ValueError, match="not allowed"):
            await reader_for(app, tmp_path).history_call(name, {})
    assert calls == []  # the canary write tools of the live fake were never reached


def test_history_tools_are_exactly_the_two_read_tools():
    assert HISTORY_TOOLS == ALLOWED_TOOLS == ("get-workout-events", "get-exercise-template")


@pytest.mark.anyio
async def test_a_hung_call_is_bounded(tmp_path, monkeypatch):
    real = anyio.fail_after
    requested: list[float] = []

    def short_deadline(seconds: float):
        requested.append(seconds)
        return real(0.3)

    monkeypatch.setattr(hevy_module.anyio, "fail_after", short_deadline)
    app, _ = fake_hevy_app(TOKEN, hang=frozenset({"get-workout-events"}))
    async with fake_garmin_running(app):
        got = await reader_for(app, tmp_path).history_call("get-workout-events", dict(EVENTS_ARGS))
    assert requested[0] == 180  # the reader's own overall deadline for the whole MCP session
    assert got == ("unreachable", None)


@pytest.mark.anyio
async def test_reader_logs_nothing(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    app, _ = fake_hevy_app(TOKEN, errors={"get-exercise-template": "SECRET-ERROR-TEXT"})
    async with fake_garmin_running(app):
        reader = reader_for(app, tmp_path)
        await reader.history_call("get-workout-events", dict(EVENTS_ARGS))
        await reader.history_call("get-exercise-template", dict(TEMPLATE_ARGS))
    assert [r for r in caplog.records if r.name.startswith("coach_mcp")] == []
    assert TOKEN not in caplog.text
