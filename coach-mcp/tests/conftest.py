from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from coach_mcp.config import Settings
from coach_mcp.server import create_app
from coach_mcp.store import Store

BACKEND_TOKEN = "test-backend-token-0123456789abcdef"
# Wednesday of ISO week 2026-W41 (Mon 2026-10-05 .. Sun 2026-10-11). Every test
# uses this injected clock and fixed dates; no test reads the wall clock.
TODAY = date(2026, 10, 7)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "coach.db"


@pytest.fixture
def bearer_file(tmp_path: Path) -> Path:
    f = tmp_path / "coach_backend_token"
    f.write_text(BACKEND_TOKEN + "\n", encoding="utf-8")
    return f


@pytest.fixture
def settings(db_path: Path, bearer_file: Path) -> Settings:
    return Settings(db_path=db_path, bearer_token_file=bearer_file, timezone="Europe/Prague")


@pytest.fixture
def store(db_path: Path) -> Store:
    return Store(db_path)


@pytest.fixture
def app(settings: Settings, store: Store) -> Any:
    return create_app(settings, store, today=lambda: TODAY)


@asynccontextmanager
async def running_client(app: Any):
    """httpx client bound to the ASGI app with its lifespan running."""
    inner = app.app  # BearerAuthMiddleware -> Starlette
    async with inner.router.lifespan_context(inner):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://coach-mcp:8000") as client:
            yield client


MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def auth_headers(token: str = BACKEND_TOKEN, protocol: str | None = "2025-06-18") -> dict[str, str]:
    headers = dict(MCP_HEADERS, Authorization=f"Bearer {token}")
    if protocol:
        headers["MCP-Protocol-Version"] = protocol
    return headers


async def call_tool(client: Any, name: str, arguments: dict[str, Any], id_: int = 2) -> dict[str, Any]:
    """POST a tools/call and return its JSON-RPC ``result``."""
    body = {"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    r = await client.post("/mcp", headers=auth_headers(), json=body)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def payload(result: dict[str, Any]) -> Any:
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    names = ("mcp", "mcp.client", "client", "httpx", "httpx2", "httpcore", "httpcore2", "sse_starlette")
    saved = {n: logging.getLogger(n).level for n in names}
    saved_root = (root.level, list(root.handlers))
    yield
    for n, lvl in saved.items():
        logging.getLogger(n).setLevel(lvl)
    root.setLevel(saved_root[0])
    root.handlers[:] = saved_root[1]
