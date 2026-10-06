"""Entry point: ``python -m coach_mcp`` runs the HTTP server."""

from __future__ import annotations

import logging

import uvicorn

from .config import Settings
from .server import create_app


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Library chatter (session lifecycle, HTTP client lines) is noise here; the
    # tool wrapper logs one line per call. The MCP SDK logs the text of every
    # ToolError at INFO and those texts can echo caller input, so keep it at
    # WARNING or above.
    for name in ("mcp", "httpx", "httpx2", "httpcore", "httpcore2", "sse_starlette"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> None:
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


if __name__ == "__main__":
    main()
