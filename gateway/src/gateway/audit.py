"""Structured audit log (JSON lines) of authentication events and MCP calls.

Callers pass only non-sensitive fields: never tokens, codes, passwords, TOTP
codes, tool arguments or tool results.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler


class Audit:
    def __init__(self, path: str | None = None) -> None:
        self._logger = logging.getLogger("gateway.audit")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        for handler in list(self._logger.handlers):
            self._logger.removeHandler(handler)
            handler.close()
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(stream)
        if path:
            file_handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(file_handler)

    def event(self, name: str, **fields: object) -> None:
        record: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "event": name,
        }
        record.update({k: v for k, v in fields.items() if v is not None})
        self._logger.info(json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str))
