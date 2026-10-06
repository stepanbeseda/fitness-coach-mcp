import datetime as dt
import logging
from pathlib import Path

import pytest

from coach_mcp.__main__ import configure_logging
from coach_mcp.config import Settings


def test_from_env_defaults():
    s = Settings.from_env({})
    assert s.db_path == Path("/data/coach/coach.db")
    assert s.bearer_token_file == Path("/run/secrets/coach_backend_token")
    assert s.host == "0.0.0.0"
    assert s.port == 8000
    assert s.timezone == "Europe/Prague"
    assert s.log_level == "INFO"
    assert s.allowed_hosts == []
    assert s.ntfy_url == ""
    assert s.ntfy_topic == ""
    assert s.ntfy_token_file == Path("/run/secrets/ntfy_token")
    assert s.garmin_url == "http://garmin-mcp:8000/mcp"
    assert s.garmin_token_file == Path("/run/secrets/garmin_backend_token")
    assert s.briefing_time == dt.time(6, 30)
    assert s.briefing_cutoff == dt.time(9, 30)
    assert s.briefing_retry_minutes == 15
    assert s.push_enabled is False
    assert s == Settings()


def test_empty_tz_falls_back_to_default():
    assert Settings.from_env({"TZ": ""}).timezone == "Europe/Prague"


def test_from_env_overrides():
    s = Settings.from_env(
        {
            "COACH_MCP_DB_PATH": "/tmp/x/coach.db",
            "COACH_MCP_BEARER_TOKEN_FILE": "/tmp/tok",
            "COACH_MCP_HOST": "127.0.0.1",
            "COACH_MCP_PORT": "9001",
            "TZ": "UTC",
            "COACH_MCP_LOG_LEVEL": "debug",
            "COACH_MCP_ALLOWED_HOSTS": " coach-mcp:8000 , localhost ,, ",
        }
    )
    assert s.db_path == Path("/tmp/x/coach.db")
    assert s.bearer_token_file == Path("/tmp/tok")
    assert s.host == "127.0.0.1"
    assert s.port == 9001
    assert s.timezone == "UTC"
    assert s.log_level == "DEBUG"
    assert s.allowed_hosts == ["coach-mcp:8000", "localhost"]


@pytest.mark.parametrize(
    "env",
    [
        {"COACH_MCP_DB_PATH": "relative/coach.db"},
        {"COACH_MCP_DB_PATH": ""},
        {"COACH_MCP_PORT": "0"},
        {"COACH_MCP_PORT": "65536"},
        {"COACH_MCP_PORT": "abc"},
        {"TZ": "Mars/Base"},
        {"COACH_MCP_LOG_LEVEL": "LOUD"},
        {"COACH_MCP_HOST": " "},
        {"COACH_MCP_BEARER_TOKEN_FILE": ""},
        {"COACH_NTFY_URL": "https://ntfy.sh"},
        {"COACH_NTFY_TOPIC": "abc"},
        {"COACH_NTFY_URL": "ftp://x", "COACH_NTFY_TOPIC": "abc"},
        {"COACH_NTFY_URL": "httpx://ntfy.sh", "COACH_NTFY_TOPIC": "abc"},
        {"COACH_NTFY_URL": "https:/ntfy.sh", "COACH_NTFY_TOPIC": "abc"},
        {"COACH_NTFY_URL": "https://", "COACH_NTFY_TOPIC": "abc"},
        {"COACH_NTFY_URL": "https://ntfy.sh?x", "COACH_NTFY_TOPIC": "abc"},
        {"COACH_NTFY_URL": "https://ntfy.sh#x", "COACH_NTFY_TOPIC": "abc"},
        {"COACH_NTFY_URL": "https://ntfy.sh", "COACH_NTFY_TOPIC": "a/b"},
        {"COACH_NTFY_URL": "https://ntfy.sh", "COACH_NTFY_TOPIC": "a" * 65},
        {"COACH_GARMIN_MCP_URL": "http://garmin-mcp:8000"},
        {"COACH_GARMIN_MCP_URL": "ftp://garmin-mcp:8000/mcp"},
        {"COACH_GARMIN_MCP_URL": "http://garmin-mcp:8000/mcp?x=1"},
        {"COACH_BRIEFING_TIME": "6:30"},
        {"COACH_BRIEFING_TIME": "24:00"},
        {"COACH_BRIEFING_CUTOFF": "09:60"},
        {"COACH_BRIEFING_CUTOFF": "06:30"},
        {"COACH_BRIEFING_TIME": "10:00"},
        {"COACH_BRIEFING_RETRY_MINUTES": "0"},
        {"COACH_BRIEFING_RETRY_MINUTES": "181"},
        {"COACH_BRIEFING_RETRY_MINUTES": "x"},
    ],
)
def test_from_env_rejects_invalid_values(env):
    with pytest.raises(ValueError):
        Settings.from_env(env)


def test_configure_logging_keeps_sdk_quiet(restore_logging):
    logging.getLogger("mcp").setLevel(logging.NOTSET)
    configure_logging("DEBUG")
    assert logging.getLogger("mcp").level >= logging.WARNING
    # The SDK client logs under "mcp.client" and under a top-level logger literally named "client".
    for name in ("mcp.client", "client"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.CRITICAL, name


def test_briefing_settings_from_env():
    s = Settings.from_env(
        {
            "COACH_NTFY_URL": "https://ntfy.example/",
            "COACH_NTFY_TOPIC": "my-Topic_1",
            "COACH_NTFY_TOKEN_FILE": "/tmp/ntfy",
            "COACH_GARMIN_MCP_URL": "https://garmin.example/mcp",
            "COACH_GARMIN_TOKEN_FILE": "/tmp/garmin",
            "COACH_BRIEFING_TIME": "05:45",
            "COACH_BRIEFING_CUTOFF": "08:00",
            "COACH_BRIEFING_RETRY_MINUTES": "10",
        }
    )
    assert s.ntfy_url == "https://ntfy.example"  # trailing slash removed
    assert s.ntfy_topic == "my-Topic_1"
    assert s.ntfy_token_file == Path("/tmp/ntfy")
    assert s.garmin_url == "https://garmin.example/mcp"
    assert s.garmin_token_file == Path("/tmp/garmin")
    assert s.briefing_time == dt.time(5, 45)
    assert s.briefing_cutoff == dt.time(8, 0)
    assert s.briefing_retry_minutes == 10
    assert s.push_enabled is True


def test_blank_briefing_values_fall_back_to_defaults():
    names = (
        "COACH_NTFY_URL",
        "COACH_NTFY_TOPIC",
        "COACH_NTFY_TOKEN_FILE",
        "COACH_GARMIN_MCP_URL",
        "COACH_GARMIN_TOKEN_FILE",
        "COACH_BRIEFING_TIME",
        "COACH_BRIEFING_CUTOFF",
        "COACH_BRIEFING_RETRY_MINUTES",
    )
    assert Settings.from_env(dict.fromkeys(names, "")) == Settings()
    assert Settings.from_env(dict.fromkeys(names, "  ")) == Settings()


def test_topic_not_in_repr():
    s = Settings.from_env({"COACH_NTFY_URL": "https://ntfy.sh", "COACH_NTFY_TOPIC": "secret-topic-XYZ"})
    assert "secret-topic-XYZ" not in repr(s)
    assert "secret-topic-XYZ" not in str(s)
    assert s.ntfy_topic == "secret-topic-XYZ"
