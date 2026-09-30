import pytest
from mcp.server.mcpserver.exceptions import ToolError

import fixtures as fx
from conftest import CONN_ERROR
from garmin_coach_mcp import activities


def test_row_shapes_per_sport():
    run, bike, strength = (activities.shape_activity_row(a) for a in fx.ACTIVITIES)
    assert run["avg_pace_min_per_km"] == "5:00"
    assert run["distance_km"] == 10.02 and run["duration_min"] == 50.1
    assert run["avg_cadence_spm"] == 172 and run["te_label"] == "TEMPO"
    assert "avg_speed_kmh" not in run
    assert bike["avg_speed_kmh"] == 27.4 and bike["norm_power_w"] == 205
    assert "avg_pace_min_per_km" not in bike and "max_speed_kmh" not in bike
    assert strength["sets"] == 18 and "distance_km" not in strength


@pytest.mark.anyio
async def test_list_activities_sorted_and_limited(service, fake):
    out = await activities.list_activities(service, 7, None, "2026-09-30", None, 2)
    assert out["start"] == "2026-09-24" and out["count"] == 3
    assert [a["id"] for a in out["activities"]] == [20001, 20003]
    assert out["truncated"] is True
    name, args = next(c for c in fake.calls if c[0] == "get_activities_by_date")
    assert args == ("2026-09-24", "2026-09-30", None)


@pytest.mark.anyio
async def test_list_activities_validates(service):
    with pytest.raises(ToolError):
        await activities.list_activities(service, 7, None, None, None, 500)


@pytest.mark.anyio
async def test_get_activity_detail(service):
    out = await activities.get_activity(service, 20001)
    assert out["name"] == "Praha Běh" and out["type"] == "running"
    assert out["duration"] == "50:05" and out["avg_pace_min_per_km"] == "5:00"
    assert len(out["laps"]) == 10 and out["laps"][0]["duration"] == "4:59"
    assert [z["zone"] for z in out["hr_zones"]] == [1, 2, 3, 4]
    assert out["hr_zones"][2] == {"zone": 3, "from_bpm": 140, "minutes": 25.0, "percent": 50}


@pytest.mark.anyio
async def test_get_activity_tolerates_missing_splits(service, fake):
    fake.fail["get_activity_splits"] = CONN_ERROR
    out = await activities.get_activity(service, 20001)
    assert "laps_error" in out and out["name"] == "Praha Běh"


@pytest.mark.anyio
async def test_get_activity_main_failure_raises(service, fake):
    fake.fail["get_activity"] = CONN_ERROR
    with pytest.raises(ToolError, match="500"):
        await activities.get_activity(service, 20001)
