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
    ],
)
def test_from_env_rejects_invalid_values(env):
    with pytest.raises(ValueError):
        Settings.from_env(env)


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    names = ("mcp", "httpx", "httpx2", "httpcore", "httpcore2", "sse_starlette")
    saved = {n: logging.getLogger(n).level for n in names}
    saved_root = (root.level, list(root.handlers))
    yield
    for n, lvl in saved.items():
        logging.getLogger(n).setLevel(lvl)
    root.setLevel(saved_root[0])
    root.handlers[:] = saved_root[1]


def test_configure_logging_keeps_sdk_quiet(restore_logging):
    logging.getLogger("mcp").setLevel(logging.NOTSET)
    configure_logging("DEBUG")
    assert logging.getLogger("mcp").level >= logging.WARNING
