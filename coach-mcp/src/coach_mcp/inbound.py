"""Two-way Telegram: long polling of ``getUpdates`` and the athlete's commands, deterministic and without an LLM.

Only outbound HTTPS, like the push. Accepted is a text message of the private chat whose chat id and sender id both
equal ``COACH_TELEGRAM_CHAT_ID``; everything else is skipped without a reply. Commands: ``/dnes`` or ``/today``
(the briefing), ``/plan`` (this week), ``/done <id>``, ``/skip <id>``; any other text becomes a note. The athlete's own
message is the confirmation that the write tools ask the coach for.

Every update is handled once: its effect and the next ``getUpdates`` offset are stored before the reply is sent, the
note (the one effect that must not repeat) in the same transaction as the offset. A database error stops the batch and
the same updates are asked for again; any other error of one update is answered, logged and skipped, so one bad
message never blocks the ones behind it. No answer of Telegram ever ends the loop.

The bot token is part of the request URL and the message text is the athlete's: log lines carry outcome words, the
command word from a fixed list, counters, status codes, durations and exception class names only.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx2
from mcp.server.mcpserver.exceptions import ToolError

from . import tools
from .briefing import MAX_MESSAGE_BYTES, _guard_bytes, _session_line
from .config import Settings, read_bot_id, read_bot_token
from .garmin import GarminSource, exception_kind
from .messages import ReplyTexts, Texts, reply_texts, texts
from .push import TELEGRAM_API, TextSender
from .scheduler import compose_briefing
from .store import Store

log = logging.getLogger("coach_mcp.inbound")

POLL_SECONDS = 50  # long poll: Telegram holds the request this long when there is nothing to return
POLL_LIMIT = 10  # updates per answer
MAX_ANSWER_BYTES = 1024 * 1024
# A stored offset is sent only while it is fresh: Telegram keeps an update for at most 24 hours and may restart the
# update ids after a week without updates, so an old offset proves nothing and could skip a new, lower id.
OFFSET_MAX_AGE = timedelta(days=2)
NOTE_MAX = 2000
NOTE_DATE_WINDOW = timedelta(hours=48)
DRAIN_MAX_BATCHES = 50
BACKOFF_FIRST, BACKOFF_MAX = 5.0, 300.0
CONFLICT_WAIT = 60.0
REJECTED_WAIT = 300.0
EMPTY_FLOOR = 1.0
REPLY_RETRY_WAIT = 2.0

_COMMAND_RE = re.compile(r"/([A-Za-z0-9_]{1,32})(?:@[A-Za-z0-9_]{1,64})?(?:\s+(.*))?", re.DOTALL)
_ID_RE = re.compile(r"#?([0-9]{1,9})")
_STATUS = {"done": "done", "skip": "skipped"}


def parse_command(text: str) -> tuple[str | None, str]:
    """(command word, argument) of a message; (None, text) for free text and ("", "") for a malformed command."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None, stripped
    match = _COMMAND_RE.fullmatch(stripped)
    if match is None:
        return "", ""
    return match.group(1).lower(), (match.group(2) or "").strip()


def _is_int(value: Any, expected: int) -> bool:
    return type(value) is int and value == expected  # bool is an int subclass: True must not pass as chat id 1


