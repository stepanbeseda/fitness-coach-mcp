"""Per-session advice for today's planned sessions (PR 41): verdict x session class -> ok / reduce / swap.

Pure: no I/O, no clock and no words. The words live in ``messages.py`` (``Texts.session_advice`` and the fields after
it), the line layout in ``briefing.py``. The class is read from the stored session: ``type`` decides strength and
mobility / other; for running, cycling and swimming a fixed keyword table over the free-text ``intensity`` and
``description`` decides easy or hard, and text the table cannot read counts as hard (on yellow it is kept in zone 2,
which is harmless for an easy session). Sets come from ``N×M`` schemes in the description, because coach-mcp may not
read Hevy routines (plan section 1). A later rule (PR 44: back pain turns strength into swap) raises the action with
``floors`` without touching ``MATRIX``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

Action = Literal["ok", "reduce", "swap"]
SessionClass = Literal["easy_endurance", "hard_endurance", "strength", "mobility_other"]
Reason = Literal["verdict"]

ACTIONS: tuple[Action, ...] = ("ok", "reduce", "swap")  # ordered: a later action is stronger
CLASSES: tuple[SessionClass, ...] = ("easy_endurance", "hard_endurance", "strength", "mobility_other")
REASONS: tuple[Reason, ...] = ("verdict",)
ENDURANCE_TYPES = frozenset({"running", "cycling", "swimming"})

# Green has no row: on a green day no session gets advice (the verdict line already says "full plan").
MATRIX: Mapping[str, Mapping[SessionClass, Action]] = {
    "yellow": {"easy_endurance": "ok", "hard_endurance": "reduce", "strength": "reduce", "mobility_other": "ok"},
    "red": {"easy_endurance": "reduce", "hard_endurance": "swap", "strength": "swap", "mobility_other": "ok"},
}
STRENGTH_RPE_MAX = 7
EASY_ZONE_MAX = 2
RED_EASY_MAX_MIN = 40
MAX_SCHEMES = 4

_RANK = {action: i for i, action in enumerate(ACTIONS)}
NO_FLOORS: Mapping[SessionClass, tuple[Action, Reason]] = MappingProxyType({})

# Word starts, case-insensitive, Czech and English. Hard is checked before easy: "Z2, rovinky svižně" is hard.
_HARD = re.compile(
    r"\b(?:interval|tempo|rovink|stride|závod|race|fartlek|threshold|práh|prahov|sprint|kopc|hill)", re.IGNORECASE
)
_EASY = re.compile(r"\b(?:lehk|lehce|easy|regener|recovery|volně|pohodov)", re.IGNORECASE)
# "Z2", "Z1–Z2", "zóna 2", "zóně 1–2", "zone 3-4": every zone number named.
_ZONE = re.compile(r"(?:\bz|\bzón\w*\s*|\bzone\s*)([1-5])(?:\s*[–-]\s*z?([1-5]))?(?!\d)", re.IGNORECASE)
# "3×8", "3x10", "4 × 8–10"; not "6× 20 s", "6×800 m", "4x4 min", "4×8–10 min" (a duration or distance, not reps).
# The rep range is possessive, so the unit guard cannot be dodged by backtracking out of "8–10".
_SCHEME = re.compile(
    r"(?<![\d.,])(\d{1,2})\s*[×xX]\s*(\d{1,3}(?:\s*[–-]\s*\d{1,3})?+)(?!\d)(?!\s*(?:s|sec|sek|min|m|km|h)\b)"
)
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Advice:
    session_id: Any
    action: Action
    cls: SessionClass
    reason: Reason
    change: Mapping[str, Any]

    def payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "action": self.action,
            "class": self.cls,
            "reason": self.reason,
            "change": {k: list(v) if isinstance(v, list) else v for k, v in self.change.items()},
        }


def _text(session: Mapping[str, Any]) -> str:
    return f"{session.get('intensity') or ''} {session.get('description') or ''}"


def classify(session: Mapping[str, Any]) -> SessionClass:
    kind = session.get("type")
    if kind == "strength":
        return "strength"
    if kind not in ENDURANCE_TYPES:
        return "mobility_other"
    text = _text(session)
    zones = [int(z) for m in _ZONE.finditer(text) for z in m.groups() if z]
    if _HARD.search(text) or (zones and max(zones) > EASY_ZONE_MAX):
        return "hard_endurance"
    if _EASY.search(text) or zones:
        return "easy_endurance"
    return "hard_endurance"  # unreadable endurance text counts as hard


def reduced_sets(description: str) -> tuple[list[dict[str, str]], bool]:
    """(distinct {"from", "to"} pairs of schemes with at least 2 sets, first MAX_SCHEMES; no scheme read at all)."""
    found = list(_SCHEME.finditer(description))
    pairs: list[dict[str, str]] = []
    for match in found:
        sets = int(match[1])
        reps = _WS.sub("", match[2]).replace("-", "–")
        pair = {"from": f"{sets}×{reps}", "to": f"{sets - 1}×{reps}"}
        if sets >= 2 and pair not in pairs:
            pairs.append(pair)
    return pairs[:MAX_SCHEMES], not found


def _change(action: Action, cls: SessionClass, session: Mapping[str, Any]) -> dict[str, Any]:
    if action == "ok":
        return {}
    if action == "swap":
        return {"to": "recovery"}
    if cls == "easy_endurance":
        return {"zone_max": EASY_ZONE_MAX, "max_minutes": RED_EASY_MAX_MIN}
    if cls == "hard_endurance":
        return {"zone_max": EASY_ZONE_MAX}
    if cls == "strength":
        pairs, generic = reduced_sets(str(session.get("description") or ""))
        return {"rpe_max": STRENGTH_RPE_MAX, "sets": pairs, "minus_one_set": generic}
    return {}  # mobility / other reduced only through a floor


def session_advice(
    level: str,
    session: Mapping[str, Any],
    *,
    floors: Mapping[SessionClass, tuple[Action, Reason]] = NO_FLOORS,
) -> Advice | None:
    """The advice for one of today's sessions, or None (not ``planned``, or green without a floor).

    The action is the matrix cell for (level, class); a floor for the class replaces it, with its own reason, when
    it is at least as strong (on a tie the floor's reason wins). A floor of "ok" never applies.
    """
    if session.get("status") != "planned":
        return None
    cls = classify(session)
    action: Action | None = MATRIX.get(level, {}).get(cls)
    reason: Reason = "verdict"
    floor = floors.get(cls)
    if floor is not None and floor[0] != "ok" and _RANK[floor[0]] >= (_RANK[action] if action is not None else 0):
        action, reason = floor
    if action is None:
        return None
    return Advice(session.get("id"), action, cls, reason, _change(action, cls, session))
