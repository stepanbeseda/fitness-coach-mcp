"""Value guards shared by the read side (``history_view``), the write side (``history``, ``strength``) and the tools.

One rule each, so the modules cannot drift apart: a finite number, a whole number, a Hevy id, a ``YYYY-MM-DD``
tool argument. The module imports nothing from the package, so every other module can import it.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from typing import TypeGuard

from mcp.server.mcpserver.exceptions import ToolError

MAX_ID = 64  # the longest Hevy workout or template id that is kept
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def finite(value: object) -> int | float | None:
    """``value`` itself when it is an int or a float (not a bool) and finite; otherwise None."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return value


def finite_float(value: object) -> float | None:
    """``finite`` as a float (an int becomes ``100.0``); None otherwise."""
    number = finite(value)
    return None if number is None else float(number)


def whole(value: object) -> int | None:
    """An int, or a float that holds a whole number; bool and everything else is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def valid_id(value: object) -> TypeGuard[str]:
    """A Hevy id: text of 1 to ``MAX_ID`` characters."""
    return isinstance(value, str) and 1 <= len(value) <= MAX_ID


def parse_date(value: str, name: str) -> dt.date:
    """A ``YYYY-MM-DD`` tool argument (surrounding spaces allowed); raises ``ToolError`` naming the argument."""
    text = value.strip() if isinstance(value, str) else value
    if not isinstance(text, str) or not _DATE_RE.fullmatch(text):
        raise ToolError(f"{name} must be YYYY-MM-DD, got {value!r}")
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise ToolError(f"{name} is not a valid date: {value!r}") from None
