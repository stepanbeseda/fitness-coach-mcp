import datetime as dt
import logging
from pathlib import Path

import pytest

from coach_mcp.__main__ import configure_logging
from coach_mcp.config import CHAT_ID_ERROR, Settings, db_path_from_env


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
    assert s.garmin_token_file == Path("/run/secrets/garmin_readonly_token")
    assert s.hevy_url == "http://hevy-mcp:3000/mcp"
    assert s.hevy_token_file == Path("/run/secrets/hevy_backend_token")
    assert s.briefing_time == dt.time(6, 30)
    assert s.briefing_cutoff == dt.time(9, 30)
    assert s.briefing_retry_minutes == 15
    assert s.history_enabled is True
    assert s.history_time == dt.time(3, 30)
    assert s.history_backfill_days == 90
    assert s.history_call_delay_seconds == 15
    assert s.home_latitude is None
    assert s.home_longitude is None
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
    # httpx2 logs every request URL at INFO; the Telegram bot token and the weather request's coordinates are part
    # of that URL. The logger's own
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
        "COACH_HISTORY_ENABLED",
        "COACH_HISTORY_TIME",
        "COACH_HISTORY_BACKFILL_DAYS",
        "COACH_HISTORY_CALL_DELAY_SECONDS",
        "COACH_HEVY_MCP_URL",
        "COACH_HEVY_TOKEN_FILE",
        "COACH_HOME_LATITUDE",
        "COACH_HOME_LONGITUDE",
    )
    defaults = Settings()
    assert defaults.hevy_url == "http://hevy-mcp:3000/mcp"
    assert defaults.hevy_token_file == Path("/run/secrets/hevy_backend_token")
    assert Settings.from_env(dict.fromkeys(names, "")) == defaults
    assert Settings.from_env(dict.fromkeys(names, "  ")) == defaults


def test_home_location_is_rounded():
    def home(lat, lon):
        s = Settings.from_env({"COACH_HOME_LATITUDE": lat, "COACH_HOME_LONGITUDE": lon})
        return s.home_latitude, s.home_longitude

    assert home("50.0873", "14.4209") == (50.09, 14.42)
    assert home("-33.8688", "151.2093") == (-33.87, 151.21)
    assert home(" 51.48 ", "-0.001") == (51.48, 0.0)
    lon = home("0", "-0.001")[1]
    assert str(lon) == "0.0"  # never -0.0


_SET_TOGETHER = "COACH_HOME_LATITUDE and COACH_HOME_LONGITUDE must be set together"
_NUMBER = r"COACH_HOME_(LATITUDE|LONGITUDE) must be a number in decimal degrees, e\.g\. 51\.48"


@pytest.mark.parametrize(
    ("env", "match", "secret"),
    [
        ({"COACH_HOME_LATITUDE": "50.08"}, _SET_TOGETHER, "50.08"),
        ({"COACH_HOME_LONGITUDE": "14.42"}, _SET_TOGETHER, "14.42"),
        ({"COACH_HOME_LATITUDE": "abc", "COACH_HOME_LONGITUDE": "14.42"}, _NUMBER, "abc"),
        ({"COACH_HOME_LATITUDE": "nan", "COACH_HOME_LONGITUDE": "14.42"}, _NUMBER, "nan"),
        ({"COACH_HOME_LATITUDE": "50.08", "COACH_HOME_LONGITUDE": "inf"}, _NUMBER, "inf"),
        ({"COACH_HOME_LATITUDE": "50,08", "COACH_HOME_LONGITUDE": "14.42"}, _NUMBER, "50,08"),
        ({"COACH_HOME_LATITUDE": "91", "COACH_HOME_LONGITUDE": "14.42"}, "COACH_HOME_LATITUDE must be between -90 and 90", "91"),
        ({"COACH_HOME_LATITUDE": "-90.5", "COACH_HOME_LONGITUDE": "14.42"}, "COACH_HOME_LATITUDE must be between -90 and 90", "90.5"),
        ({"COACH_HOME_LATITUDE": "50.08", "COACH_HOME_LONGITUDE": "181"}, "COACH_HOME_LONGITUDE must be between -180 and 180", "181"),
    ],
)
def test_home_location_rejects_invalid_values(env, match, secret):
    with pytest.raises(ValueError, match=match) as exc:
        Settings.from_env(env)
    assert secret not in str(exc.value)


