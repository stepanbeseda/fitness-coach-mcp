"""The audit log records events and never a secret, a code, a tool argument or a tool result."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit

from conftest import CLAUDE, FUNNEL_ANTHROPIC, PASSWORD, pkce_pair

ARGUMENT_SENTINEL = "sentinel-argument-7c1e"
WRONG_PASSWORD = "wrong password!"


def test_audit_log_holds_no_secrets_or_tool_data(make_harness, tmp_path):
    h = make_harness(audit_path=tmp_path / "audit.jsonl")
    verifier, challenge = pkce_pair()
    page = h.start(challenge=challenge)
    txn = h.txn(page)

    code1 = h.totp()
    assert h.submit(txn, password=WRONG_PASSWORD, code=code1).status_code == 401
    code2 = h.totp()
    done = h.submit(txn, code=code2)
    assert done.status_code == 303
    auth_code = parse_qs(urlsplit(done.headers["location"]).query)["code"][0]

    first = h.exchange(auth_code, verifier).json()
    second = h.refresh(first["refresh_token"]).json()

    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "get-workouts", "arguments": {"note": ARGUMENT_SENTINEL}}}
    answered = h.mcp(second["access_token"], call)
    assert answered.status_code == 200
    assert "called get-workouts" in answered.text

    blocked = h.mcp(second["access_token"], {**call, "params": {"name": "feedback", "arguments": {}}})
    assert blocked.status_code == 200
    assert "error" in blocked.json()

    revoked = h.client.post("/revoke", data={"token": second["refresh_token"], "client_id": CLAUDE},
                            headers=FUNNEL_ANTHROPIC)
    assert revoked.status_code == 200

    text = (tmp_path / "audit.jsonl").read_text()
    secrets = [PASSWORD, WRONG_PASSWORD, verifier, auth_code, first["access_token"], first["refresh_token"],
               second["access_token"], second["refresh_token"], ARGUMENT_SENTINEL, "called get-workouts"]
    for secret in secrets:
        assert secret not in text
    # A wrong-password submit does not burn the TOTP step, so both codes are usually the same value.
    for code in {code1, code2}:
        assert re.search(rf"(?<!\d){code}(?!\d)", text) is None

    events = {json.loads(line)["event"] for line in text.splitlines()}
    assert events >= {
        "gateway.started", "oauth.authorize_started", "auth.login_failed", "auth.login_ok", "oauth.token_issued",
        "oauth.token_refreshed", "mcp.request", "mcp.tool_blocked", "oauth.token_revoked",
    }
