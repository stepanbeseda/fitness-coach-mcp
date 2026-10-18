"""The answers coach-mcp reads, pinned (PR 31): `contract/garmin_answers.json`.

Each scenario calls one of the six tools coach-mcp may call (`ALLOWED_TOOLS` in coach-mcp/src/coach_mcp/garmin.py)
through the real app over HTTP, with the read-only token, the coach's arguments for 2026-09-30 and the canned
payloads of `fixtures.py`, and records what the coach receives: the answer (parsed), or the error text and `_meta`
with its kind. coach-mcp keeps a byte-identical copy (`scripts/check.sh` compares the two) and feeds it to its
readers (coach-mcp/tests/test_garmin_contract.py). A change of an answer fails here first; after a deliberate
change, `scripts/record-garmin-contract.sh` records both copies again.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from garminconnect import GarminConnectNotFoundError, GarminConnectTooManyRequestsError

from conftest import AUTH_ERROR, CONN_ERROR, READONLY_TOKEN, FakeGarmin, auth_headers, running_client
from garmin_coach_mcp.errors import ERROR_KIND_KEY
from garmin_coach_mcp.formatting import to_json
from garmin_coach_mcp.garmin_client import TOKEN_FILE_NAME, GarminService
from garmin_coach_mcp.server import create_app

GOLDEN = Path(__file__).parent / "contract" / "garmin_answers.json"
DAY, YESTERDAY, WEEK_AGO = "2026-09-30", "2026-09-29", "2026-09-24"
# The arguments coach-mcp sends: garmin.GarminReader.fetch (the first four) and the history job (the last two).
ARGUMENTS: dict[str, dict[str, Any]] = {
    "get_device_status": {},
    "get_recovery_snapshot": {"date": DAY},
    "get_hrv_trend": {"days": 7, "end_date": DAY},
    "list_scheduled_workouts": {"start_date": YESTERDAY, "end_date": DAY},
    "list_activities": {"start_date": WEEK_AGO, "end_date": DAY, "limit": 100},
    "get_race_predictions": {"start_date": WEEK_AGO, "end_date": DAY},
}
SNAPSHOT_CALLS = ("get_training_readiness", "get_hrv_data", "get_sleep_data", "get_user_summary", "get_training_status")
Setup = Callable[[FakeGarmin, Path], None]


def _fail_all(error: Exception) -> Setup:
    def setup(fake: FakeGarmin, tokens: Path) -> None:
        for method in fake.responses:
            fake.fail[method] = error

    return setup


def _not_logged_in(fake: FakeGarmin, tokens: Path) -> None:
    (tokens / TOKEN_FILE_NAME).unlink()


def _session_expired(fake: FakeGarmin, tokens: Path) -> None:
    fake.fail["login"] = AUTH_ERROR


def _degraded(fake: FakeGarmin, tokens: Path) -> None:
    fake.fail["get_hrv_data"] = GarminConnectTooManyRequestsError("Too many requests")
    fake.fail["get_sleep_data"] = CONN_ERROR
    fake.fail["get_training_status"] = GarminConnectNotFoundError("API Error 404")
    fake.responses["get_user_summary"] = None


def _no_data(fake: FakeGarmin, tokens: Path) -> None:
    for method in SNAPSHOT_CALLS:
        fake.responses[method] = None


def _canned(fake: FakeGarmin, tokens: Path) -> None:
    return None


FAILURES: dict[str, Setup] = {
    "rate_limited": _fail_all(GarminConnectTooManyRequestsError("Too many requests")),
    "garmin_unavailable": _fail_all(CONN_ERROR),
    "not_logged_in": _not_logged_in,
    "session_expired": _session_expired,
}


def scenarios() -> list[tuple[str, str, dict[str, Any], Setup]]:
    rows: list[tuple[str, str, dict[str, Any], Setup]] = []
    for tool, args in ARGUMENTS.items():
        rows.append((f"{tool} ok", tool, args, _canned))
        rows += [(f"{tool} {name}", tool, args, setup) for name, setup in FAILURES.items()]
    snapshot = ARGUMENTS["get_recovery_snapshot"]
    rows.append(("get_recovery_snapshot degraded", "get_recovery_snapshot", snapshot, _degraded))
    rows.append(("get_recovery_snapshot no_data", "get_recovery_snapshot", snapshot, _no_data))
    rows.append(("list_activities truncated", "list_activities", dict(ARGUMENTS["list_activities"], limit=2), _canned))
    return rows


async def _record(settings, tool: str, args: dict[str, Any], setup: Setup) -> dict[str, Any]:
    fake = FakeGarmin()
    setup(fake, settings.tokens_dir)
    app = create_app(settings, GarminService(settings, garmin_factory=lambda: fake))
    # Protocol 2025-06-18: the SDK adds no serverInfo stamp (which carries the version) to `_meta`.
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}}
    async with running_client(app) as client:
        response = await client.post("/mcp", headers=auth_headers(READONLY_TOKEN), json=body)
    result = response.json()["result"]
    (text,) = [c["text"] for c in result["content"]]
    record: dict[str, Any] = {"tool": tool, "arguments": args, "is_error": result.get("isError", False)}
    record["meta"] = result.get("_meta")
    if record["is_error"]:
        record |= {"text": text, "answer": None}
    else:
        answer = json.loads(text)
        assert to_json(answer) == text  # the parsed answer stands for the exact text the coach receives
        record |= {"text": None, "answer": answer}
    return record


def render(records: dict[str, Any]) -> str:
    return json.dumps(records, indent=1, ensure_ascii=False) + "\n"


@pytest.mark.anyio
async def test_the_answers_coach_mcp_reads_match_the_contract(settings):
    token_file = settings.tokens_dir / TOKEN_FILE_NAME
    token_text = token_file.read_text(encoding="utf-8")
    records = {}
    for name, tool, args, setup in scenarios():
        records[name] = await _record(settings, tool, args, setup)
        token_file.write_text(token_text, encoding="utf-8")  # the next scenario is logged in again
    text = render(records)
    if os.environ.get("RECORD_GARMIN_CONTRACT") == "1":
        GOLDEN.write_text(text, encoding="utf-8")
    assert GOLDEN.read_text(encoding="utf-8") == text, (
        "an answer coach-mcp reads changed; if on purpose: scripts/record-garmin-contract.sh, then adapt coach-mcp"
    )


def test_the_contract_has_every_tool_and_the_kinds_the_coach_reads():
    records = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert {r["tool"] for r in records.values()} == set(ARGUMENTS)
    kinds = {r["meta"][ERROR_KIND_KEY] for r in records.values() if r["is_error"]}
    assert {"rate_limited", "garmin_auth"} <= kinds
