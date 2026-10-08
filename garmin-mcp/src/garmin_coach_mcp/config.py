"""Runtime settings, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


READONLY_TOKEN_FILE = "/run/secrets/garmin_readonly_token"


@dataclass(frozen=True)
class Settings:
    tokens_dir: Path = Path("~/.garminconnect").expanduser()
    bearer_token_file: Path = Path("/run/secrets/garmin_backend_token")
    # coach-mcp's token: tool calls of tools not annotated read-only are refused (access.py).
    readonly_token_file: Path = Path(READONLY_TOKEN_FILE)
    host: str = "0.0.0.0"
    port: int = 8000
    timezone: str = "Europe/Prague"
    # "requests" (library default) or "curl_cffi" (workaround for
    # python-garminconnect issue #444: data calls answered with HTTP 403).
    api_session: str = "requests"
    keepalive_hours: float = 6.0
    log_level: str = "INFO"
    # Optional Host allowlist (enables the SDK's DNS-rebinding check when set).
    allowed_hosts: list[str] = field(default_factory=list)
    max_concurrent_calls: int = 4

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        return cls(
            tokens_dir=Path(env.get("GARMINTOKENS", "~/.garminconnect")).expanduser(),
            bearer_token_file=Path(
                env.get("GARMIN_MCP_BEARER_TOKEN_FILE", "/run/secrets/garmin_backend_token")
            ),
            readonly_token_file=Path(env.get("GARMIN_MCP_READONLY_TOKEN_FILE", READONLY_TOKEN_FILE)),
            host=env.get("GARMIN_MCP_HOST", "0.0.0.0"),
            port=int(env.get("GARMIN_MCP_PORT", "8000")),
            timezone=env.get("TZ") or "Europe/Prague",
            api_session=env.get("GARMIN_API_SESSION", "requests").strip().lower(),
            keepalive_hours=float(env.get("GARMIN_KEEPALIVE_HOURS", "6")),
            log_level=env.get("GARMIN_MCP_LOG_LEVEL", "INFO").upper(),
            allowed_hosts=_split(env.get("GARMIN_MCP_ALLOWED_HOSTS")),
            max_concurrent_calls=int(env.get("GARMIN_MAX_CONCURRENT_CALLS", "4")),
        )
