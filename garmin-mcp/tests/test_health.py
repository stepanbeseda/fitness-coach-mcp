import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import fixtures as fx
from conftest import AUTH_ERROR, CONN_ERROR
from garmin_coach_mcp import health


def test_shape_readiness_prefers_latest_and_reports_morning():
    out = health.shape_readiness(fx.TRAINING_READINESS)
    assert out["score"] == 58
    assert out["level"] == "MODERATE"
    assert out["morning_score"] == 76
    assert out["recovery_time_h"] == 9.0
    assert out["factors"]["hrv"] == {"percent": 70, "feedback": "GOOD", "hrv_weekly_avg_ms": 52}
    assert out["factors"]["sleep"]["sleep_score"] == 71
    assert out["time_local"] == "2026-09-30T14:05"
    assert health.shape_readiness([]) == {"available": False}


def test_shape_readiness_reached_zero_means_recovered():
    snap = dict(fx.TRAINING_READINESS[0], recoveryTimeChangePhrase="REACHED_ZERO", recoveryTime=300)
    assert health.shape_readiness([snap])["recovery_time_h"] == 0


def test_shape_hrv():
    out = health.shape_hrv(fx.HRV)
    assert out == {
        "last_night_avg_ms": 48,
        "last_night_5min_high_ms": 71,
        "weekly_avg_ms": 52,
        "status": "BALANCED",
        "feedback": "HRV_BALANCED_1",
        "baseline_balanced_ms": [47, 60],
        "baseline_low_upper_ms": 44,
    }
    assert health.shape_hrv(None) == {"available": False}


def test_shape_sleep_detail_and_brief():
    out = health.shape_sleep(fx.SLEEP)
    assert out["score"] == 79 and out["quality"] == "FAIR"
    assert out["duration_h"] == 7.05
    assert (out["deep_min"], out["light_min"], out["rem_min"], out["awake_min"]) == (90, 230, 93, 10)
    assert out["start_local"] == "2025-09-29T22:40"
    assert out["avg_overnight_hrv_ms"] == 48
    assert out["components"]["rem_percentage"] == {"value": 22, "quality": "EXCELLENT"}
    assert "sleepMovement" not in json.dumps(out)
    brief = health.shape_sleep(fx.SLEEP, brief=True)
    assert "components" not in brief and brief["score"] == 79
    assert health.shape_sleep({"dailySleepDTO": {"calendarDate": "2026-09-30"}}) == {
        "available": False,
        "date": "2026-09-30",
    }


def test_shape_daily_summary():
    out = health.shape_daily_summary(fx.USER_SUMMARY)
    assert out["steps"] == 8123
    assert out["heart_rate"] == {"resting": 49, "resting_7d_avg": 50, "min": 45, "max": 151}
    assert out["stress"]["high_min"] == 15
    assert out["body_battery"] == {"latest": 40, "at_wake": 85, "high": 88, "low": 27, "charged": 61, "drained": 48}
    assert out["floors_up"] == 8


def test_shape_body_battery_days_sorted_and_computed():
    days = health.shape_body_battery_days(fx.BODY_BATTERY)
    assert [d["date"] for d in days] == ["2026-09-29", "2026-09-30"]
    assert days[0] == {"date": "2026-09-29", "charged": 55, "drained": 60, "start": 70, "high": 70, "low": 25, "latest": 25}
    assert days[1]["high"] == 88 and days[1]["latest"] == 40


def test_shape_training_status_picks_primary_device():
    out = health.shape_training_status(fx.TRAINING_STATUS)
    assert out["status"] == "PRODUCTIVE"
    assert out["load"]["acute"] == 612 and out["load"]["acwr_ratio"] == 1.13
    assert out["load"]["optimal_chronic_range"] == [432, 810]
    assert out["load_balance_4w"]["target_anaerobic"] == [50, 200]
    assert out["vo2max"] == {"running": 53.4, "cycling": 55.1, "measured_on": "2026-09-28"}
    brief = health.shape_training_status(fx.TRAINING_STATUS, brief=True)
    assert brief == {
        "status": "PRODUCTIVE",
        "acute": 612,
        "chronic": 540,
        "acwr_ratio": 1.13,
        "acwr_status": "OPTIMAL",
        "vo2max_running": 53.4,
        "vo2max_cycling": 55.1,
    }
    assert health.shape_training_status({}) == {"available": False}


@pytest.mark.anyio
async def test_recovery_snapshot_all_sections(service, fake):
    out = await health.recovery_snapshot(service, "2026-09-30")
    assert out["date"] == "2026-09-30"
    assert out["readiness"]["score"] == 58
    assert out["hrv"]["status"] == "BALANCED"
    assert out["sleep"]["score"] == 79
    assert out["body_battery"]["at_wake"] == 85
    assert out["resting_hr"] == {"today": 49, "avg_7d": 50}
    assert out["stress"] == {"avg": 27, "max": 91}
    assert out["training_status"]["status"] == "PRODUCTIVE"
    assert len(json.dumps(out)) < 3000
    called = {name for name, _ in fake.calls}
    assert {"get_training_readiness", "get_hrv_data", "get_sleep_data", "get_user_summary", "get_training_status"} <= called


@pytest.mark.anyio
async def test_recovery_snapshot_degrades_per_section(service, fake):
    fake.fail["get_hrv_data"] = CONN_ERROR
    fake.responses["get_training_status"] = {"weird": True}
    out = await health.recovery_snapshot(service, "2026-09-30")
    assert "error" in out["hrv"] and "500" in out["hrv"]["error"]
    assert out["training_status"] == {"available": False}
    assert out["sleep"]["score"] == 79


@pytest.mark.anyio
async def test_recovery_snapshot_auth_error_fails_whole_tool(service, fake):
    fake.fail["get_sleep_data"] = AUTH_ERROR
    with pytest.raises(ToolError, match="garmin-login.sh"):
        await health.recovery_snapshot(service, "2026-09-30")


@pytest.mark.anyio
async def test_not_logged_in(service, tokens_dir):
    (tokens_dir / "garmin_tokens.json").unlink()
    with pytest.raises(ToolError, match="not connected yet"):
        await health.sleep(service, None)


@pytest.mark.anyio
async def test_hrv_trend_range_endpoint(service, fake):
    out = await health.hrv_trend(service, 14, "2026-09-30")
    assert out["start"] == "2026-09-17" and out["end"] == "2026-09-30"
    assert len(out["days"]) == 14
    assert out["days"][0]["date"] == "2026-09-17"
    summary = out["summary"]
    assert summary["days_with_data"] == 14 and "change_pct" in summary


@pytest.mark.anyio
async def test_hrv_trend_falls_back_to_daily_calls(service, fake):
    fake.responses["get_hrv_data_range"] = None
    out = await health.hrv_trend(service, 3, "2026-09-30")
    assert [d["date"] for d in out["days"]] == ["2026-09-28", "2026-09-29", "2026-09-30"]
    assert sum(1 for name, _ in fake.calls if name == "get_hrv_data") == 3


@pytest.mark.anyio
async def test_hrv_trend_validates_days(service):
    with pytest.raises(ToolError):
        await health.hrv_trend(service, 60, None)


@pytest.mark.anyio
async def test_body_battery_range_limit(service):
    out = await health.body_battery(service, "2026-09-29", "2026-09-30")
    assert out["start"] == "2026-09-29" and len(out["days"]) == 2
    with pytest.raises(ToolError):
        await health.body_battery(service, "2026-01-01", "2026-09-30")
