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


def main() -> int:
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        public = client.get(f"{GW}/authorize", headers=FUNNEL)
        check(public.status_code == 403, "login page refused through Funnel")
        hevy = exercise_backend(client, "hevy", "get-workouts", {"page": 1, "page_size": 3})
        exercise_backend(client, "garmin", "get_recovery_snapshot", {})
        cross = client.post(f"{GW}/garmin/mcp", headers={**FUNNEL, "authorization": f"Bearer {hevy}"},
                            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        check(cross.status_code == 401, "Hevy token rejected by the Garmin endpoint (audience)")
    print(f"\n{'E2E OK' if not failures else 'E2E FAILED: ' + '; '.join(failures)}")
    return 1 if failures else 0


sys.exit(main())
