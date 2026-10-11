"""Push transport: the Telegram Bot API (sendMessage, plain text).

The bot token is part of the request URL, so log lines carry outcome words, status codes,
durations and exception class names only, never the URL, the token, the chat id or the text.
``__main__.configure_logging`` keeps the ``httpx2`` logger, which logs every request URL at INFO,
at WARNING.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx2

from .config import Settings, read_bot_token

log = logging.getLogger("coach_mcp.push")

TELEGRAM_API = "https://api.telegram.org"

PushOutcome = Literal["sent", "transient", "permanent"]


@dataclass(frozen=True)
class PushMessage:
    title: str  # first line: verdict circle, level, date
    body: str

    @property
    def text(self) -> str:
        """Exactly what Telegram receives: the title line, a blank line, the body."""
        return f"{self.title}\n\n{self.body}"


class PushSender(Protocol):
    async def send(self, msg: PushMessage) -> PushOutcome: ...


class TextSender(Protocol):
    async def send_text(self, text: str) -> PushOutcome: ...


class TelegramPush:
    """Sends one plain-text message to the configured chat through ``sendMessage``.

    The token is read from its file on every send, so a rotated token works without a restart,
    and it exists only inside ``send``: never as an argument, an attribute or a log value.
    """

    def __init__(self, settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport

    async def send(self, msg: PushMessage) -> PushOutcome:
        return await self.send_text(msg.text)

    async def send_text(self, text: str) -> PushOutcome:
        """One plain-text message with exactly ``text`` (the replies of the inbound part use this directly)."""
        token = read_bot_token(self._settings.telegram_token_file)
        if token is None:
            log.warning("push status=permanent reason=no_token")
            return "permanent"
        body = {"chat_id": int(self._settings.telegram_chat_id), "text": text}
        started = time.monotonic()
        try:
            async with httpx2.AsyncClient(timeout=httpx2.Timeout(20.0), transport=self._transport) as http:
                # stream(): only the status line is needed, the reply body is never read into memory.
                # Never raise_for_status() or log exc.request: both carry the URL with the token.
                async with http.stream("POST", f"{TELEGRAM_API}/bot{token}/sendMessage", json=body) as response:
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