class InboundPoller:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        garmin: GarminSource,
        replies: TextSender,
        *,
        now: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]],
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._garmin = garmin
        self._replies = replies
        self._now = now
        self._sleep = sleep
        self._transport = transport
        self._zone = ZoneInfo(settings.timezone)
        self._owner = int(settings.telegram_chat_id)
        self._failures = 0

    # ----------------------------------------------------------------- loop

    async def run(self) -> None:
        log.info("inbound status=on")
        while True:
            try:
                delay = await self.step()
            except Exception as exc:  # BaseException (cancellation) propagates
                log.error("inbound status=crashed kind=%s", exception_kind(exc))
                delay = self._wait("transient")
            if delay > 0:
                await self._sleep(delay)

    async def step(self) -> float:
        """One ``getUpdates`` round and the handling of what it returned; returns the seconds to wait."""
        bot_id = read_bot_id(self._settings.telegram_token_file)
        if bot_id is None:
            log.warning("inbound poll=no_token")
            return self._wait("no_token")
        state = self._store.inbound_state()
        if state is None or state["bot_id"] != bot_id:
            return await self._drain(bot_id)
        offset = state["next_update_id"]
        if offset is not None:
            age = self._now().astimezone(UTC) - datetime.fromisoformat(state["advanced_at"])
            if age >= OFFSET_MAX_AGE:
                offset = None
        started = time.monotonic()
        outcome, updates = await self._get_updates(offset, POLL_SECONDS)
        if outcome != "ok":
            return self._wait(outcome)
        self._failures = 0
        if not updates:
            return EMPTY_FLOOR if time.monotonic() - started < EMPTY_FLOOR else 0.0
        ignored = 0
        for update in sorted(updates, key=lambda item: item["update_id"]):
            next_id = update["update_id"] + 1
            message = self._accepted(update)
            if message is None:
                self._store.advance_inbound(bot_id, next_id, self._now())
                ignored += 1
                continue
            began = time.monotonic()
            try:
                command, status, reply = await self._handle(message, bot_id, next_id)
            except sqlite3.Error:
                raise  # the database is the problem, not the message: ask for the same updates again
            except Exception as exc:
                log.error("inbound status=failed kind=%s", exception_kind(exc))
                command, status, reply = "-", "failed", reply_texts(self._settings.language).failed
                self._store.advance_inbound(bot_id, next_id, self._now())
            sent = await self._reply(reply)
            log.info(
                "inbound cmd=%s status=%s reply=%s ms=%.0f", command, status, sent, (time.monotonic() - began) * 1000
            )
        if ignored:
            log.info("inbound status=ignored count=%d", ignored)
        return 0.0

    async def _drain(self, bot_id: str) -> float:
        """First start with this bot: drop what Telegram still holds (the messages of the bot set-up)."""
        offset: int | None = None
        discarded = 0
        for _ in range(DRAIN_MAX_BATCHES):
            outcome, updates = await self._get_updates(offset, 0)
            if outcome != "ok":
                return self._wait(outcome)  # nothing is stored: the next step starts the drain again
            if not updates:
                break
            offset = max(update["update_id"] for update in updates) + 1
            discarded += len(updates)
        self._failures = 0
        self._store.advance_inbound(bot_id, offset, self._now())
        log.info("inbound status=started discarded=%d", discarded)
        return 0.0

    def _wait(self, outcome: str) -> float:
        if outcome == "conflict":
            return CONFLICT_WAIT
        if outcome in ("unauthorized", "rejected"):
            return REJECTED_WAIT
        delay = min(BACKOFF_FIRST * 2.0**self._failures, BACKOFF_MAX)
        self._failures = min(self._failures + 1, 16)
        return delay

    # ------------------------------------------------------------- Telegram

    async def _get_updates(self, offset: int | None, wait: int) -> tuple[str, list[dict[str, Any]]]:
        """(outcome, updates): ``ok``, ``no_token``, ``transient``, ``unauthorized``, ``conflict``, ``rejected``
        or ``bad_answer``. The token exists only inside this method."""
        token = read_bot_token(self._settings.telegram_token_file)
        if token is None:
            log.warning("inbound poll=no_token")
            return "no_token", []
        body: dict[str, Any] = {"timeout": wait, "limit": POLL_LIMIT, "allowed_updates": ["message"]}
        if offset is not None:
            body["offset"] = offset
        timeout = httpx2.Timeout(10.0, read=wait + 15.0)
        started = time.monotonic()
        raw = b""
        try:
            async with httpx2.AsyncClient(timeout=timeout, transport=self._transport) as http:
                # Never raise_for_status() or log exc.request: both carry the URL with the token.
                async with http.stream("POST", f"{TELEGRAM_API}/bot{token}/getUpdates", json=body) as response:
                    code = response.status_code
                    if code == 200:
                        async for chunk in response.aiter_bytes():
                            raw += chunk
                            if len(raw) > MAX_ANSWER_BYTES:
                                break
        except (httpx2.TimeoutException, httpx2.RequestError, OSError) as exc:
            log.warning("inbound poll=transient kind=%s ms=%.0f", type(exc).__name__, _ms(started))
            return "transient", []
        if code == 200:
            updates = _updates(raw)
            if updates is None:
                log.warning("inbound poll=bad_answer ms=%.0f", _ms(started))
                return "bad_answer", []
            return "ok", updates
        if code in (401, 404):
            outcome = "unauthorized"
        elif code == 409:
            outcome = "conflict"
        elif code == 429 or code >= 500:
            outcome = "transient"
        else:
            outcome = "rejected"
        log.warning("inbound poll=%s code=%d ms=%.0f", outcome, code, _ms(started))
        return outcome, []

    async def _reply(self, text: str) -> str:
        """``sent`` or ``failed``; a transient failure is tried once more. The effect is stored either way."""
        message = _guard_bytes(text, MAX_MESSAGE_BYTES)
        outcome: str = "transient"
        for attempt in (1, 2):
            try:
                outcome = await self._replies.send_text(message)
            except Exception as exc:
                log.warning("inbound reply=crashed kind=%s", exception_kind(exc))
                return "failed"
            if outcome != "transient":
                break
            if attempt == 1:
                await self._sleep(REPLY_RETRY_WAIT)
        return "sent" if outcome == "sent" else "failed"

    # ------------------------------------------------------------- messages

    def _accepted(self, update: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """The message of ``update`` when it is the athlete's own, in the private chat with the bot."""
        message = update.get("message")
        if not isinstance(message, Mapping):
            return None
        chat, sender = message.get("chat"), message.get("from")
        if not isinstance(chat, Mapping) or not isinstance(sender, Mapping):
            return None
        if chat.get("type") != "private" or sender.get("is_bot") is True:
            return None
        if not _is_int(chat.get("id"), self._owner) or not _is_int(sender.get("id"), self._owner):
            return None
        return message

    async def _handle(self, message: Mapping[str, Any], bot_id: str, next_id: int) -> tuple[str, str, str]:
        """(command word, status word, reply). Stores the effect and the offset; never sends."""
        language = self._settings.language
        t, r = texts(language), reply_texts(language)
        now = self._now().astimezone(self._zone)
        today = now.date()
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            self._store.advance_inbound(bot_id, next_id, now)
            return "unsupported", "refused", r.unsupported
        command, argument = parse_command(text)

        if command is None:
            if len(argument) > NOTE_MAX or "\x00" in argument:
                self._store.advance_inbound(bot_id, next_id, now)
                if "\x00" in argument:
                    return "note", "refused", r.note_refused
                return "note", "refused", r.note_too_long.format(length=len(argument), limit=NOTE_MAX)
            day = self._sent_day(message, now)
            self._store.insert_note(day, argument, inbound=(bot_id, next_id, now))
            return "note", "ok", r.note_saved.format(day=t.day_title(day))

        if command in ("dnes", "today"):
            word, status, reply = "today", "bad_argument", r.usage_today
            if not argument:
                try:
                    reply = (await compose_briefing(self._store, self._garmin, today, language)).message.text
                    status = "ok"
                except Exception as exc:
                    log.warning("inbound briefing=failed kind=%s", exception_kind(exc))
                    status, reply = "failed", r.briefing_failed
        elif command == "plan":
            word, status, reply = "plan", "bad_argument", r.usage_plan
            if not argument:
                status, reply = "ok", self._plan(t, r, today)
        elif command in _STATUS:
            word = command
            status, reply = self._mark(t, r, today, command, argument)
        elif command in ("start", "help"):
            word, status, reply = "help", "ok", r.help
        else:
            word, status, reply = "unknown", "unknown", f"{r.unknown_command}\n\n{r.help}"
        self._store.advance_inbound(bot_id, next_id, now)
        return word, status, reply

    def _sent_day(self, message: Mapping[str, Any], now: datetime) -> date:
        """The athlete's local day the message was sent on; today when Telegram's date is missing or not recent."""
        sent = message.get("date")
        if type(sent) is int:
            try:
                at = datetime.fromtimestamp(sent, UTC)
            except (ValueError, OverflowError, OSError):
                return now.date()
            if now - at <= NOTE_DATE_WINDOW:
                return min(at, now).astimezone(self._zone).date()
        return now.date()

    def _line(self, t: Texts, r: ReplyTexts, session: Mapping[str, Any]) -> str:
        day = date.fromisoformat(session["date"])
        text = _session_line(t, session).removeprefix("- ")
        return f"#{session['id']} {r.weekdays[day.weekday()]} {t.day_title(day)} · {text}"

    def _plan(self, t: Texts, r: ReplyTexts, today: date) -> str:
        start, end, label = tools.week_of(today)
        sessions = tools.sessions_out(self._store, self._store.sessions_between(start, end))
        title = r.plan_title.format(week=label, start=t.day_title(start), end=t.day_title(end))
        if not sessions:
            return f"{title}\n{r.plan_empty}"
        return "\n".join([title, *(self._line(t, r, session) for session in sessions)])

    def _usage(self, t: Texts, r: ReplyTexts, today: date, command: str) -> str:
        usage = r.usage_done if command == "done" else r.usage_skip
        rows = [row for row in self._store.sessions_between(today, today) if row["status"] == "planned"]
        if not rows:
            return f"{usage}\n{r.no_open_today}"
        lines = (self._line(t, r, session) for session in tools.sessions_out(self._store, rows))
        return "\n".join([usage, r.open_today, *lines])

    def _mark(self, t: Texts, r: ReplyTexts, today: date, command: str, argument: str) -> tuple[str, str]:
        """(status word, reply) of ``/done`` and ``/skip``."""
        match = _ID_RE.fullmatch(argument)
        session_id = int(match.group(1)) if match else 0
        if session_id < 1:
            return "bad_argument", self._usage(t, r, today, command)
        row = self._store.get_session(session_id)
        if row is None:
            return "bad_argument", f"{r.unknown_session.format(id=session_id)}\n{self._usage(t, r, today, command)}"
        if row["status"] == "superseded":
            return "refused", r.session_superseded.format(id=session_id)
        if row["status"] == "moved":
            link = self._store.moved_to([session_id]).get(session_id)
            if link is None:
                return "refused", r.session_moved.format(id=session_id)
            copy_day = t.day_title(date.fromisoformat(link[1]))
            return "refused", r.session_moved_to.format(id=session_id, copy=link[0], day=copy_day)
        day = date.fromisoformat(row["date"])
        if command == "done" and day > today:
            return "refused", r.session_future.format(id=session_id, day=t.day_title(day))
        try:
            result = tools.update_session(self._store, today, session_id=session_id, status=_STATUS[command])
        except ToolError:  # the session changed between the read and the write
            return "refused", r.cannot_change.format(id=session_id)
        return "ok", r.recorded.format(line=self._line(t, r, result["session"]))


def _updates(raw: bytes) -> list[dict[str, Any]] | None:
    """The ``result`` list of a getUpdates answer, or None when the answer is not one (too long, not JSON, not
    ``ok``, an item without an integer ``update_id``)."""
    if len(raw) > MAX_ANSWER_BYTES:
        return None
    try:
        answer = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        return None
    result = answer.get("result")
    if not isinstance(result, list):
        return None
    for item in result:
        if not isinstance(item, dict) or type(item.get("update_id")) is not int or item["update_id"] < 0:
            return None
    return result


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000
