"""Checks inside the coach-mcp container of the e2e stack, run by scripts/e2e-test.sh.

coach-mcp mounts only the read-only Garmin token, the nightly history job's client reaches
garmin-mcp with it, and garmin-mcp refuses that token a write tool before the tool runs. The
e2e stack has no Garmin login, so a tool that runs ends with the login error: its kind (garmin_auth, PR 31)
is what the coach classifies, not its text.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
from pathlib import Path
from typing import Any

import httpx2
import mcp.types

from coach_mcp.config import Settings
from coach_mcp.garmin import ERROR_KIND_KEY, HISTORY_TOOLS, GarminReader, error_outcome

READ_ONLY_TOKEN = Path("/run/secrets/garmin_readonly_token")
GATEWAY_TOKEN = Path("/run/secrets/garmin_backend_token")
# Must equal READ_ONLY_REFUSAL in garmin-mcp/src/garmin_coach_mcp/access.py.
REFUSAL = (
    "Refused: this token has read-only access to garmin-mcp, and this tool is not annotated read-only."
)

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    print(("  ok   " if condition else "  FAIL ") + label)
    if not condition:
        failures.append(label)


async def raw_call(settings: Settings, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """tools/call straight to garmin-mcp with the coach's token (past the coach's allowlist): the JSON result."""
    try:
        token = settings.garmin_token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}
    try:
        async with httpx2.AsyncClient(timeout=30) as http:
            response = await http.post(settings.garmin_url, headers=headers, json=body)
        result = response.json().get("result") if response.status_code == 200 else None
    except Exception:
        return None
    return result if isinstance(result, dict) else None


async def main() -> int:
    print("\n[coach-mcp -> garmin-mcp]")
    settings = Settings.from_env()
    check(not GATEWAY_TOKEN.exists(), "coach-mcp does not mount garmin_backend_token")
    check(settings.garmin_token_file == READ_ONLY_TOKEN and READ_ONLY_TOKEN.is_file(),
          "coach-mcp reads garmin-mcp with /run/secrets/garmin_readonly_token")
    today = dt.date.today().isoformat()
    arguments = {
        "get_recovery_snapshot": {"date": today},
        "list_activities": {"start_date": today, "end_date": today},
        "get_race_predictions": {"start_date": today, "end_date": today},
    }
    reader = GarminReader(settings)
    for name in HISTORY_TOOLS:
        outcome, _ = await reader.history_call(name, arguments[name])
        check(outcome == "auth_error",
              f"history client: {name} runs in garmin-mcp with the read-only token (auth_error, got {outcome})")
    result = await raw_call(settings, "get_recovery_snapshot", arguments["get_recovery_snapshot"])
    kind = ((result or {}).get("_meta") or {}).get(ERROR_KIND_KEY)
    check(result is not None and result.get("isError") is True and kind == "garmin_auth",
          f"garmin-mcp's login error carries the kind garmin_auth in _meta (got {kind})")
    if result is not None:
        reworded = mcp.types.CallToolResult.model_validate(result).model_copy(
            update={"content": [mcp.types.TextContent(type="text", text="reworded")]}
        )
        check(error_outcome(reworded) == "auth_error", "the coach classifies that error by its kind, not its text")
    result = await raw_call(settings, "unschedule_workout", {"schedule_id": 1})
    content = (result or {}).get("content") or [{}]
    text = content[0].get("text") if isinstance(content[0], dict) else None
    kind = ((result or {}).get("_meta") or {}).get(ERROR_KIND_KEY)
    check(result is not None and result.get("isError") is True and text == REFUSAL and kind == "read_only",
          "garmin-mcp refuses the read-only token unschedule_workout before it runs (kind read_only)")
    print("COACH-GARMIN OK" if not failures else "COACH-GARMIN FAILED: " + "; ".join(failures))
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
