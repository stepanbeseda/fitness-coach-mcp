"""`tools/list` is pinned (PR 31): `tools_list.json` was recorded on the code before PR 31 (`RECORD_TOOLS_LIST=1`).

The error kinds change no tool: names, titles, descriptions, input schemas and annotations stay as they were, and
no tool publishes an output schema. PR 28 compares its tool definitions with the same file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import auth_headers, running_client

SNAPSHOT = Path(__file__).with_name("tools_list.json")


@pytest.mark.anyio
async def test_tools_list_is_unchanged(app):
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    async with running_client(app) as client:
        response = await client.post("/mcp", headers=auth_headers(), json=body)
    tools = sorted(response.json()["result"]["tools"], key=lambda t: t["name"])
    text = json.dumps(tools, indent=1, ensure_ascii=False) + "\n"
    if os.environ.get("RECORD_TOOLS_LIST") == "1":
        SNAPSHOT.write_text(text, encoding="utf-8")
    assert SNAPSHOT.read_text(encoding="utf-8") == text
    assert all("outputSchema" not in t for t in tools)
