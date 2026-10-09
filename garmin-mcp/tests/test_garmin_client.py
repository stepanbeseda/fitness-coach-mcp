import json
import os
import time
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import AUTH_ERROR, FakeGarmin
from garmin_coach_mcp.config import Settings
from garmin_coach_mcp.garmin_client import GarminService
from garmin_coach_mcp.garmin_client import GarminConnectTooManyRequestsError


def test_auth_state_transitions(service, fake, tokens_dir):
    assert service.auth_state() == "ok"
    fake.fail["login"] = AUTH_ERROR
    with pytest.raises(ToolError, match="expired or revoked"):
        service.call_sync(lambda c: c.get_sleep_data("2026-09-30"))
    assert service.auth_state() == "error"
    # a fresh login writes a new token file -> state goes back to ok
    del fake.fail["login"]
    token_file = tokens_dir / "garmin_tokens.json"
    token_file.write_text(json.dumps({"di_token": "new", "di_refresh_token": "r2", "di_client_id": "c"}))
    os.utime(token_file, (time.time() + 5, time.time() + 5))
    assert service.auth_state() == "ok"
    service.call_sync(lambda c: c.get_sleep_data("2026-09-30"))
    assert service.auth_state() == "ok"


def test_client_is_cached_and_reloaded_when_file_changes(settings, tokens_dir):
    instances = []

    def factory():
        instances.append(FakeGarmin())
        return instances[-1]

    service = GarminService(settings, garmin_factory=factory)
    service.call_sync(lambda c: c.get_user_summary("2026-09-30"))
    service.call_sync(lambda c: c.get_user_summary("2026-09-30"))
    assert len(instances) == 1
    assert instances[0].logged_in_with == str(tokens_dir)

    token_file = tokens_dir / "garmin_tokens.json"
    # same content, newer mtime (the library's own refresh) -> no reload
    os.utime(token_file, (time.time() + 10, time.time() + 10))
    service.call_sync(lambda c: c.get_user_summary("2026-09-30"))
    assert len(instances) == 1
    # different content (garmin-login.sh ran) -> reload
    token_file.write_text(json.dumps({"di_token": "other", "di_refresh_token": "x", "di_client_id": "y"}))
    os.utime(token_file, (time.time() + 20, time.time() + 20))
    service.call_sync(lambda c: c.get_user_summary("2026-09-30"))
    assert len(instances) == 2


def test_error_mapping(service, fake):
    fake.fail["get_sleep_data"] = GarminConnectTooManyRequestsError("429")
    with pytest.raises(ToolError, match="rate limit"):
        service.call_sync(lambda c: c.get_sleep_data("2026-09-30"))
    fake.fail["get_sleep_data"] = ValueError("cdate must be in format 'YYYY-MM-DD'")
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        service.call_sync(lambda c: c.get_sleep_data("x"))


def test_empty_token_file_counts_as_missing(service, tokens_dir):
    (tokens_dir / "garmin_tokens.json").write_text("")
    assert service.auth_state() == "missing"
    with pytest.raises(ToolError, match="not connected yet"):
        service.call_sync(lambda c: None)


def test_curl_cffi_session_option(tokens_dir, bearer_file):
    settings = Settings(tokens_dir=tokens_dir, bearer_token_file=bearer_file, api_session="curl_cffi")
    garmin = GarminService(settings)._default_factory()
    assert type(garmin.client._api_session).__module__.startswith("curl_cffi")


def test_settings_from_env(tmp_path):
    s = Settings.from_env({
        "GARMINTOKENS": str(tmp_path),
        "GARMIN_API_SESSION": "CURL_CFFI",
        "GARMIN_MCP_ALLOWED_HOSTS": "garmin-mcp:8000, localhost:*",
        "TZ": "Europe/Prague",
    })
    assert s.tokens_dir == tmp_path and s.api_session == "curl_cffi"
    assert s.allowed_hosts == ["garmin-mcp:8000", "localhost:*"]
    assert s.port == 8000 and s.host == "0.0.0.0"
    assert s.readonly_token_file == Path("/run/secrets/garmin_readonly_token")
    assert Settings.from_env({"GARMIN_MCP_READONLY_TOKEN_FILE": "/tmp/ro"}).readonly_token_file == Path("/tmp/ro")


@pytest.mark.anyio
async def test_keepalive_disabled_in_tests_does_not_block(app):
    from conftest import running_client

    async with running_client(app) as client:
        assert (await client.get("/healthz")).status_code == 200
