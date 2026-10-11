"""Two-way Telegram: the poller and the commands, driven step by step through a fake Bot API (no network)."""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx2
import pytest
from fakes import FakeGarmin, FakeTelegram, StopLoop
from garmin_payloads import garmin_data

from coach_mcp import inbound as inbound_module
from coach_mcp.__main__ import configure_logging
from coach_mcp.config import Settings
from coach_mcp.inbound import (
    NOTE_MAX,
    InboundPoller,
    parse_command,
)
from coach_mcp.messages import REPLY_CS, REPLY_EN
from coach_mcp.push import TelegramPush
from coach_mcp.scheduler import compose_briefing
from coach_mcp.store import Store

PRAGUE = ZoneInfo("Europe/Prague")
OWNER = 123456789
BOT_ID = "111111111"
TOKEN = f"{BOT_ID}:AAfake-token-fake-token-fake-token-1"
OTHER_BOT_ID = "222222222"
OTHER_TOKEN = f"{OTHER_BOT_ID}:BBfake-token-fake-token-fake-token-2"
D = date(2026, 10, 7)  # Wednesday of ISO week 2026-W41 (Mon 5 Oct .. Sun 11 Oct)
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=PRAGUE)
SECRET = "tajny-text-zpravy"


def message(update_id: int, text: Any = "ahoj", *, chat: Any = OWNER, sender: Any = OWNER, kind: str = "private",
            sent: Any = None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "message_id": update_id,
        "from": {"id": sender, "is_bot": False, "first_name": "A"},
        "chat": {"id": chat, "type": kind},
        "date": int(NOW.timestamp()) if sent is None else sent,
    }
    if text is not None:
        body["text"] = text
    body.update(extra)
    return {"update_id": update_id, "message": body}


class Bench:
    """One poller wired to a fake Bot API, a fake Garmin and a real store; the replies use the real TelegramPush."""

    def __init__(self, tmp_path: Path, telegram: FakeTelegram | None = None, *, language: str = "en",
                 token: str | None = TOKEN, garmin: FakeGarmin | None = None, started: bool = True) -> None:
        self.token_file = tmp_path / "telegram_bot_token"
        if token is not None:
            self.token_file.write_text(token + "\n", encoding="utf-8")
        self.settings = Settings(
            db_path=tmp_path / "coach.db", timezone="Europe/Prague", language=language,
            telegram_chat_id=str(OWNER), telegram_token_file=self.token_file,
        )
        self.store = Store(self.settings.db_path)
        self.telegram = telegram or FakeTelegram()
        self.garmin = garmin or FakeGarmin(garmin_data())
        self.now = NOW
        self.sleeps: list[float] = []
        if started:  # past the first-start drain: the offset row exists
            self.store.advance_inbound(BOT_ID, None, NOW)
        self.poller = self.new_poller()

    def new_poller(self) -> InboundPoller:
        return InboundPoller(
            self.settings, self.store, self.garmin,
            TelegramPush(self.settings, transport=self.telegram.transport()),
            now=lambda: self.now, sleep=self._sleep, transport=self.telegram.transport(),
        )

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    async def say(self, text: Any, update_id: int = 100, **kwargs: Any) -> str:
        """Deliver one message of the owner, run one step and return the one reply."""
        self.telegram.pending.append(message(update_id, text, **kwargs))
        before = len(self.telegram.sent)
        assert await self.poller.step() == 0.0
        (reply,) = self.telegram.texts[before:]
        return reply

    def notes(self) -> list[tuple[str, str]]:
        return [(n["date"], n["text"]) for n in reversed(self.store.latest_notes(50))]

    def offset(self) -> int | None:
        state = self.store.inbound_state()
        assert state is not None
        return state["next_update_id"]

    def seed(self, *rows: dict[str, Any]) -> list[int]:
        """Planned sessions, inserted without superseding anything."""
        days = [row["date"] for row in rows]
        return self.store.replace_planned(min(days), max(days), max(days) + timedelta(days=1), list(rows))[1]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


pytestmark = pytest.mark.anyio


# ------------------------------------------------------------ parse_command


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ahoj", (None, "ahoj")),
        ("  dnes bolelo koleno \n", (None, "dnes bolelo koleno")),
        ("/dnes", ("dnes", "")),
        ("/DNES", ("dnes", "")),
        ("/today", ("today", "")),
        ("/plan@coach_bot", ("plan", "")),
        ("/done 12", ("done", "12")),
        ("/done   #12  ", ("done", "#12")),
        ("/skip 12 knee\nhurts", ("skip", "12 knee\nhurts")),
        ("/done@coach_bot 12", ("done", "12")),
        ("/", ("", "")),
        ("/ dnes", ("", "")),
        ("/dnes!", ("", "")),
        ("/" + "a" * 33, ("", "")),
        ("/" + "a" * 32, ("a" * 32, "")),
        ("dnes /plan", (None, "dnes /plan")),
    ],
)
def test_parse_command(text, expected):
    assert parse_command(text) == expected


# ------------------------------------------------- criterion 1: access, offset


