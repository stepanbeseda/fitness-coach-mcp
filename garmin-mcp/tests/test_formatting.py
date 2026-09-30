from datetime import date

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from garmin_coach_mcp import formatting as f


def test_pace_roundtrip():
    assert f.pace_per_km(f.parse_pace("5:15")) == "5:15"
    assert f.pace_per_km(1000 / 300) == "5:00"
    assert f.pace_per_km(0) is None


@pytest.mark.parametrize("bad", ["5", "5:7", "1:30", "25:00", "abc", ""])
def test_parse_pace_rejects(bad):
    with pytest.raises(ValueError):
        f.parse_pace(bad)


def test_clock_and_units():
    assert f.clock(3005.4) == "50:05"
    assert f.clock(3725) == "1:02:05"
    assert f.minutes(90) == 1.5
    assert f.km(10020) == 10.02
    assert f.speed_kmh(7.6) == 27.4
    assert f.num(float("nan")) is None
    assert f.num(True) is None


def test_local_timestamps():
    assert f.local_ms_to_iso(1759185600000) == "2025-09-29T22:40"
    assert f.local_str("2026-09-29 18:02:11") == "2026-09-29T18:02"
    assert f.gmt_ms_to_local_iso(1759178400000, "Europe/Prague") == "2025-09-29T22:40"


def test_resolve_date():
    assert f.resolve_date("2026-09-30", "Europe/Prague") == date(2026, 9, 30)
    assert f.resolve_date(None, "Europe/Prague") == f.today("Europe/Prague")
    with pytest.raises(ToolError):
        f.resolve_date("30.9.2026", "Europe/Prague")
    with pytest.raises(ToolError):
        f.resolve_date("2026-02-30", "Europe/Prague")


def test_resolve_range_limits():
    start, end = f.resolve_range(None, "2026-09-30", "UTC", default_days=7, max_days=31)
    assert (start, end) == (date(2026, 9, 24), date(2026, 9, 30))
    with pytest.raises(ToolError):
        f.resolve_range("2026-10-02", "2026-09-30", "UTC", default_days=7, max_days=31)
    with pytest.raises(ToolError):
        f.resolve_range("2026-01-01", "2026-09-30", "UTC", default_days=7, max_days=31)


def test_clean_and_helpers():
    assert f.clean({"a": None, "b": {}, "c": [None, 1], "d": {"e": None}}) == {"c": [1]}
    assert f.phrase_label("PRODUCTIVE_6") == "PRODUCTIVE"
    assert f.phrase_label("NO_STATUS") == "NO_STATUS"
    assert f.pick_primary_device({"1": {"x": 1}, "2": {"primaryTrainingDevice": True, "x": 2}}) == {
        "primaryTrainingDevice": True,
        "x": 2,
    }
    assert f.as_list({"a": 1}) == [{"a": 1}]
    assert f.as_list(None) == []
