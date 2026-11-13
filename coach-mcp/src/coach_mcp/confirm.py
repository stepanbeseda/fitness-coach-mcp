"""Confirmation buttons of the morning push and the automatic confirmation note.

Pure: no I/O, no clock. A button carries ``callback_data`` of the grammar ``<action>:<session id>``, where the
action is ``d`` (mark done), ``s`` (mark skipped) or ``x`` (a result row: a tap changes nothing) and the id is a
positive decimal integer of at most nine digits without a leading zero (at most 11 bytes, inside the Bot API limit
of 64). Everything a keyboard shows is rendered from the store and the catalogue; a keyboard received back from
Telegram is read only for its session ids, never for its texts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .briefing import _type_name, fmt, fmt_ratio
from .messages import ReplyTexts, Texts
from .push import Button

MAX_ROWS = 8  # rows of one keyboard; more planned sessions of one day are not offered (the coach still sees them)
ACTIONS = {"d": "done", "s": "skipped"}  # "x": a result row, a tap changes nothing
_DATA_RE = re.compile(r"([dsx]):([1-9][0-9]{0,8})")
_SOURCES = {"garmin": "Garmin", "hevy": "Hevy"}  # proper nouns, not catalogue text


def parse_data(data: Any) -> tuple[str, int] | None:
    """(action letter, session id) of a button's callback data, or None for anything else."""
    if not isinstance(data, str):
        return None
    match = _DATA_RE.fullmatch(data)
    return None if match is None else (match.group(1), int(match.group(2)))


def label(t: Texts, session: Mapping[str, Any]) -> str:
    """``#12 run``: the session id and its type name in the language of ``t``."""
    return f"#{session['id']} {_type_name(t, session.get('type'))}"


def rows(t: Texts, r: ReplyTexts, sessions: Sequence[Mapping[str, Any]]) -> tuple[tuple[Button, ...], ...]:
    """One row per session in the given order, at most MAX_ROWS: planned -> done and skipped buttons; done or
    skipped -> one result button (data x:<id>); any other status -> no row."""
    out: list[tuple[Button, ...]] = []
    for session in sessions:
        if len(out) == MAX_ROWS:
            break
        sid, name, status = session["id"], label(t, session), session.get("status")
        if status == "planned":
            out.append((Button(r.button_done.format(label=name), f"d:{sid}"), Button(r.button_skipped, f"s:{sid}")))
        elif status == "done":
            out.append((Button(r.button_marked_done.format(label=name), f"x:{sid}"),))
        elif status == "skipped":
            out.append((Button(r.button_marked_skipped.format(label=name), f"x:{sid}"),))
    return tuple(out)


def ids_of(markup: Any) -> list[int]:
    """Session ids of a received inline keyboard in row order, each once, at most MAX_ROWS; only buttons whose
    callback_data parse_data accepts count; any other shape gives []."""
    if not isinstance(markup, Mapping):
        return []
    keyboard = markup.get("inline_keyboard")
    if not isinstance(keyboard, list):
        return []
    ids: list[int] = []
    for row in keyboard[:MAX_ROWS]:
        if not isinstance(row, list):
            continue
        for button in row:
            parsed = parse_data(button.get("callback_data")) if isinstance(button, Mapping) else None
            if parsed is not None:
                if parsed[1] not in ids:
                    ids.append(parsed[1])
                break
    return ids


def auto_note(t: Texts, r: ReplyTexts, session: Mapping[str, Any], evidence: Mapping[str, Any]) -> str:
    """r.auto_confirmed with the source and the evidence: the session's type name, then distance, duration, working
    sets and volume, each only when the evidence has it."""
    sep = t.decimal_separator
    parts = [_type_name(t, session.get("type"))]
    if "distance_km" in evidence:
        parts.append(f"{fmt_ratio(evidence['distance_km'], sep)} km")
    if "duration_min" in evidence:
        parts.append(f"{fmt(evidence['duration_min'], sep)} min")
    if "sets" in evidence:
        parts.append(r.evidence_sets.format(sets=evidence["sets"]))
    if "volume_kg" in evidence:
        parts.append(f"{fmt(evidence['volume_kg'], sep)} kg")
    source = _SOURCES.get(str(evidence.get("source")), "Garmin")
    return r.auto_confirmed.format(source=source, evidence=", ".join(parts))