async def test_first_start_discards_the_backlog_and_then_handles_new_messages(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.inbound")
    backlog = [message(i, f"/start {SECRET}") for i in range(40, 40 + 23)]
    bench = Bench(tmp_path, FakeTelegram(backlog), started=False)
    assert bench.store.inbound_state() is None
    assert await bench.poller.step() == 0.0
    assert bench.notes() == [] and bench.telegram.sent == []
    assert bench.store.inbound_state() == {
        "bot_id": BOT_ID, "next_update_id": 63, "advanced_at": NOW.astimezone(UTC).isoformat(timespec="seconds"),
    }
    assert "inbound status=started discarded=23" in caplog.text
    # the drain asks without waiting and without an offset first, then from the id after the last batch
    assert [(p.get("offset"), p["timeout"]) for p in bench.telegram.polls] == [(None, 0), (50, 0), (60, 0), (63, 0)]
    assert await bench.say("po startu", 63) == "Note saved (7 Oct)."
    assert bench.telegram.polls[-1] == {
        "timeout": 50, "limit": 10, "allowed_updates": ["message"], "offset": 63,
    }
    assert bench.notes() == [("2026-10-07", "po startu")]


async def test_first_start_with_an_empty_queue_stores_no_offset_and_asks_without_one(tmp_path):
    bench = Bench(tmp_path, started=False)
    assert await bench.poller.step() == 0.0
    assert bench.offset() is None
    assert await bench.say("prvni", 500) == "Note saved (7 Oct)."
    assert "offset" not in bench.telegram.polls[-1] and bench.telegram.polls[-1]["timeout"] == 50
    assert bench.offset() == 501
    await bench.poller.step()
    assert bench.telegram.polls[-1]["offset"] == 501


async def test_a_failed_drain_stores_nothing_and_starts_again(tmp_path):
    telegram = FakeTelegram([message(7, SECRET)], script=[httpx2.Response(500)])
    bench = Bench(tmp_path, telegram, started=False)
    assert await bench.poller.step() == 5.0
    assert bench.store.inbound_state() is None
    assert await bench.poller.step() == 0.0
    assert bench.offset() == 8 and bench.notes() == []


async def test_a_backlog_that_never_ends_is_drained_in_fifty_batches_and_then_handled(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.inbound")
    endless = [httpx2.Response(200, json={"ok": True, "result": [message(i, SECRET)]}) for i in range(1, 61)]
    bench = Bench(tmp_path, FakeTelegram(script=endless), started=False)
    assert await bench.poller.step() == 0.0
    assert len(bench.telegram.polls) == 50 and all(p["timeout"] == 0 for p in bench.telegram.polls)
    assert bench.offset() == 51 and "inbound status=started discarded=50" in caplog.text
    assert bench.notes() == [] and bench.telegram.sent == []
    assert await bench.say("po fronte", 51) == "Note saved (7 Oct)."  # the first long poll asks from the stored offset
    assert bench.telegram.polls[50]["offset"] == 51 and bench.telegram.polls[50]["timeout"] == 50


async def test_the_offset_survives_a_restart(tmp_path):
    bench = Bench(tmp_path)
    assert await bench.say("pred restartem", 10) == "Note saved (7 Oct)."
    # The process ends before the next getUpdates: Telegram still holds update 10 (it was never confirmed).
    assert [u["update_id"] for u in bench.telegram.pending] == [10]
    bench.telegram.pending.append(message(11, "po restartu"))
    bench.poller = bench.new_poller()  # same database, same Telegram
    assert await bench.poller.step() == 0.0
    assert bench.telegram.polls[-1]["offset"] == 11 and bench.telegram.polls[-1]["timeout"] == 50
    assert bench.notes() == [("2026-10-07", "pred restartem"), ("2026-10-07", "po restartu")]
    assert bench.offset() == 12


MIB = 1024 * 1024  # the size limit of an answer, written out so that a change of the constant is seen
OK_EMPTY = b'{"ok": true, "result": []}'

IGNORED = [
    pytest.param(message(1, SECRET, chat=987654321, sender=987654321), id="another private chat"),
    pytest.param(message(1, SECRET, sender=987654321), id="another sender in the owner's chat"),
    pytest.param(message(1, SECRET, chat=987654321), id="the owner in another chat"),
    pytest.param(message(1, SECRET, kind="group"), id="a group with the owner's ids"),
    pytest.param(message(1, SECRET, kind="supergroup", chat=-100123456789), id="a supergroup"),
    pytest.param(message(1, SECRET, chat=str(OWNER)), id="chat id as a string"),
    pytest.param(message(1, SECRET, sender=str(OWNER)), id="sender id as a string"),
    pytest.param(message(1, SECRET, chat=float(OWNER)), id="chat id as a float"),
    pytest.param({"update_id": 1, "message": {**message(1, SECRET)["message"], "from": {"id": OWNER, "is_bot": True}}},
                 id="a bot as the sender"),
    pytest.param({"update_id": 1, "message": {**message(1, SECRET)["message"], "chat": [OWNER]}}, id="chat is a list"),
    pytest.param({"update_id": 1, "message": {**message(1, SECRET)["message"], "from": "me"}}, id="sender is a text"),
    pytest.param({"update_id": 1, "message": {"chat": {"id": OWNER, "type": "private"}, "text": SECRET}},
                 id="no sender"),
    pytest.param({"update_id": 1, "message": {"from": {"id": OWNER}, "text": SECRET}}, id="no chat"),
    pytest.param({"update_id": 1, "edited_message": message(1, SECRET)["message"]}, id="an edited message"),
    pytest.param({"update_id": 1, "channel_post": message(1, SECRET)["message"]}, id="a channel post"),
    pytest.param({"update_id": 1, "message": SECRET}, id="a message that is not an object"),
    pytest.param({"update_id": 1}, id="an update with nothing in it"),
]


@pytest.mark.parametrize("update", IGNORED)
async def test_messages_of_anyone_else_are_ignored_and_never_logged(tmp_path, caplog, restore_logging, update):
    configure_logging("DEBUG")
    caplog.set_level(logging.DEBUG)
    bench = Bench(tmp_path, FakeTelegram([update, message(2, "moje")]))
    assert await bench.poller.step() == 0.0
    assert bench.telegram.texts == ["Note saved (7 Oct)."]  # the one reply belongs to the owner's message
    assert bench.notes() == [("2026-10-07", "moje")] and bench.offset() == 3
    assert "inbound status=ignored count=1" in caplog.text
    for forbidden in (SECRET, "moje", TOKEN, BOT_ID, str(OWNER), "987654321", "api.telegram.org"):
        assert forbidden not in caplog.text, forbidden


async def test_true_is_not_chat_id_one(tmp_path):
    bench = Bench(tmp_path, FakeTelegram([message(1, SECRET, chat=True, sender=True)]))
    bench.poller._owner = 1  # the only owner id a bool could equal
    assert await bench.poller.step() == 0.0
    assert bench.telegram.sent == [] and bench.notes() == []


async def test_an_ignored_update_is_confirmed_on_its_own(tmp_path):
    bench = Bench(tmp_path, FakeTelegram([message(5, SECRET, chat=987654321, sender=987654321)]))
    assert await bench.poller.step() == 0.0
    assert bench.offset() == 6 and bench.telegram.sent == []


async def test_log_lines_carry_outcome_words_only(tmp_path, caplog, restore_logging):
    configure_logging("DEBUG")
    caplog.set_level(logging.DEBUG)
    bench = Bench(tmp_path)
    (sid,) = bench.seed({"date": D, "type": "running", "description": SECRET})
    for update_id, text in enumerate(
        (SECRET, f"/{SECRET}", f"/done {SECRET}", f"/done {sid}", "/plan", f"/plan {SECRET}", "/dnes", "/help"), 1
    ):
        await bench.say(text, update_id)
    lines = [r.getMessage() for r in caplog.records if r.name == "coach_mcp.inbound"]
    words = [re.sub(r" ms=\d+$", "", line) for line in lines]
    assert words == [
        "inbound cmd=note status=ok reply=sent",
        "inbound cmd=unknown status=unknown reply=sent",
        "inbound cmd=done status=bad_argument reply=sent",
        "inbound cmd=done status=ok reply=sent",
        "inbound cmd=plan status=ok reply=sent",
        "inbound cmd=plan status=bad_argument reply=sent",
        "inbound cmd=today status=ok reply=sent",
        "inbound cmd=help status=ok reply=sent",
    ]
    assert all(re.fullmatch(r"inbound cmd=\w+ status=\w+ reply=sent ms=\d+", line) for line in lines)
    for forbidden in (SECRET, TOKEN, BOT_ID, str(OWNER), "api.telegram.org", "Note saved", "Readiness"):
        assert forbidden not in caplog.text, forbidden


# ----------------------------------------------------- offset age, other bot


@pytest.mark.parametrize(
    ("age", "sends_offset"),
    [(timedelta(days=2) - timedelta(seconds=1), True), (timedelta(days=2), False), (timedelta(days=30), False)],
)
async def test_an_old_offset_is_not_sent(tmp_path, age, sends_offset):
    bench = Bench(tmp_path)
    bench.store.advance_inbound(BOT_ID, 900, NOW - age)
    # After a week without updates Telegram may restart the ids below the stored offset.
    bench.telegram.pending.append(message(17, "po dlouhe dobe"))
    await bench.poller.step()
    assert ("offset" in bench.telegram.polls[-1]) is sends_offset
    if sends_offset:
        assert bench.telegram.polls[-1]["offset"] == 900 and bench.notes() == []
    else:
        assert bench.notes() == [("2026-10-07", "po dlouhe dobe")] and bench.offset() == 18


async def test_another_bot_starts_from_scratch(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="coach_mcp.inbound")
    bench = Bench(tmp_path)
    bench.store.advance_inbound(BOT_ID, 900, NOW)
    bench.token_file.write_text(OTHER_TOKEN + "\n", encoding="utf-8")
    bench.telegram.pending.append(message(3, SECRET))  # what the new bot had queued: its set-up messages
    assert await bench.poller.step() == 0.0
    assert bench.telegram.polls[0] == {"timeout": 0, "limit": 10, "allowed_updates": ["message"]}
    assert bench.store.inbound_state()["bot_id"] == OTHER_BOT_ID and bench.offset() == 4
    assert bench.notes() == [] and "inbound status=started discarded=1" in caplog.text
    assert all(url == f"https://api.telegram.org/bot{OTHER_TOKEN}/getUpdates" for url in bench.telegram.urls)


# --------------------------------------------------------------- failures


@pytest.mark.parametrize(
    ("answer", "wait", "word"),
    [
        (httpx2.Response(401), 300.0, "inbound poll=unauthorized code=401"),
        (httpx2.Response(404), 300.0, "inbound poll=unauthorized code=404"),
        (httpx2.Response(400), 300.0, "inbound poll=rejected code=400"),
        (httpx2.Response(403), 300.0, "inbound poll=rejected code=403"),
        (httpx2.Response(302), 300.0, "inbound poll=rejected code=302"),
        (httpx2.Response(409), 60.0, "inbound poll=conflict code=409"),
        (httpx2.Response(429), 5.0, "inbound poll=transient code=429"),
        (httpx2.Response(500), 5.0, "inbound poll=transient code=500"),
        (httpx2.Response(503), 5.0, "inbound poll=transient code=503"),
        (httpx2.ReadTimeout("slow"), 5.0, "inbound poll=transient kind=ReadTimeout"),
        (httpx2.ConnectError("down"), 5.0, "inbound poll=transient kind=ConnectError"),
        (OSError("reset"), 5.0, "inbound poll=transient kind=OSError"),
        (httpx2.Response(200, content=b"<html>"), 5.0, "inbound poll=bad_answer"),
        (httpx2.Response(200, json={"ok": False, "result": []}), 5.0, "inbound poll=bad_answer"),
        (httpx2.Response(200, json={"ok": True}), 5.0, "inbound poll=bad_answer"),
        (httpx2.Response(200, json={"ok": True, "result": {}}), 5.0, "inbound poll=bad_answer"),
        (httpx2.Response(200, json=[]), 5.0, "inbound poll=bad_answer"),
        (httpx2.Response(200, json={"ok": True, "result": [{"message": {}}]}), 5.0, "inbound poll=bad_answer"),
        (httpx2.Response(200, json={"ok": True, "result": [{"update_id": "7"}]}), 5.0, "inbound poll=bad_answer"),
        (httpx2.Response(200, json={"ok": True, "result": [{"update_id": True}]}), 5.0, "inbound poll=bad_answer"),
        (httpx2.Response(200, json={"ok": True, "result": [{"update_id": -1}]}), 5.0, "inbound poll=bad_answer"),
        (httpx2.Response(200, json={"ok": True, "result": ["x"]}), 5.0, "inbound poll=bad_answer"),
        (httpx2.Response(200, content=b"[" * 100_000), 5.0, "inbound poll=bad_answer"),
        (httpx2.Response(200, content=OK_EMPTY + b" " * (MIB + 1 - len(OK_EMPTY))), 5.0,
         "inbound poll=bad_answer"),  # valid JSON, one byte over the limit
    ],
)
async def test_no_answer_of_telegram_stops_the_loop(tmp_path, caplog, restore_logging, answer, wait, word):
    configure_logging("DEBUG")
    caplog.set_level(logging.DEBUG)
    bench = Bench(tmp_path, FakeTelegram([message(1, "pocka")], script=[answer]))
    assert await bench.poller.step() == wait
    assert word in caplog.text
    for forbidden in ("slow", "down", "reset", TOKEN, BOT_ID, "api.telegram.org", "pocka"):
        assert forbidden not in caplog.text, forbidden
    assert bench.notes() == [] and bench.offset() is None
    assert await bench.poller.step() == 0.0  # the update was not lost
    assert bench.notes() == [("2026-10-07", "pocka")]


async def test_an_answer_of_exactly_the_size_limit_is_read(tmp_path):
    padded = OK_EMPTY + b" " * (MIB - len(OK_EMPTY))
    bench = Bench(tmp_path, FakeTelegram(script=[httpx2.Response(200, content=padded)]))
    assert await bench.poller.step() == 1.0


async def test_an_endless_answer_is_not_read_into_memory(tmp_path):
    pulled: list[int] = []

    async def chunks():
        for _ in range(256):  # 16 MiB if all of it were read
            pulled.append(1)
            yield b" " * 65536

    bench = Bench(tmp_path, FakeTelegram(script=[httpx2.Response(200, content=chunks())]))
    assert await bench.poller.step() == 5.0
    assert len(pulled) <= MIB // 65536 + 2


async def test_the_read_timeout_is_above_the_long_poll(tmp_path):
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(dict(request.extensions["timeout"]))
        return httpx2.Response(200, content=OK_EMPTY)

    bench = Bench(tmp_path, started=False)
    bench.poller._transport = httpx2.MockTransport(handler)
    await bench.poller.step()  # the drain: no long poll
    await bench.poller.step()  # the long poll
    assert seen == [
        {"connect": 10.0, "read": 15.0, "write": 10.0, "pool": 10.0},
        {"connect": 10.0, "read": 65.0, "write": 10.0, "pool": 10.0},
    ]


async def test_updates_are_handled_in_id_order_whatever_the_order_of_the_answer(tmp_path):
    answer = httpx2.Response(200, json={"ok": True, "result": [message(2, "druha"), message(1, "prvni")]})
    bench = Bench(tmp_path, FakeTelegram(script=[answer]))
    assert await bench.poller.step() == 0.0
    assert bench.notes() == [("2026-10-07", "prvni"), ("2026-10-07", "druha")] and bench.offset() == 3


async def test_backoff_doubles_up_to_the_cap_and_resets_after_an_answer(tmp_path):
    bench = Bench(tmp_path, FakeTelegram(script=[httpx2.Response(500) for _ in range(8)]))
    waits = [await bench.poller.step() for _ in range(8)]
    assert waits == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0]
    assert await bench.poller.step() == 1.0  # an answer, nothing new
    bench.telegram.script.append(httpx2.Response(500))
    assert await bench.poller.step() == 5.0


@pytest.mark.parametrize("token", [None, "", "\n", "tk_abcdef0123456789", "123456789:short"])
async def test_an_unusable_token_waits_without_a_request(tmp_path, caplog, token):
    caplog.set_level(logging.DEBUG)
    bench = Bench(tmp_path, token=token)
    assert await bench.poller.step() == 5.0
    assert bench.telegram.urls == [] and "inbound poll=no_token" in caplog.text


async def test_an_empty_answer_that_came_at_once_waits_a_moment(tmp_path):
    bench = Bench(tmp_path)
    assert await bench.poller.step() == 1.0


async def test_a_database_error_keeps_the_update_for_the_next_round(tmp_path, monkeypatch):
    bench = Bench(tmp_path, FakeTelegram([message(1, "jednou"), message(2, "/plan")]))
    real, calls = bench.store.insert_note, []

    def flaky(*args: Any, **kwargs: Any) -> int:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(bench.store, "insert_note", flaky)
    with pytest.raises(sqlite3.OperationalError):
        await bench.poller.step()
    assert bench.notes() == [] and bench.telegram.sent == [] and bench.offset() is None
    assert await bench.poller.step() == 0.0
    assert bench.notes() == [("2026-10-07", "jednou")] and len(bench.telegram.sent) == 2 and bench.offset() == 3


async def test_one_broken_message_does_not_block_the_next(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="coach_mcp.inbound")
    bench = Bench(tmp_path, FakeTelegram([message(1, "/plan"), message(2, "druha")]))

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(inbound_module.tools, "sessions_out", broken)
    assert await bench.poller.step() == 0.0
    assert bench.telegram.texts == [REPLY_EN.failed, "Note saved (7 Oct)."]
    assert bench.offset() == 3 and bench.notes() == [("2026-10-07", "druha")]
    assert "inbound status=failed kind=RuntimeError" in caplog.text and SECRET not in caplog.text
    assert "inbound cmd=- status=failed reply=sent" in caplog.text


async def test_a_broken_message_is_answered_once_and_confirmed(tmp_path, monkeypatch):
    bench = Bench(tmp_path, FakeTelegram([message(1, "/plan")]))

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(inbound_module.tools, "sessions_out", broken)
    assert await bench.poller.step() == 0.0
    assert bench.offset() == 2 and bench.telegram.texts == [REPLY_EN.failed]
    assert await bench.poller.step() == 1.0  # the broken message is not asked for again
    assert bench.telegram.polls[-1]["offset"] == 2 and len(bench.telegram.sent) == 1


async def test_run_survives_a_crashing_step_and_backs_off(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="coach_mcp.inbound")
    bench = Bench(tmp_path)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 3:
            raise StopLoop

    def broken() -> Any:
        raise sqlite3.OperationalError(SECRET)

    bench.poller._sleep = sleep
    bench.store.inbound_state = broken  # type: ignore[method-assign]
    with pytest.raises(StopLoop):
        await bench.poller.run()
    assert slept == [5.0, 10.0, 20.0]
    assert caplog.text.count("inbound status=crashed kind=OperationalError") == 3 and SECRET not in caplog.text
    assert "inbound status=on" in caplog.text


# ----------------------------------------------------------------- replies


@pytest.mark.parametrize(
    ("codes", "requests", "sleeps", "word"),
    [
        ([200], 1, [], "reply=sent"),
        ([500, 200], 2, [2.0], "reply=sent"),
        ([429, 500], 2, [2.0], "reply=failed"),
        ([403], 1, [], "reply=failed"),
    ],
)
async def test_a_reply_is_tried_twice_at_most_and_the_note_is_kept(tmp_path, caplog, codes, requests, sleeps, word):
    caplog.set_level(logging.DEBUG, logger="coach_mcp.inbound")
    bench = Bench(tmp_path, FakeTelegram([message(1, "drzi")], send_codes=codes))
    assert await bench.poller.step() == 0.0
    assert len(bench.telegram.sent) == requests and bench.sleeps == sleeps
    assert f"inbound cmd=note status=ok {word}" in caplog.text
    assert bench.notes() == [("2026-10-07", "drzi")] and bench.offset() == 2


async def test_a_reply_goes_to_the_owner_as_plain_text(tmp_path):
    bench = Bench(tmp_path)
    await bench.say("/help")
    assert bench.telegram.sent == [{"chat_id": OWNER, "text": REPLY_EN.help}]
    assert bench.telegram.urls[-1] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"


# ------------------------------------------------------- criterion 2: /dnes


@pytest.mark.parametrize("language", ["en", "cs"])
@pytest.mark.parametrize("command", ["/dnes", "/today", "/DNES", " /dnes ", "/dnes@coach_bot"])
async def test_today_answers_with_the_briefing_text_and_records_nothing(tmp_path, language, command):
    bench = Bench(tmp_path, language=language)
    bench.seed({"date": D, "type": "running", "description": "10 km easy", "duration_min": 60})
    expected = (await compose_briefing(bench.store, FakeGarmin(garmin_data()), D, language)).message.text
    assert await bench.say(command) == expected
    assert expected.startswith("\U0001f7e2 ") and ("Today's plan:" if language == "en" else "Dnes v plánu:") in expected
    assert bench.garmin.days == [D] and not bench.store.briefing_sent(D) and bench.store.alert_states() == {}


@pytest.mark.parametrize(("language", "usage"), [("en", REPLY_EN.usage_today), ("cs", REPLY_CS.usage_today)])
async def test_today_with_an_argument_is_a_usage_reply_without_a_garmin_read(tmp_path, language, usage):
    bench = Bench(tmp_path, language=language)
    assert await bench.say("/dnes zitra") == usage
    assert bench.garmin.calls == 0 and bench.offset() == 101


async def test_today_that_cannot_be_computed_says_so_and_moves_on(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="coach_mcp.inbound")
    bench = Bench(tmp_path, garmin=FakeGarmin(RuntimeError(SECRET)))
    assert await bench.say("/dnes") == REPLY_EN.briefing_failed
    assert bench.offset() == 101
    assert "inbound briefing=failed kind=RuntimeError" in caplog.text and SECRET not in caplog.text
    assert "inbound cmd=today status=failed reply=sent" in caplog.text


# ------------------------------------------------------- criterion 2: /plan


def week_rows() -> list[dict[str, Any]]:
    return [
        {"date": date(2026, 10, 5), "type": "running", "description": "10 km easy", "duration_min": 60,
         "distance_km": 10.0, "intensity": "zone 2"},
        {"date": date(2026, 10, 6), "type": "strength", "description": "Upper body"},
        {"date": D, "type": "cycling", "description": "Intervals 5x4 min", "duration_min": 75},
        {"date": date(2026, 10, 9), "type": "mobility", "description": "Stretching", "duration_min": 20},
        {"date": date(2026, 10, 11), "type": "running", "description": "Long run", "distance_km": 18.5},
    ]


def seed_week(bench: Bench) -> list[int]:
    ids = bench.seed(*week_rows())
    bench.store.update_session_status(ids[0], "done", None, None)
    bench.store.update_session_status(ids[1], "skipped", None, None)
    bench.store.update_session_status(ids[3], "moved", date(2026, 10, 10), None)  # the copy gets id 6
    bench.seed({"date": date(2026, 10, 12), "type": "running", "description": "Next week"})  # not this week
    return ids


async def test_plan_lists_this_week_with_ids_en(tmp_path):
    bench = Bench(tmp_path)
    assert seed_week(bench) == [1, 2, 3, 4, 5]
    assert await bench.say("/plan") == (
        "Plan 2026-W41 (5 Oct – 11 Oct):\n"
        "#1 Mon 5 Oct · run: 10 km easy (60 min, 10 km, zone 2) – done\n"
        "#2 Tue 6 Oct · strength: Upper body – skipped\n"
        "#3 Wed 7 Oct · ride: Intervals 5x4 min (75 min)\n"
        "#4 Fri 9 Oct · mobility: Stretching (20 min) – moved\n"
        "#6 Sat 10 Oct · mobility: Stretching (20 min)\n"
        "#5 Sun 11 Oct · run: Long run (18.5 km)"
    )


async def test_plan_lists_this_week_with_ids_cs(tmp_path):
    bench = Bench(tmp_path, language="cs")
    seed_week(bench)
    assert await bench.say("/plan") == (
        "Plán 2026-W41 (5. 10. – 11. 10.):\n"
        "#1 po 5. 10. · běh: 10 km easy (60 min, 10 km, zone 2) – splněno\n"
        "#2 út 6. 10. · síla: Upper body – přeskočeno\n"
        "#3 st 7. 10. · kolo: Intervals 5x4 min (75 min)\n"
        "#4 pá 9. 10. · mobilita: Stretching (20 min) – přesunuto\n"
        "#6 so 10. 10. · mobilita: Stretching (20 min)\n"
        "#5 ne 11. 10. · běh: Long run (18,5 km)"
    )


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "Plan 2026-W41 (5 Oct – 11 Oct):\nNo sessions this week."),
        ("cs", "Plán 2026-W41 (5. 10. – 11. 10.):\nTento týden není nic v plánu."),
    ],
)
async def test_plan_of_an_empty_week(tmp_path, language, expected):
    bench = Bench(tmp_path, language=language)
    bench.seed({"date": date(2026, 10, 12), "type": "running", "description": "Next week"})
    assert await bench.say("/plan") == expected


