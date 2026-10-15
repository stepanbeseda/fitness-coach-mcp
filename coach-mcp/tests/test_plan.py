"""The Plan module: every rule and refusal of marking a session and adding a note, against a temporary Store."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from typing import Any

import pytest

from coach_mcp import plan
from coach_mcp.plan import Marked, Noted, Reason, Refusal

TODAY = date(2026, 10, 7)
AT = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
TABLES = ("sessions", "notes", "telegram_inbound")


def seed(store: Any, day: str, status: str = "planned", moved_from_id: int | None = None) -> int:
    with sqlite3.connect(store.path) as conn:
        cur = conn.execute(
            "INSERT INTO sessions (date, type, description, status, moved_from_id, created_at, updated_at) "
            "VALUES (?, 'running', 'Run', ?, ?, '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00')",
            (day, status, moved_from_id),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid


def snapshot(store: Any) -> dict[str, list[Any]]:
    with sqlite3.connect(store.path) as conn:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in TABLES}


def refused(outcome: Marked | Noted | Refusal) -> Refusal:
    assert isinstance(outcome, Refusal), outcome
    return outcome


# ------------------------------------------------------------------ refusals of mark_session


def test_every_session_refusal_writes_nothing(store):
    planned_id = seed(store, "2026-10-08")
    done_id = seed(store, "2026-10-06", status="done")
    gone_id = seed(store, "2026-10-06", status="superseded")
    lost_id = seed(store, "2026-10-06", status="moved")  # moved without a copy row
    moved_id = seed(store, "2026-10-08", status="moved")  # dated after today: moved is named before future
    copy_id = seed(store, "2026-10-09", moved_from_id=moved_id)
    replaced_id = seed(store, "2026-10-06", status="moved")
    replaced_copy = seed(store, "2026-10-10", status="superseded", moved_from_id=replaced_id)
    before = snapshot(store)
    cases = [
        ((999, "done"), {}, Refusal(Reason.NOT_FOUND, 999)),
        ((gone_id, "done"), {}, Refusal(Reason.SUPERSEDED, gone_id)),
        ((lost_id, "skipped"), {}, Refusal(Reason.MOVED, lost_id)),
        ((moved_id, "done"), {}, Refusal(Reason.MOVED_TO, moved_id, copy_id=copy_id, copy_day=date(2026, 10, 9))),
        (
            (replaced_id, "done"),
            {},
            Refusal(Reason.COPY_SUPERSEDED, replaced_id, copy_id=replaced_copy, copy_day=date(2026, 10, 10)),
        ),
        ((planned_id, "done"), {}, Refusal(Reason.FUTURE, planned_id, day=date(2026, 10, 8))),
        ((done_id, "moved"), {"new_date": date(2026, 10, 9)}, Refusal(Reason.NOT_PLANNED, done_id)),
        ((planned_id, "moved"), {"new_date": date(2026, 10, 8)}, Refusal(Reason.SAME_DATE, planned_id)),
        ((planned_id, "moved"), {"new_date": date(2026, 10, 6)}, Refusal(Reason.PAST_DATE, planned_id)),
        ((planned_id, "done"), {"note": "   "}, Refusal(Reason.TEXT_EMPTY)),
        ((999, "done"), {"note": "a\x00"}, Refusal(Reason.TEXT_NUL)),  # the note is checked before the session
        ((planned_id, "done"), {"note": "x" * 2001}, Refusal(Reason.TEXT_TOO_LONG, length=2001)),
    ]
    assert {expected.reason for _args, _kw, expected in cases} == set(Reason)
    for args, kwargs, expected in cases:
        assert plan.mark_session(store, TODAY, *args, **kwargs) == expected, expected.reason
    assert snapshot(store) == before


def test_done_is_refused_only_after_today(store):
    yesterday, today, tomorrow = seed(store, "2026-10-06"), seed(store, "2026-10-07"), seed(store, "2026-10-08")
    assert isinstance(plan.mark_session(store, TODAY, yesterday, "done"), Marked)
    assert isinstance(plan.mark_session(store, TODAY, today, "done"), Marked)
    assert refused(plan.mark_session(store, TODAY, tomorrow, "done")).reason is Reason.FUTURE
    for status in ("skipped", "planned"):  # only done waits for the day
        marked = plan.mark_session(store, TODAY, tomorrow, status)
        assert isinstance(marked, Marked) and marked.session["status"] == status
    assert refused(plan.mark_session(store, TODAY, tomorrow, "done")).reason is Reason.FUTURE


def test_a_move_to_today_is_allowed_and_creates_the_copy(store):
    sid = seed(store, "2026-10-06")
    marked = plan.mark_session(store, TODAY, sid, "moved", new_date=TODAY, note="  knee ")
    assert isinstance(marked, Marked) and marked.copy is not None and marked.note is not None
    assert marked.session["status"] == "moved"
    assert (marked.copy["date"], marked.copy["status"], marked.copy["moved_from_id"]) == ("2026-10-07", "planned", sid)
    assert (marked.note["text"], marked.note["date"], marked.note["session_id"]) == ("knee", "2026-10-07", sid)


def test_a_done_session_can_go_back_to_planned_and_be_corrected(store):
    sid = seed(store, "2026-10-06", status="done")
    assert plan.mark_session(store, TODAY, sid, "skipped") == Marked(
        session=store.get_session(sid), copy=None, note=None
    )
    assert store.get_session(sid)["status"] == "skipped"
    assert isinstance(plan.mark_session(store, TODAY, sid, "planned"), Marked)


def test_new_date_goes_with_moved_only(store):
    sid = seed(store, "2026-10-06")
    with pytest.raises(ValueError):
        plan.mark_session(store, TODAY, sid, "moved")
    with pytest.raises(ValueError):
        plan.mark_session(store, TODAY, sid, "done", new_date=TODAY)
    with pytest.raises(ValueError):
        plan.mark_session(store, TODAY, sid, "superseded")


def test_the_status_and_its_note_are_one_transaction(store):
    sid = seed(store, "2026-10-06")
    before = snapshot(store)
    with sqlite3.connect(store.path) as conn:
        conn.execute("CREATE TRIGGER notes_fail BEFORE INSERT ON notes BEGIN SELECT RAISE(ABORT, 'test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        plan.mark_session(store, TODAY, sid, "moved", new_date=date(2026, 10, 9), note="n")
    assert snapshot(store) == before  # no status change, no copy, no note


# ------------------------------------------------------------------ add_note


def test_note_text_rules_in_order():
    assert plan.note_text("  hello \n") == "hello"
    assert plan.note_text("ž" * 2000) == "ž" * 2000  # characters, not bytes
    assert plan.note_text(" " + "x" * 2000 + " ") == "x" * 2000  # measured after trimming
    assert plan.note_text("x" * 2001) == Refusal(Reason.TEXT_TOO_LONG, length=2001)
    assert plan.note_text(" \n ") == Refusal(Reason.TEXT_EMPTY)
    assert plan.note_text("\x00") == Refusal(Reason.TEXT_NUL)
    assert plan.note_text("a\x00" + "b" * 2000) == Refusal(Reason.TEXT_NUL)  # NUL is named before the length


def test_add_note_refusals_write_nothing(store):
    store.advance_inbound("111111111", 10, AT)
    before = snapshot(store)
    inbound = ("111111111", 11, AT)
    assert plan.add_note(store, TODAY, "x", session_id=999) == Refusal(Reason.NOT_FOUND, 999)
    assert plan.add_note(store, TODAY, "x" * 2001, inbound=inbound) == Refusal(Reason.TEXT_TOO_LONG, length=2001)
    assert plan.add_note(store, TODAY, "a\x00b", inbound=inbound) == Refusal(Reason.TEXT_NUL)
    assert plan.add_note(store, TODAY, "  ", session_id=999) == Refusal(Reason.TEXT_EMPTY)  # text before session
    assert snapshot(store) == before


def test_add_note_stores_the_trimmed_text_for_the_given_day(store):
    sid = seed(store, "2026-10-08")
    noted = plan.add_note(store, date(2026, 9, 1), "  felt heavy ", session_id=sid)
    assert isinstance(noted, Noted)
    assert (noted.note["date"], noted.note["text"], noted.note["session_id"]) == ("2026-09-01", "felt heavy", sid)
    assert store.inbound_state() is None  # no offset without inbound


def test_a_note_and_its_offset_are_one_transaction(store):
    store.advance_inbound("111111111", 10, AT)
    before = snapshot(store)
    with pytest.raises(sqlite3.IntegrityError):  # the offset fails its CHECK: the note must not stay
        plan.add_note(store, TODAY, "kept?", inbound=("", 11, AT))
    assert snapshot(store) == before
    noted = plan.add_note(store, TODAY, "both", inbound=("111111111", 11, AT))
    assert isinstance(noted, Noted) and store.inbound_state()["next_update_id"] == 11
    assert [n["id"] for n in store.latest_notes(5)] == [noted.note["id"]]
