"""Run inside the coach-mcp container of the e2e stack: coach-mcp reaches hevy-mcp with its mounted token and
the sample shapes of the tests match hevy-mcp's schemas.

Prints only its own OK / FAIL lines (no token, no payload) and exits 1 on any failure.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import httpx2
import jsonschema
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

# The SDK client logs "Error parsing SSE message" / "Session termination failed: 404" tracebacks through the
# last-resort handler; this script prints nothing but its own lines.
logging.disable(logging.CRITICAL)

from coach_mcp.config import Settings  # noqa: E402
from coach_mcp.hevy import HISTORY_TOOLS, HevyReader  # noqa: E402

EVENTS_SAMPLE: dict[str, Any] = {
    "events": [
        {
            "type": "updated",
            "workout": {
                "id": "W1",
                "title": "Push",
                "start_time": "2026-10-05T10:00:00Z",
                "end_time": "2026-10-05T11:00:00Z",
                "updated_at": "2026-10-05T11:05:00Z",
                "duration": "1h 0m 0s",
                "exercises": [
                    {
                        "index": 0,
                        "title": "Bench Press (Barbell)",
                        "exercise_template_id": "79D0BB3A",
                        "sets": [
                            {"index": 0, "type": "warmup", "weight_kg": 40, "reps": 10},
                            {"index": 1, "type": "normal", "weight_kg": 100, "reps": 5, "rpe": 8},
                            {"index": 2, "type": "dropset", "weight_kg": 80, "reps": 8},
                            {"index": 3, "type": "failure", "weight_kg": 70, "reps": 10},
                        ],
                    },
                    {
                        "index": 1,
                        "title": "Plank",
                        "exercise_template_id": "A1B2C3D4",
                        "sets": [
                            {"index": 0, "type": "normal", "duration_seconds": 60},
                            {"index": 1, "type": "normal", "reps": 12},
                        ],
                    },
                ],
            },
        },
        {"type": "deleted", "id": "W2", "deleted_at": "2026-10-06T08:00:00Z"},
    ],
    "page": 1,
    "page_count": 1,
    "has_next_page": False,
}
TEMPLATE_SAMPLE: dict[str, Any] = {
    "id": "79D0BB3A",
    "title": "Bench Press (Barbell)",
    "type": "weight_reps",
    "primary_muscle_group": "chest",
    "secondary_muscle_groups": ["triceps", "shoulders"],
    "is_custom": False,
}

failures = 0


def report(ok: bool, label: str) -> None:
    global failures
    print(("OK   " if ok else "FAIL ") + label)
    failures += 0 if ok else 1


async def listed_tools(settings: Settings) -> dict[str, Any]:
    """The tools of hevy-mcp as the coach sees them; the token is read here and never passed around."""
    token = settings.hevy_token_file.read_text(encoding="utf-8").strip()
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30.0) as http:
        async with Client(streamable_http_client(settings.hevy_url, http_client=http)) as client:
            return {tool.name: tool for tool in (await client.list_tools()).tools}


def valid(instance: Any, schema: Any) -> bool:
    try:
        jsonschema.validate(instance, schema)
    except jsonschema.ValidationError:
        return False
    return True


async def main() -> int:
    settings = Settings.from_env()
    outcome, _ = await HevyReader(settings).history_call(
        "get-workout-events", {"page": 1, "page_size": 10, "since": "2026-01-01T00:00:00Z"}
    )
    # hevy-mcp answers (the Hevy API itself is out of reach in the internal network): "error". "unreachable" would
    # mean no MCP session or a wrong token, "no_credentials" that the secret is not mounted.
    report(outcome == "error", f"coach reaches hevy-mcp with its token (outcome {outcome}, expected error)")

    try:
        tools = await listed_tools(settings)
    except Exception as exc:  # noqa: BLE001
        report(False, f"coach lists the hevy-mcp tools ({type(exc).__name__})")
        return 1
    for name in HISTORY_TOOLS:
        tool = tools.get(name)
        report(
            tool is not None and tool.annotations is not None and tool.annotations.read_only_hint is True,
            f"{name} is listed and read-only",
        )
    events, template = tools.get(HISTORY_TOOLS[0]), tools.get(HISTORY_TOOLS[1])
    report(events is not None and valid(EVENTS_SAMPLE, events.output_schema), "events sample matches the output schema")
    report(
        template is not None and valid({"exercise_template": TEMPLATE_SAMPLE}, template.output_schema),
        "template sample matches the output schema",
    )
    report(
        template is not None and valid({"exercise_template": None}, template.output_schema),
        "a null template matches the output schema",
    )
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
