import dataclasses
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


# ------------------------------------------------------------ device status (PR 17)

UPLOAD_UTC = "2026-09-30T04:58:12Z"
LAST_UPLOAD = {"device": "Example Watch 1", "time_utc": UPLOAD_UTC, "time_local": "2026-09-30T06:58"}
PRIMARY_DEVICE = {
    "name": "Example Watch 1", "status": "active", "primary_activity_tracker": True, "registered_devices": 1,
}


def _upload(**over):
    return {**fx.DEVICE_LAST_USED, **over}


def test_shape_last_upload_utc_to_the_second_and_local_to_the_minute():
    assert health.shape_last_upload(fx.DEVICE_LAST_USED, "Europe/Prague") == LAST_UPLOAD
    assert health.shape_last_upload(fx.DEVICE_LAST_USED, "America/New_York")["time_local"] == "2026-09-30T00:58"
    assert health.shape_last_upload(fx.DEVICE_LAST_USED, "America/New_York")["time_utc"] == UPLOAD_UTC


def test_shape_last_upload_bounds_are_2000_inclusive_to_2100_exclusive():
    low, high = health.UPLOAD_MS_MIN, health.UPLOAD_MS_MAX
    out = health.shape_last_upload(_upload(lastUsedDeviceUploadTime=low), "Europe/Prague")
    assert out["time_utc"] == "2000-01-01T00:00:00Z"
    assert health.shape_last_upload(_upload(lastUsedDeviceUploadTime=low - 1), "Europe/Prague") == {"available": False}
    out = health.shape_last_upload(_upload(lastUsedDeviceUploadTime=high - 1), "Europe/Prague")
    assert out["time_utc"] == "2099-12-31T23:59:59Z" and out["time_local"] == "2100-01-01T00:59"
    assert health.shape_last_upload(_upload(lastUsedDeviceUploadTime=high), "Europe/Prague") == {"available": False}


@pytest.mark.parametrize(
    "raw",
    [
        _upload(lastUsedDeviceUploadTime=True),
        _upload(lastUsedDeviceUploadTime=1_790_744_292_000.0),
        _upload(lastUsedDeviceUploadTime="1790744292000"),
        _upload(lastUsedDeviceUploadTime=None),
        {k: v for k, v in fx.DEVICE_LAST_USED.items() if k != "lastUsedDeviceUploadTime"},
        {},  # the 204 answer of the library
        [fx.DEVICE_LAST_USED],
        None,
    ],
    ids=["bool", "float", "str", "none", "missing", "204", "list", "null"],
)
def test_shape_last_upload_is_unavailable_for_an_unusable_time(raw):
    assert health.shape_last_upload(raw, "Europe/Prague") == {"available": False}


def test_shape_last_upload_name_is_optional():
    without = {k: v for k, v in fx.DEVICE_LAST_USED.items() if k != "lastUsedDeviceName"}
    expected = {"time_utc": UPLOAD_UTC, "time_local": "2026-09-30T06:58"}
    assert health.shape_last_upload(without, "Europe/Prague") == expected
    assert health.shape_last_upload(_upload(lastUsedDeviceName="  "), "Europe/Prague") == expected
    assert health.shape_last_upload(_upload(lastUsedDeviceName=7), "Europe/Prague") == expected
    padded = health.shape_last_upload(_upload(lastUsedDeviceName=" Example Watch 1 "), "Europe/Prague")
    assert padded["device"] == "Example Watch 1"


def test_shape_devices_primary_tracker_and_count():
    assert health.shape_devices(fx.DEVICES) == PRIMARY_DEVICE
    scale = fx.device(productDisplayName="Example Scale", primaryActivityTrackerIndicator=False)
    out = health.shape_devices([scale, fx.device()])
    assert out == {**PRIMARY_DEVICE, "registered_devices": 2}


def test_shape_devices_fallback_is_the_first_and_the_flag_must_be_true():
    first = fx.device(productDisplayName="A", primaryActivityTrackerIndicator=False)
    second = fx.device(productDisplayName="B", primaryActivityTrackerIndicator="true")
    out = health.shape_devices([first, second])
    assert out["name"] == "A" and out["primary_activity_tracker"] is False and out["registered_devices"] == 2
    # a single device is the fallback; its truthy-but-not-True flag is not reported as a primary tracker
    only = health.shape_devices([fx.device(primaryActivityTrackerIndicator="true")])
    assert only["primary_activity_tracker"] is False


@pytest.mark.parametrize(
    "raw", [[], fx.device(), None, "x", 5, ["x", 1]], ids=["empty", "dict", "none", "str", "int", "junk"]
)
def test_shape_devices_unavailable(raw):
    assert health.shape_devices(raw) == {"available": False}


def test_shape_devices_skips_non_dict_items():
    out = health.shape_devices(["x", 1, fx.device()])
    assert out["registered_devices"] == 1 and out["name"] == "Example Watch 1"


def test_shape_devices_drops_unusable_name_and_status():
    out = health.shape_devices([fx.device(productDisplayName=None, deviceStatus=None)])
    assert out == {"primary_activity_tracker": True, "registered_devices": 1}
    for bad in ("  ", "", 7):
        out = health.shape_devices([fx.device(productDisplayName=bad, deviceStatus=bad)])
        assert out == {"primary_activity_tracker": True, "registered_devices": 1}, bad


@pytest.mark.parametrize(
    "raw",
    [
        fx.DEVICES,
        [fx.device(productDisplayName="Example Scale", primaryActivityTrackerIndicator=False), fx.device()],
        [fx.device(productDisplayName=None, deviceStatus=None)],
    ],
)
def test_shape_devices_never_returns_ids_or_serial_numbers(raw):
    text = json.dumps(health.shape_devices(raw))
    assert "0000000000" not in text and "123456789" not in text


@pytest.mark.anyio
async def test_device_status_returns_both_sections(service, fake):
    out = await health.device_status(service)
    assert out == {"last_upload": LAST_UPLOAD, "primary_device": PRIMARY_DEVICE}
    assert ("get_device_last_used", ()) in fake.calls and ("get_devices", ()) in fake.calls


@pytest.mark.anyio
async def test_device_status_uses_the_configured_time_zone(settings, fake):
    from garmin_coach_mcp.garmin_client import GarminService

    new_york = GarminService(dataclasses.replace(settings, timezone="America/New_York"), garmin_factory=lambda: fake)
    out = await health.device_status(new_york)
    assert out["last_upload"]["time_local"] == "2026-09-30T00:58"
    assert out["last_upload"]["time_utc"] == UPLOAD_UTC


@pytest.mark.anyio
async def test_device_status_is_fail_soft_per_section(service, fake):
    fake.fail["get_devices"] = CONN_ERROR
    out = await health.device_status(service)
    assert "error" in out["primary_device"] and out["last_upload"] == LAST_UPLOAD

    fake.fail.clear()
    fake.responses["get_devices"] = 5  # the shaper sees an int: unavailable, not an error
    out = await health.device_status(service)
    assert out["primary_device"] == {"available": False} and out["last_upload"] == LAST_UPLOAD

    fake.fail["get_device_last_used"] = CONN_ERROR
    out = await health.device_status(service)
    assert "error" in out["last_upload"] and out["primary_device"] == {"available": False}


@pytest.mark.anyio
async def test_device_status_auth_error_fails_whole_tool(service, fake):
    fake.fail["get_device_last_used"] = AUTH_ERROR
    with pytest.raises(ToolError, match="garmin-login.sh"):
        await health.device_status(service)