@pytest.mark.parametrize(("language", "usage"), [("en", REPLY_EN.usage_plan), ("cs", REPLY_CS.usage_plan)])
async def test_plan_with_an_argument_is_a_usage_reply(tmp_path, language, usage):
    bench = Bench(tmp_path, language=language)
    seed_week(bench)
    assert await bench.say("/plan next") == usage


async def test_a_plan_longer_than_one_message_is_cut(tmp_path):
    bench = Bench(tmp_path, language="cs")
    bench.seed(*({"date": D, "type": "running", "description": "ž" * 120, "intensity": "ř" * 120} for _ in range(14)))
    reply = await bench.say("/plan")
    assert len(reply.encode()) <= 4000 and reply.endswith("…") and reply.startswith("Plán 2026-W41")
    assert len(bench.telegram.sent) == 1


# ---------------------------------------------- criterion 2: /done and /skip


@pytest.mark.parametrize(
    ("language", "command", "status", "reply"),
    [
        ("en", "/done {id}", "done", "Recorded: #1 Wed 7 Oct · run: 10 km easy (60 min) – done"),
        ("en", "/done #{id}", "done", "Recorded: #1 Wed 7 Oct · run: 10 km easy (60 min) – done"),
        ("en", "/skip {id}", "skipped", "Recorded: #1 Wed 7 Oct · run: 10 km easy (60 min) – skipped"),
        ("cs", "/done {id}", "done", "Zapsáno: #1 st 7. 10. · běh: 10 km easy (60 min) – splněno"),
        ("cs", "/skip {id}", "skipped", "Zapsáno: #1 st 7. 10. · běh: 10 km easy (60 min) – přeskočeno"),
        ("cs", "/SKIP@coach_bot {id}", "skipped", "Zapsáno: #1 st 7. 10. · běh: 10 km easy (60 min) – přeskočeno"),
    ],
)
async def test_done_and_skip_record_the_status(tmp_path, language, command, status, reply):
    bench = Bench(tmp_path, language=language)
    (sid,) = bench.seed({"date": D, "type": "running", "description": "10 km easy", "duration_min": 60})
    assert await bench.say(command.format(id=sid)) == reply
    assert bench.store.get_session(sid)["status"] == status
    assert bench.notes() == [] and bench.offset() == 101


