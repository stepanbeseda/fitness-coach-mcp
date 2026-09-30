import json

from conftest import FUNNEL_PUBLIC, TAILNET


def call(name, msg_id=7):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": name, "arguments": {}}}


def test_tools_list_filters_denylisted_tools_json(h):
    token = h.tokens()["access_token"]
    r = h.mcp(token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert names == ["get-workouts"]


def test_tools_list_filters_denylisted_tools_sse(make_harness):
    h = make_harness(sse=True)
    token = h.tokens()["access_token"]
    r = h.mcp(token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.headers["content-type"].startswith("text/event-stream")
    data_lines = [line[6:] for line in r.text.splitlines() if line.startswith("data: ")]
    names = [t["name"] for t in json.loads(data_lines[0])["result"]["tools"]]
    assert names == ["get-workouts"]
    assert "event: message" in r.text


def test_denylisted_tool_call_is_blocked_before_backend(h):
    token = h.tokens()["access_token"]
    before = len(h.backends["hevy"].requests)
    r = h.mcp(token, call("feedback"))
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32602
    assert len(h.backends["hevy"].requests) == before


def test_allowed_tool_call_and_session_header_passthrough(h):
    token = h.tokens()["access_token"]
    r = h.mcp(token, call("get-workouts"), headers={**TAILNET, "mcp-session-id": "sess-9",
                                                  "mcp-protocol-version": "2025-06-18",
                                                  "cookie": "a=b", "origin": "https://claude.ai"})
    assert r.status_code == 200
    assert r.json()["result"]["content"][0]["text"] == "called get-workouts"
    assert r.headers["mcp-session-id"] == "sess-1"
    forwarded = h.backends["hevy"].requests[-1]
    assert forwarded.headers["mcp-session-id"] == "sess-9"
    assert forwarded.headers["mcp-protocol-version"] == "2025-06-18"
    assert "cookie" not in forwarded.headers
    assert "origin" not in forwarded.headers
    assert "tailscale-user-login" not in forwarded.headers


def test_valid_token_from_unexpected_network_is_refused(h):
    token = h.tokens()["access_token"]
    r = h.mcp(token, headers=FUNNEL_PUBLIC)
    assert r.status_code == 403


def test_notifications_and_stream_and_delete(h):
    token = h.tokens()["access_token"]
    note = h.mcp(token, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202
    stream = h.mcp(token, method="GET")
    assert stream.status_code == 200 and "notifications/ping" in stream.text
    delete = h.mcp(token, method="DELETE")
    assert delete.status_code == 200
    assert h.mcp(token, method="PUT").status_code == 405


def test_backend_unavailable_and_misconfigured(h):
    token = h.tokens()["access_token"]
    h.backends["hevy"].down = True
    assert h.mcp(token).status_code == 502
    h.backends["hevy"].down = False
    h.backends["hevy"].reject_auth = True
    r = h.mcp(token)
    assert r.status_code == 502 and r.json()["error"] == "backend_misconfigured"
    assert "www-authenticate" not in r.headers


def test_invalid_bodies(h):
    token = h.tokens()["access_token"]
    hdrs = {**TAILNET, "authorization": f"Bearer {token}", "content-type": "application/json"}
    assert h.client.post("/hevy/mcp", content=b"{not json", headers=hdrs).status_code == 400
    assert h.client.post("/hevy/mcp", content=b"[1, 2]", headers=hdrs).status_code == 400
    big = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "x", "params": {"p": "a" * (1024 * 1024)}})
    assert h.client.post("/hevy/mcp", content=big.encode(), headers=hdrs).status_code == 413


def test_bearer_scheme_required(h):
    r = h.client.post("/hevy/mcp", json={}, headers={**TAILNET, "authorization": "Basic abc"})
    assert r.status_code == 401
    assert 'error="invalid_request"' in r.headers["www-authenticate"]


def test_mcp_rate_limit_per_grant(make_harness):
    h = make_harness(mcp_rate=60, mcp_burst=3)
    token = h.tokens()["access_token"]
    codes = [h.mcp(token).status_code for _ in range(6)]
    assert codes[:3] == [200, 200, 200]
    assert 429 in codes[3:]
