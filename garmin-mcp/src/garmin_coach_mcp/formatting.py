"""Small helpers that turn raw Garmin payloads into compact, LLM-friendly values."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.mcpserver.exceptions import ToolError

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def to_json(value: Any) -> str:
    """Compact JSON for tool results."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def today(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()


def resolve_date(value: str | None, tz: str, name: str = "date") -> date:
    """Parse YYYY-MM-DD (or default to today in the configured timezone)."""
    if value is None or str(value).strip() in ("", "today"):
        return today(tz)
    text = str(value).strip()
    if text == "yesterday":
        return today(tz) - timedelta(days=1)
    if not _DATE_RE.match(text):
        raise ToolError(f"{name} must be YYYY-MM-DD, got {value!r}")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ToolError(f"{name} is not a valid date: {value!r}") from exc


def resolve_range(
    start: str | None, end: str | None, tz: str, *, default_days: int, max_days: int
) -> tuple[date, date]:
    end_d = resolve_date(end, tz, "end_date")
    start_d = resolve_date(start, tz, "start_date") if start else end_d - timedelta(days=default_days - 1)
    if start_d > end_d:
        raise ToolError("start_date must be on or before end_date")
    if (end_d - start_d).days + 1 > max_days:
        raise ToolError(f"date range too long (max {max_days} days)")
    return start_d, end_d


def num(value: Any, digits: int = 1) -> float | int | None:
    """Round numbers; drop NaN/None/non-numbers; keep ints as ints."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    rounded = round(f, digits)
    return int(rounded) if digits == 0 else rounded


def minutes(seconds: Any, digits: int = 1) -> float | int | None:
    s = num(seconds, 3)
    return None if s is None else num(s / 60, digits)


def hours(seconds: Any, digits: int = 2) -> float | None:
    s = num(seconds, 3)
    return None if s is None else num(s / 3600, digits)


def km(meters: Any, digits: int = 2) -> float | None:
    m = num(meters, 3)
    return None if m is None else num(m / 1000, digits)


def clock(seconds: Any) -> str | None:
    """Seconds -> H:MM:SS or M:SS."""
    s = num(seconds, 0)
    if s is None or s < 0:
        return None
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def pace_per_km(speed_mps: Any) -> str | None:
    """m/s -> 'M:SS' per km (None for zero/None speed)."""
    v = num(speed_mps, 6)
    if not v or v <= 0:
        return None
    return clock(1000 / v)


def speed_kmh(speed_mps: Any) -> float | None:
    v = num(speed_mps, 6)
    return None if v is None else num(v * 3.6, 1)


def parse_pace(text: str) -> float:
    """'M:SS' (per km) -> m/s. Raises ValueError for nonsense."""
    match = re.fullmatch(r"\s*(\d{1,2}):([0-5]\d)\s*", text or "")
    if not match:
        raise ValueError(f"pace must look like '5:15' (min:sec per km), got {text!r}")
    total = int(match.group(1)) * 60 + int(match.group(2))
    if not 120 <= total <= 1200:
        raise ValueError(f"pace {text!r} is outside 2:00-20:00 per km")
    return 1000 / total


def local_ms_to_iso(ms: Any) -> str | None:
    """Garmin '...Local' epoch-ms values already encode local wall time."""
    v = num(ms, 0)
    if v is None:
        return None
    return datetime.fromtimestamp(v / 1000, UTC).strftime("%Y-%m-%dT%H:%M")


def gmt_ms_to_local_iso(ms: Any, tz: str) -> str | None:
    v = num(ms, 0)
    if v is None:
        return None
    return datetime.fromtimestamp(v / 1000, UTC).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M")


def local_str(value: Any) -> str | None:
    """'2026-09-30 07:12:33' / '2026-09-30T07:12:33.0' -> '2026-09-30T07:12'."""
    if not value or not isinstance(value, str):
        return None
    text = value.replace(" ", "T")
    return text[:16]


def phrase_label(value: Any) -> str | None:
    """'PRODUCTIVE_6' -> 'PRODUCTIVE' (Garmin feedback phrases carry a variant suffix)."""
    if not value or not isinstance(value, str):
        return None
    return re.sub(r"_\d+$", "", value)


def clean(value: Any) -> Any:
    """Recursively drop None values and empty containers."""
    if isinstance(value, dict):
        out = {k: clean(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, {}, [])}
    if isinstance(value, list):
        return [clean(v) for v in value if v is not None]
    return value


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and value:
        return [value]
    return []


def pick_primary_device(mapping: Any) -> dict[str, Any]:
    """Garmin keys some sections by device id; prefer the primary training device."""
    first: dict[str, Any] = {}
    for item in as_dict(mapping).values():
        if not isinstance(item, dict) or not item:
            continue
        if item.get("primaryTrainingDevice"):
            return item
        first = first or item
    return first
