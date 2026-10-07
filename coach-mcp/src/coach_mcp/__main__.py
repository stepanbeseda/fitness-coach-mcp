"""Entry point: ``python -m coach_mcp`` runs the HTTP server."""

from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn

from .config import Settings
from .scheduler import send_briefing_command
from .server import create_app

USAGE = "usage: python -m coach_mcp [send-briefing]"


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Library chatter (session lifecycle, HTTP client lines) is noise here; the
    # tool wrapper logs one line per call. The MCP SDK logs the text of every
    # ToolError at INFO and those texts can echo caller input, so keep it at
    # WARNING or above. httpx2 logs every request URL at INFO, and the Telegram
    # bot token is part of that URL.
    for name in ("mcp", "httpx", "httpx2", "httpcore", "httpcore2", "sse_starlette"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # The SDK *client* (used to read garmin-mcp) logs every client message with
    # its arguments at DEBUG and tracebacks that can carry response text under
    # "mcp.client" and under a top-level logger literally named "client".
    for name in ("mcp.client", "client"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def main(argv: list[str] | None = None) -> int | None:
    args = sys.argv[1:] if argv is None else argv
    if args == ["send-briefing"]:
        settings = Settings.from_env()
        configure_logging(settings.log_level)
        return asyncio.run(send_briefing_command(settings))
    if args:
        print(USAGE, file=sys.stderr)
        return 2
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
        server_header=False,
        date_header=False,
        proxy_headers=False,
        timeout_graceful_shutdown=5,
    )
    return None


if __name__ == "__main__":
    sys.exit(main())
