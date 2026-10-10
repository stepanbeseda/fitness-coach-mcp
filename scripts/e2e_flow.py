"""End-to-end OAuth + MCP flow, executed inside the gateway container by
scripts/e2e-test.sh. It talks to the gateway on 127.0.0.1:8080 and simulates the
headers `tailscale serve` adds (tailnet identity / Funnel + client IP)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pyotp

GW = "http://127.0.0.1:8080"
BASE = os.environ["PUBLIC_BASE_URL"]
OWNER = os.environ["OWNER_TAILSCALE_LOGIN"]
PASSWORD = os.environ["E2E_PASSWORD"]
TOTP = pyotp.TOTP(open("/run/secrets/gateway_totp_secret").read().strip())
CLAUDE = "https://claude.ai/oauth/mcp-oauth-client-metadata"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
TAILNET = {"Tailscale-User-Login": OWNER, "X-Forwarded-For": "100.64.0.10"}
FUNNEL = {"Tailscale-Funnel-Request": "?1", "X-Forwarded-For": "160.79.104.20"}
FUNNEL_OTHER = {"Tailscale-Funnel-Request": "?1", "X-Forwarded-For": "198.51.100.9"}

failures: list[str] = []
step_offset = -1


def check(condition: bool, label: str) -> None:
    print(("  ok   " if condition else "  FAIL ") + label)
    if not condition:
        failures.append(label)


def next_code() -> str:
    global step_offset
    code = TOTP.generate_otp(int(time.time() // 30) + step_offset)
    step_offset += 1
    return code


def oauth(client: httpx.Client, resource: str) -> dict:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    page = client.get(f"{GW}/authorize", headers=TAILNET, params={
        "response_type": "code", "client_id": CLAUDE, "redirect_uri": CALLBACK, "state": "e2e",
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": resource, "scope": "mcp"})
    check(page.status_code == 200, f"authorize page for {resource}")
    txn = re.search(r'name="txn" value="([^"]+)"', page.text).group(1)
    # The CSRF cookie is Secure (browsers reach the gateway over https via tailscale
    # serve); this client speaks plain http to 127.0.0.1, so send it like a browser would.
    cookie = page.headers["set-cookie"].split(";", 1)[0]
    client.cookies.clear()
    done = client.post(f"{GW}/authorize", headers={**TAILNET, "cookie": cookie}, data={
        "txn": txn, "password": PASSWORD, "totp": next_code(), "decision": "approve"})
    check(done.status_code == 303, "login + consent redirects back to Claude")
    query = parse_qs(urlsplit(done.headers["location"]).query)
    check(query.get("iss") == [BASE], "authorization response carries iss")
    token = client.post(f"{GW}/token", headers=FUNNEL, data={
        "grant_type": "authorization_code", "code": query["code"][0], "code_verifier": verifier,
        "client_id": CLAUDE, "redirect_uri": CALLBACK, "resource": resource})
    check(token.status_code == 200, "token exchange from Anthropic's range")
    return token.json()


def rpc(client: httpx.Client, path: str, token: str, method: str, params: dict | None = None,
        session: str | None = None, msg_id: int | None = 1) -> tuple[httpx.Response, dict | None]:
    headers = {**FUNNEL, "authorization": f"Bearer {token}", "accept": "application/json, text/event-stream",
               "mcp-protocol-version": "2025-06-18"}
    if session:
        headers["mcp-session-id"] = session
    message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if msg_id is not None:
        message["id"] = msg_id
    response = client.post(f"{GW}{path}", headers=headers, json=message)
    body = None
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                body = json.loads(line[5:].strip())
    elif response.content:
        body = response.json()
    return response, body


def exercise_backend(client: httpx.Client, name: str, probe_tool: str, probe_args: dict) -> str:
    print(f"\n[{name}]")
    resource = f"{BASE}/{name}/mcp"
    tokens = oauth(client, resource)
    access = tokens["access_token"]
    path = f"/{name}/mcp"
    init, body = rpc(client, path, access, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}})
    check(init.status_code == 200 and body and "result" in body, "initialize")
    session = init.headers.get("mcp-session-id")
    rpc(client, path, access, "notifications/initialized", session=session, msg_id=None)
    listed, body = rpc(client, path, access, "tools/list", session=session, msg_id=2)
    tools = [t["name"] for t in (body or {}).get("result", {}).get("tools", [])]
    check(listed.status_code == 200 and len(tools) > 0, f"tools/list → {len(tools)} tools")
    print("       " + ", ".join(tools))
    if name == "hevy":
        check("feedback" not in tools, "denylisted 'feedback' tool hidden")
        blocked, body = rpc(client, path, access, "tools/call", {"name": "feedback", "arguments": {}},
                            session=session, msg_id=3)
        check(bool(body and body.get("error")), "denylisted tool call blocked")
    called, body = rpc(client, path, access, "tools/call", {"name": probe_tool, "arguments": probe_args},
                       session=session, msg_id=4)
    shaped = bool(body) and ("result" in body or "error" in body)
    check(called.status_code == 200 and shaped, f"tools/call {probe_tool} returns a JSON-RPC response")
    text = json.dumps(body, ensure_ascii=False)[:300] if body else called.text[:300]
    print(f"       {text}")
    other = rpc(client, path, access, "tools/list", msg_id=5)[0]
    denied = client.post(f"{GW}{path}", headers={**FUNNEL_OTHER, "authorization": f"Bearer {access}"},
                         json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    check(denied.status_code == 403, "same token from a non-Anthropic public IP is refused")
    refreshed = client.post(f"{GW}/token", headers=FUNNEL, data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": CLAUDE})
    check(refreshed.status_code == 200, "refresh token rotation")
    del other
    return refreshed.json()["access_token"]


def tool_result(body: dict | None) -> tuple[bool, object]:
    """(call succeeded, JSON payload of the first text content or None) of a tools/call response."""
    if not isinstance(body, dict) or not isinstance(body.get("result"), dict):
        return False, None
    result = body["result"]
    is_error = result.get("isError") is True
    try:
        payload = json.loads(result["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        payload = None
    return not is_error, payload


def tool_error_text(body: dict | None) -> str | None:
    """The first text content of a tools/call result with isError true, else None (also for malformed bodies)."""
    if not isinstance(body, dict) or not isinstance(body.get("result"), dict):
        return None
    result = body["result"]
    if result.get("isError") is not True:
        return None
    try:
        text = result["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None
    return text if isinstance(text, str) else None


def exercise_coach_memory(client: httpx.Client, token: str) -> None:
    """Round trip on throwaway data: set_goal, then get_coach_context returns the goal."""
    _, body = rpc(client, "/coach/mcp", token, "tools/call", {
        "name": "set_goal", "arguments": {"title": "E2E goal", "target_date": "2030-01-01"}}, msg_id=6)
    ok, _ = tool_result(body)
    check(ok, "coach set_goal stores a goal")
    _, body = rpc(client, "/coach/mcp", token, "tools/call", {"name": "get_coach_context", "arguments": {}},
                  msg_id=7)
    ok, data = tool_result(body)
    goals = data.get("goals", []) if isinstance(data, dict) else []
    check(ok and any(isinstance(g, dict) and g.get("title") == "E2E goal" for g in goals),
          "coach get_coach_context returns the stored goal")


def exercise_daily_briefing(client: httpx.Client, token: str) -> None:
    """get_daily_briefing reaches garmin-mcp with coach-mcp's read-only token (no Garmin login in e2e)."""
    _, body = rpc(client, "/coach/mcp", token, "tools/call", {"name": "get_daily_briefing", "arguments": {}},
                  msg_id=8)
    ok, data = tool_result(body)
    data = data if isinstance(data, dict) else {}
    garmin = data.get("garmin") if isinstance(data.get("garmin"), dict) else {}
    calls = garmin.get("calls") if isinstance(garmin.get("calls"), dict) else {}
    alerts = {a.get("key"): a.get("holds") for a in data.get("alerts", []) if isinstance(a, dict)}
    check(ok and data.get("verdict") == "yellow" and set(calls.values()) == {"auth_error"}
          and alerts.get("garmin_auth") is True,
          "coach get_daily_briefing reads garmin-mcp (not logged in: yellow, garmin_auth alert)")


