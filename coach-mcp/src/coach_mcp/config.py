"""Runtime settings, read from the environment."""

from __future__ import annotations

import datetime as dt
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

_TIME_RE = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
# Telegram bot tokens look like 123456789:AA... (bot id, a colon, 30+ token characters).
# scripts/setup-secrets.sh checks the same shape.
BOT_TOKEN_RE = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{30,64}")
# A private chat id is a positive integer, a group id a negative one (-100... for supergroups).
CHAT_ID_RE = re.compile(r"-?[1-9][0-9]{0,19}")
CHAT_ID_ERROR = "COACH_TELEGRAM_CHAT_ID must be a numeric Telegram chat id (e.g. 123456789; groups are negative)"


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _text(env: dict[str, str], name: str, default: str) -> str:
    value = env.get(name, default).strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def db_path_from_env(env: dict[str, str] | None = None) -> Path:
    """The ``COACH_MCP_DB_PATH`` setting alone (the backup and restore commands need nothing else)."""
    env = dict(os.environ if env is None else env)
    db_path = Path(_text(env, "COACH_MCP_DB_PATH", "/data/coach/coach.db"))
    if not db_path.is_absolute():
        raise ValueError("COACH_MCP_DB_PATH must be an absolute path")
    return db_path


def _opt(env: dict[str, str], name: str, default: str) -> str:
    """Like ``_text`` but a missing or blank value means the default (Compose passes ``${X:-}``)."""
    return env.get(name, "").strip() or default


def _url(name: str, value: str) -> str:
    if not value.startswith(("http://", "https://")):
        raise ValueError(f"{name} must start with http:// or https://")
    if "?" in value or "#" in value:
        raise ValueError(f"{name} must not contain ? or #")
    if not urlsplit(value).hostname:
        raise ValueError(f"{name} must contain a host")
    return value


def _clock_time(name: str, value: str) -> dt.time:
    if not _TIME_RE.fullmatch(value):
        raise ValueError(f"{name} must be HH:MM (00:00-23:59), got {value!r}")
    return dt.time.fromisoformat(value)


def _int_in(env: dict[str, str], name: str, default: str, low: int, high: int) -> int:
    raw = _opt(env, name, default)
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def round_coordinate(value: float) -> float:
    """Two decimals (about 1 km); -0.0 becomes 0.0 so a request never says -0.00."""
    return round(value, 2) + 0.0


def _coordinate(env: dict[str, str], name: str, low: int, high: int) -> float | None:
    """A decimal-degree coordinate rounded to two decimals; None when unset. Errors never echo the value."""
    raw = _opt(env, name, "")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a number in decimal degrees, e.g. 51.48")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return round_coordinate(value)