BAD_IDS = ["", "abc", "0", "#0", "-1", "1.0", "12 13", "1 knee hurts", "#", "##1", "1000000000", "١"]


@pytest.mark.parametrize("command", ["done", "skip"])
@pytest.mark.parametrize("argument", BAD_IDS)
async def test_a_bad_id_changes_nothing_and_lists_the_open_sessions_of_today(tmp_path, command, argument):
    bench = Bench(tmp_path)
    ids = bench.seed(
        {"date": D, "type": "running", "description": "10 km easy", "duration_min": 60},
        {"date": D, "type": "strength", "description": "Upper body"},
        {"date": D, "type": "mobility", "description": "Already done"},
        {"date": D + timedelta(days=1), "type": "cycling", "description": "Tomorrow"},
    )
    bench.store.update_session_status(ids[2], "done", None, None)
    usage = REPLY_EN.usage_done if command == "done" else REPLY_EN.usage_skip
    assert await bench.say(f"/{command} {argument}") == (
        f"{usage}\nOpen today:\n#1 Wed 7 Oct · run: 10 km easy (60 min)\n#2 Wed 7 Oct · strength: Upper body"
    )
    assert [bench.store.get_session(i)["status"] for i in ids] == ["planned", "planned", "done", "planned"]
    assert bench.notes() == [] and bench.offset() == 101


