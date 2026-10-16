"""Sending daily messages once a day: the morning briefing and the Monday report share these rules.

``DailyDelivery`` runs the window of one local day: nothing before the start time, attempts every retry interval
until the cutoff, one final attempt at or after it, and nothing more once the day is decided. Its messages go out in
order: a later one is tried only after the earlier one went out today (in this process, or recorded before a
restart). For every message it decides from the injected functions whether to send, skip, retry or give up:

- already sent (``already_sent``) or not due (``due``): nothing to do, the next message follows;
- the first look of the day was already past the cutoff (a late start): skipped for the day;
- compose said the data is not complete yet (``waiting``) and this is not the final attempt: retry;
- push ``sent``: recorded (a failing record is logged, the message is not sent again in this process), next message;
- push ``permanent``, or ``transient`` on the final attempt: given up for the day;
- push ``transient`` before the cutoff: retry.

A crash (an exception of the functions or the push) is handled per message. ``crash="retry"`` logs it here and
treats it like a transient failure. ``crash="raise"`` lets it leave ``tick()``, so the caller's loop logs it and
waits its own interval; a crash of compose or push on the final attempt ends the day first, a crash of the
already-sent look does not (the late-start decision was taken before it).

``send_now`` is the manual send of the CLI commands. Log lines carry outcome words, the message name, attempt
numbers and exception class names only, never message content.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Generic, Literal, Protocol, TypeVar

from .clock import LocalClock
from .config import Settings
from .garmin import exception_kind
from .push import PushMessage, PushSender, TelegramPush

log = logging.getLogger("coach_mcp.briefing")


class Composed(Protocol):
    @property
    def message(self) -> PushMessage: ...

    @property
    def waiting(self) -> bool: ...


C = TypeVar("C", bound=Composed)
Step = Literal["done", "retry", "stop"]


@dataclass(frozen=True)
class Attempt:
    day: date  # the local day the message is for
    now: datetime  # the start of this scheduling step, in the zone
    number: int  # 1 for the first attempt of this message today
    final: bool  # at or after the cutoff: no further attempt today


def _always(day: date) -> bool:
    return True


@dataclass(frozen=True)
class DailyMessage(Generic[C]):
    name: str  # the word of the log lines: "briefing", "weekly"
    compose: Callable[[Attempt], Awaitable[C]]
    already_sent: Callable[[date], bool]
    record: Callable[[date, C], None]
    due: Callable[[date], bool] = _always
    crash: Literal["raise", "retry"] = "retry"


@dataclass
class _State:
    attempts: int = 0
    sent: bool = False  # went out today in this process (also when recording it failed)


class DailyDelivery:
    def __init__(
        self,
        clock: LocalClock,
        push: PushSender,
        messages: Sequence[DailyMessage[Any]],
        *,
        start: time,
        cutoff: time,
        retry_seconds: float,
    ) -> None:
        self._clock = clock
        self._push = push
        self._messages = tuple(messages)
        self._start = start
        self._cutoff = cutoff
        self._retry = retry_seconds
        self._day: date | None = None
        self._window_seen = False
        self._late_start = False
        self._stopped = False
        self._states: dict[str, _State] = {}

    async def tick(self) -> float:
        """One scheduling step; returns the seconds to wait before the next one."""
        clock = self._clock
        now = clock.now()
        day = now.date()
        if day != self._day:
            self._day, self._window_seen, self._late_start, self._stopped = day, False, False, False
            self._states = {message.name: _State() for message in self._messages}
        start = clock.at(day, self._start)
        cutoff = clock.at(day, self._cutoff)
        to_tomorrow = clock.until(clock.next_at(self._start, now), now)

        if self._stopped:
            return to_tomorrow
        if clock.until(start, now) > 0:
            return clock.until(start, now)
        final = clock.until(cutoff, now) <= 0
        # Remember, before anything that can raise, whether the first look inside today's window was already past
        # the cutoff: a store error on that look must neither turn a late start into a late send (retry is "final"
        # too) nor an ordinary start into a "late start".
        if not self._window_seen:
            self._window_seen = True
            self._late_start = final
        retry = min(self._retry, max(clock.until(cutoff, now), 0.0))
        for message in self._messages:
            step = await self._step(message, day, now, final)
            if step == "retry":
                return retry
            if step == "stop":
                break
        self._stopped = True
        return to_tomorrow

    async def _step(self, message: DailyMessage[Any], day: date, now: datetime, final: bool) -> Step:
        state = self._states[message.name]
        name = message.name
        attempting = False
        try:
            if state.sent or not message.due(day) or message.already_sent(day):
                return "done"
            if self._late_start:
                log.info("%s status=skipped reason=late_start", name)
                return "stop"
            attempting = True
            state.attempts += 1
            composed = await message.compose(Attempt(day, now, state.attempts, final))
            if composed.waiting and not final:
                log.info("%s status=waiting attempt=%d", name, state.attempts)
                return "retry"
            outcome = await self._push.send(composed.message)
        except Exception as exc:  # BaseException (cancellation) propagates
            if message.crash == "raise":
                if final and attempting:  # no more attempts today: do not crash-loop until midnight
                    self._stopped = True
                raise
            log.error("%s status=crashed kind=%s", name, exception_kind(exc))
            return "stop" if final else "retry"

        if outcome == "sent":
            state.sent = True
            try:
                message.record(day, composed)
            except Exception as exc:
                log.error("%s status=state_error kind=%s", name, exception_kind(exc))
            log.info("%s status=sent attempt=%d", name, state.attempts)
            return "done"
        if outcome == "permanent":
            log.warning("%s status=gave_up reason=push_permanent", name)
            return "stop"
        if final:
            log.warning("%s status=gave_up reason=push_transient", name)
            return "stop"
        log.info("%s status=waiting attempt=%d", name, state.attempts)
        return "retry"


async def send_now(
    settings: Settings,
    name: str,
    compose: Callable[[date], Awaitable[C]],
    summary: Callable[[C], str],
    *,
    push: PushSender | None = None,
    today: Callable[[], date] | None = None,
) -> int:
    """Compose today's message and push it now; writes no state. Exit codes: 0 sent, 1 failed, 2 not configured."""
    if not settings.push_enabled:
        print("push is not configured: set COACH_TELEGRAM_CHAT_ID", file=sys.stderr)
        return 2
    day = today or LocalClock(settings.timezone).today
    try:
        composed = await compose(day())
        outcome = await (push or TelegramPush(settings)).send(composed.message)
    except Exception as exc:
        log.error("%s status=crashed kind=%s", name, exception_kind(exc))
        return 1
    print(f"{name} status={outcome} {summary(composed)}")
    return 0 if outcome == "sent" else 1
