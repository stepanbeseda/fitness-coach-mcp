"""The confirmation buttons and the automatic confirmation note (confirm.py): pure, exact catalogue literals."""

from __future__ import annotations

from typing import Any

import pytest

from coach_mcp import confirm
from coach_mcp.messages import reply_texts, texts
from coach_mcp.push import Button


def session(sid: int, kind: str, status: str = "planned") -> dict[str, Any]:
    return {"id": sid, "type": kind, "status": status, "date": "2026-10-06"}


def pairs(rows: tuple[tuple[Button, ...], ...]) -> list[list[tuple[str, str]]]:
    return [[(b.text, b.data) for b in row] for row in rows]


def rows_of(language: str, sessions: list[dict[str, Any]]) -> tuple[tuple[Button, ...], ...]:
    return confirm.rows(texts(language), reply_texts(language), sessions)


def test_parse_data_accepts_the_three_actions():
    assert confirm.parse_data("d:12") == ("d", 12)
    assert confirm.parse_data("s:999999999") == ("s", 999999999)
    assert confirm.parse_data("x:1") == ("x", 1)


@pytest.mark.parametrize(
    "data",
    [
        None,
        12,
        True,
        b"d:1",
        "",
        "d:",
        "d:0",
        "d:012",
        "d:1234567890",
        "q:1",
        "D:1",
        "d:12 ",
        " d:12",
        "d:12\n",
        "d:１２",
        "d:-1",
        "d:1:2",
        "d:" + "1" * 62,
        "d:" + "1" * 63,
    ],
)
def test_parse_data_rejects_everything_else(data):
    assert confirm.parse_data(data) is None


def test_rows_of_planned_sessions_en():
    assert pairs(rows_of("en", [session(12, "running"), session(14, "mobility")])) == [
        [("✅ #12 run: done", "d:12"), ("⏭ skipped", "s:12")],
        [("✅ #14 mobility: done", "d:14"), ("⏭ skipped", "s:14")],
    ]


def test_rows_of_planned_sessions_cs():
    assert pairs(rows_of("cs", [session(12, "running"), session(13, "strength")])) == [
        [("✅ #12 běh: splněno", "d:12"), ("⏭ přeskočeno", "s:12")],
        [("✅ #13 síla: splněno", "d:13"), ("⏭ přeskočeno", "s:13")],
    ]


def test_rows_of_marked_sessions():
    sessions = [
        session(12, "running", "done"),
        session(20, "running", "moved"),
        session(13, "strength", "skipped"),
        session(21, "running", "superseded"),
        session(14, "mobility"),
    ]
    assert pairs(rows_of("en", sessions)) == [
        [("#12 run: ✅ done", "x:12")],
        [("#13 strength: ⏭ skipped", "x:13")],
        [("✅ #14 mobility: done", "d:14"), ("⏭ skipped", "s:14")],
    ]
    assert pairs(rows_of("cs", sessions[:3])) == [[("#12 běh: ✅ splněno", "x:12")], [("#13 síla: ⏭ přeskočeno", "x:13")]]


def test_rows_keep_at_most_max_rows():
    nine = [session(i, "running") for i in range(1, 10)]
    assert [row[0].data for row in rows_of("en", nine)] == [f"d:{i}" for i in range(1, 9)]
    assert len(rows_of("en", nine[:8])) == 8


def test_ids_of_reads_only_well_formed_buttons():
    def markup(*rows: Any) -> dict[str, Any]:
        return {"inline_keyboard": list(rows)}

    planned = [{"text": "✅ #12 run: done", "callback_data": "d:12"}, {"text": "⏭ skipped", "callback_data": "s:12"}]
    assert confirm.ids_of(markup(planned, [{"text": "#14 mobility: ✅ done", "callback_data": "x:14"}])) == [12, 14]
    assert confirm.ids_of(markup(planned, planned)) == [12]
    odd = markup(
        [{"text": "buy", "callback_data": "buy:1"}],
        [{"text": "web", "url": "https://example.org"}],
        ["not a button"],
        "not a row",
        [{"text": "late", "callback_data": "buy:2"}, {"text": "ok", "callback_data": "s:7"}],
    )
    assert confirm.ids_of(odd) == [7]
    for shape in (None, "x", {"inline_keyboard": "x"}, {}, [], {"inline_keyboard": None}):
        assert confirm.ids_of(shape) == []
    nine = markup(*[[{"text": "t", "callback_data": f"d:{i}"}] for i in range(1, 10)])
    assert confirm.ids_of(nine) == list(range(1, 9))


GARMIN_RUN = {"source": "garmin", "sport": "running", "count": 1, "distance_km": 5.05, "duration_min": 31.0}
HEVY = {"source": "hevy", "sport": "strength", "count": 1, "duration_min": 55.0, "sets": 12, "volume_kg": 4200.5}
COUNT_ONLY = {"source": "garmin", "sport": "cycling", "count": 1}


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "en",
            [
                "Confirmed automatically (Garmin): run, 5.05 km, 31 min.",
                "Confirmed automatically (Hevy): strength, 55 min, working sets 12, 4200.5 kg.",
                "Confirmed automatically (Garmin): ride.",
            ],
        ),
        (
            "cs",
            [
                "Automaticky potvrzeno (Garmin): běh, 5,05 km, 31 min.",
                "Automaticky potvrzeno (Hevy): síla, 55 min, pracovní série 12, 4200,5 kg.",
                "Automaticky potvrzeno (Garmin): kolo.",
            ],
        ),
    ],
)
def test_auto_note_literals(language, expected):
    t, r = texts(language), reply_texts(language)
    cases = [(session(12, "running"), GARMIN_RUN), (session(13, "strength"), HEVY), (session(15, "cycling"), COUNT_ONLY)]
    assert [confirm.auto_note(t, r, s, evidence) for s, evidence in cases] == expected


def test_auto_note_whole_distance_has_two_decimals_and_unknown_source_is_garmin():
    note = confirm.auto_note(texts("en"), reply_texts("en"), session(12, "running"), {"source": "x", "distance_km": 5})
    assert note == "Confirmed automatically (Garmin): run, 5.00 km."


def test_label():
    t = texts("en")
    assert confirm.label(t, session(12, "running")) == "#12 run"
    assert confirm.label(t, session(14, "mobility")) == "#14 mobility"
    assert confirm.label(t, session(15, "yoga-ish")) == "#15 other"