@pytest.mark.parametrize(
    ("language", "command", "expected"),
    [
        ("en", "/done", "Usage: /done <id>, for example /done 12. Nothing was changed.\nNo open session today."),
        ("en", "/skip", "Usage: /skip <id>, for example /skip 12. Nothing was changed.\nNo open session today."),
        ("cs", "/done", "Použití: /done <id>, například /done 12. Nic se nezměnilo.\nDnes není žádný otevřený trénink."),
        ("cs", "/skip", "Použití: /skip <id>, například /skip 12. Nic se nezměnilo.\nDnes není žádný otevřený trénink."),
    ],
)
async def test_usage_without_an_open_session(tmp_path, language, command, expected):
    bench = Bench(tmp_path, language=language)
    bench.seed({"date": D + timedelta(days=1), "type": "cycling", "description": "Tomorrow"})
    assert await bench.say(command) == expected


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "Session #999999999 does not exist.\nUsage: /done <id>, for example /done 12. Nothing was changed.\n"
               "Open today:\n#1 Wed 7 Oct · run: 10 km easy"),
        ("cs", "Trénink #999999999 neexistuje.\nPoužití: /done <id>, například /done 12. Nic se nezměnilo.\n"
               "Dnes otevřené:\n#1 st 7. 10. · běh: 10 km easy"),
    ],
)
async def test_an_unknown_id_says_so_and_lists_the_open_sessions(tmp_path, language, expected):
    bench = Bench(tmp_path, language=language)
    bench.seed({"date": D, "type": "running", "description": "10 km easy"})
    assert await bench.say("/done 999999999") == expected  # nine digits: the longest id that is looked up
    assert bench.store.get_session(1)["status"] == "planned"