def exercise_garmin_gateway_write(client: httpx.Client, token: str) -> None:
    """The gateway's token keeps full access: a write tool runs and stops at the missing Garmin login."""
    _, body = rpc(client, "/garmin/mcp", token, "tools/call", {
        "name": "unschedule_workout", "arguments": {"schedule_id": 1}}, msg_id=11)
    result = body.get("result") if isinstance(body, dict) else None
    content = result.get("content") if isinstance(result, dict) else None
    first = content[0] if isinstance(content, list) and content and isinstance(content[0], dict) else {}
    text = str(first.get("text", ""))
    check(isinstance(result, dict) and result.get("isError") is True and "scripts/garmin-login.sh" in text
          and "read-only access" not in text,
          "garmin write tool runs with the gateway's token (stops at the missing Garmin login)")


def exercise_history(client: httpx.Client, token: str) -> None:
    """The history tools answer from the empty store (the nightly job is off in e2e)."""
    _, body = rpc(client, "/coach/mcp", token, "tools/call", {
        "name": "get_trends", "arguments": {"metrics": ["hrv_last_night_ms"]}}, msg_id=9)
    ok, data = tool_result(body)
    metrics = data.get("metrics") if isinstance(data, dict) else None
    hrv = metrics.get("hrv_last_night_ms") if isinstance(metrics, dict) else None
    check(ok and isinstance(hrv, dict) and hrv.get("summary") == {"n": 0},
          "coach get_trends answers from the empty history")
    _, body = rpc(client, "/coach/mcp", token, "tools/call", {"name": "get_weekly_report", "arguments": {}},
                  msg_id=10)
    ok, data = tool_result(body)
    strength = data.get("strength") if isinstance(data, dict) else None
    check(ok and isinstance(strength, dict) and strength.get("available") is False,
          "coach get_weekly_report marks sections without data")
    _, body = rpc(client, "/coach/mcp", token, "tools/call", {"name": "get_strength_progress", "arguments": {}},
                  msg_id=11)
    ok, data = tool_result(body)
    check(ok and isinstance(data, dict) and data.get("available") is False and data.get("exercises") == [],
          "coach get_strength_progress answers from the empty history")
    _, body = rpc(client, "/coach/mcp", token, "tools/call", {
        "name": "get_correlations", "arguments": {"metrics": ["acute_load", "hrv_last_night_ms"]}}, msg_id=12)
    ok, data = tool_result(body)
    pairs = data.get("pairs") if isinstance(data, dict) else None
    first = pairs[0] if isinstance(pairs, list) and pairs and isinstance(pairs[0], dict) else {}
    caveat = data.get("caveat") if isinstance(data, dict) else None
    check(ok and first.get("available") is False and first.get("reason") == "too_few_pairs"
          and isinstance(caveat, str) and "not a cause" in caveat,
          "coach get_correlations answers from the empty history with the caveat")


