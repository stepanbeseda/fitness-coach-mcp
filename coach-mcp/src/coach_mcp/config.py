"""Runtime settings, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _text(env: dict[str, str], name: str, default: str) -> str:
    value = env.get(name, default).strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


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

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)

        db_path = Path(_text(env, "COACH_MCP_DB_PATH", "/data/coach/coach.db"))
        if not db_path.is_absolute():
            raise ValueError("COACH_MCP_DB_PATH must be an absolute path")

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

        return cls(
            db_path=db_path,
            bearer_token_file=Path(_text(env, "COACH_MCP_BEARER_TOKEN_FILE", "/run/secrets/coach_backend_token")),
            host=_text(env, "COACH_MCP_HOST", "0.0.0.0"),
            port=port,
            timezone=timezone,
            log_level=log_level,
            allowed_hosts=_split(env.get("COACH_MCP_ALLOWED_HOSTS")),
        )