@pytest.mark.parametrize(
    ("language", "command", "expected"),
    [
        ("en", "/done 1", "Session #1 was replaced by a later plan and cannot be changed."),
        ("cs", "/skip 1", "Trénink #1 nahradil pozdější plán a nejde změnit."),
        ("en", "/done 2", "Session #2 was moved to #4 (9 Oct); change that one."),
        ("cs", "/skip 2", "Trénink #2 byl přesunut na #4 (9. 10.); změň ten."),
        ("en", "/done 3", "Session #3 is planned for 8 Oct and cannot be done yet."),
        ("cs", "/done 3", "Trénink #3 je v plánu až 8. 10. a ještě nemůže být splněný."),
    ],
)
async def test_sessions_that_cannot_be_changed(tmp_path, language, command, expected):
    bench = Bench(tmp_path, language=language)
    bench.seed(
        {"date": D, "type": "running", "description": "Superseded"},
        {"date": D, "type": "running", "description": "Moved"},
        {"date": D + timedelta(days=1), "type": "running", "description": "Tomorrow"},
    )
    with sqlite3.connect(bench.settings.db_path) as conn:
        conn.execute("UPDATE sessions SET status = 'superseded' WHERE id = 1")
    bench.store.update_session_status(2, "moved", date(2026, 10, 9), None)  # the copy gets id 4
    before = [bench.store.get_session(i)["status"] for i in (1, 2, 3, 4)]
    assert await bench.say(command) == expected
    assert [bench.store.get_session(i)["status"] for i in (1, 2, 3, 4)] == before
    assert bench.offset() == 101