def test_home_location_not_in_repr():
    s = Settings.from_env({"COACH_HOME_LATITUDE": "50.0873", "COACH_HOME_LONGITUDE": "14.4209"})
    assert (s.home_latitude, s.home_longitude) == (50.09, 14.42)
    assert "50.09" not in repr(s) and "14.42" not in repr(s)
    assert "50.09" not in str(s) and "14.42" not in str(s)


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
    [None, "", "\n", "tk_SECRETabc", "123456789:SECRETshort", "123456789:" + "S" * 35 + " SECRET", b"\xff\xfeSECRET"],
)
def test_chat_id_requires_a_usable_token_file(tmp_path, content):
    path = tmp_path / "telegram_bot_token"
    if isinstance(content, bytes):  # not valid UTF-8: read_bot_token must treat it as no token, not raise
        path.write_bytes(content)
    elif content is not None:
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


def test_history_overrides():
    s = Settings.from_env(
        {
            "COACH_HISTORY_ENABLED": "False",
            "COACH_HISTORY_TIME": "04:15",
            "COACH_HISTORY_BACKFILL_DAYS": "30",
            "COACH_HISTORY_CALL_DELAY_SECONDS": "20",
        }
    )
    assert s.history_enabled is False
    assert s.history_time == dt.time(4, 15)
    assert s.history_backfill_days == 30
    assert s.history_call_delay_seconds == 20
    assert Settings.from_env({"COACH_HISTORY_ENABLED": "TRUE"}).history_enabled is True
    for days in ("1", "366"):
        assert Settings.from_env({"COACH_HISTORY_BACKFILL_DAYS": days}).history_backfill_days == int(days)
    for seconds in ("1", "600"):
        assert Settings.from_env({"COACH_HISTORY_CALL_DELAY_SECONDS": seconds}).history_call_delay_seconds == int(
            seconds
        )


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("COACH_HISTORY_ENABLED", "yes"),
        ("COACH_HISTORY_ENABLED", "1"),
        ("COACH_HISTORY_TIME", "25:00"),
        ("COACH_HISTORY_TIME", "abc"),
        ("COACH_HISTORY_BACKFILL_DAYS", "0"),
        ("COACH_HISTORY_BACKFILL_DAYS", "367"),
        ("COACH_HISTORY_BACKFILL_DAYS", "abc"),
        ("COACH_HISTORY_CALL_DELAY_SECONDS", "0"),
        ("COACH_HISTORY_CALL_DELAY_SECONDS", "601"),
        ("COACH_HISTORY_CALL_DELAY_SECONDS", "abc"),
    ],
)
def test_invalid_history_values_are_rejected(variable, value):
    with pytest.raises(ValueError, match=variable):
        Settings.from_env({variable: value})


def test_history_time_inside_briefing_window_is_accepted():
    # The job pauses a run due inside the window and starts it at the cutoff, so nothing needs refusing.
    for good in ("06:30", "07:00", "09:29", "09:30", "06:29"):
        assert Settings.from_env({"COACH_HISTORY_TIME": good}).history_time == dt.time.fromisoformat(good)
    env = {"COACH_BRIEFING_TIME": "05:00", "COACH_BRIEFING_CUTOFF": "06:00"}
    assert Settings.from_env({**env, "COACH_HISTORY_TIME": "05:00"}).history_time == dt.time(5, 0)


def test_db_path_from_env():
    assert db_path_from_env({}) == Path("/data/coach/coach.db")
    assert db_path_from_env({"COACH_MCP_DB_PATH": " /tmp/x/coach.db "}) == Path("/tmp/x/coach.db")
    with pytest.raises(ValueError, match="COACH_MCP_DB_PATH must be an absolute path"):
        db_path_from_env({"COACH_MCP_DB_PATH": "relative/coach.db"})
    with pytest.raises(ValueError, match="COACH_MCP_DB_PATH must not be empty"):
        db_path_from_env({"COACH_MCP_DB_PATH": " "})


def test_hevy_settings_overrides():
    s = Settings.from_env({"COACH_HEVY_MCP_URL": "https://hevy.example/mcp", "COACH_HEVY_TOKEN_FILE": "/tmp/hevy"})
    assert s.hevy_url == "https://hevy.example/mcp"
    assert s.hevy_token_file == Path("/tmp/hevy")


@pytest.mark.parametrize(
    "url",
    ["ftp://hevy-mcp:3000/mcp", "http://hevy-mcp:3000/x", "http://hevy-mcp:3000/mcp?x", "http://hevy-mcp:3000"],
)
def test_invalid_hevy_url_is_rejected(url):
    with pytest.raises(ValueError, match="COACH_HEVY_MCP_URL"):
        Settings.from_env({"COACH_HEVY_MCP_URL": url})
