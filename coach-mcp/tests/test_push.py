from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx2
import pytest

from coach_mcp.config import Settings
from coach_mcp.push import PushMessage, TelegramPush

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1"
SECOND_TOKEN = "987654321:BBHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw2"

MSG = PushMessage(
    title="\U0001f7e1 ⚠️ ŽLUTÁ · ranní briefing 7. 10.",
    body="Verdikt: žlutá\nReadiness: 61 (žlutá)",
)


def make_push(tmp_path: Path, handler, token: str | None = TOKEN + "\n", chat_id: str = "123456789") -> TelegramPush:
    token_file = tmp_path / "telegram_bot_token"
    if token is not None:
        token_file.write_text(token, encoding="utf-8")
    settings = Settings(telegram_chat_id=chat_id, telegram_token_file=token_file)
    return TelegramPush(settings, transport=httpx2.MockTransport(handler))


@pytest.mark.anyio
async def test_sends_text_to_the_configured_chat(tmp_path):
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"ok": True})

    assert await make_push(tmp_path, handler).send(MSG) == "sent"
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert json.loads(request.content) == {"chat_id": 123456789, "text": MSG.text}
    assert MSG.text == MSG.title + "\n\n" + MSG.body
    assert "authorization" not in request.headers


@pytest.mark.anyio
async def test_negative_group_chat_id_is_sent_as_integer(tmp_path):
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200)

    assert await make_push(tmp_path, handler, chat_id="-100123456789").send(MSG) == "sent"
    sent = json.loads(seen[0].content)
    assert sent["chat_id"] == -100123456789
    assert isinstance(sent["chat_id"], int)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (200, "sent"),
        (201, "sent"),
        (429, "transient"),
        (500, "transient"),
        (503, "transient"),
        (400, "permanent"),
        (401, "permanent"),
        (403, "permanent"),
        (404, "permanent"),
        (302, "permanent"),
    ],
)
async def test_status_classification(tmp_path, status, outcome):
    push = make_push(tmp_path, lambda request: httpx2.Response(status))
    assert await push.send(MSG) == outcome


@pytest.mark.anyio
@pytest.mark.parametrize("error", [httpx2.ReadTimeout("slow"), httpx2.ConnectError("down"), OSError("reset")])
async def test_network_errors_are_transient(tmp_path, error, caplog):
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise error

    caplog.set_level(logging.DEBUG)
    assert await make_push(tmp_path, handler).send(MSG) == "transient"
    assert f"push status=transient kind={type(error).__name__}" in caplog.text
    for forbidden in ("slow", "down", TOKEN, "123456789", "api.telegram.org"):
        assert forbidden not in caplog.text, forbidden


@pytest.mark.anyio
@pytest.mark.parametrize("token", [None, "", "\n", "tk_abcdef0123456789", "123456789:short"])
async def test_unusable_token_is_permanent_without_a_request(tmp_path, token, caplog):
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200)

    caplog.set_level(logging.DEBUG)
    assert await make_push(tmp_path, handler, token=token).send(MSG) == "permanent"
    assert calls == []
    assert "push status=permanent reason=no_token" in caplog.text


@pytest.mark.anyio
async def test_token_is_read_fresh_on_every_send(tmp_path):
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(200)

    push = make_push(tmp_path, handler)
    assert await push.send(MSG) == "sent"
    (tmp_path / "telegram_bot_token").write_text(SECOND_TOKEN + "\n", encoding="utf-8")
    assert await push.send(MSG) == "sent"
    assert seen == [
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        f"https://api.telegram.org/bot{SECOND_TOKEN}/sendMessage",
    ]