async def test_a_session_of_today_can_be_done_and_a_future_one_skipped(tmp_path):
    bench = Bench(tmp_path)
    bench.seed(
        {"date": D - timedelta(days=1), "type": "running", "description": "Yesterday"},
        {"date": D, "type": "running", "description": "Today"},
        {"date": D + timedelta(days=1), "type": "running", "description": "Tomorrow"},
    )
    assert await bench.say("/done 1", 1) == "Recorded: #1 Tue 6 Oct · run: Yesterday – done"
    assert await bench.say("/done 2", 2) == "Recorded: #2 Wed 7 Oct · run: Today – done"
    assert await bench.say("/skip 3", 3) == "Recorded: #3 Thu 8 Oct · run: Tomorrow – skipped"
    assert await bench.say("/skip 2", 4) == "Recorded: #2 Wed 7 Oct · run: Today – skipped"  # a status can be corrected


async def test_a_moved_session_without_a_copy_is_refused(tmp_path):
    bench = Bench(tmp_path)
    bench.seed({"date": D, "type": "running", "description": "Moved"})
    with sqlite3.connect(bench.settings.db_path) as conn:
        conn.execute("UPDATE sessions SET status = 'moved' WHERE id = 1")
    assert await bench.say("/done 1") == "Session #1 was moved and cannot be changed."


async def test_a_session_that_changes_under_the_command_is_refused(tmp_path, monkeypatch):
    bench = Bench(tmp_path, language="cs")
    bench.seed({"date": D, "type": "running", "description": "Race"})
    real = bench.store.get_session
    seen: list[int] = []

    def racing(session_id: int) -> Any:
        row = real(session_id)
        seen.append(session_id)
        if len(seen) == 1:  # after the poller's own look, before the write: a later plan supersedes the session
            with sqlite3.connect(bench.settings.db_path) as conn:
                conn.execute("UPDATE sessions SET status = 'superseded' WHERE id = 1")
        return row

    monkeypatch.setattr(bench.store, "get_session", racing)
    assert await bench.say("/done 1") == "Trénink #1 nejde změnit."
    assert real(1)["status"] == "superseded" and bench.offset() == 101


# ------------------------------------------------------ criterion 2: notes


@pytest.mark.parametrize(("language", "reply"), [("en", "Note saved (7 Oct)."), ("cs", "Poznámka uložena (7. 10.).")])
async def test_free_text_becomes_a_note_for_today(tmp_path, language, reply):
    bench = Bench(tmp_path, language=language)
    assert await bench.say("  Koleno po běhu trochu bolí.\nZítra volno? \n") == reply
    assert bench.notes() == [("2026-10-07", "Koleno po běhu trochu bolí.\nZítra volno?")]
    assert bench.store.latest_notes(1)[0]["session_id"] is None and bench.offset() == 101


def ts(moment: datetime) -> int:
    return int(moment.timestamp())


@pytest.mark.parametrize(
    ("sent", "day"),
    [
        (ts(datetime(2026, 10, 6, 23, 30, tzinfo=PRAGUE)), "2026-10-06"),  # sent late yesterday, handled today
        (ts(datetime(2026, 10, 6, 22, 30, tzinfo=UTC)), "2026-10-07"),  # 00:30 local: the athlete's day, not UTC's
        (ts(NOW - timedelta(hours=48)), "2026-10-05"),  # the oldest date that is believed
        (ts(NOW - timedelta(hours=48, seconds=1)), "2026-10-07"),
        (ts(NOW + timedelta(hours=13)), "2026-10-07"),  # a date after now is now
        (0, "2026-10-07"),
        (-1, "2026-10-07"),
        (253402300800, "2026-10-07"),  # year 10000
        (10**30, "2026-10-07"),
        (None, "2026-10-07"),
        ("1791330600", "2026-10-07"),
        (float(ts(datetime(2026, 10, 6, 12, 0, tzinfo=PRAGUE))), "2026-10-07"),
        (True, "2026-10-07"),
    ],
)
async def test_a_note_is_dated_by_the_day_it_was_sent(tmp_path, sent, day):
    bench = Bench(tmp_path)
    update = message(100, "kdy")
    if sent is None:
        del update["message"]["date"]
    else:
        update["message"]["date"] = sent
    bench.telegram.pending.append(update)
    assert await bench.poller.step() == 0.0
    assert bench.notes() == [(day, "kdy")]


@pytest.mark.parametrize(
    ("language", "reply"),
    [
        ("en", "The note is too long (2001 characters, at most 2000). Nothing was saved."),
        ("cs", "Poznámka je moc dlouhá (2001 znaků, nejvýš 2000). Nic se neuložilo."),
    ],
)
async def test_a_note_over_the_limit_is_refused_not_cut(tmp_path, language, reply):
    bench = Bench(tmp_path, language=language)
    saved = "Note saved (7 Oct)." if language == "en" else "Poznámka uložena (7. 10.)."
    assert await bench.say("ž" * NOTE_MAX, 1) == saved  # the limit counts characters, not bytes
    assert await bench.say(" " + "ž" * NOTE_MAX + " \n", 2) == saved  # measured after trimming
    assert await bench.say("ž" * (NOTE_MAX + 1), 3) == reply
    assert [len(text) for _day, text in bench.notes()] == [NOTE_MAX, NOTE_MAX] and bench.offset() == 4


@pytest.mark.parametrize(("language", "reply"), [("en", REPLY_EN.note_refused), ("cs", REPLY_CS.note_refused)])
async def test_a_note_with_a_nul_character_is_refused(tmp_path, language, reply):
    bench = Bench(tmp_path, language=language)
    assert await bench.say("a\x00b") == reply
    assert await bench.say("a\x00" + "b" * NOTE_MAX, 101) == reply  # NUL is named before the length
    assert bench.notes() == [] and bench.offset() == 102


# ------------------------------------------- help, unknown, unsupported


@pytest.mark.parametrize("command", ["/start", "/help", "/HELP", "/start@coach_bot", "/start abc"])
@pytest.mark.parametrize(("language", "catalogue"), [("en", REPLY_EN), ("cs", REPLY_CS)])
async def test_start_and_help_list_the_commands(tmp_path, command, language, catalogue):
    bench = Bench(tmp_path, language=language)
    assert await bench.say(command) == catalogue.help
    assert bench.notes() == []


