import datetime as dt
import logging
from pathlib import Path

import pytest

from coach_mcp.__main__ import configure_logging
from coach_mcp.config import CHAT_ID_ERROR, Settings


def test_from_env_defaults():
    s = Settings.from_env({})
    assert s.db_path == Path("/data/coach/coach.db")
    assert s.bearer_token_file == Path("/run/secrets/coach_backend_token")
    assert s.host == "0.0.0.0"
    assert s.port == 8000
    assert s.timezone == "Europe/Prague"
    assert s.log_level == "INFO"
    assert s.allowed_hosts == []
    assert s.telegram_chat_id == ""
    assert s.telegram_token_file == Path("/run/secrets/telegram_bot_token")
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
    for name in ("httpx2", "httpcore2"):
        logging.getLogger(name).setLevel(logging.NOTSET)
    configure_logging("DEBUG")
    assert logging.getLogger("mcp").level >= logging.WARNING
    # httpx2 logs every request URL at INFO and the Telegram bot token is part of that URL. The logger's own
    # level is asserted: the effective level would be WARNING anyway while the root stays at WARNING under pytest.
    for name in ("httpx2", "httpcore2"):
        assert logging.getLogger(name).level >= logging.WARNING, name
    # The SDK client logs under "mcp.client" and under a top-level logger literally named "client".
    for name in ("mcp.client", "client"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.CRITICAL, name


def valid_token_file(tmp_path: Path) -> Path:
    path = tmp_path / "telegram_bot_token"
    path.write_text("123456789:" + "A" * 35 + "\n", encoding="utf-8")
    return path


def test_briefing_settings_from_env(tmp_path):
    token_file = valid_token_file(tmp_path)
    s = Settings.from_env(
        {
            "COACH_TELEGRAM_CHAT_ID": "-100123456789",
            "COACH_TELEGRAM_TOKEN_FILE": str(token_file),
            "COACH_GARMIN_MCP_URL": "https://garmin.example/mcp",
            "COACH_GARMIN_TOKEN_FILE": "/tmp/garmin",
            "COACH_BRIEFING_TIME": "05:45",
            "COACH_BRIEFING_CUTOFF": "08:00",
            "COACH_BRIEFING_RETRY_MINUTES": "10",
        }
    )
    assert s.telegram_chat_id == "-100123456789"
    assert s.telegram_token_file == token_file
    assert s.garmin_url == "https://garmin.example/mcp"
    assert s.garmin_token_file == Path("/tmp/garmin")
    assert s.briefing_time == dt.time(5, 45)
    assert s.briefing_cutoff == dt.time(8, 0)
    assert s.briefing_retry_minutes == 10
    assert s.push_enabled is True


def test_blank_briefing_values_fall_back_to_defaults():
    names = (
        "COACH_TELEGRAM_CHAT_ID",
        "COACH_TELEGRAM_TOKEN_FILE",
        "COACH_GARMIN_MCP_URL",
        "COACH_GARMIN_TOKEN_FILE",
        "COACH_BRIEFING_TIME",
        "COACH_BRIEFING_CUTOFF",
        "COACH_BRIEFING_RETRY_MINUTES",
    )
    assert Settings.from_env(dict.fromkeys(names, "")) == Settings()
    assert Settings.from_env(dict.fromkeys(names, "  ")) == Settings()


def test_chat_id_not_in_repr(tmp_path):
    token_file = valid_token_file(tmp_path)
    s = Settings.from_env({"COACH_TELEGRAM_CHAT_ID": "918273645", "COACH_TELEGRAM_TOKEN_FILE": str(token_file)})
    assert "918273645" not in repr(s)
    assert "918273645" not in str(s)
    assert s.telegram_chat_id == "918273645"


@pytest.mark.parametrize("value", ["abc", "@mychannel", "12a", "0", "-0", "+123", "1.5", "1 2", "1" * 21])
def test_malformed_chat_id_is_rejected(tmp_path, value):
    env = {"COACH_TELEGRAM_CHAT_ID": value, "COACH_TELEGRAM_TOKEN_FILE": str(valid_token_file(tmp_path))}
    with pytest.raises(ValueError) as exc:
        Settings.from_env(env)
    # Exact text: the value is not echoed back.
    assert str(exc.value) == CHAT_ID_ERROR


@pytest.mark.parametrize(
    "content",
    [None, "", "\n", "tk_SECRETabc", "123456789:SECRETshort", "123456789:" + "S" * 35 + " SECRET"],
)
def test_chat_id_requires_a_usable_token_file(tmp_path, content):
    path = tmp_path / "telegram_bot_token"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    env = {"COACH_TELEGRAM_CHAT_ID": "123456789", "COACH_TELEGRAM_TOKEN_FILE": str(path)}
    with pytest.raises(ValueError) as exc:
        Settings.from_env(env)
    assert str(exc.value) == (
        f"COACH_TELEGRAM_CHAT_ID is set but {path} does not hold a Telegram bot token "
        "(scripts/setup-secrets.sh telegram-token)"
    )
    # The path (a tmp dir named after the test) is part of the message; nothing read from the file is.
    assert "SECRET" not in str(exc.value).replace(str(path), "")


def test_chat_id_with_valid_token_enables_push(tmp_path):
    token_file = valid_token_file(tmp_path)
    token = token_file.read_text(encoding="utf-8").strip()
    s = Settings.from_env({"COACH_TELEGRAM_CHAT_ID": "123456789", "COACH_TELEGRAM_TOKEN_FILE": str(token_file)})
    assert s.push_enabled is True
    assert all(token not in str(v) for v in s.__dict__.values())