def read_bot_token(path: Path) -> str | None:
    """The token from ``path`` when the file holds one in bot-token shape, else None.

    Takes only the path, so the token never appears as an argument in a traceback; never raises.
    """
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return token if BOT_TOKEN_RE.fullmatch(token) else None


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path("/data/coach/coach.db")
    bearer_token_file: Path = Path("/run/secrets/coach_backend_token")
    host: str = "0.0.0.0"
    port: int = 8000
    timezone: str = "Europe/Prague"
    log_level: str = "INFO"
    # Optional Host allowlist (enables the SDK's DNS-rebinding check when set).
    allowed_hosts: list[str] = field(default_factory=list)
    # Morning briefing to Telegram (off while the chat id is empty).
    telegram_chat_id: str = field(default="", repr=False)  # personal: never in a repr or a log
    telegram_token_file: Path = Path("/run/secrets/telegram_bot_token")
    garmin_url: str = "http://garmin-mcp:8000/mcp"
    garmin_token_file: Path = Path("/run/secrets/garmin_readonly_token")
    hevy_url: str = "http://hevy-mcp:3000/mcp"
    hevy_token_file: Path = Path("/run/secrets/hevy_backend_token")
    briefing_time: dt.time = dt.time(6, 30)
    briefing_cutoff: dt.time = dt.time(9, 30)
    briefing_retry_minutes: int = 15
    # Nightly history of Garmin daily values (get_trends, get_weekly_report).
    history_enabled: bool = True
    history_time: dt.time = dt.time(3, 30)
    history_backfill_days: int = 90
    history_call_delay_seconds: int = 15
    # Home location for get_weather_forecast (rounded to two decimals; personal: never in a repr or a log).
    home_latitude: float | None = field(default=None, repr=False)
    home_longitude: float | None = field(default=None, repr=False)

    @property
    def push_enabled(self) -> bool:
        return bool(self.telegram_chat_id)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)

        db_path = db_path_from_env(env)

        raw_port = _text(env, "COACH_MCP_PORT", "8000")
        try:
            port = int(raw_port)
        except ValueError:
            raise ValueError("COACH_MCP_PORT must be an integer") from None
        if not 1 <= port <= 65535:
            raise ValueError("COACH_MCP_PORT must be between 1 and 65535")

        timezone = env.get("TZ", "").strip() or "Europe/Prague"
        try:
            ZoneInfo(timezone)
        except Exception:
            raise ValueError(f"TZ is not a known time zone: {timezone!r}") from None

        log_level = _text(env, "COACH_MCP_LOG_LEVEL", "INFO").upper()
        if log_level not in LOG_LEVELS:
            raise ValueError(f"COACH_MCP_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")

        telegram_chat_id = _opt(env, "COACH_TELEGRAM_CHAT_ID", "")
        if telegram_chat_id and not CHAT_ID_RE.fullmatch(telegram_chat_id):
            raise ValueError(CHAT_ID_ERROR)
        telegram_token_file = Path(_opt(env, "COACH_TELEGRAM_TOKEN_FILE", "/run/secrets/telegram_bot_token"))
        if telegram_chat_id and read_bot_token(telegram_token_file) is None:
            raise ValueError(
                f"COACH_TELEGRAM_CHAT_ID is set but {telegram_token_file} does not hold a Telegram bot token "
                "(scripts/setup-secrets.sh telegram-token)"
            )

        garmin_url = _opt(env, "COACH_GARMIN_MCP_URL", "http://garmin-mcp:8000/mcp")
        _url("COACH_GARMIN_MCP_URL", garmin_url)
        if not garmin_url.endswith("/mcp"):
            raise ValueError("COACH_GARMIN_MCP_URL must end with /mcp")

        hevy_url = _opt(env, "COACH_HEVY_MCP_URL", "http://hevy-mcp:3000/mcp")
        _url("COACH_HEVY_MCP_URL", hevy_url)
        if not hevy_url.endswith("/mcp"):
            raise ValueError("COACH_HEVY_MCP_URL must end with /mcp")

        briefing_time = _clock_time("COACH_BRIEFING_TIME", _opt(env, "COACH_BRIEFING_TIME", "06:30"))
        briefing_cutoff = _clock_time("COACH_BRIEFING_CUTOFF", _opt(env, "COACH_BRIEFING_CUTOFF", "09:30"))
        if briefing_cutoff <= briefing_time:
            raise ValueError("COACH_BRIEFING_CUTOFF must be later than COACH_BRIEFING_TIME")

        raw_retry = _opt(env, "COACH_BRIEFING_RETRY_MINUTES", "15")
        try:
            retry = int(raw_retry)
        except ValueError:
            raise ValueError("COACH_BRIEFING_RETRY_MINUTES must be an integer") from None
        if not 1 <= retry <= 180:
            raise ValueError("COACH_BRIEFING_RETRY_MINUTES must be between 1 and 180")

        raw_history = _opt(env, "COACH_HISTORY_ENABLED", "true").lower()
        if raw_history not in ("true", "false"):
            raise ValueError("COACH_HISTORY_ENABLED must be true or false")
        history_time = _clock_time("COACH_HISTORY_TIME", _opt(env, "COACH_HISTORY_TIME", "03:30"))
        backfill_days = _int_in(env, "COACH_HISTORY_BACKFILL_DAYS", "90", 1, 366)
        call_delay = _int_in(env, "COACH_HISTORY_CALL_DELAY_SECONDS", "15", 1, 600)

        # Rule 1 of the spec: both or neither. Checked before the values so a lone value is named as such.
        has_lat = bool(_opt(env, "COACH_HOME_LATITUDE", ""))
        has_lon = bool(_opt(env, "COACH_HOME_LONGITUDE", ""))
        if has_lat != has_lon:
            raise ValueError("COACH_HOME_LATITUDE and COACH_HOME_LONGITUDE must be set together")
        home_latitude = _coordinate(env, "COACH_HOME_LATITUDE", -90, 90)
        home_longitude = _coordinate(env, "COACH_HOME_LONGITUDE", -180, 180)

        return cls(
            db_path=db_path,
            bearer_token_file=Path(_text(env, "COACH_MCP_BEARER_TOKEN_FILE", "/run/secrets/coach_backend_token")),
            host=_text(env, "COACH_MCP_HOST", "0.0.0.0"),
            port=port,
            timezone=timezone,
            log_level=log_level,
            allowed_hosts=_split(env.get("COACH_MCP_ALLOWED_HOSTS")),
            telegram_chat_id=telegram_chat_id,
            telegram_token_file=telegram_token_file,
            garmin_url=garmin_url,
            garmin_token_file=Path(_opt(env, "COACH_GARMIN_TOKEN_FILE", "/run/secrets/garmin_readonly_token")),
            hevy_url=hevy_url,
            hevy_token_file=Path(_opt(env, "COACH_HEVY_TOKEN_FILE", "/run/secrets/hevy_backend_token")),
            briefing_time=briefing_time,
            briefing_cutoff=briefing_cutoff,
            briefing_retry_minutes=retry,
            history_enabled=raw_history == "true",
            history_time=history_time,
            history_backfill_days=backfill_days,
            history_call_delay_seconds=call_delay,
            home_latitude=home_latitude,
            home_longitude=home_longitude,
        )
