#!/usr/bin/env python3
"""Smoke test of both MCP endpoints through the gateway (stdlib only).

Usage: smoke_test.py BASE_URL HEVY_TOKEN GARMIN_TOKEN
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


def main() -> int:
    base, hevy_token, garmin_token = sys.argv[1:4]
    failures = 0

    section("Hevy: initialize + tools/list")
    hevy = McpSession(f"{base}/hevy/mcp", hevy_token)
    try:
        info = hevy.initialize()
        tools = [t["name"] for t in hevy.request("tools/list")["tools"]]
        print(f"server: {info.get('serverInfo')} | {len(tools)} tools: {', '.join(tools)}")
        assert "feedback" not in tools, "denylisted tool leaked"
        section("Hevy: poslední 3 workouty (z první stránky, seřazeno podle data)")
        text = hevy.call("get-workouts", {"page": 1, "page_size": 10})
        try:
            data = json.loads(text)
            workouts = data if isinstance(data, list) else data.get("workouts", data)
            workouts = sorted(workouts, key=lambda w: w.get("start_time") or w.get("startTime") or "", reverse=True)
            for w in workouts[:3]:
                count = w.get("exercise_count", len(w.get("exercises", [])))
                print(f"- {w.get('start_time') or w.get('startTime')} | {w.get('title')} | "
                      f"{count} cviků, {w.get('set_count', '?')} sérií, {w.get('duration', '?')}")
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
        section(f"Garmin: recovery snapshot {date.today().isoformat()} (HRV, spánek, readiness, Body Battery)")
        print(garmin.call("get_recovery_snapshot", {})[:3000])
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"FAIL: {exc}")

    print("\nOK" if failures == 0 else f"\n{failures} část(i) selhala")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
