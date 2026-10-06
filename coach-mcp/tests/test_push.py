from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx2
import pytest

from coach_mcp.config import Settings
from coach_mcp.push import NtfyPush, PushMessage

MSG = PushMessage(title="ŽLUTÁ · ranní briefing 7. 10.", message="Verdikt: žlutá", priority=4, tags=("yellow_circle", "warning"))


def make_push(tmp_path: Path, handler, token: str | None = "tk_abcdef0123456789") -> NtfyPush:
    token_file = tmp_path / "ntfy_token"
    if token is not None:
        token_file.write_text(token, encoding="utf-8")
    settings = Settings(ntfy_url="https://ntfy.example", ntfy_topic="my-topic", ntfy_token_file=token_file)
    return NtfyPush(settings, transport=httpx2.MockTransport(handler))


@pytest.mark.anyio
async def test_publishes_json_to_server_root_with_token(tmp_path):
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"id": "x"})

    assert await make_push(tmp_path, handler, token="tk_abcdef0123456789\n").send(MSG) == "sent"
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "https://ntfy.example/"
    assert json.loads(request.content) == {
        "topic": "my-topic",
        "title": "ŽLUTÁ · ranní briefing 7. 10.",
        "message": "Verdikt: žlutá",
        "priority": 4,
        "tags": ["yellow_circle", "warning"],
    }
    assert request.headers["Authorization"] == "Bearer tk_abcdef0123456789"
    assert "my-topic" not in str(request.url)


@pytest.mark.anyio
@pytest.mark.parametrize("token", ["", "\n", None])
async def test_no_authorization_header_without_token(tmp_path, token):
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200)

    assert await make_push(tmp_path, handler, token=token).send(MSG) == "sent"
    assert "authorization" not in seen[0].headers


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
    assert "my-topic" not in caplog.text and "slow" not in caplog.text and "down" not in caplog.text
