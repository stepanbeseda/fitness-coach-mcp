"""Sections of a Garmin tool answer: the fail-soft rule of plan section 1 in one place.

A tool that combines Garmin calls declares them (`Call`) and the sections of its answer (`Section`);
`fetch_sections` runs the calls concurrently and returns every section shaped, in declaration order:
- an authentication problem aborts the whole tool (every call would fail the same way);
- a 404 of a call declared `missing_is_empty` hands the shaper `None` (no data, not an error);
- a failed call, or a shaper that raises on an unexpected payload, gives the section its error value
  (`{"error": message, "error_kind": kind}` unless the section declares `if_error`, which gets the message);
  the shaper failure is logged without data;
- only a failed `required` call fails the whole tool, with that call's message and kind.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from garminconnect.exceptions import GarminConnectNotFoundError
from .errors import UNEXPECTED_ANSWER
from .garmin_client import GarminService, is_section_error, section_error, section_failure

log = logging.getLogger("garmin_coach_mcp.sections")

UNAVAILABLE: dict[str, Any] = {"available": False}


def error_value(message: str, kind: str) -> dict[str, str]:
    """The default value of a section whose call failed or whose payload could not be shaped."""
    return {"error": message, "error_kind": kind}


@dataclass(frozen=True)
class Call:
    """One library call: `fetch(client)` runs in a worker thread through `GarminService.call`."""

    fetch: Callable[[Any], Any]
    missing_is_empty: bool = False  # Garmin answers 404 for data that does not exist: the shaper gets None
    required: bool = False  # a failure of this call fails the whole tool with its message


@dataclass(frozen=True)
class Section:
    """One key of the answer: `shape(payload_of_call, *payloads_of_uses)`."""

    call: str
    shape: Callable[..., Any]
    uses: tuple[str, ...] = ()  # further calls passed to the shaper as is; a failed one is passed as None
    if_error: Callable[[str], Any] | None = None  # gets the message; None: error_value(message, kind)


def _none_if_missing(fetch: Callable[[Any], Any]) -> Callable[[Any], Any]:
    def run(client: Any) -> Any:
        try:
            return fetch(client)
        except GarminConnectNotFoundError:
            return None

    return run


async def fetch_sections(
    service: GarminService, calls: Mapping[str, Call], sections: Mapping[str, Section]
) -> dict[str, Any]:
    for name, section in sections.items():
        unknown = [c for c in (section.call, *section.uses) if c not in calls]
        if unknown:
            raise ValueError(f"section {name!r} names undeclared calls: {unknown}")
    results = await service.call_many(
        {name: _none_if_missing(c.fetch) if c.missing_is_empty else c.fetch for name, c in calls.items()}
    )
    for name, c in calls.items():
        if c.required and is_section_error(results[name]):
            raise section_failure(results[name])
    out: dict[str, Any] = {}
    for name, section in sections.items():
        payload = results[section.call]
        if is_section_error(payload):
            failed = section_error(payload)
            out[name] = failed if section.if_error is None else section.if_error(failed["error"])
            continue
        extra = [None if is_section_error(results[u]) else results[u] for u in section.uses]
        try:
            out[name] = section.shape(payload, *extra)
        except Exception as exc:  # unexpected payload shape: degrade the section, not the tool
            log.warning("could not shape %s: %s", section.call, type(exc).__name__)
            message = f"unexpected Garmin response for {section.call}"
            out[name] = (
                error_value(message, UNEXPECTED_ANSWER) if section.if_error is None else section.if_error(message)
            )
    return out
