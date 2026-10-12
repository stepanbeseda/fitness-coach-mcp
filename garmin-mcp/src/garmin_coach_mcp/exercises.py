"""Garmin's strength exercise catalogue: display names and a ranked search.

The catalogue is ``garminconnect.exercises.EXERCISES`` of the pinned library (read at import, no Garmin call).
The library's own ``find`` is a substring match in catalogue order, so a Hevy title such as
"Bench Press (Barbell)" or "pull up" finds nothing there. The search here is a word match: the words of the
query and of each exercise name are lower-cased and given one plural rule, a compound word is split where both
parts are catalogue words, rare words weigh more than common ones, and ties are broken deterministically.
"""

from __future__ import annotations

import math
import re
from typing import Any

from garminconnect import exercises as _catalogue

MIN_SCORE = 0.3
_WORD = re.compile(r"[a-z0-9]+")
_ES_PLURALS = ("ches", "shes", "sses", "xes")


def _stem(word: str) -> str:
    if word.endswith(_ES_PLURALS):
        return word[:-2]
    if len(word) > 2 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _tokens(text: str) -> list[str]:
    return [_stem(word) for word in _WORD.findall(text.lower())]


_ENTRIES: list[tuple[dict[str, str], frozenset[str]]] = [
    (entry, frozenset(_tokens(entry["name"]))) for entry in _catalogue.EXERCISES
]
_DISPLAY: dict[tuple[str, str], str] = {(e["category"], e["exercise"]): e["name"] for e in _catalogue.EXERCISES}
_COUNT: dict[str, int] = {}
for _, _words in _ENTRIES:
    for _word in _words:
        _COUNT[_word] = _COUNT.get(_word, 0) + 1


def display_name(category: Any, name: Any) -> str | None:
    """Garmin's display name of an exact (category, exercise name) pair, None when the catalogue has no such pair."""
    if not isinstance(category, str) or not isinstance(name, str):
        return None
    return _DISPLAY.get((category, name))


def _weight(token: str) -> float:
    return math.log((len(_ENTRIES) + 1) / (_COUNT.get(token, 0) + 1))


def _query_tokens(text: str) -> frozenset[str]:
    out: set[str] = set()
    for token in _tokens(text):
        if token in _COUNT:
            out.add(token)
            continue
        for cut in range(1, len(token)):  # the first split (from the left) into two catalogue words
            if token[:cut] in _COUNT and token[cut:] in _COUNT:
                out.update((token[:cut], token[cut:]))
                break
        else:
            out.add(token)
    return frozenset(out)


def _score(query: frozenset[str], entry: frozenset[str]) -> float:
    common = sum(_weight(t) for t in query & entry)
    if not common:
        return 0.0
    return round(2 * common / (sum(_weight(t) for t in query) + sum(_weight(t) for t in entry)), 3)


def find(query: str, limit: int) -> dict[str, Any]:
    """The best ``limit`` catalogue entries for a title, best first; an empty list when nothing is close."""
    words = _query_tokens(query)
    hits = []
    for entry, entry_words in _ENTRIES:
        score = _score(words, entry_words)
        if score >= MIN_SCORE:
            hits.append((-score, len(entry["name"]), entry["name"], entry))
    hits.sort(key=lambda hit: hit[:3])
    matches = [
        {"exercise": e["name"], "category": e["category"], "name": e["exercise"], "score": -negative}
        for negative, _, _, e in hits[:limit]
    ]
    return {"query": query, "count": len(matches), "matches": matches}
