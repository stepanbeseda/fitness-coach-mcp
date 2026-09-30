"""Entry point: `python -m gateway`."""

from __future__ import annotations

import logging
import sys

import uvicorn

from .app import create_app
from .config import ConfigError, load_settings


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = load_settings()
        app = create_app(settings)
    except (ConfigError, ValueError) as exc:
        logging.getLogger("gateway").error("refusing to start: %s", exc)
        return 2
    uvicorn.run(
        app,
        host=settings.listen_host,
        port=settings.listen_port,
        log_level="info",
        access_log=False,
        proxy_headers=False,
        server_header=False,
        timeout_graceful_shutdown=5,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