@pytest.mark.parametrize("text", ["/foo", "/", "/ dnes", "/dnes!", "/done12", "/planuj", "/" + SECRET])
@pytest.mark.parametrize(("language", "catalogue"), [("en", REPLY_EN), ("cs", REPLY_CS)])
async def test_an_unknown_command_is_answered_with_the_list_and_never_becomes_a_note(tmp_path, text, language, catalogue):
    bench = Bench(tmp_path, language=language)
    assert await bench.say(text) == f"{catalogue.unknown_command}\n\n{catalogue.help}"
    assert bench.notes() == [] and bench.offset() == 101


@pytest.mark.parametrize(
    "update",
    [
        pytest.param(message(100, None, photo=[{"file_id": "x"}], caption=SECRET), id="a photo with a caption"),
        pytest.param(message(100, None, sticker={"file_id": "x"}), id="a sticker"),
        pytest.param(message(100, ""), id="an empty text"),
        pytest.param(message(100, " \n "), id="only white space"),
        pytest.param(message(100, 12), id="a text that is not a string"),
    ],
)
@pytest.mark.parametrize(("language", "catalogue"), [("en", REPLY_EN), ("cs", REPLY_CS)])
async def test_the_owner_s_message_without_text_is_answered_and_stores_nothing(tmp_path, update, language, catalogue):
    bench = Bench(tmp_path, FakeTelegram([update]), language=language)
    assert await bench.poller.step() == 0.0
    assert bench.telegram.texts == [catalogue.unsupported]
    assert bench.notes() == [] and bench.offset() == 101


def test_the_reply_catalogues_are_complete_and_plain():
    for catalogue in (REPLY_EN, REPLY_CS):
        assert len(catalogue.weekdays) == 7
        for name, value in vars(catalogue).items():
            if name == "weekdays":
                continue
            assert isinstance(value, str) and value.strip() == value and value, name
            # plain text: nothing Telegram would read as Markdown or HTML if a parse mode were ever set
            assert not re.search(r"[*_`\[\]]|</?[a-z]+>", value.replace("<id>", "")), name
            assert len(value.encode()) < 500, name
    assert {f for f in vars(REPLY_EN)} == {f for f in vars(REPLY_CS)}
    for name in ("help", "usage_today"):
        for catalogue in (REPLY_EN, REPLY_CS):
            assert "/dnes" in getattr(catalogue, name) and "/today" in getattr(catalogue, name)


# --------------------------------------- the exact wording of the remaining replies (both catalogues)


@pytest.mark.parametrize(
    ("language", "text", "expected"),
    [
        ("en", "/help", "Commands:\n/today (or /dnes) – today's briefing\n/plan – this week's plan with the session ids\n"
                        "/done <id> – mark a session as done\n/skip <id> – mark a session as skipped\n"
                        "Any other text is saved as a note for today."),
        ("cs", "/help", "Příkazy:\n/dnes (nebo /today) – dnešní briefing\n/plan – plán na tento týden s id tréninků\n"
                        "/done <id> – označí trénink jako splněný\n/skip <id> – označí trénink jako přeskočený\n"
                        "Jakýkoli jiný text se uloží jako dnešní poznámka."),
        ("en", "/foo", "Unknown command.\n\nCommands:\n/today (or /dnes) – today's briefing\n"
                       "/plan – this week's plan with the session ids\n/done <id> – mark a session as done\n"
                       "/skip <id> – mark a session as skipped\nAny other text is saved as a note for today."),
        ("cs", "/foo", "Neznámý příkaz.\n\nPříkazy:\n/dnes (nebo /today) – dnešní briefing\n"
                       "/plan – plán na tento týden s id tréninků\n/done <id> – označí trénink jako splněný\n"
                       "/skip <id> – označí trénink jako přeskočený\nJakýkoli jiný text se uloží jako dnešní poznámka."),
        ("en", "/today now", "Usage: /today (or /dnes) with nothing after it."),
        ("cs", "/dnes ted", "Použití: /dnes (nebo /today), bez dalšího textu."),
        ("en", "/plan next", "Usage: /plan with nothing after it."),
        ("cs", "/plan pristi", "Použití: /plan, bez dalšího textu."),
        ("en", "a\x00b", "The note could not be saved."),
        ("cs", "a\x00b", "Poznámku se nepodařilo uložit."),
    ],
)
async def test_the_wording_of_the_fixed_replies_is_pinned_in_both_languages(tmp_path, language, text, expected):
    bench = Bench(tmp_path, language=language)
    assert await bench.say(text) == expected


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "The briefing could not be computed right now. Try again later."),
        ("cs", "Briefing se teď nepodařilo spočítat. Zkus to později."),
    ],
)
async def test_the_wording_of_the_briefing_failure_is_pinned(tmp_path, language, expected):
    bench = Bench(tmp_path, garmin=FakeGarmin(RuntimeError(SECRET)), language=language)
    assert await bench.say("/dnes") == expected


@pytest.mark.parametrize(
    ("language", "expected"),
    [("en", "Something went wrong (internal error)."), ("cs", "Něco se pokazilo (vnitřní chyba).")],
)
async def test_the_wording_of_the_internal_error_reply_is_pinned(tmp_path, monkeypatch, language, expected):
    bench = Bench(tmp_path, language=language)

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(inbound_module.tools, "sessions_out", broken)
    assert await bench.say("/plan") == expected


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "Only text messages are supported."),
        ("cs", "Umím jen textové zprávy."),
    ],
)
async def test_the_wording_of_the_unsupported_reply_is_pinned(tmp_path, language, expected):
    bench = Bench(tmp_path, FakeTelegram([message(100, None, sticker={"file_id": "x"})]), language=language)
    assert await bench.poller.step() == 0.0
    assert bench.telegram.texts == [expected]


async def test_the_czech_list_of_open_sessions_after_a_bad_id_is_pinned(tmp_path):
    bench = Bench(tmp_path, language="cs")
    bench.seed({"date": D, "type": "running", "description": "10 km easy", "duration_min": 60})
    assert await bench.say("/skip abc") == (
        "Použití: /skip <id>, například /skip 12. Nic se nezměnilo.\nDnes otevřené:\n#1 st 7. 10. · běh: 10 km easy (60 min)"
    )
