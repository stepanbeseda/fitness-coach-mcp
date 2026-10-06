"""Push notification transport (ntfy, JSON publish).

Log lines carry outcome words, status codes, durations and exception class names
only, never the title, the message, the topic, the URL or the token.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx2

from .config import Settings

log = logging.getLogger("coach_mcp.push")

PushOutcome = Literal["sent", "transient", "permanent"]


@dataclass(frozen=True)
class PushMessage:
    title: str
    message: str
    priority: int
    tags: tuple[str, ...]


class PushSender(Protocol):
    async def send(self, msg: PushMessage) -> PushOutcome: ...


class NtfyPush:
    """Publishes through the ntfy "publish as JSON" endpoint (a POST to the server root).

    The topic travels in the JSON body, never in a URL, so no HTTP client log line can carry it.
    """

    def __init__(self, settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport

    def _token(self) -> str:
        try:
            return self._settings.ntfy_token_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""

    async def send(self, msg: PushMessage) -> PushOutcome:
        headers = {}
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = {
            "topic": self._settings.ntfy_topic,
            "title": msg.title,
            "message": msg.message,
            "priority": msg.priority,
            "tags": list(msg.tags),
        }
        started = time.monotonic()
        try:
            async with httpx2.AsyncClient(
                headers=headers, timeout=httpx2.Timeout(20.0), transport=self._transport
            ) as http:
                # stream(): only the status line is needed, the reply body is never read into memory.
                async with http.stream("POST", f"{self._settings.ntfy_url}/", json=body) as response:
                    code = response.status_code
        except (httpx2.TimeoutException, httpx2.RequestError, OSError) as exc:
            log.warning("push status=transient kind=%s ms=%.0f", type(exc).__name__, _ms(started))
            return "transient"
        if 200 <= code < 300:
            outcome: PushOutcome = "sent"
        elif code == 429 or code >= 500:
            outcome = "transient"
        else:
            outcome = "permanent"
        log.log(logging.INFO if outcome == "sent" else logging.WARNING, "push status=%s code=%d ms=%.0f", outcome, code, _ms(started))
        return outcome


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000
