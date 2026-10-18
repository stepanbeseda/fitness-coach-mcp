"""Error kinds (PR 31): every tool error carries its kind in `_meta`, every error section next to its text.

Texts are compared with literals, not with the module constants: the literal is what coach-mcp falls back to.
"""

from __future__ import annotations

import pytest
import requests
from garminconnect import GarminConnectNotFoundError, GarminConnectTooManyRequestsError
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from conftest import AUTH_ERROR, BACKEND_TOKEN, CONN_ERROR, READONLY_TOKEN, auth_headers, running_client
from garmin_coach_mcp import errors
from garmin_coach_mcp.errors import ERROR_KIND_KEY, ErrorKinds, GarminError
from garmin_coach_mcp.garmin_client import TOKEN_FILE_NAME

RATE_LIMIT_TEXT = "Garmin Connect rate limit (HTTP 429). Wait a few minutes and retry."
LOGIN_HINT = "`scripts/garmin-login.sh`"


@pytest.mark.parametrize(
    ("error", "kind", "text"),
    [
        (AUTH_ERROR, "garmin_auth", LOGIN_HINT + " to log in again"),
        (GarminConnectTooManyRequestsError("429"), "rate_limited", RATE_LIMIT_TEXT),
        (GarminConnectNotFoundError("API Error 404"), "not_found", "Not found in Garmin Connect (API Error 404)."),
        (CONN_ERROR, "garmin_unavailable", "Garmin Connect request failed: API Error 500 - boom"),
        (requests.ConnectionError("x"), "garmin_unavailable", "Network error talking to Garmin Connect"),
        (OSError("x"), "garmin_unavailable", "Network error talking to Garmin Connect"),
        (ValueError("bad date"), "invalid_request", "bad date"),
    ],
)
def test_every_library_error_gets_its_kind(service, fake, error, kind, text):
    fake.fail["login" if error is AUTH_ERROR else "get_sleep_data"] = error
    with pytest.raises(GarminError) as raised:
        service.call_sync(lambda c: c.get_sleep_data("2026-09-30"))
    assert raised.value.kind == kind
    assert text in str(raised.value)


def test_a_missing_login_is_garmin_auth_with_the_login_hint(service, tokens_dir):
    (tokens_dir / TOKEN_FILE_NAME).unlink()
    with pytest.raises(GarminError) as raised:
        service.call_sync(lambda c: None)
    assert raised.value.kind == "garmin_auth"
    assert LOGIN_HINT + " (one-time interactive login" in str(raised.value)


def test_kind_of_a_plain_tool_error_and_of_a_crash():
    assert errors.kind_of(ToolError("limit must be between 1 and 100")) == "invalid_request"
    assert errors.kind_of(RuntimeError("x")) == "internal"
    assert errors.kind_of(GarminError("x", "not_found")) == "not_found"


async def _call(app, name, arguments, token=BACKEND_TOKEN):
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    async with running_client(app) as client:
        response = await client.post("/mcp", headers=auth_headers(token), json=body)
    return response.json()["result"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("setup", "name", "arguments", "kind", "text"),
    [
        ("429", "list_activities", {"days": 7}, "rate_limited",
         "Error executing tool list_activities: " + RATE_LIMIT_TEXT),
        ("auth", "list_activities", {"days": 7}, "garmin_auth", LOGIN_HINT + " to log in again"),
        ("logout", "get_recovery_snapshot", {}, "garmin_auth", LOGIN_HINT + " (one-time interactive login"),
        ("none", "get_recovery_snapshot", {"date": "30.9.2026"}, "invalid_request",
         "Error executing tool get_recovery_snapshot: date must be YYYY-MM-DD, got '30.9.2026'"),
        ("none", "list_activities", {"limit": 0}, "invalid_request", "validation error"),
        ("none", "no_such_tool", {}, "invalid_request", "Unknown tool: no_such_tool"),
        ("crash", "list_activities", {"days": 7}, "internal", "Error executing tool list_activities"),
        ("gear", "get_gear", {}, "unexpected_answer", "did not report a profile id"),
    ],
)
async def test_a_tool_error_carries_its_kind_over_http(app, fake, tokens_dir, setup, name, arguments, kind, text):
    if setup == "429":
        fake.fail["get_activities_by_date"] = GarminConnectTooManyRequestsError("429")
    if setup == "auth":
        fake.fail["login"] = AUTH_ERROR
    if setup == "logout":
        (tokens_dir / TOKEN_FILE_NAME).unlink()
    if setup == "crash":
        fake.fail["get_activities_by_date"] = RuntimeError("secret detail")
    if setup == "gear":
        fake.profile_id = None
    result = await _call(app, name, arguments)
    assert result["isError"] is True
    assert result["_meta"] == {ERROR_KIND_KEY: kind}
    assert text in result["content"][0]["text"]
    assert "secret detail" not in result["content"][0]["text"]


@pytest.mark.anyio
async def test_the_read_only_refusal_is_read_only(app):
    result = await _call(app, "unschedule_workout", {"schedule_id": 1}, token=READONLY_TOKEN)
    assert result["isError"] is True and result["_meta"] == {ERROR_KIND_KEY: "read_only"}


@pytest.mark.anyio
async def test_a_success_has_no_kind_and_an_error_section_has_one(app, fake):
    fake.fail["get_hrv_data"] = GarminConnectTooManyRequestsError("429")
    result = await _call(app, "get_recovery_snapshot", {"date": "2026-09-30"})
    assert result.get("isError", False) is False
    assert ERROR_KIND_KEY not in (result.get("_meta") or {})
    assert '"hrv":{"error":"' + RATE_LIMIT_TEXT + '","error_kind":"rate_limited"}' in result["content"][0]["text"]


@pytest.mark.anyio
async def test_a_kind_already_set_is_kept():
    refusal = CallToolResult(
        content=[TextContent(type="text", text="no")], is_error=True, meta={ERROR_KIND_KEY: "read_only"}
    )

    async def call_next(ctx):
        errors.record_kind(GarminError("x", "rate_limited"))
        return refusal

    assert (await ErrorKinds().intercept_tool_call(None, None, call_next)).meta == {ERROR_KIND_KEY: "read_only"}
