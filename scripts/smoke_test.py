#!/usr/bin/env python3
"""Smoke test of the three MCP endpoints through the gateway (stdlib only).

Usage: smoke_test.py BASE_URL HEVY_TOKEN GARMIN_TOKEN COACH_TOKEN
Tokens come from `scripts/admin.sh mint-token` (see scripts/smoke-test.sh).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import date

PROTOCOL = "2025-06-18"


class McpSession:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self.session: str | None = None
        self._id = 0

    def _post(self, message: dict) -> tuple[int, dict | None]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL,
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(message).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                self.session = resp.headers.get("Mcp-Session-Id") or self.session
                body = resp.read().decode()
                ctype = resp.headers.get("Content-Type", "")
                status = resp.status
        except urllib.error.HTTPError as exc:
            return exc.code, None
        if not body:
            return status, None
        if ctype.startswith("text/event-stream"):
            for line in body.splitlines():
                if line.startswith("data:"):
                    data = json.loads(line[5:].strip())
                    if data.get("id") == message.get("id"):
                        return status, data
            return status, None
        return status, json.loads(body)

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        status, data = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        if status != 200 or data is None:
            raise RuntimeError(f"{method}: HTTP {status}")
        if "error" in data:
            raise RuntimeError(f"{method}: {data['error']}")
        return data["result"]

    def initialize(self) -> dict:
        result = self.request(
            "initialize",
            {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "smoke-test", "version": "1"}},
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def call(self, tool: str, arguments: dict) -> str:
        result = self.request("tools/call", {"name": tool, "arguments": arguments})
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise RuntimeError(f"{tool} returned an error: {text[:500]}")
        return text


def section(title: str) -> None:
    print(f"\n=== {title}")


def garmin_new_signals(garmin: McpSession) -> None:
    """PR 19: running tolerance, the newest strength and outdoor activity (units vs. Garmin Connect)."""
    data = json.loads(garmin.call("get_running_tolerance", {}))
    weeks = data.get("weeks") or []
    if weeks:
        latest = weeks[-1]
        fields = ", ".join(key for key in latest if key != "date")
        print(f"running_tolerance: {data['count']} weeks | latest {latest.get('date')} | fields {fields}")
    else:
        print(f"running_tolerance: {json.dumps(data, ensure_ascii=False)[:300]}")
    data = json.loads(garmin.call("get_race_predictions", {}))
    days = data.get("days") or []
    print(f"get_race_predictions: {data.get('count', 0)} days | latest {days[-1]['date'] if days else '-'}")
    rows = json.loads(garmin.call("list_activities", {"days": 30}))["activities"]
    strength = next((a for a in rows if "strength" in str(a.get("type") or "")), None)
    outdoor = next((a for a in rows if a.get("distance_km") and a is not strength), None)
    if strength is None:
        print("exercise_sets: no strength activity in the last 30 days")
    else:
        detail = json.loads(garmin.call("get_activity", {"activity_id": strength["id"]}))
        sets = detail.get("exercise_sets", {})
        counts = f"active_sets {detail.get('active_sets')} | total_reps {detail.get('total_reps')}"
        if "count" in sets:
            first = json.dumps(sets["sets"][0], ensure_ascii=False)
            print(f"exercise_sets of {strength.get('start_local')}: {sets['count']} sets | "
                  f"{len(sets.get('exercises', []))} exercises | first set {first} | {counts}")
        else:
            text = json.dumps(sets, ensure_ascii=False)
            print(f"exercise_sets of {strength.get('start_local')}: {text} | {counts}")
    if outdoor is None:
        print("weather: no activity with a distance in the last 30 days")
    else:
        detail = json.loads(garmin.call("get_activity", {"activity_id": outdoor["id"]}))
        weather = json.dumps(detail.get("weather", {}), ensure_ascii=False)
        print(f"weather of {outdoor.get('start_local')} ({outdoor.get('type')}): {weather}")


def main() -> int:
    base, hevy_token, garmin_token, coach_token = sys.argv[1:5]
    failures = 0

    section("Hevy: initialize + tools/list")
    hevy = McpSession(f"{base}/hevy/mcp", hevy_token)
    try:
        info = hevy.initialize()
        tools = [t["name"] for t in hevy.request("tools/list")["tools"]]
        print(f"server: {info.get('serverInfo')} | {len(tools)} tools: {', '.join(tools)}")
        assert "feedback" not in tools, "denylisted tool leaked"
        section("Hevy: last 3 workouts (from the first page, sorted by date)")
        text = hevy.call("get-workouts", {"page": 1, "page_size": 10})
        try:
            data = json.loads(text)
            workouts = data if isinstance(data, list) else data.get("workouts", data)
            workouts = sorted(workouts, key=lambda w: w.get("start_time") or w.get("startTime") or "", reverse=True)
            for w in workouts[:3]:
                count = w.get("exercise_count", len(w.get("exercises", [])))
                print(f"- {w.get('start_time') or w.get('startTime')} | {w.get('title')} | "
                      f"{count} exercises, {w.get('set_count', '?')} sets, {w.get('duration', '?')}")
        except (ValueError, AttributeError, TypeError):
            print(text[:1500])
    except Exception as exc:  # noqa: BLE001 - report and continue with Garmin
        failures += 1
        print(f"FAIL: {exc}")

    section("Garmin: initialize + tools/list")
    garmin = McpSession(f"{base}/garmin/mcp", garmin_token)
    try:
        info = garmin.initialize()
        tools = [t["name"] for t in garmin.request("tools/list")["tools"]]
        print(f"server: {info.get('serverInfo')} | {len(tools)} tools: {', '.join(tools)}")
        section(f"Garmin: recovery snapshot {date.today().isoformat()} (HRV, sleep, readiness, Body Battery)")
        print(garmin.call("get_recovery_snapshot", {})[:3000])
        section("Garmin: device status (last upload)")
        print(garmin.call("get_device_status", {})[:1000])
        section("Garmin: performance metrics (race predictions, heart-rate zones)")
        text = garmin.call("get_performance_metrics", {})
        try:
            data = json.loads(text)
            for key in ("race_predictions", "heart_rate_zones"):
                print(f"{key}: {json.dumps(data.get(key), ensure_ascii=False)[:2000]}")
        except (ValueError, AttributeError):
            print(text[:3000])
        section("Garmin: body composition over the last 30 days (summary) and today's nutrition/hydration")
        text = garmin.call("get_body_composition", {})
        try:
            data = json.loads(text)
            print(f"body_composition: {data.get('count')} days, summary={json.dumps(data.get('summary'), ensure_ascii=False)}"
                  if data.get("count") else f"body_composition: {json.dumps(data, ensure_ascii=False)[:500]}")
        except (ValueError, AttributeError):
            print(text[:1500])
        print(f"nutrition: {garmin.call('get_nutrition', {})[:1500]}")
        section("Garmin: PR 19 signals (running tolerance, strength sets, activity weather)")
        garmin_new_signals(garmin)
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"FAIL: {exc}")

    section("Coach: initialize + tools/list")
    coach = McpSession(f"{base}/coach/mcp", coach_token)
    coach_ready = False
    try:
        info = coach.initialize()
        coach_ready = True
        tools = [t["name"] for t in coach.request("tools/list")["tools"]]
        print(f"server: {info.get('serverInfo')} | {len(tools)} tools: {', '.join(tools)}")
        section("Coach: get_coach_context (counts only)")
        data = json.loads(coach.call("get_coach_context", {}))
        print(f"today: {data['today']} | week: {data['week']['iso_week']} ({data['week']['start']} - {data['week']['end']})")
        print(", ".join(f"{key}: {len(data[key])}" for key in ("goals", "constraints", "sessions", "notes")))
        section("Coach: get_daily_briefing (verdict, missing data and Garmin state only)")
        data = json.loads(coach.call("get_daily_briefing", {}))
        print(f"briefing: date {data['date']} | verdict {data['verdict']} | missing {', '.join(data['missing']) or '-'} | "
              f"garmin auth {data['garmin']['auth']} | calls {','.join(data['garmin']['calls'].values())}")
        section("Coach: history (counts and availability only)")
        data = json.loads(coach.call("get_trends", {"metrics": ["hrv_last_night_ms"], "period": "day"}))
        print(f"get_trends: {data['start']} - {data['end']} | days with data {data['days_with_data']} | "
              f"nights with HRV {data['metrics']['hrv_last_night_ms']['summary']['n']}")
        data = json.loads(coach.call("get_weekly_report", {}))
        endurance = data["endurance"]
        print(f"get_weekly_report: {data['week']['iso_week']} | activities {endurance.get('total', {}).get('count', 0)} | "
              f"sports {', '.join(endurance.get('by_sport', {})) or '-'} | "
              + ", ".join(f"{key} {'no' if data[key].get('available') is False else 'yes'}"
                          for key in ("sleep", "hrv", "resting_hr", "adherence", "strength")))
        data = json.loads(coach.call("get_goal_progress", {}))
        parts = "".join(
            f" | {g['metric']} available {'yes' if g['available'] else 'no'} reason {g['reason'] or '-'} "
            f"on_track {'-' if g['on_track'] is None else str(g['on_track']).lower()}"
            for g in data["goals"]
        )
        print(f"get_goal_progress: {len(data['goals'])} goals{parts} | without metric {data['goals_without_metric']}")
        data = json.loads(coach.call("get_strength_progress", {}))
        covered = data["coverage"] or {}
        print(f"get_strength_progress: {data['start']} - {data['end']} | "
              f"available {'yes' if data['available'] else 'no'} | exercises {len(data['exercises'])} | "
              f"coverage {covered.get('from', '-')} - {covered.get('through', '-')}")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"FAIL: {exc}")

    if coach_ready:
        section("Coach: weather (no coordinates)")
        try:
            data = json.loads(coach.call("get_weather_forecast", {}))
            first = data["days"][0] if data["days"] else {}
            summary = first.get("summary", {})
            print(f"weather: {data['timezone']} | days {len(data['days'])} | {first.get('date')} "
                  f"sunrise {first.get('sunrise')} sunset {first.get('sunset')} | "
                  f"temperature {summary.get('temperature_min_c')} to {summary.get('temperature_max_c')} °C | "
                  f"outdoor windows {len(data['best_outdoor_windows'])} | fetched {data['fetched_at']}")
        except Exception as exc:  # noqa: BLE001
            if "No home location configured" in str(exc):
                print("weather: no location set (COACH_HOME_LATITUDE and COACH_HOME_LONGITUDE in .env)")
            else:
                failures += 1
                print(f"FAIL: {exc}")

    print("\nOK" if failures == 0 else f"\n{failures} part(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
