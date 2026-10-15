"""The rules for changing the training plan: marking a planned session and adding a note.

Both ways in call this module and keep no rule of their own: the MCP tools (``tools.update_session`` and
``tools.add_note``) and the athlete's Telegram messages (``inbound.py``: ``/done``, ``/skip`` and free text). Each
call checks and writes in one SQLite write transaction (``Store.transaction``, ``BEGIN IMMEDIATE``), so no other
writer can change the session between the check and the write. A call returns what it wrote, or a ``Refusal``
whose ``Reason`` comes from a closed set; the caller puts the reason into its own words (the English ``ToolError``
texts in ``tools.py``, the ``ReplyTexts`` catalogue in ``inbound.py``). The clock is the caller's: ``today`` and
the note's day are passed in, never read here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from .store import Store

NOTE_MAX = 2000  # characters after trimming; the notes.text CHECK of the schema allows 1 to 2000
STATUSES = ("planned", "done", "skipped", "moved")


class Reason(StrEnum):
    """Why a call changed nothing."""

    NOT_FOUND = "not_found"  # no session with that id
    SUPERSEDED = "superseded"  # a later plan replaced the session
    MOVED = "moved"  # the session was moved and its copy cannot be found
    MOVED_TO = "moved_to"  # the session was moved to another session (copy_id, copy_day)
    COPY_SUPERSEDED = "copy_superseded"  # moved, and a later plan replaced that copy
    FUTURE = "future"  # done for a session dated after today (day)
    NOT_PLANNED = "not_planned"  # moved, but the session is not planned
    SAME_DATE = "same_date"  # moved to the date it already has
    PAST_DATE = "past_date"  # moved to a date before today
    TEXT_EMPTY = "text_empty"  # the note is empty after trimming
    TEXT_NUL = "text_nul"  # the note contains a NUL character
    TEXT_TOO_LONG = "text_too_long"  # the note is longer than NOTE_MAX after trimming (length)


@dataclass(frozen=True)
class Refusal:
    reason: Reason
    session_id: int | None = None
    day: date | None = None  # FUTURE: the session's date
    copy_id: int | None = None  # MOVED_TO, COPY_SUPERSEDED
    copy_day: date | None = None  # MOVED_TO, COPY_SUPERSEDED
    length: int | None = None  # TEXT_TOO_LONG: characters after trimming


@dataclass(frozen=True)
class Marked:
    """The rows as stored by the same transaction."""

    session: dict[str, Any]
    copy: dict[str, Any] | None  # moved: the new planned session
    note: dict[str, Any] | None


@dataclass(frozen=True)
class Noted:
    note: dict[str, Any]


def note_text(text: str) -> str | Refusal:
    """The note as it is stored (trimmed), or why it cannot be. A NUL is named before the length."""
    body = text.strip()
    if "\x00" in body:  # SQLite's length() stops at NUL, so the CHECK would not give a clear answer
        return Refusal(Reason.TEXT_NUL)
    if not body:
        return Refusal(Reason.TEXT_EMPTY)
    if len(body) > NOTE_MAX:
        return Refusal(Reason.TEXT_TOO_LONG, length=len(body))
    return body


def mark_session(
    store: Store,
    today: date,
    session_id: int,
    status: str,
    *,
    new_date: date | None = None,
    note: str | None = None,
) -> Marked | Refusal:
    """Set ``status`` (planned, done, skipped, or moved with ``new_date``) and store ``note`` with the session.

    ``new_date`` is given with ``moved`` and only then; anything else is a programming error (``ValueError``). The
    note is checked first, then the session, in the order of ``Reason``.
    """
    if status not in STATUSES or (status == "moved") != (new_date is not None):
        raise ValueError("mark_session: new_date goes with status 'moved' and only with it")
    body = None
    if note is not None:
        checked = note_text(note)
        if isinstance(checked, Refusal):
            return checked
        body = checked
    with store.transaction() as tx:
        row = tx.get_session(session_id)
        if row is None:
            return Refusal(Reason.NOT_FOUND, session_id)
        if row["status"] == "superseded":
            return Refusal(Reason.SUPERSEDED, session_id)
        if row["status"] == "moved":
            link = tx.moved_to(session_id)
            if link is None:
                return Refusal(Reason.MOVED, session_id)
            copy_id, copy_day = link[0], date.fromisoformat(link[1])
            copy = tx.get_session(copy_id)
            if copy is not None and copy["status"] == "superseded":
                return Refusal(Reason.COPY_SUPERSEDED, session_id, copy_id=copy_id, copy_day=copy_day)
            return Refusal(Reason.MOVED_TO, session_id, copy_id=copy_id, copy_day=copy_day)
        day = date.fromisoformat(row["date"])
        if new_date is not None:
            if row["status"] != "planned":
                return Refusal(Reason.NOT_PLANNED, session_id)
            if new_date == day:
                return Refusal(Reason.SAME_DATE, session_id)
            if new_date < today:
                return Refusal(Reason.PAST_DATE, session_id)
        if status == "done" and day > today:
            return Refusal(Reason.FUTURE, session_id, day=day)
        copy_id_, note_id = tx.update_session_status(
            session_id, status, new_date, (today, body) if body is not None else None
        )
        return Marked(
            session=_must(tx.get_session(session_id)),
            copy=None if copy_id_ is None else _must(tx.get_session(copy_id_)),
            note=None if note_id is None else _must(tx.get_note(note_id)),
        )


def add_note(
    store: Store,
    day: date,
    text: str,
    *,
    session_id: int | None = None,
    inbound: tuple[str, int, datetime] | None = None,
) -> Noted | Refusal:
    """Store a note for ``day``, optionally linked to a session. ``inbound``: (bot id, next update id, now) of the
    Telegram update the note came from, stored in the same transaction, so a restart can neither lose the note nor
    store it twice."""
    checked = note_text(text)
    if isinstance(checked, Refusal):
        return checked
    with store.transaction() as tx:
        if session_id is not None and tx.get_session(session_id) is None:
            return Refusal(Reason.NOT_FOUND, session_id)
        note_id = tx.insert_note(day, checked, session_id)
        if inbound is not None:
            tx.set_inbound(*inbound)
        return Noted(_must(tx.get_note(note_id)))


def _must(row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None:  # pragma: no cover - a row this transaction just wrote must exist
        raise LookupError("a row written by this transaction is missing")
    return row