def exercise_goal_progress(client: httpx.Client, token: str) -> None:
    """A goal with a metric, then get_goal_progress from the empty history (the nightly job is off in e2e)."""
    _, body = rpc(client, "/coach/mcp", token, "tools/call", {"name": "set_goal", "arguments": {
        "title": "E2E 10k", "metric": "race_10k", "target_value": "45:00", "target_date": "2030-01-01"}}, msg_id=13)
    ok, data = tool_result(body)
    goal = data.get("goal") if isinstance(data, dict) else None
    check(ok and isinstance(goal, dict) and goal.get("metric") == "race_10k" and goal.get("target_value") == "45:00",
          "coach set_goal stores a metric and a target value")
    _, body = rpc(client, "/coach/mcp", token, "tools/call", {"name": "get_goal_progress", "arguments": {}},
                  msg_id=17)
    ok, data = tool_result(body)
    goals = data.get("goals") if isinstance(data, dict) else None
    mine = next((g for g in goals if isinstance(g, dict) and g.get("title") == "E2E 10k"), {}) if isinstance(goals, list) else {}
    target = mine.get("target_value") if isinstance(mine.get("target_value"), dict) else {}
    without = data.get("goals_without_metric") if isinstance(data, dict) else None
    check(ok and mine.get("available") is False and mine.get("reason") == "no_recent_value"
          and target.get("time") == "45:00" and isinstance(without, int) and without >= 1,
          "coach get_goal_progress answers from the empty history (no_recent_value)")


def exercise_weather(client: httpx.Client, token: str) -> None:
    """get_weather_forecast fails cleanly: no home location configured, and no internet in the e2e stack."""
    _, body = rpc(client, "/coach/mcp", token, "tools/call", {"name": "get_weather_forecast", "arguments": {}},
                  msg_id=14)
    check("No home location configured" in (tool_error_text(body) or ""),
          "coach get_weather_forecast without a location: clear tool error")
    _, body = rpc(client, "/coach/mcp", token, "tools/call", {
        "name": "get_weather_forecast", "arguments": {"latitude": 51.48, "longitude": 0.0}}, msg_id=15)
    text = tool_error_text(body) or ""
    check("Weather service unreachable" in text or "Weather service timed out" in text,
          "coach get_weather_forecast without internet: short tool error")
    _, body = rpc(client, "/coach/mcp", token, "tools/call", {"name": "get_coach_context", "arguments": {}},
                  msg_id=16)
    ok, _ = tool_result(body)
    check(ok, "coach still answers after the weather error")


def main() -> int:
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        public = client.get(f"{GW}/authorize", headers=FUNNEL)
        check(public.status_code == 403, "login page refused through Funnel")
        hevy = exercise_backend(client, "hevy", "get-workouts", {"page": 1, "page_size": 3})
        garmin = exercise_backend(client, "garmin", "get_recovery_snapshot", {})
        exercise_garmin_gateway_write(client, garmin)
        cross = client.post(f"{GW}/garmin/mcp", headers={**FUNNEL, "authorization": f"Bearer {hevy}"},
                            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        check(cross.status_code == 401, "Hevy token rejected by the Garmin endpoint (audience)")
        # The third OAuth login below uses TOTP step offset +1, the largest the gateway accepts
        # (current +/- 1 step): do not add a fourth login to this script.
        coach = exercise_backend(client, "coach", "get_coach_context", {})
        exercise_coach_memory(client, coach)
        exercise_daily_briefing(client, coach)
        exercise_history(client, coach)
        exercise_goal_progress(client, coach)
        exercise_weather(client, coach)
        cross = client.post(f"{GW}/coach/mcp", headers={**FUNNEL, "authorization": f"Bearer {garmin}"},
                            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        check(cross.status_code == 401, "Garmin token rejected by the coach endpoint (audience)")
    print(f"\n{'E2E OK' if not failures else 'E2E FAILED: ' + '; '.join(failures)}")
    return 1 if failures else 0


sys.exit(main())
