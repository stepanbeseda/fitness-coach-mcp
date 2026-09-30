"""Recovery / wellness data: shaping of raw Garmin payloads + the tool logic."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from statistics import mean
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .formatting import (
    as_dict,
    as_list,
    clean,
    hours,
    km,
    local_ms_to_iso,
    local_str,
    minutes,
    num,
    phrase_label,
    pick_primary_device,
    resolve_date,
    resolve_range,
)
from .garmin_client import GarminService, is_section_error, section_error

log = logging.getLogger("garmin_coach_mcp.health")

UNAVAILABLE = {"available": False}


# ---------------------------------------------------------------- readiness


def _readiness_snapshots(raw: Any) -> list[dict[str, Any]]:
    return [s for s in as_list(raw) if isinstance(s, dict)]


def _latest(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    with_ts = [s for s in snapshots if s.get("timestamp")]
    if with_ts:
        return max(with_ts, key=lambda s: str(s["timestamp"]))
    return snapshots[0]


def shape_readiness(raw: Any, *, brief: bool = False) -> dict[str, Any]:
    snapshots = _readiness_snapshots(raw)
    if not snapshots:
        return dict(UNAVAILABLE)
    latest = _latest(snapshots)
    morning = next((s for s in snapshots if s.get("inputContext") == "AFTER_WAKEUP_RESET"), None)

    recovered = latest.get("recoveryTimeChangePhrase") == "REACHED_ZERO"
    recovery_min = 0 if recovered else latest.get("recoveryTime")

    def factor(prefix: str) -> dict[str, Any]:
        return {
            "percent": num(latest.get(f"{prefix}FactorPercent"), 0),
            "feedback": latest.get(f"{prefix}FactorFeedback"),
        }

    out: dict[str, Any] = {
        "score": num(latest.get("score"), 0),
        "level": latest.get("level"),
        "feedback": latest.get("feedbackShort"),
        "time_local": local_str(latest.get("timestampLocal")),
        "recovery_time_h": num(recovery_min / 60, 1) if isinstance(recovery_min, (int, float)) else None,
        "factors": {
            "sleep": {**factor("sleepScore"), "sleep_score": num(latest.get("sleepScore"), 0)},
            "sleep_history": factor("sleepHistory"),
            "recovery_time": factor("recoveryTime"),
            "acute_load": {**factor("acwr"), "acute_load": num(latest.get("acuteLoad"), 0)},
            "hrv": {**factor("hrv"), "hrv_weekly_avg_ms": num(latest.get("hrvWeeklyAverage"), 0)},
            "stress_history": factor("stressHistory"),
        },
    }
    if morning is not None and morning is not latest:
        out["morning_score"] = num(morning.get("score"), 0)
        out["morning_level"] = morning.get("level")
    if not brief:
        out["date"] = latest.get("calendarDate")
        out["feedback_long"] = latest.get("feedbackLong")
        out["snapshots_today"] = len(snapshots)
    return clean(out)


# ---------------------------------------------------------------------- HRV


def shape_hrv(raw: Any) -> dict[str, Any]:
    summary = as_dict(as_dict(raw).get("hrvSummary"))
    if not summary:
        return dict(UNAVAILABLE)
    baseline = as_dict(summary.get("baseline"))
    return clean(
        {
            "last_night_avg_ms": num(summary.get("lastNightAvg"), 0),
            "last_night_5min_high_ms": num(summary.get("lastNight5MinHigh"), 0),
            "weekly_avg_ms": num(summary.get("weeklyAvg"), 0),
            "status": summary.get("status"),
            "feedback": summary.get("feedbackPhrase"),
            "baseline_balanced_ms": [num(baseline.get("balancedLow"), 0), num(baseline.get("balancedUpper"), 0)]
            if baseline.get("balancedLow") is not None
            else None,
            "baseline_low_upper_ms": num(baseline.get("lowUpper"), 0),
        }
    )


def _hrv_day(summary: dict[str, Any]) -> dict[str, Any]:
    baseline = as_dict(summary.get("baseline"))
    return clean(
        {
            "date": summary.get("calendarDate"),
            "last_night_avg_ms": num(summary.get("lastNightAvg"), 0),
            "weekly_avg_ms": num(summary.get("weeklyAvg"), 0),
            "status": summary.get("status"),
            "baseline_balanced_ms": [num(baseline.get("balancedLow"), 0), num(baseline.get("balancedUpper"), 0)]
            if baseline.get("balancedLow") is not None
            else None,
        }
    )


def shape_hrv_trend(summaries: list[dict[str, Any]], start: str, end: str) -> dict[str, Any]:
    days = sorted((_hrv_day(s) for s in summaries if isinstance(s, dict)), key=lambda d: d.get("date") or "")
    nightly = [d["last_night_avg_ms"] for d in days if isinstance(d.get("last_night_avg_ms"), (int, float))]
    out: dict[str, Any] = {"start": start, "end": end, "days": days}
    if nightly:
        latest = days[-1]
        summary: dict[str, Any] = {
            "days_with_data": len(nightly),
            "mean_last_night_ms": num(mean(nightly), 1),
            "min_ms": min(nightly),
            "max_ms": max(nightly),
            "latest_weekly_avg_ms": latest.get("weekly_avg_ms"),
            "latest_status": latest.get("status"),
        }
        if len(nightly) >= 6:
            half = len(nightly) // 2
            first, second = mean(nightly[:half]), mean(nightly[half:])
            summary["first_half_mean_ms"] = num(first, 1)
            summary["second_half_mean_ms"] = num(second, 1)
            summary["change_pct"] = num((second - first) / first * 100, 1) if first else None
        out["summary"] = clean(summary)
    return out


# -------------------------------------------------------------------- sleep

_SLEEP_COMPONENTS = {
    "totalDuration": "total_duration",
    "stress": "stress",
    "awakeCount": "awake_count",
    "remPercentage": "rem_percentage",
    "lightPercentage": "light_percentage",
    "deepPercentage": "deep_percentage",
    "restlessness": "restlessness",
}


def shape_sleep(raw: Any, *, brief: bool = False) -> dict[str, Any]:
    raw = as_dict(raw)
    dto = as_dict(raw.get("dailySleepDTO"))
    if not dto or not dto.get("sleepTimeSeconds"):
        return {**UNAVAILABLE, "date": dto.get("calendarDate")} if dto else dict(UNAVAILABLE)
    scores = as_dict(dto.get("sleepScores"))
    overall = as_dict(scores.get("overall"))
    out: dict[str, Any] = {
        "score": num(overall.get("value"), 0),
        "quality": overall.get("qualifierKey"),
        "duration_h": hours(dto.get("sleepTimeSeconds")),
        "deep_min": minutes(dto.get("deepSleepSeconds"), 0),
        "light_min": minutes(dto.get("lightSleepSeconds"), 0),
        "rem_min": minutes(dto.get("remSleepSeconds"), 0),
        "awake_min": minutes(dto.get("awakeSleepSeconds"), 0),
        "start_local": local_ms_to_iso(dto.get("sleepStartTimestampLocal")),
        "end_local": local_ms_to_iso(dto.get("sleepEndTimestampLocal")),
        "avg_overnight_hrv_ms": num(raw.get("avgOvernightHrv") or dto.get("avgSleepHRV"), 0),
        "resting_hr": num(raw.get("restingHeartRate") or dto.get("restingHeartRate"), 0),
    }
    if not brief:
        spo2 = as_dict(raw.get("wellnessSpO2SleepSummaryDTO"))
        out.update(
            {
                "date": dto.get("calendarDate"),
                "nap_min": minutes(dto.get("napTimeSeconds"), 0) or None,
                "awake_count": dto.get("awakeCount"),
                "restless_moments": raw.get("restlessMomentsCount"),
                "avg_sleep_stress": num(dto.get("avgSleepStress"), 0),
                "avg_respiration": num(dto.get("averageRespirationValue"), 1),
                "avg_spo2": num(dto.get("averageSpO2Value") or dto.get("avgSpO2") or spo2.get("averageSpo2"), 0),
                "lowest_spo2": num(dto.get("lowestSpO2Value") or spo2.get("lowestSpo2"), 0),
                "body_battery_change": num(raw.get("bodyBatteryChange"), 0),
                "feedback": dto.get("sleepScoreFeedback"),
                "insight": dto.get("sleepScoreInsight"),
                "components": {
                    out_key: clean({"value": num(as_dict(scores.get(key)).get("value"), 0),
                                    "quality": as_dict(scores.get(key)).get("qualifierKey")})
                    for key, out_key in _SLEEP_COMPONENTS.items()
                    if scores.get(key)
                },
            }
        )
    return clean(out)


# ------------------------------------------------------------ daily summary


def shape_body_battery_from_summary(summary: dict[str, Any]) -> dict[str, Any]:
    out = clean(
        {
            "latest": num(summary.get("bodyBatteryMostRecentValue"), 0),
            "at_wake": num(summary.get("bodyBatteryAtWakeTime"), 0),
            "high": num(summary.get("bodyBatteryHighestValue"), 0),
            "low": num(summary.get("bodyBatteryLowestValue"), 0),
            "charged": num(summary.get("bodyBatteryChargedValue"), 0),
            "drained": num(summary.get("bodyBatteryDrainedValue"), 0),
        }
    )
    return out or dict(UNAVAILABLE)


def shape_daily_summary(raw: Any) -> dict[str, Any]:
    s = as_dict(raw)
    if not s:
        return dict(UNAVAILABLE)
    return clean(
        {
            "date": s.get("calendarDate"),
            "steps": s.get("totalSteps"),
            "step_goal": s.get("dailyStepGoal"),
            "distance_km": km(s.get("totalDistanceMeters")),
            "calories": {"total": num(s.get("totalKilocalories"), 0), "active": num(s.get("activeKilocalories"), 0)},
            "heart_rate": {
                "resting": s.get("restingHeartRate"),
                "resting_7d_avg": s.get("lastSevenDaysAvgRestingHeartRate"),
                "min": s.get("minHeartRate"),
                "max": s.get("maxHeartRate"),
            },
            "stress": {
                "avg": s.get("averageStressLevel"),
                "max": s.get("maxStressLevel"),
                "rest_min": minutes(s.get("restStressDuration"), 0),
                "low_min": minutes(s.get("lowStressDuration"), 0),
                "medium_min": minutes(s.get("mediumStressDuration"), 0),
                "high_min": minutes(s.get("highStressDuration"), 0),
            },
            "intensity_minutes": {
                "moderate": s.get("moderateIntensityMinutes"),
                "vigorous": s.get("vigorousIntensityMinutes"),
                "weekly_goal": s.get("intensityMinutesGoal"),
            },
            "body_battery": shape_body_battery_from_summary(s) if s.get("bodyBatteryHighestValue") is not None else None,
            "floors_up": num(s.get("floorsAscended"), 0),
            "sleep_h": hours(s.get("sleepingSeconds")),
            "avg_spo2": num(s.get("averageSpo2"), 0),
            "avg_waking_respiration": num(s.get("avgWakingRespirationValue"), 1),
        }
    )


# -------------------------------------------------------------- body battery


def _level_index(entry: dict[str, Any]) -> int:
    for desc in as_list(entry.get("bodyBatteryValueDescriptorDTOList")):
        key = str(as_dict(desc).get("bodyBatteryValueDescriptorKey", "")).lower()
        if "level" in key:
            idx = as_dict(desc).get("bodyBatteryValueDescriptorIndex")
            if isinstance(idx, int):
                return idx
    return 1


def shape_body_battery_days(raw: Any) -> list[dict[str, Any]]:
    days = []
    for entry in as_list(raw):
        if not isinstance(entry, dict):
            continue
        idx = _level_index(entry)
        rows = [r for r in as_list(entry.get("bodyBatteryValuesArray")) if isinstance(r, list) and len(r) > idx]
        rows.sort(key=lambda r: r[0] if isinstance(r[0], (int, float)) else 0)
        levels = [r[idx] for r in rows if isinstance(r[idx], (int, float))]
        days.append(
            clean(
                {
                    "date": entry.get("date"),
                    "charged": num(entry.get("charged"), 0),
                    "drained": num(entry.get("drained"), 0),
                    "start": levels[0] if levels else None,
                    "high": max(levels) if levels else None,
                    "low": min(levels) if levels else None,
                    "latest": levels[-1] if levels else None,
                }
            )
        )
    return sorted(days, key=lambda d: d.get("date") or "")


# ----------------------------------------------------------- training status


def shape_training_status(raw: Any, *, brief: bool = False) -> dict[str, Any]:
    status = as_dict(raw)
    recent = as_dict(status.get("mostRecentTrainingStatus"))
    device = pick_primary_device(recent.get("latestTrainingStatusData"))
    acwr = as_dict(device.get("acuteTrainingLoadDTO"))
    vo2 = as_dict(status.get("mostRecentVO2Max"))
    generic, cycling = as_dict(vo2.get("generic")), as_dict(vo2.get("cycling"))
    if not device and not generic and not cycling:
        return dict(UNAVAILABLE)

    phrase = device.get("trainingStatusFeedbackPhrase")
    label = phrase_label(phrase) or (device.get("trainingStatus") if isinstance(device.get("trainingStatus"), str) else None)
    load = {
        "acute": num(acwr.get("dailyTrainingLoadAcute"), 0),
        "chronic": num(acwr.get("dailyTrainingLoadChronic"), 0),
        "acwr_ratio": num(acwr.get("dailyAcuteChronicWorkloadRatio"), 2),
        "acwr_status": acwr.get("acwrStatus"),
    }
    vo2max = {
        "running": num(generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue"), 1),
        "cycling": num(cycling.get("vo2MaxPreciseValue") or cycling.get("vo2MaxValue"), 1),
    }
    if brief:
        return clean({"status": label, **load, "vo2max_running": vo2max["running"], "vo2max_cycling": vo2max["cycling"]})

    balance = pick_primary_device(as_dict(status.get("mostRecentTrainingLoadBalance")).get("metricsTrainingLoadBalanceDTOMap"))

    def target(prefix: str) -> list[Any] | None:
        lo, hi = balance.get(f"{prefix}TargetMin"), balance.get(f"{prefix}TargetMax")
        return [num(lo, 0), num(hi, 0)] if lo is not None and hi is not None else None

    return clean(
        {
            "date": device.get("calendarDate"),
            "status": label,
            "status_phrase": phrase,
            "since": device.get("sinceDate"),
            "training_paused": device.get("trainingPaused") or None,
            "fitness_trend": device.get("fitnessTrend"),
            "load": {
                **load,
                "acwr_percent": num(acwr.get("acwrPercent"), 0),
                "optimal_chronic_range": [num(acwr.get("minTrainingLoadChronic"), 0), num(acwr.get("maxTrainingLoadChronic"), 0)]
                if acwr.get("minTrainingLoadChronic") is not None
                else None,
            },
            "load_balance_4w": {
                "aerobic_low": num(balance.get("monthlyLoadAerobicLow"), 0),
                "aerobic_high": num(balance.get("monthlyLoadAerobicHigh"), 0),
                "anaerobic": num(balance.get("monthlyLoadAnaerobic"), 0),
                "feedback": balance.get("trainingBalanceFeedbackPhrase"),
                "target_aerobic_low": target("monthlyLoadAerobicLow"),
                "target_aerobic_high": target("monthlyLoadAerobicHigh"),
                "target_anaerobic": target("monthlyLoadAnaerobic"),
            },
            "vo2max": {**vo2max, "measured_on": generic.get("calendarDate")},
        }
    )


# --------------------------------------------------------------------- tools


def _section(results: dict[str, Any], key: str, shaper: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    value = results.get(key)
    if is_section_error(value):
        return section_error(value)
    try:
        return shaper(value)
    except Exception as exc:  # unexpected payload shape: degrade, don't fail the tool
        log.warning("could not shape %s: %s", key, type(exc).__name__)
        return {"error": f"unexpected Garmin response for {key}"}


async def recovery_snapshot(service: GarminService, date: str | None) -> dict[str, Any]:
    d = resolve_date(date, service.settings.timezone).isoformat()
    results = await service.call_many(
        {
            "readiness": lambda c: c.get_training_readiness(d),
            "hrv": lambda c: c.get_hrv_data(d),
            "sleep": lambda c: c.get_sleep_data(d),
            "summary": lambda c: c.get_user_summary(d),
            "status": lambda c: c.get_training_status(d),
        }
    )

    def summary_part(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[Any], dict[str, Any]]:
        return lambda raw: fn(as_dict(raw)) if as_dict(raw) else dict(UNAVAILABLE)

    return {
        "date": d,
        "readiness": _section(results, "readiness", lambda r: shape_readiness(r, brief=True)),
        "hrv": _section(results, "hrv", shape_hrv),
        "sleep": _section(results, "sleep", lambda r: shape_sleep(r, brief=True)),
        "body_battery": _section(results, "summary", summary_part(shape_body_battery_from_summary)),
        "resting_hr": _section(
            results,
            "summary",
            summary_part(lambda s: clean({"today": s.get("restingHeartRate"), "avg_7d": s.get("lastSevenDaysAvgRestingHeartRate")})
                         or dict(UNAVAILABLE)),
        ),
        "stress": _section(
            results,
            "summary",
            summary_part(lambda s: clean({"avg": s.get("averageStressLevel"), "max": s.get("maxStressLevel")})
                         or dict(UNAVAILABLE)),
        ),
        "training_status": _section(results, "status", lambda r: shape_training_status(r, brief=True)),
    }


async def training_readiness(service: GarminService, date: str | None) -> dict[str, Any]:
    d = resolve_date(date, service.settings.timezone).isoformat()
    raw = await service.call(lambda c: c.get_training_readiness(d))
    return {"date": d, **shape_readiness(raw)}


async def sleep(service: GarminService, date: str | None) -> dict[str, Any]:
    d = resolve_date(date, service.settings.timezone).isoformat()
    raw = await service.call(lambda c: c.get_sleep_data(d))
    return {"date": d, **shape_sleep(raw)}


async def training_status(service: GarminService, date: str | None) -> dict[str, Any]:
    d = resolve_date(date, service.settings.timezone).isoformat()
    raw = await service.call(lambda c: c.get_training_status(d))
    return {"date": d, **shape_training_status(raw)}


async def daily_summary(service: GarminService, date: str | None) -> dict[str, Any]:
    d = resolve_date(date, service.settings.timezone).isoformat()
    raw = await service.call(lambda c: c.get_user_summary(d))
    return {"date": d, **shape_daily_summary(raw)}


async def body_battery(service: GarminService, start_date: str | None, end_date: str | None) -> dict[str, Any]:
    start, end = resolve_range(start_date, end_date, service.settings.timezone, default_days=1, max_days=31)
    raw = await service.call(lambda c: c.get_body_battery(start.isoformat(), end.isoformat()))
    return {"start": start.isoformat(), "end": end.isoformat(), "days": shape_body_battery_days(raw)}


async def hrv_trend(service: GarminService, days: int, end_date: str | None) -> dict[str, Any]:
    if not 1 <= days <= 28:
        raise ToolError("days must be between 1 and 28")
    end = resolve_date(end_date, service.settings.timezone, "end_date")
    start = end - timedelta(days=days - 1)
    s, e = start.isoformat(), end.isoformat()

    raw = await service.call(lambda c: c.get_hrv_data_range(s, e))
    summaries: list[dict[str, Any]] | None = None
    if isinstance(raw, dict) and isinstance(raw.get("hrvSummaries"), list):
        summaries = raw["hrvSummaries"]
    elif isinstance(raw, list):
        summaries = raw
    if summaries is None:  # unknown shape: fall back to one call per day
        dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]
        per_day = await service.call_many({day: (lambda c, day=day: c.get_hrv_data(day)) for day in dates})
        summaries = [
            {**as_dict(as_dict(v).get("hrvSummary")), "calendarDate": day}
            for day, v in per_day.items()
            if not is_section_error(v) and as_dict(as_dict(v).get("hrvSummary"))
        ]
    return shape_hrv_trend(summaries, s, e)
